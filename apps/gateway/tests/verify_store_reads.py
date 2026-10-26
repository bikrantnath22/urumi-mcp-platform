"""Verify store reads with real auth and isolated PostgreSQL data.

Run from apps/gateway with gateway-local-env.sh sourced.
No Kubernetes, Groq, WordPress or live-account changes are requested.
"""

import asyncio
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

if not Path("app/stores.py").is_file():
    raise SystemExit("Run from Wo_MCP/apps/gateway.")
sys.path.insert(0, str(Path.cwd()))

import httpx
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app import stores as api
from app.config import database_url
from app.database import engine, get_session
from app.models import (
    Base, BrowserSession, SecurityEvent, Store, StoreOperation,
    StoreProvisioningSettings, User,
)
from app.security import create_session_token, token_hash

ADMIN, ALICE, BOB, EMPTY = 97101, 97102, 97103, 97104
PRIVATE = "fixture-private-diagnostic"
ROOT = "/api/stores"


def expect(response, status):
    assert response.status_code == status, (
        response.status_code, response.text,
    )
    if status == 200:
        assert response.headers["cache-control"] == "no-store"
    return response.json()


async def check():
    schema = "urumi_store_reads_test_" + uuid4().hex
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
            await connection.run_sync(Base.metadata.create_all)

        now = datetime.now(timezone.utc)
        base = now - timedelta(minutes=5)
        fixtures = (
            ("alice-ready", ALICE, "Ready", 0),
            ("alice-provisioning", ALICE, "Provisioning", -1),
            ("alice-failed", ALICE, "Failed", 0),
            ("alice-deleting", ALICE, "Deleting", -2),
            ("alice-deleted", ALICE, "Deleted", 1),
            ("bob-ready", BOB, "Ready", 0),
            ("bob-deleted", BOB, "Deleted", -1),
            ("admin-ready", ADMIN, "Ready", 1),
        )
        records, ids, tokens, session_ids = [], {}, {}, {}

        async with factory() as db:
            for user_id in (ADMIN, ALICE, BOB, EMPTY):
                db.add(User(
                    id=user_id,
                    email=f"user-{user_id}@fixture.invalid",
                    role="admin" if user_id == ADMIN else "member",
                    active=True,
                    password_hash="fixture-only",
                ))
            await db.flush()

            db.add(StoreProvisioningSettings(
                id=1, enabled=False, max_per_user=2, max_total=4,
            ))

            for user_id in (ADMIN, ALICE, BOB, EMPTY):
                nonce = secrets.token_urlsafe(32)
                session_ids[user_id] = token_hash(nonce)
                tokens[user_id] = create_session_token(
                    user_id, session_id=nonce,
                )
                db.add(BrowserSession(
                    id=session_ids[user_id],
                    user_id=user_id,
                    session_version=0,
                    created_at=now,
                    last_activity_at=now,
                    idle_expires_at=now + timedelta(minutes=30),
                    absolute_expires_at=now + timedelta(hours=8),
                ))

            for index, (name, owner, status, minutes) in enumerate(fixtures, 1):
                store_id = UUID(int=index * 10)
                ids[name] = store_id
                record = Store(
                    id=store_id,
                    owner_id=owner,
                    name=name,
                    slug=name,
                    namespace="fixture-" + name,
                    url=f"https://{name}.fixture.invalid",
                    status=status,
                    desired_state=(
                        "absent"
                        if status in ("Deleting", "Deleted")
                        else "present"
                    ),
                    deleted_at=now if status == "Deleted" else None,
                    created_at=base + timedelta(minutes=minutes),
                )
                records.append(record)
                db.add(record)
            await db.flush()

            # Equal timestamps exercise operation-ID ordering.
            for number, status in (
                (500, "Failed"),
                (600, "Succeeded"),
                (700, "Running"),
            ):
                running = status == "Running"
                target = (
                    ids["alice-provisioning"]
                    if running
                    else ids["alice-ready"]
                )
                db.add(StoreOperation(
                    id=UUID(int=number),
                    store_id=target,
                    requested_by_id=ALICE,
                    request_key=UUID(int=number + 1000),
                    request_fingerprint="a" * 64,
                    action="provision",
                    store_generation=1,
                    status=status,
                    attempts=1,
                    created_at=base,
                    deadline_at=now + timedelta(minutes=20),
                    started_at=base,
                    finished_at=(
                        None if running else base + timedelta(seconds=1)
                    ),
                    lease_token=UUID(int=900) if running else None,
                    lease_expires_at=(
                        now + timedelta(minutes=1) if running else None
                    ),
                    last_error=PRIVATE,
                ))
            await db.commit()

        ordered = sorted(
            records,
            key=lambda row: (row.created_at, row.id.int),
            reverse=True,
        )

        def expected_ids(owner=None):
            return [
                str(row.id) for row in ordered
                if row.status != "Deleted"
                and (owner is None or row.owner_id == owner)
            ]

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
            return instance

        async def snapshot():
            async with factory() as db:
                result = {}
                for model in (
                    Store, StoreOperation, BrowserSession, SecurityEvent,
                ):
                    table = model.__table__
                    result[table.name] = (
                        await db.execute(
                            select(table).order_by(table.c.id)
                        )
                    ).all()
                return result

        async def pages(client_instance, expected, size):
            collected, cursor, cursors = [], None, set()

            for _ in range(len(expected) + 2):
                params = {"limit": size}
                if cursor is not None:
                    params["before_id"] = cursor

                data = expect(
                    await client_instance.get(ROOT, params=params), 200,
                )
                items = data["items"]
                assert len(items) <= size
                collected.extend(item["id"] for item in items)
                cursor = data["next_before_id"]

                if cursor is None:
                    break

                assert items and cursor == items[-1]["id"]
                assert cursor not in cursors
                cursors.add(cursor)
            else:
                raise AssertionError("Pagination did not terminate.")

            assert collected == expected, (collected, expected)
            assert len(collected) == len(set(collected))

        before_reads = await snapshot()

        async with (
            client(ADMIN) as admin,
            client(ALICE) as alice,
            client(BOB) as bob,
            client(EMPTY) as empty,
            client() as anonymous,
        ):
            expect(await anonymous.get(ROOT), 401)
            expect(await anonymous.get(
                f"{ROOT}/{ids['alice-ready']}"
            ), 401)

            for instance, owner in (
                (admin, None), (alice, ALICE),
                (bob, BOB), (empty, EMPTY),
            ):
                data = expect(await instance.get(ROOT), 200)
                assert [
                    item["id"] for item in data["items"]
                ] == expected_ids(owner)
                assert data["next_before_id"] is None

            forged = expect(await alice.get(ROOT, params={
                "owner_id": BOB, "user_id": ADMIN,
            }), 200)
            assert [
                item["id"] for item in forged["items"]
            ] == expected_ids(ALICE)

            print("Authentication and ownership: members see their own stores; admin sees all")

            for instance, name in (
                (alice, "bob-ready"),
                (alice, "bob-deleted"),
                (bob, "alice-ready"),
                (admin, "alice-deleted"),
            ):
                data = expect(
                    await instance.get(f"{ROOT}/{ids[name]}"), 404,
                )
                assert data == {"detail": "Store not found."}

            expect(await alice.get(f"{ROOT}/{uuid4()}"), 404)

            for cursor in (
                ids["bob-ready"], ids["bob-deleted"], uuid4(),
            ):
                expect(await alice.get(
                    ROOT, params={"before_id": cursor},
                ), 404)

            print("Other members' store IDs/cursors and deleted-store details are hidden")

            for params in (
                {"limit": 0},
                {"limit": 101},
                {"before_id": "invalid"},
            ):
                expect(await alice.get(ROOT, params=params), 422)
            expect(await alice.get(f"{ROOT}/invalid"), 422)

            for instance, owner in (
                (admin, None), (alice, ALICE),
                (bob, BOB), (empty, EMPTY),
            ):
                for size in (1, 2):
                    await pages(instance, expected_ids(owner), size)

            print("Pagination: equal timestamps, UUID ordering, bounds and empty results passed")

            for name, operation_id in (
                ("alice-ready", UUID(int=600)),
                ("alice-provisioning", UUID(int=700)),
            ):
                response = await alice.get(f"{ROOT}/{ids[name]}")
                data = expect(response, 200)
                assert data["store"]["id"] == str(ids[name])
                assert data["operation"]["id"] == str(operation_id)
                assert not {
                    "lease_token", "lease_expires_at", "request_key",
                    "request_fingerprint", "last_error",
                }.intersection(data["operation"])
                assert PRIVATE not in response.text

            data = expect(await bob.get(
                f"{ROOT}/{ids['bob-ready']}"
            ), 200)
            assert data["operation"] is None

            print("Details: latest operation selected; worker leases and raw errors stay private")

            with patch.object(
                api, "visible_to",
                side_effect=SQLAlchemyError(PRIVATE),
            ):
                for path in (ROOT, f"{ROOT}/{ids['alice-ready']}"):
                    response = await alice.get(path)
                    expect(response, 503)
                    assert PRIVATE not in response.text

            assert await snapshot() == before_reads
            print("Read requests preserve stored data/sessions; injected errors return safe HTTP 503")

            # Simulate cleanup while the user is paging.
            first = expect(await alice.get(
                ROOT, params={"limit": 1},
            ), 200)
            cursor = first["next_before_id"]
            assert cursor is not None

            async with factory() as db:
                await db.execute(
                    update(Store)
                    .where(Store.id == UUID(cursor))
                    .values(
                        status="Deleted",
                        desired_state="absent",
                        deleted_at=now,
                    )
                )
                await db.commit()

            remaining = expect(await alice.get(
                ROOT, params={"before_id": cursor},
            ), 200)
            assert [
                item["id"] for item in remaining["items"]
            ] == expected_ids(ALICE)[1:]
            expect(await alice.get(f"{ROOT}/{cursor}"), 404)

            print("A deleted pagination anchor still works without exposing its details")

            async with factory() as db:
                await db.execute(
                    update(User)
                    .where(User.id == BOB)
                    .values(active=False)
                )
                await db.execute(
                    update(BrowserSession)
                    .where(BrowserSession.id == session_ids[ALICE])
                    .values(revoked_at=now)
                )
                await db.execute(
                    update(BrowserSession)
                    .where(BrowserSession.id == session_ids[EMPTY])
                    .values(idle_expires_at=now - timedelta(seconds=1))
                )
                await db.commit()

            for instance, name in (
                (alice, "alice-ready"),
                (bob, "bob-ready"),
                (empty, "admin-ready"),
            ):
                expect(await instance.get(ROOT), 401)
                expect(await instance.get(
                    f"{ROOT}/{ids[name]}"
                ), 401)

            print("Disabled accounts, revoked sessions and expired sessions are rejected")

        print("Store read verification passed. Live accounts/settings were unchanged.")

    finally:
        if isolated_engine is not None:
            await isolated_engine.dispose()
        try:
            if created:
                assert schema.startswith("urumi_store_reads_test_")
                async with engine.begin() as connection:
                    await connection.execute(
                        DropSchema(schema, cascade=True)
                    )
                print("Temporary PostgreSQL schema removed.")
        finally:
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(check())