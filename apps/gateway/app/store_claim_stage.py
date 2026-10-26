"""Read, record and recheck one existing PVC, including Pending storage."""

from app.store_claim_api import read_store_claim
from app.store_claims import StoreClaimIdentity
from app.store_job_guard import StoreJobClaimLost, require_claim
from app.store_jobs import StoreJobClaim
from app.store_namespace import _opaque, namespace_for
from app.store_volume_binding import record_store_claim
from app.store_volumes import STORE_CLAIM_NAMES, StoreVolumeError


def _belongs(value, identity, namespace_uid, name):
    if (
        type(value) is not StoreClaimIdentity
        or value.identity != identity
        or value.namespace_uid != namespace_uid
        or value.claim_name != name
    ):
        raise StoreVolumeError("Claim recording returned a different store identity.")
    return value


async def _save(factory, claim, observed, *, for_cleanup):
    saved = await record_store_claim(
        factory, claim, observed, for_cleanup=for_cleanup,
    )
    if saved is None:
        raise StoreJobClaimLost("The worker claim was lost before the PVC was recorded.")
    _belongs(saved, observed.identity, observed.namespace_uid, observed.claim_name)
    if (
        saved.claim_uid != observed.claim_uid
        or saved.storage_class != observed.storage_class
        or (
            observed.volume_name is not None
            and saved.volume_name != observed.volume_name
        )
    ):
        raise StoreVolumeError("The saved PVC differs from its verified observation.")
    return saved


async def capture_store_claim(
    session_factory, claim, connection, scope, *, claim_name, for_cleanup=False,
):
    """Capture one PVC; missing resources stay pending.

    The caller renews the lease. Cleanup may pass the original deadline only
    with a live claim. This stage creates/deletes no Kubernetes resources and
    supplies no readiness or deletion proof. Committed progress survives errors.
    """
    if (
        not callable(session_factory) or not isinstance(claim, StoreJobClaim)
        or claim_name not in STORE_CLAIM_NAMES or type(for_cleanup) is not bool
        or claim.action not in ("provision", "delete")
        or (claim.action == "delete" and not for_cleanup)
    ):
        raise ValueError("A private worker claim and fixed PVC are required.")
    checked = await require_claim(
        session_factory, claim, for_cleanup=for_cleanup,
    )
    namespace_uid = checked.namespace_uid
    if not _opaque(namespace_uid):
        raise StoreVolumeError("Save namespace ownership before recording storage.")
    identity = namespace_for(checked.claim)

    async def read(expected=None):
        value = await read_store_claim(
            session_factory, checked.claim, connection, scope,
            namespace_uid=namespace_uid, claim_name=claim_name,
            expected=expected, for_cleanup=for_cleanup,
        )
        return _belongs(value, identity, namespace_uid, claim_name)

    observed = await read()
    saved = await _save(
        session_factory, checked.claim, observed, for_cleanup=for_cleanup,
    )
    latest = await read(saved)
    if latest != saved:
        # A Pending PVC can gain its first PV name during the readback.
        saved = await _save(
            session_factory, checked.claim, latest, for_cleanup=for_cleanup,
        )
        latest = await read(saved)
    if latest != saved:
        raise StoreVolumeError("The store PVC changed after its identity was saved.")

    await require_claim(
        session_factory, checked.claim, for_cleanup=for_cleanup,
        expected_namespace_uid=namespace_uid,
    )
    return saved
