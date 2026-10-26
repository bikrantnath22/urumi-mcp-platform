"""Lifecycle API checks in a temporary PostgreSQL schema; no cluster calls.

Run from apps/gateway after sourcing gateway-local-env.sh.
"""
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path.cwd()))
import httpx
from fastapi import FastAPI
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema
from app import store_lifecycle as api, store_access, stores
from app.config import database_url
from app.database import engine, get_session
from app.managed_upstreams import authorize_managed_endpoint
from app.models import Base, BrowserSession, MCPServer, SecurityEvent, Store, StoreOperation, StoreProvisioningSettings, ToolGrant, User
from app.security import APP_ORIGIN, create_session_token, token_hash
from app.store_registration import definitions_for
from app.upstream_security import UpstreamPolicyError

ADMIN, ALICE, BOB = 99101, 99102, 99103
PRIVATE = "private-lifecycle-fixture"


def expect(response, code):
    assert response.status_code == code, (code, response.status_code, response.text)
    assert PRIVATE not in response.text
    if code in (200, 202):
        assert response.headers["cache-control"] == "no-store"
    return response.json()


async def check():
    schema = "urumi_store_lifecycle_test_" + uuid4().hex
    created, isolated = False, None
    try:
        async with engine.begin() as db:
            await db.execute(CreateSchema(schema))
        created = True
        isolated = create_async_engine(
            database_url(), hide_parameters=True, pool_size=8, max_overflow=0,
            connect_args={"connect_timeout": 5, "options":
                f"-c search_path={schema} -c timezone=UTC -c statement_timeout=10000 -c lock_timeout=7000"},
        )
        factory = async_sessionmaker(isolated, expire_on_commit=False)
        async with isolated.begin() as db:
            assert await db.scalar(select(func.current_schema())) == schema
            await db.run_sync(Base.metadata.create_all)
        now = datetime.now(timezone.utc)
        tokens, sessions, ids, definitions = {}, {}, [], {}
        async with factory() as db:
            for identity in (ADMIN, ALICE, BOB):
                db.add(User(id=identity, email=f"user-{identity}@fixture.invalid",
                    role="admin" if identity == ADMIN else "member",
                    active=True, password_hash="fixture-only"))
            await db.flush()
            db.add(StoreProvisioningSettings(id=1, enabled=False))
            for identity in (ADMIN, ALICE, BOB):
                nonce = secrets.token_urlsafe(32)
                sessions[identity] = token_hash(nonce)
                tokens[identity] = create_session_token(identity, session_id=nonce)
                db.add(BrowserSession(id=sessions[identity], user_id=identity,
                    session_version=0, created_at=now, last_activity_at=now,
                    idle_expires_at=now + timedelta(minutes=30),
                    absolute_expires_at=now + timedelta(hours=8)))
            for index, owner in enumerate((ALICE, BOB)):
                sid = uuid4()
                ids.append(sid)
                claim = SimpleNamespace(store_id=sid, namespace="urumi-store-" + sid.hex,
                    release_name="store", slug="fixture-" + str(index), name="Fixture " + str(index))
                defs = definitions_for(claim)
                servers = []
                for definition in defs:
                    server = MCPServer(slug=definition.slug, name=definition.name,
                        url=definition.url, enabled=True, status="Connected", timeout_s=15)
                    db.add(server)
                    servers.append(server)
                await db.flush()
                store = Store(id=sid, owner_id=owner, name=claim.name, slug=claim.slug,
                    namespace=claim.namespace, namespace_uid="ns-" + sid.hex,
                    credentials_secret_uid="secret-" + sid.hex, url="http://" + claim.slug + ".localtest.me",
                    status="Ready", desired_state="present", access_guarded=True,
                    mcp_server_id=servers[0].id, analytics_mcp_server_id=servers[1].id)
                db.add(store)
                definitions[sid] = servers
            await db.flush()
            db.add(ToolGrant(user_id=BOB, server_id=definitions[ids[0]][0].id,
                tool_name="update_product", granted_by_id=ADMIN))
            await db.commit()

        async def dependency():
            async with factory() as db:
                yield db
        app = FastAPI()
        for router in (api.router, store_access.router, stores.router):
            app.include_router(router)
        app.dependency_overrides[get_session] = dependency

        def client(identity=None):
            instance = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                base_url="http://fixture.invalid", trust_env=False)
            if identity is not None:
                instance.cookies.set("urm_session", tokens[identity])
                instance.cookies.set("urm_csrf", "fixture-csrf")
                instance.headers.update({"X-CSRF-Token": "fixture-csrf", "Origin": APP_ORIGIN})
            return instance

        async def snapshot():
            async with factory() as db:
                return {
                    model.__tablename__: (await db.execute(select(model.__table__).order_by(*model.__table__.primary_key.columns))).all()
                    for model in (Store, StoreOperation, MCPServer, ToolGrant, SecurityEvent)
                }

        async def edit(model, key, **values):
            async with factory.begin() as db:
                await db.execute(update(model).where(model.id == key).values(**values))

        async def access(who, enabled, *, revision=None, generation=None):
            data = expect(await who.get("/api/stores/" + str(ids[0])), 200)["store"]
            return await who.put("/api/stores/" + str(ids[0]) + "/access", json={
                "generation": generation or data["generation"],
                "revision": revision or data["access_revision"], "enabled": enabled,
            })

        async with AsyncExitStack() as stack:
            admin, alice, bob, anonymous = [
                await stack.enter_async_context(client(identity))
                for identity in (ADMIN, ALICE, BOB, None)
            ]
            url = "/api/stores/" + str(ids[0])
            delete_body = {"generation": 1, "request_id": str(uuid4())}
            original = await snapshot()
            expect(await anonymous.request("DELETE", url, json=delete_body), 401)
            expect(await bob.request("DELETE", url, json=delete_body), 404)
            expect(await alice.put(url + "/access", json={
                "generation": 1, "revision": 1, "enabled": False}), 403)
            headers = dict(alice.headers)
            alice.headers.pop("X-CSRF-Token")
            expect(await alice.request("DELETE", url, json=delete_body), 403)
            alice.headers.update(headers)
            assert await snapshot() == original
            assert (await anonymous.get("/api/store-access/" + str(ids[0]))).status_code == 204
            print("Anonymous/CSRF and delegated member lifecycle requests reject changes; product grants do not transfer ownership.")

            expect(await access(admin, False), 200)
            disabled = await snapshot()
            assert disabled["tool_grants"] == original["tool_grants"]
            assert all(not row.enabled for row in disabled["mcp_servers"] if row.id in {item.id for item in definitions[ids[0]]})
            gate = await anonymous.get("/api/store-access/" + str(ids[0]))
            assert gate.status_code == 503 and gate.headers["cache-control"].startswith("no-store")
            assert str(ids[0]) not in gate.text and "Fixture" not in gate.text
            try:
                server = definitions[ids[0]][0]
                await authorize_managed_endpoint(server.url, server_id=server.id, session_factory=factory)
            except UpstreamPolicyError:
                pass
            else:
                raise AssertionError("Disabled managed tools remained usable")
            expect(await access(admin, False, revision=1), 200)
            assert await snapshot() == disabled
            expect(await access(admin, True), 200)
            assert (await snapshot())["tool_grants"] == original["tool_grants"]
            assert (await anonymous.get("/api/store-access/" + str(ids[0]))).status_code == 204
            await authorize_managed_endpoint(server.url, server_id=server.id, session_factory=factory)
            expect(await access(admin, False, revision=1), 409)
            print("Disable blocks the public gate and managed tools; Enable preserves grants and restores access; stale changes are rejected.")

            before = await snapshot()
            responses = await asyncio.gather(*(access(admin, False) for _ in range(8)))
            assert all(response.status_code == 200 for response in responses)
            after = await snapshot()
            assert len(after["security_events"]) == len(before["security_events"]) + 1
            assert after["tool_grants"] == before["tool_grants"]
            expect(await access(admin, True), 200)

            await edit(Store, ids[0], access_guarded=False)
            before = await snapshot()
            expect(await access(admin, False), 409)
            assert await snapshot() == before
            await edit(Store, ids[0], access_guarded=True)
            await edit(MCPServer, server.id, credential_encrypted="fixture-only")
            before = await snapshot()
            expect(await access(admin, False), 409)
            assert await snapshot() == before
            await edit(MCPServer, server.id, credential_encrypted=None)
            print("Concurrent same-state requests commit one audit; missing gates and altered managed identities cannot change access.")

            before = await snapshot()
            normal_flush = AsyncSession.flush
            async def broken_flush(db, *args, **kwargs):
                if any(isinstance(item, Store) and item.access_enabled is False for item in db.dirty):
                    await normal_flush(db, *args, **kwargs)
                    raise SQLAlchemyError(PRIVATE)
                return await normal_flush(db, *args, **kwargs)
            with patch.object(AsyncSession, "flush", broken_flush):
                expect(await access(admin, False), 503)
            assert await snapshot() == before
            entered, continue_flush = asyncio.Event(), asyncio.Event()
            async def held_flush(db, *args, **kwargs):
                if any(isinstance(item, Store) and item.access_enabled is False for item in db.dirty):
                    await normal_flush(db, *args, **kwargs)
                    entered.set()
                    await continue_flush.wait()
                    return
                return await normal_flush(db, *args, **kwargs)
            task = None
            try:
                with patch.object(AsyncSession, "flush", held_flush):
                    task = asyncio.create_task(access(admin, False))
                    await asyncio.wait_for(entered.wait(), 5)
                    assert await snapshot() == before
                    task.cancel()
                    try: await task
                    except asyncio.CancelledError: pass
            finally:
                continue_flush.set()
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            assert await snapshot() == before
            print("Post-flush failures and cancellation roll back flags, both registrations and audits; uncommitted changes stay hidden.")

            # A role change while admission is blocked is checked again before mutation.
            entered = asyncio.Event()
            original_gate = api.lock_store_admission
            async def signalled_gate(db):
                entered.set()
                await original_gate(db)
            task = None
            async with factory() as blocker:
                await original_gate(blocker)
                with patch.object(api, "lock_store_admission", signalled_gate):
                    task = asyncio.create_task(access(admin, False))
                    await asyncio.wait_for(entered.wait(), 5)
                    await edit(User, ADMIN, role="member")
                    await blocker.rollback()
                    expect(await task, 403)
            await edit(User, ADMIN, role="admin")
            assert await snapshot() == before
            print("A waiting request cannot keep admin authority after a concurrent demotion.")

            replies = await asyncio.gather(*(
                alice.request("DELETE", url, json=delete_body) for _ in range(8)
            ))
            assert sorted(response.status_code for response in replies) == [200] * 7 + [202]
            data = replies[0].json()
            assert data["store"]["status"] == "Deleting" and data["store"]["generation"] == 2
            after = await snapshot()
            assert len(after["store_operations"]) == 1
            assert after["tool_grants"] == before["tool_grants"]
            assert (await anonymous.get("/api/store-access/" + str(ids[0]))).status_code == 503
            expect(await access(admin, True), 409)
            expect(await alice.request("DELETE", url, json={**delete_body, "generation": 2}), 409)
            expect(await alice.request("DELETE", "/api/stores/" + str(ids[1]), json=delete_body), 404)
            print("Owner deletion queues one private cleanup job even with creation paused; replay does not add jobs or remove data/grants early.")

            # Terminal cleanup is a fixture transition here; real cleanup is tested separately.
            await edit(StoreOperation, data["operation"]["id"], status="Succeeded")
            await edit(Store, ids[0], status="Deleted", deleted_at=now)
            replay = expect(await alice.request("DELETE", url, json=delete_body), 200)
            assert replay["store"]["status"] == "Deleted" and replay["replayed"] is True
            await edit(Store, ids[1], status="Provisioning")
            expect(await bob.request("DELETE", "/api/stores/" + str(ids[1]), json={
                "request_id": str(uuid4()), "generation": 1}), 409)
            await edit(Store, ids[1], status="Failed")
            expect(await admin.request("DELETE", "/api/stores/" + str(ids[1]), json={
                "request_id": str(uuid4()), "generation": 1}), 202)
            assert (await anonymous.get("/api/store-access/" + str(uuid4()))).status_code == 503
            print("Deleted replays cannot resurrect a store; active creation rejects deletion and administrators can queue Failed-store cleanup.")
        assert isolated.pool.checkedout() == 0
        print("Store lifecycle PostgreSQL verification passed. Live accounts, stores, Notes and external services were unchanged.")
    finally:
        if isolated:
            await isolated.dispose()
        if created:
            async with engine.begin() as db:
                await db.execute(DropSchema(schema, cascade=True))
            print("Temporary PostgreSQL schema removed.")
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 180))
