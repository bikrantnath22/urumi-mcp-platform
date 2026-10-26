"""One guarded cleanup pass; terminal completion remains a separate transaction."""

from dataclasses import dataclass, field

from app.store_claim_stage import capture_store_claim
from app.store_job_guard import StoreJobClaimLost, require_claim
from app.store_jobs import StoreJobClaim
from app.store_namespace import namespace_for
from app.store_namespace_stage import _api, _save_uid, cleanup_namespace
from app.store_storage_records import load_store_storage
from app.store_volume_binding import bind_store_volume
from app.store_volume_inventory import read_store_volume_inventory
from app.store_volume_recovery import _known_records
from app.store_volumes import (
    STORE_CLAIM_NAMES, StoreVolumeBinding, StoreVolumeError, StoreVolumePending,
)


@dataclass(frozen=True, slots=True)
class StoreCleanupPass:
    claim: StoreJobClaim = field(repr=False)
    namespace_uid: str | None
    namespace_absent: bool
    storage: tuple
    remaining_volumes: tuple
    unresolved_claims: tuple


async def _recover(factory, claim, connection, scope, uid):
    before = await load_store_storage(
        factory, claim, namespace_uid=uid, for_cleanup=True,
    )
    scan = await read_store_volume_inventory(
        factory, claim, connection, scope, namespace_uid=uid, known=before,
    )
    if scan.namespace_uid != uid:
        raise StoreVolumeError("The recovered namespace identity changed.")
    expected = {item.claim_name: item for item in before}
    for observed in scan.bindings:
        saved = await bind_store_volume(
            factory, claim, observed, for_cleanup=True,
        )
        if saved is None:
            raise StoreJobClaimLost("The claim was lost while saving recovered storage.")
        if saved != observed or type(saved) is not StoreVolumeBinding:
            raise StoreVolumeError("Recovered storage was not saved unchanged.")
        expected[saved.claim_name] = saved
    after = await load_store_storage(
        factory, claim, namespace_uid=uid, for_cleanup=True,
    )
    wanted = tuple(expected[name] for name in STORE_CLAIM_NAMES if name in expected)
    if after != wanted:
        raise StoreVolumeError("Saved storage changed during cleanup recovery.")
    await require_claim(
        factory, claim, for_cleanup=True, expected_namespace_uid=uid,
    )
    return scan, after


async def cleanup_store_resources(factory, claim, connection, scope):
    """Capture storage before deletion, then inspect recoverable leftovers.

    The caller owns lease renewal. Writes whose replies are lost propagate
    without retry. This stage never marks a store Deleted or Failed, removes
    finalizers, deletes PVs directly, or touches a host filesystem.
    Partial/unobserved claims remain explicit; absence alone cannot settle
    in-flight provisioning. Committed identities survive interruption.
    """
    checked = await require_claim(factory, claim, for_cleanup=True)
    claim = checked.claim
    identity = namespace_for(claim)
    uid = checked.namespace_uid
    api = _api(factory, claim, connection, scope, uid, cleanup=True)
    document = await api.read_namespace(
        identity, expected_uid=uid, allow_terminating=True,
    )
    if document is not None and uid is None:
        uid = await _save_uid(factory, claim, document, cleanup=True)
    if uid is None:
        await require_claim(
            factory, claim, for_cleanup=True, expected_namespace_uid=None,
        )
        return StoreCleanupPass(claim, None, True, (), (), ())

    if document is not None:
        for name in STORE_CLAIM_NAMES:
            try:
                await capture_store_claim(
                    factory, claim, connection, scope,
                    claim_name=name, for_cleanup=True,
                )
            except StoreVolumePending:
                # An absent claim is not evidence that its volume never existed.
                pass

    scan, saved = await _recover(factory, claim, connection, scope, uid)
    absent = await cleanup_namespace(factory, claim, connection, scope)
    if type(absent) is not bool:
        raise StoreVolumeError("Namespace cleanup returned an invalid observation.")
    if absent:
        scan, saved = await _recover(factory, claim, connection, scope, uid)
        if scan.namespace_present:
            raise StoreVolumeError("The store namespace reappeared after cleanup.")
    saved = _known_records(identity, uid, saved)
    full = {item.claim_name for item in saved if type(item) is StoreVolumeBinding}
    await require_claim(
        factory, claim, for_cleanup=True, expected_namespace_uid=uid,
    )
    return StoreCleanupPass(
        claim, uid, absent, saved, scan.bindings,
        tuple(name for name in STORE_CLAIM_NAMES if name not in full),
    )
