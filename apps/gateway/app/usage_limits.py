"""Admin-managed quotas; reads are uncached and edits are audited."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.auth import require_admin
from app.database import get_session
from app.models import SecurityEvent, UsageLimitSettings, User
from app.rate_limits import DEFAULTS, public_settings, settings

router = APIRouter(prefix="/api/admin/usage-limits", tags=["usage limits"])


class LimitsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=1)
    login_account_limit: int = Field(ge=1, le=100)
    login_ip_limit: int = Field(ge=1, le=1000)
    login_window_seconds: int = Field(ge=60, le=86400, multiple_of=60)
    chat_per_minute: int = Field(ge=1, le=1000)
    chat_per_hour: int = Field(ge=1, le=10000)
    chat_global_per_minute: int = Field(ge=1, le=10000)
    tool_per_minute: int = Field(ge=1, le=10000)

    @model_validator(mode="after")
    def consistent(self):
        if self.login_ip_limit < self.login_account_limit:
            raise ValueError("The IP login limit must be at least the account login limit.")
        if self.chat_per_hour < self.chat_per_minute:
            raise ValueError("The hourly chat limit must be at least the per-minute chat limit.")
        return self


@router.get("")
async def get_limits(response: Response, admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session)):
    record = await settings(session)
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return public_settings(record)


@router.put("")
async def update_limits(body: LimitsBody, response: Response,
    admin: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    snapshot_version = admin.session_version
    actor = await session.scalar(select(User).where(User.id == admin.id)
        .with_for_update().execution_options(populate_existing=True))
    if not actor.active or not actor.password_hash or actor.session_version != snapshot_version:
        raise HTTPException(401, "Account or login session is unavailable.")
    if actor.role != "admin":
        raise HTTPException(403, "Admin access is required.")
    await settings(session)
    record = await session.scalar(select(UsageLimitSettings).where(UsageLimitSettings.id == 1)
        .with_for_update().execution_options(populate_existing=True))
    if record.revision != body.revision:
        raise HTTPException(409, "Another admin changed these limits. Refresh before saving.")
    before = {name: getattr(record, name) for name in DEFAULTS}
    after = body.model_dump(exclude={"revision"})
    if before != after:
        for name, value in after.items():
            setattr(record, name, value)
        record.revision += 1
        record.updated_at = datetime.now(timezone.utc)
        record.updated_by_id = actor.id
        session.add(SecurityEvent(actor_id=actor.id, target_user_id=None,
            action="usage_limits.updated", details={"before": before, "after": after,
                "revision": record.revision}))
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return public_settings(record)
