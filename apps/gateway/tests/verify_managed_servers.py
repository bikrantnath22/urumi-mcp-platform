"""Verify managed-server API guards in a disposable PostgreSQL schema."""

import asyncio
import secrets
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

if not Path("app/managed_servers.py").is_file():
    raise SystemExit("Run from Wo_MCP/apps/gateway.")
sys.path.insert(0, str(Path.cwd()))

import httpx
from fastapi import FastAPI
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app import managed_servers as guard
from app import registry, servers as api
from app.config import database_url
from app.database import engine, get_session
from app.models import AuditLog, Base, BrowserSession, MCPServer, Store, ToolGrant, User
from app.security import APP_ORIGIN, create_session_token, token_hash
from app.store_registration import definitions_for
from app.store_settings import STORE_ADMISSION_LOCK, lock_store_admission
from app.upstream_security import parse_endpoint


ADMIN, MEMBER = 98501, 98502
ROOT = "/api/servers"


def expect(response, status):
    assert response.status_code == status, (response.status_code, response.text)
    return None if status == 204 else response.json()


def payload(**changes):
    return {
        "slug": "manual-fixture", "name": "Manual fixture",
        "url": "https://manual.fixture.invalid/mcp",
        "enabled": True, "timeout_s": 15, **changes,
    }


