"""Admin monitoring from durable audit/job rows. No upstream probes or writes."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from time import monotonic

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import DateTime, and_, bindparam, func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.auth import require_admin
from app.database import Session
from app.models import AuditLog, MCPServer, Store, StoreOperation
from app.observability import log, http_snapshot

router = APIRouter(prefix='/api/admin/monitoring', tags=['monitoring'])
_CACHE = {}
_LOCK = asyncio.Lock()
_TTL = 20
_INCOMPLETE = ('Execution started; a final outcome has not been recorded.',
               'Execution ended before a final outcome was recorded.')

def tool_columns():
    uncertain = AuditLog.error.in_(_INCOMPLETE)
    settled = or_(AuditLog.error.is_(None), ~uncertain)
    measured = and_(settled, AuditLog.outcome != 'denied')
    return (
        func.count().label('calls'),
        func.count().filter(and_(settled, AuditLog.outcome == 'success')).label('success'),
        func.count().filter(and_(settled, AuditLog.outcome == 'error')).label('errors'),
        func.count().filter(AuditLog.outcome == 'denied').label('denied'),
        func.count().filter(uncertain).label('uncertain'),
        func.avg(AuditLog.duration_ms).filter(measured).label('average_ms'),
        func.percentile_cont(0.95).within_group(AuditLog.duration_ms).filter(measured).label('p95_ms'),
    )

def tool_summary(row):
    value = {name: int(row[name] or 0) for name in ('calls', 'success', 'errors', 'denied', 'uncertain')}
    value.update({name: round(float(row[name]), 1) if row[name] is not None else None
                  for name in ('average_ms', 'p95_ms')})
    measured = value['success'] + value['errors']
    value['error_rate_percent'] = round(value['errors'] * 100 / measured, 1) if measured else None
    return value

def status_note(status, reason):
    if status == 'Disabled':
        return 'Disabled by an administrator.'
    if status == 'Connected':
        return 'Last discovery or call succeeded.'
    reason = (reason or '').lower()
    if 'exceeded' in reason or 'timeout' in reason:
        return 'The upstream request timed out.'
    if any(word in reason for word in ('policy', 'blocked', 'destination', 'address', 'allowlist')):
        return 'The destination failed a network or ownership policy check.'
    return 'Discovery or a tool connection failed. Review the MCP servers page.'

def alerts_for(tools, upstreams, jobs):
    alerts = []
    def add(code, count, severity, message):
        if count:
            alerts.append({'code': code, 'count': count, 'severity': severity, 'message': message})
    add('upstream_unreachable', upstreams.get('Unreachable', 0), 'warning', 'Enabled upstreams were last observed unreachable.')
    add('overdue_cleanup', jobs['overdue'], 'warning', 'Store jobs passed their original deadline and still require cleanup.')
    add('expired_leases', jobs['expired_leases'], 'critical', 'Running jobs have worker leases expired by more than 60 seconds.')
    add('queue_wait', jobs['waiting'], 'warning', 'Available store jobs have waited more than two minutes.')
    add('creation_failed', jobs['failed_creations'], 'warning', 'Store creation failures occurred in this time window.')
    add('uncertain_calls', tools['uncertain'], 'warning', 'Some calls have no confirmed final outcome. Inspect audit history before retrying.')
    if tools['success'] + tools['errors'] >= 10 and (tools['error_rate_percent'] or 0) >= 20:
        add('tool_errors', tools['errors'], 'warning', 'At least 20% of confirmed tool calls failed (minimum ten calls).')
    return alerts

async def collect_snapshot(factory, minutes):
    async with factory() as db:
        async with db.begin():
            await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            await db.execute(text("SET LOCAL statement_timeout = '2000ms'"))
            now = await db.scalar(select(func.clock_timestamp()))
            start = now - timedelta(minutes=minutes)
            in_window = and_(AuditLog.created_at >= start, AuditLog.created_at < now)
            totals = tool_summary((await db.execute(select(*tool_columns()).where(in_window))).mappings().one())
            groups = (await db.execute(select(AuditLog.server_id, AuditLog.server_slug, *tool_columns())
                .where(in_window).group_by(AuditLog.server_id, AuditLog.server_slug)
                .order_by(func.count().desc()).limit(201))).mappings().all()
            servers = (await db.execute(select(MCPServer.id, MCPServer.slug, MCPServer.name,
                MCPServer.enabled, MCPServer.status, MCPServer.status_reason, MCPServer.last_checked_at)
                .order_by(MCPServer.id).limit(201))).mappings().all()
            upstreams = dict((await db.execute(select(MCPServer.status, func.count())
                .where(MCPServer.enabled.is_(True)).group_by(MCPServer.status))).all())
            inventory = dict((await db.execute(select(Store.status, func.count()).group_by(Store.status))).all())
            active = StoreOperation.status.in_(('Pending', 'Running'))
            creation = and_(StoreOperation.action == 'provision', StoreOperation.finished_at >= start,
                            StoreOperation.finished_at < now)
            jobs = dict((await db.execute(select(
                func.count().filter(StoreOperation.status == 'Pending').label('queued'),
                func.count().filter(StoreOperation.status == 'Running').label('running'),
                func.count().filter(and_(active, StoreOperation.deadline_at < now)).label('overdue'),
                func.count().filter(and_(StoreOperation.status == 'Running',
                    StoreOperation.lease_expires_at < now - timedelta(seconds=60))).label('expired_leases'),
                func.count().filter(and_(StoreOperation.status == 'Pending',
                    StoreOperation.available_at < now - timedelta(minutes=2))).label('waiting'),
                func.count().filter(and_(creation, StoreOperation.status == 'Failed')).label('failed_creations'),
                func.count().filter(and_(creation, StoreOperation.status == 'Succeeded')).label('created'),
                func.avg(func.extract('epoch', StoreOperation.finished_at - StoreOperation.created_at))
                    .filter(and_(creation, StoreOperation.status == 'Succeeded')).label('average_creation_seconds'),
            ))).mappings().one())
            jobs['average_creation_seconds'] = (round(float(jobs['average_creation_seconds']), 1)
                if jobs['average_creation_seconds'] is not None else None)
            seconds = minutes * 60 / 12
            bucket = func.floor(func.extract('epoch', AuditLog.created_at - bindparam(
                'window_start', start, type_=DateTime(timezone=True))) / seconds)
            points = (await db.execute(select(bucket.label('bucket'), *tool_columns()).where(in_window)
                .group_by(bucket).order_by(bucket))).mappings().all()
    # No transaction remains open while building/serializing the response.
    by_server = {(row['server_id'], row['server_slug']): tool_summary(row) for row in groups[:200]}
    empty = tool_summary({name: None for name in ('calls', 'success', 'errors', 'denied', 'uncertain', 'average_ms', 'p95_ms')})
    public_servers = []
    for row in servers[:200]:
        status = row['status'] if row['enabled'] else 'Disabled'
        public_servers.append({'id': row['id'], 'slug': row['slug'], 'name': row['name'], 'status': status,
            'last_checked_at': row['last_checked_at'], 'status_note': status_note(status, row['status_reason']),
            'metrics': by_server.get((row['id'], row['slug']), empty)})
    values = {int(row['bucket']): tool_summary(row) for row in points}
    return {'generated_at': now, 'window_minutes': minutes, 'cache_seconds': _TTL,
        'tools': totals, 'servers': public_servers, 'upstreams': upstreams, 'stores': inventory, 'jobs': jobs,
        'http_process': http_snapshot(),
        'series': [{'start': start + timedelta(seconds=index * seconds),
                    'end': start + timedelta(seconds=(index + 1) * seconds),
                    **values.get(index, empty)} for index in range(12)],
        'truncated': len(servers) > 200 or len(groups) > 200,
        'alerts': alerts_for(totals, upstreams, jobs)}

async def snapshot(minutes):
    try:
        async with asyncio.timeout(8):
            async with _LOCK:
                cached = _CACHE.get(minutes)
                if cached and monotonic() - cached[0] < _TTL:
                    return deepcopy(cached[1])
                result = await collect_snapshot(Session, minutes)
                _CACHE[minutes] = (monotonic(), result)
                return deepcopy(result)
    except (SQLAlchemyError, TimeoutError):
        log.warning('monitoring_unavailable', extra={'reason_code': 'metrics_storage_unavailable'})
        raise HTTPException(503, 'Monitoring data is temporarily unavailable.') from None

@router.get('')
async def read_monitoring(response: Response, window_minutes: int = Query(default=60), admin=Depends(require_admin)):
    if window_minutes not in (60, 360, 1440):
        raise HTTPException(422, 'Choose a 60, 360 or 1440 minute window.')
    response.headers['Cache-Control'] = 'no-store'
    return await snapshot(window_minutes)
