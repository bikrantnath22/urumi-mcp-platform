"""Verify namespace UID bindings in an isolated PostgreSQL schema."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

if not Path("app/store_namespace_binding.py").is_file():
    raise SystemExit("Run from Wo_MCP/apps/gateway.")
sys.path.insert(0, str(Path.cwd()))

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app import store_jobs as jobs
from app import store_namespace_binding as binding
from app.config import database_url
from app.database import engine
from app.models import (
    Base, SecurityEvent, Store, StoreOperation, StoreProvisioningSettings, User,
)
from app.store_namespace import StoreNamespaceError, namespace_for
from app.store_settings import STORE_ADMISSION_LOCK


OWNER_A, OWNER_B = 98701, 98702


async def check():
    schema = "urumi_namespace_bind_test_" + uuid4().hex
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
            for owner in (OWNER_A, OWNER_B):
                db.add(User(
                    id=owner, email=f"owner-{owner}@fixture.invalid",
                    role="member", active=True, password_hash="fixture-only",
                ))

        async def reset():
            async with factory.begin() as db:
                for model in (SecurityEvent, StoreOperation, Store):
                    await db.execute(delete(model))

        async def fresh(owner=OWNER_A, *, action="provision", seconds=1200):
            identity = uuid4()
            async with factory.begin() as db:
                now = await db.scalar(select(func.clock_timestamp()))
                db.add(Store(
                    id=identity, owner_id=owner, name="Fixture tea",
                    slug="tea-" + identity.hex[:16],
                    namespace="urumi-store-" + identity.hex, release_name="store",
                    url=f"http://tea-{identity.hex[:16]}.localtest.me",
                    status="Provisioning" if action == "provision" else "Deleting",
                    desired_state="present" if action == "provision" else "absent",
                    generation=1,
                ))
                await db.flush()
                db.add(StoreOperation(
                    id=uuid4(), store_id=identity, requested_by_id=owner,
                    request_key=uuid4(), request_fingerprint="a" * 64,
                    action=action, store_generation=1, status="Pending", attempts=0,
                    available_at=now - timedelta(seconds=1),
                    deadline_at=now + timedelta(seconds=seconds),
                ))
            claim = await jobs.claim_next(factory)
            assert claim is not None and claim.store_id == identity
            return claim

        def document(claim, uid=None):
            data = namespace_for(claim).manifest()
            data["metadata"].update(
                uid=uid or str(uuid4()), resourceVersion="opaque-version-7",
            )
            data["status"] = {"phase": "Active"}
            return data

        async def change(model, identity, **values):
            async with factory.begin() as db:
                await db.execute(
                    update(model).where(model.id == identity).values(**values)
                )

        async def snapshot():
            async with factory() as db:
                result = {}
                for model in (
                    Store, StoreOperation, SecurityEvent, User,
                    StoreProvisioningSettings,
                ):
                    table = model.__table__
                    result[table.name] = [
                        tuple(row) for row in await db.execute(
                            select(table).order_by(*table.primary_key.columns)
                        )
                    ]
                return result

        async def expect_error(expected, call, message=None):
            try:
                await call
            except expected as error:
                if message is not None:
                    assert str(error) == message
                return error
            raise AssertionError(f"Expected {expected.__name__}.")

        async def bind(claim, data, **options):
            return await binding.bind_namespace(factory, claim, data, **options)

        async def wait_past(db, instant):
            now = await db.scalar(select(func.clock_timestamp()))
            await asyncio.sleep(max(0, (instant - now).total_seconds()) + 0.1)
            assert await db.scalar(select(func.clock_timestamp())) >= instant

        async def cancel_tasks(*tasks):
            pending = [task for task in tasks if task is not None]
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        await reset()
        claim = await fresh()
        data = document(claim)
        uid = data["metadata"]["uid"]
        before = await snapshot()
        results = await asyncio.wait_for(
            asyncio.gather(*(bind(claim, deepcopy(data)) for _ in range(8))), 25,
        )
        assert results == [uid] * 8
        async with factory() as db:
            store = await db.get(Store, claim.store_id)
            events = (await db.scalars(select(SecurityEvent))).all()
            assert store.namespace_uid == uid and store.status == "Provisioning"
            assert len(events) == 1
            event = events[0]
            assert event.actor_id is None and event.target_user_id == OWNER_A
            assert event.action == "store.namespace_bound"
            assert event.details == {
                "store_id": str(claim.store_id),
                "operation_id": str(claim.operation_id),
                "generation": 1,
                "requested_by_id": OWNER_A,
                "namespace_uid": uid,
                "for_cleanup": False,
            }
        after = await snapshot()
        assert before["store_operations"] == after["store_operations"]
        assert await bind(claim, data) == uid
        assert await snapshot() == after
        await expect_error(StoreNamespaceError, bind(claim, document(claim)))
        assert await snapshot() == after
        print("Eight concurrent retries bind once; replay preserves the UID, audit and deadline")

        await change(
            StoreOperation, claim.operation_id,
            lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
        )
        recovered = await jobs.claim_next(factory)
        assert recovered is not None and recovered.lease_token != claim.lease_token
        assert recovered.attempt == claim.attempt + 1
        before = await snapshot()
        for invalid in (
            claim, replace(recovered, lease_token=uuid4()),
            replace(recovered, operation_id=uuid4()),
            replace(recovered, attempt=99), replace(recovered, generation=99),
        ):
            assert await bind(invalid, data) is None
        wrong_store = uuid4()
        invalid = replace(
            recovered, store_id=wrong_store,
            namespace="urumi-store-" + wrong_store.hex,
        )
        assert await bind(invalid, document(invalid, uid)) is None
        assert await bind(recovered, data) == uid
        assert await snapshot() == before
        print("Recovery reuses the saved UID; expired and forged claims cannot change it")

        await reset()
        first, second = await fresh(), await fresh(OWNER_B)
        first_data = document(first)
        uid = first_data["metadata"]["uid"]
        assert await bind(first, first_data) == uid
        before = await snapshot()
        await expect_error(
            StoreNamespaceError, bind(second, document(second, uid)),
        )
        assert await snapshot() == before
        await expect_error(
            IntegrityError, change(Store, second.store_id, namespace_uid=uid),
        )
        for invalid_uid in ("", "x" * 257):
            await expect_error(
                SQLAlchemyError,
                change(Store, second.store_id, namespace_uid=invalid_uid),
            )
        await change(
            Store, first.store_id, status="Deleted", desired_state="absent",
            deleted_at=func.clock_timestamp(),
        )
        before = await snapshot()
        await expect_error(
            StoreNamespaceError, bind(second, document(second, uid)),
        )
        assert await snapshot() == before
        second_uid = str(uuid4())
        assert await bind(second, document(second, second_uid)) == second_uid
        print("Stores keep separate UIDs; database constraints and deleted-store bindings prevent reuse")

        for values in (
            {"owner_id": OWNER_B}, {"namespace": "unmanaged"},
            {"release_name": "another"}, {"name": "Changed"},
            {"slug": "changed"}, {"url": "https://changed.invalid"},
        ):
            await reset()
            claim = await fresh()
            data = document(claim)
            await change(Store, claim.store_id, **values)
            before = await snapshot()
            await expect_error(StoreNamespaceError, bind(claim, data))
            assert await snapshot() == before
        await reset()
        claim = await fresh()
        before = await snapshot()
        await expect_error(
            StoreNamespaceError,
            bind(replace(claim, deadline_at=claim.deadline_at + timedelta(seconds=1)),
                 document(claim)),
        )
        assert await snapshot() == before
        for values in (
            {"generation": 2}, {"status": "Ready"},
            {"desired_state": "absent"}, {"deleted_at": func.clock_timestamp()},
        ):
            await reset()
            claim = await fresh()
            data = document(claim)
            await change(Store, claim.store_id, **values)
            before = await snapshot()
            assert await bind(claim, data) is None
            assert await snapshot() == before
        print("Changed store identity, deadlines, generations and terminal states reject binding")

        for action in ("provision", "delete"):
            await reset()
            claim = await fresh(action=action, seconds=-1)
            data = document(claim)
            data["metadata"]["deletionTimestamp"] = "2026-10-17T00:00:00Z"
            before = await snapshot()
            if action == "delete":
                await expect_error(ValueError, bind(claim, data))
            else:
                await expect_error(StoreNamespaceError, bind(claim, data))
                active = document(claim, data["metadata"]["uid"])
                assert await bind(claim, active) is None
            assert await snapshot() == before
            assert await bind(claim, data, for_cleanup=True) == data["metadata"]["uid"]
            after = await snapshot()
            assert before["store_operations"] == after["store_operations"]
        print("Overdue and terminating namespaces require explicit cleanup; original deadlines stay fixed")

        normal_flush = AsyncSession.flush
        for mode in ("storage", "constraint", "lease", "deadline"):
            await reset()
            claim = await fresh(seconds=3 if mode == "deadline" else 1200)
            data = document(claim)
            other = await fresh(OWNER_B) if mode == "constraint" else None
            if mode == "lease":
                await change(
                    StoreOperation, claim.operation_id,
                    lease_expires_at=func.clock_timestamp() + timedelta(seconds=2),
                )
            before = await snapshot()

            async def interrupted_flush(db, *args, **kwargs):
                await normal_flush(db, *args, **kwargs)
                if mode == "storage":
                    raise SQLAlchemyError("fixture-only private diagnostic")
                if mode == "constraint":
                    await db.execute(
                        update(Store).where(Store.id == other.store_id)
                        .values(namespace_uid=data["metadata"]["uid"])
                    )
                else:
                    field = (
                        StoreOperation.lease_expires_at if mode == "lease"
                        else StoreOperation.deadline_at
                    )
                    cutoff = await db.scalar(
                        select(field).where(StoreOperation.id == claim.operation_id)
                    )
                    await wait_past(db, cutoff)

            with patch.object(AsyncSession, "flush", interrupted_flush):
                if mode == "storage":
                    await expect_error(
                        binding.NamespaceBindingUnavailable, bind(claim, data),
                        "Namespace ownership storage is unavailable.",
                    )
                elif mode == "constraint":
                    await expect_error(
                        StoreNamespaceError, bind(claim, data),
                        "The namespace binding conflicts with an existing record.",
                    )
                else:
                    assert await bind(claim, data) is None
            assert await snapshot() == before
        print("Post-flush failures and lease/deadline expiry roll back UID and event together")

        normal_execute = AsyncSession.execute
        for field in ("lease", "deadline"):
            await reset()
            claim = await fresh(seconds=3 if field == "deadline" else 1200)
            data = document(claim)
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
                                update(StoreOperation)
                                .where(StoreOperation.id == claim.operation_id)
                                .values(lease_expires_at=cutoff)
                            )
                        else:
                            cutoff = claim.deadline_at
                            await blocker.execute(
                                select(Store).where(Store.id == claim.store_id)
                                .with_for_update()
                            )
                        task = asyncio.create_task(bind(claim, data))
                        await asyncio.wait_for(entered.wait(), 5)
                        assert not task.done()
                        await wait_past(blocker, cutoff)
                    assert await asyncio.wait_for(task, 10) is None
                async with factory() as db:
                    assert (await db.get(Store, claim.store_id)).namespace_uid is None
                    assert await db.scalar(
                        select(func.count()).select_from(SecurityEvent)
                    ) == 0
            finally:
                await cancel_tasks(task)
        print("Real row-lock waits cannot revive an elapsed lease or provisioning deadline")

        for cancel in (False, True):
            await reset()
            claim = await fresh()
            data = document(claim)
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
                    task = asyncio.create_task(bind(claim, data))
                    await asyncio.wait_for(paused.wait(), 5)
                    async with factory.begin() as probe:
                        assert await probe.scalar(select(
                            func.pg_try_advisory_xact_lock(STORE_ADMISSION_LOCK)
                        )) is False
                    assert await snapshot() == before
                    if cancel:
                        task.cancel()
                        await expect_error(asyncio.CancelledError, task)
                    else:
                        competing = asyncio.create_task(bind(claim, deepcopy(data)))
                        resume.set()
                        results = await asyncio.wait_for(
                            asyncio.gather(task, competing), 15,
                        )
                        assert results == [data["metadata"]["uid"]] * 2
            finally:
                resume.set()
                await cancel_tasks(task, competing)
            if cancel:
                assert await snapshot() == before
                assert await bind(claim, data) == data["metadata"]["uid"]
            async with factory() as db:
                assert await db.scalar(
                    select(func.count()).select_from(SecurityEvent)
                ) == 1
        print("The admission gate lasts through commit; partial work stays hidden and cancellation rolls back")
        print("Namespace binding verification passed. Live accounts/stores/settings were unchanged.")
        print("No Kubernetes, model or WordPress calls were made.")

    finally:
        try:
            if isolated is not None:
                await isolated.dispose()
        finally:
            try:
                if created:
                    assert schema.startswith("urumi_namespace_bind_test_")
                    assert len(schema) < 64
                    async with engine.begin() as connection:
                        await connection.execute(DropSchema(schema, cascade=True))
                    print("Temporary PostgreSQL schema removed.")
            finally:
                await engine.dispose()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 180))
