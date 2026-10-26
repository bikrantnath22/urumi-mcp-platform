"""Private concurrent job loop; real provisioning handlers are supplied separately."""

import asyncio
import math

from app.store_jobs import claim_next, release_claim
from app.store_lease_runner import _stop_tasks, run_with_lease


from app.observability import diagnostic_logger, failure_code, request_id
from app.store_worker_health import WorkerHealth

_LOG = diagnostic_logger(__name__)
_STORAGE_TIMEOUT_SECONDS = 10


async def _release(session_factory, claim):
    try:
        async with asyncio.timeout(_STORAGE_TIMEOUT_SECONDS):
            await release_claim(session_factory, claim, retry_after_seconds=5)
    except asyncio.CancelledError:
        raise
    except Exception:
        # A lost reply or unavailable database is recovered through lease expiry.
        _LOG.warning(
            "Store job release could not be confirmed.",
            extra={"operation_id": str(claim.operation_id)},
        )


async def _run_claim(session_factory, claim, work, finish, stop_event):
    token = request_id.set(str(claim.operation_id))
    cleanup = claim.action == "delete" or claim.deadline_reached

    async def execute(current):
        if stop_event.is_set():
            raise asyncio.CancelledError()
        return await work(current, for_cleanup=cleanup)

    try:
        result = await run_with_lease(
            session_factory, claim, execute, for_cleanup=cleanup,
        )
        async with asyncio.timeout(_STORAGE_TIMEOUT_SECONDS):
            await finish(claim, result, for_cleanup=cleanup)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        # Do not declare failure before resource cleanup has been verified.
        _LOG.warning(
            "Store job stopped; saved progress will be checked on recovery.",
            extra={"operation_id": str(claim.operation_id), "store_id": str(claim.store_id),
                   "action": claim.action, "attempt": claim.attempt, "reason_code": failure_code(error)},
        )
    finally:
        request_id.reset(token)


async def _shutdown(session_factory, active, stop_task):
    await _stop_tasks([stop_task, *active])
    # Include claims whose tasks were cancelled before they could start.
    await asyncio.gather(*(
        _release(session_factory, claim) for claim in active.values()
    ), return_exceptions=True)


async def run_store_worker(
    session_factory, work, finish, *, stop_event,
    max_jobs=100, poll_seconds=1.0,
):
    """Dispatch private jobs under the database scheduler's shared limits.

    work(claim, for_cleanup=...) performs guarded external stages.
    finish(claim, result, for_cleanup=...) commits a verified terminal state,
    or leaves incomplete work for a later pass. It performs no external work.
    Work must await request/subprocess cleanup when cancelled.
    max_jobs is a local ceiling; PostgreSQL enforces the admin's global limit.
    """
    if (
        not callable(session_factory)
        or not callable(work)
        or not callable(finish)
        or not isinstance(stop_event, asyncio.Event)
        or type(max_jobs) is not int
        or not 1 <= max_jobs <= 100
        or type(poll_seconds) not in (int, float)
        or not math.isfinite(poll_seconds)
        or not 0.05 <= poll_seconds <= 30
    ):
        raise ValueError("Invalid private store worker configuration.")

    health = WorkerHealth.from_environment()
    active = {}
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        while not stop_event.is_set():
            health.beat()
            for task in tuple(active):
                if task.done():
                    if not task.cancelled():
                        task.exception()
                    await _release(session_factory, active[task])
                    health.beat()
                    del active[task]

            if stop_event.is_set():
                break

            if len(active) < max_jobs:
                claim = None
                try:
                    async with asyncio.timeout(_STORAGE_TIMEOUT_SECONDS):
                        claim = await claim_next(session_factory)
                    health.beat(queue_ok=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    health.beat(queue_ok=False)
                    _LOG.warning("Store job queue is temporarily unavailable.", extra={"reason_code": "queue_unavailable"})

                if claim is not None:
                    task = asyncio.create_task(
                        _run_claim(
                            session_factory, claim, work, finish, stop_event,
                        ),
                    )
                    active[task] = claim
                    continue

            await asyncio.wait(
                {stop_task, *active},
                timeout=poll_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
    finally:
        cleanup = asyncio.create_task(
            _shutdown(session_factory, active, stop_task),
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            raise
