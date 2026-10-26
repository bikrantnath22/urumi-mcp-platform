"""Verify queued store creation using isolated PostgreSQL fixtures."""

import asyncio
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

if not Path("app/store_requests.py").is_file():
    raise SystemExit("Run from Wo_MCP/apps/gateway.")
sys.path.insert(0, str(Path.cwd()))

import httpx
from fastapi import FastAPI
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app import store_requests as api, store_settings_api, stores
from app.config import database_url
from app.database import engine, get_session
from app.models import (
    Base, BrowserSession, SecurityEvent, Store, StoreOperation,
    StoreProvisioningSettings, User,
)
from app.security import APP_ORIGIN, create_session_token, token_hash

ROOT = "/api/stores"
ADMIN, ALICE, BOB = 98101, 98102, 98103
PRIVATE = "fixture-private-storage-diagnostic"


def expect(response, status):
    assert response.status_code == status, (response.status_code, response.text)
    if status in (200, 202):
        assert response.headers["cache-control"] == "no-store"
    assert PRIVATE not in response.text
    return response.json()


def payload(slug=None, **changes):
    return {
        "request_id": str(uuid4()),
        "name": "Fixture tea store",
        "slug": slug or "tea-" + uuid4().hex[:12],
        **changes,
    }


async def check():
    schema = "urumi_store_create_test_" + uuid4().hex
    created, isolated = False, None

    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
        created = True

        isolated = create_async_engine(
            database_url(),
            pool_size=8,
            max_overflow=0,
            pool_timeout=5,
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

        now = datetime.now(timezone.utc)
        tokens, session_ids = {}, {}

        async with factory() as db:
            for identity in (ADMIN, ALICE, BOB):
                db.add(User(
                    id=identity,
                    email=f"user-{identity}@fixture.invalid",
                    role="admin" if identity == ADMIN else "member",
                    active=True,
                    password_hash="fixture-only",
                ))
            await db.flush()

            db.add(StoreProvisioningSettings(id=1))

            for identity in (ADMIN, ALICE, BOB):
                nonce = secrets.token_urlsafe(32)
                session_ids[identity] = token_hash(nonce)
                tokens[identity] = create_session_token(identity, session_id=nonce)
                db.add(BrowserSession(
                    id=session_ids[identity],
                    user_id=identity,
                    session_version=0,
                    created_at=now,
                    last_activity_at=now,
                    idle_expires_at=now + timedelta(minutes=30),
                    absolute_expires_at=now + timedelta(hours=8),
                ))

            await db.commit()

        async def dependency():
            async with factory() as db:
                yield db

        app = FastAPI()
        for router in (api.router, stores.router, store_settings_api.router):
            app.include_router(router)
        app.dependency_overrides[get_session] = dependency

        def client(identity=None):
            result = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://fixture.invalid",
                trust_env=False,
            )
            if identity is not None:
                result.cookies.set("urm_session", tokens[identity])
                result.cookies.set("urm_csrf", "fixture-csrf")
                result.headers.update({
                    "X-CSRF-Token": "fixture-csrf",
                    "Origin": APP_ORIGIN,
                })
            return result

        async def configure(**changes):
            async with factory() as db:
                await db.execute(
                    update(StoreProvisioningSettings)
                    .where(StoreProvisioningSettings.id == 1)
                    .values(**changes)
                )
                await db.commit()

        async def reset(**changes):
            async with factory() as db:
                for model in (StoreOperation, Store, SecurityEvent):
                    await db.execute(delete(model))
                await db.execute(
                    update(StoreProvisioningSettings)
                    .where(StoreProvisioningSettings.id == 1)
                    .values(
                        enabled=True,
                        max_per_user=2,
                        max_total=4,
                        timeout_seconds=1200,
                        revision=1,
                        updated_by_id=None,
                    )
                )
                await db.commit()

            if changes:
                await configure(**changes)

        async def counts():
            async with factory() as db:
                return tuple([
                    await db.scalar(select(func.count()).select_from(model))
                    for model in (Store, StoreOperation, SecurityEvent)
                ])

        async def post(identity, body):
            async with client(identity) as instance:
                return await instance.post(ROOT, json=body)

        normal_lock = api.lock_store_admission

        async def burst(jobs):
            arrived = 0
            gate = asyncio.Event()

            async def simultaneous_lock(db):
                nonlocal arrived
                arrived += 1
                if arrived == len(jobs):
                    gate.set()
                await asyncio.wait_for(gate.wait(), 10)
                await normal_lock(db)

            with patch.object(api, "lock_store_admission", simultaneous_lock):
                return await asyncio.wait_for(
                    asyncio.gather(*(post(owner, body) for owner, body in jobs)),
                    30,
                )

        async with client() as anonymous, client(ALICE) as alice, client(BOB) as bob:
            expect(await anonymous.post(ROOT, json=payload()), 401)

            for headers in (
                {"X-CSRF-Token": ""},
                {"X-CSRF-Token": "wrong"},
                {"Origin": "https://untrusted.invalid"},
            ):
                expect(await alice.post(
                    ROOT, json=payload(), headers=headers,
                ), 403)

            for invalid in (
                {"owner_id": BOB},
                {"namespace": "platform"},
                {"url": "http://untrusted.invalid"},
                {"request_id": "invalid"},
                {"name": "  "},
                {"name": 12},
                {"name": "Tea\x00"},
                {"slug": "app"},
                {"slug": "../other"},
                {"slug": "-tea"},
                {"slug": "a" * 33},
            ):
                expect(await alice.post(ROOT, json=payload(**invalid)), 422)

            expect(await alice.post(ROOT, json=payload()), 409)
            assert await counts() == (0, 0, 0)
            print("Anonymous access, CSRF/origin, owner overrides, invalid input and pause checks passed")

            await configure(enabled=True)
            body = payload(" TEA-SHOP ", name=" Fixture tea store ")
            response = await alice.post(
                ROOT, json=body, headers={"X-User-ID": str(BOB)},
            )
            original = expect(response, 202)
            store_id = UUID(original["store"]["id"])

            assert original["store"]["owner_id"] == ALICE
            assert original["store"]["slug"] == "tea-shop"
            assert original["store"]["name"] == "Fixture tea store"
            assert original["store"]["status"] == "Provisioning"
            assert original["operation"]["status"] == "Pending"
            assert original["replayed"] is False
            assert response.headers["location"] == f"{ROOT}/{store_id}"
            assert not {
                "namespace", "release_name", "mcp_server_id",
            } & original["store"].keys()
            assert not {
                "request_key", "lease_token", "last_error",
            } & original["operation"].keys()
            assert await counts() == (1, 1, 1)

            async with factory() as db:
                record = await db.get(Store, store_id)
                operation = await db.get(
                    StoreOperation, UUID(original["operation"]["id"]),
                )
                event = await db.scalar(select(SecurityEvent))

                assert record.namespace == f"urumi-store-{store_id.hex}"
                assert record.release_name == "store" and record.generation == 1
                assert record.desired_state == "present"
                assert operation.requested_by_id == ALICE
                assert operation.attempts == 0
                assert operation.request_key == UUID(body["request_id"])
                assert operation.store_generation == record.generation
                assert (
                    operation.deadline_at - operation.created_at
                ).total_seconds() == 1200
                assert operation.available_at == operation.created_at
                assert event.actor_id == event.target_user_id == ALICE
                assert event.action == "store.creation_requested"
                assert event.details == {
                    "store_id": str(store_id),
                    "operation_id": str(operation.id),
                    "slug": "tea-shop",
                }

            expect(await bob.get(f"{ROOT}/{store_id}"), 404)

            await configure(
                enabled=False,
                max_per_user=1,
                max_total=1,
                timeout_seconds=60,
            )
            with patch.dict(os.environ, {"STORE_BASE_DOMAIN": "invalid://domain"}):
                replayed = expect(await alice.post(ROOT, json=body), 200)

            assert replayed == {**original, "replayed": True}
            expect(await alice.post(
                ROOT, json={**body, "name": "Changed"},
            ), 409)
            assert await counts() == (1, 1, 1)
            print("Atomic queue/audit, fixed ownership, public fields and replay with original deadline passed")

        await reset()
        same = payload()
        responses = await burst([(ALICE, same)] * 6)
        assert sorted(r.status_code for r in responses) == [200] * 5 + [202], [
            (r.status_code, r.text) for r in responses
        ]
        assert len({
            expect(r, r.status_code)["store"]["id"] for r in responses
        }) == 1
        assert await counts() == (1, 1, 1)

        await reset()
        same = payload()
        responses = await burst([
            (ALICE, same),
            (ALICE, {**same, "name": "Different"}),
        ])
        assert sorted(r.status_code for r in responses) == [202, 409]
        assert await counts() == (1, 1, 1)
        print("Concurrent identical retries create once; changed payload with the same ID conflicts")

        await reset(max_total=10)
        responses = await burst([(ALICE, payload()) for _ in range(6)])
        assert sorted(r.status_code for r in responses) == [202] * 2 + [409] * 4, [
            (r.status_code, r.text) for r in responses
        ]
        assert await counts() == (2, 2, 2)

        await reset(max_total=2)
        responses = await burst([
            (owner, payload()) for owner in (ALICE, BOB, ADMIN) * 2
        ])
        assert sorted(r.status_code for r in responses) == [202] * 2 + [409] * 4, [
            (r.status_code, r.text) for r in responses
        ]
        assert await counts() == (2, 2, 2)
        print("Six independent PostgreSQL requests respect per-user and platform limits exactly")

        await reset()
        shared_id = str(uuid4())
        first = expect(await post(
            ALICE, payload("alice-store", request_id=shared_id),
        ), 202)
        second = expect(await post(
            BOB, payload("bob-store", request_id=shared_id),
        ), 202)
        assert first["store"]["owner_id"] == ALICE
        assert second["store"]["owner_id"] == BOB
        assert first["store"]["id"] != second["store"]["id"]

        await reset()
        same_slug = "shared-address"
        responses = await burst([
            (ALICE, payload(same_slug)),
            (BOB, payload(same_slug)),
        ])
        assert sorted(r.status_code for r in responses) == [202, 409]
        assert await counts() == (1, 1, 1)

        await reset(max_per_user=1)
        expect(await post(ADMIN, payload()), 202)
        expect(await post(ADMIN, payload()), 409)
        print("Request IDs are user-scoped; slug collisions are safe; admin creation obeys limits")

        await reset()
        first_body, second_body = payload(), payload()
        first = expect(await post(ALICE, first_body), 202)
        second = expect(await post(ALICE, second_body), 202)

        async with factory() as db:
            for result, status in ((first, "Failed"), (second, "Deleting")):
                await db.execute(
                    update(Store)
                    .where(Store.id == UUID(result["store"]["id"]))
                    .values(
                        status=status,
                        desired_state="absent" if status == "Deleting" else "present",
                    )
                )
            await db.commit()

        expect(await post(ALICE, payload()), 409)

        async with factory() as db:
            await db.execute(
                update(Store)
                .where(Store.id == UUID(first["store"]["id"]))
                .values(
                    status="Deleted",
                    desired_state="absent",
                    deleted_at=datetime.now(timezone.utc),
                )
            )
            await db.execute(
                update(StoreOperation)
                .where(StoreOperation.id == UUID(first["operation"]["id"]))
                .values(
                    status="Cancelled",
                    finished_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()

        expect(await post(ALICE, first_body), 409)
        expect(await post(ALICE, payload(first_body["slug"])), 409)
        expect(await post(ALICE, payload()), 202)
        print("Failed/deleting stores retain capacity; deletion frees capacity without reviving old requests")

        await reset()

        for model, identity, changes, restore in (
            (User, BOB, {"active": False}, {"active": True}),
            (User, BOB, {"password_hash": None}, {"password_hash": "fixture-only"}),
            (User, BOB, {"session_version": 1}, {"session_version": 0}),
            (
                BrowserSession, session_ids[BOB],
                {"revoked_at": now}, {"revoked_at": None},
            ),
            (
                BrowserSession, session_ids[BOB],
                {"idle_expires_at": now - timedelta(seconds=1)},
                {"idle_expires_at": now + timedelta(minutes=30)},
            ),
            (
                BrowserSession, session_ids[BOB],
                {
                    "idle_expires_at": now - timedelta(seconds=2),
                    "absolute_expires_at": now - timedelta(seconds=1),
                },
                {
                    "idle_expires_at": now + timedelta(minutes=30),
                    "absolute_expires_at": now + timedelta(hours=8),
                },
            ),
        ):
            async def change_after_auth(db):
                async with factory() as writer:
                    await writer.execute(
                        update(model).where(model.id == identity).values(**changes)
                    )
                    await writer.commit()
                await normal_lock(db)

            try:
                with patch.object(api, "lock_store_admission", change_after_auth):
                    expect(await post(BOB, payload()), 401)
            finally:
                async with factory() as writer:
                    await writer.execute(
                        update(model).where(model.id == identity).values(**restore)
                    )
                    await writer.commit()

            assert await counts() == (0, 0, 0)

        print("Account changes, revoked sessions and expiry after initial authentication reject creation")

        await reset()
        arrived = 0
        gate = asyncio.Event()

        async def settings_race(db):
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), 10)
            await normal_lock(db)

        async with client(ADMIN) as admin:
            with (
                patch.object(api, "lock_store_admission", settings_race),
                patch.object(store_settings_api, "lock_store_admission", settings_race),
            ):
                created_response, paused_response = await asyncio.wait_for(
                    asyncio.gather(
                        post(ALICE, payload()),
                        admin.put("/api/admin/store-settings", json={
                            "revision": 1,
                            "enabled": False,
                            "max_per_user": 1,
                            "max_total": 1,
                            "timeout_seconds": 60,
                        }),
                    ),
                    30,
                )

            expect(paused_response, 200)
            assert created_response.status_code in (202, 409), created_response.text

            if created_response.status_code == 202:
                operation = created_response.json()["operation"]
                assert (
                    datetime.fromisoformat(operation["deadline_at"])
                    - datetime.fromisoformat(operation["created_at"])
                ).total_seconds() == 1200

            expect(await post(ALICE, payload()), 409)

        print("Creation and concurrent admin settings changes serialize; admitted deadlines stay fixed")

        await reset()
        flush_count = 0
        original_flush = AsyncSession.flush

        async def failed_after_write(db, *args, **kwargs):
            nonlocal flush_count
            await original_flush(db, *args, **kwargs)
            flush_count += 1
            if flush_count == 2:
                raise SQLAlchemyError(PRIVATE)

        retry_body = payload()
        with patch.object(AsyncSession, "flush", failed_after_write):
            expect(await post(ALICE, retry_body), 503)

        assert flush_count == 2 and await counts() == (0, 0, 0)
        expect(await post(ALICE, retry_body), 202)
        assert await counts() == (1, 1, 1)

        await reset()

        with patch.dict(os.environ, {"STORE_BASE_DOMAIN": "invalid://domain"}):
            expect(await post(ALICE, payload()), 503)
        assert await counts() == (0, 0, 0)

        async with factory() as db:
            await db.execute(delete(StoreProvisioningSettings))
            await db.commit()

        expect(await post(ALICE, payload()), 503)
        assert await counts() == (0, 0, 0)
        print("Partial-write rollback, safe retry, invalid configuration and missing settings passed")
        print("Store creation verification passed. Live accounts/settings and external services were unchanged.")

    finally:
        if isolated is not None:
            await isolated.dispose()

        try:
            if created:
                assert schema.startswith("urumi_store_create_test_") and len(schema) < 64
                async with engine.begin() as connection:
                    await connection.execute(DropSchema(schema, cascade=True))
                print("Temporary PostgreSQL schema removed.")
        finally:
            await engine.dispose()


if __name__ == "__main__":
    with patch.dict(os.environ, {
        "STORE_BASE_DOMAIN": "localtest.me",
        "STORE_URL_SCHEME": "http",
    }):
        asyncio.run(asyncio.wait_for(check(), 180))
