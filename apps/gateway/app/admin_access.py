from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import public_key, require_admin
from app.database import get_session
from app.models import APIKey, BrowserSession, SecurityEvent, User
from app.security import new_api_key, token_hash
from app.users import public_account

router = APIRouter(prefix="/api/admin", tags=["admin account access"])


class ActiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    active: bool


class IssueKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Key name must not be blank.")
        return value


async def lock_admin(session, admin):
    # Serialize account mutations across workers, preventing cross-admin races.
    await session.execute(text("SELECT pg_advisory_xact_lock(714202602)"))
    current = await session.scalar(select(User).where(User.id == admin.id)
        .with_for_update().execution_options(populate_existing=True))
    if (current is None or not current.active or not current.password_hash
        or current.role != "admin" or current.session_version != admin.session_version):
        raise HTTPException(401, "Admin session is unavailable. Please log in again.")
    return current


async def account(session, user_id, *, member=False, lock=False):
    statement = select(User).where(User.id == user_id)
    if lock:
        statement = statement.with_for_update()
    user = await session.scalar(statement.execution_options(populate_existing=True))
    if user is None or (member and user.role != "member"):
        raise HTTPException(404, "Member not found." if member else "Account not found.")
    return user


def record_event(session, actor, target, action, details):
    session.add(SecurityEvent(actor_id=actor.id, target_user_id=target.id,
        action=action, details=details))


@router.patch("/users/{user_id}/active")
async def set_active(user_id: int, body: ActiveBody,
    admin: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    session.expunge(admin)
    actor = await lock_admin(session, admin)
    target = await account(session, user_id, lock=True)
    if target.active == body.active:
        return {"user": public_account(target), "revoked_keys": 0, "changed": False}
    if not body.active:
        if target.role == "admin":
            remaining = await session.scalar(select(func.count()).select_from(User).where(
                User.role == "admin", User.active.is_(True), User.password_hash.is_not(None)))
            if remaining <= 1:
                raise HTTPException(409, "The last active admin cannot be disabled.")
        if target.id == actor.id:
            raise HTTPException(409, "You cannot disable your own account.")
        revoked = await session.execute(update(APIKey).where(
            APIKey.user_id == target.id, APIKey.revoked_at.is_(None)
        ).values(revoked_at=datetime.now(timezone.utc)))
        await session.execute(update(BrowserSession).where(BrowserSession.user_id == target.id,
            BrowserSession.revoked_at.is_(None)).values(revoked_at=datetime.now(timezone.utc)))
        count = revoked.rowcount
        target.invite_token_hash = None
        target.invite_expires_at = None
    else:
        count = 0
    target.active = body.active
    target.session_version += 1
    record_event(session, actor, target,
        "account.enabled" if body.active else "account.disabled", {"revoked_keys": count})
    await session.commit()
    return {"user": public_account(target), "revoked_keys": count, "changed": True}


@router.get("/users/{user_id}/keys")
async def member_keys(user_id: int, admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session), limit: int = Query(50, ge=1, le=100),
    before_id: int | None = Query(None, ge=1)):
    target = await account(session, user_id, member=True)
    statement = select(APIKey).where(APIKey.user_id == target.id)
    if before_id is not None:
        statement = statement.where(APIKey.id < before_id)
    keys = list(await session.scalars(statement.order_by(APIKey.id.desc()).limit(limit + 1)))
    return {"user": public_account(target), "items": [public_key(key) for key in keys[:limit]],
        "next_before_id": keys[limit-1].id if len(keys) > limit else None}


@router.post("/users/{user_id}/keys", status_code=201)
async def issue_key(user_id: int, body: IssueKeyBody, response: Response,
    admin: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    session.expunge(admin)
    actor = await lock_admin(session, admin)
    target = await account(session, user_id, member=True, lock=True)
    if not target.active or not target.password_hash:
        raise HTTPException(409, "Enable the member and complete registration before issuing a key.")
    raw_key = new_api_key()
    key = APIKey(user_id=target.id, name=body.name, prefix=raw_key[:8], key_hash=token_hash(raw_key))
    session.add(key)
    await session.flush()
    record_event(session, actor, target, "api_key.issued",
        {"key_id": key.id, "name": key.name, "prefix": key.prefix})
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return {**public_key(key), "user_id": target.id, "key": raw_key}


@router.delete("/users/{user_id}/keys/{key_id}", status_code=204)
async def revoke_member_key(user_id: int, key_id: int,
    admin: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    session.expunge(admin)
    actor = await lock_admin(session, admin)
    target = await account(session, user_id, member=True, lock=True)
    key = await session.scalar(select(APIKey).where(APIKey.id == key_id,
        APIKey.user_id == target.id).with_for_update())
    if key is None:
        raise HTTPException(404, "API key not found for this member.")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(timezone.utc)
        record_event(session, actor, target, "api_key.revoked",
            {"key_id": key.id, "name": key.name, "prefix": key.prefix})
        await session.commit()
    return Response(status_code=204)


@router.get("/security-events")
async def security_events(admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session), limit: int = Query(20, ge=1, le=100),
    before_id: int | None = Query(None, ge=1)):
    statement = select(SecurityEvent)
    if before_id is not None:
        statement = statement.where(SecurityEvent.id < before_id)
    events = list(await session.scalars(statement.order_by(SecurityEvent.id.desc()).limit(limit + 1)))
    ids = {value for event in events for value in (event.actor_id, event.target_user_id) if value is not None}
    emails = dict((await session.execute(select(User.id, User.email).where(User.id.in_(ids)))).all())
    return {"items": [{"id": event.id, "created_at": event.created_at,
        "actor_email": emails.get(event.actor_id), "target_email": emails.get(event.target_user_id),
        "action": event.action, "details": event.details} for event in events[:limit]],
        "next_before_id": events[limit-1].id if len(events) > limit else None}
