"""Verify managed MCP registration in a disposable PostgreSQL schema."""

import asyncio
import re
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

if not Path("app/store_registration.py").is_file():
    raise SystemExit("Run from Wo_MCP/apps/gateway.")
sys.path.insert(0, str(Path.cwd()))

from fastapi import HTTPException
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app import store_completion as completion
from app import store_jobs as jobs
from app import store_registration as registration
from app.config import database_url
from app.managed_servers import reject_reserved_identity
from app.store_requests import StoreCreationBody
from app.database import engine
from app.models import (
    AuditLog, Base, MCPServer, SecurityEvent, Store, StoreOperation,
    StoreProvisioningSettings, ToolGrant, User,
)
from app.store_settings import STORE_ADMISSION_LOCK, lock_store_admission


OWNER_A, OWNER_B = 98401, 98402


async def check():
    schema = "urumi_registration_test_" + uuid4().hex
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
            db.add(StoreProvisioningSettings(id=1, max_concurrent_creations=4))
            for owner in (OWNER_A, OWNER_B):
                db.add(User(
                    id=owner, email=f"owner-{owner}@fixture.invalid",
                    role="member", active=True, password_hash="fixture-only",
                ))

        async def reset():
            async with factory.begin() as db:
                for model in (
                    ToolGrant, AuditLog, SecurityEvent, StoreOperation, Store, MCPServer,
                ):
                    await db.execute(delete(model))
                db.add(MCPServer(
                    slug="notes", name="Unrelated fixture server",
                    url="http://mcp-notes.platform.svc.cluster.local:8000/mcp",
                    enabled=True, status="Connected", timeout_s=15,
                ))

        async def seed(*, owner=OWNER_A, identity=None, slug=None, overdue=False):
            identity = identity or uuid4()
            async with factory.begin() as db:
                now = await db.scalar(select(func.clock_timestamp()))
                store = Store(
                    id=identity, owner_id=owner, name="Assam Tea",
                    slug=slug or "assam-tea-" + identity.hex[:12],
                    namespace="urumi-store-" + identity.hex, release_name="store",
                    url=f"http://tea-{identity.hex}.localtest.me",
                    status="Provisioning", desired_state="present", generation=1,
                )
                db.add(store)
                await db.flush()
                db.add(StoreOperation(
                    id=uuid4(), store_id=identity, requested_by_id=owner,
                    request_key=uuid4(), request_fingerprint="a" * 64,
                    action="provision", store_generation=1, status="Pending", attempts=0,
                    available_at=now - timedelta(seconds=1),
                    deadline_at=now + timedelta(seconds=-1 if overdue else 1200),
                ))
            return identity

        async def owned(**kwargs):
            identity = await seed(**kwargs)
            claim = await jobs.claim_next(factory)
            assert claim is not None and claim.store_id == identity
            return claim

        async def change(model, identity, **values):
            async with factory.begin() as db:
                await db.execute(update(model).where(model.id == identity).values(**values))

        async def register(claim):
            return await registration.register_store_mcps(factory, claim)

        async def together(*operations):
            tasks = [asyncio.create_task(operation) for operation in operations]
            try:
                return await asyncio.wait_for(asyncio.gather(*tasks), 20)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        async def expect_error(expected, operation, *, message=None):
            try:
                await operation
            except expected as error:
                if message is not None:
                    assert str(error) == message
                return
            raise AssertionError(f"Expected {expected.__name__}.")

        async def snapshot():
            async with factory() as db:
                data = {}
                for model in (
                    User, StoreProvisioningSettings, Store, StoreOperation,
                    MCPServer, ToolGrant, AuditLog, SecurityEvent,
                ):
                    table = model.__table__
                    rows = await db.execute(
                        select(table).order_by(*table.primary_key.columns)
                    )
                    data[table.name] = [tuple(row) for row in rows]
                return data

        async def inspect_draft(claim, result):
            definitions = registration.definitions_for(claim)
            async with factory() as db:
                store = await db.get(Store, claim.store_id)
                operation = await db.get(StoreOperation, claim.operation_id)
                assert store.status == "Provisioning" and store.deleted_at is None
                assert operation.status == "Running"
                assert operation.deadline_at == claim.deadline_at
                assert operation.lease_token == claim.lease_token
                assert operation.lease_expires_at == claim.lease_expires_at
                assert operation.attempts == claim.attempt
                assert store.mcp_server_id == result.woo_server_id
                assert store.analytics_mcp_server_id == result.analytics_server_id
                ids = (result.woo_server_id, result.analytics_server_id)
                assert len(set(ids)) == 2
                servers = (await db.scalars(
                    select(MCPServer).where(MCPServer.id.in_(ids))
                )).all()
                by_url = {server.url: server for server in servers}
                assert len(by_url) == 2
                for definition in definitions:
                    server = by_url[definition.url]
                    assert server.slug == definition.slug
                    assert server.name == definition.name
                    assert not server.enabled and server.status == "Disabled"
                    assert server.timeout_s == 15 and server.last_checked_at is None
                    assert server.auth_header_name is None
                    assert server.credential_encrypted is None
                events = (await db.scalars(select(SecurityEvent))).all()
                assert len(events) == 1
                assert events[0].actor_id is None
                assert events[0].target_user_id == claim.owner_id
                assert events[0].action == "store.mcp_registered"
                assert events[0].details == {
                    "store_id": str(claim.store_id),
                    "operation_id": str(claim.operation_id),
                    "generation": claim.generation,
                    "woo_server_id": ids[0],
                    "analytics_server_id": ids[1],
                }
                assert await db.scalar(
                    select(func.count()).select_from(ToolGrant)
                ) == 0
                assert await db.scalar(
                    select(func.count()).select_from(MCPServer)
                ) == 3

        await reset()
        claim = await owned()
        for item in registration.definitions_for(
            replace(claim, slug="a" * 32, name="T" * 100)
        ):
            assert len(item.slug) <= 32 and len(item.name) <= 100
            assert re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", item.slug)

        def no_storage():
            raise AssertionError("Invalid arguments reached storage.")

        for invalid in (
            replace(claim, action="delete"),
            replace(claim, namespace="unmanaged"),
            replace(claim, release_name="unexpected"),
            replace(claim, slug="invalid_slug"),
            replace(claim, name=""),
        ):
            await expect_error(
                ValueError, registration.register_store_mcps(no_storage, invalid),
            )

        results = await together(*(register(claim) for _ in range(8)))
        assert all(result is not None for result in results)
        assert len(set(results)) == 1
        result = results[0]
        await inspect_draft(claim, result)
        committed = await snapshot()
        assert await register(claim) == result
        assert await snapshot() == committed
        try:
            result.woo_server_id = 999
        except FrozenInstanceError:
            pass
        else:
            raise AssertionError("Registration snapshot is mutable.")
        print("Eight simultaneous retries create two disabled registrations and one event; IDs and deadlines remain stable")

        # Recovery reuses registrations after the old worker loses its lease.
        await change(
            StoreOperation, claim.operation_id,
            lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
        )
        before = await snapshot()
        assert await register(claim) is None
        assert await snapshot() == before
        recovered = await jobs.claim_next(factory)
        assert recovered is not None and recovered.lease_token != claim.lease_token
        assert await register(claim) is None
        assert await register(recovered) == result
        await inspect_draft(recovered, result)
        print("A replacement worker reuses both registration IDs; the expired worker cannot write or duplicate events")

        # Accepted store addresses must also register and remain worker-managed.
        for slug in (
            "123-tea", "tea--shop", "7", "a" * 32,
            "1" + "-" * 30 + "2", "1234567--tea",
        ):
            await reset()
            body = StoreCreationBody(
                request_id=uuid4(), name="Fixture tea", slug=slug,
            )
            claim = await owned(slug=body.slug)
            result = await register(claim)
            assert result is not None
            await inspect_draft(claim, result)
            for definition in registration.definitions_for(claim):
                assert len(definition.slug) <= 32
                try:
                    reject_reserved_identity(
                        definition.slug, "https://fixture.invalid/mcp",
                    )
                except HTTPException as error:
                    assert error.status_code == 409
                else:
                    raise AssertionError("Managed MCP prefix was not reserved.")
        print("Numeric addresses, internal hyphens and maximum-length addresses register safely")

        await reset()
        first, second = await owned(), await owned(owner=OWNER_B)
        first_result, second_result = await together(
            register(first), register(second),
        )
        assert {
            first_result.woo_server_id, first_result.analytics_server_id,
        }.isdisjoint({
            second_result.woo_server_id, second_result.analytics_server_id,
        })
        assert first_result.woo_slug != second_result.woo_slug
        assert first_result.analytics_slug != second_result.analytics_slug
        assert first_result.woo_url != second_result.woo_url

        for collision in ("slug", "url"):
            await reset()
            claim = await owned()
            definition = registration.definitions_for(claim)[0]
            async with factory.begin() as db:
                db.add(MCPServer(
                    slug=definition.slug if collision == "slug" else "manual-fixture",
                    url=definition.url if collision == "url" else "https://fixture.invalid/mcp",
                    name="Manual fixture", enabled=True,
                    status="Connected", timeout_s=15,
                ))
            before = await snapshot()
            await expect_error(completion.StoreCompletionConflict, register(claim))
            assert await snapshot() == before

        # Deliberate shortened-ID collisions cannot adopt another store's server.
        await reset()
        first = await owned(
            identity=UUID("11111111-1111-4000-8000-000000000001"),
            slug="assam-tea-one",
        )
        await register(first)
        second = await owned(
            owner=OWNER_B,
            identity=UUID("11111111-1111-4000-8000-000000000002"),
            slug="assam-tea-two",
        )
        before = await snapshot()
        await expect_error(completion.StoreCompletionConflict, register(second))
        assert await snapshot() == before
        print("Different stores get separate registrations; slug, URL and deliberate prefix collisions are rejected without adoption")

        for values in (
            {"enabled": True}, {"status": "Unreachable"}, {"timeout_s": 30},
            {"name": "Edited"}, {"slug": "edited-server"},
            {"url": "https://fixture.invalid/changed"},
            {"auth_header_name": "Authorization"},
            {"credential_encrypted": "fixture-only"},
        ):
            await reset()
            claim = await owned()
            result = await register(claim)
            await change(MCPServer, result.woo_server_id, **values)
            before = await snapshot()
            await expect_error(completion.StoreCompletionConflict, register(claim))
            assert await snapshot() == before

        for values in (
            {"name": "Changed store"}, {"slug": "changed-store"},
            {"namespace": "urumi-store-" + uuid4().hex},
            {"release_name": "unexpected"}, {"owner_id": OWNER_B},
            {"url": "http://changed.localtest.me"},
        ):
            await reset()
            claim = await owned()
            await change(Store, claim.store_id, **values)
            before = await snapshot()
            await expect_error(completion.StoreCompletionConflict, register(claim))
            assert await snapshot() == before

        for clear_both in (False, True):
            await reset()
            claim = await owned()
            await register(claim)
            values = {"analytics_mcp_server_id": None}
            if clear_both:
                values["mcp_server_id"] = None
            await change(Store, claim.store_id, **values)
            before = await snapshot()
            await expect_error(completion.StoreCompletionConflict, register(claim))
            assert await snapshot() == before

        await reset()
        claim = await owned()
        result = await register(claim)
        other_store = await seed(owner=OWNER_B)
        await change(
            Store, other_store, analytics_mcp_server_id=result.woo_server_id,
        )
        before = await snapshot()
        await expect_error(completion.StoreCompletionConflict, register(claim))
        assert await snapshot() == before
        print("Changed store/server identity, credentials, enabled state, partial links and cross-store sharing reject reuse without changes")

        await reset()
        claim = await owned()
        wrong_store = uuid4()
        for invalid in (
            replace(claim, lease_token=uuid4()),
            replace(claim, operation_id=uuid4()),
            replace(
                claim, store_id=wrong_store,
                namespace="urumi-store-" + wrong_store.hex,
            ),
            replace(claim, generation=999),
            replace(claim, attempt=999),
        ):
            before = await snapshot()
            assert await register(invalid) is None
            assert await snapshot() == before
        await reset()
        claim = await owned(overdue=True)
        before = await snapshot()
        assert await register(claim) is None
        assert await snapshot() == before
        print("Wrong tokens, IDs, attempts, generations and elapsed provisioning deadlines create no registrations")

        normal_flush = AsyncSession.flush
        for failure_at in (1, 2, "constraint"):
            await reset()
            claim = await owned()
            definition = registration.definitions_for(claim)[0]
            before = await snapshot()
            calls = 0

            async def failed_flush(db, *args, **kwargs):
                nonlocal calls
                await normal_flush(db, *args, **kwargs)
                calls += 1
                if failure_at == "constraint" and calls == 1:
                    await db.execute(insert(MCPServer).values(
                        slug=definition.slug, name="Duplicate fixture",
                        url="https://fixture.invalid/mcp",
                        enabled=False, timeout_s=15, status="Disabled",
                    ))
                elif calls == failure_at:
                    raise SQLAlchemyError("fixture-only post-flush failure")

            with patch.object(AsyncSession, "flush", failed_flush):
                if failure_at == "constraint":
                    await expect_error(
                        completion.StoreCompletionConflict, register(claim),
                        message="Managed MCP registration could not be saved.",
                    )
                else:
                    await expect_error(SQLAlchemyError, register(claim))
            assert await snapshot() == before
            assert await register(claim) is not None
        print("Failures after server insertion or binding/event flush roll back fully; real constraint errors return a safe conflict")

        # Force expiry while uncommitted registration rows already exist.
        for field in ("deadline_at", "lease_expires_at"):
            await reset()
            claim = await owned()
            await change(
                StoreOperation, claim.operation_id,
                **{field: func.clock_timestamp() + timedelta(seconds=2)},
            )
            before = await snapshot()
            calls = 0

            async def delayed_flush(db, *args, **kwargs):
                nonlocal calls
                await normal_flush(db, *args, **kwargs)
                calls += 1
                if calls == 1:
                    await asyncio.sleep(2.1)

            with patch.object(AsyncSession, "flush", delayed_flush):
                assert await register(claim) is None
            assert calls == 2
            assert await snapshot() == before
        print("Lease or deadline expiry after writes rolls back both servers, links and the registration event")

        await reset()
        claim = await owned()
        before = await snapshot()
        paused, resume = asyncio.Event(), asyncio.Event()
        task = competing = None
        calls = 0

        async def hold_flush(db, *args, **kwargs):
            nonlocal calls
            await normal_flush(db, *args, **kwargs)
            if asyncio.current_task() is task:
                calls += 1
                if calls == 2:
                    paused.set()
                    await asyncio.wait_for(resume.wait(), 10)

        try:
            with patch.object(AsyncSession, "flush", hold_flush):
                task = asyncio.create_task(register(claim))
                await asyncio.wait_for(paused.wait(), 10)
                async with factory.begin() as probe:
                    assert await probe.scalar(
                        select(func.pg_try_advisory_xact_lock(STORE_ADMISSION_LOCK))
                    ) is False
                assert await snapshot() == before
                competing = asyncio.create_task(register(claim))
                resume.set()
                first_result, second_result = await asyncio.wait_for(
                    asyncio.gather(task, competing), 15,
                )
                assert first_result == second_result
                await inspect_draft(claim, first_result)
        finally:
            resume.set()
            tasks = [item for item in (task, competing) if item is not None]
            for item in tasks:
                if not item.done():
                    item.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        print("Registration holds the scheduling gate through commit; other sessions never see partial bindings or duplicate rows")

        # Database-only lifecycle integration; external verification is simulated.
        await reset()
        claim = await owned()
        result = await register(claim)
        async with factory.begin() as db:
            db.add(ToolGrant(
                user_id=OWNER_A, server_id=result.woo_server_id,
                tool_name="list_products", granted_by_id=None,
            ))
            db.add(AuditLog(
                user_id=OWNER_A, server_id=result.woo_server_id,
                server_slug=result.woo_slug, tool="list_products",
                duration_ms=1, outcome="success", source="mcp",
            ))
        before = await snapshot()
        assert await register(claim) == result
        assert await snapshot() == before
        assert await completion.finish_ready(factory, claim)
        before = await snapshot()
        assert await register(claim) is None
        assert await snapshot() == before

        async with factory.begin() as db:
            await lock_store_admission(db)
            store = await db.get(Store, claim.store_id)
            store.generation += 1
            store.desired_state = "absent"
            store.status = "Deleting"
            now = await db.scalar(select(func.clock_timestamp()))
            db.add(StoreOperation(
                id=uuid4(), store_id=store.id, requested_by_id=OWNER_A,
                request_key=uuid4(), request_fingerprint="b" * 64,
                action="delete", store_generation=store.generation,
                status="Pending", attempts=0, available_at=now,
                deadline_at=now + timedelta(seconds=1200),
            ))
        deletion = await jobs.claim_next(factory)
        assert deletion is not None and deletion.action == "delete"
        assert await completion.finish_deleted(factory, deletion)
        before = await snapshot()
        assert await register(claim) is None
        assert await snapshot() == before
        async with factory() as db:
            store = await db.get(Store, claim.store_id)
            assert store.status == "Deleted"
            assert store.mcp_server_id is None
            assert store.analytics_mcp_server_id is None
            assert await db.scalar(
                select(func.count()).select_from(MCPServer)
            ) == 1
            assert await db.scalar(
                select(func.count()).select_from(ToolGrant)
            ) == 0
            audit = await db.scalar(select(AuditLog))
            assert audit.server_id is None
            assert audit.server_slug == result.woo_slug
        await reset()
        claim = await owned()
        await register(claim)
        assert await completion.finish_failed(factory, claim, reason="bootstrap")
        before = await snapshot()
        assert await register(claim) is None
        assert await snapshot() == before
        print("Registration preserves explicit grants on replay; Ready, failure and deletion integrate without registration resurrection")
        print("Store registration verification passed. Live accounts/stores/settings and external services were unchanged.")
        print("Kubernetes provisioning, upstream connectivity and real readiness checks are not exercised.")
    finally:
        if isolated is not None:
            await isolated.dispose()
        try:
            if created:
                assert schema.startswith("urumi_registration_test_") and len(schema) < 64
                async with engine.begin() as connection:
                    await connection.execute(DropSchema(schema, cascade=True))
                print("Temporary PostgreSQL schema removed.")
        finally:
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 180))
