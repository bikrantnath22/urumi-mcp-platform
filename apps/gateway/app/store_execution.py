"""Concrete worker handlers; construction starts no worker or external operation."""

from sqlalchemy import func, select

from app.models import StoreVolume
from app.store_cleanup import StoreCleanupPass, cleanup_store_resources
from app.store_cluster_scope import StoreClusterScope
from app.store_completion import (
    FAILURE_REASONS, StoreCompletionConflict, _finish,
    _registrations, _remove_registrations,
)
from app.store_credentials import _EMAIL
from app.store_job_guard import require_claim
from app.store_jobs import StoreJobClaim, _locked_claim
from app.store_kube_connection import KubernetesConnection
from app.store_namespace import _opaque, namespace_for
from app.store_provisioning import (
    StoreProvisioningResult, finish_provisioning, provision_store,
)
from app.store_storage_records import _saved_record
from app.store_volume_recovery import _known_records
from app.store_volumes import STORE_CLAIM_NAMES, StoreVolumeBinding


class _CleanupExpired(Exception):
    pass


def _conflict():
    raise StoreCompletionConflict("Cleanup evidence or saved ownership changed.")


def _cleanup_evidence(claim, result):
    if (
        type(claim) is not StoreJobClaim
        or claim.action not in ("provision", "delete")
        or type(result) is not StoreCleanupPass
        or type(result.claim) is not StoreJobClaim
        or type(result.namespace_absent) is not bool
        or any(type(getattr(result, name)) is not tuple for name in (
            "storage", "remaining_volumes", "unresolved_claims",
        ))
    ):
        _conflict()
    for name in StoreJobClaim.__dataclass_fields__:
        if name not in ("checked_at", "lease_expires_at"):
            expected, value = getattr(claim, name), getattr(result.claim, name)
            if type(value) is not type(expected) or value != expected:
                _conflict()
    uid = result.namespace_uid
    if uid is None:
        if (
            not result.namespace_absent or result.storage != ()
            or result.remaining_volumes != () or result.unresolved_claims != ()
        ):
            _conflict()
        return ()
    if not _opaque(uid):
        _conflict()
    identity = namespace_for(claim)
    try:
        saved = _known_records(identity, uid, result.storage)
        remaining = _known_records(identity, uid, result.remaining_volumes)
    except (ValueError, TypeError, AttributeError):
        _conflict()
    by_name = {item.claim_name: item for item in saved}
    full = {item.claim_name for item in saved if type(item) is StoreVolumeBinding}
    unresolved = tuple(name for name in STORE_CLAIM_NAMES if name not in full)
    if (
        type(result.unresolved_claims) is not tuple
        or result.unresolved_claims != unresolved
        or any(
            type(item) is not StoreVolumeBinding or by_name.get(item.claim_name) != item
            for item in remaining
        )
    ):
        _conflict()
    if not result.namespace_absent or remaining or unresolved:
        return None
    return tuple(by_name[name] for name in STORE_CLAIM_NAMES)


async def _check_saved(session, store, operation, claim, result, saved):
    if (
        store.id != claim.store_id or store.owner_id != claim.owner_id
        or store.name != claim.name or store.slug != claim.slug
        or store.url != claim.url or operation.deadline_at != claim.deadline_at
        or store.namespace_uid != result.namespace_uid
        or (result.namespace_uid is None and (
            store.credentials_secret_uid is not None
            or store.mcp_server_id is not None
            or store.analytics_mcp_server_id is not None
        ))
    ):
        _conflict()
    rows = list((await session.scalars(
        select(StoreVolume).where(StoreVolume.store_id == store.id)
        .order_by(StoreVolume.claim_name).limit(3).with_for_update()
        .execution_options(populate_existing=True)
    )).all())
    try:
        actual = tuple(
            _saved_record(row, namespace_for(claim), result.namespace_uid)
            for row in rows
        )
    except (ValueError, TypeError, AttributeError):
        _conflict()
    if actual != saved:
        _conflict()


async def finish_store_cleanup(factory, claim, result):
    """Commit only complete private evidence; pending observations change nothing.

    This performs no external requests. The worker calls it after stopping lease
    renewal. Storage identities and historical audits remain as tombstones.
    """
    saved = _cleanup_evidence(claim, result)
    if saved is None:
        return False
    try:
        async with factory() as session:
            async with session.begin():
                locked = await _locked_claim(session, claim)
                if locked is None:
                    return False
                operation, store, _ = locked
                servers = await _registrations(
                    session, store, claim, require_both=False,
                )
                await _check_saved(session, store, operation, claim, result, saved)
                lease_until = operation.lease_expires_at
                now = await session.scalar(select(func.clock_timestamp()))
                if lease_until <= now:
                    return False
                if claim.action == "provision" and operation.deadline_at > now:
                    return False
                await _remove_registrations(session, store, servers)
                deleting = claim.action == "delete"
                message = None if deleting else FAILURE_REASONS["timeout"]
                store.status = "Deleted" if deleting else "Failed"
                store.status_reason = message
                if deleting:
                    store.deleted_at = now
                _finish(
                    session, operation, store, now,
                    status="Succeeded" if deleting else "Failed",
                    action="store.deleted" if deleting else "store.provision_failed",
                    error=message,
                )
                await session.flush()
                now = await session.scalar(select(func.clock_timestamp()))
                if lease_until <= now:
                    raise _CleanupExpired()
    except _CleanupExpired:
        return False
    return True


class StoreWorkerHandlers:
    """Shared connection configuration for run_store_worker(work, finish)."""

    def __init__(self, factory, connection, scope, *, admin_email):
        if (
            not callable(factory) or not isinstance(connection, KubernetesConnection)
            or not isinstance(scope, StoreClusterScope)
            or not isinstance(admin_email, str) or len(admin_email) > 254
            or not _EMAIL.fullmatch(admin_email)
        ):
            raise ValueError("Invalid private store worker configuration.")
        self._factory = factory
        self._connection = connection
        self._scope = scope
        self._email = admin_email

    @staticmethod
    def _mode(claim, for_cleanup):
        if (
            type(claim) is not StoreJobClaim or type(for_cleanup) is not bool
            or claim.action not in ("provision", "delete")
            or (claim.action == "delete" and not for_cleanup)
        ):
            raise ValueError("Invalid private store worker job or mode.")

    async def work(self, claim, *, for_cleanup=False):
        self._mode(claim, for_cleanup)
        if for_cleanup:
            checked = await require_claim(self._factory, claim, for_cleanup=True)
            if claim.action == "provision" and not checked.claim.deadline_reached:
                raise ValueError("Provisioning cleanup requires its original deadline.")
            return await cleanup_store_resources(
                self._factory, claim, self._connection, self._scope,
            )
        return await provision_store(
            self._factory, claim, self._connection, self._scope,
            admin_email=self._email,
        )

    async def finish(self, claim, result, *, for_cleanup=False):
        self._mode(claim, for_cleanup)
        if for_cleanup:
            return await finish_store_cleanup(self._factory, claim, result)
        if type(result) is not StoreProvisioningResult:
            raise ValueError("A private provisioning result is required.")
        return await finish_provisioning(self._factory, claim, result)
