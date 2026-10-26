"""Private Linux subprocess lifecycle for Helm; this module grants no job access."""

import asyncio
from collections.abc import Mapping
import math
import os
from pathlib import PurePosixPath
import signal


_MAX_INPUT = 64 * 1024
_MAX_OUTPUT = 1024 * 1024
_STOP_GRACE_SECONDS = 3


class HelmProcessError(RuntimeError):
    def __init__(self, message, *, write_may_have_applied=False, exit_code=None):
        super().__init__(message)
        self.write_may_have_applied = write_may_have_applied
        self.exit_code = exit_code


def _configuration(executable, arguments, env, payload, seconds, writing):
    if (
        not isinstance(executable, str)
        or not executable.startswith("/")
        or "\x00" in executable
        or ".." in PurePosixPath(executable).parts
        or not isinstance(arguments, (tuple, list))
        or not 1 <= len(arguments) <= 128
        or any(not isinstance(arg, str) or "\x00" in arg for arg in arguments)
        or sum(len(arg.encode("utf-8")) for arg in arguments) > 65536
        or not isinstance(env, Mapping)
        or not env
        or any(
            not isinstance(key, str) or not key or "=" in key or "\x00" in key
            or not isinstance(value, str) or "\x00" in value
            for key, value in env.items()
        )
        or not isinstance(payload, bytes)
        or len(payload) > _MAX_INPUT
        or type(seconds) not in (int, float)
        or not math.isfinite(seconds)
        or not 0 < seconds <= 3600
        or type(writing) is not bool
    ):
        raise ValueError("Invalid private Helm process configuration.")
    return tuple(arguments), dict(env)


async def _read(stream, overflow, budget, *, retain):
    chunks = []
    while chunk := await stream.read(16384):
        if overflow.is_set():
            continue
        budget[0] += len(chunk)
        if budget[0] > _MAX_OUTPUT:
            overflow.set()
        elif retain:
            chunks.append(chunk)
    return b"".join(chunks)


async def _feed(process, payload):
    try:
        process.stdin.write(payload)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        process.stdin.close()


async def _discard(stream):
    while await stream.read(16384):
        pass


def _signal_group(process, number):
    try:
        os.killpg(process.pid, number)
    except ProcessLookupError:
        pass


async def _cleanup(spawn, tasks):
    # Shielded spawning lets cancellation still obtain and stop a created child.
    if spawn is None:
        return
    try:
        process = await spawn
    except Exception:
        return
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    process.stdin.close()
    drains = [
        asyncio.create_task(_discard(stream))
        for stream in (process.stdout, process.stderr)
    ]
    try:
        _signal_group(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), _STOP_GRACE_SECONDS)
        except TimeoutError:
            pass
        # Also stop descendants if the process leader has already exited.
        _signal_group(process, signal.SIGKILL)
        await process.wait()
    finally:
        await asyncio.gather(*drains, return_exceptions=True)


async def _await_cleanup(spawn, tasks):
    cleanup = asyncio.create_task(_cleanup(spawn, tasks))
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    try:
        cleanup.result()
    except Exception:
        raise HelmProcessError(
            "Helm process cleanup could not be confirmed.",
            write_may_have_applied=True,
        ) from None
    if cancelled:
        raise asyncio.CancelledError()


async def run_helm_process(
    executable, arguments, *, env, payload=b"", timeout_seconds, writing,
):
    """Run one internal command and return bounded stdout bytes.

    The caller supplies a fixed executable, arguments and isolated environment.
    It must authorize the live claim and both saved UIDs before starting, keep
    renewing the lease, cancel this call on lost ownership, and verify the
    outcome afterward. This function does not retry or declare a store Ready.
    Cancellation waits for subprocess cleanup before returning to the worker.
    """
    arguments, environment = _configuration(
        executable, arguments, env, payload, timeout_seconds, writing,
    )
    if not hasattr(os, "killpg"):
        raise ValueError("The private Helm process runner requires Linux/POSIX.")

    spawn = None
    tasks = []
    try:
        async with asyncio.timeout(timeout_seconds):
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                executable, *arguments,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                cwd="/",
                start_new_session=True,
                limit=32768,
            ))
            process = await asyncio.shield(spawn)
            overflow = asyncio.Event()
            budget = [0]
            output = asyncio.create_task(
                _read(process.stdout, overflow, budget, retain=True),
            )
            errors = asyncio.create_task(
                _read(process.stderr, overflow, budget, retain=False),
            )
            feed = asyncio.create_task(_feed(process, payload))
            exited = asyncio.create_task(process.wait())
            too_large = asyncio.create_task(overflow.wait())
            tasks.extend((output, errors, feed, exited, too_large))
            pending = {output, errors, feed, exited}
            while pending:
                done, _ = await asyncio.wait(
                    pending | {too_large},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if too_large in done:
                    raise HelmProcessError(
                        "Helm output exceeded the private response limit.",
                        write_may_have_applied=writing,
                    )
                for task in done:
                    task.result()
                    pending.discard(task)
            if overflow.is_set():
                raise HelmProcessError(
                    "Helm output exceeded the private response limit.",
                    write_may_have_applied=writing,
                )
            if process.returncode != 0:
                raise HelmProcessError(
                    "Helm did not complete successfully.",
                    write_may_have_applied=writing,
                    exit_code=process.returncode,
                )
            return output.result()
    except asyncio.CancelledError:
        raise
    except HelmProcessError:
        raise
    except TimeoutError:
        raise HelmProcessError(
            "Helm exceeded its command deadline.",
            write_may_have_applied=writing and spawn is not None,
        ) from None
    except Exception:
        raise HelmProcessError(
            "Helm execution could not be confirmed.",
            write_may_have_applied=writing and spawn is not None,
        ) from None
    finally:
        await _await_cleanup(spawn, tasks)
