"""Shared PostgreSQL settings for managed store provisioning."""

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StoreProvisioningSettings


STORE_ADMISSION_LOCK = 714202610

EDITABLE_FIELDS = (
    "enabled",
    "max_per_user",
    "max_total",
    "max_concurrent_creations",
    "timeout_seconds",
)


def editable_settings(record):
    return {
        name: getattr(record, name)
        for name in EDITABLE_FIELDS
    }


def public_store_settings(record):
    return {
        "revision": record.revision,
        **editable_settings(record),
        "updated_at": record.updated_at,
        "updated_by_id": record.updated_by_id,
    }


async def lock_store_admission(session: AsyncSession):
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": STORE_ADMISSION_LOCK},
    )


async def get_store_settings(
    session: AsyncSession,
    *,
    lock: bool = False,
) -> StoreProvisioningSettings:
    statement = (
        select(StoreProvisioningSettings)
        .where(StoreProvisioningSettings.id == 1)
        .execution_options(populate_existing=True)
    )

    if lock:
        statement = statement.with_for_update()

    record = await session.scalar(statement)

    if record is None:
        raise HTTPException(
            503,
            "Store provisioning settings are unavailable.",
        )

    return record