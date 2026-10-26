"""Cleanup composition with real namespace/claim/inventory helpers and mock I/O."""

import asyncio
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sys
import time
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from app import store_cleanup as module
from app import store_claim_stage as claims
from app import store_namespace_stage as namespaces
from app import store_guarded_kubernetes as guards
from app.store_claims import StoreClaimIdentity
from app.store_job_guard import CheckedStoreJob, StoreJobClaimLost
from app.store_kube_connection import KubernetesAPIError
from app.store_namespace import StoreNamespaceError, namespace_for
from app.store_volume_binding import VolumeBindingUnavailable
from app.store_volume_recovery import _known_records
from app.store_volumes import STORE_CLAIM_NAMES, StoreVolumeBinding, StoreVolumeError
from verify_store_volume_api import Fixture as BaseFixture, SCOPE, error
from verify_store_volume_pages import volume, page
from verify_store_volumes import pair


class Fixture(BaseFixture):
    def __init__(self):
        super().__init__()
        self.uid = "namespace-uid"
        self.records = {}
        self.events = []
        self.pvcs, self.pvs = {}, {}
        for name in STORE_CLAIM_NAMES:
            pvc, _ = pair(name)
            pv = volume(name)
            pvc["metadata"]["uid"] = pv["spec"]["claimRef"]["uid"]
            pvc["spec"]["volumeName"] = pv["metadata"]["name"]
            self.pvcs[name], self.pvs[name] = pvc, pv
        self.deletes = 0
        self.immediate = False
        self.remove_volumes = False
        self.fail_save = None
        self.uncertain = False
        self.pause_pages = False
        self.lost_after_save = False

    async def require(
        self, factory, claim, *, for_cleanup=False, expected_namespace_uid=None,
    ):
        self.checks += 1
        assert factory == self.factory and claim == self.claim and for_cleanup
        assert expected_namespace_uid in (None, self.uid)
        if self.lost:
            raise StoreJobClaimLost("The worker claim was lost.")
        return CheckedStoreJob(
            claim, self.uid, True, time.monotonic() + 30,
        )

    async def load(self, factory, claim, *, namespace_uid, for_cleanup):
        await self.require(
            factory, claim, for_cleanup=for_cleanup,
            expected_namespace_uid=namespace_uid,
        )
        return _known_records(
            namespace_for(claim), namespace_uid,
            tuple(self.records[name] for name in STORE_CLAIM_NAMES if name in self.records),
        )

    async def record(self, factory, claim, item, *, for_cleanup):
        await self.require(factory, claim, for_cleanup=for_cleanup)
        previous = self.records.get(item.claim_name)
        if previous is not None:
            assert previous.claim_uid == item.claim_uid
            if previous.volume_name is not None:
                assert item.volume_name in (None, previous.volume_name)
                item = replace(item, volume_name=previous.volume_name)
        if previous is None or previous.volume_name != item.volume_name:
            self.records[item.claim_name] = item
            self.events.append(("claim", item.claim_name))
        return item

    async def bind(self, factory, claim, item, *, for_cleanup):
        await self.require(factory, claim, for_cleanup=for_cleanup)
        assert type(item) is StoreVolumeBinding
        if self.fail_save == item.claim_name:
            raise VolumeBindingUnavailable("Saved storage is unavailable.")
        previous = self.records.get(item.claim_name)
        if previous is not None:
            assert previous.claim_uid == item.claim_uid
            assert previous.volume_name in (None, item.volume_name)
        if previous != item:
            self.records[item.claim_name] = item
            self.events.append(("volume", item.claim_name))
        if self.lost_after_save:
            self.lost = True
        return item

    async def save_uid(self, factory, claim, document, *, for_cleanup):
        await self.require(factory, claim, for_cleanup=for_cleanup)
        SCOPE.check_namespace(namespace_for(claim), document, allow_terminating=True)
        self.uid = document["metadata"]["uid"]
        self.events.append(("namespace", self.uid))
        return self.uid

    async def peer(self, request):
        path = request.url.path
        self.calls.append((request.method, request.url.raw_path.decode("ascii")))
        if path == self.ns_path:
            if request.method == "GET":
                return httpx.Response(
                    200 if self.namespace is not None else 404,
                    json=deepcopy(self.namespace or {}),
                )
            assert request.method == "DELETE" and self.namespace is not None
            import json
            body = json.loads(request.content)
            assert body["preconditions"] == {
                "uid": self.uid,
                "resourceVersion": self.namespace["metadata"]["resourceVersion"],
            }
            assert self.uid == self.namespace["metadata"]["uid"]
            self.deletes += 1
            self.namespace["metadata"]["deletionTimestamp"] = "fixture-time"
            self.namespace["status"]["phase"] = "Terminating"
            if self.immediate:
                self.namespace = None
            if self.remove_volumes:
                self.pvs.clear()
            if self.uncertain:
                raise httpx.ReadTimeout("PRIVATE LOST DELETE REPLY")
            return httpx.Response(202, json={"kind": "Status"})
        prefix = self.ns_path + "/persistentvolumeclaims/"
        if path.startswith(prefix):
            assert request.method == "GET"
            item = self.pvcs.get(path[len(prefix):]) if self.namespace is not None else None
            return httpx.Response(200 if item is not None else 404, json=deepcopy(item or {}))
        assert path == "/api/v1/persistentvolumes" and request.method == "GET"
        self.active = True
        self.started.set()
        try:
            if self.pause_pages:
                await asyncio.sleep(30)
            document = page(self.pvs.values())
            for item in document["items"]:
                item.pop("apiVersion")
                item.pop("kind")
            return httpx.Response(200, json=document)
        finally:
            self.active = False

    async def run(self):
        with ExitStack() as stack:
            for target in (module, claims, namespaces, guards):
                stack.enter_context(patch.object(target, "require_claim", self.require))
            stack.enter_context(patch.object(module, "load_store_storage", self.load))
            stack.enter_context(patch.object(module, "bind_store_volume", self.bind))
            stack.enter_context(patch.object(claims, "record_store_claim", self.record))
            stack.enter_context(patch.object(namespaces, "bind_namespace", self.save_uid))
            return await super().run(
                lambda raw: module.cleanup_store_resources(
                    self.factory, self.claim, raw, SCOPE,
                )
            )


