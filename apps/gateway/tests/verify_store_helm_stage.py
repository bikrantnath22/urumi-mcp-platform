"""Installation stage through real guards; HTTP, storage and Helm are mocked."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import store_helm_stage as stage
from app.store_helm_plan import StoreHelmPlan, StoreHelmPlanError
from app.store_helm_process import HelmProcessError
from app.store_helm_release import StoreReleaseError
from app.store_job_guard import StoreJobClaimLost, StoreJobGuardUnavailable
import verify_store_guarded_helm as guarded
import verify_store_guarded_secrets as fixtures


class Fixture(guarded.Fixture):
    def __init__(self, state=None):
        super().__init__()
        self.state = state
        self.values = StoreHelmPlan(
            fixtures.IDENTITY, fixtures.SCOPE, "namespace-uid", "secret-uid",
            "fixture-tea.localtest.me", "http", 600,
            fixtures.CLAIM.name,
        ).values()
        self.after_install = "deployed"
        self.install_error = self.read_error = self.hold_install = False
        self.bad_listing = self.bad_after_install = False
        self.unbound = None
        self.installs = 0
        self.upgrades = 0
        self.revision = 1
        self.install_started = asyncio.Event()

    @contextmanager
    def workspace(self, identity):
        self.workspace_closed = False
        with super().workspace(identity) as config:
            yield config

    async def stage_require(self, factory, claim, **kwargs):
        if not kwargs:
            kwargs = {
                "expected_namespace_uid": "namespace-uid",
                "expected_credentials_secret_uid": "secret-uid",
            }
        checked = await self.require(factory, claim, **kwargs)
        return replace(checked, **{self.unbound: None}) if self.unbound else checked

    async def process(self, executable, arguments, **kwargs):
        operation = arguments[0]
        self.process_stopped = False
        if operation in ("install", "upgrade"):
            if operation == "upgrade":
                self.upgrades += 1
                self.revision += 1
                assert "--reuse-values" in arguments and kwargs["payload"] == b""
            else:
                self.installs += 1
                self.values = json.loads(kwargs["payload"])
            self.state = "pending-install"
            self.hold = self.hold_cleanup = self.hold_install
            self.cleanup_started.clear()
            self.install_started.set()
        await super().process(executable, arguments, **kwargs)
        if operation in ("install", "upgrade"):
            self.state = self.after_install
            if self.install_error:
                raise HelmProcessError(
                    "PRIVATE PROCESS ERROR", write_may_have_applied=True,
                )
            return b"stdout is deliberately not used as readiness proof"
        if operation == "list":
            if self.bad_listing or (self.installs and self.bad_after_install):
                return b"PRIVATE INVALID JSON"
            return json.dumps([] if self.state is None else [{
                "name": "store", "namespace": fixtures.IDENTITY.name,
                "revision": str(self.revision), "chart": "store-0.1.0",
                "app_version": "1.0", "status": self.state,
            }]).encode()
        if self.read_error:
            raise HelmProcessError("PRIVATE READ ERROR")
        return json.dumps({
            "name": "store", "namespace": fixtures.IDENTITY.name, "version": self.revision,
            "info": {"status": self.state, "description": "PRIVATE DIAGNOSTIC"},
            "config": self.values,
        }).encode()

    async def execute(self):
        async def work(caller):
            with patch.object(stage, "require_claim", self.stage_require):
                return await stage.prepare_installation(
                    self.factory, fixtures.CLAIM,
                    caller._connection, fixtures.SCOPE,
                )
        return await self.run(callback=work)

    def commands(self):
        return [args[0] for args, options in self.invocations]


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
    result = await f.execute()
    assert result.state == "deployed" and f.installs == 1
    assert f.commands() == ["list", "install", "list", "status"]
    assert result.bootstrap_seconds == f.values["bootstrap"]["activeDeadlineSeconds"]
    before = deepcopy(f.values)
    assert await f.execute() == result
    assert f.commands()[-2:] == ["list", "status"] and f.installs == 1
    assert f.values == before
    assert all(method == "GET" for method, path, body in f.requests)
    assert f.process_stopped and f.workspace_closed and f.closed
    print("Missing releases install once; persisted state is checked and deployed retries do not reinstall")

    for state in ("pending-install", "failed"):
        f = Fixture(state)
        caught = await error(stage.StoreInstallationNeedsRecovery, f.execute())
        assert caught.state == state and caught.write_may_have_applied
        assert f.commands() == ["list", "status"] and f.installs == 0
    f = Fixture("deployed")
    assert (await f.execute()).bootstrap_seconds == 600
    assert f.installs == 0
    print("Interrupted/failed releases request recovery; existing bootstrap deadlines remain unchanged")

    for field in ("namespace_uid", "credentials_secret_uid"):
        f = Fixture()
        f.unbound = field
        await error(StoreHelmPlanError, f.execute())
        assert not f.invocations and not f.requests
    for failure in (
        StoreJobClaimLost("The claim was lost."),
        StoreJobGuardUnavailable("Ownership storage is unavailable."),
    ):
        f = Fixture()
        f.before = failure
        await error(type(failure), f.execute())
        assert not f.invocations and not f.requests
    f = Fixture()
    f.bad_listing = True
    await error(StoreReleaseError, f.execute())
    assert f.commands() == ["list"]
    f = Fixture("deployed")
    f.values["storeHost"] = "another-store.localtest.me"
    await error(StoreReleaseError, f.execute())
    assert f.installs == 0
    print("Missing bindings, lost ownership, invalid JSON and cross-store values prevent installation")

    f = Fixture()
    f.install_error = True
    caught = await error(stage.StoreInstallationUncertain, f.execute())
    assert caught.write_may_have_applied and f.installs == 1
    assert f.state == "deployed" and f.commands() == ["list", "install"]
    f.install_error = False
    assert (await f.execute()).state == "deployed" and f.installs == 1
    for mode in ("bad_json", "read_error", "ownership"):
        f = Fixture()
        if mode == "bad_json":
            f.bad_after_install = True
        elif mode == "read_error":
            f.read_error = True
        else:
            def changed(f=f):
                if f.installs:
                    f.stored_uid = "replacement-binding"
            f.on_return = changed
        caught = await error(stage.StoreInstallationUncertain, f.execute())
        assert caught.write_may_have_applied and f.installs == 1
        assert f.process_stopped and f.workspace_closed and f.closed
    print("Accepted writes survive lost replies or failed later checks; no command is retried")

    for state in (None, "pending-install", "failed"):
        f = Fixture()
        f.after_install = state
        caught = await error(stage.StoreInstallationNeedsRecovery, f.execute())
        assert caught.state == ("absent" if state is None else state)
        assert f.installs == 1
    print("Command success alone cannot finish installation or mark a store Ready")

    f = Fixture()
    f.hold_install = True
    task = asyncio.create_task(f.execute())
    try:
        await f.install_started.wait()
        task.cancel()
        await f.cleanup_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert f.active and not f.workspace_closed and not task.done()
        f.finish_cleanup.set()
        await error(asyncio.CancelledError, task)
        assert f.installs == 1 and f.process_stopped and f.workspace_closed
    finally:
        f.finish_cleanup.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    f.hold = f.hold_cleanup = f.hold_install = False
    caught = await error(stage.StoreInstallationNeedsRecovery, f.execute())
    assert caught.state == "pending-install" and f.installs == 1
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)
    print("Repeated cancellation finishes command cleanup; recovery observes the interrupted release")
    print("Installation stage verification passed. No live services or real processes were used.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 20))
