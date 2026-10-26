"""Read-only volume checks; ownership storage and HTTP are mocked."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import ssl
import sys
import time
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from app import store_volume_api as module
from app import store_guarded_kubernetes as guards
from app.store_cluster_scope import StoreClusterScope
from app.store_job_guard import CheckedStoreJob, StoreJobClaimLost, StoreJobGuardUnavailable
from app.store_jobs import StoreJobClaim
from app.store_kube_connection import KubernetesAPIError, KubernetesConnection
from app.store_namespace import StoreNamespaceError
from app.store_volumes import STORE_CLAIM_NAMES, StoreVolumeError, StoreVolumePending
import verify_store_volumes as data


SCOPE = StoreClusterScope("platform", "platform")
IDENTITY = data.IDENTITY
NOW = datetime.now(timezone.utc)
CLAIM = StoreJobClaim(
    operation_id=uuid4(), store_id=IDENTITY.store_id, owner_id=IDENTITY.owner_id,
    action="provision", generation=1, attempt=1, namespace=IDENTITY.name,
    release_name="store", name="Fixture tea", slug="fixture-tea",
    url="http://fixture-tea.localtest.me", deadline_at=NOW + timedelta(minutes=20),
    checked_at=NOW, lease_expires_at=NOW + timedelta(seconds=60), lease_token=uuid4(),
)


class Fixture:
    def __init__(self, name="wordpress-data"):
        self.name = name
        self.claim = CLAIM
        self.namespace = SCOPE.namespace_manifest(IDENTITY)
        self.namespace["metadata"].update(uid="namespace-uid", resourceVersion="1")
        self.namespace["status"] = {"phase": "Active"}
        self.pvc, self.pv = data.pair(name)
        self.ns_path = "/api/v1/namespaces/" + IDENTITY.name
        self.pvc_path = self.ns_path + "/persistentvolumeclaims/" + name
        self.pv_path = "/api/v1/persistentvolumes/pvc-fixture"
        self.calls = []
        self.checks = 0
        self.lost = False
        self.failure = None
        self.remaining = 30
        self.after = None
        self.status = 200
        self.delay = 0
        self.started = asyncio.Event()
        self.active = False
        self.closed = False

    def factory(self):
        raise AssertionError("No database connection belongs in this fixture.")

    async def require(self, factory, claim, *, for_cleanup=False, expected_namespace_uid):
        self.checks += 1
        assert factory == self.factory and claim == self.claim
        assert expected_namespace_uid == "namespace-uid"
        if self.failure is not None:
            raise self.failure
        if self.lost or (not for_cleanup and claim.deadline_at <= datetime.now(timezone.utc)):
            raise StoreJobClaimLost("PRIVATE LOST CLAIM")
        return CheckedStoreJob(
            claim=claim, namespace_uid="namespace-uid", for_cleanup=for_cleanup,
            stop_at=time.monotonic() + self.remaining,
        )

    async def peer(self, request):
        target = request.url.path
        self.calls.append(target)
        assert request.method == "GET" and not request.content
        items = {self.ns_path: self.namespace, self.pvc_path: self.pvc, self.pv_path: self.pv}
        assert target in items
        item = deepcopy(items[target])
        if target == self.pv_path:
            self.active = True
            self.started.set()
            try:
                if self.delay:
                    await asyncio.sleep(self.delay)
                if self.after:
                    callback, self.after = self.after, None
                    callback()
                if self.status != 200:
                    return httpx.Response(self.status, json={"message": "PRIVATE HTTP"})
            finally:
                self.active = False
        return httpx.Response(200 if item is not None else 404, json=item or {})

    async def run(self, callback=None, **changes):
        raw = KubernetesConnection(
            lambda: "fixture.storage.signature", ssl.create_default_context(),
            transport=httpx.MockTransport(self.peer),
        )
        options = {"namespace_uid": "namespace-uid", "claim_name": self.name}
        options.update(changes)
        try:
            with patch.object(guards, "require_claim", self.require):
                if callback:
                    return await callback(raw)
                return await module.read_store_volume(
                    self.factory, self.claim, raw, SCOPE, **options,
                )
        finally:
            await raw.aclose()
            self.closed = raw._client.is_closed


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
        f = Fixture(name)
        before = deepcopy((f.namespace, f.pvc, f.pv))
        saved = await f.run()
        assert saved.claim_name == name and saved.claim_uid == "claim-uid"
        assert saved.volume_uid == "volume-uid"
        assert (f.namespace, f.pvc, f.pv) == before
        assert f.calls == [f.ns_path, f.pvc_path, f.ns_path, f.pv_path, f.pvc_path, f.ns_path]
        assert f.checks >= 2 * len(f.calls)
        assert await f.run(expected=saved) == saved
        assert f.closed
    print("Both claims return immutable bindings; namespaces and live job ownership bracket scoped reads")

    f = Fixture()
    async def forbidden(raw):
        reader = module._VolumeReads(
            raw, f.factory, f.claim, SCOPE,
            namespace_uid="namespace-uid", claim_name=f.name,
        )
        for target in (f.pv_path, f.pvc_path.replace(IDENTITY.name, "platform"),
                       f.pvc_path.rsplit("/", 1)[0], f.pvc_path + "/status"):
            await error(ValueError, reader.request("GET", target))
        reader._select_volume(f.pvc, None)
        await error(ValueError, reader.request("GET", "/api/v1/persistentvolumes/other"))
        for method in ("POST", "PATCH", "DELETE"):
            await error(ValueError, reader.request(method, f.pvc_path))
        await error(ValueError, reader.request("GET", f.pvc_path, body={}))
        base = guards.GuardedKubernetesConnection(
            raw, f.factory, f.claim, SCOPE, namespace_uid="namespace-uid",
        )
        for target in (f.pvc_path, f.pv_path):
            await error(ValueError, base.request("GET", target))
    await f.run(forbidden)
    assert not f.calls and f.checks == 0
    f = Fixture()
    await error(ValueError, f.run(claim_name="other"))
    assert not f.calls
    print("Unselected or unrelated PVs, other stores, lists and writes send no HTTP; older guards stay restricted")

    for missing in ("pvc", "pv"):
        f = Fixture()
        setattr(f, missing, None)
        await error(StoreVolumePending, f.run())
        assert f.closed
    for change in (
        lambda f: f.pvc["status"].update(phase="Pending"),
        lambda f: f.pvc["metadata"].update(namespace="another-store"),
        lambda f: f.pvc["metadata"]["annotations"].update({"meta.helm.sh/release-name": "other"}),
    ):
        f = Fixture()
        change(f)
        await error((StoreVolumePending, StoreVolumeError), f.run())
        assert f.pv_path not in f.calls
    f = Fixture()
    saved = await f.run()
    f.calls.clear()
    f.pvc["metadata"]["uid"] = f.pv["spec"]["claimRef"]["uid"] = "replacement"
    await error(StoreVolumeError, f.run(expected=saved))
    assert f.pv_path not in f.calls
    f = Fixture()
    await error(ValueError, f.run(expected=replace(saved, namespace_uid="other")))
    assert not f.calls
    print("Pending and forged claims cannot authorize PV reads; saved bindings reject replacements")

    for failure in (StoreJobClaimLost("PRIVATE"), StoreJobGuardUnavailable("PRIVATE")):
        f = Fixture()
        f.failure = failure
        caught = await error(guards.StoreKubernetesStopped, f.run())
        assert not caught.write_may_have_applied and not f.calls
    f = Fixture()
    f.remaining = 0
    await error(guards.StoreKubernetesStopped, f.run())
    assert not f.calls
    f = Fixture()
    f.after = lambda: setattr(f, "lost", True)
    caught = await error(guards.StoreKubernetesStopped, f.run())
    assert not caught.write_may_have_applied
    f = Fixture()
    f.after = lambda: f.pvc["metadata"].update(uid="replaced-after-pv")
    await error(StoreVolumeError, f.run())
    f = Fixture()
    f.after = lambda: f.namespace["metadata"].update(uid="replaced-namespace")
    await error(StoreNamespaceError, f.run())
    f = Fixture()
    f.pv["spec"]["claimRef"]["namespace"] = "other-store"
    await error(StoreVolumeError, f.run())
    print("Lease loss, unavailable guards, changed actual namespaces and rebound claims reject success")

    f = Fixture()
    f.claim = replace(f.claim, deadline_at=NOW - timedelta(seconds=1))
    await error(guards.StoreKubernetesStopped, f.run())
    f.namespace["status"]["phase"] = "Terminating"
    f.namespace["metadata"]["deletionTimestamp"] = "fixture-time"
    assert (await f.run(for_cleanup=True)).namespace_uid == "namespace-uid"
    for status in (403, 503):
        f = Fixture()
        f.status = status
        caught = await error(KubernetesAPIError, f.run())
        assert caught.status_code == status and not caught.write_may_have_applied
        assert f.calls.count(f.pv_path) == 1
    print("Overdue cleanup requires explicit mode and a live lease; denied reads remain safe and are never retried")

    f = Fixture()
    f.delay = 1
    with patch.object(module, "_CHECK_TIMEOUT_SECONDS", 0.02):
        caught = await error(KubernetesAPIError, f.run())
    assert not caught.write_may_have_applied and f.closed and not f.active
    f = Fixture()
    f.delay = 1
    task = asyncio.create_task(f.run())
    await f.started.wait()
    task.cancel()
    await error(asyncio.CancelledError, task)
    assert f.closed and not f.active and f.calls.count(f.pv_path) == 1
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)
    print("Timeouts and cancellation close reads without retries or leftover tasks")
    print("Guarded volume reads passed. No database, Kubernetes or filesystem changes made.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 30))
