"""API transaction composition using fixtures; no database or HTTP upstreams."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException, Response
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from app import store_lifecycle as module, store_access
from app.models import SecurityEvent, StoreOperation


class Fixture:
    def __init__(self):
        self.now = datetime.now(timezone.utc)
        self.user = SimpleNamespace(id=7, session_version=0)
        self.actor = SimpleNamespace(id=7, session_version=0, role="admin", active=True, password_hash="fixture")
        self.store = SimpleNamespace(
            id=uuid4(), owner_id=8, name="Fixture", slug="fixture", url="http://fixture.localtest.me",
            namespace="", release_name="store", status="Ready", desired_state="present",
            status_reason=None, generation=1, access_revision=1, access_enabled=True,
            access_guarded=True, deleted_at=None, created_at=self.now, updated_at=self.now,
        )
        self.store.namespace = "urumi-store-" + self.store.id.hex
        self.servers = [
            SimpleNamespace(id=i, enabled=True, status="Connected", status_reason=None)
            for i in (1, 2)
        ]
        self.values, self.added, self.committed, self.rolled_back = [], [], 0, 0
        self.events = []
        self.fail_flush = False
        self.saved = [(item, deepcopy(vars(item))) for item in (self.store, *self.servers)]

    async def scalar(self, statement):
        str(statement.compile(dialect=postgresql.dialect()))
        self.events.append("scalar")
        assert self.values, "Unexpected database query"
        return self.values.pop(0)

    async def gate(self, session):
        assert session is self
        self.events.append("gate")

    async def browser(self, session, actor, sid, lock):
        assert session is self and actor is self.actor and sid == "fixture-session" and lock is True
        self.events.append("browser")

    async def registrations(self, session, store, *, both):
        assert session is self and store is self.store
        self.events.append("servers")
        return self.servers

    async def settings(self, session, *, lock):
        assert session is self and lock
        return SimpleNamespace(timeout_seconds=1200)

    def add(self, value): self.added.append(value)

    async def flush(self):
        self.events.append("flush")
        if self.fail_flush: raise SQLAlchemyError("PRIVATE fixture storage")

    async def commit(self):
        self.events.append("commit")
        self.committed += 1

    async def rollback(self):
        self.rolled_back += 1
        for item, values in self.saved:
            vars(item).update(deepcopy(values))
        self.added.clear()

    def patched(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(module, "lock_store_admission", self.gate))
        stack.enter_context(patch.object(module.browser_sessions, "load", self.browser))
        stack.enter_context(patch.object(module, "_servers", self.registrations))
        stack.enter_context(patch.object(module, "get_store_settings", self.settings))
        return stack

    async def access(self, enabled=False, revision=1, actor=None):
        self.values = [actor or self.actor, self.store]
        if self.store.access_enabled != enabled and self.store.access_revision == revision and self.store.access_guarded:
            self.values += [None, self.now]
        request = SimpleNamespace(state=SimpleNamespace(browser_session=SimpleNamespace(id="fixture-session")))
        with self.patched():
            return await module.set_store_access(
                self.store.id, module.StoreAccessBody(generation=1, revision=revision, enabled=enabled),
                request, Response(), self.user, self,
            )

    async def delete(self, previous=None, body=None):
        self.values = [self.actor, previous, self.store]
        if previous is None and self.store.status in ("Ready", "Failed"):
            self.values += [None, self.now]
        request = SimpleNamespace(state=SimpleNamespace(browser_session=SimpleNamespace(id="fixture-session")))
        with self.patched():
            return await module.delete_store(
                self.store.id, body or module.DeleteStoreBody(request_id=uuid4(), generation=1),
                request, Response(), self.user, self,
            )


async def reject(code, operation):
    try: await operation
    except HTTPException as error:
        assert error.status_code == code and "PRIVATE" not in str(error.detail)
        return
    raise AssertionError("Expected status " + str(code))


async def check():
    f = Fixture()
    result = await f.access()
    assert result["store"]["access_enabled"] is False
    assert all(not row.enabled and row.status == "Disabled" for row in f.servers)
    assert f.events.index("gate") < f.events.index("browser") < f.events.index("servers") < f.events.index("commit")
    assert len(f.added) == 1 and isinstance(f.added[0], SecurityEvent)
    f = Fixture()
    f.store.access_enabled = False
    for row in f.servers: row.enabled, row.status = False, "Disabled"
    result = await f.access(True)
    assert result["store"]["access_enabled"] and all(row.status == "Unreachable" for row in f.servers)
    f = Fixture()
    await f.access(True)
    assert not f.added and "servers" not in f.events
    f = Fixture()
    f.actor.role = "member"
    await reject(403, f.access())
    assert f.rolled_back == 1 and "servers" not in f.events
    for field, value in (("access_guarded", False), ("access_revision", 2), ("status", "Deleting")):
        f = Fixture()
        setattr(f.store, field, value)
        await reject(409, f.access())
        assert not f.added and f.committed == 0
    f = Fixture()
    f.fail_flush = True
    await reject(503, f.access())
    assert f.store.access_enabled and all(row.enabled for row in f.servers) and not f.added
    print("Access changes bracket authority and registration checks; no-ops, stale requests and storage failures preserve atomicity.")

    f = Fixture()
    result = await f.delete()
    assert result["store"]["status"] == "Deleting" and result["store"]["generation"] == 2
    assert not result["store"]["access_enabled"] and f.committed == 1
    operations = [item for item in f.added if isinstance(item, StoreOperation)]
    assert len(operations) == 1 and operations[0].action == "delete"
    assert operations[0].store_generation == 2 and operations[0].status == "Pending"
    assert (operations[0].deadline_at - f.now).total_seconds() == 1200
    assert all(not row.enabled for row in f.servers)
    body = module.DeleteStoreBody(request_id=operations[0].request_key, generation=1)
    f.added.clear()
    f.store.status = "Deleted"
    repeated = await f.delete(operations[0], body)
    assert repeated["replayed"] and repeated["store"]["status"] == "Deleted" and not f.added
    f = Fixture()
    f.store.status = "Provisioning"
    await reject(409, f.delete())
    assert not f.added
    f = Fixture()
    f.fail_flush = True
    await reject(503, f.delete())
    assert f.store.generation == 1 and f.store.status == "Ready"
    assert all(row.enabled for row in f.servers) and not f.added
    print("Deletion queues one generation-bound cleanup job, disables registrations and safely replays terminal results.")

    class GateSession:
        def __init__(self, value=None, failure=False): self.value, self.failure = value, failure
        async def scalar(self, statement):
            sql = str(statement.compile(dialect=postgresql.dialect()))
            assert "stores.access_enabled IS true" in sql and "stores.access_guarded IS true" in sql
            if self.failure: raise SQLAlchemyError("PRIVATE storage")
            return self.value
        async def rollback(self): pass
    for session, status in ((GateSession(uuid4()), 204), (GateSession(), 503), (GateSession(failure=True), 503)):
        response = await store_access.store_access_gate(uuid4(), session)
        assert response.status_code == status and response.headers["cache-control"].startswith("no-store")
        assert b"PRIVATE" not in response.body
    print("Public gating uses Ready/present and access flags; unavailable storage returns a generic maintenance page.")
    print("Lifecycle unit checks passed. No files, database or live upstreams were changed.")


if __name__ == "__main__":
    asyncio.run(check())
