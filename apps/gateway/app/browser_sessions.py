"""Server-enforced dashboard sessions. Ordinary requests never extend expiry."""
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select, update

from app.models import BrowserSession
from app.security import COOKIE_SECURE, SESSION_TTL, create_session_token, token_hash

IDLE_SECONDS = 30 * 60
ABSOLUTE_SECONDS = 8 * 60 * 60


def utcnow():
    return datetime.now(timezone.utc)


def aware(value):
    # PostgreSQL returns aware timestamps; also supports the isolated SQLite checks.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def validate(record, user, now):
    if (record is None or record.user_id != user.id or record.revoked_at is not None
        or record.session_version != user.session_version
        or aware(record.idle_expires_at) <= now or aware(record.absolute_expires_at) <= now):
        raise HTTPException(401, "Login session expired or was revoked. Please sign in again.")
    return record


async def load(session, user, session_id, *, lock=False):
    statement = select(BrowserSession).where(BrowserSession.id == session_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return validate(await session.scalar(statement), user, utcnow())


def metadata(record):
    return {"user_id": record.user_id, "server_now": utcnow(), "idle_expires_at": aware(record.idle_expires_at),
        "absolute_expires_at": aware(record.absolute_expires_at), "idle_timeout_seconds": IDLE_SECONDS}


async def issue(session, user, response):
    now = utcnow()
    session_nonce = secrets.token_urlsafe(32)
    record = BrowserSession(id=token_hash(session_nonce), user_id=user.id,
        session_version=user.session_version, created_at=now, last_activity_at=now,
        idle_expires_at=now + timedelta(seconds=IDLE_SECONDS),
        absolute_expires_at=now + timedelta(seconds=ABSOLUTE_SECONDS))
    session.add(record)
    await session.flush()
    response.set_cookie("urm_session", create_session_token(user.id, user.session_version,
        session_id=session_nonce, expires_at=record.absolute_expires_at), max_age=SESSION_TTL,
        httponly=True, secure=COOKIE_SECURE, samesite="strict", path="/")
    response.set_cookie("urm_csrf", secrets.token_urlsafe(32), max_age=SESSION_TTL,
        httponly=False, secure=COOKIE_SECURE, samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    return record


async def revoke(session, session_id):
    await session.execute(update(BrowserSession).where(BrowserSession.id == session_id,
        BrowserSession.revoked_at.is_(None)).values(revoked_at=utcnow()))


async def renew(session, user, session_id):
    # Callers lock/recheck the user first, matching account-disabling lock order.
    record = await load(session, user, session_id, lock=True)
    now = utcnow()
    record.last_activity_at = now
    record.idle_expires_at = min(now + timedelta(seconds=IDLE_SECONDS), aware(record.absolute_expires_at))
    await session.commit()
    return metadata(record)
