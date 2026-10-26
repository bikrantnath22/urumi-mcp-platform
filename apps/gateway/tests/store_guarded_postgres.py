"""Real PostgreSQL guard integration; Kubernetes HTTP remains mocked.

Loaded by verify_store_job_guard.py using its isolated schema and fixtures.
"""

import asyncio
from copy import deepcopy
from datetime import timedelta
import json
import ssl
from uuid import uuid4

import httpx
from sqlalchemy import func

from app import store_jobs as jobs
from app.models import Store, StoreOperation
from app.store_cluster_scope import StoreClusterScope
from app.store_guarded_kubernetes import (
    GuardedKubernetesConnection, StoreKubernetesStopped,
)
from app.store_kube_connection import KubernetesConnection
from app.store_namespace import StoreNamespaceError, namespace_for
from app.store_namespace_api import StoreNamespaceAPI
from app.store_namespace_binding import bind_namespace
from app.store_namespace_stage import prepare_namespace, cleanup_namespace


async def verify_guarded_requests(factory, fresh, change, snapshot, unlocked):
    scope = StoreClusterScope("platform", "platform")
    tls = ssl.create_default_context()

    class Peer:
        def __init__(self, claim):
            self.claim = claim
            self.identity = namespace_for(claim)
            self.namespace = None
            self.binding = None
            self.calls = []
            self.after_create = None
            self.started = None
            self.resume = None

        @property
        def namespace_path(self):
            return "/api/v1/namespaces/" + self.identity.name

        @property
        def binding_collection(self):
            return (
                "/apis/rbac.authorization.k8s.io/v1/namespaces/"
                + self.identity.name + "/rolebindings"
            )

        async def __call__(self, request):
            # An independent connection must acquire the gate and row lock.
            await unlocked(self.claim)
            method, path = request.method, request.url.path
            body = json.loads(request.content) if request.content else None
            self.calls.append((method, path, deepcopy(body)))
            if method == "GET" and path == self.namespace_path:
                return httpx.Response(
                    200 if self.namespace is not None else 404,
                    json=deepcopy(self.namespace or {}),
                )
            if method == "POST" and path == "/api/v1/namespaces":
                if self.namespace is not None:
                    return httpx.Response(409, json={})
                self.namespace = deepcopy(body)
                self.namespace["metadata"].update(uid=str(uuid4()), resourceVersion="17")
                self.namespace["status"] = {"phase": "Active"}
                if self.after_create:
                    await self.after_create()
                if self.started is not None:
                    self.started.set()
                    await self.resume.wait()
                return httpx.Response(201, json=deepcopy(self.namespace))
            if method == "GET" and path == self.binding_collection + "/urumi-store-provisioner":
                return httpx.Response(
                    200 if self.binding is not None else 404,
                    json=deepcopy(self.binding or {}),
                )
            if method == "POST" and path == self.binding_collection:
                assert self.namespace is not None
                async with factory() as db:
                    saved = await db.get(Store, self.claim.store_id)
                    assert saved.namespace_uid == self.namespace["metadata"]["uid"]
                if self.binding is not None:
                    return httpx.Response(409, json={})
                self.binding = deepcopy(body)
                self.binding["metadata"].update(uid=str(uuid4()), resourceVersion="3")
                return httpx.Response(201, json=deepcopy(self.binding))
            if method == "DELETE" and path == self.namespace_path:
                assert self.namespace is not None
                assert body["preconditions"] == {
                    "uid": self.namespace["metadata"]["uid"],
                    "resourceVersion": self.namespace["metadata"]["resourceVersion"],
                }
                self.namespace["metadata"]["deletionTimestamp"] = "2026-10-18T00:00:00Z"
                self.namespace["status"]["phase"] = "Terminating"
                return httpx.Response(202, json={"kind": "Status"})
            raise AssertionError("Unexpected mocked Kubernetes request.")

        def count(self, method, path):
            return sum(item[0] == method and item[1] == path for item in self.calls)

    def raw_connection(peer):
        return KubernetesConnection(
            lambda: "fixture.worker.signature", tls,
            transport=httpx.MockTransport(peer),
        )

    def checked(raw, claim, uid, *, cleanup=False):
        return GuardedKubernetesConnection(
            raw, factory, claim, scope,
            namespace_uid=uid, for_cleanup=cleanup,
        )

    async def stopped(operation, *, uncertain):
        try:
            await operation
        except StoreKubernetesStopped as error:
            assert error.write_may_have_applied is uncertain
            return
        raise AssertionError("A stopped worker was allowed to continue.")

    claim = await fresh()
    peer = Peer(claim)
    raw = raw_connection(peer)
    try:
        before = await snapshot()
        api = StoreNamespaceAPI(checked(raw, claim, None), scope)
        document = await api.ensure_namespace(peer.identity, expected_uid=None)
        uid = document["metadata"]["uid"]
        assert await snapshot() == before
        body = scope.role_binding_manifest(peer.identity, document, expected_uid=uid)
        call_count = len(peer.calls)
        await stopped(
            checked(raw, claim, uid).request("POST", peer.binding_collection, body=body),
            uncertain=False,
        )
        assert len(peer.calls) == call_count
        assert await bind_namespace(factory, claim, document) == uid

        before = await snapshot()
        api = StoreNamespaceAPI(checked(raw, claim, uid), scope)
        result = await api.ensure_role_binding(peer.identity, expected_uid=uid)
        assert result["roleRef"]["name"] == scope.workload_role
        await api.ensure_role_binding(peer.identity, expected_uid=uid)
        assert peer.count("POST", "/api/v1/namespaces") == 1
        assert peer.count("POST", peer.binding_collection) == 1
        assert await snapshot() == before
    finally:
        await raw.aclose()
    print("Real guards release DB locks before HTTP; UID persistence is required before grants")

    claim = await fresh()
    peer = Peer(claim)
    raw = raw_connection(peer)
    try:
        async def lose_lease():
            await change(
                StoreOperation, claim.operation_id,
                lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
            )

        peer.after_create = lose_lease
        api = StoreNamespaceAPI(checked(raw, claim, None), scope)
        await stopped(api.ensure_namespace(peer.identity, expected_uid=None), uncertain=True)
        assert peer.namespace is not None
        async with factory() as db:
            assert (await db.get(Store, claim.store_id)).namespace_uid is None

        before_calls = len(peer.calls)
        await stopped(
            checked(raw, claim, None).request("GET", peer.namespace_path),
            uncertain=False,
        )
        assert len(peer.calls) == before_calls
        recovered = await jobs.claim_next(factory)
        assert recovered is not None and recovered.lease_token != claim.lease_token
        assert recovered.deadline_at == claim.deadline_at
        peer.claim, peer.after_create = recovered, None
        document = await StoreNamespaceAPI(
            checked(raw, recovered, None), scope,
        ).ensure_namespace(peer.identity, expected_uid=None)
        assert await bind_namespace(factory, recovered, document) == document["metadata"]["uid"]
        assert peer.count("POST", "/api/v1/namespaces") == 1
    finally:
        await raw.aclose()
    print("An accepted write survives lease loss; recovery reuses it without another creation")

    claim = await fresh()
    peer = Peer(claim)
    peer.started, peer.resume = asyncio.Event(), asyncio.Event()
    raw = raw_connection(peer)
    task = None
    try:
        task = asyncio.create_task(checked(raw, claim, None).request(
            "POST", "/api/v1/namespaces",
            body=scope.namespace_manifest(peer.identity),
        ))
        await asyncio.wait_for(peer.started.wait(), 5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Cancellation was swallowed.")
        assert task.done() and peer.namespace is not None
        assert await jobs.release_claim(factory, claim, retry_after_seconds=0)
        recovered = await jobs.claim_next(factory)
        assert recovered is not None and recovered.lease_token != claim.lease_token
        assert recovered.deadline_at == claim.deadline_at
        peer.claim, peer.started = recovered, None
        await StoreNamespaceAPI(checked(raw, recovered, None), scope).ensure_namespace(
            peer.identity, expected_uid=None,
        )
        assert peer.count("POST", "/api/v1/namespaces") == 1
    finally:
        peer.resume.set()
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await raw.aclose()
    print("Cancellation stops HTTP work before release; the replacement worker avoids repeating it")

    claim = await fresh(action="delete", seconds=-1)
    peer = Peer(claim)
    peer.namespace = scope.namespace_manifest(peer.identity)
    peer.namespace["metadata"].update(uid=str(uuid4()), resourceVersion="17")
    peer.namespace["status"] = {"phase": "Active"}
    raw = raw_connection(peer)
    try:
        api = StoreNamespaceAPI(checked(raw, claim, None, cleanup=True), scope)
        document = await api.read_namespace(peer.identity, allow_terminating=True)
        uid = await bind_namespace(factory, claim, document, for_cleanup=True)
        assert uid == document["metadata"]["uid"]
        before = await snapshot()
        api = StoreNamespaceAPI(checked(raw, claim, uid, cleanup=True), scope)
        assert await api.delete_namespace(peer.identity, expected_uid=uid) is False
        assert await api.delete_namespace(peer.identity, expected_uid=uid) is False
        peer.namespace = None
        assert await api.delete_namespace(peer.identity, expected_uid=uid) is True
        assert peer.count("DELETE", peer.namespace_path) == 1
        assert await snapshot() == before
    finally:
        await raw.aclose()
    print("Overdue cleanup uses a live claim and saved UID; deletion waits for confirmed absence")
    claim = await fresh()
    peer = Peer(claim)
    raw = raw_connection(peer)
    try:
        uid = await prepare_namespace(factory, claim, raw, scope)
        assert uid == peer.namespace["metadata"]["uid"]
        async with factory() as db:
            assert (await db.get(Store, claim.store_id)).namespace_uid == uid
        before = await snapshot()
        assert await prepare_namespace(factory, claim, raw, scope) == uid
        assert await snapshot() == before
        assert peer.count("POST", "/api/v1/namespaces") == 1
        assert peer.count("POST", peer.binding_collection) == 1

        document = deepcopy(peer.namespace)
        for replacement in (None, deepcopy(document)):
            if replacement is not None:
                replacement["metadata"]["uid"] = str(uuid4())
            peer.namespace = replacement
            try:
                await prepare_namespace(factory, claim, raw, scope)
            except StoreNamespaceError:
                pass
            else:
                raise AssertionError("A missing or replaced namespace was reused.")
        assert peer.count("POST", "/api/v1/namespaces") == 1
        assert await snapshot() == before
    finally:
        await raw.aclose()
    print("Namespace stage saves ownership before grants; retries reuse resources and reject replacements")

    claim = await fresh()
    peer = Peer(claim)
    raw = raw_connection(peer)
    try:
        async def expire_stage_lease():
            await change(
                StoreOperation, claim.operation_id,
                lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
            )

        peer.after_create = expire_stage_lease
        await stopped(
            prepare_namespace(factory, claim, raw, scope),
            uncertain=True,
        )
        assert peer.namespace is not None and peer.binding is None
        async with factory() as db:
            assert (await db.get(Store, claim.store_id)).namespace_uid is None

        recovered = await jobs.claim_next(factory)
        assert recovered is not None and recovered.lease_token != claim.lease_token
        assert recovered.deadline_at == claim.deadline_at
        peer.claim, peer.after_create = recovered, None
        uid = await prepare_namespace(factory, recovered, raw, scope)
        assert uid == peer.namespace["metadata"]["uid"]
        assert peer.count("POST", "/api/v1/namespaces") == 1
        assert peer.count("POST", peer.binding_collection) == 1
    finally:
        await raw.aclose()
    print("Namespace stage recovers an accepted creation without repeating it or changing its deadline")

    claim = await fresh(action="delete", seconds=-1)
    peer = Peer(claim)
    raw = raw_connection(peer)
    try:
        before = await snapshot()
        assert await cleanup_namespace(factory, claim, raw, scope) is True
        assert await snapshot() == before

        peer.namespace = scope.namespace_manifest(peer.identity)
        peer.namespace["metadata"].update(uid=str(uuid4()), resourceVersion="19")
        peer.namespace["status"] = {"phase": "Active"}
        uid = peer.namespace["metadata"]["uid"]
        assert await cleanup_namespace(factory, claim, raw, scope) is False
        async with factory() as db:
            assert (await db.get(Store, claim.store_id)).namespace_uid == uid
        before = await snapshot()
        assert await cleanup_namespace(factory, claim, raw, scope) is False
        peer.namespace = None
        assert await cleanup_namespace(factory, claim, raw, scope) is True
        assert peer.count("DELETE", peer.namespace_path) == 1
        assert peer.count("POST", "/api/v1/namespaces") == 0
        assert peer.binding is None
        assert await snapshot() == before
    finally:
        await raw.aclose()
    print("Cleanup stage records recovered ownership, waits for absence and creates no resources")

    print("PostgreSQL and mocked Kubernetes integration passed; no cluster resources changed.")


if __name__ == "__main__":
    raise SystemExit("Run: uv run python tests/verify_store_job_guard.py")
