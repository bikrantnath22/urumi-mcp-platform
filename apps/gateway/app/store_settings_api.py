"""Admin controls for PostgreSQL-backed store provisioning settings."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import browser_sessions
from app.auth import require_admin
from app.database import get_session
from app.models import SecurityEvent, User
from app.store_settings import (
    editable_settings,
    get_store_settings,
    lock_store_admission,
    public_store_settings,
)


router = APIRouter(
    prefix="/api/admin/store-settings",
    tags=["store settings"],
)


class StoreSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    revision: int = Field(ge=1)
    enabled: bool
    max_per_user: int = Field(ge=1, le=100)
    max_total: int = Field(ge=1, le=100)
    max_concurrent_creations: int = Field(default=2, ge=1, le=100)
    timeout_seconds: int = Field(
        ge=60,
        le=3600,
        multiple_of=60,
    )

    @model_validator(mode="after")
    def consistent_limits(self):
        if self.max_per_user > self.max_total:
            raise ValueError(
                "The per-user limit cannot exceed the platform limit."
            )
        return self


@router.get("")
async def read_settings(
    response: Response,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    response.headers["Cache-Control"] = "no-store"

    try:
        record = await get_store_settings(session)
        return public_store_settings(record)
    except SQLAlchemyError:
        await session.rollback()
        raise HTTPException(
            503,
            "Store settings storage is unavailable.",
        ) from None


@router.put("")
async def update_settings(
    body: StoreSettingsBody,
    request: Request,
    response: Response,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    response.headers["Cache-Control"] = "no-store"
    actor_id = admin.id
    session_version = admin.session_version

    try:
        await lock_store_admission(session)

        actor = await session.scalar(
            select(User)
            .where(User.id == actor_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

        if (
            actor is None
            or not actor.active
            or not actor.password_hash
            or actor.session_version != session_version
        ):
            raise HTTPException(
                401,
                "Account or login session is unavailable.",
            )

        if actor.role != "admin":
            raise HTTPException(
                403,
                "Admin access is required.",
            )

        # Recheck the session after waiting for mutation locks.
        await browser_sessions.load(
            session,
            actor,
            request.state.browser_session.id,
            lock=True,
        )

        record = await get_store_settings(session, lock=True)

        if record.revision != body.revision:
            raise HTTPException(
                409,
                "Another admin changed these settings. "
                "Refresh before saving.",
            )

        before = editable_settings(record)
        # Preserve this setting when an older dashboard omits the new field.
        after = {
            **before,
            **body.model_dump(exclude={"revision"}, exclude_unset=True),
        }

        if before != after:
            for name, value in after.items():
                setattr(record, name, value)

            record.revision += 1
            record.updated_at = await session.scalar(
                select(func.clock_timestamp())
            )
            record.updated_by_id = actor.id

            session.add(
                SecurityEvent(
                    actor_id=actor.id,
                    target_user_id=None,
                    action="store_settings.updated",
                    details={
                        "before": before,
                        "after": after,
                        "revision": record.revision,
                    },
                )
            )

        await session.flush()
        result = public_store_settings(record)
        await session.commit()

        return result

    except HTTPException:
        await session.rollback()
        raise

    except SQLAlchemyError:
        await session.rollback()
        raise HTTPException(
            503,
            "Store settings storage is unavailable. "
            "Refresh before retrying.",
        ) from None