"""Verify job guards in a temporary PostgreSQL schema; no Kubernetes calls."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

if not Path("app/store_job_guard.py").is_file():
    raise SystemExit("Run from Wo_MCP/apps/gateway.")
sys.path.insert(0, str(Path.cwd()))

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app import store_job_guard as guard
from app import store_jobs as jobs
from app.config import database_url
from app.database import engine
from app.models import Base, SecurityEvent, Store, StoreOperation, StoreProvisioningSettings, User
from app.store_settings import STORE_ADMISSION_LOCK


async def expect_error(expected, call):
    try:
        await call
    except expected as error:
        return error
    raise AssertionError(f"Expected {expected.__name__}.")


async def check():
    schema = "urumi_guard_test_" + uuid4().hex
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
            db.add(StoreProvisioningSettings(id=1, max_concurrent_creations=8))
            for owner in (98701, 98702):
                db.add(User(
                    id=owner, email=f"guard-{owner}@fixture.invalid",
                    role="member", active=True, password_hash="fixture-only",
                ))

        async def fresh(*, action="provision", seconds=1200):
            async with factory.begin() as db:
                for model in (SecurityEvent, StoreOperation, Store):
                    await db.execute(delete(model))
                identity = uuid4()
                now = await db.scalar(select(func.clock_timestamp()))
                db.add(Store(
                    id=identity, owner_id=98701, name="Fixture tea",
                    slug="tea-" + identity.hex[:16],
                    namespace="urumi-store-" + identity.hex, release_name="store",
                    url=f"http://tea-{identity.hex[:16]}.localtest.me",
                    status="Provisioning" if action == "provision" else "Deleting",
                    desired_state="present" if action == "provision" else "absent",
                    generation=1,
                ))
                await db.flush()
                db.add(StoreOperation(
                    id=uuid4(), store_id=identity, requested_by_id=98701,
                    request_key=uuid4(), request_fingerprint="a" * 64,
                    action=action, store_generation=1, status="Pending", attempts=0,
                    available_at=now - timedelta(seconds=1),
                    deadline_at=now + timedelta(seconds=seconds),
                ))
            claim = await jobs.claim_next(factory)
            assert claim is not None and claim.store_id == identity
            return claim

        async def change(model, identity, **values):
            async with factory.begin() as db:
                await db.execute(
                    update(model).where(model.id == identity).values(**values)
                )

        async def snapshot():
            async with factory() as db:
                result = {}
                for model in (User, StoreProvisioningSettings, Store, StoreOperation, SecurityEvent):
                    table = model.__table__
                    result[table.name] = [
                        tuple(row) for row in await db.execute(
                            select(table).order_by(*table.primary_key.columns)
                        )
                    ]
                return result

        async def unlocked(claim):
            async with factory.begin() as db:
                assert await db.scalar(select(
                    func.pg_try_advisory_xact_lock(STORE_ADMISSION_LOCK)
                )) is True
                await db.execute(
                    select(StoreOperation)
                    .where(StoreOperation.id == claim.operation_id)
                    .with_for_update(nowait=True)
                )

        claim = await fresh()
        before = await snapshot()
        results = await asyncio.wait_for(asyncio.gather(*(
            guard.check_claim(factory, claim) for _ in range(8)
        )), 20)
        for result in results:
            assert result is not None and result.namespace_uid is None
            assert result.claim.lease_token == claim.lease_token
            assert result.claim.lease_expires_at == claim.lease_expires_at
            assert result.claim.deadline_at == claim.deadline_at
            assert 0 < result.remaining_seconds <= 60
            assert str(claim.lease_token) not in repr(result)
        assert await snapshot() == before
        await unlocked(claim)
        print("Concurrent checks preserve rows, leases and deadlines; locks close before return")

        uid = str(uuid4())
        await change(Store, claim.store_id, namespace_uid=uid)
        before = await snapshot()
        result = await guard.require_claim(factory, claim, expected_namespace_uid=uid)
        assert result.namespace_uid == uid
        assert await guard.check_claim(factory, claim, expected_namespace_uid=None) is None
        assert await guard.check_claim(factory, claim, expected_namespace_uid="other") is None
        for invalid in (
            replace(claim, operation_id=uuid4()), replace(claim, lease_token=uuid4()),
            replace(claim, attempt=99), replace(claim, generation=99),
            replace(claim, owner_id=98702),
            replace(claim, deadline_at=claim.deadline_at + timedelta(seconds=1)),
        ):
            assert await guard.check_claim(factory, invalid) is None
        forged = replace(
            claim, checked_at=claim.checked_at + timedelta(days=1),
            lease_expires_at=claim.lease_expires_at + timedelta(days=1),
        )
        result = await guard.require_claim(factory, forged)
        assert result.claim.lease_expires_at == claim.lease_expires_at
        assert result.claim.checked_at < forged.checked_at
        assert await snapshot() == before

        await change(
            StoreOperation, claim.operation_id,
            lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
        )
        recovered = await jobs.claim_next(factory)
        assert recovered is not None and recovered.lease_token != claim.lease_token
        assert recovered.deadline_at == claim.deadline_at
        before = await snapshot()
        assert await guard.check_claim(factory, claim) is None
        await expect_error(guard.StoreJobClaimLost, guard.require_claim(factory, claim))
        assert (await guard.require_claim(factory, recovered)).namespace_uid == uid
        assert await snapshot() == before
        print("UID expectations, forged snapshots and old claims after recovery are checked")

        claim = await fresh()
        result = await guard.require_claim(
            factory, claim, expected_credentials_secret_uid=None,
        )
        assert result.credentials_secret_uid is None
        assert await guard.check_claim(
            factory, claim, expected_credentials_secret_uid="not-bound",
        ) is None
        before = await snapshot()
        for invalid in ("", "invalid uid", "x" * 257, 123, True):
            await expect_error(
                ValueError,
                guard.check_claim(
                    factory, claim, expected_credentials_secret_uid=invalid,
                ),
            )
        assert await snapshot() == before
        uid, secret_uid = str(uuid4()), str(uuid4())
        await change(
            Store, claim.store_id,
            namespace_uid=uid, credentials_secret_uid=secret_uid,
        )
        before = await snapshot()
        results = await asyncio.gather(*(
            guard.require_claim(
                factory, claim, expected_namespace_uid=uid,
                expected_credentials_secret_uid=secret_uid,
            )
            for _ in range(8)
        ))
        assert all(result.credentials_secret_uid == secret_uid for result in results)
        assert all(result.claim.deadline_at == claim.deadline_at for result in results)
        assert await snapshot() == before
        await unlocked(claim)
        for expected in (None, "another-secret"):
            assert await guard.check_claim(
                factory, claim, expected_credentials_secret_uid=expected,
            ) is None
        await expect_error(
            guard.StoreJobClaimLost,
            guard.require_claim(
                factory, claim, expected_credentials_secret_uid="another-secret",
            ),
        )
        replacement = str(uuid4())
        await change(Store, claim.store_id, credentials_secret_uid=replacement)
        assert await guard.check_claim(
            factory, claim, expected_credentials_secret_uid=secret_uid,
        ) is None
        assert (
            await guard.require_claim(
                factory, claim, expected_credentials_secret_uid=replacement,
            )
        ).credentials_secret_uid == replacement
        await change(Store, claim.store_id, credentials_secret_uid=None)
        assert await guard.check_claim(
            factory, claim, expected_credentials_secret_uid=replacement,
        ) is None
        assert (
            await guard.require_claim(
                factory, claim, expected_credentials_secret_uid=None,
            )
        ).credentials_secret_uid is None
        await change(Store, claim.store_id, credentials_secret_uid="invalid secret uid")
        error = await expect_error(
            guard.StoreJobGuardUnavailable, guard.check_claim(factory, claim),
        )
        assert "invalid secret uid" not in str(error)
        print("Secret UID observations preserve data; unbound, replaced and malformed bindings are checked")

        claim = await fresh(action="delete", seconds=-1)
        uid, secret_uid = str(uuid4()), str(uuid4())
        await change(
            Store, claim.store_id,
            namespace_uid=uid, credentials_secret_uid=secret_uid,
        )
        before = await snapshot()
        result = await guard.require_claim(
            factory, claim, for_cleanup=True,
            expected_namespace_uid=uid, expected_credentials_secret_uid=secret_uid,
        )
        assert result.for_cleanup and result.credentials_secret_uid == secret_uid
        assert await guard.check_claim(
            factory, claim, for_cleanup=True, expected_credentials_secret_uid=None,
        ) is None
        assert await snapshot() == before
        await unlocked(claim)
        print("Overdue cleanup checks Secret ownership without extending its original deadline")

        for values in (
            {"owner_id": 98702}, {"namespace": "unmanaged"}, {"release_name": "other"},
            {"name": "Changed"}, {"slug": "changed"}, {"url": "https://changed.invalid"},
            {"generation": 2}, {"status": "Ready"}, {"desired_state": "absent"},
            {"deleted_at": func.clock_timestamp()},
        ):
            claim = await fresh()
            await change(Store, claim.store_id, **values)
            before = await snapshot()
            assert await guard.check_claim(factory, claim) is None
            assert await snapshot() == before
        claim = await fresh()
        await change(Store, claim.store_id, namespace_uid="invalid uid")
        error = await expect_error(
            guard.StoreJobGuardUnavailable, guard.check_claim(factory, claim),
        )
        assert "invalid uid" not in str(error)
        print("Changed ownership, identity, generations and terminal states reject old work")

        for action in ("provision", "delete"):
            claim = await fresh(action=action, seconds=-1)
            before = await snapshot()
            if action == "delete":
                await expect_error(ValueError, guard.check_claim(factory, claim))
            else:
                assert await guard.check_claim(factory, claim) is None
            result = await guard.require_claim(factory, claim, for_cleanup=True)
            assert result.for_cleanup and result.claim.deadline_at == claim.deadline_at
            assert await snapshot() == before
            await change(
                StoreOperation, claim.operation_id,
                lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
            )
            assert await guard.check_claim(factory, claim, for_cleanup=True) is None
        print("Overdue provisioning stops; cleanup still requires a live lease")

        normal_execute = AsyncSession.execute
        for field in ("lease", "deadline"):
            claim = await fresh(seconds=3 if field == "deadline" else 1200)
            task = None
            entered = asyncio.Event()

            async def observe_wait(db, statement, *args, **kwargs):
                if (
                    asyncio.current_task() is task
                    and getattr(statement, "_for_update_arg", None) is not None
                ):
                    entered.set()
                return await normal_execute(db, statement, *args, **kwargs)

            try:
                with patch.object(AsyncSession, "execute", observe_wait):
                    async with factory.begin() as blocker:
                        if field == "lease":
                            cutoff = await blocker.scalar(select(func.clock_timestamp()))
                            cutoff += timedelta(seconds=1)
                            await blocker.execute(
                                update(StoreOperation).where(StoreOperation.id == claim.operation_id)
                                .values(lease_expires_at=cutoff)
                            )
                        else:
                            cutoff = claim.deadline_at
                            await blocker.execute(
                                select(Store).where(Store.id == claim.store_id).with_for_update()
                            )
                        task = asyncio.create_task(guard.check_claim(factory, claim))
                        await asyncio.wait_for(entered.wait(), 5)
                        assert not task.done()
                        now = await blocker.scalar(select(func.clock_timestamp()))
                        await asyncio.sleep(max(0, (cutoff - now).total_seconds()) + 0.1)
                    assert await asyncio.wait_for(task, 10) is None
            finally:
                if task is not None:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            await unlocked(claim)
        print("Real row-lock waits cannot revive an expired lease or provisioning deadline")

        claim = await fresh()
        before = await snapshot()
        normal_locked = guard._locked_claim
        for failure in (SQLAlchemyError("private diagnostic"), asyncio.CancelledError()):
            async def interrupt(db, candidate):
                await normal_locked(db, candidate)
                raise failure

            with patch.object(guard, "_locked_claim", interrupt):
                expected = (
                    asyncio.CancelledError if isinstance(failure, asyncio.CancelledError)
                    else guard.StoreJobGuardUnavailable
                )
                error = await expect_error(expected, guard.check_claim(factory, claim))
                assert "private diagnostic" not in str(error)
            await unlocked(claim)
            assert await snapshot() == before
        unsupported = async_sessionmaker(
            isolated.execution_options(isolation_level="REPEATABLE READ"),
            expire_on_commit=False,
        )
        await expect_error(
            guard.StoreJobGuardUnavailable, guard.check_claim(unsupported, claim),
        )
        await unlocked(claim)
        assert await snapshot() == before
        print("Storage failures, unsupported isolation and cancellation release locks safely")

        normal_close = AsyncSession.close
        clock = [100.0]

        async def late_close(db):
            await normal_close(db)
            clock[0] += 120

        with patch.object(guard, "time", SimpleNamespace(monotonic=lambda: clock[0])), patch.object(
            AsyncSession, "close", late_close,
        ):
            assert await guard.check_claim(factory, claim) is None
        await unlocked(claim)
        assert await snapshot() == before
        assert isolated.pool.checkedout() == 0
        print("Time spent completing the transaction cannot extend the local allowance")
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from store_guarded_postgres import verify_guarded_requests
        await verify_guarded_requests(factory, fresh, change, snapshot, unlocked)
        assert isolated.pool.checkedout() == 0
        from store_secret_postgres import verify_secret_stage
        await verify_secret_stage(factory, fresh, change, snapshot, unlocked)
        assert isolated.pool.checkedout() == 0
        from store_lease_postgres import verify_lease_runner
        await verify_lease_runner(factory, fresh, change, snapshot, unlocked)
        assert isolated.pool.checkedout() == 0
        from store_worker_postgres import verify_worker
        await verify_worker(factory)
        assert isolated.pool.checkedout() == 0
        print("Job guard verification passed. Live accounts, stores and settings were unchanged.")
        print("No Kubernetes, model or WordPress calls were made.")
    finally:
        try:
            if isolated is not None:
                await isolated.dispose()
        finally:
            try:
                if created:
                    assert schema.startswith("urumi_guard_test_") and len(schema) < 64
                    async with engine.begin() as connection:
                        await connection.execute(DropSchema(schema, cascade=True))
                    print("Temporary PostgreSQL schema removed.")
            finally:
                await engine.dispose()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 180))
