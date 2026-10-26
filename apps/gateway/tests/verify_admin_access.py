"""Run inside apps/gateway with gateway-local-env.sh sourced.

Uses an isolated PostgreSQL schema and fake accounts. No actual account, key,
product or chat is modified; no Groq or upstream MCP requests are made.
"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

if not (Path.cwd() / "app/admin_access.py").is_file():
    raise SystemExit("Run from Wo_MCP/apps/gateway after applying the update and sourcing the local environment.")
sys.path.insert(0, str(Path.cwd()))

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema
from starlette.routing import Route

from app import admin_access, auth, core, mcp_api, tools, users
from app import rate_limits
from app.models import UsageLimitSettings
from app.config import database_url
from app.database import engine, get_session
from app.models import APIKey, AuditLog, Base, BrowserSession, ChatConversation, User
from app.security import create_session_token, hash_password, token_hash


async def check():
    schema = "urumi_access_test_" + uuid4().hex
    created = False
    isolated_engine = None
    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
        created = True
        isolated_engine = create_async_engine(database_url(), hide_parameters=True,
            connect_args={"options": f"-c search_path={schema} -c timezone=UTC"})
        factory = async_sessionmaker(isolated_engine, expire_on_commit=False)
        async with isolated_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        password = "fixture-pass-123"
        password_hash = await hash_password(password)
        async with factory() as session:
            session.add(UsageLimitSettings(id=1, login_account_limit=100, login_ip_limit=1000))
            session.add_all([
                User(id=50101, email="admin-a@access.invalid", role="admin", active=True, password_hash=password_hash),
                User(id=50102, email="member@access.invalid", role="member", active=True, password_hash=password_hash),
                User(id=50103, email="admin-b@access.invalid", role="admin", active=False, password_hash=password_hash),
                User(id=50104, email="pending@access.invalid", role="member", active=True),
            ])
            await session.flush()
            session.add(ChatConversation(user_id=50102, title="Preserved member chat"))
            session.add(AuditLog(user_id=50102, server_slug="fixture", tool="fixture__read", duration_ms=1,
                source="dashboard", outcome="success"))
            await session.commit()
            stale_member = await session.get(User, 50102)
            session.expunge(stale_member)

        fixture_tokens = {}
        async with factory() as session:
            for user_id in (50101, 50102):
                now = datetime.now(timezone.utc)
                nonce = secrets.token_urlsafe(32)
                session.add(BrowserSession(id=token_hash(nonce), user_id=user_id, session_version=0,
                    created_at=now, last_activity_at=now, idle_expires_at=now + timedelta(minutes=30),
                    absolute_expires_at=now + timedelta(hours=8)))
                fixture_tokens[user_id] = create_session_token(user_id, session_id=nonce)
            await session.commit()

        async def dependency():
            async with factory() as session:
                yield session

        app = FastAPI()
        for router in (auth.router, users.router, admin_access.router, tools.router):
            app.include_router(router)
        app.router.routes.append(Route("/mcp", endpoint=mcp_api.mcp_app))
        app.dependency_overrides[get_session] = dependency

        def client(user_id=None):
            instance = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8003")
            if user_id is not None:
                instance.cookies.set("urm_session", fixture_tokens[user_id])
                instance.cookies.set("urm_csrf", "fixture-csrf")
                instance.headers["X-CSRF-Token"] = "fixture-csrf"
            return instance

        async def expect(response, status):
            assert response.status_code == status, (response.status_code, response.text)
            return response.json() if response.content else None

        with patch.object(rate_limits, "Session", factory), patch.object(auth, "Session", factory), patch.object(core, "Session", factory), patch.object(mcp_api, "Session", factory):
            async with client(50101) as admin, client(50102) as member, client() as anonymous:
                endpoint = "/api/admin/users/50102/keys"
                await expect(await anonymous.get(endpoint), 401)
                await expect(await member.get(endpoint), 403)
                await expect(await member.patch("/api/admin/users/50102/active", json={"active": False}), 403)
                await expect(await member.get("/api/admin/security-events"), 403)
                await expect(await admin.post(endpoint, json={"name": "No CSRF"}, headers={"X-CSRF-Token": ""}), 403)
                await expect(await admin.post(endpoint, json={"name": "Override", "user_id": 50101}), 422)
                await expect(await admin.post("/api/admin/users/50104/keys", json={"name": "Pending"}), 409)
                print("Admin endpoints: anonymous/member access, missing CSRF, caller override and unregistered issuance rejected")

                first = await expect(await admin.post(endpoint, json={"name": "Admin-issued fixture"}), 201)
                raw = first["key"]
                assert first["user_id"] == 50102
                data = await expect(await admin.get(endpoint), 200)
                assert data["items"][0]["id"] == first["id"]
                assert "key" not in data["items"][0] and "key_hash" not in data["items"][0]
                own = await expect(await member.get("/api/keys"), 200)
                assert first["id"] in [item["id"] for item in own]
                assert await expect(await member.get("/api/key-check", headers={"Authorization": f"Bearer {raw}"}), 200)
                async with factory() as session:
                    saved = await session.get(APIKey, first["id"])
                    assert saved.key_hash == token_hash(raw) and saved.key_hash != raw
                await expect(await admin.delete(f"/api/admin/users/50104/keys/{first['id']}"), 404)
                await expect(await admin.delete(f"{endpoint}/{first['id']}"), 204)
                await expect(await member.get("/api/key-check", headers={"Authorization": f"Bearer {raw}"}), 401)
                print("Admin-issued key belongs to the member, is hashed, listed without its secret, and revoked immediately")

                second = await expect(await member.post("/api/keys", json={"name": "Member fixture"}), 201)
                third = await expect(await admin.post(endpoint, json={"name": "Admin fixture 2"}), 201)
                page = await expect(await admin.get(endpoint, params={"limit": 1}), 200)
                assert page["next_before_id"]
                older = await expect(await admin.get(endpoint, params={"limit": 1, "before_id": page["next_before_id"]}), 200)
                assert older["items"][0]["id"] < page["items"][0]["id"]
                result = await expect(await admin.patch("/api/admin/users/50102/active", json={"active": False}), 200)
                assert not result["user"]["active"] and result["revoked_keys"] == 2
                async with factory() as session:
                    assert await session.scalar(select(func.count()).select_from(BrowserSession).where(
                        BrowserSession.user_id == 50102, BrowserSession.revoked_at.is_not(None))) == 1
                await expect(await member.get("/api/auth/me"), 401)
                await expect(await member.post("/api/auth/login", json={"email": "member@access.invalid", "password": password}), 401)
                await expect(await member.post("/api/keys", json={"name": "Forbidden after disabling"}), 401)
                for key in (second, third):
                    await expect(await member.get("/api/key-check", headers={"Authorization": f"Bearer {key['key']}"}), 401)
                denied = await member.post("/mcp", headers={"Authorization": f"Bearer {second['key']}",
                    "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1,
                    "method": "tools/call", "params": {"name": "fixture__read", "arguments": {}}})
                await expect(denied, 401)
                await expect(await admin.post(endpoint, json={"name": "Forbidden disabled issuance"}), 409)
                result = await expect(await admin.patch("/api/admin/users/50102/active", json={"active": True}), 200)
                assert result["user"]["active"]
                await expect(await member.get("/api/auth/me"), 401)
                for key in (second, third):
                    await expect(await member.get("/api/key-check", headers={"Authorization": f"Bearer {key['key']}"}), 401)
                stale_result = await core.execute_gateway_tool(stale_member, "fixture__read", {}, source="dashboard")
                assert stale_result.isError and stale_result.content[0].text == "Account is unavailable."
                async with factory() as session:
                    assert await session.scalar(select(func.count()).select_from(ChatConversation).where(ChatConversation.user_id == 50102)) == 1
                    assert await session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.user_id == 50102)) >= 3
                print("Disable blocks login, cookies and MCP keys; re-enable does not revive old credentials; chats/audits preserved")

                async with client() as fresh:
                    await expect(await fresh.post("/api/auth/login", json={"email": "member@access.invalid", "password": password}), 200)
                    fresh.headers["X-CSRF-Token"] = fresh.cookies["urm_csrf"]
                    await expect(await fresh.get("/api/auth/me"), 200)
                    new_key = await expect(await fresh.post("/api/keys", json={"name": "Fresh after enabling"}), 201)
                    await expect(await fresh.get("/api/key-check", headers={"Authorization": f"Bearer {new_key['key']}"}), 200)
                events = await expect(await admin.get("/api/admin/security-events"), 200)
                actions = {event["action"] for event in events["items"]}
                assert {"api_key.issued", "api_key.revoked", "account.disabled", "account.enabled"} <= actions
                for secret in (raw, second["key"], third["key"], new_key["key"]):
                    assert secret not in json.dumps(events)
                print("Fresh login/new key work; security events identify actor and target without full keys")

                response = await admin.patch("/api/admin/users/50101/active", json={"active": False})
                data = await expect(response, 409)
                assert "last active admin" in data["detail"]
                await expect(await admin.patch("/api/admin/users/50103/active", json={"active": True}), 200)
                async with client() as other:
                    await expect(await other.post("/api/auth/login", json={"email": "admin-b@access.invalid", "password": password}), 200)
                    other.headers["X-CSRF-Token"] = other.cookies["urm_csrf"]
                    await expect(await admin.patch("/api/admin/users/50101/active", json={"active": False}), 409)
                    responses = await asyncio.gather(
                        admin.patch("/api/admin/users/50103/active", json={"active": False}),
                        other.patch("/api/admin/users/50101/active", json={"active": False}))
                    assert sorted(response.status_code for response in responses) == [200, 401], [(r.status_code, r.text) for r in responses]
                    async with factory() as session:
                        count = await session.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.active.is_(True)))
                        assert count == 1
                print("Self-disable and last-admin protections work, including concurrent cross-admin requests")
        print("Access verification passed. No real accounts or upstream data were changed.")
    finally:
        if isolated_engine is not None:
            await isolated_engine.dispose()
        if created:
            assert schema.startswith("urumi_access_test_")
            async with engine.begin() as connection:
                await connection.execute(DropSchema(schema, cascade=True))
            print("Temporary test schema removed.")
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(check())
