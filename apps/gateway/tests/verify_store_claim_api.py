"""Scoped PVC identity reads; ownership storage and HTTP are mocked."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import ssl
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from app import store_claim_api as module
from app import store_guarded_kubernetes as guards
from app.store_claims import StoreClaimIdentity
from app.store_kube_connection import KubernetesAPIError, KubernetesConnection
from app.store_job_guard import StoreJobClaimLost, StoreJobGuardUnavailable
from app.store_namespace import StoreNamespaceError
from app.store_volumes import STORE_CLAIM_NAMES, StoreVolumeError, StoreVolumePending
from verify_store_volume_api import Fixture as VolumeFixture, SCOPE, NOW
import verify_store_volumes as data


class Fixture(VolumeFixture):
    def __init__(self, name="wordpress-data"):
        super().__init__(name)
        self.after_read = None
        self.after_number = 2
        self.pvc_status = 200

    async def peer(self, request):
        target = request.url.path
        self.calls.append(target)
        assert request.method == "GET" and not request.content
        assert target in (self.ns_path, self.pvc_path)
        item = deepcopy(self.namespace if target == self.ns_path else self.pvc)
        if target == self.pvc_path:
            self.active = True
            self.started.set()
            try:
                if self.delay:
                    await asyncio.sleep(self.delay)
                if self.pvc_status != 200:
                    return httpx.Response(self.pvc_status, json={"message": "PRIVATE"})
            finally:
                self.active = False
        if self.after_read and len(self.calls) == self.after_number:
            callback, self.after_read = self.after_read, None
            callback()
        return httpx.Response(200 if item is not None else 404, json=item or {})

    async def run(self, callback=None, **changes):
        raw = KubernetesConnection(
            lambda: "fixture.claim.signature", ssl.create_default_context(),
            transport=httpx.MockTransport(self.peer),
        )
        options = {"namespace_uid": "namespace-uid", "claim_name": self.name}
        options.update(changes)
        try:
            with patch.object(guards, "require_claim", self.require):
                if callback:
                    return await callback(raw)
                return await module.read_store_claim(
                    self.factory, self.claim, raw, SCOPE, **options,
                )
        finally:
            await raw.aclose()
            self.closed = raw._client.is_closed

    def pending(self):
        self.pvc["status"]["phase"] = "Pending"
        self.pvc["spec"].pop("volumeName", None)


async def error(kind, operation):
    try:
        await operation
    except kind as caught:
        assert "PRIVATE" not in str(caught)
        return caught
    raise AssertionError("Expected " + str(kind))


async def check():
    baseline = asyncio.all_tasks()
    for name in STORE_CLAIM_NAMES:
        for pending in (False, True):
            f = Fixture(name)
            if pending:
                f.pending()
            before = deepcopy((f.namespace, f.pvc))
            result = await f.run()
            assert type(result) is StoreClaimIdentity
            assert result.claim_name == name and result.claim_uid == "claim-uid"
            assert result.volume_name == (None if pending else "pvc-fixture")
            assert (f.namespace, f.pvc) == before
            assert f.calls == [
                f.ns_path, f.pvc_path, f.ns_path, f.pvc_path, f.ns_path,
            ]
            assert f.checks >= 2 * len(f.calls) and f.closed
            assert await f.run(expected=result) == result
    f = Fixture()
    bound = data.check(f.name, f.pvc, f.pv)
    assert (await f.run(expected=bound)).volume_name == bound.volume_name
    print("Both fixed PVCs return Pending/Bound identities; saved full bindings remain compatible")

    f = Fixture()
    f.pending()
    def bind():
        f.pvc["spec"]["volumeName"] = "pvc-fixture"
        f.pvc["status"]["phase"] = "Bound"
    f.after_read = bind
    assert (await f.run()).volume_name == "pvc-fixture"
    for field in ("uid", "namespace"):
        f = Fixture()
        f.after_read = lambda: f.pvc["metadata"].update({field: "replacement"})
        await error(StoreVolumeError, f.run())
    f = Fixture()
    f.after_read = lambda: f.pvc["spec"].update(volumeName="different-volume")
    await error(StoreVolumeError, f.run())
    f = Fixture()
    f.after_read = f.pending
    await error(StoreVolumeError, f.run())
    f = Fixture()
    f.after_read = lambda: f.namespace["metadata"].update(uid="replacement")
    await error(StoreNamespaceError, f.run())
    for target in ("pvc", "namespace"):
        f = Fixture()
        setattr(f, target, None)
        await error(StoreVolumePending, f.run())
        await error(StoreVolumePending, f.run(for_cleanup=True))
        assert f.closed
    print("Pending-to-Bound progress is accepted; replaced claims/namespaces and missing resources cannot pass")

    f = Fixture()
    saved = await f.run()
    for changes in (
        {"namespace_uid": None}, {"claim_name": "other"},
        {"expected": replace(saved, namespace_uid="other")},
        {"expected": replace(saved, claim_name="data-mysql-0")},
    ):
        f = Fixture()
        await error(ValueError, f.run(**changes))
        assert not f.calls
    f = Fixture()
    async def forbidden(raw):
        reader = module._ClaimReads(
            raw, f.factory, f.claim, SCOPE,
            namespace_uid="namespace-uid", claim_name=f.name,
        )
        for target in (
            f.pv_path, f.pvc_path.rsplit("/", 1)[0], f.pvc_path + "/status",
            f.pvc_path.replace(data.IDENTITY.name, "platform"),
            f.pvc_path.replace(f.name, "data-mysql-0"),
        ):
            await error(ValueError, reader.request("GET", target))
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            await error(ValueError, reader.request(method, f.pvc_path))
        await error(ValueError, reader.request("GET", f.pvc_path, body={}))
        base = guards.GuardedKubernetesConnection(
            raw, f.factory, f.claim, SCOPE, namespace_uid="namespace-uid",
        )
        await error(ValueError, base.request("GET", f.pvc_path))
    await f.run(forbidden)
    assert not f.calls and f.checks == 0
    print("Cross-store/other-claim routes, PV reads, lists, subresources and all writes send no HTTP")

    for failure in (StoreJobClaimLost("PRIVATE"), StoreJobGuardUnavailable("PRIVATE")):
        f = Fixture()
        f.failure = failure
        caught = await error(guards.StoreKubernetesStopped, f.run())
        assert not f.calls and not caught.write_may_have_applied
    f = Fixture()
    f.remaining = 0
    await error(guards.StoreKubernetesStopped, f.run())
    assert not f.calls
    f = Fixture()
    f.after_read = lambda: setattr(f, "lost", True)
    await error(guards.StoreKubernetesStopped, f.run())
    assert len(f.calls) == 2
    for status in (403, 503):
        f = Fixture()
        f.pvc_status = status
        caught = await error(KubernetesAPIError, f.run())
        assert caught.status_code == status and not caught.write_may_have_applied
        assert f.calls.count(f.pvc_path) == 1
    print("Live leases bracket every read; lost ownership and denied reads stop without retry")

    f = Fixture()
    f.claim = replace(f.claim, deadline_at=NOW - timedelta(seconds=1))
    f.namespace["status"]["phase"] = "Terminating"
    f.namespace["metadata"]["deletionTimestamp"] = "fixture-time"
    f.pvc["metadata"]["deletionTimestamp"] = "fixture-time"
    f.pvc["status"]["phase"] = "Lost"
    await error(guards.StoreKubernetesStopped, f.run())
    assert await f.run(for_cleanup=True)
    f = Fixture()
    f.claim = replace(f.claim, action="delete")
    await error(ValueError, f.run())
    assert not f.calls
    assert await f.run(for_cleanup=True)
    f.lost = True
    await error(guards.StoreKubernetesStopped, f.run(for_cleanup=True))
    print("Terminating and overdue claims need explicit cleanup mode; missing resources never prove deletion")

    f = Fixture()
    f.delay = 1
    with patch.object(module, "_CHECK_TIMEOUT_SECONDS", 0.1):
        caught = await error(KubernetesAPIError, f.run())
    assert not caught.write_may_have_applied and f.closed and not f.active
    f = Fixture()
    f.delay = 1
    task = asyncio.create_task(f.run())
    signal = asyncio.create_task(f.started.wait())
    try:
        done, _ = await asyncio.wait(
            (task, signal), timeout=2, return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            await task
            raise AssertionError("The reader returned before its cancellation checkpoint.")
        assert signal in done, "The reader did not reach its cancellation checkpoint."
        task.cancel()
        await error(asyncio.CancelledError, task)
    finally:
        for pending in (task, signal):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, signal, return_exceptions=True)
    assert f.closed and not f.active and f.calls.count(f.pvc_path) == 1
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)
    print("Timeouts and cancellation close reads without retries or leftover tasks")
    print("Store claim reader verification passed. No live services or database calls were made.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 30))
