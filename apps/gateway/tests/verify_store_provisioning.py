"""Provisioning composition checks; all service, process and storage stages are mocked."""

import asyncio
from contextlib import ExitStack, contextmanager
from dataclasses import replace
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
# Process-local, unused: no real storage or MCP connection is opened.
os.environ["PGPASSWORD"] = "isolated-provisioning-unused"

from app import store_provisioning as module
from app.store_helm_release import StoreRelease
from app.store_helm_stage import StoreInstallationUncertain
from app.store_job_guard import CheckedStoreJob, StoreJobClaimLost
from app.store_volumes import STORE_CLAIM_NAMES, StoreVolumeBinding, StoreVolumePending
from app.store_registration import StoreMCPRegistrations, definitions_for
import verify_store_guarded_secrets as fixtures


class Fixture:
    def __init__(self):
        self.claim = fixtures.CLAIM
        self.connection = object()
        self.namespace_uid = self.secret_uid = None
        self.calls = []
        self.lost = False
        self.failure = self.after_mcp = None
        self.hold = None
        self.started = asyncio.Event()
        self.workload_observed = asyncio.Event()
        self.active = False
        self.workloads = dict.fromkeys(module.resource_read_paths(fixtures.IDENTITY), True)
        self.wordpress = dict.fromkeys((
            "storefront", "woo_rw_key_read", "woo_ro_key_read", "media_login",
        ), True)
        self.counts = {"woo-mcp": 12, "mcp-analytics": 3}
        self.release = StoreRelease(fixtures.IDENTITY, "deployed", 600)
        woo, analytics = definitions_for(self.claim)
        self.registrations = StoreMCPRegistrations(
            101, woo.slug, woo.url, 102, analytics.slug, analytics.url,
        )
        self.return_registration = True
        self.gate_ok = True
        self.volume_records = tuple(
            StoreVolumeBinding(
                fixtures.IDENTITY, "namespace-uid", name,
                "claim-" + name, "pvc-" + name, "pv-" + name,
            )
            for name in STORE_CLAIM_NAMES
        )
        self.volumes = self.volume_records
        self.readback = {item.claim_name: item for item in self.volume_records}

    def factory(self):
        raise AssertionError("No database connection is allowed.")

    async def require(self, factory, claim, **expected):
        assert factory == self.factory and claim == self.claim
        assert claim.deadline_at == fixtures.CLAIM.deadline_at
        if self.lost or any(
            value != {
                "expected_namespace_uid": self.namespace_uid,
                "expected_credentials_secret_uid": self.secret_uid,
            }[key] for key, value in expected.items()
        ):
            raise StoreJobClaimLost("The fixture claim was lost.")
        return CheckedStoreJob(
            claim=claim, namespace_uid=self.namespace_uid,
            credentials_secret_uid=self.secret_uid, for_cleanup=False,
            stop_at=time.monotonic() + 30,
        )

    async def enter(self, name, factory, claim, connection=None, scope=None, **options):
        assert factory == self.factory and claim == self.claim
        if connection is not None:
            assert connection is self.connection and scope == fixtures.SCOPE
        self.calls.append(name)
        self.active = True
        try:
            if self.hold == name:
                self.started.set()
                await asyncio.Event().wait()
            if self.failure is not None and self.failure[0] == name:
                raise self.failure[1]
        finally:
            self.active = False

    async def namespace(self, *args, **options):
        await self.enter("namespace", *args, **options)
        self.namespace_uid = self.namespace_uid or "namespace-uid"
        return self.namespace_uid

    async def secret(self, *args, **options):
        assert self.namespace_uid == "namespace-uid"
        assert options == {"admin_email": "owner@example.test"}
        await self.enter("secret", *args, **options)
        self.secret_uid = self.secret_uid or "secret-uid"
        return self.secret_uid

    async def installation(self, *args, **options):
        assert self.namespace_uid and self.secret_uid
        await self.enter("installation", *args, **options)
        return self.release

    def options(self, options, *, bootstrap=False):
        expected = {
            "namespace_uid": "namespace-uid", "credentials_secret_uid": "secret-uid",
        }
        if bootstrap:
            expected["bootstrap_seconds"] = 600
            expected["managed_bootstrap"] = self.release.managed_bootstrap
        assert options == expected

    async def workloads_check(self, *args, **options):
        self.options(options, bootstrap=True)
        await self.enter("workloads", *args, **options)
        self.workload_observed.set()
        return dict(self.workloads)

    async def volumes_prepare(self, *args, **options):
        assert not options
        await self.enter("volumes", *args)
        return self.volumes

    async def volume_read(self, *args, **options):
        name = options["claim_name"]
        original = next(item for item in self.volume_records if item.claim_name == name)
        assert options == {
            "namespace_uid": "namespace-uid", "claim_name": name, "expected": original,
        }
        await self.enter("volume-read", *args)
        return self.readback[name]

    async def wordpress_check(self, *args, **options):
        self.options(options)
        await self.enter("wordpress", *args, **options)
        return dict(self.wordpress)

    async def register(self, *args):
        await self.enter("register", *args)
        return self.registrations if self.return_registration else None

    async def mcp_check(self, factory, claim, connection, scope, registrations, **options):
        assert registrations == self.registrations
        self.options(options)
        await self.enter("mcp", factory, claim, connection, scope, **options)
        if self.after_mcp is not None:
            self.after_mcp()
        return dict(self.counts)

    async def gate_check(self, *args, **options):
        self.options(options)
        await self.enter("gate", *args, **options)
        return self.gate_ok

    @contextmanager
    def patched(self):
        with ExitStack() as stack:
            for name, value in (
                ("require_claim", self.require),
                ("verify_store_access_gate", self.gate_check),
                ("prepare_namespace", self.namespace), ("prepare_secret", self.secret),
                ("prepare_installation", self.installation),
                ("check_workloads", self.workloads_check),
                ("prepare_store_volumes", self.volumes_prepare),
                ("read_store_volume", self.volume_read),
                ("verify_store_wordpress", self.wordpress_check),
                ("register_store_mcps", self.register),
                ("verify_store_mcps", self.mcp_check),
            ):
                stack.enter_context(patch.object(module, name, value))
            yield

    async def run(self, claim=None):
        with self.patched():
            return await module.provision_store(
                self.factory, self.claim if claim is None else claim,
                self.connection, fixtures.SCOPE, admin_email="owner@example.test",
            )


