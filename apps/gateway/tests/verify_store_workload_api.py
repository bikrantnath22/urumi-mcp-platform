"""Guarded workload collection; all HTTP and ownership storage are mocked."""

import asyncio
from copy import deepcopy
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from app import store_workload_api as module
from app import store_guarded_secrets as secret_guard
from app.store_guarded_kubernetes import GuardedKubernetesConnection, StoreKubernetesStopped
from app.store_job_guard import StoreJobClaimLost, StoreJobGuardUnavailable
from app.store_kube_connection import KubernetesAPIError, KubernetesConnection
from app.store_namespace import StoreNamespaceError
from app.store_readiness import StoreBootstrapFailed, resource_read_paths
import verify_store_secret_api as ownership
import verify_store_guarded_secrets as fixtures


def document(name):
    job = name == "store-bootstrap"
    kind = "Job" if job else ("StatefulSet" if name == "mysql" else "Deployment")
    annotations = {
        "meta.helm.sh/release-name": "store",
        "meta.helm.sh/release-namespace": fixtures.IDENTITY.name,
    }
    status = {
        "observedGeneration": 1, "replicas": 1, "updatedReplicas": 1,
        "readyReplicas": 1, "availableReplicas": 1, "currentReplicas": 1,
        "currentRevision": "revision", "updateRevision": "revision",
        "conditions": [{"type": "Available", "status": "True"}],
    }
    if job:
        annotations = {
            "helm.sh/hook": "post-install,post-upgrade",
            "helm.sh/hook-delete-policy": "before-hook-creation",
        }
        status = {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]}
    return {
        "apiVersion": "batch/v1" if job else "apps/v1", "kind": kind,
        "metadata": {
            "name": name, "namespace": fixtures.IDENTITY.name, "uid": "fixture-uid",
            "resourceVersion": "1", "generation": 1, "annotations": annotations,
            "labels": {"app.kubernetes.io/managed-by": "Helm"},
        },
        "spec": {"activeDeadlineSeconds": 900} if job else {"replicas": 1},
        "status": status,
    }


class Fixture(ownership.Fixture):
    def __init__(self):
        super().__init__("secret-uid")
        self.paths = resource_read_paths(fixtures.IDENTITY)
        self.resources = {name: document(name) for name in self.paths}
        self.namespace_binding = "namespace-uid"
        self.on_resource = None
        self.delay = 0
        self.status = 200
        self.started = asyncio.Event()
        self.active = False

    async def require(self, factory, claim, **kwargs):
        if self.namespace_binding != kwargs["expected_namespace_uid"]:
            raise StoreJobClaimLost("PRIVATE NAMESPACE BINDING")
        return await super().require(factory, claim, **kwargs)

    async def peer(self, request):
        names = {path: name for name, path in self.paths.items()}
        if request.url.path not in names:
            return await super().peer(request)
        assert request.method == "GET" and not request.content
        name = names[request.url.path]
        self.requests.append(("GET", request.url.path, None))
        self.started.set()
        self.active = True
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.on_resource is not None:
                callback, self.on_resource = self.on_resource, None
                callback()
            if self.status != 200:
                return httpx.Response(self.status, json={"message": "PRIVATE HTTP ERROR"})
            item = self.resources[name]
            return httpx.Response(200 if item is not None else 404, json=item or {})
        finally:
            self.active = False

    async def run(self, callback=None, **changes):
        raw = KubernetesConnection(
            lambda: "fixture.worker.signature", fixtures.TLS,
            transport=httpx.MockTransport(self.peer),
        )
        options = {
            "namespace_uid": "namespace-uid", "credentials_secret_uid": "secret-uid",
            "bootstrap_seconds": 900,
        }
        options.update(changes)
        try:
            with patch.object(module, "require_claim", self.require), patch.object(
                secret_guard, "require_claim", self.require,
            ):
                if callback is not None:
                    return await callback(raw)
                return await module.check_workloads(
                    self.factory, fixtures.CLAIM, raw, fixtures.SCOPE, **options,
                )
        finally:
            await raw.aclose()
            self.closed = raw._client.is_closed

    def workload_requests(self):
        return [path for method, path, body in self.requests if path in self.paths.values()]


async def error(kind, operation):
    try:
        await operation
    except kind as caught:
        assert "PRIVATE" not in str(caught)
        return caught
    raise AssertionError("Expected " + kind.__name__)