async def check():
    baseline = asyncio.all_tasks()
    f = Fixture()
    result = await f.run()
    assert not result.namespace_absent and f.deletes == 1
    assert len(result.storage) == len(result.remaining_volumes) == 2
    assert result.unresolved_claims == ()
    assert not hasattr(result, "complete")
    before = deepcopy((f.records, f.events))
    assert not (await f.run()).namespace_absent
    assert f.deletes == 1 and (f.records, f.events) == before
    f.namespace, f.pvs, f.pvcs = None, {}, {}
    result = await f.run()
    assert result.namespace_absent and result.remaining_volumes == ()
    assert result.storage == tuple(f.records[name] for name in STORE_CLAIM_NAMES)
    assert (f.records, f.events) == before and f.closed
    print("Both volumes are recorded before deletion; terminating passes and later absence reuse saved identities")

    f = Fixture()
    f.uid = None
    f.immediate = f.remove_volumes = True
    result = await f.run()
    assert result.namespace_uid == "namespace-uid" and not result.namespace_absent
    result = await f.run()
    assert result.namespace_absent
    assert f.events[0] == ("namespace", "namespace-uid")
    assert result.unresolved_claims == () and not result.remaining_volumes
    f = Fixture()
    f.uid = None
    f.namespace = None
    result = await f.run()
    assert result.namespace_uid is None and result.namespace_absent
    assert not f.events and not f.deletes and len(f.calls) == 1
    print("Unrecorded namespace ownership is saved before cleanup; never-bound missing namespaces create no resources")

    f = Fixture()
    for pvc in f.pvcs.values():
        pvc["status"]["phase"] = "Pending"
        pvc["spec"].pop("volumeName")
    late = deepcopy(f.pvs)
    f.pvs.clear()
    f.immediate = True
    result = await f.run()
    assert not result.namespace_absent
    result = await f.run()
    assert result.namespace_absent
    assert result.unresolved_claims == STORE_CLAIM_NAMES
    assert all(type(item) is StoreClaimIdentity for item in result.storage)
    f.pvs = late
    result = await f.run()
    assert len(result.remaining_volumes) == 2 and not result.unresolved_claims
    f.pvs.clear()
    result = await f.run()
    assert not result.remaining_volumes and not result.unresolved_claims
    print("Pending claims survive namespace removal; late PVs are recovered without rewriting their ownership")

    f = Fixture()
    f.fail_save = "wordpress-data"
    await error(VolumeBindingUnavailable, f.run())
    assert not f.deletes
    assert type(f.records["data-mysql-0"]) is StoreVolumeBinding
    first = f.records["data-mysql-0"]
    f.fail_save = None
    await f.run()
    assert f.records["data-mysql-0"] == first and f.deletes == 1
    f = Fixture()
    f.lost_after_save = True
    await error(StoreJobClaimLost, f.run())
    assert not f.deletes
    print("A partial database save remains recoverable; unavailable storage or lost leases block namespace deletion")

    for change in ("namespace", "volume"):
        f = Fixture()
        if change == "namespace":
            f.namespace["metadata"]["labels"]["urumi.ai/owner-id"] = "999"
        else:
            f.pvs["wordpress-data"]["spec"]["claimRef"]["namespace"] = "other-store"
            # A saved link makes the foreign reference an ownership conflict.
            item = f.pvcs["wordpress-data"]
            f.records["wordpress-data"] = StoreClaimIdentity(
                namespace_for(f.claim), f.uid, "wordpress-data",
                item["metadata"]["uid"], item["spec"]["volumeName"],
            )
        await error((StoreNamespaceError, StoreVolumeError), f.run())
        assert not f.deletes
    f = Fixture()
    f.uncertain = True
    caught = await error(KubernetesAPIError, f.run())
    assert caught.write_may_have_applied and f.deletes == 1
    f.uncertain = False
    await f.run()
    assert f.deletes == 1
    print("Ownership conflicts send no deletion; an accepted deletion with a lost reply is inspected without another write")

    f = Fixture()
    f.pause_pages = True
    task = asyncio.create_task(f.run())
    waiter = asyncio.create_task(f.started.wait())
    try:
        done, _ = await asyncio.wait(
            (task, waiter), timeout=5, return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            await task
            raise AssertionError("Cleanup ended before the pause.")
        assert waiter in done
        task.cancel()
        await error(asyncio.CancelledError, task)
    finally:
        task.cancel()
        waiter.cancel()
        await asyncio.gather(task, waiter, return_exceptions=True)
    assert f.closed and not f.active and not f.deletes and len(f.records) == 2
    f.pause_pages = False
    f.claim = replace(f.claim, attempt=2, lease_token=uuid4())
    await f.run()
    assert f.deletes == 1
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)
    print("Cancellation closes HTTP before recovery; saved PVC progress survives and no tasks remain")
    print("Cleanup integration passed. All database and Kubernetes operations were fixtures.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 30))
