"""Worker startup fixtures; no signals, subprocesses, database or HTTP are used."""

import asyncio
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
import signal
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store_worker_main as module


ENV = {
    "STORE_WORKER_ENABLED": "true",
    "STORE_PLATFORM_NAMESPACE": "platform",
    "STORE_PLATFORM_RELEASE": "platform",
    "STORE_ADMIN_EMAIL": "owner@example.test",
}


def error(kind, callback):
    try:
        callback()
    except kind:
        return
    raise AssertionError("Expected " + str(kind))


async def async_error(kind, operation):
    try:
        await operation
    except kind:
        return
    raise AssertionError("Expected " + str(kind))


def configuration():
    assert module.WorkerConfig.from_environment({}) is None
    assert module.WorkerConfig.from_environment({
        "STORE_WORKER_ENABLED": "false", "STORE_ADMIN_EMAIL": "ignored",
    }) is None
    config = module.WorkerConfig.from_environment(ENV)
    assert config.max_jobs == 100 and config.poll_seconds == 1
    assert config.scope.service_account == "store-provisioner-platform"
    assert module.WorkerConfig.from_environment({
        **ENV, "STORE_WORKER_MAX_JOBS": "6", "STORE_WORKER_POLL_SECONDS": "0.5",
    }).max_jobs == 6
    for key, value in (
        ("STORE_WORKER_ENABLED", "yes"), ("STORE_PLATFORM_NAMESPACE", ""),
        ("STORE_PLATFORM_RELEASE", "../other"), ("STORE_ADMIN_EMAIL", "bad"),
        ("STORE_ADMIN_EMAIL", "owner@example.test\n"),
        ("STORE_WORKER_MAX_JOBS", "0"), ("STORE_WORKER_MAX_JOBS", "101"),
        ("STORE_WORKER_MAX_JOBS", "2.0"), ("STORE_WORKER_MAX_JOBS", "9" * 500),
        ("STORE_WORKER_POLL_SECONDS", "nan"), ("STORE_WORKER_POLL_SECONDS", "inf"),
        ("STORE_WORKER_POLL_SECONDS", "0"), ("STORE_WORKER_POLL_SECONDS", "31"),
    ):
        error(ValueError, lambda: module.WorkerConfig.from_environment({**ENV, key: value}))
    for values in ({"max_jobs": True}, {"max_jobs": 101}, {"poll_seconds": float("nan")}):
        error(ValueError, lambda: replace(config, **values))
    with patch.object(module, "_dependencies", side_effect=AssertionError("Services started")), \
         patch.object(module, "check_package") as package, \
         redirect_stdout(StringIO()) as output:
        with patch.dict(module.os.environ, {}, clear=True):
            assert module.main([]) == 0
        assert "disabled" in output.getvalue()
        package.assert_not_called()
        assert module.main(["--check-package"]) == 0
        package.assert_called_once_with()
    with patch.object(module, "_dependencies", side_effect=AssertionError("Services started")), \
         redirect_stderr(StringIO()) as output:
        with patch.dict(module.os.environ, {**ENV, "STORE_ADMIN_EMAIL": "PRIVATE invalid"}, clear=True):
            assert module.main([]) == 2
        assert "PRIVATE" not in output.getvalue()
    print("Disabled startup and package checks need no database credentials; invalid settings start no services")


