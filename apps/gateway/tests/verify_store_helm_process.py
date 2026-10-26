"""Helm runner checks with mocked processes and temporary local Python children.

Run in WSL from apps/gateway. No Helm, Kubernetes, database or network calls.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store_helm_process as module

import asyncio
from contextlib import contextmanager
import signal
from unittest.mock import patch

class Input:
    def __init__(self):
        self.data = b""
        self.closed = False
    def write(self, data): self.data += data
    async def drain(self): await asyncio.sleep(0)
    def close(self): self.closed = True

class Process:
    def __init__(self, *, out=b"", err=b"", code=0, ignore_term=False):
        self.pid = 12345
        self.stdin = Input()
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdout.feed_data(out)
        self.stderr.feed_data(err)
        self.returncode = code
        self.exited = asyncio.Event()
        self.term = asyncio.Event()
        self.ignore_term = ignore_term
        self.signals = []
        if code is not None:
            self.finish(code)
    def finish(self, code):
        self.returncode = code
        self.exited.set()
        if not self.stdout.at_eof(): self.stdout.feed_eof()
        if not self.stderr.at_eof(): self.stderr.feed_eof()
    async def wait(self):
        await self.exited.wait()
        return self.returncode
    def killpg(self, pid, number):
        assert pid == self.pid
        self.signals.append(number)
        if number == signal.SIGTERM:
            self.term.set()
            if self.ignore_term: return
        self.finish(-number)

class Fixture:
    def __init__(self, **kwargs):
        self.process = Process(**kwargs)
        self.started = asyncio.Event()
        self.gate = None
        self.calls = []
    async def spawn(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.started.set()
        if self.gate is not None: await self.gate.wait()
        return self.process
    @contextmanager
    def patches(self):
        with (
            patch.object(module.asyncio, "create_subprocess_exec", self.spawn),
            patch.object(module.os, "killpg", self.process.killpg, create=True),
            patch.object(module.signal, "SIGKILL", 9, create=True),
            patch.object(module, "_STOP_GRACE_SECONDS", 0.02),
        ):
            yield
    async def run(self, *, writing=True, timeout=1, payload=b"{}", env=None):
        return await module.run_helm_process(
            "/fixture/helm", ("install", "store"),
            env=env or {"PATH": "/fixture"}, payload=payload,
            timeout_seconds=timeout, writing=writing,
        )

async def error(kind, operation):
    try: await operation
    except kind as exc: return exc
    raise AssertionError("Expected " + kind.__name__)

async def check_mocked():
    baseline = asyncio.all_tasks()
    f = Fixture(out=b'{"name":"store"}', err=b"PRIVATE HELM DETAIL")
    settings = {"PATH": "/fixture"}
    with f.patches():
        assert await f.run(env=settings) == b'{"name":"store"}'
    args, options = f.calls[0]
    assert args == ("/fixture/helm", "install", "store")
    assert options["env"] == settings and options["env"] is not settings
    assert options["cwd"] == "/" and options["start_new_session"] is True
    assert "shell" not in options and f.process.stdin.data == b"{}"
    assert f.process.stdin.closed
    print("Fixed executable arguments, copied environment, stdin and bounded stdout passed")

    for writing in (False, True):
        f = Fixture(err=b"PRIVATE TOKEN", code=1)
        with f.patches():
            exc = await error(module.HelmProcessError, f.run(writing=writing))
        assert exc.exit_code == 1 and exc.write_may_have_applied == writing
        assert "PRIVATE" not in str(exc)
    for target in ("out", "err"):
        f = Fixture(**{target: b"x" * (module._MAX_OUTPUT + 1), "code": None})
        with f.patches():
            exc = await error(module.HelmProcessError, f.run())
        assert "response limit" in str(exc) and exc.write_may_have_applied
        assert f.process.returncode is not None
    f = Fixture(
        out=b"x" * (module._MAX_OUTPUT // 2 + 1),
        err=b"x" * (module._MAX_OUTPUT // 2 + 1), code=None,
    )
    with f.patches():
        await error(module.HelmProcessError, f.run())
    print("Nonzero exits hide diagnostics; combined stdout/stderr limits stop execution")

    f = Fixture(code=None, ignore_term=True)
    with f.patches():
        exc = await error(module.HelmProcessError, f.run(timeout=0.01))
    assert "deadline" in str(exc) and exc.write_may_have_applied
    assert f.process.returncode == -9
    assert f.process.signals == [signal.SIGTERM, 9]
    print("Timeout stops the process group; a process ignoring TERM is killed and reaped")

    for delayed_spawn in (False, True):
        f = Fixture(code=None, ignore_term=True)
        if delayed_spawn: f.gate = asyncio.Event()
        with f.patches():
            task = asyncio.create_task(f.run())
            await f.started.wait()
            task.cancel()
            await asyncio.sleep(0)
            if delayed_spawn:
                assert not task.done() and not f.process.signals
            else:
                await f.process.term.wait()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            if delayed_spawn: f.gate.set()
            await error(asyncio.CancelledError, task)
        assert f.process.returncode is not None and f.process.stdin.closed
    print("Single/repeated cancellation awaits cleanup, including cancellation during spawning")

    f = Fixture()
    with f.patches():
        for kwargs in (
            {"executable": "helm"}, {"arguments": []}, {"arguments": ("x\x00y",)},
            {"env": {}}, {"env": {"A": "x\x00y"}}, {"payload": "text"},
            {"payload": b"x" * (module._MAX_INPUT + 1)},
            {"timeout_seconds": True}, {"timeout_seconds": 0},
            {"timeout_seconds": float("nan")}, {"timeout_seconds": 3601},
            {"writing": 1},
        ):
            arguments = dict(
                executable="/fixture/helm", arguments=("status", "store"),
                env={"PATH": "/fixture"}, payload=b"", timeout_seconds=1, writing=False,
            )
            arguments.update(kwargs)
            await error(ValueError, module.run_helm_process(**arguments))
    assert not f.calls
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline), asyncio.all_tasks() - baseline
    print("Invalid configuration sends no subprocess request; no runner tasks remain")

from contextlib import asynccontextmanager
import json
import os

_NATIVE_SPAWN = asyncio.create_subprocess_exec
_NATIVE_SIGNAL = module._signal_group


class RealFixture:
    def __init__(self, ready=False):
        self.needs_ready = ready
        self.started = asyncio.Event()
        self.term_sent = asyncio.Event()
        self.processes = []
        self.tasks = []
        self.child_pid = None

    async def spawn(self, *args, **kwargs):
        assert kwargs["start_new_session"] is True
        assert args[0] == sys.executable
        process = await _NATIVE_SPAWN(*args, **kwargs)
        self.processes.append(process)
        if self.needs_ready:
            while True:
                line = await process.stdout.readline()
                if line == b"READY\n":
                    break
                assert line.startswith(b"CHILD:"), "Missing fixture readiness"
                self.child_pid = int(line.removeprefix(b"CHILD:"))
        self.started.set()
        return process

    def signal(self, process, number):
        assert process in self.processes
        if number == signal.SIGTERM:
            self.term_sent.set()
        _NATIVE_SIGNAL(process, number)

    def start(self, body, *, args=(), payload=b"", seconds=10, writing=True):
        task = asyncio.create_task(module.run_helm_process(
            sys.executable, ("-I", "-S", "-B", "-u", "-c", body, *args),
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "FIXTURE_ONLY": "kept"},
            payload=payload, timeout_seconds=seconds, writing=writing,
        ))
        self.tasks.append(task)
        return task


@asynccontextmanager
async def real_case(*, ready=False):
    fixture = RealFixture(ready)
    with (
        patch.object(module.asyncio, "create_subprocess_exec", fixture.spawn),
        patch.object(module, "_signal_group", fixture.signal),
    ):
        try:
            yield fixture
        finally:
            for task in fixture.tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*fixture.tasks, return_exceptions=True)
            for process in fixture.processes:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()


def assert_reaped(process):
    assert process.returncode is not None
    try:
        os.waitpid(process.pid, os.WNOHANG)
    except ChildProcessError:
        return
    raise AssertionError("The process was not reaped")


async def check_real():
    baseline = asyncio.all_tasks()
    body = """import json, os, sys
