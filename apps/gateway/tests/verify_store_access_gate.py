"""Owned gate verification with real guarded transport and fixture HTTP only."""
import asyncio
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import httpx
from app import store_access_guard as module, store_guarded_secrets as secret_guard
from app.store_guarded_kubernetes import GuardedKubernetesConnection, StoreKubernetesStopped
from app.store_kube_connection import KubernetesConnection
import verify_store_secret_api as ownership
import verify_store_guarded_secrets as fixtures


def documents():
    ingress = {
        "apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
        "metadata": {
            "name": "wordpress", "namespace": fixtures.IDENTITY.name,
            "uid": "ingress-uid", "resourceVersion": "11",
            "annotations": {module.ANNOTATION: fixtures.IDENTITY.name + "-store-access@kubernetescrd"},
        },
        "spec": {"rules": [{
            "host": "fixture-tea.localtest.me", "http": {"paths": [{
                "path": "/", "pathType": "Prefix",
                "backend": {"service": {"name": "wordpress", "port": {"number": 80}}},
            }]},
        }]},
    }
    middleware = {
        "apiVersion": "traefik.io/v1alpha1", "kind": "Middleware",
        "metadata": {"name": "store-access", "namespace": fixtures.IDENTITY.name,
                     "uid": "middleware-uid", "resourceVersion": "12"},
        "spec": {"forwardAuth": {
            "address": module.gate_address(fixtures.IDENTITY, fixtures.SCOPE),
            "trustForwardHeader": False,
        }},
    }
    return ingress, middleware


class Fixture(ownership.Fixture):
    def __init__(self):
        super().__init__("secret-uid")
        self.docs = dict(zip(module.gate_paths(fixtures.IDENTITY), documents()))
        self.after_gate = None
        self.hold = False
        self.entered = asyncio.Event()
        self.active = False

    async def peer(self, request):
        if request.url.path not in self.docs:
            return await super().peer(request)
        self.requests.append(("GET", request.url.path, None))
        assert request.method == "GET"
        self.entered.set()
        self.active = True
        try:
            if self.hold:
                await asyncio.Event().wait()
            if self.after_gate:
                self.after_gate()
            doc = self.docs[request.url.path]
            return httpx.Response(200 if doc is not None else 404, json=doc or {})
        finally:
            self.active = False

    async def run(self, callback=None):
        raw = KubernetesConnection(
            lambda: "fixture.worker.signature", fixtures.TLS,
            transport=httpx.MockTransport(self.peer),
        )
        try:
            with patch.object(module, "require_claim", self.require), patch.object(
                secret_guard, "require_claim", self.require,
            ):
                if callback:
                    return await callback(raw)
                return await module.verify_store_access_gate(
                    self.factory, fixtures.CLAIM, raw, fixtures.SCOPE,
                    namespace_uid="namespace-uid", credentials_secret_uid="secret-uid",
                )
        finally:
            await raw.aclose()


async def rejects(kind, operation):
    try:
        await operation
    except kind:
        return
    raise AssertionError("Expected " + kind.__name__)


async def check():
    baseline = asyncio.all_tasks()
    f = Fixture()
    assert await f.run() is True
    assert all(method == "GET" for method, path, body in f.requests)
    for target in (0, 1):
        for key, value in (("uid", ""), ("namespace", "store"), ("deletionTimestamp", "now")):
            f = Fixture()
            list(f.docs.values())[target]["metadata"][key] = value
            await rejects(ValueError, f.run())
    for alteration in ("host", "annotation", "address", "forward-headers", "extra-spec", "missing"):
        f = Fixture()
        ingress, middleware = f.docs.values()
        if alteration == "host": ingress["spec"]["rules"][0]["host"] = "other.localtest.me"
        if alteration == "annotation": ingress["metadata"]["annotations"][module.ANNOTATION] = "other-store-access@kubernetescrd"
        if alteration == "address": middleware["spec"]["forwardAuth"]["address"] += "?other=1"
        if alteration == "forward-headers": middleware["spec"]["forwardAuth"]["trustForwardHeader"] = True
        if alteration == "extra-spec": middleware["spec"]["headers"] = {}
        if alteration == "missing": f.docs[next(iter(f.docs))] = None
        await rejects(ValueError, f.run())
    print("Exact store ingress and maintenance destination are required; altered or missing gates fail.")

    for target, method in (
        (module.gate_paths(fixtures.IDENTITY)[0].replace(fixtures.IDENTITY.store_id.hex, uuid4().hex), "GET"),
        (module.gate_paths(fixtures.IDENTITY)[0] + "?watch=1", "GET"),
        (module.gate_paths(fixtures.IDENTITY)[1], "DELETE"),
        ("/api/v1/persistentvolumes?limit=100", "GET"),
    ):
        f = Fixture()
        async def invalid(raw):
            reader = module._GateReads(
                raw, f.factory, fixtures.CLAIM, fixtures.SCOPE,
                namespace_uid="namespace-uid", credentials_secret_uid="secret-uid",
            )
            return await reader.request(method, target)
        await rejects(ValueError, f.run(invalid))
        assert not f.requests
    f = Fixture()
    async def old_guard(raw):
        reader = GuardedKubernetesConnection(
            raw, f.factory, fixtures.CLAIM, fixtures.SCOPE, namespace_uid="namespace-uid",
        )
        return await reader.request("GET", module.gate_paths(fixtures.IDENTITY)[0])
    await rejects(ValueError, f.run(old_guard))
    assert not f.requests
    print("Other stores, collection routes and writes send no HTTP; existing guards stay restricted.")

    f = Fixture()
    f.after_gate = lambda: setattr(f, "stored_uid", "replaced")
    await rejects(StoreKubernetesStopped, f.run())
    f = Fixture()
    f.hold = True
    task = asyncio.create_task(f.run())
    await f.entered.wait()
    task.cancel()
    await rejects(asyncio.CancelledError, task)
    assert not f.active and not (asyncio.all_tasks() - baseline)
    print("Lost ownership and cancellation cannot return gate verification; no tasks remain.")
    print("Store gate verification passed. No cluster, database or business calls were made.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 30))
