"""Interrupted managed installs recover through real guards and mocked HTTP/Helm."""

import asyncio
import base64
from copy import deepcopy
from dataclasses import replace
import json
import gzip
from pathlib import Path
import sys
import time
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from app import store_helm_recovery as recovery
from app import store_helm_stage as stage
from app.store_helm_plan import plan_store_installation
from app.store_helm_release import StoreRelease, StoreReleaseError
from app.store_job_guard import CheckedStoreJob, StoreJobClaimLost
from app.store_namespace import namespace_for
from app.store_kube_connection import _check_request
import verify_store_helm_stage as installation
import verify_store_guarded_secrets as fixtures


class Fixture(installation.Fixture):
    def __init__(self, state="pending-install"):
        super().__init__(state)
        self.path = fixtures.COLLECTION + "/sh.helm.release.v1.store.v1"
        self.secret_record = self.record()
        self.fault = None
        self.patches = 0
        self.patch_started = asyncio.Event()
        self.pause_patch = False
        self.patch_closed = False

    def record(self):
        release = {
            "name": "store", "namespace": fixtures.IDENTITY.name, "version": self.revision,
            "info": {"status": self.state}, "config": deepcopy(self.values),
            "chart": {"metadata": {"name": "store", "version": "0.1.0", "appVersion": "1.0"}},
            "manifest": "PRIVATE MANIFEST", "hooks": [],
        }
        return {
            "apiVersion": "v1", "kind": "Secret", "type": "helm.sh/release.v1",
            "metadata": {
                "name": "sh.helm.release.v1.store.v1", "namespace": fixtures.IDENTITY.name,
                "uid": "helm-secret-uid", "resourceVersion": "5",
                "labels": {**fixtures.IDENTITY.labels, "owner": "helm", "name": "store",
                           "status": self.state, "version": str(self.revision)},
            }, "data": {"release": recovery._pack(release)},
        }

    async def peer(self, request):
        if request.url.path != self.path:
            return await super().peer(request)
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, deepcopy(body)))
        if request.method == "GET":
            reply = deepcopy(self.secret_record)
            if self.fault == "namespace":
                self.namespace["metadata"]["uid"] = "replacement"
            elif self.fault == "claim":
                self.before = StoreJobClaimLost("PRIVATE CLAIM")
            elif self.fault == "completed":
                self.state = "deployed"
                self.secret_record = self.record()
                self.secret_record["metadata"]["resourceVersion"] = "6"
            return httpx.Response(200, json=reply)
        assert request.method == "PATCH"
        assert request.headers["content-type"] == "application/json-patch+json"
        fields = {
            "/metadata/uid": self.secret_record["metadata"]["uid"],
            "/metadata/resourceVersion": self.secret_record["metadata"]["resourceVersion"],
            "/data/release": self.secret_record["data"]["release"],
            "/metadata/labels/status": self.secret_record["metadata"]["labels"]["status"],
        }
        if any(item["value"] != fields[item["path"]] for item in body[:4]):
            return httpx.Response(409, json={"message": "PRIVATE CONFLICT"})
        self.patches += 1
        self.state = "failed"
        self.secret_record["data"]["release"] = body[4]["value"]
        self.secret_record["metadata"]["labels"]["status"] = "failed"
        self.secret_record["metadata"]["resourceVersion"] = "6"
        self.patch_started.set()
        try:
            if self.pause_patch:
                await asyncio.Event().wait()
            if self.fault == "lost_reply":
                raise httpx.ReadTimeout("PRIVATE ACKNOWLEDGEMENT")
            if self.fault == "lost_after_patch":
                self.before = StoreJobClaimLost("PRIVATE LOST CLAIM")
            return httpx.Response(200, json=deepcopy(self.secret_record))
        finally:
            self.patch_closed = True

    async def recover(self):
        with patch.object(recovery, "require_claim", self.stage_require):
            return await self.execute()


async def rejected(kind, awaitable):
    try:
        await awaitable
    except kind as error:
        assert "PRIVATE" not in str(error)
        return error
    raise AssertionError("Expected " + kind.__name__)


