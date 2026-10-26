"""Volume-stage integration: real PostgreSQL, simulated Kubernetes HTTP only."""

import asyncio
from copy import deepcopy
from datetime import timedelta
import re
import ssl

import httpx
from sqlalchemy import func, select

from app import store_jobs as jobs
from app.models import StoreOperation
from app.store_cluster_scope import StoreClusterScope
from app.store_job_guard import StoreJobClaimLost
from app.store_kube_connection import KubernetesConnection
from app.store_namespace import StoreNamespaceError, namespace_for
from app.store_settings import STORE_ADMISSION_LOCK
from app.store_volume_stage import prepare_store_volumes
from app.store_volumes import STORE_CLAIM_NAMES, StoreVolumeError, StoreVolumePending


SCOPE = StoreClusterScope("platform", "platform")
TLS = ssl.create_default_context()


class Peer:
    def __init__(self, factory, claim, records):
        self.factory, self.claim, self.records = factory, claim, tuple(records)
        self.identity = namespace_for(claim)
        self.ns_path = "/api/v1/namespaces/" + self.identity.name
        self.documents, self.claim_paths, self.volume_paths = {}, {}, {}
        self.calls = []
        self.on_request = None
        self.pause_path = None
        self.started, self.resume = asyncio.Event(), asyncio.Event()
        self.active = self.closed = False
        namespace = SCOPE.namespace_manifest(self.identity)
        namespace["metadata"].update(
            uid=records[0].namespace_uid, resourceVersion="1",
        )
        namespace["status"] = {"phase": "Active"}
        self.documents[self.ns_path] = namespace
        for record in records:
            name = record.claim_name
            claim_path = self.ns_path + "/persistentvolumeclaims/" + name
            volume_path = "/api/v1/persistentvolumes/" + record.volume_name
            self.claim_paths[name], self.volume_paths[name] = claim_path, volume_path
            labels = {"app": "mysql" if name == "data-mysql-0" else "wordpress"}
            annotations = {}
            if name == "wordpress-data":
                labels["app.kubernetes.io/managed-by"] = "Helm"
                annotations = {
                    "meta.helm.sh/release-name": "store",
                    "meta.helm.sh/release-namespace": self.identity.name,
                }
            self.documents[claim_path] = {
                "apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {
                    "name": name, "namespace": self.identity.name,
                    "uid": record.claim_uid, "resourceVersion": "2",
                    "labels": labels, "annotations": annotations,
                },
                "spec": {
                    "storageClassName": "local-path", "volumeName": record.volume_name,
                    "accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem",
                },
                "status": {"phase": "Bound"},
            }
            self.documents[volume_path] = {
                "apiVersion": "v1", "kind": "PersistentVolume",
                "metadata": {
                    "name": record.volume_name, "uid": record.volume_uid,
                    "resourceVersion": "3",
                    "annotations": {"pv.kubernetes.io/provisioned-by": "rancher.io/local-path"},
                },
                "spec": {
                    "storageClassName": "local-path",
                    "persistentVolumeReclaimPolicy": "Delete",
                    "accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem",
                    "claimRef": {
                        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
                        "namespace": self.identity.name, "name": name, "uid": record.claim_uid,
                    },
                },
                "status": {"phase": "Bound"},
            }

    async def handle(self, request):
        path = request.url.path
        assert request.url.scheme == "https" and request.url.host == "kubernetes.default.svc"
        assert request.method == "GET" and not request.content
        assert path in self.documents, "A request escaped the simulated store."
        self.calls.append(path)
        # No caller transaction or scheduling lock may span this HTTP request.
        async with self.factory.begin() as db:
            assert await db.scalar(select(
                func.pg_try_advisory_xact_lock(STORE_ADMISSION_LOCK)
            )) is True
        if self.on_request is not None:
            await self.on_request(self, path)
        if path == self.pause_path:
            self.active = True
            self.started.set()
            try:
                await self.resume.wait()
            finally:
                self.active = False
        document = self.documents[path]
        return httpx.Response(200 if document is not None else 404, json=deepcopy(document or {}))

    async def run(self):
        self.closed = False
        raw = KubernetesConnection(
            lambda: "fixture.volume.signature", TLS,
            transport=httpx.MockTransport(self.handle),
        )
        try:
            return await prepare_store_volumes(self.factory, self.claim, raw, SCOPE)
        finally:
            await raw.aclose()
            self.closed = raw._client.is_closed


async def expect(kind, operation):
    try:
        await operation
    except kind as error:
        assert "PRIVATE" not in str(error)
        return
    raise AssertionError("Expected " + str(kind))


