"""Bind a validated credentials Secret UID before installing store workloads."""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.models import SecurityEvent, Store
from app.store_jobs import StoreJobClaim, _locked_claim
from app.store_namespace import namespace_for
from app.store_scheduling import StoreSchedulingUnavailable
from app.store_secret import StoreSecret, StoreSecretError


class SecretBindingUnavailable(RuntimeError):
    """Credentials Secret ownership storage could not be used."""


class _SecretClaimLost(Exception):
    """Roll back when the lease or provisioning deadline has elapsed."""


def _check_live(operation, now):
    if operation.lease_expires_at <= now or operation.deadline_at <= now:
        raise _SecretClaimLost()


async def bind_store_secret(
    session_factory, claim, document, *, scope, namespace_uid,
):
    """Return the committed Secret UID, or None after loss of the claim.

    The caller supplies an observed Kubernetes response. Only its validated UID
    is kept across database awaits; passwords and API keys are never persisted.
    This function makes no Kubernetes calls and does not renew the lease.
    """
    if not isinstance(claim, StoreJobClaim) or claim.action != "provision":
        raise ValueError("A private provisioning claim is required.")
    identity = namespace_for(claim)
    uid = StoreSecret(identity, scope, namespace_uid).check(document).uid
    del document

    try:
        async with session_factory() as session:
            async with session.begin():
                locked = await _locked_claim(session, claim)
                if locked is None:
                    return None
                operation, store, now = locked
                if (
                    store.id != claim.store_id
                    or store.owner_id != claim.owner_id
                    or store.namespace != identity.name
                    or store.release_name != identity.release_name
                    or store.name != claim.name
                    or store.slug != claim.slug
                    or store.url != claim.url
                    or store.namespace_uid != namespace_uid
                    or operation.deadline_at != claim.deadline_at
                ):
                    raise StoreSecretError("Managed store ownership changed.")
                _check_live(operation, now)

                if store.credentials_secret_uid is not None:
                    if store.credentials_secret_uid != uid:
                        raise StoreSecretError("The store Secret UID changed.")
                else:
                    other = await session.scalar(
                        select(Store.id)
                        .where(
                            Store.id != store.id,
                            Store.credentials_secret_uid == uid,
                        )
                        .limit(1)
                    )
                    if other is not None:
                        raise StoreSecretError(
                            "The Secret UID belongs to another store."
                        )
                    now = await session.scalar(select(func.clock_timestamp()))
                    _check_live(operation, now)
                    store.credentials_secret_uid = uid
                    store.updated_at = now
                    session.add(SecurityEvent(
                        actor_id=None,
                        target_user_id=store.owner_id,
                        action="store.credentials_bound",
                        details={
                            "store_id": str(store.id),
                            "operation_id": str(operation.id),
                            "generation": operation.store_generation,
                            "requested_by_id": operation.requested_by_id,
                            "namespace_uid": namespace_uid,
                            "credentials_secret_uid": uid,
                        },
                    ))
                    await session.flush()

                now = await session.scalar(select(func.clock_timestamp()))
                _check_live(operation, now)
                result = uid
        return result
    except _SecretClaimLost:
        return None
    except IntegrityError:
        raise StoreSecretError(
            "The store Secret binding conflicts with an existing record."
        ) from None
    except (SQLAlchemyError, StoreSchedulingUnavailable):
        raise SecretBindingUnavailable(
            "Credentials Secret ownership storage is unavailable."
        ) from None
