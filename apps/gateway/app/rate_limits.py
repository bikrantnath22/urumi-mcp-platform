"""Database-backed quotas shared by gateway processes and all user API keys."""
import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta, timezone
import hashlib
import hmac
import ipaddress
import logging
import math
import os

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from app.database import Session
from app.models import RateLimitBucket, UsageLimitSettings
from app.security import JWT_SECRET

logger = logging.getLogger(__name__)
DEFAULTS = {
    "login_account_limit": 5, "login_ip_limit": 30, "login_window_seconds": 600,
    "chat_per_minute": 10, "chat_per_hour": 100,
    "chat_global_per_minute": 30, "tool_per_minute": 60,
}
TRUSTED_PROXY_NETWORKS = tuple(ipaddress.ip_network(value.strip())
    for value in os.getenv("TRUSTED_PROXY_CIDRS", "").split(",") if value.strip())


class RateLimitExceeded(Exception):
    def __init__(self, label, retry_after):
        self.retry_after = max(1, math.ceil(retry_after))
        super().__init__(f"{label} limit reached. Wait before trying again.")

    def payload(self):
        return {"detail": str(self), "rate_limited": True,
            "request_not_started": True, "retry_after_s": self.retry_after,
            "error": {"message": str(self), "retry_after_s": self.retry_after,
                "automatic_retry_allowed": False}}


class LimitsUnavailable(Exception):
    pass


async def limit_response(request, exc):
    return JSONResponse(exc.payload(), status_code=429,
        headers={"Retry-After": str(exc.retry_after), "Cache-Control": "no-store"})


async def unavailable_response(request, exc):
    return JSONResponse({"detail": "Usage-limit storage is unavailable. Try again shortly.",
        "request_not_started": True}, status_code=503,
        headers={"Retry-After": "5", "Cache-Control": "no-store"})


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _insert(session, model):
    if session.bind.dialect.name == "postgresql":
        return pg_insert(model)
    if session.bind.dialect.name == "sqlite":  # Portable verification only.
        return sqlite_insert(model)
    raise RuntimeError("Usage limits require PostgreSQL.")


async def database_now(session):
    function = func.clock_timestamp() if session.bind.dialect.name == "postgresql" else func.current_timestamp()
    return aware(await session.scalar(select(function)))


async def settings(session):
    record = await session.get(UsageLimitSettings, 1, populate_existing=True)
    if record is None:
        await session.execute(_insert(session, UsageLimitSettings).values(id=1, **DEFAULTS)
            .on_conflict_do_nothing(index_elements=["id"]))
        record = await session.get(UsageLimitSettings, 1, populate_existing=True)
    return record


def public_settings(record):
    return {**{name: getattr(record, name) for name in DEFAULTS},
        "revision": record.revision, "updated_at": record.updated_at,
        "updated_by_id": record.updated_by_id}


def client_ip(request: Request):
    """Trust forwarding headers only from configured proxies.

    Run Uvicorn with --no-proxy-headers to retain the original TCP peer.
    """
    def address(value):
        result = ipaddress.ip_address(value.strip())
        return result.ipv4_mapped if isinstance(result, ipaddress.IPv6Address) and result.ipv4_mapped else result
    def trusted(value):
        return any(value in network for network in TRUSTED_PROXY_NETWORKS)
    try:
        peer = address(request.client.host)
    except (AttributeError, ValueError):
        return "unknown-peer"
    if not trusted(peer):
        return str(peer)
    raw = request.headers.get("x-forwarded-for", "")
    if not raw or len(raw) > 2048:
        return str(peer)
    try:
        chain = [address(value) for value in raw.split(",")]
        if len(chain) > 32:
            return str(peer)
    except ValueError:
        return str(peer)
    current = peer
    for forwarded in reversed(chain):
        if not trusted(current):
            break
        current = forwarded
    return str(current)


@dataclass(frozen=True)
class Rule:
    scope: str
    subject: str
    maximum: int
    seconds: int
    label: str

    @property
    def key(self):
        # Duration changes start a fresh allowance. Maximum changes retain it.
        value = f"{self.scope}\0{self.seconds}\0{self.subject}".encode()
        return hmac.new(JWT_SECRET.encode(), value, hashlib.sha256).hexdigest()


async def consume(session, rules):
    now = await database_now(session)
    for rule in sorted(rules, key=lambda item: item.key):
        table = RateLimitBucket
        expired = table.reset_at <= now
        statement = _insert(session, table).values(id=rule.key, scope=rule.scope,
            used=1, reset_at=now + timedelta(seconds=rule.seconds))
        statement = statement.on_conflict_do_update(index_elements=["id"],
            set_={"used": case((expired, 1), else_=table.used + 1),
                "reset_at": case((expired, statement.excluded.reset_at), else_=table.reset_at)},
            where=or_(expired, table.used < rule.maximum)).returning(table.id)
        accepted = (await session.execute(statement)).scalar_one_or_none()
        if accepted is None:
            reset_at = await session.scalar(select(table.reset_at).where(table.id == rule.key))
            raise RateLimitExceeded(rule.label, (aware(reset_at) - now).total_seconds())


async def enforce_login(request, email):
    try:
        async with Session() as session:
            limits = public_settings(await settings(session))
            # Count IP attempts even when the account allowance is exhausted.
            await consume(session, [Rule("login-ip", client_ip(request),
                limits["login_ip_limit"], limits["login_window_seconds"], "Login")])
            await session.commit()
        async with Session() as session:
            await consume(session, [Rule("login-account", email,
                limits["login_account_limit"], limits["login_window_seconds"], "Login")])
            await session.commit()
    except SQLAlchemyError:
        raise LimitsUnavailable() from None


async def enforce_chat(session, user_id):
    try:
        limits = await settings(session)
        await consume(session, [
            Rule("chat-minute", str(user_id), limits.chat_per_minute, 60, "Chat"),
            Rule("chat-hour", str(user_id), limits.chat_per_hour, 3600, "Hourly chat"),
            Rule("chat-global", "all-users", limits.chat_global_per_minute, 60, "Shared chat"),
        ])
    except SQLAlchemyError:
        raise LimitsUnavailable() from None


async def enforce_tool(user_id):
    try:
        async with Session() as session:
            limits = await settings(session)
            await consume(session, [Rule("tool-minute", str(user_id),
                limits.tool_per_minute, 60, "Tool call")])
            await session.commit()
    except SQLAlchemyError:
        raise LimitsUnavailable() from None


async def cleanup_loop():
    while True:
        try:
            async with Session() as session:
                cutoff = await database_now(session) - timedelta(days=1)
                expired = select(RateLimitBucket.id).where(RateLimitBucket.reset_at < cutoff)
                expired = expired.order_by(RateLimitBucket.reset_at).limit(1000).with_for_update(skip_locked=True)
                await session.execute(delete(RateLimitBucket).where(RateLimitBucket.id.in_(expired)))
                await session.commit()
        except SQLAlchemyError:
            logger.warning("Usage-limit cleanup could not finish")
        await asyncio.sleep(300)


async def stop_cleanup(task):
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
