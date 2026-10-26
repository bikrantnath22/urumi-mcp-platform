"""Run one private job stage while renewing its existing worker lease."""

import asyncio

from app.store_job_guard import StoreJobClaimLost, require_claim
from app.store_jobs import renew_claim


_RENEW_EVERY_SECONDS = 20
_RENEW_TIMEOUT_SECONDS = 10


class StoreLeaseStopped(StoreJobClaimLost):
    """Lease ownership could not be confirmed; the stage must stop."""


async def _renew(session_factory, checked, for_cleanup):
    remaining = checked.remaining_seconds
    if remaining <= 0:
        raise StoreLeaseStopped("The worker lease allowance has elapsed.")
    try:
        async with asyncio.timeout(min(_RENEW_TIMEOUT_SECONDS, remaining)):
            renewed = await renew_claim(session_factory, checked.claim)
            if renewed is None:
                raise StoreLeaseStopped("The worker lease was lost.")
            return await require_claim(
                session_factory, checked.claim, for_cleanup=for_cleanup,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        raise StoreLeaseStopped(
            "The worker lease could not be renewed; work stopped.",
        ) from None


async def _stop_tasks(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    drained = asyncio.gather(*tasks, return_exceptions=True)
    try:
        await asyncio.shield(drained)
    except asyncio.CancelledError:
        # Further cancellation must not let the caller release unfinished work.
        while not drained.done():
            try:
                await asyncio.shield(drained)
            except asyncio.CancelledError:
                continue
        raise


async def run_with_lease(session_factory, claim, work, *, for_cleanup=False):
    """Run an async work(claim) callback under the same renewable lease.

    The callback must await its request/subprocess cleanup when cancelled.
    Complete or release the database job only after this function has exited.
    This helper neither claims another job nor changes its original deadline.
    """
    if not callable(session_factory) or not callable(work):
        raise ValueError("A session factory and async work callback are required.")

    async with asyncio.timeout(_RENEW_TIMEOUT_SECONDS):
        checked = await require_claim(
            session_factory, claim, for_cleanup=for_cleanup,
        )
    checked = await _renew(session_factory, checked, for_cleanup)
    work_claim = checked.claim

    async def keep_alive():
        nonlocal checked
        while True:
            remaining = checked.remaining_seconds
            if remaining <= 0:
                raise StoreLeaseStopped("The worker lease allowance has elapsed.")
            await asyncio.sleep(min(_RENEW_EVERY_SECONDS, remaining))
            checked = await _renew(session_factory, checked, for_cleanup)

    async def execute():
        return await work(work_claim)

    tasks = []
    try:
        keeper = asyncio.create_task(keep_alive())
        tasks.append(keeper)
        running = asyncio.create_task(execute())
        tasks.append(running)
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if keeper.done():
            keeper.result()
            raise StoreLeaseStopped("Worker lease renewal stopped unexpectedly.")
        result = running.result()

        remaining = checked.remaining_seconds
        if remaining <= 0:
            raise StoreLeaseStopped("The worker lease allowance has elapsed.")
        async with asyncio.timeout(min(_RENEW_TIMEOUT_SECONDS, remaining)):
            await require_claim(
                session_factory, work_claim, for_cleanup=for_cleanup,
            )
        if keeper.done():
            keeper.result()
            raise StoreLeaseStopped("Worker lease renewal stopped unexpectedly.")
        return result
    finally:
        await _stop_tasks(tasks)
