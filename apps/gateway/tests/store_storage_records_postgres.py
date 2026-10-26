"""Saved storage inventory checks in the existing isolated PostgreSQL suite."""

import asyncio
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
import re
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction

from app import store_jobs as jobs
from app import store_storage_records as module
from app.models import Store, StoreOperation, StoreVolume
from app.store_claims import StoreClaimIdentity
from app.store_job_guard import StoreJobClaimLost
from app.store_scheduling import StoreSchedulingUnavailable
from app.store_settings import STORE_ADMISSION_LOCK
from app.store_volume_binding import (
    VolumeBindingUnavailable, bind_store_volume, record_store_claim,
)
from app.store_volumes import StoreVolumeError
from store_claim_recording_postgres import error, started, stop


def _inventory_query(statement):
    return (
        getattr(statement, "_for_update_arg", None) is not None
        and any(
            item.get("entity") is StoreVolume
            for item in getattr(statement, "column_descriptions", ())
        )
    )


async def verify_storage_records(factory, reset, fresh, proof, change, snapshot, wait_past):
    async with factory() as db:
        schema = await db.scalar(select(func.current_schema()))
        assert re.fullmatch(r"urumi_volume_bind_test_[0-9a-f]{32}", schema)

    async def fixture():
        job = await fresh()
        full = await proof(job)
        pending = StoreClaimIdentity(
            full.identity, full.namespace_uid, full.claim_name, full.claim_uid,
        )
        return job, pending, full

    def read(job, uid, **options):
        return module.load_store_storage(factory, job, namespace_uid=uid, **options)

    async def locks_free(job):
        async with factory.begin() as db:
            assert await db.scalar(select(
                func.pg_try_advisory_xact_lock(STORE_ADMISSION_LOCK)
            )) is True
            await db.execute(
                select(StoreVolume).where(StoreVolume.store_id == job.store_id)
                .with_for_update(nowait=True)
            )

    await reset()
    job, pending, full = await fixture()
    before = await snapshot()
    assert await read(job, pending.namespace_uid) == ()
    assert await snapshot() == before
    assert await record_store_claim(factory, job, pending) == pending
    mysql = await proof(job, "data-mysql-0")
    assert await bind_store_volume(factory, job, mysql) == mysql
    before = await snapshot()
    result = await read(job, pending.namespace_uid)
    assert result == (mysql, pending)
    assert await snapshot() == before
    try:
        result[1].claim_uid = "changed"
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("The returned identity is mutable.")
    await locks_free(job)
    assert await bind_store_volume(factory, job, full) == full
    before = await snapshot()
    assert await read(job, pending.namespace_uid) == (mysql, full)
    assert result[1].volume_name is None and await snapshot() == before
    print("Empty, partial and full inventories return immutable copies; reads change no rows and release locks")

    await reset()
    first, left, _ = await fixture()
    second, right, _ = await fixture()
    assert await record_store_claim(factory, first, left) == left
    assert await record_store_claim(factory, second, right) == right
    before = await snapshot()
    assert await read(first, left.namespace_uid) == (left,)
    await error(StoreVolumeError, read(first, right.namespace_uid))
    for invalid in (
        replace(first, lease_token=uuid4()), replace(first, operation_id=uuid4()),
        replace(first, attempt=first.attempt + 1),
        replace(first, generation=first.generation + 1),
    ):
        await error(StoreJobClaimLost, read(invalid, left.namespace_uid))
    assert await snapshot() == before
    await change(
        StoreOperation, first.operation_id,
        lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
    )
    recovered = await jobs.claim_next(factory)
    assert recovered and recovered.store_id == first.store_id
    assert recovered.lease_token != first.lease_token
    assert recovered.deadline_at == first.deadline_at
    before = await snapshot()
    await error(StoreJobClaimLost, read(first, left.namespace_uid))
    assert await read(recovered, left.namespace_uid) == (left,)
    assert await snapshot() == before
    print("Stores remain separate; forged and expired claims cannot read inventories after lease recovery")

    for changes in (
        {"owner_id": 98802}, {"name": "Changed"}, {"slug": "changed"},
        {"url": "http://changed.localtest.me"}, {"release_name": "other"},
    ):
        await reset()
        job, pending, _ = await fixture()
        await record_store_claim(factory, job, pending)
        await change(Store, job.store_id, **changes)
        before = await snapshot()
        await error(StoreVolumeError, read(job, pending.namespace_uid))
        assert await snapshot() == before
    for values in (
        {"claim_uid": "bad uid"}, {"volume_name": "../unsafe"},
        {"volume_uid": "bad uid"},
    ):
        await reset()
        job, pending, full = await fixture()
        await bind_store_volume(factory, job, full)
        async with factory.begin() as db:
            await db.execute(
                update(StoreVolume).where(StoreVolume.store_id == job.store_id)
                .values(**values)
            )
        before = await snapshot()
        await error(StoreVolumeError, read(job, pending.namespace_uid))
        assert await snapshot() == before
    print("Changed store ownership and malformed stored identifiers reject the whole snapshot without repair")

    await reset()
    job = await fresh(seconds=-1)
    full = await proof(job)
    pending = StoreClaimIdentity(
        full.identity, full.namespace_uid, full.claim_name, full.claim_uid,
    )
    await record_store_claim(factory, job, pending, for_cleanup=True)
    before = await snapshot()
    await error(StoreJobClaimLost, read(job, pending.namespace_uid))
    assert await read(job, pending.namespace_uid, for_cleanup=True) == (pending,)
    assert await snapshot() == before
    await change(Store, job.store_id, status="Deleting", desired_state="absent")
    await change(StoreOperation, job.operation_id, action="delete")
    job = replace(job, action="delete")
    before = await snapshot()
    await error(ValueError, read(job, pending.namespace_uid))
    assert await read(job, pending.namespace_uid, for_cleanup=True) == (pending,)
    assert await snapshot() == before
    await change(
        StoreOperation, job.operation_id,
        lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
    )
    before = await snapshot()
    await error(StoreJobClaimLost, read(job, pending.namespace_uid, for_cleanup=True))
    assert await snapshot() == before
    print("Overdue/deletion inventory reads require explicit cleanup mode and a live lease; deadlines stay fixed")

    normal_scalars = AsyncSession.scalars
    for field in ("lease_expires_at", "deadline_at"):
        await reset()
        job, pending, _ = await fixture()
        await record_store_claim(factory, job, pending)
        task, entered = None, asyncio.Event()

        async def observe(db, statement, *args, **kwargs):
            if asyncio.current_task() is task and _inventory_query(statement):
                entered.set()
            return await normal_scalars(db, statement, *args, **kwargs)

        try:
            with patch.object(AsyncSession, "scalars", observe):
                async with factory.begin() as holder:
                    await holder.execute(
                        select(StoreVolume).where(StoreVolume.store_id == job.store_id)
                        .with_for_update()
                    )
                    async with factory.begin() as db:
                        cutoff = await db.scalar(
                            update(StoreOperation).where(StoreOperation.id == job.operation_id)
                            .values({field: func.clock_timestamp() + timedelta(seconds=3)})
                            .returning(getattr(StoreOperation, field))
                        )
                    job = replace(job, **{field: cutoff})
                    before = await snapshot()
                    task = asyncio.create_task(read(job, pending.namespace_uid))
                    await started(task, entered)
                    await wait_past(holder, cutoff)
                    assert not task.done()
                await error(StoreJobClaimLost, asyncio.wait_for(task, 5))
                assert await snapshot() == before
        finally:
            await stop(task)
        await locks_free(job)
    print("Real storage-row lock waits cannot revive expired leases or original provisioning deadlines")

    for cancel in (False, True):
        await reset()
        job, pending, _ = await fixture()
        await record_store_claim(factory, job, pending)
        before = await snapshot()
        task = None
        paused, resume = asyncio.Event(), asyncio.Event()

        async def hold_rows(db, statement, *args, **kwargs):
            result = await normal_scalars(db, statement, *args, **kwargs)
            if asyncio.current_task() is task and _inventory_query(statement):
                paused.set()
                await asyncio.wait_for(resume.wait(), 10)
            return result

        try:
            with patch.object(AsyncSession, "scalars", hold_rows):
                task = asyncio.create_task(read(job, pending.namespace_uid))
                await started(task, paused)
                async with factory.begin() as probe:
                    assert await probe.scalar(select(
                        func.pg_try_advisory_xact_lock(STORE_ADMISSION_LOCK)
                    )) is False
                assert await snapshot() == before
                if cancel:
                    task.cancel()
                    await error(asyncio.CancelledError, task)
                else:
                    resume.set()
                    assert await asyncio.wait_for(task, 5) == (pending,)
        finally:
            resume.set()
            await stop(task)
        assert await snapshot() == before
        await locks_free(job)
    print("The admission gate remains held during reads; success and cancellation release rows and sessions")

    await reset()
    job, pending, _ = await fixture()
    await record_store_claim(factory, job, pending)
    before = await snapshot()
    for failure in (SQLAlchemyError("PRIVATE"), StoreSchedulingUnavailable("PRIVATE")):
        async def unavailable(db, statement, *args, **kwargs):
            if _inventory_query(statement):
                raise failure
            return await normal_scalars(db, statement, *args, **kwargs)
        with patch.object(AsyncSession, "scalars", unavailable):
            await error(VolumeBindingUnavailable, read(job, pending.namespace_uid))
        assert await snapshot() == before
        await locks_free(job)

    local_time = [100.0]
    normal_exit = AsyncSessionTransaction.__aexit__
    async def elapsed_after_commit(transaction, *args):
        result = await normal_exit(transaction, *args)
        local_time[0] += 120
        return result
    with patch.object(module, "time", SimpleNamespace(monotonic=lambda: local_time[0])), patch.object(
        AsyncSessionTransaction, "__aexit__", elapsed_after_commit,
    ):
        await error(StoreJobClaimLost, read(job, pending.namespace_uid))
    assert await snapshot() == before
    await locks_free(job)
    print("Storage failures and elapsed post-commit allowance return safe errors without changing saved data")
    print("Saved storage PostgreSQL verification passed. No Kubernetes or external services were called.")
