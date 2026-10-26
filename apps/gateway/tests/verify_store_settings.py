"""Verify store settings with isolated PostgreSQL data.

Run from apps/gateway with gateway-local-env.sh sourced.
Uses fake accounts and a temporary schema; no external services are called.
"""

import asyncio
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

if not Path("app/store_settings_api.py").is_file():
    raise SystemExit("Run from Wo_MCP/apps/gateway.")

sys.path.insert(0, str(Path.cwd()))

import httpx
from fastapi import FastAPI
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app import store_settings_api as api
from app.config import database_url
from app.database import engine, get_session
from app.models import (
    Base,
    BrowserSession,
    SecurityEvent,
    Store,
    StoreOperation,
    StoreProvisioningSettings,
    User,
)
from app.security import APP_ORIGIN, create_session_token, token_hash
from app.store_settings import EDITABLE_FIELDS, public_store_settings

ENDPOINT = "/api/admin/store-settings"
ADMIN_A, MEMBER, ADMIN_B = 96101, 96102, 96103


def expect(response, status):
    assert response.status_code == status, (
        response.status_code,
        response.text,
    )
    return response.json()


def body(settings, **changes):
    return {
        "revision": settings["revision"],
        **{name: settings[name] for name in EDITABLE_FIELDS},
        **changes,
    }


