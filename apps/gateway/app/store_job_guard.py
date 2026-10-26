"""Short-lived worker claim observations; no Kubernetes calls or lease renewal."""

from dataclasses import dataclass, field, replace
import time

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.store_jobs import StoreJobClaim, _locked_claim
from app.store_namespace import _opaque, namespace_for
from app.store_scheduling import StoreSchedulingUnavailable


_UNSET = object()


class StoreJobGuardUnavailable(RuntimeError):
    """The worker cannot safely check its current database ownership."""


class StoreJobClaimLost(RuntimeError):
    """The worker must stop using its old claim."""


@dataclass(frozen=True, slots=True)
class CheckedStoreJob:
    """An observation, not a transferable authorization token."""

    claim: StoreJobClaim = field(repr=False)
    namespace_uid: str | None
    for_cleanup: bool
    stop_at: float = field(repr=False)
    credentials_secret_uid: str | None = None

    @property
    def remaining_seconds(self):
        return max(0.0, self.stop_at - time.monotonic())


async def check_claim(
    session_factory, claim, *, for_cleanup=False,
    expected_namespace_uid=_UNSET,
    expected_credentials_secret_uid=_UNSET,
):
    """Return a fresh observation, or None when this worker must stop.

    Recheck before each external operation. This does not renew the lease.
    Cleanup may pass the original deadline, but still needs a live lease.
    """
    if not isinstance(claim, StoreJobClaim):
        raise ValueError("A private store job claim is required.")
    if type(for_cleanup) is not bool:
        raise ValueError("Cleanup must be a boolean.")
    if claim.action not in ("provision", "delete") or (
        claim.action == "delete" and not for_cleanup
    ):
        raise ValueError("A delete claim requires cleanup mode.")
    if (
        expected_namespace_uid is not _UNSET
        and expected_namespace_uid is not None
        and not _opaque(expected_namespace_uid)
    ):
        raise ValueError("Invalid expected namespace UID.")
    if (
        expected_credentials_secret_uid is not _UNSET
        and expected_credentials_secret_uid is not None
        and not _opaque(expected_credentials_secret_uid)
    ):
        raise ValueError("Invalid expected credentials Secret UID.")
    identity = namespace_for(claim)
    # Starting before the DB wait makes the local cutoff conservative.
    started = time.monotonic()

    try:
        async with session_factory() as session:
            async with session.begin():
                locked = await _locked_claim(session, claim)
                if locked is None:
                    return None
                operation, store, _ = locked
                if (
                    store.id != claim.store_id
                    or store.owner_id != claim.owner_id
                    or store.namespace != identity.name
                    or store.release_name != identity.release_name
                    or store.name != claim.name
                    or store.slug != claim.slug
                    or store.url != claim.url
                    or operation.deadline_at != claim.deadline_at
                ):
                    return None
                uid = store.namespace_uid
                if uid is not None and not _opaque(uid):
                    raise StoreJobGuardUnavailable(
                        "Stored namespace ownership is unavailable.",
                    )
                if (
                    expected_namespace_uid is not _UNSET
                    and uid != expected_namespace_uid
                ):
                    return None

                secret_uid = store.credentials_secret_uid
                if secret_uid is not None and (
                    not _opaque(secret_uid) or uid is None
                ):
                    raise StoreJobGuardUnavailable(
                        "Stored credentials ownership is unavailable.",
                    )
                if (
                    expected_credentials_secret_uid is not _UNSET
                    and secret_uid != expected_credentials_secret_uid
                ):
                    return None

                now = await session.scalar(select(func.clock_timestamp()))
                remaining = (operation.lease_expires_at - now).total_seconds()
                if not for_cleanup:
                    remaining = min(
                        remaining, (operation.deadline_at - now).total_seconds(),
                    )
                if remaining <= 0:
                    return None
                result = CheckedStoreJob(
                    claim=replace(
                        claim, checked_at=now,
                        lease_expires_at=operation.lease_expires_at,
                    ),
                    namespace_uid=uid,
                    credentials_secret_uid=secret_uid,
                    for_cleanup=for_cleanup,
                    stop_at=started + remaining,
                )
        # Session and transaction have both closed before returning.
        if result.remaining_seconds <= 0:
            return None
        return result
    except (SQLAlchemyError, StoreSchedulingUnavailable):
        raise StoreJobGuardUnavailable(
            "Worker claim storage is unavailable.",
        ) from None


async def require_claim(
    session_factory, claim, *, for_cleanup=False,
    expected_namespace_uid=_UNSET,
    expected_credentials_secret_uid=_UNSET,
):
    result = await check_claim(
        session_factory, claim, for_cleanup=for_cleanup,
        expected_namespace_uid=expected_namespace_uid,
        expected_credentials_secret_uid=expected_credentials_secret_uid,
    )
    if result is None:
        raise StoreJobClaimLost("The store job claim is no longer usable.")
    return result
