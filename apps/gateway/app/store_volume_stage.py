"""Read, save and recheck the two bound volumes of a provisioning job."""

from app.store_job_guard import StoreJobClaimLost, require_claim
from app.store_jobs import StoreJobClaim
from app.store_namespace import _opaque, namespace_for
from app.store_volume_api import read_store_volume
from app.store_volume_binding import bind_store_volume
from app.store_volumes import STORE_CLAIM_NAMES, StoreVolumeBinding, StoreVolumeError


def _belongs(value, identity, namespace_uid, name):
    if (
        type(value) is not StoreVolumeBinding
        or value.identity != identity
        or value.namespace_uid != namespace_uid
        or value.claim_name != name
    ):
        raise StoreVolumeError("Storage preparation returned a different store binding.")
    return value


async def prepare_store_volumes(session_factory, claim, connection, scope):
    """Return two committed bindings after guarded resource readbacks.

    Use only during provisioning, after workloads have bound their storage.
    The caller owns lease renewal and the shared connection. Missing/unbound
    storage stays pending. A failed pass may leave one valid binding saved;
    retries reuse it. This function performs no deletion or Ready completion.
    """
    if not isinstance(claim, StoreJobClaim) or claim.action != "provision":
        raise ValueError("A private provisioning claim is required.")
    checked = await require_claim(session_factory, claim)
    namespace_uid = checked.namespace_uid
    if not _opaque(namespace_uid):
        raise StoreVolumeError("Save namespace ownership before preparing storage.")
    identity = namespace_for(checked.claim)
    saved = []

    for name in STORE_CLAIM_NAMES:
        observed = await read_store_volume(
            session_factory, checked.claim, connection, scope,
            namespace_uid=namespace_uid, claim_name=name,
        )
        _belongs(observed, identity, namespace_uid, name)
        committed = await bind_store_volume(
            session_factory, checked.claim, observed,
        )
        if committed is None:
            raise StoreJobClaimLost("The worker claim was lost before storage was saved.")
        _belongs(committed, identity, namespace_uid, name)
        if committed != observed:
            raise StoreVolumeError("Saved storage differs from its verified observation.")
        saved.append(committed)

    # Recheck both resources after both database saves have committed.
    for committed in saved:
        current = await read_store_volume(
            session_factory, checked.claim, connection, scope,
            namespace_uid=namespace_uid, claim_name=committed.claim_name,
            expected=committed,
        )
        _belongs(current, identity, namespace_uid, committed.claim_name)
        if current != committed:
            raise StoreVolumeError("Store storage changed after its binding was saved.")

    await require_claim(
        session_factory, checked.claim,
        expected_namespace_uid=namespace_uid,
    )
    return tuple(saved)
