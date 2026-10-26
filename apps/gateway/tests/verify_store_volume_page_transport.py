"""Fixed volume-page transport checks; all HTTP and credentials are fixtures."""

import asyncio
from pathlib import Path
import ssl
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from app import store_kube_connection as module
from app.store_guarded_kubernetes import GuardedKubernetesConnection
from app.store_claim_api import _ClaimReads
from app.store_volume_api import _VolumeReads
from app.store_volume_pages import volume_page_path
from verify_store_volume_api import Fixture, SCOPE


async def error(kind, operation):
    try:
        await operation
    except kind as caught:
        assert "PRIVATE" not in str(caught)
        if isinstance(caught, module.KubernetesAPIError):
            assert not caught.write_may_have_applied
        return caught
    raise AssertionError("Expected " + str(kind))


async def check():
    baseline = asyncio.all_tasks()
    calls = []
    credentials = []
    mode = {"status": 200, "content": None, "delay": 0}
    started = asyncio.Event()
    active = False
    document = {
        "apiVersion": "v1", "kind": "PersistentVolumeList",
        "metadata": {"resourceVersion": "42", "continue": ""},
        "items": [],
    }

    def token():
        value = f"fixture.page{len(credentials) + 1}.signature"
        credentials.append(value)
        return value

    async def peer(request):
        nonlocal active
        calls.append(request)
        assert request.url.scheme == "https"
        assert request.url.host == "kubernetes.default.svc"
        assert request.url.path == "/api/v1/persistentvolumes"
        assert request.method == "GET" and not request.content
        if mode["delay"]:
            active = True
            started.set()
            try:
                await asyncio.sleep(mode["delay"])
            finally:
                active = False
        if mode["content"] is not None:
            return httpx.Response(mode["status"], content=mode["content"])
        return httpx.Response(
            mode["status"], json=document,
            headers={"location": "https://example.test/escape"},
        )

    connection = module.KubernetesConnection(
        token, ssl.create_default_context(), transport=httpx.MockTransport(peer),
    )
    paths = [volume_page_path(), volume_page_path("a/b+c=xyz&watch=true")]
    try:
        for path in paths:
            assert await connection.request("GET", path) == document
            assert calls[-1].url.raw_path.decode("ascii") == path
        assert calls[0].headers["authorization"] == "Bearer fixture.page1.signature"
        assert calls[1].headers["authorization"] == "Bearer fixture.page2.signature"
        assert calls[1].url.params["continue"] == "a/b+c=xyz&watch=true"
        assert set(calls[1].url.params) == {"limit", "continue"}
        print("Fixed-origin GET pages preserve encoded continuation tokens and read fresh credentials")

        rejected = [
            (method, path, None)
            for method in ("POST", "PUT", "PATCH", "DELETE")
            for path in paths
        ]
        rejected += [("GET", path, {}) for path in paths]
        rejected += [("GET", path, None) for path in (
            "/api/v1/persistentvolumes", "/api/v1/persistentvolumes?limit=101",
            "/api/v1/persistentvolumes?limit=100&watch=true",
            "/api/v1/persistentvolumes?limit=100&continue=",
            "/api/v1/persistentvolumes?limit=100&continue=a&continue=b",
            "/api/v1/persistentvolumes?limit=100&continue=%0A",
            "/api/v1/persistentvolumes?limit=100&continue=%2f",
            "/api/v1/secrets?limit=100",
            "https://example.test/api/v1/persistentvolumes?limit=100",
        )]
        before = len(calls), len(credentials)
        for method, path, body in rejected:
            await error(ValueError, connection.request(method, path, body=body))
        assert (len(calls), len(credentials)) == before
        print("Writes, broader queries and other destinations are rejected before HTTP or credential access")

        fixture = Fixture()
        views = [
            GuardedKubernetesConnection(
                connection, fixture.factory, fixture.claim, SCOPE,
                namespace_uid="namespace-uid",
            ),
            _ClaimReads(
                connection, fixture.factory, fixture.claim, SCOPE,
                namespace_uid="namespace-uid", claim_name="wordpress-data",
            ),
            _VolumeReads(
                connection, fixture.factory, fixture.claim, SCOPE,
                namespace_uid="namespace-uid", claim_name="wordpress-data",
            ),
        ]
        for view in views:
            for path in paths:
                await error(ValueError, view.request("GET", path))
        assert (len(calls), len(credentials)) == before
        print("Existing job, claim and bound-volume guards still reject collection pages without database access")

        for status in (301, 307, 401, 403, 410, 429, 500):
            mode.update(status=status, content=b"PRIVATE API diagnostic")
            before = len(calls)
            caught = await error(
                module.KubernetesAPIError, connection.request("GET", paths[0]),
            )
            assert caught.status_code == status and len(calls) == before + 1
        mode.update(status=404, content=None)
        assert await connection.request("GET", paths[0]) is None
        for content in (b"not JSON", b"[]", b"x" * (module._MAX_BYTES + 1)):
            mode.update(status=200, content=content)
            before = len(calls)
            await error(
                module.KubernetesAPIError, connection.request("GET", paths[0]),
            )
            assert len(calls) == before + 1
        print("Expired cursors, denied reads, redirects and malformed/oversized replies fail without retry")

        mode.update(status=200, content=None, delay=1)
        original_timeout = asyncio.timeout
        with patch.object(module.asyncio, "timeout", lambda seconds: original_timeout(0.1)):
            before = len(calls)
            await error(
                module.KubernetesAPIError, connection.request("GET", paths[0]),
            )
        assert not active and len(calls) == before + 1
        started.clear()
        task = asyncio.create_task(connection.request("GET", paths[0]))
        signal = asyncio.create_task(started.wait())
        try:
            done, _ = await asyncio.wait(
                (task, signal), timeout=2, return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                await task
                raise AssertionError("The read ended before its cancellation checkpoint.")
            assert signal in done
            task.cancel()
            await error(asyncio.CancelledError, task)
        finally:
            for pending in (task, signal):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, signal, return_exceptions=True)
        assert not active
        print("Timeout and cancellation stop the page request without another HTTP attempt")
    finally:
        await connection.aclose()
    assert connection._client.is_closed
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)
    print("Volume-page transport verification passed. No live services or permissions were changed.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 30))