async def parallel_checks():
    """Two actual stage coroutines overlap and retain independent recovery state."""
    factory = lambda: None
    for interrupted in (False, True):
        records = {}
        for number, slug in enumerate(("parallel-a", "parallel-b"), start=7):
            store_id = uuid4()
            claim = replace(
                fixtures.CLAIM, store_id=store_id, operation_id=uuid4(),
                namespace="urumi-store-" + store_id.hex, owner_id=number,
                name=slug, slug=slug, url=f"http://{slug}.localtest.me",
                attempt=1, lease_token=uuid4(),
            )
            records[store_id] = {
                "claim": claim, "state": None, "values": None, "revision": 1,
                "namespace_uid": "ns-" + slug, "secret_uid": "secret-" + slug,
                "job_uid": None, "installs": 0, "upgrades": 0,
            }
        all_started, continue_install = asyncio.Event(), asyncio.Event()
        started = 0

        async def require(actual_factory, claim, **expected):
            assert actual_factory is factory
            row = records[claim.store_id]
            if claim != row["claim"]:
                raise StoreJobClaimLost("The old claim cannot continue.")
            assert all(value == {
                "expected_namespace_uid": row["namespace_uid"],
                "expected_credentials_secret_uid": row["secret_uid"],
            }[key] for key, value in expected.items())
            return CheckedStoreJob(
                claim, row["namespace_uid"], False, time.monotonic() + 30,
                row["secret_uid"],
            )

        class Helm:
            def __init__(self, connection, actual_factory, claim, scope, **options):
                assert actual_factory is factory
                self.claim = claim
                self.row = records[claim.store_id]
                assert options == {
                    "namespace_uid": self.row["namespace_uid"],
                    "credentials_secret_uid": self.row["secret_uid"],
                }

            async def list_releases(self):
                row, identity = self.row, namespace_for(self.claim)
                return json.dumps([] if row["state"] is None else [{
                    "name": "store", "namespace": identity.name,
                    "revision": str(row["revision"]), "status": row["state"],
                    "chart": "store-0.1.0", "app_version": "1.0",
                }]).encode()

            async def read_release(self):
                row = self.row
                return json.dumps({
                    "name": "store", "namespace": self.claim.namespace,
                    "version": row["revision"], "info": {"status": row["state"]},
                    "config": row["values"],
                }).encode()

            async def install(self):
                nonlocal started
                row = self.row
                row["installs"] += 1
                row["values"] = plan_store_installation(
                    await require(factory, self.claim), fixtures.SCOPE,
                ).values()
                row["state"] = "pending-install"
                # Simulate an interruption between chart-object creations.
                if started == 0 or not interrupted:
                    row["job_uid"] = "bootstrap-" + self.claim.store_id.hex
                started += 1
                if started == 2:
                    all_started.set()
                try:
                    await continue_install.wait()
                    row["state"] = "deployed"
                except asyncio.CancelledError:
                    row["state"] = "failed"
                    raise

            async def recover(self, observed):
                await require(factory, self.claim)
                assert observed.state == "failed" and self.claim.attempt == 2
                self.row["job_uid"] = self.row["job_uid"] or "bootstrap-" + self.claim.store_id.hex
                self.row["upgrades"] += 1
                self.row["revision"] += 1
                self.row["state"] = "deployed"

        with patch.object(stage, "require_claim", require), patch.object(stage, "GuardedStoreHelm", Helm):
            async def run(row):
                return await stage.prepare_installation(factory, row["claim"], object(), fixtures.SCOPE)
            tasks = [asyncio.create_task(run(row)) for row in records.values()]
            await asyncio.wait_for(all_started.wait(), 2)
            assert all(not task.done() for task in tasks)
            snapshots = {key: (deepcopy(row["values"]), row["claim"].deadline_at, row["job_uid"])
                         for key, row in records.items()}
            if interrupted:
                for task in tasks:
                    task.cancel()
                assert all(isinstance(result, asyncio.CancelledError)
                           for result in await asyncio.gather(*tasks, return_exceptions=True))
                for row in records.values():
                    row["claim"] = replace(row["claim"], attempt=2, lease_token=uuid4())
                results = await asyncio.gather(*(run(row) for row in records.values()))
            else:
                continue_install.set()
                results = await asyncio.gather(*tasks)
            assert all(result.state == "deployed" and result.managed_bootstrap for result in results)
            for key, row in records.items():
                values, deadline, job_uid = snapshots[key]
                assert row["values"] == values and row["claim"].deadline_at == deadline
                assert row["installs"] == 1 and row["upgrades"] == int(interrupted)
                assert row["job_uid"] and (job_uid is None or row["job_uid"] == job_uid)
            assert len({result.identity.name for result in results}) == 2
            assert len({row["secret_uid"] for row in records.values()}) == 2
    print("Two stages overlap successfully; interrupted attempts resume separately, reuse existing Jobs and retain both original deadlines")


