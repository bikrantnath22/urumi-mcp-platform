import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import check_origin, public_user, require_admin
from app.database import get_session
from app.models import User
from app.security import APP_ORIGIN, hash_password, token_hash


router = APIRouter(prefix="/api", tags=["users"])


class InviteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid email address.")
        return value


class AcceptInviteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: SecretStr
    password: SecretStr

    @field_validator("token")
    @classmethod
    def validate_token(cls, value):
        if not 40 <= len(value.get_secret_value()) <= 128:
            raise ValueError("Invalid invitation token format.")
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value):
        password = value.get_secret_value()
        if not 6 <= len(password) <= 16 or not password.strip():
            raise ValueError("Password must contain 6 to 16 characters.")
        return value


def public_account(user: User) -> dict:
    return {
        **public_user(user),
        "active": user.active,
        "registered": bool(user.password_hash),
        "invite_expires_at": user.invite_expires_at,
        "created_at": user.created_at,
    }


@router.get("/users")
async def list_users(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    users = await session.scalars(select(User).order_by(User.id))
    return [public_account(user) for user in users]


@router.post("/users/invite", status_code=201)
async def invite_member(
    body: InviteBody,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    user = await session.scalar(
        select(User)
        .where(User.email == body.email)
        .with_for_update()
    )

    if user is not None and (
        user.password_hash
        or user.role != "member"
        or not user.active
    ):
        raise HTTPException(
            409,
            "This account already exists or cannot be invited.",
        )

    if user is None:
        user = User(
            email=body.email,
            role="member",
            active=True,
            password_hash=None,
        )
        session.add(user)

    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    # Reissuing a pending invitation invalidates its previous token.
    user.invite_token_hash = token_hash(raw_token)
    user.invite_expires_at = expires_at

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            409, "The account changed; retry the invitation."
        ) from None

    return {
        "user": public_account(user),
        "invite_token": raw_token,
        "invite_url": f"{APP_ORIGIN}/accept-invite?token={raw_token}",
        "expires_at": expires_at,
    }


@router.post("/auth/accept-invite")
async def accept_invite(
    body: AcceptInviteBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    check_origin(request)

    user = await session.scalar(
        select(User)
        .where(
            User.invite_token_hash
            == token_hash(body.token.get_secret_value())
        )
        .with_for_update()
    )

    now = datetime.now(timezone.utc)
    if (
        user is None
        or not user.active
        or user.role != "member"
        or user.password_hash is not None
        or user.invite_expires_at is None
        or user.invite_expires_at <= now
    ):
        raise HTTPException(
            400, "Invitation is invalid, expired, or already used."
        )

    user.password_hash = await hash_password(
        body.password.get_secret_value()
    )
    user.invite_token_hash = None
    user.invite_expires_at = None
    await session.commit()

    return public_user(user)
