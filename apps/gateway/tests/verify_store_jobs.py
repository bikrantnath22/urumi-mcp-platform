"""Verify store job claims in a disposable PostgreSQL schema."""

import asyncio
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

if not Path("app/store_jobs.py").is_file():
    raise SystemExit("Run from Wo_MCP/apps/gateway.")
sys.path.insert(0, str(Path.cwd()))

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app import store_jobs as jobs
from app.config import database_url
from app.database import engine
from app.models import Base, Store, StoreOperation, StoreProvisioningSettings, User
from app.store_scheduling import StoreSchedulingUnavailable
from app.store_settings import STORE_ADMISSION_LOCK, lock_store_admission


OWNER_A, OWNER_B = 98201, 98202


async def check():
    schema = "urumi_store_jobs_test_" + uuid4().hex
    created, isolated = False, None

    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
        created = True

        isolated = create_async_engine(
            database_url(), pool_size=8, max_overflow=0, pool_timeout=5,
            hide_parameters=True,
            connect_args={
                "connect_timeout": 5,
                "options": (
                    f"-c search_path={schema} -c timezone=UTC "
                    "-c statement_timeout=10000 -c lock_timeout=5000"
                ),
            },
        )
        factory = async_sessionmaker(isolated, expire_on_commit=False)

        async with isolated.begin() as connection:
            assert await connection.scalar(select(func.current_schema())) == schema
            await connection.run_sync(Base.metadata.create_all)

        async with factory.begin() as db:
            # This first suite tests leases with ample fixture capacity.
            db.add(StoreProvisioningSettings(id=1, max_concurrent_creations=100))
            for owner in (OWNER_A, OWNER_B):
                db.add(User(
                    id=owner, email=f"owner-{owner}@fixture.invalid",
                    role="member", active=True, password_hash="fixture-only",
                ))

        async def reset(limit=100):
            async with factory.begin() as db:
                await db.execute(delete(StoreOperation))
                await db.execute(delete(Store))
                await db.execute(
                    update(StoreProvisioningSettings)
                    .where(StoreProvisioningSettings.id == 1)
                    .values(max_concurrent_creations=limit)
                )

        async def seed(owner=OWNER_A, *, action="provision", overdue=False, age=0):
            async with factory.begin() as db:
                now = await db.scalar(select(func.clock_timestamp()))
                identity = uuid4()
                store = Store(
                    id=identity, owner_id=owner, name="Fixture store",
                    slug="tea-" + identity.hex[:16],
                    namespace="urumi-store-" + identity.hex, release_name="store",
                    url=f"http://tea-{identity.hex[:16]}.localtest.me",
                    status="Provisioning" if action == "provision" else "Deleting",
                    desired_state="present" if action == "provision" else "absent",
                    generation=1,
                )
                db.add(store)
                await db.flush()
                operation = StoreOperation(
                    id=uuid4(), store_id=identity, requested_by_id=owner,
                    request_key=uuid4(), request_fingerprint="a" * 64,
                    action=action, store_generation=1, status="Pending", attempts=0,
                    available_at=now - timedelta(seconds=1),
                    created_at=now - timedelta(seconds=age),
                    deadline_at=now + timedelta(seconds=-1 if overdue else 1200),
                )
                db.add(operation)
                await db.flush()
                return identity, operation.id, operation.deadline_at

        async def change(model, identity, **values):
            async with factory.begin() as db:
                await db.execute(
                    update(model).where(model.id == identity).values(**values)
                )

        async def record(identity):
            async with factory() as db:
                return await db.get(StoreOperation, identity)

        async def assert_lost(claim):
            assert await jobs.renew_claim(factory, claim) is None
            assert await jobs.release_claim(factory, claim) is False

        async def expect_storage_error(call):
            try:
                await call()
            except SQLAlchemyError:
                return
            raise AssertionError("Expected the injected storage failure.")

        def no_storage():
            raise AssertionError("Invalid configuration reached storage.")

        for value in (True, False, 0, 14, 301, 1.5, "60", None):
            try:
                await jobs.claim_next(no_storage, lease_seconds=value)
            except ValueError:
                pass
            else:
                raise AssertionError(value)

        assert await jobs.claim_next(factory) is None
        for owner in (OWNER_A, OWNER_B) * 3:
            await seed(owner)

        gate = asyncio.Event()

        async def contender():
            await gate.wait()
            return await jobs.claim_next(factory)

        attempts = [asyncio.create_task(contender()) for _ in range(8)]
        gate.set()
        results = await asyncio.wait_for(asyncio.gather(*attempts), 20)
        claims = [result for result in results if result is not None]
        assert len(claims) == 6
        assert len({claim.operation_id for claim in claims}) == 6
        assert {claim.owner_id for claim in claims} == {OWNER_A, OWNER_B}
        assert all(claim.attempt == 1 for claim in claims)
        assert await jobs.claim_next(factory) is None
        print("Eight simultaneous claim attempts take six different jobs exactly once")

        first = claims[0]
        assert str(first.lease_token) not in repr(first)
        try:
            first.attempt = 99
        except FrozenInstanceError:
            pass
        else:
            raise AssertionError("Claim snapshot is mutable.")

        # A separate connection can lock rows as soon as claim_next returns.
        async with factory.begin() as db:
            await db.execute(
                select(StoreOperation).where(StoreOperation.id == first.operation_id)
                .with_for_update(nowait=True)
            )
            await db.execute(
                select(Store).where(Store.id == first.store_id)
                .with_for_update(nowait=True)
            )

        renewed = await jobs.renew_claim(factory, first, lease_seconds=90)
        assert renewed is not None
        assert renewed.lease_token == first.lease_token
        assert renewed.attempt == first.attempt
        assert renewed.deadline_at == first.deadline_at
        assert renewed.lease_expires_at - renewed.checked_at == timedelta(seconds=90)
        print("Claims commit before return; renewal preserves token, attempt and deadline")

        await reset()
        store_id, operation_id, deadline = await seed()
        original = await jobs.claim_next(factory)
        started = (await record(operation_id)).started_at
        await change(
            StoreOperation, operation_id,
            lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
        )
        await assert_lost(original)
        recovered = await jobs.claim_next(factory)
        assert recovered is not None and recovered.attempt == 2
        assert recovered.lease_token != original.lease_token
        assert recovered.deadline_at == deadline
        assert (await record(operation_id)).started_at == started
        await assert_lost(original)

        for forged in (
            replace(recovered, lease_token=uuid4()),
            replace(recovered, store_id=uuid4()),
            replace(recovered, operation_id=uuid4()),
            replace(recovered, generation=2),
            replace(recovered, attempt=999),
            replace(recovered, action="delete"),
        ):
            await assert_lost(forged)
        assert (await record(operation_id)).lease_token == recovered.lease_token
        print("Expired work is recovered with a new token; stale and mismatched claims are rejected")

        assert await jobs.release_claim(factory, recovered, retry_after_seconds=30)
        pending = await record(operation_id)
        assert pending.status == "Pending"
        assert pending.lease_token is None and pending.lease_expires_at is None
        assert pending.available_at - pending.updated_at == timedelta(seconds=30)
        assert pending.deadline_at == deadline and pending.started_at == started
        assert await jobs.claim_next(factory) is None
        await assert_lost(recovered)
        await change(
            StoreOperation, operation_id,
            available_at=func.clock_timestamp() - timedelta(seconds=1),
        )
        retried = await jobs.claim_next(factory)
        assert retried.attempt == 3 and retried.deadline_at == deadline

        for value in (True, -1, 301, "5", None):
            try:
                await jobs.release_claim(no_storage, retried, retry_after_seconds=value)
            except ValueError:
                pass
            else:
                raise AssertionError(value)
        print("Release clears ownership, delays retry and retains the original deadline")

        for action in ("provision", "delete"):
            await reset()
            store_id, operation_id, deadline = await seed(action=action, overdue=True)
            overdue = await jobs.claim_next(factory)
            assert overdue.deadline_reached and overdue.deadline_at == deadline
            refreshed = await jobs.renew_claim(factory, overdue)
            assert refreshed is not None and refreshed.deadline_at == deadline
            async with factory() as db:
                store = await db.get(Store, store_id)
                assert store.status == ("Provisioning" if action == "provision" else "Deleting")
        print("Overdue jobs remain recoverable for cleanup without extending deadlines or declaring success")

        for changes in (
            {"generation": 2},
            {"desired_state": "absent"},
            {"status": "Failed"},
            {"status": "Deleted", "desired_state": "absent",
             "deleted_at": func.clock_timestamp()},
            {"deleted_at": func.clock_timestamp()},
        ):
            await reset()
            store_id, operation_id, _ = await seed()
            claim = await jobs.claim_next(factory)
            await change(Store, store_id, **changes)
            await assert_lost(claim)
            await change(
                StoreOperation, operation_id,
                lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
            )
            assert await jobs.claim_next(factory) is None

        for status in ("Succeeded", "Failed", "Cancelled"):
            await reset()
            _, operation_id, _ = await seed()
            claim = await jobs.claim_next(factory)
            await change(
                StoreOperation, operation_id, status=status,
                lease_token=None, lease_expires_at=None,
                finished_at=func.clock_timestamp(),
            )
            await assert_lost(claim)
            assert await jobs.claim_next(factory) is None
        print("Changed store generations/state and terminal operations cannot be resumed")

        for model in (StoreOperation, Store):
            await reset()
            older_store, older_operation, _ = await seed(age=10)
            _, newer_operation, _ = await seed(OWNER_B)
            locked_id = older_operation if model is StoreOperation else older_store
            async with factory.begin() as holder:
                await holder.execute(
                    select(model).where(model.id == locked_id).with_for_update()
                )
                other = await asyncio.wait_for(jobs.claim_next(factory), 3)
                assert other.operation_id == newer_operation
            older = await jobs.claim_next(factory)
            assert older.operation_id == older_operation
        print("Locked operation/store rows are skipped so other jobs can progress")

        # The lease expires while renewal is waiting for another transaction.
        await reset()
        _, operation_id, _ = await seed()
        claim = await jobs.claim_next(factory)
        waiting = asyncio.Event()
        normal_execute = AsyncSession.execute

        async def observed_execute(db, statement, *args, **kwargs):
            waiting.set()
            return await normal_execute(db, statement, *args, **kwargs)

        task = None
        try:
            async with factory.begin() as holder:
                await holder.execute(
                    update(StoreOperation).where(StoreOperation.id == operation_id)
                    .values(lease_expires_at=func.clock_timestamp() + timedelta(seconds=1))
                )
                with patch.object(AsyncSession, "execute", observed_execute):
                    task = asyncio.create_task(jobs.renew_claim(factory, claim))
                    await asyncio.wait_for(waiting.wait(), 5)
                    await asyncio.sleep(1.2)
                    assert not task.done()
            assert await asyncio.wait_for(task, 5) is None
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        print("A lease that expires during a lock wait cannot be renewed")

        await reset()
        _, operation_id, deadline = await seed()
        normal_flush = AsyncSession.flush

        async def failed_flush(db, *args, **kwargs):
            await normal_flush(db, *args, **kwargs)
            raise SQLAlchemyError("fixture-only injected storage failure")

        with patch.object(AsyncSession, "flush", failed_flush):
            await expect_storage_error(lambda: jobs.claim_next(factory))
        untouched = await record(operation_id)
        assert untouched.status == "Pending" and untouched.attempts == 0
        assert untouched.started_at is None and untouched.lease_token is None

        claim = await jobs.claim_next(factory)
        before = await record(operation_id)
        with patch.object(AsyncSession, "flush", failed_flush):
            await expect_storage_error(
                lambda: jobs.renew_claim(factory, claim, lease_seconds=120)
            )
        after = await record(operation_id)
        assert after.lease_expires_at == before.lease_expires_at
        with patch.object(AsyncSession, "flush", failed_flush):
            await expect_storage_error(lambda: jobs.release_claim(factory, claim))
        after = await record(operation_id)
        assert after.status == "Running" and after.lease_token == claim.lease_token
        assert after.deadline_at == deadline
        assert await jobs.release_claim(factory, claim)
        print("Storage failures roll back claim, renewal and release without partial writes")
        # Shared capacity is enforced across independent worker sessions.
        await reset(limit=2)
        for owner, age in (
            (OWNER_A, 90), (OWNER_A, 80), (OWNER_A, 70),
            (OWNER_B, 20), (OWNER_B, 10), (OWNER_B, 0),
        ):
            await seed(owner, age=age)

        async def burst():
            results = await asyncio.wait_for(
                asyncio.gather(*(jobs.claim_next(factory) for _ in range(8))),
                20,
            )
            return [result for result in results if result is not None]

        first_wave = await burst()
        assert len(first_wave) == 2
        assert {claim.owner_id for claim in first_wave} == {OWNER_A, OWNER_B}
        assert await jobs.claim_next(factory) is None

        async def configure_capacity(limit):
            async with factory.begin() as db:
                await lock_store_admission(db)
                await db.execute(
                    update(StoreProvisioningSettings)
                    .where(StoreProvisioningSettings.id == 1)
                    .values(max_concurrent_creations=limit)
                )

        await configure_capacity(4)
        second_wave = await burst()
        assert len(second_wave) == 2
        running = first_wave + second_wave
        assert len({claim.operation_id for claim in running}) == 4
        await configure_capacity(1)
        assert await jobs.claim_next(factory) is None
        for claim in running:
            renewed = await jobs.renew_claim(factory, claim)
            assert renewed is not None and renewed.deadline_at == claim.deadline_at
        for claim in running[:-1]:
            assert await jobs.release_claim(
                factory, claim, retry_after_seconds=300,
            )
        assert await jobs.claim_next(factory) is None
        assert await jobs.release_claim(
            factory, running[-1], retry_after_seconds=300,
        )
        assert len(await burst()) == 1
        print("Global capacity is exact; increases apply immediately and decreases preserve running work")

        async def complete_fixture(claim):
            async with factory.begin() as db:
                await lock_store_admission(db)
                await db.execute(
                    update(StoreOperation)
                    .where(StoreOperation.id == claim.operation_id)
                    .values(
                        status="Succeeded", lease_token=None, lease_expires_at=None,
                        finished_at=func.clock_timestamp(),
                        updated_at=func.clock_timestamp(),
                    )
                )
                await db.execute(
                    update(Store).where(Store.id == claim.store_id)
                    .values(status="Ready")
                )

        await reset(limit=1)
        _, first_id, _ = await seed(OWNER_A, age=30)
        _, second_a_id, _ = await seed(OWNER_A, age=20)
        _, first_b_id, _ = await seed(OWNER_B, age=10)
        first = await jobs.claim_next(factory)
        assert first.operation_id == first_id
        await complete_fixture(first)
        second = await jobs.claim_next(factory)
        assert second.operation_id == first_b_id
        await complete_fixture(second)
        third = await jobs.claim_next(factory)
        assert third.operation_id == second_a_id

        await reset(limit=2)
        await seed(OWNER_A, age=20)
        await seed(OWNER_A, age=10)
        assert (await jobs.claim_next(factory)).owner_id == OWNER_A
        assert (await jobs.claim_next(factory)).owner_id == OWNER_A
        print("Members receive fair turns; one member can still use otherwise idle capacity")

        await reset(limit=2)
        _, fresh_id, _ = await seed(OWNER_A, age=60)
        _, recovered_id, _ = await seed(OWNER_A, age=50)
        await change(StoreOperation, recovered_id, attempts=1)
        _, overdue_id, _ = await seed(OWNER_B, age=40, overdue=True)
        _, delete_id, _ = await seed(OWNER_A, action="delete", age=30)
        assert (await jobs.claim_next(factory)).operation_id == delete_id
        assert (await jobs.claim_next(factory)).operation_id == overdue_id
        assert (await jobs.claim_next(factory)).operation_id == recovered_id
        assert (await record(fresh_id)).status == "Pending"
        assert await jobs.claim_next(factory) is None
        _, another_delete, _ = await seed(OWNER_B, action="delete")
        assert (await jobs.claim_next(factory)).operation_id == another_delete
        print("Deletion, overdue cleanup and recovery have priority; creation capacity remains bounded")

        # Prove each mutation holds the same database gate until commit.
        for action in ("claim", "renew", "release"):
            await reset(limit=1)
            await seed(OWNER_A, age=20)
            await seed(OWNER_B, age=10)
            owned = None if action == "claim" else await jobs.claim_next(factory)
            paused, resume = asyncio.Event(), asyncio.Event()
            normal_flush = AsyncSession.flush
            task = None

            async def hold_flush(db, *args, **kwargs):
                await normal_flush(db, *args, **kwargs)
                if asyncio.current_task() is task:
                    paused.set()
                    await asyncio.wait_for(resume.wait(), 10)

            async def mutate():
                if action == "claim":
                    return await jobs.claim_next(factory)
                if action == "renew":
                    return await jobs.renew_claim(factory, owned)
                return await jobs.release_claim(
                    factory, owned, retry_after_seconds=300,
                )

            competing = None
            try:
                with patch.object(AsyncSession, "flush", hold_flush):
                    task = asyncio.create_task(mutate())
                    await asyncio.wait_for(paused.wait(), 10)
                    async with factory.begin() as probe:
                        acquired = await probe.scalar(
                            select(func.pg_try_advisory_xact_lock(
                                STORE_ADMISSION_LOCK,
                            ))
                        )
                        assert acquired is False, action
                    competing = asyncio.create_task(jobs.claim_next(factory))
                    resume.set()
                    result, competitor = await asyncio.wait_for(
                        asyncio.gather(task, competing), 15,
                    )
                    if action == "release":
                        assert result is True and competitor is not None
                        assert competitor.owner_id == OWNER_B
                    else:
                        assert result is not None and competitor is None
            finally:
                resume.set()
                tasks = [item for item in (task, competing) if item is not None]
                for item in tasks:
                    if not item.done():
                        item.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        print("Claim, renewal and release share the admission gate through commit")

        await reset(limit=1)
        _, pending_id, _ = await seed()
        strict_engine = isolated.execution_options(isolation_level="REPEATABLE READ")
        strict_factory = async_sessionmaker(strict_engine, expire_on_commit=False)
        try:
            await jobs.claim_next(strict_factory)
        except StoreSchedulingUnavailable:
            pass
        else:
            raise AssertionError("Unsupported isolation admitted work.")
        assert (await record(pending_id)).attempts == 0

        async with factory.begin() as db:
            await db.execute(delete(StoreProvisioningSettings))
        try:
            await jobs.claim_next(factory)
        except StoreSchedulingUnavailable:
            pass
        else:
            raise AssertionError("Missing settings admitted work.")
        pending = await record(pending_id)
        assert pending.status == "Pending" and pending.attempts == 0
        print("Unsupported isolation and missing settings reject work without partial claims")
        print("Store job verification passed. Live stores/settings and external services were unchanged.")

    finally:
        if isolated is not None:
            await isolated.dispose()
        try:
            if created:
                assert schema.startswith("urumi_store_jobs_test_") and len(schema) < 64
                async with engine.begin() as connection:
                    await connection.execute(DropSchema(schema, cascade=True))
                print("Temporary PostgreSQL schema removed.")
        finally:
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 180))
