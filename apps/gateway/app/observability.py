"""Bounded diagnostic fields; credentials, arguments and exception text stay out."""
import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from time import perf_counter
from uuid import uuid4

request_id = ContextVar('urumi_request_id', default=None)
_HTTP = {'since': datetime.now(timezone.utc).isoformat(), 'requests': 0,
         'errors': 0, 'rejected': 0, 'cancelled': 0, 'total_ms': 0}

def http_snapshot():
    return {**{key: value for key, value in _HTTP.items() if key != 'total_ms'},
            'average_ms': round(_HTTP['total_ms'] / _HTTP['requests'], 1) if _HTTP['requests'] else None}

_FIELDS = {'audit_id', 'operation_id', 'store_id', 'server_id', 'action', 'attempt',
           'outcome', 'duration_ms', 'http_status', 'route', 'method', 'reason_code'}

class SafeJSONFormatter(logging.Formatter):
    def format(self, record):
        result = {'time': datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                  'level': record.levelname, 'event': record.getMessage()[:240]}
        current = request_id.get()
        if current:
            result['request_id'] = current
        for field in _FIELDS:
            value = getattr(record, field, None)
            if isinstance(value, (str, int, float, bool)):
                result[field] = value[:160] if isinstance(value, str) else value
        return json.dumps(result, ensure_ascii=True, allow_nan=False)

def diagnostic_logger(name):
    logger = logging.getLogger(name)
    if not any(getattr(handler, '_urumi_diagnostic', False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler._urumi_diagnostic = True
        handler.setFormatter(SafeJSONFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger

log = diagnostic_logger('urumi.telemetry')

def failure_code(error):
    # Only an allowlisted category is recorded, never str(error) or traceback data.
    name = type(error).__name__
    if isinstance(error, TimeoutError):
        return 'timeout'
    if name in {'StoreJobClaimLost', 'StoreLeaseStopped', 'StoreKubernetesStopped', 'StoreHelmStopped'}:
        return 'ownership_or_allowance_lost'
    if name.endswith('Pending'):
        return 'resources_pending'
    if name.endswith('Unavailable'):
        return 'dependency_unavailable'
    if name in {'KubernetesAPIError', 'HelmProcessError', 'StoreHelmProcessError'}:
        return 'upstream_or_process_failure'
    return 'stage_rejected'

class RequestDiagnostics:
    """ASGI wrapper preserves streaming, disconnects and cancellation."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        identifier = uuid4().hex  # Do not echo caller-controlled IDs into logs.
        token = request_id.set(identifier)
        scope.setdefault('state', {})['request_id'] = identifier
        started, status, outcome = perf_counter(), 500, 'error'

        async def respond(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
                message = {**message, 'headers': [
                    (key, value) for key, value in message.get('headers', [])
                    if key.lower() != b'x-request-id'
                ] + [(b'x-request-id', identifier.encode('ascii'))]}
            await send(message)

        try:
            await self.app(scope, receive, respond)
            outcome = 'success' if status < 400 else 'error'
        except asyncio.CancelledError:
            outcome = 'cancelled'
            raise
        finally:
            route = getattr(scope.get('route'), 'path', 'unmatched')
            # Templates only: no raw path, query, headers, body or uploaded names.
            if not isinstance(route, str) or len(route) > 160:
                route = 'unmatched'
            method = scope.get('method')
            method = method if method in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'} else 'OTHER'
            try:
                elapsed = max(0, int((perf_counter() - started) * 1000))
                if route not in {'/healthz', '/readyz', '/api/admin/monitoring'}:
                    _HTTP['requests'] += 1
                    _HTTP['errors'] += int(status >= 500 and outcome != 'cancelled')
                    _HTTP['rejected'] += int(400 <= status < 500 and outcome != 'cancelled')
                    _HTTP['cancelled'] += int(outcome == 'cancelled')
                    _HTTP['total_ms'] += elapsed
                if route not in {'/healthz', '/readyz'} or status >= 400:
                    log.info('http_request', extra={'route': route, 'method': method,
                        'http_status': status, 'outcome': outcome,
                        'duration_ms': elapsed})
            finally:
                request_id.reset(token)
