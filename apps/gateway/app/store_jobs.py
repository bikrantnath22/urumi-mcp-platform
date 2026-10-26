"""Private PostgreSQL job claims; this module performs no Kubernetes work."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select

from app.models import Store, StoreOperation
from app.store_scheduling import lock_job_scheduling, select_next_job


DEFAULT_LEASE_SECONDS = 60


@dataclass(frozen=True, slots=True)
class StoreJobClaim:
    operation_id: UUID
    store_id: UUID
    owner_id: int
    action: str
    generation: int
    attempt: int
    namespace: str
    release_name: str
    name: str
    slug: str
    url: str
    deadline_at: datetime
    checked_at: datetime
    lease_expires_at: datetime
    lease_token: UUID = field(repr=False)

    @property
    def deadline_reached(self) -> bool:
        # A snapshot at the last claim/renewal, not a live clock.
        return self.checked_at >= self.deadline_at


def _seconds(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} to {maximum}.")
    return value


def _current_work():
    return and_(
        StoreOperation.store_generation == Store.generation,
        Store.deleted_at.is_(None),
        or_(
            and_(
                StoreOperation.action == "provision",
                Store.desired_state == "present",
                Store.status == "Provisioning",
            ),
            and_(
                StoreOperation.action == "delete",
                Store.desired_state == "absent",
                Store.status == "Deleting",
            ),
        ),
    )


def _job_query():
    return (
        select(StoreOperation, Store)
        .join(Store, Store.id == StoreOperation.store_id)
        .where(_current_work())
        .execution_options(populate_existing=True)
    )


def _snapshot(operation, store, now):
    return StoreJobClaim(
        operation_id=operation.id,
        store_id=store.id,
        owner_id=store.owner_id,
        action=operation.action,
        generation=operation.store_generation,
        attempt=operation.attempts,
        namespace=store.namespace,
        release_name=store.release_name,
        name=store.name,
        slug=store.slug,
        url=store.url,
        deadline_at=operation.deadline_at,
        checked_at=now,
        lease_expires_at=operation.lease_expires_at,
        lease_token=operation.lease_token,
    )


async def claim_next(session_factory, *, lease_seconds=DEFAULT_LEASE_SECONDS):
    """Claim one job within shared capacity, or recover expired work."""
    duration = _seconds(lease_seconds, 15, 300, "Lease seconds")

    async with session_factory() as session:
        async with session.begin():
            row = await select_next_job(session, _job_query())
            if row is None:
                return None

            operation, store = row
            now = await session.scalar(select(func.clock_timestamp()))
            operation.status = "Running"
            operation.attempts += 1
            operation.lease_token = uuid4()
            operation.lease_expires_at = now + timedelta(seconds=duration)
            operation.started_at = operation.started_at or now
            operation.updated_at = now
            await session.flush()
            result = _snapshot(operation, store, now)

    # Overdue work is claimed for cleanup, with its original deadline intact.
    return result


async def _locked_claim(session, claim):
    await lock_job_scheduling(session)
    statement = (
        _job_query()
        .where(
            StoreOperation.id == claim.operation_id,
            StoreOperation.store_id == claim.store_id,
            StoreOperation.action == claim.action,
            StoreOperation.store_generation == claim.generation,
            StoreOperation.attempts == claim.attempt,
            StoreOperation.status == "Running",
            StoreOperation.lease_token == claim.lease_token,
        )
        .with_for_update(of=(StoreOperation, Store))
    )
    row = (await session.execute(statement)).one_or_none()
    if row is None:
        return None

    # Read the DB clock AFTER acquiring locks; waiting must not revive a lease.
    now = await session.scalar(select(func.clock_timestamp()))
    operation, store = row
    if operation.lease_expires_at <= now:
        return None
    return operation, store, now


async def renew_claim(
    session_factory, claim, *, lease_seconds=DEFAULT_LEASE_SECONDS,
):
    """Return a fresh claim snapshot, or None when ownership has been lost."""
    duration = _seconds(lease_seconds, 15, 300, "Lease seconds")
    async with session_factory() as session:
        async with session.begin():
            locked = await _locked_claim(session, claim)
            if locked is None:
                return None
            operation, store, now = locked
            operation.lease_expires_at = now + timedelta(seconds=duration)
            operation.updated_at = now
            await session.flush()
            result = _snapshot(operation, store, now)
    return result


async def release_claim(session_factory, claim, *, retry_after_seconds=5):
    """Requeue owned work only AFTER external work has stopped."""
    delay = _seconds(retry_after_seconds, 0, 300, "Retry delay")
    async with session_factory() as session:
        async with session.begin():
            locked = await _locked_claim(session, claim)
            if locked is None:
                return False
            operation, _, now = locked
            operation.status = "Pending"
            operation.lease_token = None
            operation.lease_expires_at = None
            operation.available_at = now + timedelta(seconds=delay)
            operation.updated_at = now
            await session.flush()
    return True