data = sys.stdin.buffer.read()
print(json.dumps({
    'input': data.decode(), 'argument': sys.argv[1], 'cwd': os.getcwd(),
    'marker': os.getenv('FIXTURE_ONLY'),
    'parent_only': os.getenv('URUMI_FIXTURE_PARENT_ONLY'),
}))
"""
    with patch.dict(os.environ, {"URUMI_FIXTURE_PARENT_ONLY": "private"}):
        async with real_case() as fixture:
            output = await fixture.start(
                body, args=("literal ; $HOME",), payload=b'{"store":"fixture"}',
            )
            assert json.loads(output) == {
                "input": '{"store":"fixture"}', "argument": "literal ; $HOME",
                "cwd": "/", "marker": "kept", "parent_only": None,
            }
            assert_reaped(fixture.processes[0])
    print("Real Python subprocess preserves literal arguments and receives only its supplied environment")

    async with real_case() as fixture:
        exc = await error(module.HelmProcessError, fixture.start(
            "import sys; sys.stderr.write('PRIVATE FIXTURE DETAIL'); sys.exit(7)",
        ))
        assert exc.exit_code == 7 and exc.write_may_have_applied
        assert "PRIVATE" not in str(exc)
        assert_reaped(fixture.processes[0])

    for output, errors in (
        (module._MAX_OUTPUT + 1, 0),
        (0, module._MAX_OUTPUT + 1),
        (module._MAX_OUTPUT // 2 + 1, module._MAX_OUTPUT // 2 + 1),
    ):
        async with real_case() as fixture:
            body = (
                "import sys; "
                f"sys.stdout.buffer.write(b'x' * {output}); sys.stdout.flush(); "
                f"sys.stderr.buffer.write(b'x' * {errors}); sys.stderr.flush()"
            )
            exc = await error(module.HelmProcessError, fixture.start(body))
            assert "response limit" in str(exc) and exc.write_may_have_applied
            assert_reaped(fixture.processes[0])
    print("Real output limits drain both pipes; failures expose no private diagnostics")

    ignored = """import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print('READY', flush=True)
