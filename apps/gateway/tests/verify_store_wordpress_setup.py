"""Owner-scoped WordPress setup checks in an isolated PostgreSQL schema."""
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import sys
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema
from app import store_wordpress_setup as api
from app.config import database_url
from app.database import engine, get_session
from app.models import Base, BrowserSession, SecurityEvent, Store, StoreWordPressSetup, User
from app.security import APP_ORIGIN, create_session_token, token_hash

ADMIN, ALICE, BOB = 99181, 99182, 99183


async def exercise(factory, *, concurrent=True, real_locks=True):
    now = datetime.now(timezone.utc)
    sid, other, admin_store = uuid4(), uuid4(), uuid4()
    sessions, tokens = {}, {}
    async with factory.begin() as db:
        for identity in (ADMIN, ALICE, BOB):
            db.add(User(id=identity, email=f"setup-{identity}@fixture.invalid",
                role="admin" if identity == ADMIN else "member",
                active=True, password_hash="fixture-only"))
        await db.flush()
        for identity in (ADMIN, ALICE, BOB):
            nonce = secrets.token_urlsafe(32)
            sessions[identity] = token_hash(nonce)
            tokens[identity] = create_session_token(identity, session_id=nonce)
            db.add(BrowserSession(id=sessions[identity], user_id=identity,
                session_version=0, created_at=now, last_activity_at=now,
                idle_expires_at=now + timedelta(minutes=30),
                absolute_expires_at=now + timedelta(hours=8)))
        for key, owner, slug in ((sid, ALICE, "assamtea"), (other, BOB, "assam-tea"),
                (admin_store, ADMIN, "adminshop")):
            db.add(Store(id=key, owner_id=owner, name="urumi", slug=slug,
                namespace="urumi-store-" + key.hex, namespace_uid="namespace-" + key.hex,
                credentials_secret_uid="secret-" + key.hex, release_name="store",
                url=f"http://{slug}.localtest.me", status="Ready", desired_state="present",
                access_enabled=True, access_guarded=True, access_revision=1, generation=1))

    async def dependency():
        async with factory() as db:
            yield db
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_session] = dependency
    def client(who=None):
        instance = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url="http://fixture.invalid", trust_env=False)
        if who:
            instance.cookies.set("urm_session", tokens[who])
            instance.cookies.set("urm_csrf", "fixture-csrf")
            instance.headers.update({"Origin": APP_ORIGIN, "X-CSRF-Token": "fixture-csrf"})
        return instance
    def expect(response, status):
        assert response.status_code == status, (status, response.status_code, response.text)
        assert "private-setup-fixture" not in response.text
        return response.json() if response.content else None
    async def edit(model, key, **values):
        async with factory.begin() as db:
            await db.execute(update(model).where(
                model.store_id == key if model is StoreWordPressSetup else model.id == key
            ).values(**values))
    async def setup_snapshot():
        async with factory() as db:
            row = await db.get(StoreWordPressSetup, sid)
            saved = None if row is None else {
                column.key: getattr(row, column.key) for column in row.__table__.columns
            }
            events = list(await db.scalars(select(SecurityEvent).order_by(SecurityEvent.id)))
            return saved, [(event.action, event.actor_id, event.details) for event in events]
    async def used():
        async with factory() as db:
            row = await db.get(StoreWordPressSetup, sid)
            return row.used_at

    async with AsyncExitStack() as stack:
        admin, alice, bob, anonymous = [await stack.enter_async_context(client(who))
            for who in (ADMIN, ALICE, BOB, None)]
        url = f"/api/stores/{sid}/wordpress-setup"
        admin_url = f"/api/stores/{admin_store}/wordpress-setup"
        async def issue(who=alice, target=url, website="http://assamtea.localtest.me"):
            response = await who.post(target, json={"generation": 1})
            result = expect(response, 200)
            assert response.headers["cache-control"] == "no-store"
            assert result["url"] == website + "/wp-login.php?action=urumi_setup"
            assert "password" not in result and "username" not in result
            return result
        async def redeem(result, store_id=sid):
            return await anonymous.post("/api/store-wordpress-setup/redeem",
                json={"store_id": str(store_id), "token": result["token"]})

        expect(await anonymous.post(url, json={"generation": 1}), 401)
        expect(await bob.post(url, json={"generation": 1}), 404)
        expect(await admin.post(url, json={"generation": 1}), 404)
        assert await setup_snapshot() == (None, [])
        expect(await alice.post(url, json={"generation": 1}, headers={"X-CSRF-Token": ""}), 403)
        expect(await alice.post(url, json={"generation": 1}, headers={"Origin": "https://other.invalid"}), 403)
        expect(await alice.post(url, json={"generation": 2}), 409)
        result = await issue()
        async with factory() as db:
            row = await db.get(StoreWordPressSetup, sid)
            assert row.token_hash == api._hash(result["token"]) and result["token"] not in repr(row.__dict__)
            audits = list(await db.scalars(select(SecurityEvent)))
            assert all(result["token"] not in repr(event.details) for event in audits)
        saved = await setup_snapshot()
        expect(await admin.post(url, json={"generation": 1}), 404)
        assert await setup_snapshot() == saved
        expect(await redeem(result, other), 403)
        responses = await asyncio.gather(*(redeem(result) for _ in range(8))) if concurrent else [await redeem(result)]
        assert sorted(response.status_code for response in responses) == [204] + [403] * (len(responses) - 1)
        expect(await redeem(result), 403)
        assert await used() is not None
        old = await issue()
        result = await issue()
        expect(await redeem(old), 403)
        expect(await redeem(result), 204)
        # A capability issued by the previous admin override must stop working.
        result = await issue()
        await edit(StoreWordPressSetup, sid, actor_id=ADMIN, session_id=sessions[ADMIN])
        saved = await setup_snapshot()
        expect(await redeem(result), 403)
        assert await setup_snapshot() == saved and await used() is None
        result = await issue()
        expect(await redeem(result), 204)
        # Promoting a former owner does not bypass changed ownership at redemption.
        result = await issue()
        await edit(User, ALICE, role="admin")
        await edit(Store, sid, owner_id=BOB)
        expect(await redeem(result), 403)
        assert await used() is None
        await edit(Store, sid, owner_id=ALICE)
        await edit(User, ALICE, role="member")
        result = await issue()
        expect(await redeem(result), 204)
        print("Only owners issue/redeem setup; non-owner admins cannot alter tokens or audits, including previously issued links.")
        print("Anonymous/CSRF/cross-store requests fail; only hashes persist and tokens work once.")

        for values, restore in (
            ({"status": "Provisioning"}, {"status": "Ready"}),
            ({"status": "Deleting"}, {"status": "Ready"}),
            ({"status": "Deleted", "deleted_at": now}, {"status": "Ready", "deleted_at": None}),
            ({"access_enabled": False}, {"access_enabled": True}),
            ({"access_revision": 3}, {"access_revision": 1}),
            ({"owner_id": BOB}, {"owner_id": ALICE}),
            ({"namespace_uid": "replaced"}, {"namespace_uid": "namespace-" + sid.hex}),
            ({"generation": 2}, {"generation": 1}),
        ):
            result = await issue()
            await edit(Store, sid, **values)
            expect(await redeem(result), 403)
            assert await used() is None
            await edit(Store, sid, **restore)
        for field in ("idle_expires_at", "revoked_at"):
            result = await issue()
            await edit(BrowserSession, sessions[ALICE], **{field: now - timedelta(minutes=1)})
            expect(await redeem(result), 403)
            await edit(BrowserSession, sessions[ALICE], **{field: now + timedelta(minutes=30) if field == "idle_expires_at" else None})
        result = await issue()
        await edit(User, ALICE, session_version=1)
        expect(await redeem(result), 403)
        await edit(User, ALICE, session_version=0)
        result = await issue(admin, admin_url, "http://adminshop.localtest.me")
        await edit(User, ADMIN, role="member")
        expect(await redeem(result, admin_store), 204)
        await edit(User, ADMIN, role="admin")
        print("Admins can set passwords for their own stores; changing role alone does not remove ownership.")
        result = await issue()
        await edit(StoreWordPressSetup, sid, expires_at=now - timedelta(seconds=1))
        expect(await redeem(result), 403)
        print("Store disablement, deletion, identity changes, session revocation and expiry invalidate setup.")

        for body in ({}, {"token": ["private-setup-fixture"], "store_id": str(sid)},
            {"token": "A" * 43, "store_id": 7},
            {"token": "A" * 43, "store_id": str(sid), "password": "private-setup-fixture"}):
            expect(await anonymous.post("/api/store-wordpress-setup/redeem", json=body), 403)
        response = await anonymous.post("/api/store-wordpress-setup/redeem",
            content="x" * 1025, headers={"Content-Type": "application/json"})
        expect(response, 403)
        result = await issue()
        normal_flush = AsyncSession.flush
        async def failed_flush(db, *args, **kwargs):
            changed = any(isinstance(row, StoreWordPressSetup) and row.used_at is not None for row in db.dirty)
            await normal_flush(db, *args, **kwargs)
            if changed:
                raise SQLAlchemyError("private-setup-fixture")
        with patch.object(AsyncSession, "flush", failed_flush):
            expect(await redeem(result), 503)
        assert await used() is None
        expect(await redeem(result), 204)
        result = await issue()
        async def cancelled_flush(db, *args, **kwargs):
            changed = any(isinstance(row, StoreWordPressSetup) and row.used_at is not None for row in db.dirty)
            await normal_flush(db, *args, **kwargs)
            if changed:
                raise asyncio.CancelledError()
        with patch.object(AsyncSession, "flush", cancelled_flush):
            try:
                await redeem(result)
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("Cancellation was swallowed")
        assert await used() is None
        expect(await redeem(result), 204)
        print("Malformed requests expose no capabilities; storage failure and cancellation roll back redemption.")

        if real_locks:
            result = await issue()
            entered = asyncio.Event()
            normal_gate = api.lock_store_admission
            async def observed_gate(db):
                entered.set()
                await normal_gate(db)
            async with factory() as blocker:
                await normal_gate(blocker)
                with patch.object(api, "lock_store_admission", observed_gate):
                    task = asyncio.create_task(redeem(result))
                    try:
                        await asyncio.wait_for(entered.wait(), 5)
                        await edit(StoreWordPressSetup, sid, expires_at=now - timedelta(seconds=1))
                    finally:
                        await blocker.rollback()
                    expect(await asyncio.wait_for(task, 10), 403)
            assert await used() is None
            print("A real admission-lock wait cannot revive an expired setup capability.")


async def check():
    schema = "urumi_wp_setup_test_" + uuid4().hex
    created, isolated = False, None
    try:
        async with engine.begin() as db:
            await db.execute(CreateSchema(schema))
        created = True
        isolated = create_async_engine(database_url(), hide_parameters=True, pool_size=8, max_overflow=0,
            connect_args={"connect_timeout": 5, "options":
                f"-c search_path={schema} -c timezone=UTC -c statement_timeout=10000 -c lock_timeout=7000"})
        factory = async_sessionmaker(isolated, expire_on_commit=False)
        async with isolated.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        await exercise(factory)
        assert isolated.pool.checkedout() == 0
        print("WordPress setup PostgreSQL checks passed. Live accounts, stores and external services were unchanged.")
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
