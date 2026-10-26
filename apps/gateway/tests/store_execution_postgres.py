"""Execution checks inside the completion suite's disposable PostgreSQL schema."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import store_execution as execution
from app import store_jobs as jobs
from app.models import AuditLog, MCPServer, SecurityEvent, Store, StoreOperation, StoreVolume, ToolGrant
from app.store_cleanup import StoreCleanupPass
from app.store_completion import StoreCompletionConflict
from app.store_namespace import namespace_for
from app.store_settings import STORE_ADMISSION_LOCK
from app.store_volume_binding import bind_store_volume
from app.store_volumes import STORE_CLAIM_NAMES, StoreVolumeBinding


async def error(kind, operation):
    try:
        await operation
    except kind:
        return
    raise AssertionError("Expected " + str(kind))


async def stop(*tasks):
    for task in tasks:
        if task is not None:
            task.cancel()
    await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)


async def started(task, event):
    waiter = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait(
            (task, waiter), timeout=5, return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            outcome = await task
            raise AssertionError("Completion ended before its pause: " + repr(outcome))
        assert waiter in done, "Completion did not reach its pause."
    finally:
        await stop(waiter)


async def verify_execution(factory, reset, owned, change, snapshot):
    async with factory() as db:
        schema = await db.scalar(select(func.current_schema()))
        assert schema.startswith("urumi_completion_test_") and schema != "public"

    async def fresh(action="delete", *, overdue=False, unbound=False):
        await reset()
        claim, ids = await owned(
            action=action, overdue=overdue, registrations=0 if unbound else 2,
        )
        uid = None if unbound else "ns-" + uuid4().hex
        await change(Store, claim.store_id, namespace_uid=uid)
        values = []
        if uid is not None:
            for name in STORE_CLAIM_NAMES:
                identity = str(uuid4())
                value = StoreVolumeBinding(
                    namespace_for(claim), uid, name, identity,
                    "pvc-" + identity, str(uuid4()),
                )
                assert await bind_store_volume(factory, claim, value, for_cleanup=True) == value
                values.append(value)
        return claim, StoreCleanupPass(claim, uid, True, tuple(values), (), ()), ids

    async def finish(claim, result):
        return await execution.finish_store_cleanup(factory, claim, result)

    async def wait_past(db, instant):
        async with asyncio.timeout(8):
            while await db.scalar(select(func.clock_timestamp())) < instant:
                await asyncio.sleep(0.05)

    claim, result, ids = await fresh()
    before = await snapshot()
    outcomes = await asyncio.gather(finish(claim, result), finish(claim, result))
    assert sorted(outcomes) == [False, True]
    async with factory() as db:
        store = await db.get(Store, claim.store_id)
        operation = await db.get(StoreOperation, claim.operation_id)
        assert store.status == "Deleted" and store.deleted_at is not None
        assert operation.status == "Succeeded" and operation.finished_at is not None
        assert operation.deadline_at == claim.deadline_at and operation.lease_token is None
        assert operation.lease_expires_at is None and operation.last_error is None
        assert store.mcp_server_id is None and store.analytics_mcp_server_id is None
        assert await db.scalar(select(func.count()).select_from(ToolGrant)) == 0
        audits = (await db.scalars(select(AuditLog))).all()
        assert len(audits) == len(ids) and all(item.server_id is None for item in audits)
        assert await db.scalar(select(func.count()).select_from(MCPServer)) == 1
        assert await db.scalar(select(func.count()).select_from(SecurityEvent).where(
            SecurityEvent.action == "store.deleted",
        )) == 1
    after = await snapshot()
    assert after["store_volumes"] == before["store_volumes"]
    assert await finish(claim, result) is False and await snapshot() == after
    print("Cleanup commits once; registrations/grants disappear while storage identities, audits and Notes survive")

    for unbound in (False, True):
        claim, result, _ = await fresh("provision", overdue=True, unbound=unbound)
        assert await finish(claim, result)
        async with factory() as db:
            store = await db.get(Store, claim.store_id)
            operation = await db.get(StoreOperation, claim.operation_id)
            assert store.status == "Failed" and store.deleted_at is None
            assert store.status_reason == execution.FAILURE_REASONS["timeout"]
            assert operation.status == "Failed" and operation.deadline_at == claim.deadline_at
    claim, result, _ = await fresh("provision")
    before = await snapshot()
    assert await finish(claim, result) is False and await snapshot() == before
    print("Overdue cleanup finishes Failed with the original deadline; cleanup cannot fail provisioning early")

    claim, result, _ = await fresh()
    before = await snapshot()
    for pending in (
        replace(result, namespace_absent=False),
        replace(result, remaining_volumes=(result.storage[0],)),
        replace(result, storage=(result.storage[0],), unresolved_claims=("wordpress-data",)),
    ):
        assert await finish(claim, pending) is False
        assert await snapshot() == before
    for changes in (
        {"lease_token": uuid4()}, {"operation_id": uuid4()},
        {"owner_id": claim.owner_id + 1}, {"generation": claim.generation + 1},
        {"attempt": claim.attempt + 1}, {"deadline_at": claim.deadline_at + timedelta(seconds=1)},
    ):
        bad = replace(result, claim=replace(claim, **changes))
        await error(StoreCompletionConflict, finish(claim, bad))
    for changes in (
        {"namespace_uid": "wrong"}, {"namespace_absent": 1},
        {"storage": []}, {"remaining_volumes": []}, {"unresolved_claims": []},
        {"unresolved_claims": ("wordpress-data",)},
    ):
        await error(StoreCompletionConflict, finish(claim, replace(result, **changes)))
    assert await snapshot() == before
    for mode in ("missing", "partial", "changed"):
        claim, result, _ = await fresh()
        async with factory.begin() as db:
            condition = (StoreVolume.store_id == claim.store_id) & (
                StoreVolume.claim_name == STORE_CLAIM_NAMES[0]
            )
            if mode == "missing":
                await db.execute(delete(StoreVolume).where(condition))
            else:
                await db.execute(update(StoreVolume).where(condition).values(
                    volume_uid=None if mode == "partial" else "changed-" + uuid4().hex,
                ))
        before = await snapshot()
        await error(StoreCompletionConflict, finish(claim, result))
        assert await snapshot() == before
    print("Pending, forged and changed storage evidence cannot remove registrations or finish a job")

    claim, result, _ = await fresh()
    await change(StoreOperation, claim.operation_id, lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
    before = await snapshot()
    assert await finish(claim, result) is False and await snapshot() == before
    recovered = await jobs.claim_next(factory)
    assert recovered is not None and recovered.lease_token != claim.lease_token
    before = await snapshot()
    await error(StoreCompletionConflict, finish(recovered, result))
    assert await snapshot() == before
    print("Lease recovery rejects old cleanup results even when the store and storage are unchanged")

    normal_scalars = AsyncSession.scalars
    claim, result, _ = await fresh()
    async with factory.begin() as db:
        cutoff = await db.scalar(update(StoreOperation).where(
            StoreOperation.id == claim.operation_id,
        ).values(lease_expires_at=func.clock_timestamp() + timedelta(seconds=2))
            .returning(StoreOperation.lease_expires_at))
    claim = replace(claim, lease_expires_at=cutoff)
    result = replace(result, claim=claim)
    before = await snapshot()
    entered = asyncio.Event()
    task = None

    async def observe_rows(db, statement, *args, **kwargs):
        if asyncio.current_task() is task and any(
            item.get("entity") is StoreVolume
            for item in getattr(statement, "column_descriptions", ())
        ):
            assert statement._for_update_arg is not None
            entered.set()
        return await normal_scalars(db, statement, *args, **kwargs)

    try:
        with patch.object(AsyncSession, "scalars", observe_rows):
            async with factory.begin() as holder:
                await holder.execute(select(StoreVolume).where(
                    StoreVolume.store_id == claim.store_id,
                ).with_for_update())
                task = asyncio.create_task(finish(claim, result))
                await started(task, entered)
                await wait_past(holder, cutoff)
                assert not task.done()
            assert await asyncio.wait_for(task, 5) is False
        assert await snapshot() == before
    finally:
        await stop(task)
    print("Real volume-row waits cannot revive an expired cleanup lease")

    normal_flush = AsyncSession.flush
    for mode in ("expiry", "failure", "cancel", "success"):
        claim, result, _ = await fresh()
        if mode == "expiry":
            async with factory.begin() as db:
                cutoff = await db.scalar(update(StoreOperation).where(
                    StoreOperation.id == claim.operation_id,
                ).values(lease_expires_at=func.clock_timestamp() + timedelta(seconds=2))
                    .returning(StoreOperation.lease_expires_at))
            claim = replace(claim, lease_expires_at=cutoff)
            result = replace(result, claim=claim)
        before = await snapshot()
        paused, resume = asyncio.Event(), asyncio.Event()
        task = None

        async def hold_flush(db, *args, **kwargs):
            await normal_flush(db, *args, **kwargs)
            if asyncio.current_task() is task and any(
                isinstance(item, StoreOperation) and item.id == claim.operation_id
                and item.status == "Succeeded" for item in db.identity_map.values()
            ):
                paused.set()
                await asyncio.wait_for(resume.wait(), 10)
                if mode == "failure":
                    raise SQLAlchemyError("Private fixture rollback failure")

        try:
            with patch.object(AsyncSession, "flush", hold_flush):
                task = asyncio.create_task(finish(claim, result))
                await started(task, paused)
                assert await snapshot() == before
                async with factory.begin() as probe:
                    assert await probe.scalar(
                        select(func.pg_try_advisory_xact_lock(STORE_ADMISSION_LOCK))
                    ) is False
                if mode == "cancel":
                    task.cancel()
                    await error(asyncio.CancelledError, task)
                else:
                    if mode == "expiry":
                        async with factory() as observer:
                            await wait_past(observer, cutoff)
                    resume.set()
                    if mode == "failure":
                        await error(SQLAlchemyError, task)
                    else:
                        assert await asyncio.wait_for(task, 5) is (mode == "success")
        finally:
            resume.set()
            await stop(task)
        if mode != "success":
            assert await snapshot() == before
    print("Post-flush expiry/errors/cancellation roll back; completion holds the admission gate through commit")
    await verify_routing()
    print("Execution PostgreSQL checks passed. External cleanup was not called.")


async def verify_routing():
    import ssl
    import httpx
    from app.store_cluster_scope import StoreClusterScope
    from app.store_kube_connection import KubernetesConnection
    from verify_store_volume_api import CLAIM

    def forbidden():
        raise AssertionError("Routing must not open a database.")
    async def no_http(request):
        raise AssertionError("Routing must not send HTTP.")
    raw = KubernetesConnection(
        lambda: "fixture.unused.signature", ssl.create_default_context(),
        transport=httpx.MockTransport(no_http),
    )
    scope = StoreClusterScope("platform", "platform")
    try:
        handlers = execution.StoreWorkerHandlers(
            forbidden, raw, scope, admin_email="owner@example.test",
        )
        ready = execution.StoreProvisioningResult(CLAIM, "ns", "secret", None, ())
        cleanup = StoreCleanupPass(CLAIM, None, True, (), (), ())
        with patch.object(execution, "provision_store", AsyncMock(return_value=ready)) as provision, \
             patch.object(execution, "finish_provisioning", AsyncMock(return_value=True)) as finish_ready:
            assert await handlers.work(CLAIM) is ready
            provision.assert_awaited_once_with(
                forbidden, CLAIM, raw, scope, admin_email="owner@example.test",
            )
            assert await handlers.finish(CLAIM, ready)
            finish_ready.assert_awaited_once_with(forbidden, CLAIM, ready)
            await error(ValueError, handlers.finish(CLAIM, cleanup))
        deleting = replace(CLAIM, action="delete")
        with patch.object(execution, "require_claim", AsyncMock(return_value=SimpleNamespace(claim=deleting))), \
             patch.object(execution, "cleanup_store_resources", AsyncMock(return_value=cleanup)) as clean, \
             patch.object(execution, "finish_store_cleanup", AsyncMock(return_value=False)) as finish_clean:
            assert await handlers.work(deleting, for_cleanup=True) is cleanup
            assert await handlers.finish(deleting, cleanup, for_cleanup=True) is False
            clean.assert_awaited_once_with(forbidden, deleting, raw, scope)
            finish_clean.assert_awaited_once_with(forbidden, deleting, cleanup)
        with patch.object(execution, "require_claim", AsyncMock(return_value=SimpleNamespace(claim=CLAIM))), \
             patch.object(execution, "cleanup_store_resources", AsyncMock()) as clean:
            await error(ValueError, handlers.work(CLAIM, for_cleanup=True))
            clean.assert_not_awaited()
        await error(ValueError, handlers.work(deleting))
        for email in ("", "bad", "owner@example.test\n", "x" * 255):
            try:
                execution.StoreWorkerHandlers(forbidden, raw, scope, admin_email=email)
            except ValueError:
                pass
            else:
                raise AssertionError("Invalid email accepted.")
    finally:
        await raw.aclose()
    print("Concrete handlers route provisioning, deletion and pending completion without starting a worker")
