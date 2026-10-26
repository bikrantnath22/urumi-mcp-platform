"""Verified Ready checks inside the completion suite's temporary PostgreSQL schema."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import store_completion as completion
from app import store_jobs as jobs
from app.models import MCPServer, SecurityEvent, Store, StoreOperation, StoreVolume, ToolGrant
from app.store_provisioning import StoreProvisioningResult, finish_provisioning
from app.store_registration import register_store_mcps
from app.store_settings import STORE_ADMISSION_LOCK
from app.store_namespace import namespace_for
from app.store_volume_binding import bind_store_volume
from app.store_volumes import STORE_CLAIM_NAMES, StoreVolumeBinding


async def error(kind, operation):
    try:
        await operation
    except kind:
        return
    raise AssertionError("Expected " + kind.__name__)


async def verify_ready_results(factory, reset, owned, change, snapshot):
    async with factory() as db:
        schema = await db.scalar(select(func.current_schema()))
        assert schema.startswith("urumi_completion_test_") and schema != "public"

    async def fresh():
        await reset()
        claim, _ = await owned(registrations=0)
        namespace_uid, secret_uid = "ns-" + uuid4().hex, "secret-" + uuid4().hex
        await change(
            Store, claim.store_id,
            namespace_uid=namespace_uid, credentials_secret_uid=secret_uid,
        )
        registrations = await register_store_mcps(factory, claim)
        assert registrations is not None
        volumes = []
        for name in STORE_CLAIM_NAMES:
            uid = str(uuid4())
            observed = StoreVolumeBinding(
                namespace_for(claim), namespace_uid, name,
                uid, "pvc-" + uid, str(uuid4()),
            )
            assert await bind_store_volume(factory, claim, observed) == observed
            volumes.append(observed)
        return claim, StoreProvisioningResult(
            claim, namespace_uid, secret_uid, registrations, tuple(volumes), True,
        )

    async def finish(claim, result):
        return await finish_provisioning(factory, claim, result)

    async def shorten(claim, result, field):
        async with factory.begin() as db:
            until = await db.scalar(
                update(StoreOperation).where(StoreOperation.id == claim.operation_id)
                .values({field: func.clock_timestamp() + timedelta(seconds=2)})
                .returning(getattr(StoreOperation, field))
            )
        changed = replace(claim, **{field: until})
        return changed, replace(result, claim=changed)

    async def wait_past(db, instant):
        async with asyncio.timeout(8):
            while await db.scalar(select(func.clock_timestamp())) < instant:
                await asyncio.sleep(0.05)

    claim, result = await fresh()
    volumes_before = (await snapshot())["store_volumes"]
    renewed = await jobs.renew_claim(factory, claim)
    assert renewed is not None
    outcomes = await asyncio.gather(finish(renewed, result), finish(renewed, result))
    assert sorted(outcomes) == [False, True]
    async with factory() as db:
        store = await db.get(Store, claim.store_id)
        operation = await db.get(StoreOperation, claim.operation_id)
        assert store.status == "Ready" and operation.status == "Succeeded"
        assert operation.deadline_at == claim.deadline_at
        assert operation.lease_token is None and operation.lease_expires_at is None
        assert operation.finished_at is not None
        ids = (result.registrations.woo_server_id, result.registrations.analytics_server_id)
        servers = (await db.scalars(select(MCPServer).where(MCPServer.id.in_(ids)))).all()
        assert len(servers) == 2 and all(
            item.enabled and item.status == "Connected" and item.last_checked_at
            for item in servers
        )
        assert await db.scalar(select(func.count()).select_from(ToolGrant)) == 0
        assert await db.scalar(select(func.count()).select_from(MCPServer)) == 3
        events = (await db.scalars(select(SecurityEvent))).all()
        assert sorted(item.action for item in events) == [
            "store.mcp_registered", "store.provisioned",
            "store.volume_bound", "store.volume_bound",
        ]
    before = await snapshot()
    assert before["store_volumes"] == volumes_before
    assert await finish(claim, result) is False
    assert await snapshot() == before
    print("Verified Ready commits once under concurrent completion; renewals work and no member grants are added")

    claim, result = await fresh()
    before = await snapshot()
    await error(ValueError, finish(claim, None))
    for changes in (
        {"lease_token": uuid4()}, {"operation_id": uuid4()}, {"store_id": uuid4()},
        {"owner_id": claim.owner_id + 1}, {"generation": claim.generation + 1},
        {"attempt": claim.attempt + 1}, {"generation": True},
        {"deadline_at": claim.deadline_at + timedelta(seconds=1)},
    ):
        bad = replace(result, claim=replace(claim, **changes))
        await error(completion.StoreCompletionConflict, finish(claim, bad))
    for changes in (
        {"namespace_uid": None}, {"credentials_secret_uid": None},
        {"namespace_uid": "other-namespace"}, {"credentials_secret_uid": "other-secret"},
        {"registrations": None}, {"access_guarded": False}, {"access_guarded": 1},
    ):
        await error(completion.StoreCompletionConflict, finish(claim, replace(result, **changes)))
    for changes in (
        {"woo_server_id": True},
        {"analytics_server_id": result.registrations.woo_server_id},
        {"woo_slug": "other-store"}, {"analytics_url": "http://other-store/mcp"},
    ):
        bad = replace(result, registrations=replace(result.registrations, **changes))
        await error(completion.StoreCompletionConflict, finish(claim, bad))
    assert await snapshot() == before
    print("Forged jobs, malformed results, wrong UIDs and cross-store registration snapshots change nothing")

    # The volume foreign key separately prevents namespace replacement.
    for target, values in (
        ("store", {"credentials_secret_uid": "replacement-" + uuid4().hex}),
        ("store", {"name": "Changed fixture store"}),
        ("store", {"access_enabled": False}),
        ("store", {"analytics_mcp_server_id": None}),
        ("server", {"slug": "changed-" + uuid4().hex[:16]}),
        ("server", {"enabled": True}),
        ("server", {"credential_encrypted": "private-fixture-not-a-real-credential"}),
        ("operation", {"deadline_at": func.clock_timestamp() + timedelta(seconds=1500)}),
    ):
        claim, result = await fresh()
        model, identity = {
            "store": (Store, claim.store_id),
            "server": (MCPServer, result.registrations.woo_server_id),
            "operation": (StoreOperation, claim.operation_id),
        }[target]
        await change(model, identity, **values)
        before = await snapshot()
        await error(completion.StoreCompletionConflict, finish(claim, result))
        assert await snapshot() == before
    claim, result = await fresh()
    await change(
        StoreOperation, claim.operation_id,
        lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
    )
    before = await snapshot()
    assert await finish(claim, result) is False and await snapshot() == before
    recovered = await jobs.claim_next(factory)
    assert recovered is not None and recovered.lease_token != claim.lease_token
    before = await snapshot()
    await error(completion.StoreCompletionConflict, finish(recovered, result))
    assert await finish(claim, result) is False and await snapshot() == before
    print("Changed stored identities and credentials reject Ready; lease recovery cannot reuse an old result")

    normal_execute = AsyncSession.execute
    for field in ("lease_expires_at", "deadline_at"):
        claim, result = await fresh()
        waiting = asyncio.Event()
        task = None

        async def observed_execute(db, statement, *args, **kwargs):
            if (
                asyncio.current_task() is task
                and getattr(statement, "_for_update_arg", None) is not None
                and any(table is MCPServer.__table__ for table in statement.get_final_froms())
            ):
                waiting.set()
            return await normal_execute(db, statement, *args, **kwargs)

        try:
            async with factory.begin() as holder:
                await holder.execute(select(MCPServer).where(
                    MCPServer.id == result.registrations.woo_server_id,
                ).with_for_update())
                claim, result = await shorten(claim, result, field)
                before = await snapshot()
                with patch.object(AsyncSession, "execute", observed_execute):
                    task = asyncio.create_task(finish(claim, result))
                    await asyncio.wait_for(waiting.wait(), 5)
                    await wait_past(holder, getattr(claim, field))
                    assert not task.done()
            assert await asyncio.wait_for(task, 5) is False
            assert await snapshot() == before
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    print("Real registration-row waits cannot revive expired verified leases or provisioning deadlines")

    normal_flush = AsyncSession.flush
    for mode in ("lease_expires_at", "deadline_at", "failure", "cancel", "success"):
        claim, result = await fresh()
        if mode in ("lease_expires_at", "deadline_at"):
            claim, result = await shorten(claim, result, mode)
        before = await snapshot()
        paused, resume = asyncio.Event(), asyncio.Event()
        task = None

        async def held_flush(db, *args, **kwargs):
            await normal_flush(db, *args, **kwargs)
            if asyncio.current_task() is task and any(
                isinstance(item, StoreOperation) and item.id == claim.operation_id
                and item.status == "Succeeded" for item in db.identity_map.values()
            ):
                paused.set()
                await asyncio.wait_for(resume.wait(), 10)
                if mode == "failure":
                    raise SQLAlchemyError("private fixture failure after flush")

        try:
            with patch.object(AsyncSession, "flush", held_flush):
                task = asyncio.create_task(finish(claim, result))
                await asyncio.wait_for(paused.wait(), 5)
                assert await snapshot() == before
                async with factory.begin() as probe:
                    assert await probe.scalar(
                        select(func.pg_try_advisory_xact_lock(STORE_ADMISSION_LOCK))
                    ) is False
                if mode == "cancel":
                    task.cancel()
                    await error(asyncio.CancelledError, task)
                else:
                    if mode in ("lease_expires_at", "deadline_at"):
                        async with factory() as observer:
                            await wait_past(observer, getattr(claim, mode))
                    resume.set()
                    if mode == "failure":
                        await error(SQLAlchemyError, task)
                    else:
                        assert await asyncio.wait_for(task, 5) is (mode == "success")
        finally:
            resume.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if mode != "success":
            assert await snapshot() == before
    print("Post-flush expiry, storage errors and cancellation roll back all writes; the scheduling gate lasts through commit")
    claim, result = await fresh()
    before = await snapshot()
    left, right = result.volumes
    for invalid in (
        None, [], (), (left,), (left, left), (right, left),
        (replace(left, namespace_uid="another-namespace"), right),
        (replace(left, identity=replace(left.identity, owner_id=8)), right),
        (left, replace(right, volume_uid=left.volume_uid)),
    ):
        await error(
            completion.StoreCompletionConflict,
            finish(claim, replace(result, volumes=invalid)),
        )
        assert await snapshot() == before

    for mode in ("missing", "all-missing", "partial", "claim_uid", "volume_name", "volume_uid"):
        claim, result = await fresh()
        item = result.volumes[0]
        async with factory.begin() as db:
            condition = StoreVolume.store_id == claim.store_id
            if mode != "all-missing":
                condition = condition & (StoreVolume.claim_name == item.claim_name)
            if mode in ("missing", "all-missing"):
                await db.execute(delete(StoreVolume).where(condition))
            else:
                values = {"volume_uid": None} if mode == "partial" else {
                    mode: "changed-" + uuid4().hex,
                }
                await db.execute(update(StoreVolume).where(condition).values(**values))
        before = await snapshot()
        await error(completion.StoreCompletionConflict, finish(claim, result))
        assert await snapshot() == before

    claim, result = await fresh()
    before = await snapshot()
    await error(
        IntegrityError,
        change(Store, claim.store_id, namespace_uid="replacement-" + uuid4().hex),
    )
    assert await snapshot() == before
    print("Missing, partial, changed and cross-store volume evidence rejects Ready without changing registrations or saved data")
    for field in ("lease_expires_at", "deadline_at"):
        claim, result = await fresh()
        waiting = asyncio.Event()
        task = None

        async def observe_volume_wait(db, statement, *args, **kwargs):
            if (
                asyncio.current_task() is task
                and getattr(statement, "_for_update_arg", None) is not None
                and any(
                    getattr(table, "name", None) == "store_volumes"
                    for table in statement.get_final_froms()
                )
            ):
                waiting.set()
            return await normal_execute(db, statement, *args, **kwargs)

        try:
            with patch.object(AsyncSession, "execute", observe_volume_wait):
                async with factory.begin() as holder:
                    await holder.execute(
                        select(StoreVolume).where(
                            StoreVolume.store_id == claim.store_id,
                        ).with_for_update()
                    )
                    claim, result = await shorten(claim, result, field)
                    before = await snapshot()
                    task = asyncio.create_task(finish(claim, result))
                    signal = asyncio.create_task(waiting.wait())
                    try:
                        done, _ = await asyncio.wait(
                            (task, signal), timeout=5,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if task in done:
                            outcome = await task
                            raise AssertionError(
                                "Ready finished before reaching the held volume rows: "
                                + repr(outcome)
                            )
                        assert signal in done, "The volume-row query did not start."
                    finally:
                        signal.cancel()
                        await asyncio.gather(signal, return_exceptions=True)
                    await wait_past(holder, getattr(claim, field))
                    assert not task.done()
                assert await asyncio.wait_for(task, 5) is False
                assert await snapshot() == before
        finally:
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
    print("Real volume-row lock waits cannot revive an expired lease or provisioning deadline")
    print("Verified Ready PostgreSQL checks passed. External readiness probes were not called.")
