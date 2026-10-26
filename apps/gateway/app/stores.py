"""Authenticated reads for stores managed by the provisioner."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_user
from app.database import get_session
from app.models import Store, StoreOperation, User


router = APIRouter(prefix="/api/stores", tags=["stores"])


def visible_to(statement, user):
    if user.role != "admin":
        statement = statement.where(Store.owner_id == user.id)
    return statement


def public_store(record):
    return {
        "id": record.id,
        "owner_id": record.owner_id,
        "name": record.name,
        "slug": record.slug,
        "url": record.url,
        "status": record.status,
        "status_reason": record.status_reason,
        "generation": record.generation,
        "access_enabled": record.access_enabled,
        "access_guarded": record.access_guarded,
        "access_revision": record.access_revision,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def public_operation(record):
    if record is None:
        return None

    return {
        "id": record.id,
        "store_id": record.store_id,
        "action": record.action,
        "status": record.status,
        "attempts": record.attempts,
        "created_at": record.created_at,
        "deadline_at": record.deadline_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
    }


async def store_for_user(
    session, user, store_id, *, include_deleted=False
):
    statement = visible_to(
        select(Store).where(Store.id == store_id),
        user,
    )

    if not include_deleted:
        statement = statement.where(Store.status != "Deleted")

    record = await session.scalar(statement)

    if record is None:
        raise HTTPException(404, "Store not found.")

    return record


@router.get("")
async def list_stores(
    response: Response,
    limit: int = Query(20, ge=1, le=100),
    before_id: UUID | None = Query(None),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    response.headers["Cache-Control"] = "no-store"

    try:
        statement = visible_to(
            select(Store).where(Store.status != "Deleted"),
            user,
        )

        if before_id is not None:
            # Deleted stores can anchor pagination; ownership still applies.
            anchor = await store_for_user(
                session,
                user,
                before_id,
                include_deleted=True,
            )
            statement = statement.where(or_(
                Store.created_at < anchor.created_at,
                and_(
                    Store.created_at == anchor.created_at,
                    Store.id < anchor.id,
                ),
            ))

        records = list(await session.scalars(
            statement.order_by(
                Store.created_at.desc(),
                Store.id.desc(),
            ).limit(limit + 1)
        ))

        return {
            "items": [
                public_store(record)
                for record in records[:limit]
            ],
            "next_before_id": (
                records[limit - 1].id
                if len(records) > limit
                else None
            ),
        }

    except SQLAlchemyError:
        await session.rollback()
        raise HTTPException(
            503,
            "Store information is temporarily unavailable.",
        ) from None


@router.get("/{store_id}")
async def get_store(
    store_id: UUID,
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    response.headers["Cache-Control"] = "no-store"

    try:
        record = await store_for_user(session, user, store_id)

        operation = await session.scalar(
            select(StoreOperation)
            .where(StoreOperation.store_id == record.id)
            .order_by(
                StoreOperation.created_at.desc(),
                StoreOperation.id.desc(),
            )
            .limit(1)
        )

        return {
            "store": public_store(record),
            "operation": public_operation(operation),
        }

    except SQLAlchemyError:
        await session.rollback()
        raise HTTPException(
            503,
            "Store information is temporarily unavailable.",
        ) from None