"""Persist verified store volume identifiers under a live private worker claim."""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.models import SecurityEvent, StoreVolume
from app.store_jobs import StoreJobClaim, _locked_claim
from app.store_namespace import namespace_for
from app.store_scheduling import StoreSchedulingUnavailable
from app.store_volumes import StoreVolumeBinding, StoreVolumeError


class VolumeBindingUnavailable(RuntimeError):
    """Storage identity records could not be used."""


class _VolumeClaimLost(Exception):
    """Roll back writes after a lease or provisioning deadline expires."""


def _check_live(operation, now, for_cleanup):
    if operation.lease_expires_at <= now or (
        not for_cleanup and operation.deadline_at <= now
    ):
        raise _VolumeClaimLost()


def _check_owner(operation, store, claim, binding):
    identity = binding.identity
    if (
        store.id != claim.store_id
        or store.owner_id != claim.owner_id
        or store.namespace != identity.name
        or store.release_name != identity.release_name
        or store.name != claim.name
        or store.slug != claim.slug
        or store.url != claim.url
        or store.namespace_uid != binding.namespace_uid
        or operation.deadline_at != claim.deadline_at
    ):
        raise StoreVolumeError("Managed store storage ownership changed.")


def _check_saved(row, binding):
    if (
        row.store_id != binding.identity.store_id
        or row.namespace_uid != binding.namespace_uid
        or row.claim_name != binding.claim_name
        or row.claim_uid != binding.claim_uid
        or row.storage_class != binding.storage_class
        or row.volume_name not in (None, binding.volume_name)
        or row.volume_uid not in (None, binding.volume_uid)
    ):
        raise StoreVolumeError("The saved store storage identity changed.")


async def bind_store_volume(
    session_factory, claim, binding, *, for_cleanup=False,
):
    """Return the committed binding, or None after losing the worker claim.

    Only verified identifiers are accepted; this function performs no Kubernetes
    calls and grants no deletion permission. Matching retries change nothing.
    A pending record may gain its first PV identifiers, but never replacements.
    """
    if not callable(session_factory) or not isinstance(claim, StoreJobClaim):
        raise ValueError("A private worker claim and session factory are required.")
    if type(for_cleanup) is not bool or claim.action not in ("provision", "delete"):
        raise ValueError("Invalid storage binding mode.")
    if claim.action == "delete" and not for_cleanup:
        raise ValueError("Delete work requires cleanup mode.")
    identity = namespace_for(claim)
    if not isinstance(binding, StoreVolumeBinding) or binding.identity != identity:
        raise StoreVolumeError("The storage observation belongs to another store.")
    # Copy and validate identifiers before any database await.
    binding = StoreVolumeBinding(
        identity, binding.namespace_uid, binding.claim_name, binding.claim_uid,
        binding.volume_name, binding.volume_uid, binding.storage_class,
    )

    try:
        async with session_factory() as session:
            async with session.begin():
                locked = await _locked_claim(session, claim)
                if locked is None:
                    return None
                operation, store, now = locked
                _check_owner(operation, store, claim, binding)
                _check_live(operation, now, for_cleanup)
                row = await session.get(
                    StoreVolume, (store.id, binding.claim_name),
                    with_for_update=True, populate_existing=True,
                )
                # Waiting for a storage row must not revive an expired lease.
                now = await session.scalar(select(func.clock_timestamp()))
                _check_live(operation, now, for_cleanup)
                if row is None:
                    row = StoreVolume(
                        store_id=store.id, namespace_uid=binding.namespace_uid,
                        claim_name=binding.claim_name, claim_uid=binding.claim_uid,
                        volume_name=binding.volume_name, volume_uid=binding.volume_uid,
                        storage_class=binding.storage_class, updated_at=now,
                    )
                    session.add(row)
                    changed = True
                else:
                    _check_saved(row, binding)
                    changed = row.volume_name is None or row.volume_uid is None
                    if changed:
                        row.volume_name = binding.volume_name
                        row.volume_uid = binding.volume_uid
                        row.updated_at = now
                if changed:
                    store.updated_at = now
                    session.add(SecurityEvent(
                        actor_id=None, target_user_id=store.owner_id,
                        action="store.volume_bound",
                        details={
                            "store_id": str(store.id),
                            "operation_id": str(operation.id),
                            "generation": operation.store_generation,
                            "requested_by_id": operation.requested_by_id,
                            "namespace_uid": binding.namespace_uid,
                            "claim_name": binding.claim_name,
                            "claim_uid": binding.claim_uid,
                            "volume_name": binding.volume_name,
                            "volume_uid": binding.volume_uid,
                            "storage_class": binding.storage_class,
                            "for_cleanup": for_cleanup,
                        },
                    ))
                    await session.flush()
                now = await session.scalar(select(func.clock_timestamp()))
                _check_live(operation, now, for_cleanup)
                result = binding
        return result
    except _VolumeClaimLost:
        return None
    except IntegrityError:
        raise StoreVolumeError(
            "The store volume binding conflicts with an existing record.",
        ) from None
    except (SQLAlchemyError, StoreSchedulingUnavailable):
        raise VolumeBindingUnavailable(
            "Store volume ownership storage is unavailable.",
        ) from None


