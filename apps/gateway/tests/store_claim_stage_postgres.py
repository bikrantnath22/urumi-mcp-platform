"""Claim capture: real isolated PostgreSQL and simulated Kubernetes HTTP."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import re

import httpx
from sqlalchemy import func, select

from app import store_jobs as jobs
from app.models import Store, StoreOperation, StoreVolume
from app.store_claim_stage import capture_store_claim
from app.store_claims import StoreClaimIdentity
from app.store_job_guard import StoreJobClaimLost
from app.store_kube_connection import KubernetesConnection
from app.store_namespace import StoreNamespaceError
from app.store_volume_binding import bind_store_volume
from app.store_volumes import STORE_CLAIM_NAMES, StoreVolumeError, StoreVolumePending
from store_claim_recording_postgres import started, stop
from store_volume_stage_postgres import Peer, SCOPE, TLS, expect


class ClaimPeer(Peer):
    def pending(self, name):
        document = self.documents[self.claim_paths[name]]
        document["status"]["phase"] = "Pending"
        document["spec"].pop("volumeName", None)

    def observed(self, name, *, named=False):
        record = next(item for item in self.records if item.claim_name == name)
        return StoreClaimIdentity(
            record.identity, record.namespace_uid, name, record.claim_uid,
            record.volume_name if named else None,
        )

    async def handle(self, request):
        assert request.url.path in (self.ns_path, *self.claim_paths.values())
        return await super().handle(request)

    async def run(self, name="wordpress-data", *, for_cleanup=False):
        self.closed = False
        raw = KubernetesConnection(
            lambda: "fixture.claim.signature", TLS,
            transport=httpx.MockTransport(self.handle),
        )
        try:
            return await capture_store_claim(
                self.factory, self.claim, raw, SCOPE,
                claim_name=name, for_cleanup=for_cleanup,
            )
        finally:
            await raw.aclose()
            self.closed = raw._client.is_closed


async def verify_claim_stage(factory, reset, fresh, proof, change, snapshot):
    async with factory() as db:
        schema = await db.scalar(select(func.current_schema()))
        assert re.fullmatch(r"urumi_volume_bind_test_[0-9a-f]{32}", schema)

    async def peer(job=None):
        job = await fresh() if job is None else job
        records = [await proof(job, name) for name in STORE_CLAIM_NAMES]
        current = ClaimPeer(factory, job, records)
        for name in STORE_CLAIM_NAMES:
            current.pending(name)
        return current

    await reset()
    current = await peer()
    before = await snapshot()
    for name in STORE_CLAIM_NAMES:
        assert await current.run(name) == current.observed(name)
    saved = await snapshot()
    assert len(saved["store_volumes"]) == len(saved["security_events"]) == 2
    assert saved["store_operations"] == before["store_operations"]
    for name in STORE_CLAIM_NAMES:
        assert await current.run(name) == current.observed(name)
    assert await snapshot() == saved and current.closed
    print("Both Pending PVCs are recorded once; readbacks release DB locks before HTTP and replay changes nothing")

    await reset()
    current = await peer()
    name = "wordpress-data"
    path = current.claim_paths[name]
    async def gain_name(item, target):
        if target == path and item.calls.count(path) == 3:
            item.on_request = None
            async with factory() as db:
                row = await db.get(StoreVolume, (item.claim.store_id, name))
                assert row and row.volume_name is None and row.volume_uid is None
            document = item.documents[path]
            document["spec"]["volumeName"] = item.observed(name, named=True).volume_name
            document["status"]["phase"] = "Bound"
    current.on_request = gain_name
    assert await current.run(name) == current.observed(name, named=True)
    saved = await snapshot()
    assert len(saved["store_volumes"]) == 1 and len(saved["security_events"]) == 2
    full = next(item for item in current.records if item.claim_name == name)
    assert await bind_store_volume(factory, current.claim, full) == full
    saved = await snapshot()
    assert await current.run(name) == current.observed(name, named=True)
    assert await snapshot() == saved
    async with factory() as db:
        assert (await db.get(StoreVolume, (current.claim.store_id, name))).volume_uid == full.volume_uid
    print("A PV name learned after the first commit is saved; later claim captures preserve full PV bindings")

    await reset()
    current = await peer()
    missing = current.claim_paths["wordpress-data"]
    original = deepcopy(current.documents[missing])
    current.documents[missing] = None
    assert await current.run("data-mysql-0") == current.observed("data-mysql-0")
    saved = await snapshot()
    await expect(StoreVolumePending, current.run())
    assert await snapshot() == saved
    current.documents[missing] = original
    assert await current.run() == current.observed("wordpress-data")
    assert len((await snapshot())["store_volumes"]) == 2
    print("A missing second PVC preserves the first committed identity; recovery records only the missing claim")

    await reset()
    current = await peer()
    path = current.claim_paths["wordpress-data"]
    old = current.claim
    async def lose_lease(item, target):
        if target == path and item.calls.count(path) == 3:
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
    assert len(current.calls) == count and await snapshot() == saved
    current.claim = await jobs.claim_next(factory)
    assert current.claim and current.claim.lease_token != old.lease_token
    assert current.claim.deadline_at == old.deadline_at
    saved = await snapshot()
    assert await current.run() == current.observed("wordpress-data")
    assert await snapshot() == saved
    print("Lease loss after commit stops HTTP; a replacement worker reuses the identity and original deadline")

    for replacement in ("claim", "namespace"):
        await reset()
        current = await peer()
        path = current.claim_paths["wordpress-data"]
        async def replace_resource(item, target):
            if target == path and item.calls.count(path) == 3:
                item.on_request = None
                selected = path if replacement == "claim" else item.ns_path
                item.documents[selected]["metadata"]["uid"] = "replacement"
        current.on_request = replace_resource
        kind = StoreVolumeError if replacement == "claim" else StoreNamespaceError
        await expect(kind, current.run())
        saved = await snapshot()
        assert len(saved["store_volumes"]) == len(saved["security_events"]) == 1
        await expect(kind, current.run())
        assert await snapshot() == saved
    print("Replaced PVCs and namespaces reject completion without adopting or rewriting committed identities")

    await reset()
    current = await peer()
    path = current.claim_paths["wordpress-data"]
    async def pause_after_commit(item, target):
        if target == path and item.calls.count(path) == 3:
            item.on_request = None
            item.pause_path = path
    current.on_request = pause_after_commit
    task = asyncio.create_task(current.run())
    try:
        await started(task, current.started)
        saved = await snapshot()
        assert len(saved["store_volumes"]) == len(saved["security_events"]) == 1
        task.cancel()
        await expect(asyncio.CancelledError, task)
    finally:
        current.resume.set()
        await stop(task)
    assert current.closed and not current.active
    assert await snapshot() == saved
    old = current.claim
    assert await jobs.release_claim(factory, old, retry_after_seconds=0)
    current.claim = await jobs.claim_next(factory)
    assert current.claim and current.claim.lease_token != old.lease_token
    assert current.claim.deadline_at == old.deadline_at
    current.pause_path = None
    saved = await snapshot()
    assert await current.run() == current.observed("wordpress-data")
    assert await snapshot() == saved
    print("Cancellation closes HTTP before release; recovery preserves the previously committed PVC record")

    await reset()
    current = await peer(await fresh(seconds=-1))
    await expect(StoreJobClaimLost, current.run())
    assert not current.calls and not (await snapshot())["store_volumes"]
    for document in current.documents.values():
        if document.get("kind") in ("Namespace", "PersistentVolumeClaim"):
            document["metadata"]["deletionTimestamp"] = "fixture-time"
    current.documents[current.ns_path]["status"]["phase"] = "Terminating"
    assert await current.run(for_cleanup=True) == current.observed("wordpress-data")
    await change(Store, current.claim.store_id, status="Deleting", desired_state="absent")
    await change(StoreOperation, current.claim.operation_id, action="delete")
    current.claim = replace(current.claim, action="delete")
    await expect(ValueError, current.run())
    saved = await snapshot()
    assert await current.run(for_cleanup=True) == current.observed("wordpress-data")
    assert await snapshot() == saved
    print("Overdue and delete captures require explicit cleanup mode and never change terminal state")

    await reset()
    first, second = await peer(), await peer(await fresh(98802))
    assert await first.run() == first.observed("wordpress-data")
    second.documents[second.claim_paths["wordpress-data"]]["metadata"]["uid"] = first.observed("wordpress-data").claim_uid
    saved = await snapshot()
    await expect(StoreVolumeError, second.run())
    assert await snapshot() == saved
    print("A different store cannot adopt a recorded PVC UID; uniqueness errors roll back cleanly")
    print("PostgreSQL claim capture verification passed. No live Kubernetes resources changed.")
