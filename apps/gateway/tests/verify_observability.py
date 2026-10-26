"""Offline API, SQL-shape, diagnostics and probe checks; optional isolated PostgreSQL."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from io import StringIO
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from app import auth, monitoring as module, observability as diagnostics
from app.store_worker_health import WorkerHealth, healthy

NOW = datetime.now(timezone.utc)
TOOLS = dict(calls=16, success=9, errors=3, denied=2, uncertain=2, average_ms=175.25, p95_ms=800.5)
JOBS = dict(queued=1, running=1, overdue=1, expired_leases=1, waiting=1,
            failed_creations=1, created=2, average_creation_seconds=120.5)

class Result:
    def __init__(self, value): self.value = value
    def mappings(self): return self
    def one(self): return self.value
    def all(self): return self.value

class FakeDB:
    def __init__(self):
        self.calls, self.open = [], False
        self.results = [TOOLS, [{**TOOLS, 'server_id': 1, 'server_slug': 'woo'}],
            [{'id': 1, 'slug': 'woo', 'name': 'WooCommerce', 'enabled': True, 'status': 'Unreachable',
              'status_reason': 'PRIVATE credential connection failure', 'last_checked_at': NOW},
             {'id': 2, 'slug': 'notes', 'name': 'Notes', 'enabled': False, 'status': 'Connected',
              'status_reason': 'PRIVATE', 'last_checked_at': None}],
            [('Unreachable', 1)], [('Ready', 2)], JOBS, [{**TOOLS, 'bucket': 6}]]
    async def __aenter__(self): return self
    async def __aexit__(self, *args): self.open = False
    @asynccontextmanager
    async def begin(self):
        self.open = True
        try: yield
        finally: self.open = False
    async def scalar(self, statement):
        assert 'clock_timestamp' in str(statement)
        return NOW
    async def execute(self, statement):
        assert self.open
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.calls.append(sql)
        assert 'credential_encrypted' not in sql and 'lease_token' not in sql
        if sql.startswith('SET '): return Result(None)
        assert sql.startswith('SELECT '), sql
        return Result(self.results.pop(0))

async def checks():
    db = FakeDB()
    data = await module.collect_snapshot(lambda: db, 60)
    assert not db.open and not db.results
    assert db.calls[0].endswith('REPEATABLE READ, READ ONLY')
    assert data['tools']['error_rate_percent'] == 25 and data['tools']['average_ms'] == 175.2
    assert len(data['series']) == 12 and sum(point['calls'] for point in data['series']) == 16
    assert data['servers'][1]['status'] == 'Disabled'
    assert 'PRIVATE' not in json.dumps(data, default=str)
    assert {a['code'] for a in data['alerts']} == {'upstream_unreachable', 'overdue_cleanup',
        'expired_leases', 'queue_wait', 'creation_failed', 'uncertain_calls', 'tool_errors'}
    empty = module.tool_summary({key: None for key in TOOLS})
    assert empty['calls'] == 0 and empty['error_rate_percent'] is None and empty['p95_ms'] is None
    assert module.alerts_for(empty, {}, {key: 0 for key in JOBS}) == []
    module._CACHE.clear()
    collector = AsyncMock(return_value=data)
    with patch.object(module, 'collect_snapshot', collector):
        snapshots = await asyncio.gather(*(module.snapshot(60) for _ in range(8)))
        assert collector.await_count == 1
        snapshots[0]['tools']['calls'] = 900
        assert (await module.snapshot(60))['tools']['calls'] == 16
    module._CACHE.clear()
    app = FastAPI()
    app.include_router(module.router)
    app.add_middleware(diagnostics.RequestDiagnostics)
    async def who(): raise HTTPException(401, 'Please log in.')
    app.dependency_overrides[auth.current_user] = who
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
        assert (await client.get('/api/admin/monitoring')).status_code == 401
        async def member(): return SimpleNamespace(role='member')
        app.dependency_overrides[auth.current_user] = member
        assert (await client.get('/api/admin/monitoring')).status_code == 403
        async def admin(): return SimpleNamespace(role='admin')
        app.dependency_overrides[auth.current_user] = admin
        with patch.object(module, 'collect_snapshot', collector):
            for minutes in (60, 360, 1440):
                response = await client.get('/api/admin/monitoring', params={'window_minutes': minutes})
                assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
                assert len(response.headers['x-request-id']) == 32
            assert (await client.get('/api/admin/monitoring?window_minutes=2')).status_code == 422
            assert (await client.get('/api/admin/monitoring?window_minutes=bad')).status_code == 422
            assert (await client.post('/api/admin/monitoring')).status_code == 405
        module._CACHE.clear()
        failure = OperationalError('PRIVATE SQL', {}, RuntimeError('PRIVATE password'))
        with patch.object(module, 'collect_snapshot', AsyncMock(side_effect=failure)):
            response = await client.get('/api/admin/monitoring')
            assert response.status_code == 503 and 'PRIVATE' not in response.text
    print('Admin-only monitoring, read-only bounded PostgreSQL queries, durable counts and safe failures passed')
    print('Confirmed errors, denied calls and uncertain outcomes remain separate; concurrent requests share a bounded cache')

    output = StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(diagnostics.SafeJSONFormatter())
    logger = diagnostics.log
    original = logger.handlers[:]
    logger.handlers[:] = [handler]
    try:
        ids, seen = [], []
        async def inner(scope, receive, send):
            ids.append(diagnostics.request_id.get())
            await asyncio.sleep(0)
            assert scope['state']['request_id'] == diagnostics.request_id.get()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'chunk', 'more_body': True})
            await send({'type': 'http.response.body', 'body': b'end'})
        wrapper = diagnostics.RequestDiagnostics(inner)
        async def request():
            messages = []
            async def send(message): messages.append(message)
            await wrapper({'type': 'http', 'method': 'POST', 'path': '/PRIVATE',
                'query_string': b'password=PRIVATE', 'headers': [(b'authorization', b'PRIVATE')],
                'route': SimpleNamespace(path='/api/fixture/{id}')}, AsyncMock(), send)
            seen.append(messages)
        await asyncio.gather(request(), request())
        assert len(set(ids)) == 2 and diagnostics.request_id.get() is None
        assert all(messages[1]['body'] == b'chunk' and messages[2]['body'] == b'end' for messages in seen)
        assert all(any(key == b'x-request-id' for key, _ in messages[0]['headers']) for messages in seen)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        assert {row['request_id'] for row in rows} == set(ids)
        assert 'PRIVATE' not in output.getvalue()
        async def cancelled(*args): raise asyncio.CancelledError()
        try: await diagnostics.RequestDiagnostics(cancelled)({'type': 'http'}, AsyncMock(), AsyncMock())
        except asyncio.CancelledError: pass
        else: raise AssertionError('Cancellation was swallowed')
        assert diagnostics.request_id.get() is None
        assert json.loads(output.getvalue().splitlines()[-1])['outcome'] == 'cancelled'
    finally:
        logger.handlers[:] = original
    assert diagnostics.failure_code(TimeoutError('PRIVATE')) == 'timeout'
    assert diagnostics.failure_code(RuntimeError('PRIVATE')) == 'stage_rejected'
    print('Streaming and concurrent request IDs stay isolated; cancellation propagates and logs omit request secrets')
    with tempfile.TemporaryDirectory(prefix='urumi-health-') as directory:
        path = Path(directory) / 'worker.json'
        health = WorkerHealth(path)
        assert not healthy(path)
        health.beat()
        assert healthy(path) and not healthy(path, True)
        health.beat(queue_ok=True)
        assert healthy(path, True)
        with patch('app.store_worker_health.time.monotonic', return_value=json.loads(path.read_text())['beat'] + 91):
            assert not healthy(path) and not healthy(path, True)
        health.beat(queue_ok=False)
        assert healthy(path) and not healthy(path, True)
        path.write_text('{"beat":NaN}', encoding='utf-8')
        assert not healthy(path)
        health.clear()
        assert not path.exists()
    print('Worker probes require a fresh scheduler heartbeat; failed queue checks affect readiness, not liveness')

if __name__ == '__main__':
    asyncio.run(asyncio.wait_for(checks(), 30))
    print('Observability offline verification passed. No live services were called.')