async def check():
    baseline = asyncio.all_tasks()
    replacement = replace(fixtures.CLAIM, attempt=2, lease_token=uuid4())
    with patch.object(fixtures, "CLAIM", replacement):
        for state in ("failed", "pending-install", "pending-upgrade"):
            f = Fixture(state)
            values = deepcopy(f.values)
            deadline = fixtures.CLAIM.deadline_at
            result = await f.recover()
            assert result.state == "deployed" and result.revision == 2
            assert result.managed_bootstrap and result.bootstrap_seconds == 600
            assert f.installs == 0 and f.upgrades == 1
            assert f.patches == (state != "failed")
            assert f.values == values and fixtures.CLAIM.deadline_at == deadline
            if f.patches:
                stored = recovery._unpack(f.secret_record["data"]["release"])
                assert stored["info"]["status"] == "failed"
                assert stored["config"] == values and stored["manifest"] == "PRIVATE MANIFEST"
            assert await f.recover() == result and f.upgrades == 1
        print("Failed/pending installs reconcile once; deployed replays reuse saved values and the original bootstrap allowance")

        for fault in ("namespace", "claim", "completed"):
            f = Fixture()
            f.fault = fault
            await rejected(stage.StoreInstallationUncertain, f.recover())
            assert f.patches == 0 and f.upgrades == 0
        print("Changed namespaces, lost claims and concurrent completion prevent stale recovery writes")

        for fault in ("lost_reply", "lost_after_patch"):
            f = Fixture()
            f.fault = fault
            error = await rejected(stage.StoreInstallationUncertain, f.recover())
            assert error.write_may_have_applied and f.patches == 1 and f.upgrades == 0
            f.before = f.fault = None
            assert (await f.recover()).state == "deployed"
            assert f.patches == 1 and f.upgrades == 1
        print("Accepted interruption writes survive lost replies/leases; the next pass observes progress without repeating the patch")

        f = Fixture("failed")
        f.install_error = True
        await rejected(stage.StoreInstallationUncertain, f.recover())
        assert f.state == "deployed" and f.upgrades == 1
        f.install_error = False
        assert (await f.recover()).state == "deployed" and f.upgrades == 1
        print("A committed Helm upgrade with a lost acknowledgement is observed without another upgrade")

        f = Fixture()
        f.pause_patch = True
        with patch.object(recovery, "_RECOVERY_TIMEOUT_SECONDS", 0.03):
            await rejected(stage.StoreInstallationUncertain, f.recover())
        assert f.patch_closed and f.closed and f.patches == 1 and f.upgrades == 0
        f.pause_patch = False
        assert (await f.recover()).state == "deployed" and f.patches == 1
        print("The overall recovery timeout cancels HTTP and preserves accepted progress for a new observation")

        f = Fixture()
        f.pause_patch = True
        task = asyncio.create_task(f.recover())
        await f.patch_started.wait()
        task.cancel()
        await rejected(asyncio.CancelledError, task)
        assert f.patch_closed and f.closed and f.patches == 1 and f.upgrades == 0
        f.pause_patch = False
        assert (await f.recover()).state == "deployed" and f.patches == 1
        print("Cancellation closes HTTP before recovery; committed progress survives and is not re-applied")

        f = Fixture()
        plan = plan_store_installation(await f.stage_require(f.factory, replacement), fixtures.SCOPE)
        observed = StoreRelease(fixtures.IDENTITY, "pending-install", 600, 1, True)
        original = deepcopy(f.secret_record)
        body = recovery.interruption_patch(original, plan, observed)
        assert original == f.secret_record
        _check_request("PATCH", f.path, body)
        oversized = base64.b64encode(base64.b64encode(gzip.compress(b" " * (1024 * 1024 + 1)))).decode()
        for payload in ("!invalid!", "", oversized):
            document = deepcopy(original)
            document["data"]["release"] = payload
            try:
                recovery.interruption_patch(document, plan, observed)
            except StoreReleaseError:
                pass
            else:
                raise AssertionError("Malformed or decompression-bomb payload accepted")
        document = deepcopy(original)
        decoded = recovery._unpack(document["data"]["release"])
        decoded["config"]["storeHost"] = "another-store.localtest.me"
        document["data"]["release"] = recovery._pack(decoded)
        try:
            recovery.interruption_patch(document, plan, observed)
        except StoreReleaseError:
            pass
        else:
            raise AssertionError("Cross-store saved values accepted")
        for key, value in (("namespace", "platform"), ("uid", None), ("deletionTimestamp", "now")):
            document = deepcopy(original)
            document["metadata"][key] = value
            try:
                recovery.interruption_patch(document, plan, observed)
            except StoreReleaseError:
                pass
            else:
                raise AssertionError("Unsafe release metadata accepted")
        for key in fixtures.IDENTITY.labels:
            document = deepcopy(original)
            document["metadata"]["labels"][key] = "other"
            try:
                recovery.interruption_patch(document, plan, observed)
            except StoreReleaseError:
                pass
            else:
                raise AssertionError("Cross-store release accepted")
        for path in (fixtures.ITEM, fixtures.COLLECTION, f.path.replace(".store.", ".other."), f.path[:-1] + "9"):
            try:
                _check_request("PATCH", path, body)
            except ValueError:
                pass
            else:
                raise AssertionError("Broader secret mutation accepted")
        print("Recovery requires exact store ownership and bounded Helm revisions; credentials and broader routes remain read-only")
    await asyncio.sleep(0)
    await parallel_checks()
    assert not (asyncio.all_tasks() - baseline)
    print("Managed installation recovery checks passed. All HTTP, database and Helm calls were fixtures.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 30))
