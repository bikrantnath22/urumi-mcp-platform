"""Lease lifecycle checks using the existing isolated PostgreSQL fixtures."""

import asyncio
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import func

from app import store_jobs as jobs
from app import store_lease_runner as runner
from app.models import StoreOperation
from app.store_job_guard import StoreJobClaimLost, require_claim


async def expect_error(expected, operation):
    try:
        await operation
    except expected as error:
        return error
    raise AssertionError("Expected " + expected.__name__)


async def verify_lease_runner(factory, fresh, change, snapshot, unlocked):
    async def operation(claim):
        async with factory() as db:
            result = await db.get(StoreOperation, claim.operation_id)
            assert result is not None
            return result

    real_renew = runner.renew_claim
    claim = await fresh()
    count = 0
    committed, resume = asyncio.Event(), asyncio.Event()

    async def observed_renew(factory, current):
        nonlocal count
        renewed = await real_renew(factory, current)
        if renewed is not None:
            count += 1
            if count == 3:
                # Pause after commit, with no transaction held.
                committed.set()
                await resume.wait()
        return renewed

    async def active_work(current):
        await committed.wait()
        await unlocked(current)
        row = await operation(current)
        assert row.status == "Running"
        assert row.lease_token == claim.lease_token
        assert row.attempts == claim.attempt
        assert row.deadline_at == claim.deadline_at
        assert row.lease_expires_at > claim.lease_expires_at
        before = await snapshot()
        competitors = await asyncio.gather(*(
            jobs.claim_next(factory) for _ in range(4)
        ))
        assert competitors == [None] * 4
        assert await snapshot() == before
        return "verified"

    try:
        with (
            patch.object(runner, "renew_claim", observed_renew),
            patch.object(runner, "_RENEW_EVERY_SECONDS", 0.05),
        ):
            result = await asyncio.wait_for(
                runner.run_with_lease(factory, claim, active_work), 10,
            )
        assert result == "verified" and count == 3
    finally:
        resume.set()
    row = await operation(claim)
    assert row.status == "Running" and row.deadline_at == claim.deadline_at
    assert await jobs.release_claim(factory, claim, retry_after_seconds=0)
    recovered = await jobs.claim_next(factory)
    assert recovered is not None and recovered.lease_token != claim.lease_token
    assert recovered.attempt == claim.attempt + 1
    assert recovered.deadline_at == claim.deadline_at
    print("Renewals commit before work continues; competitors cannot take the active job")

    claim = await fresh()
    count = 0
    stopped = asyncio.Event()

    async def expire_before_renewal(factory, current):
        nonlocal count
        count += 1
        if count == 2:
            await change(
                StoreOperation, current.operation_id,
                lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
            )
        return await real_renew(factory, current)

    async def waiting_work(current):
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            stopped.set()

    with (
        patch.object(runner, "renew_claim", expire_before_renewal),
        patch.object(runner, "_RENEW_EVERY_SECONDS", 0.05),
    ):
        await expect_error(
            runner.StoreLeaseStopped,
            asyncio.wait_for(
                runner.run_with_lease(factory, claim, waiting_work), 10,
            ),
        )
    assert stopped.is_set()
    recovered = await jobs.claim_next(factory)
    assert recovered is not None and recovered.lease_token != claim.lease_token
    assert recovered.deadline_at == claim.deadline_at
    await expect_error(StoreJobClaimLost, require_claim(factory, claim))
    print("Actual lease loss stops work before recovery; the old token cannot continue")

    for repeated in (False, True):
        claim = await fresh()
        started, cleaning = asyncio.Event(), asyncio.Event()
        finished, resume = asyncio.Event(), asyncio.Event()

        async def cancellable(current):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await resume.wait()
                finished.set()

        task = asyncio.create_task(
            runner.run_with_lease(factory, claim, cancellable),
        )
        try:
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            await asyncio.wait_for(cleaning.wait(), 5)
            if repeated:
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done() and not finished.is_set()
            row = await operation(claim)
            assert row.status == "Running"
            assert row.lease_token == claim.lease_token
            assert await jobs.claim_next(factory) is None
            resume.set()
            await expect_error(asyncio.CancelledError, task)
            assert finished.is_set()
        finally:
            resume.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert await jobs.release_claim(factory, claim, retry_after_seconds=0)
        recovered = await jobs.claim_next(factory)
        assert recovered is not None and recovered.lease_token != claim.lease_token
        assert recovered.deadline_at == claim.deadline_at
    print("Single and repeated cancellation finish cleanup before explicit release and recovery")

    claim = await fresh(action="delete", seconds=-1)

    async def cleanup(current):
        await unlocked(current)
        row = await operation(current)
        assert row.deadline_at == claim.deadline_at
        assert row.lease_token == claim.lease_token
        assert await jobs.claim_next(factory) is None
        return "cleanup"

    result = await runner.run_with_lease(
        factory, claim, cleanup, for_cleanup=True,
    )
    assert result == "cleanup"
    row = await operation(claim)
    assert row.status == "Running" and row.deadline_at == claim.deadline_at
    print("Overdue cleanup renews only its live lease; terminal completion remains separate")
    print("PostgreSQL lease lifecycle verification passed. No external services were called.")


if __name__ == "__main__":
    raise SystemExit("Run: uv run python tests/verify_store_job_guard.py")
