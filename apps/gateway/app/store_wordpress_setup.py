"""Owner-authorized password setup. No WordPress password enters the gateway."""
import hashlib
import hmac
import json
import re
import secrets
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import browser_sessions
from app.auth import current_user
from app.database import get_session
from app.models import SecurityEvent, StoreWordPressSetup, User
from app.store_lifecycle import _actor, _store
from app.store_namespace import _opaque
from app.store_requests import store_address
from app.store_settings import lock_store_admission

router = APIRouter()
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class SetupBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    generation: int = Field(ge=1)


def _hash(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


async def _owned_store(session, actor, store_id):
    # Lifecycle admins can manage stores; WordPress setup requires ownership.
    store = await _store(session, actor, store_id)
    if store.owner_id != actor.id:
        raise HTTPException(404, "Store not found.")
    return store


def _ready(store):
    if (store is None or store.status != "Ready" or store.desired_state != "present"
            or store.deleted_at is not None or not store.access_enabled
            or not store.access_guarded or not _opaque(store.namespace_uid)
            or not _opaque(store.credentials_secret_uid)
            or store.namespace != "urumi-store-" + store.id.hex
            or store.release_name != "store" or store.url != store_address(store.slug)):
        raise HTTPException(409, "An enabled Ready store is required for WordPress setup.")


@router.post("/api/stores/{store_id}/wordpress-setup")
async def issue_setup(
    store_id: UUID, body: SetupBody, request: Request, response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    response.headers.update(_HEADERS)
    try:
        actor = await _actor(session, request, user)
        store = await _owned_store(session, actor, store_id)
        _ready(store)
        if store.generation != body.generation:
            raise HTTPException(409, "Store information changed. Refresh before setup.")
        now = await session.scalar(select(func.clock_timestamp()))
        row = await session.get(StoreWordPressSetup, store.id, with_for_update=True)
        if row is None:
            row = StoreWordPressSetup(store_id=store.id)
            session.add(row)
        token = secrets.token_urlsafe(32)
        row.token_hash = _hash(token)
        row.actor_id, row.owner_id = actor.id, store.owner_id
        row.session_id = request.state.browser_session.id
        row.generation, row.access_revision = store.generation, store.access_revision
        row.namespace_uid, row.used_at = store.namespace_uid, None
        row.expires_at = now + timedelta(minutes=5)
        session.add(SecurityEvent(
            actor_id=actor.id, target_user_id=store.owner_id,
            action="store.wordpress_setup_issued", details={"store_id": str(store.id)},
        ))
        result = {"store_id": str(store.id), "token": token,
            "url": store.url + "/wp-login.php?action=urumi_setup",
            "expires_at": row.expires_at}
        await session.commit()
        return result
    except HTTPException:
        await session.rollback()
        raise
    except SQLAlchemyError:
        await session.rollback()
        raise HTTPException(503, "WordPress setup is temporarily unavailable.") from None


async def _redemption(request):
    # Parse explicitly so validation errors never echo a submitted capability.
    try:
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise ValueError()
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 1024:
                raise ValueError()
        data = json.loads(raw)
        if (type(data) is not dict or set(data) != {"store_id", "token"}
                or type(data["store_id"]) is not str or type(data["token"]) is not str or not _TOKEN.fullmatch(data["token"])):
            raise ValueError()
        return UUID(data["store_id"]), data["token"]
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise HTTPException(403, "WordPress setup is invalid or expired.") from None


@router.post("/api/store-wordpress-setup/redeem", include_in_schema=False)
async def redeem_setup(request: Request, session: AsyncSession = Depends(get_session)):
    try:
        store_id, token = await _redemption(request)
        row = await session.get(StoreWordPressSetup, store_id)
        if row is None or not hmac.compare_digest(row.token_hash, _hash(token)):
            raise HTTPException(403, "WordPress setup is invalid or expired.")
        actor_id, session_id = row.actor_id, row.session_id
        # Match lifecycle lock order; no network call occurs in this transaction.
        await lock_store_admission(session)
        actor = await session.scalar(select(User).where(User.id == actor_id)
            .with_for_update().execution_options(populate_existing=True))
        if actor is None or not actor.active or not actor.password_hash:
            raise HTTPException(403, "WordPress setup is invalid or expired.")
        browser = await browser_sessions.load(session, actor, session_id, lock=True)
        store = await _owned_store(session, actor, store_id)
        _ready(store)
        row = await session.scalar(select(StoreWordPressSetup)
            .where(StoreWordPressSetup.store_id == store_id).with_for_update()
            .execution_options(populate_existing=True))
        now = await session.scalar(select(func.clock_timestamp()))
        if (row is None or not hmac.compare_digest(row.token_hash, _hash(token))
                or row.actor_id != actor_id or row.session_id != session_id
                or row.used_at is not None or row.expires_at <= now
                or row.owner_id != store.owner_id or row.generation != store.generation
                or row.access_revision != store.access_revision
                or row.namespace_uid != store.namespace_uid):
            raise HTTPException(403, "WordPress setup is invalid or expired.")
        browser_sessions.validate(browser, actor, now)
        row.used_at = now
        session.add(SecurityEvent(
            actor_id=actor.id, target_user_id=store.owner_id,
            action="store.wordpress_setup_used", details={"store_id": str(store.id)},
        ))
        await session.flush()
        final_now = await session.scalar(select(func.clock_timestamp()))
        browser_sessions.validate(browser, actor, final_now)
        if row.expires_at <= final_now:
            raise HTTPException(403, "WordPress setup is invalid or expired.")
        await session.commit()
        return Response(status_code=204, headers=_HEADERS)
    except HTTPException:
        await session.rollback()
        # Keep account, store and session state private on the redemption endpoint.
        return Response(status_code=403, headers=_HEADERS)
    except SQLAlchemyError:
        await session.rollback()
        return Response(status_code=503, headers=_HEADERS)