class Fixture:
    def __init__(self):
        self.config = module.WorkerConfig.from_environment(ENV)
        self.events = []
        self.signals = {}
        self.failure = None
        self.hold_work = False
        self.hold_close = False
        self.started, self.closing, self.resume = (asyncio.Event() for _ in range(3))
        self.raw = SimpleNamespace(aclose=self.close)
        self.engine = SimpleNamespace(dispose=self.dispose)
        self.handlers = SimpleNamespace(work=Mock(), finish=Mock())

    def factory(self):
        raise AssertionError("No actual database belongs in startup fixtures.")

    def dependencies(self):
        self.events.append("dependencies")
        if self.failure == "dependencies":
            raise RuntimeError("PRIVATE dependency failure")
        return SimpleNamespace(
            factory=self.factory, engine=self.engine,
            connection=SimpleNamespace(in_cluster=self.connect),
            handlers=self.make_handlers, worker=self.worker,
        )

    def connect(self):
        self.events.append("connect")
        if self.failure == "connect":
            raise RuntimeError("PRIVATE connection failure")
        return self.raw

    def make_handlers(self, factory, raw, scope, *, admin_email):
        assert factory == self.factory and raw is self.raw
        assert scope == self.config.scope and admin_email == self.config.admin_email
        self.events.append("handlers")
        if self.failure == "handlers":
            raise RuntimeError("PRIVATE handler failure")
        return self.handlers

    async def database(self, factory):
        assert factory == self.factory
        self.events.append("database")
        if self.failure == "database":
            raise RuntimeError("PRIVATE database failure")

    async def worker(self, factory, work, finish, *, stop_event, max_jobs, poll_seconds):
        assert factory == self.factory
        assert work is self.handlers.work and finish is self.handlers.finish
        assert max_jobs == 100 and poll_seconds == 1
        self.events.append("worker")
        try:
            self.started.set()
            if self.failure == "worker":
                raise RuntimeError("PRIVATE worker failure")
            if self.hold_work:
                await asyncio.Event().wait()
            else:
                self.signals[signal.SIGTERM]()
                self.signals[signal.SIGINT]()
                assert stop_event.is_set()
        finally:
            self.events.append("work-stopped")

    async def close(self):
        self.events.append("close")
        self.closing.set()
        if self.hold_close:
            await self.resume.wait()
        if self.failure == "close":
            raise RuntimeError("PRIVATE close failure")
        self.events.append("closed")

    async def dispose(self):
        self.events.append("dispose")

    def install(self, number, callback):
        assert number not in self.signals
        self.signals[number] = callback
        self.events.append("signal-added")
        if self.failure == "signal" and number == signal.SIGINT:
            # A failed installation must not leave this handler installed.
            del self.signals[number]
            raise NotImplementedError("PRIVATE unsupported signals")

    def remove(self, number):
        self.events.append("signal-removed")
        del self.signals[number]

    async def run(self):
        loop = asyncio.get_running_loop()
        with ExitStack() as stack:
            package = stack.enter_context(patch.object(module, "check_package"))
            stack.enter_context(patch.object(module, "_dependencies", self.dependencies))
            stack.enter_context(patch.object(module, "_check_database", self.database))
            stack.enter_context(patch.object(loop, "add_signal_handler", self.install))
            stack.enter_context(patch.object(loop, "remove_signal_handler", self.remove))
            try:
                return await module.serve(self.config)
            finally:
                package.assert_called_once_with(profile=True)


async def started(task, signal_event):
    waiter = asyncio.create_task(signal_event.wait())
    try:
        done, _ = await asyncio.wait(
            (task, waiter), timeout=5, return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            await task
            raise AssertionError("Runtime ended before reaching its pause.")
        assert waiter in done
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def check():
    baseline = asyncio.all_tasks()
    configuration()
    f = Fixture()
    await f.run()
    assert f.events.index("database") < f.events.index("worker")
    assert f.events.index("work-stopped") < f.events.index("close")
    assert f.events.index("closed") < f.events.index("dispose")
    assert not f.signals
    print("SIGTERM/SIGINT stop dispatch; jobs stop before the shared HTTP client and database close")

    for failure in ("signal", "dependencies", "connect", "handlers", "database", "worker", "close"):
        f = Fixture()
        f.failure = failure
        await async_error((RuntimeError, NotImplementedError), f.run())
        assert not f.signals
        if failure not in ("signal", "dependencies"):
            assert f.events.count("dispose") == 1
        if failure in ("handlers", "database", "worker", "close"):
            assert f.events.count("close") == 1
        if failure in ("signal", "dependencies", "connect", "handlers", "database"):
            assert "worker" not in f.events
    print("Failed startup and runtime failures close owned resources and remove installed handlers")

    for repeated in (False, True):
        f = Fixture()
        f.hold_work = f.hold_close = True
        task = asyncio.create_task(f.run())
        try:
            await started(task, f.started)
            task.cancel()
            await started(task, f.closing)
            if repeated:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done() and "dispose" not in f.events
            f.resume.set()
            await async_error(asyncio.CancelledError, task)
        finally:
            f.resume.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert f.events.index("work-stopped") < f.events.index("close")
        assert f.events.index("closed") < f.events.index("dispose")
        assert not f.signals
    print("Single and repeated cancellation await HTTP/database cleanup without remaining runtime tasks")

    def crash(coroutine):
        coroutine.close()
        raise RuntimeError("PRIVATE startup detail")
    with patch.object(module.asyncio, "run", crash), \
         redirect_stderr(StringIO()) as output, \
         patch.dict(module.os.environ, ENV, clear=True):
        assert module.main([]) == 1
        assert "PRIVATE" not in output.getvalue()
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)
    print("Command-line failures expose a fixed message; no services or worker were started")
    print("Worker startup verification passed. All runtime resources were fixtures.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 30))
