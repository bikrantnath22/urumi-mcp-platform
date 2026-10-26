"""Guarded Helm integration with mocked HTTP, storage, files and subprocesses."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import store_guarded_helm as module

import asyncio
from contextlib import ExitStack, contextmanager
from dataclasses import replace
import json
import time
from unittest.mock import patch

import httpx
import verify_store_secret_api as api_fixtures
import verify_store_guarded_secrets as secret_fixtures
from app import store_guarded_secrets as secrets_guard
from app.store_helm_config import StoreHelmConfig, HELM_EXECUTABLE
from app.store_helm_process import HelmProcessError
from app.store_job_guard import StoreJobClaimLost, StoreJobGuardUnavailable
from app.store_kube_connection import KubernetesConnection


class Fixture(api_fixtures.Fixture):
    def __init__(self):
        super().__init__("secret-uid")
        self.invocations = []
        self.active = False
        self.workspace_closed = False
        self.process_stopped = False
        self.started = asyncio.Event()
        self.finish_process = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.finish_cleanup = asyncio.Event()
        self.hold = self.hold_cleanup = False
        self.process_error = None
        self.on_return = None
        self.cutoff = None
        self.saved_namespace = "namespace-uid"

    async def require(self, factory, claim, **kwargs):
        if self.saved_namespace != kwargs["expected_namespace_uid"]:
            raise StoreJobClaimLost("PRIVATE NAMESPACE CHANGE")
        checked = await super().require(factory, claim, **kwargs)
        return replace(checked, stop_at=self.cutoff) if self.cutoff is not None else checked

    @contextmanager
    def workspace(self, identity):
        assert identity == secret_fixtures.IDENTITY
        self.active = True
        try:
            yield StoreHelmConfig("/tmp/urumi-helm-fixture01", identity)
        finally:
            assert not self.invocations or self.process_stopped
            self.active = False
            self.workspace_closed = True

    async def process(self, executable, arguments, **kwargs):
        assert self.active and executable == HELM_EXECUTABLE
        self.invocations.append((arguments, kwargs))
        self.started.set()
        try:
            if self.hold:
                await self.finish_process.wait()
            if self.on_return is not None:
                self.on_return()
            if self.process_error is not None:
                raise self.process_error
            return b'{"fixture":"result"}'
        finally:
            self.cleanup_started.set()
            if self.hold_cleanup:
                await self.finish_cleanup.wait()
            self.process_stopped = True

    def make(self, raw):
        return module.GuardedStoreHelm(
            raw, self.factory, secret_fixtures.CLAIM, secret_fixtures.SCOPE,
            namespace_uid="namespace-uid", credentials_secret_uid="secret-uid",
        )

    async def run(self, operation="install", callback=None):
        raw = KubernetesConnection(
            lambda: "fixture.worker.signature", secret_fixtures.TLS,
            transport=httpx.MockTransport(self.peer),
        )
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(module, "require_claim", self.require))
                stack.enter_context(patch.object(secrets_guard, "require_claim", self.require))
                stack.enter_context(patch.object(module, "helm_workspace", self.workspace))
                stack.enter_context(patch.object(module, "run_helm_process", self.process))
                stack.enter_context(patch.object(module, "_CHECK_SECONDS", 0.01))
                stack.enter_context(patch.object(module, "_CHECK_TIMEOUT_SECONDS", 0.2))
                caller = self.make(raw)
                if callback is not None:
                    return await callback(caller)
                return await {
                    "install": caller.install,
                    "list": caller.list_releases,
                    "status": caller.read_release,
                }[operation]()
        finally:
            await raw.aclose()
            self.closed = raw._client.is_closed


async def error(kind, operation):
    try:
        await operation
    except kind as caught:
        assert "PRIVATE" not in str(caught)
        return caught
    raise AssertionError("Expected " + kind.__name__)


async def check():
    baseline = asyncio.all_tasks()
    for operation in ("install", "list", "status"):
        f = Fixture()
        assert await f.run(operation) == b'{"fixture":"result"}'
        assert f.workspace_closed and f.process_stopped and f.closed
        assert len(f.invocations) == 1
        args, options = f.invocations[0]
        assert args[0] == operation and options["writing"] == (operation == "install")
        assert args[args.index("--namespace") + 1] == secret_fixtures.IDENTITY.name
        assert options["env"]["HELM_NAMESPACE"] == secret_fixtures.IDENTITY.name
        assert f.count("GET", secret_fixtures.ITEM) == 2
        assert all(method == "GET" for method, path, body in f.requests)
        if operation == "install":
            assert json.loads(options["payload"])["secretName"] == "store-secrets"
            assert 1000 < options["timeout_seconds"] <= 1200
        else:
            assert options["payload"] == b""
            assert 0 < options["timeout_seconds"] <= 15
    print("Fixed commands check actual resources before and after execution; reads never write Kubernetes data")

    for failure in (
        StoreJobClaimLost("PRIVATE CLAIM"),
        StoreJobGuardUnavailable("PRIVATE STORAGE"),
    ):
        f = Fixture()
        f.before = failure
        caught = await error(module.StoreHelmStopped, f.run())
        assert not caught.write_may_have_applied and not f.invocations
        assert not f.requests
    for mode in ("namespace", "secret", "missing", "pending", "credentials"):
        f = Fixture()
        if mode == "namespace":
            f.namespace["metadata"]["uid"] = "replacement"
        elif mode == "secret":
            f.secret["metadata"]["uid"] = "replacement"
        elif mode == "missing":
            f.secret = None
        elif mode == "pending":
            f.namespace["status"]["phase"] = "Pending"
        else:
            f.secret["data"] = {}
        caught = await error(module.StoreHelmStopped, f.run())
        assert not caught.write_may_have_applied and not f.invocations
        assert f.workspace_closed and f.closed
    print("Lost claims, unavailable storage and missing/replaced resources run no Helm command")

    for operation in ("install", "list"):
        for mode in ("claim", "namespace", "secret"):
            f = Fixture()
            def changed():
                if mode == "claim":
                    f.saved_namespace = "replacement"
                elif mode == "namespace":
                    f.namespace["metadata"]["uid"] = "replacement"
                else:
                    f.secret["metadata"]["uid"] = "replacement"
            f.on_return = changed
            caught = await error(module.StoreHelmStopped, f.run(operation))
            assert caught.write_may_have_applied == (operation == "install")
            assert len(f.invocations) == 1 and f.process_stopped and f.workspace_closed
    print("Lost ownership after execution rejects success and preserves possible writes")

    for mode in ("claim", "namespace", "secret", "expiry"):
        f = Fixture()
        f.hold = True
        task = asyncio.create_task(f.run())
        try:
            await f.started.wait()
            if mode == "claim":
                f.before = StoreJobGuardUnavailable("PRIVATE STORAGE")
            elif mode == "namespace":
                f.namespace["metadata"]["uid"] = "replacement"
            elif mode == "secret":
                f.secret["metadata"]["uid"] = "replacement"
            else:
                f.cutoff = time.monotonic() + 0.02
            caught = await error(module.StoreHelmStopped, task)
            assert caught.write_may_have_applied
            assert f.process_stopped and f.workspace_closed and len(f.invocations) == 1
        finally:
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    print("Monitoring stops a running command when claims, resources or observed allowances change")

    f = Fixture()
    f.hold = f.hold_cleanup = True
    task = asyncio.create_task(f.run())
    try:
        await f.started.wait()
        task.cancel()
        await f.cleanup_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert f.active and not f.workspace_closed and not task.done()
        f.finish_cleanup.set()
        await error(asyncio.CancelledError, task)
        assert f.process_stopped and f.workspace_closed
    finally:
        f.finish_cleanup.set()
        if not task.done(): task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    print("Repeated cancellation awaits command cleanup before removing the workspace")

    f = Fixture()
    f.process_error = HelmProcessError("Safe fixture failure", write_may_have_applied=True, exit_code=1)
    caught = await error(HelmProcessError, f.run())
    assert caught is f.process_error and len(f.invocations) == 1
    assert f.process_stopped and f.workspace_closed

    f = Fixture()
    async def invalid(caller):
        await error(ValueError, caller._run("upgrade"))
    await f.run(callback=invalid)
    assert not f.invocations and not f.requests

    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline), asyncio.all_tasks() - baseline
    print("Process errors remain safe, unsupported commands execute nothing and no tasks remain")
    print("Guarded Helm verification passed. All upstream and process calls were mocked.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 20))
