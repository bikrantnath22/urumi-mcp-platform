"""Verify store completion in a disposable PostgreSQL schema."""

import asyncio
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

if not Path("app/store_completion.py").is_file():
    raise SystemExit("Run from Wo_MCP/apps/gateway.")
sys.path.insert(0, str(Path.cwd()))

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app import store_completion as completion
from app import store_jobs as jobs
from app.config import database_url
from app.database import engine
from app.models import (
    AuditLog, Base, MCPServer, SecurityEvent, Store, StoreOperation,
    StoreProvisioningSettings, StoreVolume, ToolGrant, User,
)
from app.store_settings import STORE_ADMISSION_LOCK


OWNER_A, OWNER_B = 98301, 98302


async def check():
    schema = "urumi_completion_test_" + uuid4().hex
    created, isolated = False, None
    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
        created = True

        isolated = create_async_engine(
            database_url(), pool_size=6, max_overflow=0, pool_timeout=5,
            hide_parameters=True,
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
            db.add(StoreProvisioningSettings(id=1, max_concurrent_creations=1))
            for owner in (OWNER_A, OWNER_B):
                db.add(User(
                    id=owner, email=f"owner-{owner}@fixture.invalid",
                    role="member", active=True, password_hash="fixture-only",
                ))

        async def reset():
            async with factory.begin() as db:
                for model in (
                    ToolGrant, AuditLog, SecurityEvent, StoreVolume, StoreOperation, Store, MCPServer,
                ):
                    await db.execute(delete(model))
                db.add(MCPServer(
                    slug="notes", name="Unrelated fixture server",
                    url="http://mcp-notes.platform.svc.cluster.local:8000/mcp",
                    enabled=True, status="Connected", timeout_s=15,
                ))

        async def seed(*, action="provision", registrations=2, overdue=False, owner=OWNER_A):
            async with factory.begin() as db:
                now = await db.scalar(select(func.clock_timestamp()))
                identity = uuid4()
                namespace = "urumi-store-" + identity.hex
                servers = []
                for service, prefix in (
                    ("woo-mcp", "w"), ("mcp-analytics", "a"),
                )[:registrations]:
                    server = MCPServer(
                        slug=prefix + "-" + identity.hex[:24],
                        name="Fixture " + service,
                        url=f"http://{service}.{namespace}.svc.cluster.local:8000/mcp",
                        enabled=False, status="Disabled", timeout_s=15,
                        status_reason="Waiting for fixture verification.",
                    )
                    db.add(server)
                    servers.append(server)
                await db.flush()
                store = Store(
                    id=identity, owner_id=owner, name="Fixture store",
                    slug="tea-" + identity.hex[:16], namespace=namespace,
                    release_name="store",
                    url=f"http://tea-{identity.hex[:16]}.localtest.me",
                    status="Provisioning" if action == "provision" else "Deleting",
                    desired_state="present" if action == "provision" else "absent",
                    generation=1,
                    mcp_server_id=servers[0].id if servers else None,
                    analytics_mcp_server_id=servers[1].id if len(servers) == 2 else None,
                )
                db.add(store)
                await db.flush()
                operation = StoreOperation(
                    id=uuid4(), store_id=identity, requested_by_id=owner,
                    request_key=uuid4(), request_fingerprint="a" * 64,
                    action=action, store_generation=1, status="Pending", attempts=0,
                    available_at=now - timedelta(seconds=1),
                    deadline_at=now + timedelta(seconds=-1 if overdue else 1200),
                )
                db.add(operation)
                for server in servers:
                    db.add(ToolGrant(
                        user_id=owner, server_id=server.id,
                        tool_name="fixture_read", granted_by_id=None,
                    ))
                    db.add(AuditLog(
                        user_id=owner, server_id=server.id, server_slug=server.slug,
                        tool="fixture_read", duration_ms=1,
                        outcome="success", source="mcp",
                    ))
                await db.flush()
                return identity, operation.id, tuple(server.id for server in servers)

        async def owned(**kwargs):
            identity, _, ids = await seed(**kwargs)
            claim = await jobs.claim_next(factory)
            assert claim is not None and claim.store_id == identity
            return claim, ids

        async def change(model, identity, **values):
            async with factory.begin() as db:
                await db.execute(update(model).where(model.id == identity).values(**values))

        async def finish(kind, claim):
            if kind == "ready":
                return await completion.finish_ready(factory, claim)
            if kind == "failed":
                return await completion.finish_failed(factory, claim, reason="bootstrap")
            return await completion.finish_deleted(factory, claim)

        async def expect_error(expected, operation):
            try:
                await operation
            except expected:
                return
            raise AssertionError(f"Expected {expected.__name__}.")

        async def snapshot():
            async with factory() as db:
                data = {}
                for model in (
                    Store, StoreOperation, MCPServer, ToolGrant, AuditLog, SecurityEvent, StoreVolume,
                ):
                    table = model.__table__
                    rows = await db.execute(
                        select(table).order_by(*table.primary_key.columns)
                    )
                    data[table.name] = [tuple(row) for row in rows]
                return data

        async def inspect_terminal(claim, *, state, operation_status, event_action, ids):
            async with factory() as db:
                store = await db.get(Store, claim.store_id)
                operation = await db.get(StoreOperation, claim.operation_id)
                assert store.status == state and operation.status == operation_status
                assert operation.deadline_at == claim.deadline_at
                assert operation.lease_token is None and operation.lease_expires_at is None
                assert operation.finished_at is not None
                events = (await db.scalars(select(SecurityEvent))).all()
                assert len(events) == 1
                event = events[0]
                assert event.actor_id is None and event.target_user_id == OWNER_A
                assert event.action == event_action
                assert event.details == {
                    "store_id": str(claim.store_id),
                    "operation_id": str(claim.operation_id),
                    "generation": claim.generation,
                    "requested_by_id": OWNER_A,
                }
                assert await db.scalar(
                    select(func.count()).select_from(MCPServer).where(MCPServer.slug == "notes")
                ) == 1
                audits = (await db.scalars(select(AuditLog))).all()
                assert len(audits) == len(ids)
                if state == "Ready":
                    assert store.deleted_at is None and store.status_reason is None
                    assert operation.last_error is None
                    assert {store.mcp_server_id, store.analytics_mcp_server_id} == set(ids)
                    servers = (await db.scalars(
                        select(MCPServer).where(MCPServer.id.in_(ids))
                    )).all()
                    assert len(servers) == 2
                    assert all(
                        server.enabled and server.status == "Connected"
                        and server.last_checked_at is not None and server.status_reason is None
                        for server in servers
                    )
                    assert {audit.server_id for audit in audits} == set(ids)
                    assert await db.scalar(select(func.count()).select_from(ToolGrant)) == 2
                else:
                    assert store.mcp_server_id is None and store.analytics_mcp_server_id is None
                    assert await db.scalar(select(func.count()).select_from(MCPServer)) == 1
                    assert await db.scalar(select(func.count()).select_from(ToolGrant)) == 0
                    assert all(audit.server_id is None and audit.server_slug for audit in audits)
                    if state == "Deleted":
                        assert store.deleted_at is not None and store.status_reason is None
                        assert operation.last_error is None
                    else:
                        assert store.deleted_at is None
                        assert store.status_reason == operation.last_error

        await reset()
        claim, ids = await owned()
        assert await completion.finish_ready(factory, claim)
        await inspect_terminal(
            claim, state="Ready", operation_status="Succeeded",
            event_action="store.provisioned", ids=ids,
        )
        committed = await snapshot()
        assert await completion.finish_ready(factory, claim) is False
        assert await completion.finish_failed(factory, claim, reason="bootstrap") is False
        assert await snapshot() == committed
        async with factory.begin() as db:
            for model, identity in (
                (Store, claim.store_id), (StoreOperation, claim.operation_id),
            ):
                await db.execute(
                    select(model).where(model.id == identity).with_for_update(nowait=True)
                )
        print("Ready commits both scoped registrations, the operation and one worker event; replay changes nothing")

        for count in (0, 1, 2):
            for reason in completion.FAILURE_REASONS:
                await reset()
                claim, ids = await owned(registrations=count, overdue=(reason == "timeout"))
                assert await completion.finish_failed(factory, claim, reason=reason)
                await inspect_terminal(
                    claim, state="Failed", operation_status="Failed",
                    event_action="store.provision_failed", ids=ids,
                )
                async with factory() as db:
                    operation = await db.get(StoreOperation, claim.operation_id)
                    assert operation.last_error == completion.FAILURE_REASONS[reason]
                    assert await db.scalar(
                        select(func.count()).select_from(Store).where(Store.status != "Deleted")
                    ) == 1
                committed = await snapshot()
                assert await completion.finish_failed(factory, claim, reason=reason) is False
                assert await snapshot() == committed
        print("Cleaned-up failures handle zero, one or both registrations; fixed reasons persist and Failed stores retain quota")

        for count in (0, 1, 2):
            await reset()
            claim, ids = await owned(action="delete", registrations=count, overdue=True)
            assert await completion.finish_deleted(factory, claim)
            await inspect_terminal(
                claim, state="Deleted", operation_status="Succeeded",
                event_action="store.deleted", ids=ids,
            )
            committed = await snapshot()
            assert await completion.finish_deleted(factory, claim) is False
            assert await snapshot() == committed
        print("Verified deletion may finish after its deadline; grants disappear, audits survive and unrelated servers remain")

        def no_storage():
            raise AssertionError("Invalid arguments reached storage.")

        await expect_error(
            ValueError, completion.finish_ready(no_storage, replace(claim, action="delete")),
        )
        await expect_error(
            ValueError, completion.finish_deleted(no_storage, replace(claim, action="provision")),
        )
        for reason in ("raw-token-or-error", "", None, True, {}):
            await expect_error(
                ValueError,
                completion.finish_failed(
                    no_storage, replace(claim, action="provision"), reason=reason,
                ),
            )

        await reset()
        claim, _ = await owned()
        before = await snapshot()
        await expect_error(
            completion.StoreCompletionConflict,
            completion.finish_failed(factory, claim, reason="timeout"),
        )
        assert await snapshot() == before
        print("Wrong actions and raw diagnostic strings are rejected; timeout cannot be declared before the original deadline")

        for mutation in (
            {"namespace": "urumi-store-" + uuid4().hex},
            {"release_name": "unexpected"},
            {"owner_id": OWNER_B},
            {"url": "http://changed.localtest.me"},
            {"analytics_mcp_server_id": None},
        ):
            await reset()
            claim, _ = await owned()
            await change(Store, claim.store_id, **mutation)
            before = await snapshot()
            await expect_error(
                completion.StoreCompletionConflict, completion.finish_ready(factory, claim),
            )
            assert await snapshot() == before
        for kind in ("ready", "failed", "deleted"):
            await reset()
            claim, ids = await owned(action="delete" if kind == "deleted" else "provision")
            await change(MCPServer, ids[0], url="http://unexpected.invalid/mcp")
            before = await snapshot()
            await expect_error(completion.StoreCompletionConflict, finish(kind, claim))
            assert await snapshot() == before

            await reset()
            claim, ids = await owned(action="delete" if kind == "deleted" else "provision")
            other_id, _, _ = await seed(registrations=0, owner=OWNER_B)
            await change(Store, other_id, analytics_mcp_server_id=ids[0])
            before = await snapshot()
            await expect_error(completion.StoreCompletionConflict, finish(kind, claim))
            assert await snapshot() == before
        print("Changed ownership, namespace, release, URL, missing Ready links and cross-store registrations reject completion")

        for kind in ("ready", "failed", "deleted"):
            await reset()
            claim, _ = await owned(action="delete" if kind == "deleted" else "provision")
            for forged in (
                replace(claim, lease_token=uuid4()),
                replace(claim, operation_id=uuid4()),
                replace(claim, store_id=uuid4()),
                replace(claim, attempt=999),
                replace(claim, generation=999),
            ):
                before = await snapshot()
                assert await finish(kind, forged) is False
                assert await snapshot() == before
            await change(
                StoreOperation, claim.operation_id,
                lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
            )
            before = await snapshot()
            assert await finish(kind, claim) is False
            assert await snapshot() == before
            recovered = await jobs.claim_next(factory)
            assert recovered is not None and recovered.lease_token != claim.lease_token
            assert await finish(kind, claim) is False
            assert await finish(kind, recovered)
        await reset()
        claim, _ = await owned(overdue=True)
        before = await snapshot()
        assert await completion.finish_ready(factory, claim) is False
        assert await snapshot() == before
        print("Forged, expired and replaced claims cannot finish work; an overdue store cannot become Ready")

        normal_flush = AsyncSession.flush
        for kind in ("ready", "failed", "deleted"):
            await reset()
            claim, _ = await owned(action="delete" if kind == "deleted" else "provision")
            before = await snapshot()

            async def failed_flush(db, *args, **kwargs):
                await normal_flush(db, *args, **kwargs)
                if any(
                    isinstance(item, StoreOperation)
                    and item.id == claim.operation_id
                    and item.status in ("Succeeded", "Failed")
                    for item in db.identity_map.values()
                ):
                    raise SQLAlchemyError("fixture-only failure after terminal flush")

            with patch.object(AsyncSession, "flush", failed_flush):
                await expect_error(SQLAlchemyError, finish(kind, claim))
            assert await snapshot() == before
            assert await finish(kind, claim)
        print("Post-flush failures roll back statuses, registration deletion, grant cascades and audit changes together")

        for kind, expiry_field in (
            ("ready", "deadline_at"), ("ready", "lease_expires_at"),
            ("failed", "lease_expires_at"), ("deleted", "lease_expires_at"),
        ):
            await reset()
            claim, ids = await owned(action="delete" if kind == "deleted" else "provision")
            await change(
                StoreOperation, claim.operation_id,
                **{expiry_field: func.clock_timestamp() + timedelta(seconds=2)},
            )
            waiting = asyncio.Event()
            normal_execute = AsyncSession.execute
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
                    await holder.execute(
                        select(MCPServer).where(MCPServer.id == ids[0]).with_for_update()
                    )
                    with patch.object(AsyncSession, "execute", observed_execute):
                        task = asyncio.create_task(finish(kind, claim))
                        await asyncio.wait_for(waiting.wait(), 5)
                        await asyncio.sleep(2.1)
                        assert not task.done()
                assert await asyncio.wait_for(task, 5) is False
                async with factory() as db:
                    operation = await db.get(StoreOperation, claim.operation_id)
                    assert operation.status == "Running"
                    assert await db.scalar(
                        select(func.count()).select_from(SecurityEvent)
                    ) == 0
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        print("Deadlines and leases are rechecked after real registration-row lock waits")

        for kind in ("ready", "failed", "deleted"):
            await reset()
            claim, _ = await owned(action="delete" if kind == "deleted" else "provision")
            next_store, _, _ = await seed(owner=OWNER_B)
            paused, resume = asyncio.Event(), asyncio.Event()
            task = competing = None

            async def hold_flush(db, *args, **kwargs):
                await normal_flush(db, *args, **kwargs)
                if asyncio.current_task() is task and any(
                    isinstance(item, StoreOperation)
                    and item.id == claim.operation_id
                    and item.status in ("Succeeded", "Failed")
                    for item in db.identity_map.values()
                ):
                    paused.set()
                    await asyncio.wait_for(resume.wait(), 10)

            try:
                with patch.object(AsyncSession, "flush", hold_flush):
                    task = asyncio.create_task(finish(kind, claim))
                    await asyncio.wait_for(paused.wait(), 10)
                    async with factory.begin() as probe:
                        assert await probe.scalar(
                            select(func.pg_try_advisory_xact_lock(STORE_ADMISSION_LOCK))
                        ) is False
                    competing = asyncio.create_task(jobs.claim_next(factory))
                    resume.set()
                    done, admitted = await asyncio.wait_for(
                        asyncio.gather(task, competing), 15,
                    )
                    assert done is True and admitted is not None
                    assert admitted.store_id == next_store
            finally:
                resume.set()
                tasks = [item for item in (task, competing) if item is not None]
                for item in tasks:
                    if not item.done():
                        item.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        print("Every terminal transition holds the scheduling gate through commit and releases capacity consistently")
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from store_ready_postgres import verify_ready_results
        await verify_ready_results(factory, reset, owned, change, snapshot)
        assert isolated.pool.checkedout() == 0
        from store_execution_postgres import verify_execution
        await verify_execution(factory, reset, owned, change, snapshot)
        assert isolated.pool.checkedout() == 0
        print("Store completion verification passed. Live stores/settings and external services were unchanged.")
        print("Kubernetes setup and cleanup are not exercised by this database-only test.")
    finally:
        if isolated is not None:
            await isolated.dispose()
        try:
            if created:
                assert schema.startswith("urumi_completion_test_") and len(schema) < 64
                async with engine.begin() as connection:
                    await connection.execute(DropSchema(schema, cascade=True))
                print("Temporary PostgreSQL schema removed.")
        finally:
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 180))
