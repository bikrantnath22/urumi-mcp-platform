"""PVC identity recording in the volume suite's isolated PostgreSQL schema."""

import asyncio
from dataclasses import replace
from datetime import timedelta
import re
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import store_jobs as jobs
from app import store_volume_binding as module
from app.models import SecurityEvent, Store, StoreOperation, StoreVolume
from app.store_claims import StoreClaimIdentity
from app.store_settings import STORE_ADMISSION_LOCK
from app.store_volumes import StoreVolumeError


async def error(kind, operation):
    try:
        await operation
    except kind as caught:
        assert "PRIVATE" not in str(caught)
        return
    raise AssertionError("Expected " + kind.__name__)


async def stop(*tasks):
    tasks = [task for task in tasks if task is not None]
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def started(task, event):
    signal = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait(
            (task, signal), timeout=5, return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            outcome = await task
            raise AssertionError("Recording finished before the test pause: " + repr(outcome))
        assert signal in done, "The recording task did not reach the test pause."
    finally:
        await stop(signal)


async def verify_claim_recording(factory, reset, fresh, proof, change, snapshot, wait_past):
    async with factory() as db:
        schema = await db.scalar(select(func.current_schema()))
        assert re.fullmatch(r"urumi_volume_bind_test_[0-9a-f]{32}", schema)

    async def observation(job, name="wordpress-data"):
        bound = await proof(job, name)
        return StoreClaimIdentity(
            bound.identity, bound.namespace_uid, name, bound.claim_uid,
        ), bound

    def record(job, value, **options):
        return module.record_store_claim(factory, job, value, **options)

    async def shorten(job, field):
        async with factory.begin() as db:
            until = await db.scalar(
                update(StoreOperation).where(StoreOperation.id == job.operation_id)
                .values({field: func.clock_timestamp() + timedelta(seconds=3)})
                .returning(getattr(StoreOperation, field))
            )
        return replace(job, **{field: until})

    await reset()
    job = await fresh()
    pending, bound = await observation(job)
    before = await snapshot()
    tasks = [asyncio.create_task(record(job, pending)) for _ in range(8)]
    try:
        assert await asyncio.wait_for(asyncio.gather(*tasks), 25) == [pending] * 8
    finally:
        await stop(*tasks)
    saved = await snapshot()
    assert len(saved["store_volumes"]) == len(saved["security_events"]) == 1
    assert saved["store_operations"] == before["store_operations"]
    async with factory() as db:
        row = await db.get(StoreVolume, (job.store_id, pending.claim_name))
        assert row.volume_name is None and row.volume_uid is None
        event = (await db.scalars(select(SecurityEvent))).one()
        assert event.action == "store.volume_claim_recorded"
        assert event.actor_id is None and event.target_user_id == job.owner_id
    assert await record(job, pending) == pending and await snapshot() == saved
    mysql, _ = await observation(job, "data-mysql-0")
    assert await record(job, mysql) == mysql
    print("Eight concurrent saves record one Pending claim once; both fixed PVCs keep separate identities")

    named = replace(pending, volume_name=bound.volume_name)
    assert await record(job, named) == named
    assert await module.bind_store_volume(factory, job, bound) == bound
    saved = await snapshot()
    assert await record(job, pending) == named and await snapshot() == saved
    async with factory() as db:
        row = await db.get(StoreVolume, (job.store_id, pending.claim_name))
        assert row.volume_uid == bound.volume_uid
    print("A first PV name can be added; complete bindings survive less-specific retries without extra audits")

    await change(
        StoreOperation, job.operation_id,
        lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
    )
    recovered = await jobs.claim_next(factory)
    assert recovered and recovered.lease_token != job.lease_token
    assert recovered.deadline_at == job.deadline_at
    saved = await snapshot()
    for invalid in (
        job, replace(recovered, lease_token=uuid4()),
        replace(recovered, attempt=recovered.attempt + 1),
        replace(recovered, generation=recovered.generation + 1),
    ):
        assert await record(invalid, pending) is None
    assert await record(recovered, pending) == named
    for values in (
        {"claim_uid": "replacement"}, {"namespace_uid": "replacement"},
        {"volume_name": "replacement"},
        {"identity": replace(pending.identity, owner_id=98802)},
    ):
        await error(StoreVolumeError, record(recovered, replace(pending, **values)))
    assert await snapshot() == saved
    print("Recovery preserves the original deadline; old claims and changed storage ownership cannot overwrite records")

    await reset()
    first, second = await fresh(), await fresh(98802)
    left, full = await observation(first)
    right, _ = await observation(second)
    assert await record(first, left) == left
    saved = await snapshot()
    await error(StoreVolumeError, record(second, replace(right, claim_uid=left.claim_uid)))
    assert await snapshot() == saved
    left = replace(left, volume_name=full.volume_name)
    assert await record(first, left) == left
    saved = await snapshot()
    await error(StoreVolumeError, record(second, replace(right, volume_name=left.volume_name)))
    assert await snapshot() == saved
    await change(Store, first.store_id, status="Deleted", desired_state="absent",
                 deleted_at=func.clock_timestamp())
    saved = await snapshot()
    await error(StoreVolumeError, record(second, replace(right, claim_uid=left.claim_uid)))
    assert await snapshot() == saved
    print("Real uniqueness constraints prevent claim/PV-name adoption, including identities retained after deletion")

    await reset()
    job = await fresh(seconds=-1)
    pending, _ = await observation(job)
    saved = await snapshot()
    assert await record(job, pending) is None and await snapshot() == saved
    assert await record(job, pending, for_cleanup=True) == pending
    await change(StoreOperation, job.operation_id,
                 lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
    saved = await snapshot()
    assert await record(job, pending, for_cleanup=True) is None
    assert await snapshot() == saved
    await reset()
    job = await fresh()
    pending, _ = await observation(job)
    await change(Store, job.store_id, status="Deleting", desired_state="absent")
    await change(StoreOperation, job.operation_id, action="delete")
    deletion = replace(job, action="delete")
    await error(ValueError, record(deletion, pending))
    assert await record(deletion, pending, for_cleanup=True) == pending
    print("Overdue/deletion recording requires cleanup mode and a live lease; terminal states are not changed")

    normal_flush = AsyncSession.flush
    for mode in ("failure", "cancel", "lease_expires_at", "deadline_at"):
        await reset()
        job = await fresh()
        pending, _ = await observation(job)
        if mode in ("lease_expires_at", "deadline_at"):
            job = await shorten(job, mode)
        before = await snapshot()

        async def interrupted_flush(db, *args, **kwargs):
            await normal_flush(db, *args, **kwargs)
            if mode == "failure":
                raise SQLAlchemyError("PRIVATE FLUSH FAILURE")
            if mode == "cancel":
                raise asyncio.CancelledError()
            await wait_past(db, getattr(job, mode))

        with patch.object(AsyncSession, "flush", interrupted_flush):
            if mode == "failure":
                await error(module.VolumeBindingUnavailable, record(job, pending))
            elif mode == "cancel":
                await error(asyncio.CancelledError, record(job, pending))
            else:
                assert await record(job, pending) is None
        assert await snapshot() == before
    print("Post-flush storage failures, cancellation and expiry roll back the claim, parent timestamp and audit together")

    normal_get = AsyncSession.get
    for field in ("lease_expires_at", "deadline_at"):
        await reset()
        job = await fresh()
        pending, bound = await observation(job)
        assert await record(job, pending) == pending
        named = replace(pending, volume_name=bound.volume_name)
        task, entered = None, asyncio.Event()

        async def observe_get(db, model, key, *args, **kwargs):
            if asyncio.current_task() is task and model is StoreVolume:
                assert kwargs.get("with_for_update") is True
                entered.set()
            return await normal_get(db, model, key, *args, **kwargs)

        try:
            with patch.object(AsyncSession, "get", observe_get):
                async with factory.begin() as holder:
                    await holder.execute(
                        select(StoreVolume).where(
                            StoreVolume.store_id == job.store_id,
                        ).with_for_update()
                    )
                    job = await shorten(job, field)
                    before = await snapshot()
                    task = asyncio.create_task(record(job, named))
                    await started(task, entered)
                    await wait_past(holder, getattr(job, field))
                    assert not task.done()
                assert await asyncio.wait_for(task, 5) is None
                assert await snapshot() == before
        finally:
            await stop(task)
    print("Real PVC-record row waits cannot revive expired worker leases or provisioning deadlines")

    for cancel in (False, True):
        await reset()
        job = await fresh()
        pending, _ = await observation(job)
        before = await snapshot()
        task = competitor = None
        paused, resume = asyncio.Event(), asyncio.Event()

        async def held_flush(db, *args, **kwargs):
            await normal_flush(db, *args, **kwargs)
            if asyncio.current_task() is task:
                paused.set()
                await asyncio.wait_for(resume.wait(), 10)

        try:
            with patch.object(AsyncSession, "flush", held_flush):
                task = asyncio.create_task(record(job, pending))
                await started(task, paused)
                assert await snapshot() == before
                async with factory.begin() as probe:
                    assert await probe.scalar(select(
                        func.pg_try_advisory_xact_lock(STORE_ADMISSION_LOCK)
                    )) is False
                if cancel:
                    task.cancel()
                    await error(asyncio.CancelledError, task)
                else:
                    competitor = asyncio.create_task(record(job, pending))
                    resume.set()
                    assert await asyncio.wait_for(
                        asyncio.gather(task, competitor), 15,
                    ) == [pending, pending]
        finally:
            resume.set()
            await stop(task, competitor)
        if cancel:
            assert await snapshot() == before
            assert await record(job, pending) == pending
        assert len((await snapshot())["security_events"]) == 1
    print("Uncommitted records stay hidden; the scheduling gate lasts through commit and cancellation permits a clean retry")
    print("PVC identity recording verification passed. Live stores/settings and external services were unchanged.")
