"""Read saved PVC/PV identities under one live private worker claim."""

import time

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.models import StoreVolume
from app.store_claims import StoreClaimIdentity
from app.store_job_guard import StoreJobClaimLost
from app.store_jobs import StoreJobClaim, _locked_claim
from app.store_namespace import _opaque, namespace_for
from app.store_scheduling import StoreSchedulingUnavailable
from app.store_volume_binding import (
    VolumeBindingUnavailable, _VolumeClaimLost, _check_live,
)
from app.store_volumes import STORE_CLAIM_NAMES, StoreVolumeBinding, StoreVolumeError


def _saved_record(row, identity, namespace_uid):
    if row.store_id != identity.store_id or row.namespace_uid != namespace_uid:
        raise StoreVolumeError("Saved storage belongs to a different store namespace.")
    if row.volume_uid is None:
        return StoreClaimIdentity(
            identity, row.namespace_uid, row.claim_name, row.claim_uid,
            row.volume_name, row.storage_class,
        )
    return StoreVolumeBinding(
        identity, row.namespace_uid, row.claim_name, row.claim_uid,
        row.volume_name, row.volume_uid, row.storage_class,
    )


async def load_store_storage(
    session_factory, claim, *, namespace_uid, for_cleanup=False,
):
    """Return copied identities after closing the database transaction.

    Partial records remain partial. Missing records are unknown: even an empty
    tuple cannot prove storage absence. These snapshots authorize no external
    operation; recheck live ownership before Kubernetes access.
    """
    if (
        not callable(session_factory) or not isinstance(claim, StoreJobClaim)
        or not _opaque(namespace_uid) or type(for_cleanup) is not bool
        or claim.action not in ("provision", "delete")
        or (claim.action == "delete" and not for_cleanup)
    ):
        raise ValueError("A private worker claim and saved namespace UID are required.")
    identity = namespace_for(claim)
    started = time.monotonic()
    try:
        async with session_factory() as session:
            async with session.begin():
                locked = await _locked_claim(session, claim)
                if locked is None:
                    raise StoreJobClaimLost("The store job claim is no longer usable.")
                operation, store, now = locked
                if (
                    store.id != claim.store_id or store.owner_id != claim.owner_id
                    or store.namespace != identity.name
                    or store.release_name != identity.release_name
                    or store.name != claim.name or store.slug != claim.slug
                    or store.url != claim.url or store.namespace_uid != namespace_uid
                    or operation.deadline_at != claim.deadline_at
                ):
                    raise StoreVolumeError("Saved store storage ownership changed.")
                _check_live(operation, now, for_cleanup)
                rows = list((await session.scalars(
                    select(StoreVolume).where(StoreVolume.store_id == store.id)
                    .order_by(StoreVolume.claim_name).limit(3)
                    .with_for_update().execution_options(populate_existing=True)
                )).all())
                now = await session.scalar(select(func.clock_timestamp()))
                _check_live(operation, now, for_cleanup)
                if len(rows) > len(STORE_CLAIM_NAMES):
                    raise StoreVolumeError("Saved store storage inventory is invalid.")
                result = tuple(
                    _saved_record(row, identity, namespace_uid) for row in rows
                )
                for field in ("claim_name", "claim_uid", "volume_name", "volume_uid"):
                    values = [
                        getattr(item, field, None) for item in result
                        if getattr(item, field, None) is not None
                    ]
                    if len(values) != len(set(values)):
                        raise StoreVolumeError("Saved store storage identifiers conflict.")
                remaining = (operation.lease_expires_at - now).total_seconds()
                if not for_cleanup:
                    remaining = min(
                        remaining, (operation.deadline_at - now).total_seconds(),
                    )
                stop_at = started + remaining
        if time.monotonic() >= stop_at:
            raise StoreJobClaimLost("The storage observation allowance elapsed.")
        return result
    except _VolumeClaimLost:
        raise StoreJobClaimLost("The store job allowance elapsed.") from None
    except (SQLAlchemyError, StoreSchedulingUnavailable):
        raise VolumeBindingUnavailable(
            "Saved store storage is unavailable.",
        ) from None