async def check():
    schema = "urumi_store_settings_test_" + uuid4().hex
    created = False
    isolated_engine = None

    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
        created = True

        isolated_engine = create_async_engine(
            database_url(),
            hide_parameters=True,
            connect_args={
                "connect_timeout": 5,
                "options": (
                    f"-c search_path={schema} -c timezone=UTC "
                    "-c statement_timeout=10000 -c lock_timeout=5000"
                ),
            },
        )
        factory = async_sessionmaker(
            isolated_engine,
            expire_on_commit=False,
        )

        async with isolated_engine.begin() as connection:
            assert await connection.scalar(select(func.current_schema())) == schema
            await connection.run_sync(Base.metadata.create_all)

        tokens, session_ids = {}, {}
        store_id, operation_id = uuid4(), uuid4()
        deadline = datetime.now(timezone.utc) + timedelta(minutes=20)

        async with factory() as db:
            for user_id, role in (
                (ADMIN_A, "admin"),
                (MEMBER, "member"),
                (ADMIN_B, "admin"),
            ):
                db.add(User(
                    id=user_id,
                    email=f"user-{user_id}@fixture.invalid",
                    role=role,
                    active=True,
                    password_hash="fixture-only",
                ))

            await db.flush()
            db.add(StoreProvisioningSettings(id=1))

            for user_id in (ADMIN_A, MEMBER, ADMIN_B):
                now = datetime.now(timezone.utc)
                nonce = secrets.token_urlsafe(32)
                session_ids[user_id] = token_hash(nonce)

                db.add(BrowserSession(
                    id=session_ids[user_id],
                    user_id=user_id,
                    session_version=0,
                    created_at=now,
                    last_activity_at=now,
                    idle_expires_at=now + timedelta(minutes=30),
                    absolute_expires_at=now + timedelta(hours=8),
                ))
                tokens[user_id] = create_session_token(
                    user_id,
                    session_id=nonce,
                )

            db.add(Store(
                id=store_id,
                owner_id=MEMBER,
                name="Fixture store",
                slug="fixture-store",
                namespace="fixture-store",
                url="https://fixture-store.invalid",
                status="Ready",
            ))
            await db.flush()

            db.add(StoreOperation(
                id=operation_id,
                store_id=store_id,
                requested_by_id=MEMBER,
                request_key=uuid4(),
                request_fingerprint="a" * 64,
                action="provision",
                store_generation=1,
                status="Succeeded",
                deadline_at=deadline,
            ))
            await db.commit()

        async def dependency():
            async with factory() as db:
                yield db

        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[get_session] = dependency

        def client(user_id=None):
            instance = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://fixture.invalid",
                trust_env=False,
            )
            if user_id is not None:
                instance.cookies.set("urm_session", tokens[user_id])
                instance.cookies.set("urm_csrf", "fixture-csrf")
                instance.headers.update({
                    "X-CSRF-Token": "fixture-csrf",
                    "Origin": APP_ORIGIN,
                })
            return instance

        async def snapshot():
            async with factory() as db:
                record = await db.get(StoreProvisioningSettings, 1)
                events = list((await db.scalars(
                    select(SecurityEvent).order_by(SecurityEvent.id)
                )).all())
                return public_store_settings(record), events

        async with (
            client(ADMIN_A) as admin,
            client(ADMIN_B) as other,
            client(MEMBER) as member,
            client() as anonymous,
        ):
            response = await admin.get(ENDPOINT)
            original = expect(response, 200)

            assert response.headers["cache-control"] == "no-store"
            assert body(original) == {
                "revision": 1,
                "enabled": False,
                "max_per_user": 2,
                "max_total": 2,
                "max_concurrent_creations": 2,
                "timeout_seconds": 1200,
            }

            for method in ("get", "put"):
                kwargs = {"json": body(original)} if method == "put" else {}
                expect(await getattr(anonymous, method)(
                    ENDPOINT, **kwargs
                ), 401)
                expect(await getattr(member, method)(
                    ENDPOINT, **kwargs
                ), 403)

            for headers in (
                {"X-CSRF-Token": ""},
                {"X-CSRF-Token": "wrong"},
                {"Origin": "https://untrusted.invalid"},
            ):
                expect(await admin.put(
                    ENDPOINT,
                    json=body(original),
                    headers=headers,
                ), 403)

            for invalid in (
                {"updated_by_id": MEMBER},
                {"enabled": "true"},
                {"max_per_user": 3},
                {"max_total": 101},
                {"max_per_user": 0},
                {"max_concurrent_creations": 0},
                {"max_concurrent_creations": 101},
                {"max_concurrent_creations": True},
                {"max_concurrent_creations": 1.5},
                {"max_concurrent_creations": "4"},
                {"max_concurrent_creations": None},
                {"timeout_seconds": 61},
                {"timeout_seconds": 3660},
                {"revision": 0},
            ):
                expect(await admin.put(
                    ENDPOINT,
                    json=body(original, **invalid),
                ), 422)

            saved, events = await snapshot()
            assert body(saved) == body(original) and not events
            print("Access: anonymous/member, CSRF, origin and invalid edits rejected")

            response = await admin.put(ENDPOINT, json=body(
                original,
                enabled=True,
                max_per_user=3,
                max_total=6,
                max_concurrent_creations=4,
                timeout_seconds=1800,
            ))
            current = expect(response, 200)

            assert response.headers["cache-control"] == "no-store"
            saved, events = await snapshot()
            assert saved["revision"] == 2
            assert saved["updated_by_id"] == ADMIN_A
            assert body(saved) == body(current)
            assert len(events) == 1
            assert events[0].actor_id == ADMIN_A
            assert events[0].action == "store_settings.updated"
            assert events[0].details == {
                "before": {k: original[k] for k in EDITABLE_FIELDS},
                "after": {k: current[k] for k in EDITABLE_FIELDS},
                "revision": 2,
            }

            expect(await admin.put(
                ENDPOINT,
                json=body(original, max_total=9),
            ), 409)

            unchanged = expect(await admin.put(
                ENDPOINT,
                json=body(current),
            ), 200)
            assert unchanged["revision"] == 2
            assert len((await snapshot())[1]) == 1

            assert current["max_concurrent_creations"] == 4
            legacy_body = body(current)
            del legacy_body["max_concurrent_creations"]
            legacy = expect(await admin.put(ENDPOINT, json=legacy_body), 200)
            assert legacy["max_concurrent_creations"] == 4
            assert legacy["revision"] == current["revision"]
            assert len((await snapshot())[1]) == 1
            print("Parallel capacity persists; audit, stale edits, no-op and older-dashboard saves passed")

            arrived = 0
            gate = asyncio.Event()
            original_lock = api.lock_store_admission

            async def concurrent_lock(db):
                nonlocal arrived
                arrived += 1
                if arrived == 2:
                    gate.set()
                await asyncio.wait_for(gate.wait(), timeout=10)
                await original_lock(db)

            with patch.object(api, "lock_store_admission", concurrent_lock):
                responses = await asyncio.wait_for(
                    asyncio.gather(
                        admin.put(
                            ENDPOINT,
                            json=body(
                                current, max_total=7, max_concurrent_creations=3,
                            ),
                        ),
                        other.put(
                            ENDPOINT,
                            json=body(
                                current, max_total=8, max_concurrent_creations=5,
                            ),
                        ),
                    ),
                    timeout=20,
                )

            assert sorted(r.status_code for r in responses) == [200, 409], [
                (r.status_code, r.text) for r in responses
            ]
            winner = next(
                i for i, r in enumerate(responses)
                if r.status_code == 200
            )
            current = expect(responses[winner], 200)
            saved, events = await snapshot()

            assert saved["revision"] == 3 and len(events) == 2
            assert saved["max_total"] == (7, 8)[winner]
            assert saved["max_concurrent_creations"] == (3, 5)[winner]
            assert saved["updated_by_id"] == (ADMIN_A, ADMIN_B)[winner]
            assert events[-1].actor_id == saved["updated_by_id"]
            assert body(expect(
                await admin.get(ENDPOINT), 200
            )) == body(saved)

            print("Concurrent PostgreSQL saves: one succeeds; one stale edit is rejected")

            # Change account/session state after initial authentication.
            for model, identity, changes, restore, status in (
                (
                    BrowserSession,
                    session_ids[ADMIN_B],
                    {"revoked_at": datetime.now(timezone.utc)},
                    {"revoked_at": None},
                    401,
                ),
                (
                    User, ADMIN_B,
                    {"active": False}, {"active": True}, 401,
                ),
                (
                    User, ADMIN_B,
                    {"role": "member"}, {"role": "admin"}, 403,
                ),
                (
                    User, ADMIN_B,
                    {"session_version": 1}, {"session_version": 0}, 401,
                ),
            ):
                async def changed_before_lock(db):
                    async with factory() as writer:
                        await writer.execute(
                            update(model)
                            .where(model.id == identity)
                            .values(**changes)
                        )
                        await writer.commit()
                    await original_lock(db)

                try:
                    with patch.object(
                        api, "lock_store_admission", changed_before_lock
                    ):
                        expect(await other.put(
                            ENDPOINT,
                            json=body(current, max_total=9),
                        ), status)
                finally:
                    async with factory() as writer:
                        await writer.execute(
                            update(model)
                            .where(model.id == identity)
                            .values(**restore)
                        )
                        await writer.commit()

                saved, events = await snapshot()
                assert body(saved) == body(current)
                assert len(events) == 2

            print("Revoked sessions and account changes are rechecked before saving")

            current = expect(await admin.put(ENDPOINT, json=body(
                current,
                enabled=False,
                max_per_user=1,
                max_total=1,
                max_concurrent_creations=1,
                timeout_seconds=60,
            )), 200)

            async with factory() as db:
                store = await db.get(Store, store_id)
                operation = await db.get(StoreOperation, operation_id)
                assert store.status == "Ready"
                assert store.desired_state == "present"
                assert operation.status == "Succeeded"
                assert operation.deadline_at == deadline

            print("Pausing/lowering settings preserves existing stores and operation deadlines")

            async with factory() as db:
                for invalid in (0, 101, None):
                    try:
                        async with db.begin_nested():
                            await db.execute(
                                update(StoreProvisioningSettings)
                                .where(StoreProvisioningSettings.id == 1)
                                .values(max_concurrent_creations=invalid)
                            )
                    except IntegrityError:
                        pass
                    else:
                        raise AssertionError("Database accepted invalid parallel capacity.")
            assert (await snapshot())[0]["max_concurrent_creations"] == 1
            print("Database rejects invalid parallel capacity independently of the API")

            async def storage_error(*args, **kwargs):
                raise SQLAlchemyError("fixture-private-storage-detail")

            with patch.object(api, "get_store_settings", storage_error):
                for response in (
                    await admin.get(ENDPOINT),
                    await admin.put(
                        ENDPOINT,
                        json=body(current, max_total=2),
                    ),
                ):
                    expect(response, 503)
                    assert "fixture-private-storage-detail" not in response.text

            saved, events = await snapshot()
            assert body(saved) == body(current)
            assert len(events) == 3

            async with factory() as db:
                await db.execute(delete(StoreProvisioningSettings))
                await db.commit()

            expect(await admin.get(ENDPOINT), 503)
            expect(await admin.put(
                ENDPOINT,
                json=body(current),
            ), 503)

            async with factory() as db:
                events = list((await db.scalars(
                    select(SecurityEvent)
                )).all())
                assert len(events) == 3

            print("Missing settings/storage failures reject edits without leaking diagnostics")

        print("Store settings verification passed. Live accounts/settings were unchanged.")

    finally:
        if isolated_engine is not None:
            await isolated_engine.dispose()
        try:
            if created:
                assert schema.startswith("urumi_store_settings_test_")
                async with engine.begin() as connection:
                    await connection.execute(DropSchema(schema, cascade=True))
                print("Temporary PostgreSQL schema removed.")
        finally:
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 180))