"""WordPress readiness probes; DNS, HTTP and ownership storage are mocked."""

import asyncio
import base64
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from app import store_wordpress_verification as module
from app import upstream_security, store_guarded_secrets as secret_guard
from app.store_guarded_kubernetes import StoreKubernetesStopped
from app.store_job_guard import StoreJobClaimLost, StoreJobGuardUnavailable
from app.store_kube_connection import KubernetesConnection
import verify_store_guarded_secrets as fixtures
import verify_store_secret_api as ownership


class Stream(httpx.AsyncByteStream):
    def __init__(self, fixture, payload):
        self.fixture, self.payload, self.closed = fixture, payload, False

    async def __aiter__(self):
        self.fixture.started.set()
        if self.fixture.delay:
            await asyncio.sleep(self.fixture.delay)
        yield self.payload

    async def aclose(self):
        self.closed = True


class Fixture(ownership.Fixture):
    def __init__(self):
        super().__init__("secret-uid")
        self.namespace_binding = "namespace-uid"
        self.addresses = ["10.43.20.8"]
        self.resolutions, self.probes, self.streams = [], [], []
        self.after_dns = self.after_probe = None
        self.bad = None
        self.delay = 0
        self.started = asyncio.Event()

    async def require(self, factory, claim, **options):
        if options["expected_namespace_uid"] != self.namespace_binding:
            raise StoreJobClaimLost("PRIVATE NAMESPACE BINDING")
        return await super().require(factory, claim, **options)

    async def resolve(self, host, port):
        assert host == f"wordpress.{fixtures.IDENTITY.name}.svc.cluster.local"
        assert port == 80
        self.resolutions.append((host, port))
        if self.after_dns is not None:
            self.after_dns()
        return self.addresses

    async def wordpress(self, request):
        number = len(self.probes)
        expected = [
            ("/", None),
            (module._CURRENCY_PATH, ("WC_RW_KEY", "WC_RW_SECRET")),
            (module._CURRENCY_PATH, ("WC_RO_KEY", "WC_RO_SECRET")),
            (module._MEDIA_PATH, ("WP_MEDIA_USER", "WP_MEDIA_APP_PASSWORD")),
        ]
        assert number < len(expected)
        path, keys = expected[number]
        assert request.method == "GET" and not request.content and not request.url.query
        assert request.url.host == "10.43.20.8" and request.url.port in (None, 80)
        assert request.url.path == path
        assert request.headers["host"] == "fixture-tea.localtest.me"
        assert request.headers["x-forwarded-proto"] == ("http" if path == "/" else "https")
        if keys is None:
            assert "authorization" not in request.headers
        else:
            pair = ":".join(fixtures.CREDENTIALS._values[key] for key in keys)
            assert request.headers["authorization"] == (
                "Basic " + base64.b64encode(pair.encode()).decode()
            )
        self.probes.append((request.method, path))
        if self.after_probe is not None:
            self.after_probe()
        code = 200
        headers = {"content-type": "text/html" if path == "/" else "application/json"}
        if path == "/":
            payload = b"<!DOCTYPE html><html><body>Fixture store</body></html>"
        elif path == module._CURRENCY_PATH:
            payload = json.dumps({"id": "woocommerce_currency", "value": "INR"}).encode()
        else:
            payload = json.dumps({
                "id": 7, "slug": fixtures.CREDENTIALS._values["WP_MEDIA_USER"],
            }).encode()
        if self.bad == "status" or (self.bad, number) in {
            ("rw-auth", 1), ("ro-auth", 2), ("media-auth", 3),
        }:
            code, payload = 401, b"PRIVATE CREDENTIAL ERROR"
        elif self.bad == "redirect":
            code, headers["location"] = 302, "https://unrelated.example/steal"
        elif self.bad == "html":
            payload = b"Setup not finished"
        elif self.bad == "encoding":
            headers["content-encoding"] = "gzip"
        elif self.bad == "content-type":
            headers["content-type"] = "text/plain"
        elif self.bad == "huge-html":
            payload = b"<html>" + b"x" * (1024 * 1024)
        elif path == module._CURRENCY_PATH:
            if self.bad == "huge-json":
                payload = b" " * (64 * 1024 + 1)
            elif self.bad == "wrong-setting":
                payload = b'{"id":"another_setting","value":"INR"}'
            elif self.bad == "mismatch" and number == 2:
                payload = b'{"id":"woocommerce_currency","value":"USD"}'
            elif self.bad == "duplicate":
                payload = b'{"id":"wrong","id":"woocommerce_currency","value":"INR"}'
            elif self.bad == "malformed":
                payload = b"PRIVATE MALFORMED JSON"
            elif self.bad == "non-finite":
                payload = b'{"id":"woocommerce_currency","value":NaN}'
        elif path == module._MEDIA_PATH:
            if self.bad == "wrong-user":
                payload = b'{"id":7,"slug":"another_store"}'
            elif self.bad == "bool-user":
                payload = json.dumps({
                    "id": True, "slug": fixtures.CREDENTIALS._values["WP_MEDIA_USER"],
                }).encode()
        stream = Stream(self, payload)
        self.streams.append(stream)
        return httpx.Response(code, headers=headers, stream=stream)

    def transport(self, destination):
        return upstream_security.PinnedUpstreamTransport(
            destination, transport=httpx.MockTransport(self.wordpress),
        )

    async def verify(self, **options):
        raw = KubernetesConnection(
            lambda: "fixture.worker.signature", fixtures.TLS,
            transport=httpx.MockTransport(self.peer),
        )
        arguments = {
            "namespace_uid": "namespace-uid", "credentials_secret_uid": "secret-uid",
        }
        arguments.update(options)
        try:
            with patch.object(module, "require_claim", self.require), patch.object(
                secret_guard, "require_claim", self.require,
            ), patch.object(
                upstream_security, "resolve_addresses", self.resolve,
            ), patch.object(module, "PinnedUpstreamTransport", self.transport):
                return await module.verify_store_wordpress(
                    self.factory, fixtures.CLAIM, raw, fixtures.SCOPE, **arguments,
                )
        finally:
            await raw.aclose()
            self.closed = raw._client.is_closed


