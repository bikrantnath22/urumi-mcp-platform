"""Owned Secret recovery checks using guarded connections and mock HTTP."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
import verify_store_guarded_secrets as fixtures

from app import store_secret_api as secrets_api
from app.store_credentials import StoreCredentials
from app.store_guarded_kubernetes import StoreKubernetesStopped
from app.store_kube_connection import KubernetesAPIError
from app.store_namespace import StoreNamespaceError
from app.store_namespace_api import StoreNamespacePending
from app.store_secret import StoreSecretError


class Fixture(fixtures.Fixture):
    def __init__(self, uid=None, *, exists=True):
        super().__init__(uid)
        self.namespace = deepcopy(fixtures.NAMESPACE)
        self.secret = deepcopy(fixtures.RESPONSE) if exists else None
        self.conflict = self.conflict_missing = False
        self.uncertain = self.invalid_reply = False
        self.replace_after_read = self.replace_after_write = False
        self.lose_after_write = False
        self.pause = self.started = None

    def count(self, method, path):
        return sum(call[0] == method and call[1] == path for call in self.requests)

    async def peer(self, request):
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append((method, path, deepcopy(body)))
        if method == "GET" and path == fixtures.NAMESPACE_ITEM:
            if self.lose_after_write and self.secret is not None:
                self.stored_uid = "another-binding"
            return httpx.Response(
                200 if self.namespace is not None else 404,
                json=deepcopy(self.namespace or {}),
            )
        if method == "GET" and path == fixtures.ITEM:
            if self.replace_after_read:
                self.namespace["metadata"]["uid"] = "replaced-namespace"
            return httpx.Response(
                200 if self.secret is not None else 404,
                json=deepcopy(self.secret or {}),
            )
        if method == "POST" and path == fixtures.COLLECTION:
            if self.conflict or self.conflict_missing:
                if self.conflict:
                    self.secret = deepcopy(fixtures.RESPONSE)
                return httpx.Response(409, json={"message": "private fixture"})
            if self.secret is not None:
                return httpx.Response(409, json={})
            self.secret = deepcopy(body)
            self.secret["metadata"].update(uid="secret-uid", resourceVersion="5")
            if self.replace_after_write:
                self.namespace["metadata"]["uid"] = "replaced-namespace"
            if self.started is not None:
                self.started.set()
                await self.pause.wait()
            if self.uncertain:
                raise httpx.ReadTimeout("private fixture")
            result = deepcopy(self.secret)
            if self.invalid_reply:
                result["data"] = {}
            return httpx.Response(201, json=result)
        raise AssertionError("Unexpected mocked Kubernetes request.")

    async def ensure(self, *, email="owner@example.test"):
        return await self.run(
            lambda connection: secrets_api.StoreSecretAPI(connection).ensure_secret(
                admin_email=email,
            ),
        )

async def check():
    for uid in (None, "secret-uid"):
        f = Fixture(uid)
        with patch.object(
            StoreCredentials, "generate",
            side_effect=AssertionError("Existing credentials must not be regenerated."),
        ):
            result = await f.ensure(email="changed@example.test")
        assert result == fixtures.RESPONSE
        assert f.count("POST", fixtures.COLLECTION) == 0
        assert f.count("GET", fixtures.NAMESPACE_ITEM) == 2
    f = Fixture(exists=False)
    result = await f.ensure()
    assert result == f.secret and f.count("POST", fixtures.COLLECTION) == 1
    restored = StoreCredentials.from_secret_data(fixtures.IDENTITY, result["data"])
    assert restored.secret_data() == result["data"]
    assert f.count("GET", fixtures.NAMESPACE_ITEM) == 4
    with patch.object(
        StoreCredentials, "generate",
        side_effect=AssertionError("Retry must reuse the accepted Secret."),
    ):
        assert await f.ensure() == result
    assert f.count("POST", fixtures.COLLECTION) == 1
    print("Existing credentials are reused; missing unbound Secrets are created once")

    f = Fixture(exists=False)
    f.conflict = True
    assert await f.ensure() == fixtures.RESPONSE
    assert f.count("POST", fixtures.COLLECTION) == 1
    assert f.count("GET", fixtures.ITEM) == 2
    f = Fixture(exists=False)
    f.conflict_missing = True
    await fixtures.error(secrets_api.StoreSecretPending, f.ensure())
    assert f.count("POST", fixtures.COLLECTION) == 1
    assert f.secret is None
    print("Creation conflicts are read and verified; unavailable conflicts never trigger another write")

    f = Fixture("secret-uid", exists=False)
    await fixtures.error(StoreSecretError, f.ensure())
    assert f.count("POST", fixtures.COLLECTION) == 0
    f = Fixture("secret-uid")
    f.secret["metadata"]["uid"] = "replacement-secret"
    await fixtures.error(KubernetesAPIError, f.ensure())
    assert f.count("POST", fixtures.COLLECTION) == 0
    for mode in ("missing", "replaced", "terminating", "pending"):
        f = Fixture(exists=False)
        if mode == "missing":
            f.namespace = None
        elif mode == "replaced":
            f.namespace["metadata"]["uid"] = "replacement-namespace"
        elif mode == "terminating":
            f.namespace["metadata"]["deletionTimestamp"] = "2026-10-18T00:00:00Z"
        else:
            f.namespace["status"]["phase"] = "Pending"
        await fixtures.error(
            StoreNamespacePending if mode == "pending" else StoreNamespaceError,
            f.ensure(),
        )
        assert f.count("GET", fixtures.ITEM) == 0
        assert f.count("POST", fixtures.COLLECTION) == 0
    f = Fixture()
    f.replace_after_read = True
    await fixtures.error(StoreNamespaceError, f.ensure())
    assert f.count("POST", fixtures.COLLECTION) == 0
    print("Missing/replaced bound Secrets and changed namespaces stop before credential creation")

    for mode in ("uncertain", "invalid_reply", "replace_after_write"):
        f = Fixture(exists=False)
        setattr(f, mode, True)
        caught = await fixtures.error(KubernetesAPIError, f.ensure())
        assert caught.write_may_have_applied
        assert f.count("POST", fixtures.COLLECTION) == 1
        assert f.secret is not None
        if mode != "replace_after_write":
            setattr(f, mode, False)
            before = deepcopy(f.secret)
            assert await f.ensure() == before
            assert f.count("POST", fixtures.COLLECTION) == 1
    f = Fixture(exists=False)
    f.lose_after_write = True
    caught = await fixtures.error(StoreKubernetesStopped, f.ensure())
    assert caught.write_may_have_applied and f.count("POST", fixtures.COLLECTION) == 1
    print("Uncertain writes, rejected acknowledgements and failed final checks preserve recovery state")

    f = Fixture(exists=False)
    f.started, f.pause = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(f.ensure())
    try:
        await asyncio.wait_for(f.started.wait(), 5)
        assert f.secret is not None
        saved = deepcopy(f.secret)
        task.cancel()
        await fixtures.error(asyncio.CancelledError, task)
        assert f.closed and f.count("POST", fixtures.COLLECTION) == 1
        f.started = f.pause = None
        assert await f.ensure() == saved
        assert f.count("POST", fixtures.COLLECTION) == 1
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    try:
        secrets_api.StoreSecretAPI(object())
    except ValueError:
        pass
    else:
        raise AssertionError("An unguarded connection was accepted.")
    print("Cancellation recovery reuses the Secret; unguarded connections are rejected")
    print("Owned Secret API verification passed. No live services were called.")


if __name__ == "__main__":
    asyncio.run(check())