time.sleep(60)
"""
    async with real_case(ready=True) as fixture:
        task = fixture.start(ignored, seconds=2)
        await fixture.started.wait()
        exc = await error(module.HelmProcessError, task)
        assert "deadline" in str(exc) and fixture.term_sent.is_set()
        assert fixture.processes[0].returncode == -signal.SIGKILL
        assert_reaped(fixture.processes[0])
    print("Real timeout sends TERM, then KILL to a process that ignores TERM, and waits for reaping")

    for repeated in (False, True):
        async with real_case(ready=True) as fixture:
            with patch.object(module, "_STOP_GRACE_SECONDS", 0.3):
                task = fixture.start(ignored)
                await fixture.started.wait()
                task.cancel()
                await fixture.term_sent.wait()
                if repeated:
                    task.cancel()
                    await asyncio.sleep(0)
                await error(asyncio.CancelledError, task)
            assert fixture.processes[0].returncode == -signal.SIGKILL
            assert_reaped(fixture.processes[0])
    print("Real single/repeated cancellation waits for forced shutdown")

    family = """import os, signal, time
child = os.fork()
if child == 0:
    while True:
        time.sleep(60)
def stop(*unused):
    os.waitpid(child, 0)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
print('CHILD:' + str(child), flush=True)
print('READY', flush=True)
time.sleep(60)
"""
    async with real_case(ready=True) as fixture:
        task = fixture.start(family)
        await fixture.started.wait()
        task.cancel()
        await error(asyncio.CancelledError, task)
        assert fixture.processes[0].returncode == 0
        assert_reaped(fixture.processes[0])
        try:
            os.kill(fixture.child_pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("The fixture descendant is still present")
    print("Cancellation stops the whole process group, including its temporary descendant")
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline), "A process runner task remained"


async def check():
    assert os.name == "posix" and hasattr(os, "fork"), "Run this test in WSL/Linux"
    await check_mocked()
    await check_real()
    print("Helm process verification passed. No Helm, cluster, database or network calls made.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 60))