async def verify_volume_stage(factory, reset, fresh, proof, change, snapshot):
    async with factory() as db:
        schema = await db.scalar(select(func.current_schema()))
        assert re.fullmatch(r"urumi_volume_bind_test_[0-9a-f]{32}", schema)

    async def peer(claim=None):
        claim = await fresh() if claim is None else claim
        records = [await proof(claim, name) for name in STORE_CLAIM_NAMES]
        return Peer(factory, claim, records)

    await reset()
    current = await peer()
    before = await snapshot()
    assert await current.run() == current.records
    saved = await snapshot()
    assert len(saved["store_volumes"]) == len(saved["security_events"]) == 2
    assert saved["store_operations"] == before["store_operations"]
    assert await current.run() == current.records
    assert await snapshot() == saved and current.closed
    print("Real read/save/readback stores both volumes once; DB locks close before every simulated HTTP request")

    await reset()
    current = await peer()
    pending = current.documents[current.claim_paths["wordpress-data"]]
    pending["status"]["phase"] = "Pending"
    await expect(StoreVolumePending, current.run())
    saved = await snapshot()
    assert len(saved["store_volumes"]) == len(saved["security_events"]) == 1
    pending["status"]["phase"] = "Bound"
    assert await current.run() == current.records
    saved = await snapshot()
    assert len(saved["store_volumes"]) == len(saved["security_events"]) == 2
    print("A pending second claim preserves the first committed binding; recovery adds only the missing record")

    await reset()
    current = await peer()
    original = current.claim
    async def lose_lease(item, path):
        if path == item.claim_paths["wordpress-data"]:
            item.on_request = None
            await change(
                StoreOperation, item.claim.operation_id,
                lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
            )
    current.on_request = lose_lease
    await expect(StoreJobClaimLost, current.run())
    saved = await snapshot()
    assert len(saved["store_volumes"]) == len(saved["security_events"]) == 1
    count = len(current.calls)
    await expect(StoreJobClaimLost, current.run())
    assert len(current.calls) == count
    current.claim = await jobs.claim_next(factory)
    assert current.claim and current.claim.lease_token != original.lease_token
    assert current.claim.deadline_at == original.deadline_at
    assert await current.run() == current.records
    assert len((await snapshot())["security_events"]) == 2
    print("Lease loss stops HTTP work; a replacement claim reuses saved storage without changing the deadline")

    await reset()
    current = await peer()
    async def replace_pv(item, path):
        if path == item.claim_paths["data-mysql-0"] and item.calls.count(path) == 3:
            item.on_request = None
            item.documents[item.volume_paths["data-mysql-0"]]["metadata"]["uid"] = "replacement-uid"
    current.on_request = replace_pv
    await expect(StoreVolumeError, current.run())
    saved = await snapshot()
    assert len(saved["store_volumes"]) == len(saved["security_events"]) == 2
    await expect(StoreVolumeError, current.run())
    assert await snapshot() == saved
    print("A PV replaced after saving rejects successful preparation; committed identities are never adopted or rewritten")

    await reset()
    current = await peer()
    async def replace_namespace(item, path):
        if path == item.volume_paths["data-mysql-0"]:
            item.on_request = None
            item.documents[item.ns_path]["metadata"]["uid"] = "replacement-namespace"
    current.on_request = replace_namespace
    await expect(StoreNamespaceError, current.run())
    saved = await snapshot()
    assert not saved["store_volumes"] and not saved["security_events"]

    await reset()
    current = await peer()
    current.pause_path = current.claim_paths["wordpress-data"]
    task = asyncio.create_task(current.run())
    try:
        await asyncio.wait_for(current.started.wait(), 10)
        saved = await snapshot()
        assert len(saved["store_volumes"]) == len(saved["security_events"]) == 1
        task.cancel()
        await expect(asyncio.CancelledError, task)
    finally:
        current.resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert current.closed and not current.active
    assert await snapshot() == saved
    original = current.claim
    assert await jobs.release_claim(factory, original, retry_after_seconds=0)
    current.claim = await jobs.claim_next(factory)
    assert current.claim and current.claim.lease_token != original.lease_token
    assert current.claim.deadline_at == original.deadline_at
    current.pause_path = None
    assert await current.run() == current.records
    assert len((await snapshot())["security_events"]) == 2
    print("Namespace replacement rejects writes; cancellation closes HTTP before release and recoverable progress survives")

    await reset()
    first = await peer()
    second = await peer(await fresh(98802))
    assert await first.run() == first.records
    before = await snapshot()
    reference = second.documents[second.volume_paths["data-mysql-0"]]["spec"]["claimRef"]
    reference.update(namespace=first.identity.name, uid=first.records[0].claim_uid)
    await expect(StoreVolumeError, second.run())
    assert await snapshot() == before
    print("Two stores retain separate identities; a cross-store PV reference cannot create storage records")
    print("PostgreSQL and simulated Kubernetes volume-stage verification passed. No live cluster resources changed.")