async def check():
    baseline = asyncio.all_tasks()
    f = Fixture()
    snapshot = deepcopy(f.resources)
    result = await f.run()
    assert result == {name: True for name in f.paths}
    assert f.resources == snapshot and f.closed
    assert f.workload_requests() == list(f.paths.values())
    assert f.calls >= 2 * len(f.requests)
    assert all(method == "GET" for method, path, body in f.requests)
    assert all(type(value) is bool for value in result.values())
    for name in f.paths:
        f = Fixture()
        f.resources[name] = None
        result = await f.run()
        assert result[name] is False and len(f.workload_requests()) == 5
    f = Fixture()
    f.resources["wordpress"]["status"]["observedGeneration"] = 0
    assert (await f.run())["wordpress"] is False
    f.resources["mysql"] = None
    f.resources["store-bootstrap"]["status"]["conditions"] = [{
        "type": "Failed", "status": "True", "message": "PRIVATE FAILURE",
    }]
    await error(StoreBootstrapFailed, f.run())
    print("Five scoped reads return only flags; pending resources and failed bootstrap are handled")

    f = Fixture()
    async def invalid_routes(raw):
        path = f.paths["wordpress"]
        reader = module._WorkloadReads(
            raw, f.factory, fixtures.CLAIM, fixtures.SCOPE,
            namespace_uid="namespace-uid", credentials_secret_uid="secret-uid",
        )
        for method, target, body in (
            ("GET", path.replace(fixtures.IDENTITY.store_id.hex, uuid4().hex), None),
            ("GET", path + "?watch=true", None),
            ("GET", path + "/status", None),
            ("GET", path.rsplit("/", 1)[0], None),
            ("GET", fixtures.ITEM, None),
            ("GET", path, {}),
            ("POST", path, {}), ("PATCH", path, {}), ("DELETE", path, None),
        ):
            await error(ValueError, reader.request(method, target, body=body))
        base = GuardedKubernetesConnection(
            raw, f.factory, fixtures.CLAIM, fixtures.SCOPE,
            namespace_uid="namespace-uid",
        )
        secret = secret_guard.GuardedStoreSecretConnection(
            raw, f.factory, fixtures.CLAIM, fixtures.SCOPE,
            namespace_uid="namespace-uid", credentials_secret_uid="secret-uid",
        )
        await error(ValueError, base.request("GET", path))
        await error(ValueError, secret.request("GET", path))
        for target in (path + "?watch=true", path.rsplit("/", 1)[0]):
            await error(ValueError, raw.request("GET", target))
        for method in ("POST", "PATCH", "DELETE"):
            await error(ValueError, raw.request(method, path, body={}))
    await f.run(invalid_routes)
    assert not f.requests and f.calls == 0
    for key in ("namespace_uid", "credentials_secret_uid"):
        f = Fixture()
        await error(ValueError, f.run(**{key: None}))
        assert not f.requests and f.calls == 0
    f = Fixture()
    await error(ValueError, f.run(bootstrap_seconds=True))
    assert not f.requests
    print("Cross-store paths, lists, subresources and writes send no HTTP; older guards stay restricted")

    for failure in (
        StoreJobClaimLost("PRIVATE LOST CLAIM"),
        StoreJobGuardUnavailable("PRIVATE STORAGE FAILURE"),
    ):
        f = Fixture()
        f.before = failure
        caught = await error(StoreKubernetesStopped, f.run())
        assert not caught.write_may_have_applied and not f.requests
    f = Fixture()
    f.remaining = 0
    await error(StoreKubernetesStopped, f.run())
    assert not f.requests
    for field in ("namespace_binding", "stored_uid"):
        f = Fixture()
        f.on_resource = lambda field=field, f=f: setattr(f, field, "replacement")
        caught = await error(StoreKubernetesStopped, f.run())
        assert not caught.write_may_have_applied and len(f.workload_requests()) == 1
    print("Live claim and both saved UID bindings are checked before and after reads")

    for kind, change in (
        (StoreNamespaceError, lambda f: f.namespace["metadata"].update(uid="replacement")),
        (KubernetesAPIError, lambda f: f.secret["metadata"].update(uid="replacement")),
    ):
        f = Fixture()
        f.on_resource = lambda f=f, change=change: change(f)
        await error(kind, f.run())
        assert f.closed and all(method == "GET" for method, path, body in f.requests)
    f = Fixture()
    f.status = 503
    caught = await error(KubernetesAPIError, f.run())
    assert not caught.write_may_have_applied and f.closed
    print("Actual namespace/Secret replacements and API failures cannot return successful flags")

    f = Fixture()
    f.delay = 1
    with patch.object(module, "_CHECK_TIMEOUT_SECONDS", 0.03):
        caught = await error(KubernetesAPIError, f.run())
    assert not caught.write_may_have_applied and f.closed and not f.active
    assert len(f.workload_requests()) == 1
    f = Fixture()
    f.delay = 1
    task = asyncio.create_task(f.run())
    await f.started.wait()
    task.cancel()
    await error(asyncio.CancelledError, task)
    assert f.closed and not f.active and len(f.workload_requests()) == 1
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)
    print("Timeout and cancellation stop reads without retries or leftover tasks")
    f = Fixture()
    f.resources["store-bootstrap"]["metadata"]["annotations"] = {
        "meta.helm.sh/release-name": "store",
        "meta.helm.sh/release-namespace": fixtures.IDENTITY.name,
    }
    assert all((await f.run(managed_bootstrap=True)).values())
    assert all(method == "GET" for method, path, body in f.requests)
    f.resources["store-bootstrap"]["metadata"]["annotations"]["meta.helm.sh/release-namespace"] = "other"
    from app.store_readiness import StoreReadinessError
    await error(StoreReadinessError, f.run(managed_bootstrap=True))
    print("Durable bootstrap ownership is read-only and checked without changing legacy hook verification")
    print("Guarded workload verification passed. No live services or processes were used.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 30))
