import hmac
from datetime import datetime, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.rate_limits import enforce_login
from app.database import Session, get_session
from app import browser_sessions
from app.models import APIKey, SecurityEvent, User
from app.security import (
    APP_ORIGIN,
    COOKIE_SECURE,
    new_api_key,
    read_session_token,
    token_hash,
    verify_password,
)


router = APIRouter(prefix="/api")
SESSION_COOKIE = "urm_session"
CSRF_COOKIE = "urm_csrf"


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class KeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)


def public_user(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
    }


def public_key(key: APIKey) -> dict:
    return {
        "id": key.id,
        "name": key.name,
        "prefix": key.prefix,
        "created_at": key.created_at,
        "revoked_at": key.revoked_at,
        "last_used_at": key.last_used_at,
    }


def check_origin(request: Request):
    origin = request.headers.get("origin")
    if origin and origin != APP_ORIGIN:
        raise HTTPException(403, "Request origin is not allowed.")


async def current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "Please log in.")

    try:
        user_id, version, nonce = read_session_token(token, include_session=True)
    except (jwt.PyJWTError, ValueError, TypeError):
        raise HTTPException(401, "Login session is invalid or expired.")

    user = await session.get(User, user_id)
    if not user or not user.active or not user.password_hash:
        raise HTTPException(401, "Account is unavailable.")

    if user.session_version != version:
        raise HTTPException(401, "Login session was revoked. Please log in again.")

    record = await browser_sessions.load(session, user, token_hash(nonce))
    request.state.browser_session = record

    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        cookie = request.cookies.get(CSRF_COOKIE, "")
        header = request.headers.get("x-csrf-token", "")

        if (
            not cookie
            or not header
            or not hmac.compare_digest(cookie.encode(), header.encode())
        ):
            raise HTTPException(403, "CSRF check failed.")

    return user


async def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admin access is required.")
    return user


async def authenticate_api_key(raw_key: str) -> User:
    if not raw_key.startswith("urm_") or len(raw_key) > 200:
        raise HTTPException(401, "Invalid or revoked API key.")

    async with Session() as session:
        result = await session.execute(
            select(APIKey, User)
            .join(User, APIKey.user_id == User.id)
            .where(APIKey.key_hash == token_hash(raw_key))
        )
        row = result.first()

        if row is None:
            raise HTTPException(401, "Invalid or revoked API key.")

        key, user = row

        if key.revoked_at or not user.active or not user.password_hash:
            raise HTTPException(401, "Invalid or revoked API key.")

        key.last_used_at = datetime.now(timezone.utc)
        await session.commit()
        return user


@router.post("/auth/login")
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    check_origin(request)
    email = body.email.strip().lower()
    await enforce_login(request, email)

    user = await session.scalar(
        select(User).where(User.email == email)
    )
    valid = await verify_password(
        body.password,
        user.password_hash if user else None,
    )

    if not valid or not user or not user.active:
        raise HTTPException(401, "Invalid email or password.")

    snapshot_id, snapshot_hash, snapshot_version = user.id, user.password_hash, user.session_version
    user = await session.scalar(select(User).where(User.id == snapshot_id)
        .with_for_update().execution_options(populate_existing=True))
    if (not user.active or user.password_hash != snapshot_hash or user.session_version != snapshot_version):
        raise HTTPException(401, "Account changed. Please sign in again.")
    previous = request.cookies.get(SESSION_COOKIE)
    if previous:
        try:
            _, _, old_nonce = read_session_token(previous, include_session=True)
            await browser_sessions.revoke(session, token_hash(old_nonce))
        except (jwt.PyJWTError, ValueError, TypeError):
            pass
    await browser_sessions.issue(session, user, response)
    await session.commit()

    return public_user(user)


@router.get("/auth/me")
async def me(user: User = Depends(current_user)):
    return public_user(user)


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session)):
    await browser_sessions.revoke(session, request.state.browser_session.id)
    await session.commit()
    response = Response(status_code=204)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=COOKIE_SECURE,
        samesite="strict",
    )
    response.delete_cookie(
        CSRF_COOKIE,
        path="/",
        secure=COOKIE_SECURE,
        samesite="strict",
    )
    return response


@router.post("/keys", status_code=201)
async def create_key(
    body: KeyBody,
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Key name must not be blank.")

    snapshot_version = user.session_version
    # Serialize issuance with account disabling.
    user = await session.scalar(select(User).where(User.id == user.id)
        .with_for_update().execution_options(populate_existing=True))
    if not user.active or not user.password_hash or user.session_version != snapshot_version:
        raise HTTPException(401, "Account or login session is unavailable.")
    raw_key = new_api_key()
    key = APIKey(
        user_id=user.id,
        name=name,
        prefix=raw_key[:8],
        key_hash=token_hash(raw_key),
    )
    session.add(key)
    await session.flush()
    session.add(SecurityEvent(actor_id=user.id, target_user_id=user.id,
        action="api_key.issued", details={"key_id": key.id, "name": key.name, "prefix": key.prefix}))
    await session.commit()

    response.headers["Cache-Control"] = "no-store"
    return {**public_key(key), "key": raw_key}


@router.get("/keys")
async def list_keys(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    keys = await session.scalars(
        select(APIKey)
        .where(APIKey.user_id == user.id)
        .order_by(APIKey.id.desc())
    )
    return [public_key(key) for key in keys]


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_key(
    key_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    actor_id, version, role = user.id, user.session_version, user.role
    session.expunge(user)
    if role == "admin":
        await session.execute(text("SELECT pg_advisory_xact_lock(714202602)"))
    actor = await session.scalar(select(User).where(User.id == actor_id)
        .with_for_update().execution_options(populate_existing=True))
    if not actor.active or not actor.password_hash or actor.session_version != version:
        raise HTTPException(401, "Account or login session is unavailable.")
    owner_id = await session.scalar(select(APIKey.user_id).where(APIKey.id == key_id))
    if owner_id is None or (owner_id != actor.id and actor.role != "admin"):
        raise HTTPException(404, "API key not found.")
    await session.scalar(select(User).where(User.id == owner_id).with_for_update())
    key = await session.scalar(select(APIKey).where(APIKey.id == key_id)
        .with_for_update().execution_options(populate_existing=True))
    if key.revoked_at is None:
        key.revoked_at = datetime.now(timezone.utc)
        session.add(SecurityEvent(actor_id=actor.id, target_user_id=key.user_id,
            action="api_key.revoked", details={"key_id": key.id, "name": key.name, "prefix": key.prefix}))
        await session.commit()
    return Response(status_code=204)


@router.get("/key-check")
async def key_check(request: Request):
    scheme, separator, raw_key = request.headers.get(
        "authorization", ""
    ).partition(" ")

    if not separator or scheme.lower() != "bearer":
        raise HTTPException(
            401,
            "A Bearer API key is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await authenticate_api_key(raw_key.strip())
    return public_user(user)


class RenewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.get("/auth/session")
async def session_status(request: Request, response: Response, user: User = Depends(current_user)):
    response.headers["Cache-Control"] = "no-store"
    return browser_sessions.metadata(request.state.browser_session)


@router.post("/auth/renew")
async def renew_session(body: RenewBody, request: Request, response: Response,
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    version = user.session_version
    user = await session.scalar(select(User).where(User.id == user.id)
        .with_for_update().execution_options(populate_existing=True))
    if not user.active or not user.password_hash or user.session_version != version:
        raise HTTPException(401, "Account or session is unavailable.")
    response.headers["Cache-Control"] = "no-store"
    return await browser_sessions.renew(session, user, request.state.browser_session.id)
