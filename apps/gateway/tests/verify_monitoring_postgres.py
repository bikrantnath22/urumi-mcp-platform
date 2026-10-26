"""Optional PostgreSQL check. Creates/drops one isolated schema, never live rows."""
import asyncio
from datetime import timedelta
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema
from app.config import database_url
from app.database import engine
from app.models import AuditLog, Base, MCPServer, Store, StoreOperation, User
from app.monitoring import collect_snapshot

async def check():
    schema = 'urumi_observability_test_' + uuid4().hex
    isolated, created = None, False
    try:
        async with engine.begin() as db:
            await db.execute(CreateSchema(schema))
        created = True
        isolated = create_async_engine(database_url(), hide_parameters=True,
            connect_args={'options': f'-c search_path={schema} -c timezone=UTC'})
        factory = async_sessionmaker(isolated, expire_on_commit=False)
        async with isolated.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        async with factory.begin() as db:
            now = await db.scalar(select(func.clock_timestamp()))
            db.add(User(id=1, email='monitoring@fixture.invalid', password_hash='fixture', role='admin', active=True))
            db.add_all([
                MCPServer(id=1, slug='woo', name='Fixture Woo', url='http://fixture.invalid/mcp',
                    enabled=True, status='Unreachable', status_reason='PRIVATE connection failure', last_checked_at=now),
                MCPServer(id=2, slug='notes', name='Fixture Notes', url='http://fixture.invalid/mcp',
                    enabled=False, status='Connected'),
            ])
            await db.flush()
            for outcome, duration, error, when in (
                ('success', 100, None, now - timedelta(minutes=5)),
                ('success', 200, None, now - timedelta(minutes=4)),
                ('success', 300, None, now - timedelta(minutes=3)),
                ('error', 400, 'PRIVATE failure', now - timedelta(minutes=2)),
                ('denied', 1, 'PRIVATE denial', now - timedelta(minutes=1)),
                ('error', 0, 'Execution started; a final outcome has not been recorded.', now - timedelta(minutes=1)),
                ('success', 10000, None, now - timedelta(days=2)),
            ):
                db.add(AuditLog(user_id=1, server_id=1, server_slug='woo', tool='woo__fixture', source='chat',
                    duration_ms=duration, outcome=outcome, error=error, created_at=when))
            for index, status in enumerate(('Ready', 'Failed', 'Deleting', 'Provisioning')):
                sid = uuid4()
                db.add(Store(id=sid, owner_id=1, name='Fixture', slug='fixture-' + str(index),
                    namespace='urumi-store-' + sid.hex, url='http://fixture-' + str(index) + '.invalid',
                    status=status, desired_state='absent' if status == 'Deleting' else 'present'))
                await db.flush()
                job_status = ('Succeeded', 'Failed', 'Pending', 'Running')[index]
                db.add(StoreOperation(store_id=sid, requested_by_id=1, request_key=uuid4(),
                    request_fingerprint='a' * 64, action='delete' if index == 2 else 'provision',
                    store_generation=1, status=job_status, attempts=1,
                    created_at=now - timedelta(minutes=5), available_at=now - timedelta(minutes=5),
                    deadline_at=now - timedelta(minutes=1) if index == 2 else now + timedelta(minutes=15),
                    started_at=now - timedelta(minutes=4),
                    finished_at=now - timedelta(minutes=2) if index < 2 else None,
                    lease_token=uuid4() if index == 3 else None,
                    lease_expires_at=now - timedelta(minutes=2) if index == 3 else None))
        async def counts():
            async with factory() as db:
                return [await db.scalar(select(func.count()).select_from(model))
                    for model in (User, MCPServer, AuditLog, Store, StoreOperation)]
        before = await counts()
        value = await collect_snapshot(factory, 60)
        assert value['tools']['calls'] == 6 and value['tools']['success'] == 3
        assert value['tools']['denied'] == 1 and value['tools']['uncertain'] == 1
        assert value['tools']['error_rate_percent'] == 25
        assert value['tools']['average_ms'] == 250 and value['tools']['p95_ms'] == 385
        assert sum(point['calls'] for point in value['series']) == 6
        assert value['jobs']['created'] == 1 and value['jobs']['failed_creations'] == 1
        assert value['jobs']['average_creation_seconds'] == 180
        assert value['jobs']['overdue'] == value['jobs']['expired_leases'] == value['jobs']['waiting'] == 1
        assert value['servers'][1]['status'] == 'Disabled'
        assert value['upstreams'] == {'Unreachable': 1}
        assert 'PRIVATE' not in str(value)
        assert before == await counts()
        print('Real PostgreSQL FILTER, percentiles, time buckets, creation duration and job alerts passed.')
        print('Monitoring changed no fixture rows and exposed no credentials or diagnostic text.')
    finally:
        if isolated is not None:
            await isolated.dispose()
        try:
            if created:
                async with engine.begin() as db:
                    await db.execute(DropSchema(schema, cascade=True))
                print('Temporary PostgreSQL schema removed.')
        finally:
            await engine.dispose()

if __name__ == '__main__':
    asyncio.run(asyncio.wait_for(check(), 120))
