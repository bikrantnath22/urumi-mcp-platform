"""Offline checks for one claim-bound store Secret; all HTTP is mocked."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import ssl
import sys
import time
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app import store_guarded_secrets as guarded
from app.store_cluster_scope import StoreClusterScope
from app.store_credentials import StoreCredentials
from app.store_guarded_kubernetes import GuardedKubernetesConnection, StoreKubernetesStopped
from app.store_job_guard import CheckedStoreJob, StoreJobClaimLost, StoreJobGuardUnavailable
from app.store_jobs import StoreJobClaim
from app.store_kube_connection import KubernetesAPIError, KubernetesConnection
from app.store_namespace import StoreNamespace
from app.store_secret import StoreSecret, StoreSecretError


SCOPE = StoreClusterScope("platform", "platform")
IDENTITY = StoreNamespace(uuid4(), 7)
NOW = datetime.now(timezone.utc)
CLAIM = StoreJobClaim(
    operation_id=uuid4(), store_id=IDENTITY.store_id, owner_id=7,
    action="provision", generation=1, attempt=1,
    namespace=IDENTITY.name, release_name="store", name="Fixture tea",
    slug="fixture-tea", url="http://fixture-tea.localtest.me",
    deadline_at=NOW + timedelta(minutes=20), checked_at=NOW,
    lease_expires_at=NOW + timedelta(seconds=60), lease_token=uuid4(),
)
NAMESPACE_ITEM = "/api/v1/namespaces/" + IDENTITY.name
COLLECTION = NAMESPACE_ITEM + "/secrets"
ITEM = COLLECTION + "/store-secrets"
NAMESPACE = SCOPE.namespace_manifest(IDENTITY)
NAMESPACE["metadata"].update(uid="namespace-uid", resourceVersion="17")
NAMESPACE["status"] = {"phase": "Active"}
CREDENTIALS = StoreCredentials.generate(IDENTITY, admin_email="owner@example.test")
BODY = StoreSecret(IDENTITY, SCOPE, "namespace-uid").manifest(CREDENTIALS)
RESPONSE = deepcopy(BODY)
RESPONSE["metadata"].update(uid="secret-uid", resourceVersion="5")
TLS = ssl.create_default_context()


class Fixture:
    def __init__(self, uid=None):
        self.uid = self.stored_uid = uid
        self.calls, self.requests = 0, []
        self.reply = deepcopy(RESPONSE)
        self.missing = False
        self.before = self.after_http = self.before_http = None
        self.http_error = None
        self.delay, self.remaining = 0, 30
        self.closed = False

    def factory(self):
        raise AssertionError("The guard fixture must not connect to a database.")

    async def require(
        self, factory, claim, *,
        expected_namespace_uid, expected_credentials_secret_uid,
    ):
        self.calls += 1
        assert factory == self.factory and claim == CLAIM
        assert expected_namespace_uid == "namespace-uid"
        assert expected_credentials_secret_uid == self.uid
        if self.before is not None:
            raise self.before
        if self.stored_uid != expected_credentials_secret_uid:
            raise StoreJobClaimLost("fixture ownership changed")
        if self.before_http is not None:
            self.before_http()
            self.before_http = None
        return CheckedStoreJob(
            claim=claim, namespace_uid="namespace-uid", for_cleanup=False,
            stop_at=time.monotonic() + self.remaining,
            credentials_secret_uid=self.stored_uid,
        )

    async def peer(self, request):
        self.requests.append((
            request.method, request.url.path,
            json.loads(request.content) if request.content else None,
        ))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.after_http is not None:
            self.after_http()
        if self.http_error is not None:
            raise self.http_error
        if request.url.path == NAMESPACE_ITEM:
            return httpx.Response(200, json=deepcopy(NAMESPACE))
        if self.missing:
            return httpx.Response(404)
        return httpx.Response(
            201 if request.method == "POST" else 200,
            json=deepcopy(self.reply),
        )

    async def run(self, operation, *, base=False):
        raw = KubernetesConnection(
            lambda: "fixture.worker.signature", TLS,
            transport=httpx.MockTransport(self.peer),
        )
        try:
            if base:
                connection = GuardedKubernetesConnection(
                    raw, self.factory, CLAIM, SCOPE, namespace_uid="namespace-uid",
                )
            else:
                connection = guarded.GuardedStoreSecretConnection(
                    raw, self.factory, CLAIM, SCOPE,
                    namespace_uid="namespace-uid", credentials_secret_uid=self.uid,
                )
            with patch.object(guarded, "require_claim", self.require):
                return await operation(connection)
        finally:
            await raw.aclose()
            self.closed = raw._client.is_closed


async def error(expected, call):
    try:
        await call
    except expected as caught:
        assert "fixture ownership" not in str(caught)
        assert "private fixture" not in str(caught)
        assert all(
            value not in str(caught) for value in CREDENTIALS._values.values()
        )
        return caught
    raise AssertionError(f"Expected {expected.__name__}.")


async def check():
    for uid in (None, "secret-uid"):
        f = Fixture(uid)
        assert await f.run(lambda c: c.request("GET", ITEM)) == RESPONSE
        assert f.calls == 2 and len(f.requests) == 1 and f.closed
        f = Fixture(uid)
        assert await f.run(lambda c: c.request("GET", NAMESPACE_ITEM)) == NAMESPACE
        assert f.calls == 2
    f = Fixture()
    f.missing = True
    assert await f.run(lambda c: c.request("GET", ITEM)) is None
    f = Fixture()
    assert await f.run(lambda c: c.request("POST", COLLECTION, body=BODY)) == RESPONSE
    assert f.calls == 2 and f.requests[0][2] == BODY
    print("Unbound reads/creation and bound reads check both identities before and after HTTP")

    for uid, method, target, body in (
        ("secret-uid", "POST", COLLECTION, BODY),
        (None, "GET", COLLECTION, None),
        (None, "GET", COLLECTION + "/another-secret", None),
        (None, "GET", ITEM.replace(IDENTITY.store_id.hex, uuid4().hex), None),
        (None, "GET", ITEM + "?watch=1", None),
        (None, "PATCH", ITEM, {}),
        (None, "DELETE", ITEM, {}),
        (None, "POST", "/api/v1/namespaces", SCOPE.namespace_manifest(IDENTITY)),
    ):
        f = Fixture(uid)
        await error(ValueError, f.run(
            lambda c: c.request(method, target, body=body),
        ))
        assert not f.requests and f.calls == 0
    f = Fixture()
    await error(ValueError, f.run(lambda c: c.request("GET", ITEM), base=True))
    assert not f.requests and f.calls == 0

    for section, key, value in (
        ("labels", "urumi.ai/owner-id", "99"),
        ("annotations", "urumi.ai/namespace-uid", "another-namespace"),
        ("annotations", "urumi.ai/platform-release", "another-platform"),
    ):
        f, altered = Fixture(), deepcopy(BODY)
        altered["metadata"][section][key] = value
        await error(StoreSecretError, f.run(
            lambda c: c.request("POST", COLLECTION, body=altered),
        ))
        assert not f.requests and f.calls == 0
    for change in ("data", "extra-metadata", "immutable"):
        f, altered = Fixture(), deepcopy(BODY)
        if change == "data":
            altered["data"]["DB_PASSWORD"] = "not-base64!"
        elif change == "extra-metadata":
            altered["metadata"]["uid"] = "caller-supplied"
        else:
            altered["immutable"] = False
        await error(ValueError, f.run(
            lambda c: c.request("POST", COLLECTION, body=altered),
        ))
        assert not f.requests and f.calls == 0
    print("Cross-store routes, broader operations, bound creation and altered requests send no HTTP")

    f = Fixture("secret-uid")
    f.missing = True
    await error(StoreSecretError, f.run(lambda c: c.request("GET", ITEM)))
    assert len(f.requests) == 1
    for writing in (False, True):
        for change in ("uid", "ownership", "data", "immutable"):
            f = Fixture(None if writing else "secret-uid")
            if change == "uid":
                f.reply["metadata"]["uid"] = ""
            elif change == "ownership":
                f.reply["metadata"]["annotations"]["urumi.ai/namespace-uid"] = "other"
            elif change == "data":
                f.reply["data"] = {}
            else:
                f.reply["immutable"] = False
            caught = await error(KubernetesAPIError, f.run(
                lambda c: c.request(
                    "POST" if writing else "GET",
                    COLLECTION if writing else ITEM, body=BODY if writing else None,
                ),
            ))
            assert caught.write_may_have_applied == writing and len(f.requests) == 1
    f = Fixture("secret-uid")
    f.reply["metadata"]["uid"] = "replacement-secret"
    await error(KubernetesAPIError, f.run(lambda c: c.request("GET", ITEM)))
    f = Fixture()
    f.reply["data"] = StoreCredentials.generate(
        IDENTITY, admin_email="owner@example.test",
    ).secret_data()
    caught = await error(KubernetesAPIError, f.run(
        lambda c: c.request("POST", COLLECTION, body=BODY),
    ))
    assert caught.write_may_have_applied and len(f.requests) == 1
    print("Missing/replaced Secrets reject reuse; invalid write responses remain uncertain without retry")

    for failure in (
        StoreJobClaimLost("private fixture"),
        StoreJobGuardUnavailable("private fixture"),
    ):
        f = Fixture()
        f.before = failure
        stopped = await error(StoreKubernetesStopped, f.run(
            lambda c: c.request("POST", COLLECTION, body=BODY),
        ))
        assert not stopped.write_may_have_applied and not f.requests
    f = Fixture()
    f.remaining = 0
    await error(StoreKubernetesStopped, f.run(lambda c: c.request("GET", ITEM)))
    assert not f.requests
    for writing in (False, True):
        f = Fixture()
        f.after_http = lambda: setattr(f, "stored_uid", "changed-binding")
        stopped = await error(StoreKubernetesStopped, f.run(
            lambda c: c.request(
                "POST" if writing else "GET",
                COLLECTION if writing else ITEM, body=BODY if writing else None,
            ),
        ))
        assert stopped.write_may_have_applied == writing
        assert len(f.requests) == 1 and f.calls == 2
    print("Lost claims and changed bindings stop access; completed writes retain uncertain status")

    f = Fixture()
    f.http_error = httpx.ReadTimeout("private fixture")
    caught = await error(KubernetesAPIError, f.run(
        lambda c: c.request("POST", COLLECTION, body=BODY),
    ))
    assert caught.write_may_have_applied and len(f.requests) == 1
    f = Fixture()
    f.remaining, f.delay = 0.02, 1
    caught = await error(KubernetesAPIError, f.run(
        lambda c: c.request("POST", COLLECTION, body=BODY),
    ))
    assert caught.write_may_have_applied and len(f.requests) == 1

    f = Fixture()
    f.delay = 1
    task = asyncio.create_task(f.run(
        lambda c: c.request("POST", COLLECTION, body=BODY),
    ))
    try:
        for _ in range(100):
            if f.requests:
                break
            await asyncio.sleep(0.01)
        assert len(f.requests) == 1
        task.cancel()
        await error(asyncio.CancelledError, task)
        assert f.closed and len(f.requests) == 1
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    f, caller_body = Fixture(), deepcopy(BODY)
    f.before_http = lambda: caller_body["data"].clear()
    assert await f.run(
        lambda c: c.request("POST", COLLECTION, body=caller_body),
    ) == RESPONSE
    assert f.requests[0][2] == BODY and caller_body["data"] == {}
    print("Timeouts, cancellation and caller mutations do not retry or alter checked writes")
    print("Guarded Secret verification passed. No live services or credentials were used.")


if __name__ == "__main__":
    asyncio.run(check())
