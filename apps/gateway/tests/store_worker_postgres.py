"""Concurrent worker checks inside the existing temporary PostgreSQL schema."""

import asyncio
from datetime import timedelta
import re
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import delete, func, select

from app import store_jobs as jobs
from app import store_worker as worker
from app.models import (
    SecurityEvent, Store, StoreOperation, StoreProvisioningSettings,
)
from app.store_completion import finish_failed
from app.store_settings import lock_store_admission


async def verify_worker(factory):
    async def seed(count, limit):
        async with factory() as db:
            async with db.begin():
                schema = await db.scalar(select(func.current_schema()))
                assert re.fullmatch(r"urumi_guard_test_[0-9a-f]{32}", schema)
                await lock_store_admission(db)
                for model in (SecurityEvent, StoreOperation, Store):
                    await db.execute(delete(model))
                settings = await db.get(StoreProvisioningSettings, 1)
                assert settings is not None
                settings.max_total = 4
                settings.max_per_user = 2
                settings.max_concurrent_creations = limit
                now = await db.scalar(select(func.clock_timestamp()))
                stores, operations = [], []
                for index in range(count):
                    identity = uuid4()
                    owner = 98701 + index % 2
                    stores.append(Store(
                        id=identity, owner_id=owner, name="Worker fixture",
                        slug="work-" + identity.hex[:16],
                        namespace="urumi-store-" + identity.hex,
                        release_name="store",
                        url=f"http://work-{identity.hex[:16]}.localtest.me",
                        status="Provisioning", desired_state="present", generation=1,
                    ))
                    operations.append(StoreOperation(
                        id=uuid4(), store_id=identity, requested_by_id=owner,
                        request_key=uuid4(), request_fingerprint="b" * 64,
                        action="provision", store_generation=1,
                        status="Pending", attempts=0,
                        available_at=now - timedelta(seconds=1),
                        deadline_at=now + timedelta(minutes=20),
                    ))
                db.add_all(stores)
                await db.flush()
                db.add_all(operations)
                await db.flush()
                deadlines = {row.id: row.deadline_at for row in operations}
        return deadlines

    async def rows():
        async with factory() as db:
            result = (await db.scalars(select(StoreOperation))).all()
            return {row.id: row for row in result}

    async def set_limit(value):
        async with factory() as db:
            async with db.begin():
                await lock_store_admission(db)
                settings = await db.get(StoreProvisioningSettings, 1)
                settings.max_concurrent_creations = value
                settings.revision += 1

    deadlines = await seed(4, 2)
    stop, resume = asyncio.Event(), asyncio.Event()
    two, three, cleaning = asyncio.Event(), asyncio.Event(), asyncio.Event()
    seen, owners, stopped = {}, {}, set()
    cleaning_ids = set()

    def handler(label):
        async def work(claim, *, for_cleanup):
            key = claim.operation_id
            assert not for_cleanup and key in deadlines
            assert key not in seen
            assert claim.attempt == 1 and claim.deadline_at == deadlines[key]
            seen[key], owners[key] = claim, label
            if len(seen) >= 2:
                two.set()
            if len(seen) >= 3:
                three.set()
            try:
                await asyncio.Future()
            finally:
                cleaning_ids.add(key)
                if len(cleaning_ids) == 3:
                    cleaning.set()
                await resume.wait()
                stopped.add(key)
        return work

    async def unfinished(claim, result, *, for_cleanup):
        raise AssertionError("Cancelled work must not reach finalization.")

    tasks = [
        asyncio.create_task(worker.run_store_worker(
            factory, handler("first"), unfinished,
            stop_event=stop, max_jobs=1, poll_seconds=0.05,
        )),
        asyncio.create_task(worker.run_store_worker(
            factory, handler("second"), unfinished,
            stop_event=stop, max_jobs=2, poll_seconds=0.05,
        )),
    ]
    try:
        await asyncio.wait_for(two.wait(), 10)
        current = await rows()
        assert sum(row.status == "Running" for row in current.values()) == 2
        assert len(seen) == 2
        await set_limit(3)
        await asyncio.wait_for(three.wait(), 10)
        assert len(seen) == 3 and set(owners.values()) == {"first", "second"}
        stop.set()
        await asyncio.wait_for(cleaning.wait(), 10)
        tasks[0].cancel()
        await asyncio.sleep(0)
        tasks[0].cancel()
        await asyncio.sleep(0)
        assert not any(task.done() for task in tasks)
        current = await rows()
        for key, claim in seen.items():
            assert current[key].status == "Running"
            assert current[key].lease_token == claim.lease_token
        assert await jobs.claim_next(factory) is None

        resume.set()
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), 15,
        )
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1] is None
        assert stopped == set(seen)
    finally:
        stop.set()
        resume.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    current = await rows()
    assert len(current) == 4
    for key, row in current.items():
        assert row.status == "Pending"
        assert row.lease_token is None and row.lease_expires_at is None
        assert row.deadline_at == deadlines[key]
        assert row.attempts == (1 if key in seen else 0)
        if key in seen:
            assert row.available_at >= row.updated_at + timedelta(seconds=5)
    print("Two workers share PostgreSQL capacity; an admin increase applies without restart")
    print("Shutdown and repeated cancellation stop callbacks before requeue; deadlines stay fixed")

    deadlines = await seed(2, 2)
    stop, complete = asyncio.Event(), asyncio.Event()
    ended, finalized = set(), set()

    async def empty_work(claim, *, for_cleanup):
        assert not for_cleanup
        ended.add(claim.operation_id)
        return "no resources created"

    async def finish_fixture(claim, result, *, for_cleanup):
        key = claim.operation_id
        assert key in ended and key not in finalized
        assert result == "no resources created" and not for_cleanup
        # Empty fixtures can fail cleanly; no infrastructure was created.
        assert await finish_failed(factory, claim, reason="provisioning")
        finalized.add(key)
        if len(finalized) == 2:
            complete.set()

    task = asyncio.create_task(worker.run_store_worker(
        factory, empty_work, finish_fixture,
        stop_event=stop, max_jobs=2, poll_seconds=0.05,
    ))
    try:
        await asyncio.wait_for(complete.wait(), 10)
        stop.set()
        await asyncio.wait_for(task, 10)
    finally:
        stop.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    current = await rows()
    assert finalized == set(deadlines)
    assert all(
        row.status == "Failed" and row.attempts == 1
        and row.lease_token is None and row.lease_expires_at is None
        and row.deadline_at == deadlines[key]
        for key, row in current.items()
    )
    assert await jobs.claim_next(factory) is None
    async with factory() as db:
        events = (await db.scalars(select(SecurityEvent))).all()
        assert len(events) == 2
        assert {event.action for event in events} == {"store.provision_failed"}
    print("Finalizers commit once; terminal jobs are not revived by worker release")

    deadlines = await seed(1, 1)
    stop = asyncio.Event()
    real_claim = worker.claim_next

    async def stop_after_claim(factory):
        claim = await real_claim(factory)
        if claim is not None:
            stop.set()
        return claim

    async def forbidden(*args, **kwargs):
        raise AssertionError("An unstarted job must perform no work.")

    with patch.object(worker, "claim_next", stop_after_claim):
        await asyncio.wait_for(worker.run_store_worker(
            factory, forbidden, forbidden, stop_event=stop, poll_seconds=0.05,
        ), 10)
    current = await rows()
    assert len(current) == 1
    for key, row in current.items():
        assert row.status == "Pending" and row.attempts == 1
        assert row.lease_token is None and row.lease_expires_at is None
        assert row.deadline_at == deadlines[key]
    print("A committed claim interrupted before dispatch is safely requeued")
    print("PostgreSQL worker verification passed. No external services were called.")


if __name__ == "__main__":
    raise SystemExit("Run: uv run python tests/verify_store_job_guard.py")