async def error(kind, operation):
    try:
        await operation
    except kind as caught:
        assert "PRIVATE" not in str(caught)
        assert all(value not in str(caught) for value in fixtures.CREDENTIALS._values.values())
        return caught
    raise AssertionError("Expected " + str(kind))


async def check():
    baseline = asyncio.all_tasks()
    f = Fixture()
    expected = {
        "storefront": True, "woo_rw_key_read": True,
        "woo_ro_key_read": True, "media_login": True,
    }
    assert await f.verify() == expected
    assert len(f.probes) == 4 and len(f.resolutions) == 1
    assert f.closed and all(stream.closed for stream in f.streams)
    assert all(method == "GET" for method, path, body in f.requests)
    print("Four GET probes authenticate separate credentials; only flags return and no business data is written")

    for records in ([], ["127.0.0.1"], ["10.43.20.8", "169.254.169.254"], ["8.8.8.8"]):
        f = Fixture()
        f.addresses = records
        await error(module.StoreWordPressVerificationError, f.verify())
        assert not f.probes and f.closed
    for callback in (
        lambda f: setattr(f, "stored_uid", "replacement"),
        lambda f: f.namespace["metadata"].update(uid="replacement"),
        lambda f: f.secret["metadata"].update(uid="replacement"),
    ):
        f = Fixture()
        f.after_dns = lambda f=f, callback=callback: callback(f)
        await error((StoreKubernetesStopped, module.StoreWordPressVerificationError), f.verify())
        assert not f.probes and f.closed
    print("Only owned Service DNS is resolved; unsafe answers and changed ownership after DNS send no credentials")

    for bad in (
        "status", "rw-auth", "ro-auth", "media-auth", "redirect",
        "html", "encoding", "content-type", "huge-html",
        "huge-json", "wrong-setting", "mismatch", "duplicate", "malformed",
        "non-finite", "wrong-user", "bool-user",
    ):
        f = Fixture()
        f.bad = bad
        await error(module.StoreWordPressVerificationError, f.verify())
        assert len(f.probes) <= 4 and all(stream.closed for stream in f.streams)
        if bad in ("status", "redirect"):
            assert len(f.probes) == 1
    print("Redirects, failed authentication, wrong profiles/settings and oversized or malformed responses reject success")

    for problem in (StoreJobClaimLost("PRIVATE CLAIM"), StoreJobGuardUnavailable("PRIVATE STORAGE")):
        f = Fixture()
        f.before = problem
        caught = await error(StoreKubernetesStopped, f.verify())
        assert not caught.write_may_have_applied and not f.probes and not f.resolutions
    f = Fixture()
    f.after_probe = lambda: setattr(f, "stored_uid", "replacement")
    await error(StoreKubernetesStopped, f.verify())
    assert len(f.probes) == 1
    f = Fixture()
    f.after_probe = lambda: f.secret["metadata"].update(uid="replacement")
    await error(module.StoreWordPressVerificationError, f.verify())
    assert len(f.probes) == 1
    f = Fixture()
    await error(ValueError, f.verify(credentials_secret_uid=None))
    assert not f.requests and not f.probes
    print("Live claims and saved namespace/Secret identities remain required throughout verification")

    f = Fixture()
    f.delay = 1
    with patch.object(module, "_CHECK_TIMEOUT_SECONDS", 0.03):
        await error(module.StoreWordPressVerificationError, f.verify())
    assert len(f.probes) == 1 and f.closed and all(s.closed for s in f.streams)
    f = Fixture()
    f.delay = 1
    task = asyncio.create_task(f.verify())
    await f.started.wait()
    task.cancel()
    await error(asyncio.CancelledError, task)
    assert len(f.probes) == 1 and f.closed and all(s.closed for s in f.streams)
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)
    print("Timeout and cancellation close response streams without retries or leftover tasks")
    print("WordPress verification passed. No live services or database calls were made.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 30))