async def error(kind, operation):
    try:
        await operation
    except kind as caught:
        return caught
    raise AssertionError("Expected " + kind.__name__)


async def check():
    baseline = asyncio.all_tasks()
    f = Fixture()
    result = await f.run()
    assert isinstance(result, module.StoreProvisioningResult)
    assert result.claim == f.claim and result.registrations == f.registrations
    assert result.namespace_uid == "namespace-uid"
    assert result.credentials_secret_uid == "secret-uid"
    assert result.volumes == f.volume_records and result.access_guarded is True
    assert f.calls == ["namespace", "secret", "installation", "workloads",
                       "volumes", "wordpress", "register", "mcp", "workloads",
                       "volume-read", "volume-read", "gate"]
    assert result.claim.deadline_at == fixtures.CLAIM.deadline_at
    assert await f.run() == result
    print("Full preparation checks workloads twice, verifies both services and returns only this job's private result")

    f = Fixture()
    f.workloads["mysql"] = False
    await error(module.StoreProvisioningPending, f.run())
    assert f.calls == ["namespace", "secret", "installation", "workloads"]
    f = Fixture()
    f.wordpress["media_login"] = False
    await error(module.StoreProvisioningVerificationError, f.run())
    assert "register" not in f.calls
    f = Fixture()
    f.after_mcp = lambda: f.workloads.update(wordpress=False)
    await error(module.StoreProvisioningPending, f.run())
    assert f.calls[-1] == "workloads"
    print("Pending workloads, incomplete credentials and readiness changes after discovery cannot return successful preparation")

    for change in (
        {"identity": replace(fixtures.IDENTITY, owner_id=8)},
        {"state": "pending-install"}, {"bootstrap_seconds": True},
    ):
        f = Fixture()
        f.release = replace(f.release, **change)
        await error(module.StoreProvisioningVerificationError, f.run())
        assert f.calls[-1] == "installation"
    for target, value in (
        ("workloads", {}), ("workloads", {"mysql": True}),
        ("wordpress", {}), ("counts", {"woo-mcp": True, "mcp-analytics": 3}),
        ("counts", {"woo-mcp": 12, "mcp-analytics": 2}),
    ):
        f = Fixture()
        setattr(f, target, value)
        await error(module.StoreProvisioningVerificationError, f.run())
    f = Fixture()
    f.return_registration = False
    await error(StoreJobClaimLost, f.run())
    assert "mcp" not in f.calls
    print("Cross-store releases, invalid deadlines/results and lost registration claims cannot proceed")

    f = Fixture()
    f.lost = True
    await error(StoreJobClaimLost, f.run())
    assert not f.calls
    f = Fixture()
    f.after_mcp = lambda: setattr(f, "secret_uid", "replacement")
    await error(StoreJobClaimLost, f.run())
    for name in ("namespace", "secret", "installation", "volumes", "wordpress", "mcp", "volume-read"):
        f = Fixture()
        failure = StoreInstallationUncertain() if name == "installation" else StoreJobClaimLost("stopped")
        f.failure = (name, failure)
        caught = await error(type(failure), f.run())
        assert caught is failure and f.calls.count(name) == 1 and f.calls[-1] == name
    f = Fixture()
    await error(ValueError, f.run(replace(f.claim, action="delete")))
    assert not f.calls
    print("Lost ownership and uncertain writes stop immediately without retries, cleanup or terminal completion")

    for name in ("namespace", "installation", "volumes", "wordpress", "mcp", "volume-read"):
        f = Fixture()
        f.hold = name
        task = asyncio.create_task(f.run())
        await f.started.wait()
        task.cancel()
        await error(asyncio.CancelledError, task)
        assert f.calls[-1] == name and not f.active
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)
    print("Cancellation stops the active stage before another stage starts; no background tasks remain")
    for invalid in (
        None, [], (), (None, None),
        (Fixture().volume_records[0],) * 2,
        tuple(reversed(Fixture().volume_records)),
    ):
        f = Fixture()
        f.volumes = invalid
        await error(module.StoreProvisioningVerificationError, f.run())
        assert "wordpress" not in f.calls and "register" not in f.calls

    for field, value in (
        ("namespace_uid", "another-namespace"),
        ("identity", replace(fixtures.IDENTITY, owner_id=8)),
    ):
        f = Fixture()
        first, second = f.volume_records
        f.volumes = (replace(first, **{field: value}), second)
        await error(module.StoreProvisioningVerificationError, f.run())
        assert "wordpress" not in f.calls

    f = Fixture()
    pending = StoreVolumePending("Storage is still pending.")
    f.failure = ("volumes", pending)
    assert await error(StoreVolumePending, f.run()) is pending
    assert f.calls[-1] == "volumes" and "register" not in f.calls

    f = Fixture()
    first = f.volume_records[0]
    f.after_mcp = lambda: f.readback.update({
        first.claim_name: replace(first, volume_uid="replacement-volume"),
    })
    await error(module.StoreProvisioningVerificationError, f.run())
    assert f.calls[-1] == "volume-read"
    assert not (asyncio.all_tasks() - baseline)
    print("Both volumes are required before service checks; changed storage after discovery rejects preparation")
    f = Fixture()
    f.gate_ok = False
    await error(module.StoreProvisioningVerificationError, f.run())
    assert f.calls[-1] == "gate"
    f = Fixture()
    f.hold = "gate"
    task = asyncio.create_task(f.run())
    await f.started.wait()
    task.cancel()
    await error(asyncio.CancelledError, task)
    assert not f.active and not (asyncio.all_tasks() - baseline)
    print("Maintenance gate verification is required; cancellation cannot finish provisioning.")
    f = Fixture()
    f.release = replace(f.release, revision=2, managed_bootstrap=True)
    result = await f.run()
    assert result.claim == f.claim and f.calls.count("workloads") == 2
    f.workloads["store-bootstrap"] = False
    f.workload_observed.clear()
    with patch.object(module, "_WORKLOAD_POLL_SECONDS", 0.01):
        task = asyncio.create_task(f.run())
        await f.workload_observed.wait()
        await asyncio.sleep(0.02)
        assert not task.done() and f.calls[-1] == "workloads"
        assert f.calls.count("namespace") == 2  # one call in each complete pass
        f.workloads["store-bootstrap"] = True
        assert (await task).claim == f.claim
    print("Recovered managed releases still require completed bootstrap, both volumes and all service/ownership checks")
    f = Fixture()
    f.release = replace(f.release, managed_bootstrap=True)
    f.workloads["store-bootstrap"] = False
    task = asyncio.create_task(f.run())
    await f.workload_observed.wait()
    task.cancel()
    await error(asyncio.CancelledError, task)
    assert "wordpress" not in f.calls and "register" not in f.calls
    assert not (asyncio.all_tasks() - baseline)
    print("Pending managed Jobs retain their active worker slot; cancellation stops the wait before registration or completion")
    print("Provisioning composition verification passed. No live services or database calls were made.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 30))
