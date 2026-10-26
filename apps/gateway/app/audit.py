from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_user
from app.database import get_session
from app.models import AuditLog, User


router = APIRouter(prefix="/api", tags=["audit"])


@router.get("/audit")
async def read_audit(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=100),
    before_id: int | None = Query(
        default=None, ge=1, le=9223372036854775807
    ),
    user_id: int | None = Query(
        default=None, ge=1, le=2147483647
    ),
    server_slug: str | None = Query(
        default=None, min_length=1, max_length=32
    ),
    tool: str | None = Query(
        default=None, min_length=1, max_length=256
    ),
    outcome: Literal["success", "error", "denied"] | None = None,
    source: Literal["mcp", "chat", "dashboard"] | None = None,
):
    statement = (
        select(AuditLog, User.email)
        .outerjoin(User, User.id == AuditLog.user_id)
    )

    if user.role != "admin":
        if user_id is not None and user_id != user.id:
            raise HTTPException(
                403, "You can only view your own audit records."
            )
        statement = statement.where(AuditLog.user_id == user.id)
    elif user_id is not None:
        statement = statement.where(AuditLog.user_id == user_id)

    if before_id is not None:
        statement = statement.where(AuditLog.id < before_id)
    if server_slug is not None:
        statement = statement.where(AuditLog.server_slug == server_slug)
    if tool is not None:
        statement = statement.where(AuditLog.tool == tool)
    if outcome is not None:
        statement = statement.where(AuditLog.outcome == outcome)
    if source is not None:
        statement = statement.where(AuditLog.source == source)

    result = await session.execute(
        statement
        .order_by(AuditLog.id.desc())
        .limit(limit + 1)
    )
    rows = result.all()
    has_more = len(rows) > limit
    page = rows[:limit]

    items = [
        {
            "id": audit.id,
            "created_at": audit.created_at,
            "user_id": audit.user_id,
            "user_email": email,
            "server_slug": audit.server_slug,
            "tool": audit.tool,
            "source": audit.source,
            "duration_ms": audit.duration_ms,
            "outcome": audit.outcome,
            "error": audit.error,
        }
        for audit, email in page
    ]

    return {
        "items": items,
        "next_before_id": page[-1][0].id if has_more else None,
    }
