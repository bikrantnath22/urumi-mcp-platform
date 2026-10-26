"""Verify volume bindings in a temporary PostgreSQL schema; no cluster calls."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import re
import sys
from unittest.mock import patch
from uuid import uuid4

if not Path("app/store_volume_binding.py").is_file():
    raise SystemExit("Run from Wo_MCP/apps/gateway.")
sys.path.insert(0, str(Path.cwd()))

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app import store_jobs as jobs
from app import store_volume_binding as binding
from app.config import database_url
from app.database import engine
from app.models import (
    Base, SecurityEvent, Store, StoreOperation, StoreProvisioningSettings,
    StoreVolume, User,
)
from app.store_namespace import namespace_for
from app.store_settings import STORE_ADMISSION_LOCK
from app.store_volumes import StoreVolumeBinding, StoreVolumeError


OWNER_A, OWNER_B = 98801, 98802


async def expect(kind, operation):
    try:
        await operation
    except kind as error:
        assert "PRIVATE" not in str(error)
        return
    raise AssertionError("Expected " + str(kind))


async def stop_tasks(*tasks):
    pending = [task for task in tasks if task is not None]
    for task in pending:
        if not task.done():
            task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


async def check():
    schema = "urumi_volume_bind_test_" + uuid4().hex
    created, isolated = False, None
    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
        created = True
        isolated = create_async_engine(
            database_url(), pool_size=10, max_overflow=0, pool_timeout=5,
            hide_parameters=True, isolation_level="READ COMMITTED",
            connect_args={
                "connect_timeout": 5,
                "options": (
                    f"-c search_path={schema} -c timezone=UTC "
                    "-c statement_timeout=15000 -c lock_timeout=12000"
                ),
            },
        )
        factory = async_sessionmaker(isolated, expire_on_commit=False)
        async with isolated.begin() as connection:
            assert await connection.scalar(select(func.current_schema())) == schema
            await connection.run_sync(Base.metadata.create_all)
        async with factory.begin() as db:
            db.add(StoreProvisioningSettings(id=1, max_concurrent_creations=8))
            for owner in (OWNER_A, OWNER_B):
                db.add(User(
                    id=owner, email=f"owner-{owner}@fixture.invalid",
                    role="member", active=True, password_hash="fixture-only",
                ))

        async def reset():
            async with factory.begin() as db:
                assert await db.scalar(select(func.current_schema())) == schema
                for model in (SecurityEvent, StoreVolume, StoreOperation, Store):
                    await db.execute(delete(model))

        async def fresh(owner=OWNER_A, seconds=1200):
            identity = uuid4()
            async with factory.begin() as db:
                now = await db.scalar(select(func.clock_timestamp()))
                db.add(Store(
                    id=identity, owner_id=owner, name="Fixture tea",
                    slug="tea-" + identity.hex[:16],
                    namespace="urumi-store-" + identity.hex,
                    namespace_uid=str(uuid4()), release_name="store",
                    url=f"http://tea-{identity.hex[:16]}.localtest.me",
                    status="Provisioning", desired_state="present", generation=1,
                ))
                await db.flush()
                db.add(StoreOperation(
                    id=uuid4(), store_id=identity, requested_by_id=owner,
                    request_key=uuid4(), request_fingerprint="a" * 64,
                    action="provision", store_generation=1,
                    status="Pending", attempts=0,
                    available_at=now - timedelta(seconds=1),
                    deadline_at=now + timedelta(seconds=seconds),
                ))
            claim = await jobs.claim_next(factory)
            assert claim is not None and claim.store_id == identity
            return claim

        async def proof(claim, name="wordpress-data"):
            async with factory() as db:
                uid = (await db.get(Store, claim.store_id)).namespace_uid
            claim_uid = str(uuid4())
            return StoreVolumeBinding(
                namespace_for(claim), uid, name, claim_uid,
                "pvc-" + claim_uid, str(uuid4()),
            )

        def bind(claim, observed, **options):
            return binding.bind_store_volume(factory, claim, observed, **options)

        async def change(model, identity, **values):
            async with factory.begin() as db:
                await db.execute(update(model).where(model.id == identity).values(**values))

        async def snapshot():
            async with factory() as db:
                result = {}
                for model in (Store, StoreOperation, StoreVolume, SecurityEvent):
                    table = model.__table__
                    result[table.name] = [
                        tuple(row) for row in await db.execute(
                            select(table).order_by(*table.primary_key.columns)
                        )
                    ]
                return result

        async def wait_past(db, instant):
            try:
                async with asyncio.timeout(8):
                    while True:
                        now = await db.scalar(select(func.clock_timestamp()))
                        if now >= instant:
                            return
                        await asyncio.sleep(0.05)
            except TimeoutError:
                raise AssertionError(
                    "PostgreSQL's clock did not reach the test cutoff within 8 seconds."
                ) from None

        claim = await fresh()
        observed = await proof(claim)
        before = await snapshot()
        tasks = [asyncio.create_task(bind(claim, observed)) for _ in range(8)]
        try:
            assert await asyncio.wait_for(asyncio.gather(*tasks), 25) == [observed] * 8
        finally:
            await stop_tasks(*tasks)
        after = await snapshot()
        assert len(after["store_volumes"]) == len(after["security_events"]) == 1
        assert before["store_operations"] == after["store_operations"]
        assert await bind(claim, observed) == observed and await snapshot() == after
        async with factory() as db:
            event = (await db.scalars(select(SecurityEvent))).one()
            assert event.actor_id is None and event.target_user_id == OWNER_A
            assert event.action == "store.volume_bound"
            assert event.details["claim_uid"] == observed.claim_uid
            assert event.details["volume_uid"] == observed.volume_uid
        mysql = await proof(claim, "data-mysql-0")
        assert await bind(claim, mysql) == mysql
        assert len((await snapshot())["store_volumes"]) == 2
        print("Eight simultaneous retries save once; both fixed claims keep separate records and original deadlines")

        await change(
            StoreOperation, claim.operation_id,
            lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
        )
        recovered = await jobs.claim_next(factory)
        assert recovered and recovered.lease_token != claim.lease_token
        before = await snapshot()
        for invalid in (
            claim, replace(recovered, lease_token=uuid4()),
            replace(recovered, operation_id=uuid4()),
            replace(recovered, attempt=99), replace(recovered, generation=99),
        ):
            assert await bind(invalid, observed) is None
        assert await bind(recovered, observed) == observed
        assert await snapshot() == before
        print("Recovery reuses saved identities; old tokens and forged claims cannot write")

        await reset()
        first, second = await fresh(), await fresh(OWNER_B)
        left, right = await proof(first), await proof(second)
        assert await bind(first, left) == left
        for field in ("claim_uid", "volume_name", "volume_uid"):
            before = await snapshot()
            await expect(
                StoreVolumeError,
                bind(second, replace(right, **{field: getattr(left, field)})),
            )
            assert await snapshot() == before
        async def wrong_namespace():
            async with factory.begin() as db:
                db.add(StoreVolume(
                    store_id=second.store_id, namespace_uid=left.namespace_uid,
                    claim_name=right.claim_name, claim_uid=right.claim_uid,
                    volume_name=right.volume_name, volume_uid=right.volume_uid,
                    storage_class="local-path",
                ))
        await expect(IntegrityError, wrong_namespace())
        await change(
            Store, first.store_id, status="Deleted", desired_state="absent",
            deleted_at=func.clock_timestamp(),
        )
        before = await snapshot()
        await expect(StoreVolumeError, bind(second, replace(right, volume_uid=left.volume_uid)))
        assert await snapshot() == before
        print("Real uniqueness and store/namespace foreign keys block cross-store adoption, including deleted-store identities")

        for known_name in (False, True):
            await reset()
            claim = await fresh()
            observed = await proof(claim)
            async with factory.begin() as db:
                db.add(StoreVolume(
                    store_id=claim.store_id, namespace_uid=observed.namespace_uid,
                    claim_name=observed.claim_name, claim_uid=observed.claim_uid,
                    volume_name=observed.volume_name if known_name else None,
                    volume_uid=None, storage_class="local-path",
                ))
            assert await bind(claim, observed) == observed
            before = await snapshot()
            assert await bind(claim, observed) == observed
            for field in ("claim_uid", "volume_name", "volume_uid"):
                await expect(
                    StoreVolumeError,
                    bind(claim, replace(observed, **{field: "changed"})),
                )
            assert await snapshot() == before
        print("Pending records gain PV identifiers once; matching replays do not rewrite rows or duplicate audits")

        for values in (
            {"owner_id": OWNER_B}, {"namespace": "another"},
            {"namespace_uid": str(uuid4())}, {"release_name": "another"},
            {"name": "Changed"}, {"slug": "changed"},
            {"url": "https://changed.invalid"},
        ):
            await reset()
            claim = await fresh()
            observed = await proof(claim)
            await change(Store, claim.store_id, **values)
            before = await snapshot()
            await expect(StoreVolumeError, bind(claim, observed))
            assert await snapshot() == before
        await reset()
        claim = await fresh(seconds=-1)
        observed = await proof(claim)
        assert await bind(claim, observed) is None
        assert await bind(claim, observed, for_cleanup=True) == observed
        async with factory() as db:
            assert (await db.get(StoreOperation, claim.operation_id)).deadline_at == claim.deadline_at
        print("Ownership changes reject saves; overdue cleanup requires explicit mode and preserves its original deadline")

        await reset()
        claim = await fresh()
        observed = await proof(claim)
        await change(Store, claim.store_id, status="Deleting", desired_state="absent")
        await change(StoreOperation, claim.operation_id, action="delete")
        deletion = replace(claim, action="delete")
        await expect(ValueError, bind(deletion, observed))
        assert await bind(deletion, observed, for_cleanup=True) == observed

        normal_flush = AsyncSession.flush
        for mode in ("storage", "lease", "deadline"):
            await reset()
            claim = await fresh(seconds=3 if mode == "deadline" else 1200)
            observed = await proof(claim)
            if mode == "lease":
                await change(
                    StoreOperation, claim.operation_id,
                    lease_expires_at=func.clock_timestamp() + timedelta(seconds=2),
                )
            before = await snapshot()
            async def interrupted_flush(db, *args, **kwargs):
                await normal_flush(db, *args, **kwargs)
                if mode == "storage":
                    raise SQLAlchemyError("PRIVATE STORAGE FAILURE")
                field = StoreOperation.lease_expires_at if mode == "lease" else StoreOperation.deadline_at
                cutoff = await db.scalar(select(field).where(StoreOperation.id == claim.operation_id))
                await wait_past(db, cutoff)
            with patch.object(AsyncSession, "flush", interrupted_flush):
                if mode == "storage":
                    await expect(binding.VolumeBindingUnavailable, bind(claim, observed))
                else:
                    assert await bind(claim, observed) is None
            assert await snapshot() == before
        print("Post-flush storage errors and lease/deadline expiry roll back storage rows, parent updates and audits")

        normal_get = AsyncSession.get
        for field in ("lease", "deadline"):
            await reset()
            claim = await fresh(seconds=3 if field == "deadline" else 1200)
            observed = await proof(claim)
            async with factory.begin() as db:
                db.add(StoreVolume(
                    store_id=claim.store_id, namespace_uid=observed.namespace_uid,
                    claim_name=observed.claim_name, claim_uid=observed.claim_uid,
                    storage_class="local-path",
                ))
            if field == "lease":
                await change(
                    StoreOperation, claim.operation_id,
                    lease_expires_at=func.clock_timestamp() + timedelta(seconds=2),
                )
            before = await snapshot()
            task, entered = None, asyncio.Event()
            async def observe_get(db, model, key, *args, **kwargs):
                if asyncio.current_task() is task and model is StoreVolume:
                    entered.set()
                return await normal_get(db, model, key, *args, **kwargs)
            try:
                with patch.object(AsyncSession, "get", observe_get):
                    async with factory.begin() as blocker:
                        await blocker.execute(
                            select(StoreVolume).where(StoreVolume.store_id == claim.store_id).with_for_update()
                        )
                        field_column = StoreOperation.lease_expires_at if field == "lease" else StoreOperation.deadline_at
                        cutoff = await blocker.scalar(
                            select(field_column).where(StoreOperation.id == claim.operation_id)
                        )
                        task = asyncio.create_task(bind(claim, observed))
                        await asyncio.wait_for(entered.wait(), 5)
                        await wait_past(blocker, cutoff)
                    assert await asyncio.wait_for(task, 10) is None
                assert await snapshot() == before
            finally:
                await stop_tasks(task)
        print("Real storage-row lock waits cannot revive expired worker leases or provisioning deadlines")

        for cancel in (False, True):
            await reset()
            claim = await fresh()
            observed = await proof(claim)
            before = await snapshot()
            paused, resume = asyncio.Event(), asyncio.Event()
            task = competing = None
            async def hold_flush(db, *args, **kwargs):
                await normal_flush(db, *args, **kwargs)
                if asyncio.current_task() is task:
                    paused.set()
                    await asyncio.wait_for(resume.wait(), 10)
            try:
                with patch.object(AsyncSession, "flush", hold_flush):
                    task = asyncio.create_task(bind(claim, observed))
                    await asyncio.wait_for(paused.wait(), 5)
                    async with factory.begin() as probe:
                        assert await probe.scalar(select(
                            func.pg_try_advisory_xact_lock(STORE_ADMISSION_LOCK)
                        )) is False
                    assert await snapshot() == before
                    if cancel:
                        task.cancel()
                        await expect(asyncio.CancelledError, task)
                    else:
                        competing = asyncio.create_task(bind(claim, observed))
                        resume.set()
                        assert await asyncio.wait_for(
                            asyncio.gather(task, competing), 15,
                        ) == [observed, observed]
            finally:
                resume.set()
                await stop_tasks(task, competing)
            if cancel:
                assert await snapshot() == before
                assert await bind(claim, observed) == observed
            assert len((await snapshot())["security_events"]) == 1
        assert isolated.pool.checkedout() == 0
        print("The scheduling gate lasts through commit; uncommitted work stays hidden and cancellation rolls back safely")
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from store_volume_stage_postgres import verify_volume_stage
        await verify_volume_stage(factory, reset, fresh, proof, change, snapshot)
        assert isolated.pool.checkedout() == 0
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from store_claim_recording_postgres import verify_claim_recording
        await verify_claim_recording(
            factory, reset, fresh, proof, change, snapshot, wait_past,
        )
        assert isolated.pool.checkedout() == 0
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from store_claim_stage_postgres import verify_claim_stage
        await verify_claim_stage(factory, reset, fresh, proof, change, snapshot)
        assert isolated.pool.checkedout() == 0
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from store_storage_records_postgres import verify_storage_records
        await verify_storage_records(
            factory, reset, fresh, proof, change, snapshot, wait_past,
        )
        assert isolated.pool.checkedout() == 0
        print("Volume binding verification passed. Live accounts, stores and settings were unchanged.")
        print("No Kubernetes, AI provider or WordPress calls were made.")
    finally:
        try:
            if isolated is not None:
                await isolated.dispose()
        finally:
            try:
                if created:
                    assert re.fullmatch(r"urumi_volume_bind_test_[0-9a-f]{32}", schema)
                    async with engine.begin() as connection:
                        await connection.execute(DropSchema(schema, cascade=True))
                    print("Temporary PostgreSQL schema removed.")
            finally:
                await engine.dispose()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 180))
