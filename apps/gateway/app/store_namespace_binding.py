"""Save an observed namespace UID under a live private worker claim."""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.models import SecurityEvent, Store
from app.store_jobs import StoreJobClaim, _locked_claim
from app.store_namespace import StoreNamespaceError, namespace_for


class NamespaceBindingUnavailable(RuntimeError):
    """Namespace ownership storage could not be used."""


class _BindingClaimLost(Exception):
    """Leave the transaction through rollback after a late expiry."""


def _check_live(operation, now, for_cleanup):
    if operation.lease_expires_at <= now or (
        not for_cleanup and operation.deadline_at <= now
    ):
        raise _BindingClaimLost()


def _check_identity(operation, store, claim, identity):
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
        raise StoreNamespaceError("Managed store identity changed.")


async def bind_namespace(session_factory, claim, document, *, for_cleanup=False):
    """Return the committed UID, or None if the job claim is no longer usable.

    The caller supplies a real Kubernetes response. This function makes no
    Kubernetes calls. Cleanup may record an existing namespace after the
    provisioning deadline; it never creates resources or extends that deadline.
    """
    if not isinstance(claim, StoreJobClaim):
        raise ValueError("A private store job claim is required.")
    if type(for_cleanup) is not bool:
        raise ValueError("Cleanup must be a boolean.")
    if claim.action not in ("provision", "delete") or (
        claim.action == "delete" and not for_cleanup
    ):
        raise ValueError("A delete claim requires cleanup mode.")

    identity = namespace_for(claim)
    # Keep only validated immutable strings across database awaits.
    uid, _ = identity.check(document, allow_terminating=for_cleanup)

    try:
        async with session_factory() as session:
            async with session.begin():
                locked = await _locked_claim(session, claim)
                if locked is None:
                    return None
                operation, store, now = locked
                _check_identity(operation, store, claim, identity)
                _check_live(operation, now, for_cleanup)

                if store.namespace_uid is not None:
                    if store.namespace_uid != uid:
                        raise StoreNamespaceError("The namespace UID has changed.")
                else:
                    other = await session.scalar(
                        select(Store.id)
                        .where(
                            Store.id != store.id,
                            Store.namespace_uid == uid,
                        )
                        .limit(1)
                    )
                    if other is not None:
                        raise StoreNamespaceError(
                            "The namespace UID belongs to another store."
                        )

                    now = await session.scalar(select(func.clock_timestamp()))
                    _check_live(operation, now, for_cleanup)
                    store.namespace_uid = uid
                    store.updated_at = now
                    session.add(SecurityEvent(
                        actor_id=None,
                        target_user_id=store.owner_id,
                        action="store.namespace_bound",
                        details={
                            "store_id": str(store.id),
                            "operation_id": str(operation.id),
                            "generation": operation.store_generation,
                            "requested_by_id": operation.requested_by_id,
                            "namespace_uid": uid,
                            "for_cleanup": for_cleanup,
                        },
                    ))
                    await session.flush()

                now = await session.scalar(select(func.clock_timestamp()))
                _check_live(operation, now, for_cleanup)
                result = uid
        return result
    except _BindingClaimLost:
        return None
    except IntegrityError:
        raise StoreNamespaceError(
            "The namespace binding conflicts with an existing record."
        ) from None
    except SQLAlchemyError:
        raise NamespaceBindingUnavailable(
            "Namespace ownership storage is unavailable."
        ) from None
