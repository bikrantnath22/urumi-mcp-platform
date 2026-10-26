"""Select store work fairly under the shared PostgreSQL admission lock."""

from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select

from app.models import Store, StoreOperation, StoreProvisioningSettings
from app.store_settings import lock_store_admission


class StoreSchedulingUnavailable(RuntimeError):
    pass


def _priority(operation, now):
    if operation.action == "delete":
        return 0
    if operation.deadline_at <= now:
        return 1
    if operation.attempts > 0:
        return 2
    return 3


def _eligible(now):
    return or_(
        and_(
            StoreOperation.status == "Pending",
            StoreOperation.available_at <= now,
        ),
        and_(
            StoreOperation.status == "Running",
            StoreOperation.lease_expires_at <= now,
        ),
    )


async def lock_job_scheduling(session):
    await lock_store_admission(session)
    isolation = await session.scalar(
        select(func.current_setting("transaction_isolation"))
    )
    if isolation != "read committed":
        raise StoreSchedulingUnavailable(
            "Store scheduling requires READ COMMITTED transactions."
        )


async def select_next_job(session, base_query):
    """Return one locked row pair. The caller must hold one short transaction."""
    await lock_job_scheduling(session)
    settings = await session.get(StoreProvisioningSettings, 1)
    limit = None if settings is None else settings.max_concurrent_creations
    if type(limit) is not int or not 1 <= limit <= 100:
        raise StoreSchedulingUnavailable("Store scheduling settings are unavailable.")

    now = await session.scalar(select(func.clock_timestamp()))
    active_rows = (
        await session.execute(
            select(Store.owner_id, func.count(StoreOperation.id))
            .select_from(Store)
            .join(StoreOperation, StoreOperation.store_id == Store.id)
            .where(
                StoreOperation.action == "provision",
                StoreOperation.status == "Running",
                StoreOperation.lease_expires_at > now,
            )
            .group_by(Store.owner_id)
        )
    ).all()
    active = dict(active_rows)
    occupied = sum(active.values())

    served_rows = (
        await session.execute(
            select(Store.owner_id, func.max(StoreOperation.updated_at))
            .select_from(Store)
            .join(StoreOperation, StoreOperation.store_id == Store.id)
            .where(
                StoreOperation.action == "provision",
                StoreOperation.attempts > 0,
                Store.status != "Deleted",
            )
            .group_by(Store.owner_id)
        )
    ).all()
    last_served = dict(served_rows)
    never_served = datetime.min.replace(tzinfo=timezone.utc)

    candidates = (
        await session.execute(base_query.where(_eligible(now)))
    ).all()
    candidates = sorted(
        candidates,
        key=lambda row: (
            _priority(row[0], now),
            active.get(row[1].owner_id, 0),
            last_served.get(row[1].owner_id, never_served),
            row[0].created_at,
            row[0].id.int,
        ),
    )

    for operation, _ in candidates:
        if operation.action == "provision" and occupied >= limit:
            continue
        statement = (
            base_query
            .where(
                StoreOperation.id == operation.id,
                _eligible(now),
            )
            .limit(1)
            .with_for_update(of=(StoreOperation, Store), skip_locked=True)
        )
        row = (await session.execute(statement)).one_or_none()
        if row is not None:
            return row

    return None