async def record_store_claim(
    session_factory, claim, observed, *, for_cleanup=False,
):
    """Save a PVC UID before its PV is known; return committed identifiers.

    Known PV names/UIDs are retained. A claim-only observation never proves
    readiness or cleanup. The caller owns external checks and lease renewal.
    """
    from app.store_claims import StoreClaimIdentity
    from app.store_namespace import _opaque

    if not callable(session_factory) or not isinstance(claim, StoreJobClaim):
        raise ValueError("A private worker claim and session factory are required.")
    if type(for_cleanup) is not bool or claim.action not in ("provision", "delete"):
        raise ValueError("Invalid claim recording mode.")
    if claim.action == "delete" and not for_cleanup:
        raise ValueError("Delete work requires cleanup mode.")
    identity = namespace_for(claim)
    if type(observed) is not StoreClaimIdentity or observed.identity != identity:
        raise StoreVolumeError("The claim observation belongs to another store.")
    observed = StoreClaimIdentity(
        identity, observed.namespace_uid, observed.claim_name, observed.claim_uid,
        observed.volume_name, observed.storage_class,
    )

    try:
        async with session_factory() as session:
            async with session.begin():
                locked = await _locked_claim(session, claim)
                if locked is None:
                    return None
                operation, store, now = locked
                _check_owner(operation, store, claim, observed)
                _check_live(operation, now, for_cleanup)
                row = await session.get(
                    StoreVolume, (store.id, observed.claim_name),
                    with_for_update=True, populate_existing=True,
                )
                now = await session.scalar(select(func.clock_timestamp()))
                _check_live(operation, now, for_cleanup)
                changed = row is None
                if row is None:
                    row = StoreVolume(
                        store_id=store.id, namespace_uid=observed.namespace_uid,
                        claim_name=observed.claim_name, claim_uid=observed.claim_uid,
                        volume_name=observed.volume_name, volume_uid=None,
                        storage_class=observed.storage_class, updated_at=now,
                    )
                    session.add(row)
                else:
                    if (
                        row.store_id != store.id
                        or row.namespace_uid != observed.namespace_uid
                        or row.claim_name != observed.claim_name
                        or row.claim_uid != observed.claim_uid
                        or row.storage_class != observed.storage_class
                        or (
                            observed.volume_name is not None
                            and row.volume_name not in (None, observed.volume_name)
                        )
                        or (
                            row.volume_uid is not None
                            and (row.volume_name is None or not _opaque(row.volume_uid))
                        )
                    ):
                        raise StoreVolumeError("The saved store claim identity changed.")
                    if row.volume_name is None and observed.volume_name is not None:
                        row.volume_name = observed.volume_name
                        changed = True

                if changed:
                    row.updated_at = now
                    store.updated_at = now
                    session.add(SecurityEvent(
                        actor_id=None, target_user_id=store.owner_id,
                        action="store.volume_claim_recorded",
                        details={
                            "store_id": str(store.id),
                            "operation_id": str(operation.id),
                            "generation": operation.store_generation,
                            "requested_by_id": operation.requested_by_id,
                            "namespace_uid": observed.namespace_uid,
                            "claim_name": observed.claim_name,
                            "claim_uid": observed.claim_uid,
                            "volume_name": row.volume_name,
                            "for_cleanup": for_cleanup,
                        },
                    ))
                    await session.flush()
                result = StoreClaimIdentity(
                    identity, row.namespace_uid, row.claim_name, row.claim_uid,
                    row.volume_name, row.storage_class,
                )
                now = await session.scalar(select(func.clock_timestamp()))
                _check_live(operation, now, for_cleanup)
        return result
    except _VolumeClaimLost:
        return None
    except IntegrityError:
        raise StoreVolumeError(
            "The store claim conflicts with an existing storage record.",
        ) from None
    except (SQLAlchemyError, StoreSchedulingUnavailable):
        raise VolumeBindingUnavailable(
            "Store claim ownership storage is unavailable.",
        ) from None