async def check():
    schema = "urumi_server_guard_test_" + uuid4().hex
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

        tokens, session_ids = {}, {}
        async with factory.begin() as db:
            for user_id, role in ((ADMIN, "admin"), (MEMBER, "member")):
                db.add(User(
                    id=user_id, email=f"user-{user_id}@fixture.invalid",
                    role=role, active=True, password_hash="fixture-only",
                ))
            await db.flush()
            for user_id in (ADMIN, MEMBER):
                now = datetime.now(timezone.utc)
                nonce = secrets.token_urlsafe(32)
                session_ids[user_id] = token_hash(nonce)
                tokens[user_id] = create_session_token(user_id, session_id=nonce)
                db.add(BrowserSession(
                    id=session_ids[user_id], user_id=user_id, session_version=0,
                    created_at=now, last_activity_at=now,
                    idle_expires_at=now + timedelta(minutes=30),
                    absolute_expires_at=now + timedelta(hours=8),
                ))

        async def dependency():
            async with factory() as db:
                yield db

        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[get_session] = dependency

        def client(user_id=None, *, raise_errors=True):
            result = httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=app, raise_app_exceptions=raise_errors,
                ),
                base_url="http://fixture.invalid", trust_env=False,
            )
            if user_id is not None:
                result.cookies.set("urm_session", tokens[user_id])
                result.cookies.set("urm_csrf", "fixture-csrf")
                result.headers.update({
                    "X-CSRF-Token": "fixture-csrf", "Origin": APP_ORIGIN,
                })
            return result

        async def reset():
            async with factory.begin() as db:
                for model in (ToolGrant, AuditLog, Store, MCPServer):
                    await db.execute(delete(model))

        async def manual():
            async with factory.begin() as db:
                server = MCPServer(
                    **{
                        key: value for key, value in payload().items()
                        if key != "timeout_s"
                    },
                    timeout_s=15, status="Connected",
                )
                db.add(server)
                await db.flush()
                db.add(ToolGrant(
                    user_id=MEMBER, server_id=server.id,
                    tool_name="fixture_read", granted_by_id=ADMIN,
                ))
                db.add(AuditLog(
                    user_id=MEMBER, server_id=server.id,
                    server_slug=server.slug, tool="fixture_read", duration_ms=1,
                    outcome="success", source="mcp",
                ))
                return server.id

        async def store_fixture(*, status="Provisioning", bind=True):
            identity = uuid4()
            async with factory.begin() as db:
                store = Store(
                    id=identity, owner_id=MEMBER, name="Fixture store",
                    slug="shop-" + identity.hex[:16],
                    namespace="urumi-store-" + identity.hex, release_name="store",
                    url=f"http://shop-{identity.hex}.localtest.me",
                    status=status,
                    desired_state=(
                        "absent" if status in ("Deleting", "Deleted") else "present"
                    ),
                    generation=1,
                    deleted_at=(
                        datetime.now(timezone.utc) if status == "Deleted" else None
                    ),
                )
                ids = []
                if bind:
                    definitions = definitions_for(SimpleNamespace(
                        store_id=identity, namespace=store.namespace,
                        release_name="store", slug=store.slug, name=store.name,
                    ))
                    for definition in definitions:
                        server = MCPServer(
                            slug=definition.slug, name=definition.name,
                            url=definition.url,
                            enabled=False, timeout_s=15, status="Disabled",
                        )
                        db.add(server)
                        await db.flush()
                        ids.append(server.id)
                    store.mcp_server_id, store.analytics_mcp_server_id = ids
                db.add(store)
            return identity, ids

        async def snapshot():
            async with factory() as db:
                data = {}
                for model in (Store, MCPServer, ToolGrant, AuditLog):
                    table = model.__table__
                    rows = await db.execute(
                        select(table).order_by(*table.primary_key.columns)
                    )
                    data[table.name] = [tuple(row) for row in rows]
                return data

        async def change(model, identity, **values):
            async with factory.begin() as db:
                await db.execute(
                    update(model).where(model.id == identity).values(**values)
                )

        validator = AsyncMock()
        discovery = AsyncMock(
            side_effect=AssertionError("Unexpected upstream call.")
        )
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(api, "validate_destination", validator)
            )
            stack.enter_context(patch.object(registry, "Session", factory))
            stack.enter_context(
                patch.object(registry, "discover_tools", discovery)
            )

            async with (
                client(ADMIN) as admin,
                client(MEMBER) as member,
                client() as anonymous,
            ):
                await reset()
                identity = await manual()
                before = await snapshot()
                for visitor, status in ((anonymous, 401), (member, 403)):
                    for method, url, body in (
                        ("GET", ROOT, None),
                        ("POST", ROOT, payload()),
                        ("PUT", f"{ROOT}/{identity}", payload()),
                        ("DELETE", f"{ROOT}/{identity}", None),
                        ("POST", f"{ROOT}/{identity}/check", None),
                    ):
                        kwargs = {"json": body} if body is not None else {}
                        expect(
                            await visitor.request(method, url, **kwargs), status,
                        )
                for headers in (
                    {"X-CSRF-Token": ""},
                    {"X-CSRF-Token": "wrong"},
                    {"Origin": "https://untrusted.invalid"},
                ):
                    for method, url, body in (
                        ("POST", ROOT, payload()),
                        ("PUT", f"{ROOT}/{identity}", payload()),
                        ("DELETE", f"{ROOT}/{identity}", None),
                        ("POST", f"{ROOT}/{identity}/check", None),
                    ):
                        kwargs = {"json": body} if body is not None else {}
                        expect(await admin.request(
                            method, url, headers=headers, **kwargs,
                        ), 403)
                for extra in ({"owner_id": MEMBER}, {"managed": False}):
                    expect(await admin.post(ROOT, json=payload(**extra)), 422)
                assert await snapshot() == before
                validator.assert_not_awaited()

                for model, record_id, changed, restored, status in (
                    (User, ADMIN, {"active": False}, {"active": True}, 401),
                    (User, ADMIN, {"role": "member"}, {"role": "admin"}, 403),
                    (
                        User, ADMIN,
                        {"session_version": 1}, {"session_version": 0}, 401,
                    ),
                    (
                        BrowserSession, session_ids[ADMIN],
                        {"revoked_at": datetime.now(timezone.utc)},
                        {"revoked_at": None}, 401,
                    ),
                ):
                    try:
                        await change(model, record_id, **changed)
                        expect(
                            await admin.delete(f"{ROOT}/{identity}"), status,
                        )
                    finally:
                        await change(model, record_id, **restored)
                assert await snapshot() == before
                print("Anonymous/member access, CSRF/origin, caller overrides and unavailable admin sessions reject mutations")

                for status in (
                    "Provisioning", "Ready", "Failed", "Deleting", "Deleted",
                ):
                    await reset()
                    _, ids = await store_fixture(status=status)
                    before = await snapshot()
                    validator.reset_mock()
                    for server_id in ids:
                        expect(await admin.put(
                            f"{ROOT}/{server_id}",
                            json=payload(name="Attempted edit"),
                        ), 409)
                        expect(await admin.delete(
                            f"{ROOT}/{server_id}?managed=false",
                        ), 409)
                    assert await snapshot() == before
                    validator.assert_not_awaited()
                listing = expect(await admin.get(ROOT), 200)
                assert len(listing) == 2
                expect(await admin.post(f"{ROOT}/{ids[0]}/check"), 200)
                discovery.assert_not_awaited()
                print("Both linked server IDs stay protected in every store status; listing and disabled health checks remain available")

                await reset()
                identity = await manual()
                store_id = uuid4()
                for prefix in ("woo", "analytics"):
                    reserved = prefix + "-assam-te-" + store_id.hex[:12]
                    before = await snapshot()
                    expect(await admin.post(
                        ROOT, json=payload(slug=reserved),
                    ), 409)
                    expect(await admin.put(
                        f"{ROOT}/{identity}", json=payload(slug=reserved),
                    ), 409)
                    assert await snapshot() == before

                # Exercise this guard independently of the earlier SSRF rejection.
                validator.reset_mock()
                with patch.object(
                    api.destination_policy, "validate_url",
                    side_effect=parse_endpoint,
                ):
                    for suffix in (
                        "", ".svc", ".svc.cluster.local", ".svc.cluster.local.",
                    ):
                        url = (
                            f"http://woo-mcp.urumi-store-{store_id.hex}"
                            f"{suffix}:8000/mcp"
                        )
                        before = await snapshot()
                        expect(await admin.post(
                            ROOT, json=payload(url=url),
                        ), 409)
                        expect(await admin.put(
                            f"{ROOT}/{identity}", json=payload(url=url),
                        ), 409)
                        assert await snapshot() == before
                validator.assert_not_awaited()
                await change(
                    MCPServer, identity,
                    url=(
                        f"http://mcp-analytics.urumi-store-{store_id.hex}"
                        ".svc.cluster.local:8000/mcp"
                    ),
                )
                expect(await admin.delete(f"{ROOT}/{identity}"), 409)
                print("Generated prefixes, managed hostname aliases and unlinked managed endpoints reject manual replacement before DNS")

                # Normal manual operations retain their grant/audit behavior.
                await reset()
                validator.reset_mock()

                async def dns_outside_gate(url):
                    async with factory.begin() as probe:
                        assert await probe.scalar(
                            select(func.pg_try_advisory_xact_lock(
                                STORE_ADMISSION_LOCK,
                            ))
                        ) is True

                with patch.object(
                    api, "validate_destination", side_effect=dns_outside_gate,
                ):
                    created_server = expect(
                        await admin.post(ROOT, json=payload()), 201,
                    )
                    identity = created_server["id"]
                    expect(await admin.put(
                        f"{ROOT}/{identity}",
                        json=payload(
                            url="https://changed.fixture.invalid/mcp",
                        ),
                    ), 200)
                async with factory.begin() as db:
                    db.add(ToolGrant(
                        user_id=MEMBER, server_id=identity,
                        tool_name="fixture_read", granted_by_id=ADMIN,
                    ))
                    db.add(AuditLog(
                        user_id=MEMBER, server_id=identity,
                        server_slug="manual-fixture", tool="fixture_read",
                        duration_ms=1, outcome="success", source="mcp",
                    ))
                same_url = "https://changed.fixture.invalid/mcp"
                expect(await admin.put(
                    f"{ROOT}/{identity}",
                    json=payload(name="Renamed", url=same_url),
                ), 200)
                async with factory() as db:
                    assert await db.scalar(
                        select(func.count()).select_from(ToolGrant)
                    ) == 1
                expect(await admin.put(
                    f"{ROOT}/{identity}",
                    json=payload(url="https://next.fixture.invalid/mcp"),
                ), 200)
                async with factory() as db:
                    assert await db.scalar(
                        select(func.count()).select_from(ToolGrant)
                    ) == 0
                expect(await admin.delete(f"{ROOT}/{identity}"), 204)
                async with factory() as db:
                    audit = await db.scalar(select(AuditLog))
                    assert audit.server_id is None
                    assert audit.server_slug == "manual-fixture"
                print("Manual create/edit/delete still work; DNS precedes the gate, grants reset on identity changes and audits survive deletion")

                # Simulate a concurrent lifecycle attachment or admin URL change.
                normal_loader = api.load_manual_server
                for method, race in (
                    ("PUT", "attach"), ("DELETE", "attach"), ("PUT", "url"),
                ):
                    await reset()
                    identity = await manual()
                    store_id, _ = await store_fixture(bind=False)
                    waiting = asyncio.Event()
                    task = None

                    async def observed_loader(db, server_id, *, lock):
                        if lock:
                            waiting.set()
                        return await normal_loader(db, server_id, lock=lock)

                    try:
                        async with factory.begin() as holder:
                            await lock_store_admission(holder)
                            with patch.object(
                                api, "load_manual_server", observed_loader,
                            ):
                                kwargs = {
                                    "json": payload(name="Rejected edit")
                                } if method == "PUT" else {}
                                task = asyncio.create_task(admin.request(
                                    method, f"{ROOT}/{identity}", **kwargs,
                                ))
                                await asyncio.wait_for(waiting.wait(), 5)
                                assert not task.done()
                                if race == "attach":
                                    statement = update(Store).where(
                                        Store.id == store_id,
                                    ).values(mcp_server_id=identity)
                                else:
                                    statement = update(MCPServer).where(
                                        MCPServer.id == identity,
                                    ).values(
                                        url="https://concurrent.fixture.invalid/mcp",
                                    )
                                await asyncio.wait_for(
                                    holder.execute(statement), 3,
                                )
                        expect(await asyncio.wait_for(task, 5), 409)
                        async with factory() as db:
                            row = await db.get(MCPServer, identity)
                            assert row.name == "Manual fixture"
                            assert await db.scalar(
                                select(func.count()).select_from(ToolGrant)
                            ) == 1
                            if race == "attach":
                                store = await db.get(Store, store_id)
                                assert store.mcp_server_id == identity
                            else:
                                assert row.url == (
                                    "https://concurrent.fixture.invalid/mcp"
                                )
                    finally:
                        if task is not None and not task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                print("Concurrent store attachment and URL changes reject waiting edits/deletes; the MCP row is never locked ahead of the gate")

                normal_commit = AsyncSession.commit
                for method in ("POST", "PUT", "DELETE"):
                    await reset()
                    identity = await manual()
                    before = await snapshot()
                    entered, resume = asyncio.Event(), asyncio.Event()
                    task = None

                    async def held_commit(db):
                        if asyncio.current_task() is task:
                            await db.flush()
                            entered.set()
                            await asyncio.wait_for(resume.wait(), 10)
                        return await normal_commit(db)

                    try:
                        with patch.object(
                            AsyncSession, "commit", held_commit,
                        ):
                            target = (
                                ROOT if method == "POST"
                                else f"{ROOT}/{identity}"
                            )
                            kwargs = {"json": payload(
                                slug=(
                                    "new-fixture" if method == "POST"
                                    else "manual-fixture"
                                ),
                                name="Changed",
                            )} if method != "DELETE" else {}
                            task = asyncio.create_task(
                                admin.request(method, target, **kwargs)
                            )
                            await asyncio.wait_for(entered.wait(), 10)
                            async with factory.begin() as probe:
                                assert await probe.scalar(
                                    select(func.pg_try_advisory_xact_lock(
                                        STORE_ADMISSION_LOCK,
                                    ))
                                ) is False
                            assert await snapshot() == before
                            resume.set()
                            expect(await asyncio.wait_for(task, 10), {
                                "POST": 201, "PUT": 200, "DELETE": 204,
                            }[method])
                    finally:
                        resume.set()
                        if task is not None and not task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                print("Create, edit and delete hold the shared gate through commit; other sessions see no partial changes")

                await reset()
                identity = await manual()
                before = await snapshot()
                with patch.object(
                    guard, "lock_store_admission",
                    AsyncMock(side_effect=SQLAlchemyError(
                        "fixture-private-detail"
                    )),
                ):
                    for method in ("POST", "PUT", "DELETE"):
                        target = (
                            ROOT if method == "POST" else f"{ROOT}/{identity}"
                        )
                        kwargs = {
                            "json": payload(slug="new-fixture")
                        } if method != "DELETE" else {}
                        response = await admin.request(
                            method, target, **kwargs,
                        )
                        expect(response, 503)
                        assert "fixture-private-detail" not in response.text
                assert await snapshot() == before

                normal_scalar = AsyncSession.scalar

                async def broken_store_lookup(db, statement, *args, **kwargs):
                    if any(
                        table is Store.__table__
                        for table in statement.get_final_froms()
                    ):
                        raise SQLAlchemyError("fixture-private-lookup")
                    return await normal_scalar(
                        db, statement, *args, **kwargs,
                    )

                with patch.object(
                    AsyncSession, "scalar", broken_store_lookup,
                ):
                    for method in ("PUT", "DELETE"):
                        kwargs = (
                            {"json": payload()} if method == "PUT" else {}
                        )
                        response = await admin.request(
                            method, f"{ROOT}/{identity}", **kwargs,
                        )
                        expect(response, 503)
                        assert "fixture-private-lookup" not in response.text
                assert await snapshot() == before

                async def failed_commit(db):
                    await db.flush()
                    raise SQLAlchemyError("fixture-private-after-flush")

                for method in ("POST", "PUT", "DELETE"):
                    await reset()
                    identity = await manual()
                    before = await snapshot()
                    async with client(
                        ADMIN, raise_errors=False,
                    ) as failure_client:
                        with patch.object(
                            AsyncSession, "commit", failed_commit,
                        ):
                            target = (
                                ROOT if method == "POST"
                                else f"{ROOT}/{identity}"
                            )
                            kwargs = {"json": payload(
                                slug=(
                                    "new-fixture" if method == "POST"
                                    else "manual-fixture"
                                ),
                                name="Changed",
                            )} if method != "DELETE" else {}
                            response = await failure_client.request(
                                method, target, **kwargs,
                            )
                            assert response.status_code == 500, response.text
                            assert (
                                "fixture-private-after-flush"
                                not in response.text
                            )
                    assert await snapshot() == before
                async with factory.begin() as probe:
                    assert await probe.scalar(
                        select(func.pg_try_advisory_xact_lock(
                            STORE_ADMISSION_LOCK,
                        ))
                    ) is True
                print("Unavailable guard storage fails closed; post-flush failures roll back writes, grants and audit links without leaking diagnostics")
                discovery.assert_not_awaited()
        print("Managed-server guard verification passed. Live accounts/stores/settings and external services were unchanged.")
    finally:
        if isolated is not None:
            await isolated.dispose()
        try:
            if created:
                assert schema.startswith("urumi_server_guard_test_")
                assert len(schema) < 64
                async with engine.begin() as connection:
                    await connection.execute(DropSchema(schema, cascade=True))
                print("Temporary PostgreSQL schema removed.")
        finally:
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 180))
