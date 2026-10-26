"""Permission writes recheck the admin after discovery or account-lock waits.

All database/discovery operations are fixtures; no live grants are changed.
"""
import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from fastapi import HTTPException
from mcp.types import Tool

os.environ.update(PGPASSWORD="unused-permission-fixture", JWT_SECRET="permission-fixture-" * 3,
                  FERNET_KEY=Fernet.generate_key().decode())
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import permissions
from app.models import MCPServer, User


async def scenario(change, *, revoke=False):
    actor = SimpleNamespace(id=1, role="admin", active=True, password_hash="fixture", session_version=0)
    current = {**vars(actor)}
    member = SimpleNamespace(id=7, role="member", active=True)
    server = SimpleNamespace(id=11, timeout_s=15, slug="notes", url="http://fixture.invalid/mcp",
                             auth_header_name=None, credential_encrypted=None, enabled=True)
    writes, locks, commits = [], [], []

    class Database:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def expunge(self, value): pass
        async def scalar(self, statement):
            model = statement.column_descriptions[0]["entity"]
            if model is MCPServer: return server
            assert model is User
            target = next(iter(statement.compile().params.values()))
            if target == actor.id:
                assert statement._for_update_arg is not None
                if revoke: current.update(change)
                return SimpleNamespace(**current)
            assert target == member.id
            return member
        async def execute(self, statement):
            if getattr(statement, "is_insert", False) or getattr(statement, "is_delete", False):
                writes.append(statement)
            else:
                assert "pg_advisory_xact_lock" in str(statement)
                locks.append(True)
        async def commit(self): commits.append(True)

    async def discover(*args, **kwargs):
        current.update(change)
        return [Tool(name="get_note", inputSchema={"type": "object"})]

    body = permissions.PermissionBody(name="notes__get_note")
    with patch.object(permissions, "Session", Database), \
         patch.object(permissions, "release_auth_connection", AsyncMock()), \
         patch.object(permissions, "discover_tools", discover):
        try:
            if revoke:
                result = await permissions.revoke_permission(7, body, actor, Database())
            else:
                result = await permissions.grant_permission(7, body, actor, Database())
        except HTTPException as error:
            assert change and error.status_code == 401
            assert not writes and not commits
        else:
            assert not change, "Changed admin was allowed to write permissions"
            assert len(writes) == len(commits) == 1
            assert result.status_code == 204 if revoke else result["granted"] is True
    assert len(locks) == 1


async def check():
    for revoke in (False, True):
        for change in ({"active": False}, {"role": "member"}, {"session_version": 1}, {"password_hash": None}, {}):
            await scenario(change, revoke=revoke)
    print("Disabled admins, changed roles/session versions and unavailable accounts send no permission write.")
    print("Fresh admins still grant and revoke once; the account lock precedes the write.")
    print("Permission recheck verification passed. All database/discovery operations were fixtures.")


if __name__ == "__main__":
    asyncio.run(check())
