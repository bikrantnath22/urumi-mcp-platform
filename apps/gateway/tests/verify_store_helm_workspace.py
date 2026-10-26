"""WSL checks for private Helm files and cleanup; no cluster or database calls."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import json
import os
import stat
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from app import store_helm_workspace as module
from app.store_namespace import StoreNamespace
from verify_store_helm_process import assert_reaped, error, real_case


def identity():
    return StoreNamespace(uuid4(), 1)


def layout(config):
    root = Path(config.workspace)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for name in ("tmp", "cache", "config", "data", "plugins"):
        path = root / name
        assert path.is_dir() and stat.S_IMODE(path.stat().st_mode) == 0o700
    path = Path(config.kubeconfig_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_uid == os.geteuid()
    assert json.loads(path.read_text()) == config.kubeconfig()
    assert set(config.kubeconfig()["users"][0]["user"]) == {"tokenFile"}
    return root


async def check():
    assert os.name == "posix", "Run this check in WSL/Linux"
    baseline = asyncio.all_tasks()
    with module.helm_workspace(identity()) as first:
        first_root = layout(first)
        with module.helm_workspace(identity()) as second:
            second_root = layout(second)
            assert first_root != second_root
        assert not second_root.exists() and first_root.exists()
    assert not first_root.exists()
    print("Private folder/file permissions, token-file references and separate workspaces passed")

    class CallerFailure(Exception):
        pass

    try:
        with module.helm_workspace(identity()) as config:
            root = layout(config)
            raise CallerFailure("fixture caller")
    except CallerFailure:
        pass
    else:
        raise AssertionError("Caller failure was hidden")
    assert not root.exists()

    roots, handles = [], []
    native_directory = TemporaryDirectory
    native_fdopen = module.os.fdopen

    def directory(**kwargs):
        temporary = native_directory(**kwargs)
        roots.append(Path(temporary.name))
        return temporary

    def fdopen(*args, **kwargs):
        handle = native_fdopen(*args, **kwargs)
        handles.append(handle)
        return handle

    with (
        patch.object(module, "TemporaryDirectory", directory),
        patch.object(module.os, "fdopen", fdopen),
        patch.object(module.json, "dump", side_effect=OSError("PRIVATE WRITE DETAIL")),
    ):
        try:
            with module.helm_workspace(identity()):
                raise AssertionError("Incomplete setup was yielded")
        except module.HelmWorkspaceError as exc:
            assert "PRIVATE" not in str(exc)
    assert handles and all(handle.closed for handle in handles)
    assert roots and all(not root.exists() for root in roots)
    print("Caller errors survive; failed writes close files and remove partial workspaces")

    with patch.object(module, "TemporaryDirectory", side_effect=PermissionError("PRIVATE STORAGE")):
        try:
            with module.helm_workspace(identity()):
                raise AssertionError("Unavailable storage was accepted")
        except module.HelmWorkspaceError as exc:
            assert "PRIVATE" not in str(exc)

    with patch.object(module, "TemporaryDirectory") as factory:
        for invalid in (None, "platform", object()):
            try:
                with module.helm_workspace(invalid):
                    raise AssertionError("Invalid identity was accepted")
            except ValueError:
                pass
        factory.assert_not_called()

    def cleanup_failure(**kwargs):
        temporary = directory(**kwargs)
        cleanup = temporary.cleanup
        def fail():
            cleanup()
            raise PermissionError("PRIVATE CLEANUP DETAIL")
        temporary.cleanup = fail
        return temporary

    with patch.object(module, "TemporaryDirectory", cleanup_failure):
        try:
            with module.helm_workspace(identity()) as config:
                layout(config)
        except module.HelmWorkspaceError as exc:
            assert "cleanup" in str(exc) and "PRIVATE" not in str(exc)
        else:
            raise AssertionError("Cleanup failure was hidden")
    assert all(not root.exists() for root in roots)
    print("Unavailable storage, invalid identities and cleanup errors return safe failures")

    with native_directory(prefix="urumi-workspace-test-", dir="/tmp") as external:
        sentinel = Path(external) / "preserve.txt"
        sentinel.write_text("fixture")
        def linked_directory(**kwargs):
            temporary = directory(**kwargs)
            (Path(temporary.name) / "kubeconfig.json").symlink_to(sentinel)
            return temporary
        with patch.object(module, "TemporaryDirectory", linked_directory):
            try:
                with module.helm_workspace(identity()):
                    raise AssertionError("An existing kubeconfig link was accepted")
            except module.HelmWorkspaceError:
                pass
        assert sentinel.read_text() == "fixture"
    assert all(not root.exists() for root in roots)
    print("Existing kubeconfig links are rejected; cleanup preserves unrelated files")

    ready = asyncio.Queue()
    release = asyncio.Event()
    async def parallel():
        with module.helm_workspace(identity()) as config:
            root = layout(config)
            ready.put_nowait(root)
            await release.wait()
        assert not root.exists()
    tasks = [asyncio.create_task(parallel()) for _ in range(4)]
    active = []
    try:
        for _ in tasks:
            active.append(await ready.get())
        assert len(set(active)) == 4 and all(root.exists() for root in active)
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert all(not root.exists() for root in active)
    print("Four concurrent tasks use different workspaces and clean them independently")

    body = """import json, signal, sys, time
def stop(*unused):
    try:
        with open(sys.argv[1]) as handle:
            json.load(handle)
    except Exception:
        raise SystemExit(42)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
print('READY', flush=True)
time.sleep(60)
"""
    async with real_case(ready=True) as process:
        locations = []
        async def running():
            with module.helm_workspace(identity()) as config:
                locations.append(layout(config))
                await process.start(body, args=(config.kubeconfig_path,))
        task = asyncio.create_task(running())
        try:
            await process.started.wait()
            assert locations[0].exists()
            task.cancel()
            await error(asyncio.CancelledError, task)
            assert process.processes[0].returncode == 0
            assert_reaped(process.processes[0])
            assert not locations[0].exists()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)
    print("Cancellation keeps kubeconfig available until the real subprocess has stopped")
    print("Helm workspace verification passed. Temporary test files were removed.")
    print("No Helm, Kubernetes, database or external service calls were made.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 30))
