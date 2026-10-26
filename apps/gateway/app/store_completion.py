"""Private worker completion transactions; external checks happen beforehand."""

from sqlalchemy import delete, func, or_, select

from app.models import MCPServer, SecurityEvent, Store, StoreVolume
from app.store_jobs import _locked_claim


FAILURE_REASONS = {
    "timeout": "Store creation exceeded its provisioning deadline.",
    "bootstrap": "Store setup could not finish.",
    "verification": "Store verification could not finish.",
    "provisioning": "Store creation could not finish.",
}


class StoreCompletionConflict(RuntimeError):
    pass


def _check_action(claim, expected):
    if claim.action != expected:
        raise ValueError(f"This completion requires a {expected} claim.")


async def _registrations(session, store, claim, *, require_both):
    expected_namespace = f"urumi-store-{store.id.hex}"
    if (
        store.namespace != expected_namespace
        or claim.namespace != expected_namespace
        or store.release_name != "store"
        or claim.release_name != "store"
        or store.owner_id != claim.owner_id
        or store.url != claim.url
    ):
        raise StoreCompletionConflict("Managed store identity changed.")

    bindings = {
        "woo-mcp": store.mcp_server_id,
        "mcp-analytics": store.analytics_mcp_server_id,
    }
    ids = [identity for identity in bindings.values() if identity is not None]
    if (
        (require_both and len(ids) != 2)
        or len(ids) != len(set(ids))
        or any(type(identity) is not int or identity <= 0 for identity in ids)
    ):
        raise StoreCompletionConflict("Managed MCP registrations are incomplete.")

    if not ids:
        return []

    other_store = await session.scalar(
        select(Store.id)
        .where(
            Store.id != store.id,
            or_(
                Store.mcp_server_id.in_(ids),
                Store.analytics_mcp_server_id.in_(ids),
            ),
        )
        .limit(1)
    )
    if other_store is not None:
        raise StoreCompletionConflict("An MCP registration belongs to another store.")

    servers = list((
        await session.scalars(
            select(MCPServer)
            .where(MCPServer.id.in_(ids))
            .order_by(MCPServer.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all())
    by_id = {server.id: server for server in servers}
    if set(by_id) != set(ids):
        raise StoreCompletionConflict("Managed MCP registrations are unavailable.")

    for service, identity in bindings.items():
        if identity is None:
            continue
        expected_url = (
            f"http://{service}.{expected_namespace}.svc.cluster.local:8000/mcp"
        )
        if by_id[identity].url != expected_url:
            raise StoreCompletionConflict("Managed MCP registration identity changed.")

    return servers


def _finish(session, operation, store, now, *, status, action, error=None):
    operation.status = status
    operation.finished_at = now
    operation.updated_at = now
    operation.lease_token = None
    operation.lease_expires_at = None
    operation.last_error = error
    store.updated_at = now
    session.add(SecurityEvent(
        actor_id=None,
        target_user_id=store.owner_id,
        action=action,
        details={
            "store_id": str(store.id),
            "operation_id": str(operation.id),
            "generation": operation.store_generation,
            "requested_by_id": operation.requested_by_id,
        },
    ))


async def _remove_registrations(session, store, servers):
    store.mcp_server_id = None
    store.analytics_mcp_server_id = None
    await session.flush()
    if servers:
        await session.execute(
            delete(MCPServer)
            .where(MCPServer.id.in_([server.id for server in servers]))
            .execution_options(synchronize_session=False)
        )


async def finish_ready(session_factory, claim, *, verified=None):
    """Trusted private completion; the worker supplies its verified result."""
    _check_action(claim, "provision")
    definitions = _ready_result(claim, verified)
    volumes = _ready_volumes(claim, verified)
    try:
        async with session_factory() as session:
            async with session.begin():
                locked = await _locked_claim(session, claim)
                if locked is None:
                    return False
                operation, store, _ = locked
                servers = await _registrations(
                    session, store, claim, require_both=True,
                )
                if verified is not None:
                    _check_ready_snapshot(
                        operation, store, claim, servers, verified, definitions,
                    )
                    await _check_ready_volumes(session, store, volumes)
                lease_until, deadline = operation.lease_expires_at, operation.deadline_at
                now = await session.scalar(select(func.clock_timestamp()))
                if lease_until <= now or deadline <= now:
                    return False

                for server in servers:
                    server.enabled = True
                    server.status = "Connected"
                    server.status_reason = None
                    server.last_checked_at = now
                store.access_guarded = verified is not None and verified.access_guarded is True
                store.status = "Ready"
                store.status_reason = None
                _finish(
                    session, operation, store, now,
                    status="Succeeded", action="store.provisioned",
                )
                await session.flush()
                now = await session.scalar(select(func.clock_timestamp()))
                if lease_until <= now or deadline <= now:
                    raise _ReadyExpired()
    except _ReadyExpired:
        return False
    return True


async def finish_failed(session_factory, claim, *, reason):
    """Call only after partial store resources and volumes are confirmed removed."""
    _check_action(claim, "provision")
    if type(reason) is not str or reason not in FAILURE_REASONS:
        raise ValueError("Use a recognized store failure reason.")
    message = FAILURE_REASONS[reason]

    async with session_factory() as session:
        async with session.begin():
            locked = await _locked_claim(session, claim)
            if locked is None:
                return False
            operation, store, _ = locked
            servers = await _registrations(
                session, store, claim, require_both=False,
            )
            now = await session.scalar(select(func.clock_timestamp()))
            if operation.lease_expires_at <= now:
                return False
            if reason == "timeout" and operation.deadline_at > now:
                raise StoreCompletionConflict("The provisioning deadline has not elapsed.")

            await _remove_registrations(session, store, servers)
            store.status = "Failed"
            store.status_reason = message
            _finish(
                session, operation, store, now,
                status="Failed", action="store.provision_failed", error=message,
            )
            await session.flush()
    return True


async def finish_deleted(session_factory, claim):
    """Call only after the managed namespace, resources and volumes are gone."""
    _check_action(claim, "delete")
    async with session_factory() as session:
        async with session.begin():
            locked = await _locked_claim(session, claim)
            if locked is None:
                return False
            operation, store, _ = locked
            servers = await _registrations(
                session, store, claim, require_both=False,
            )
            now = await session.scalar(select(func.clock_timestamp()))
            if operation.lease_expires_at <= now:
                return False

            await _remove_registrations(session, store, servers)
            store.status = "Deleted"
            store.status_reason = None
            store.deleted_at = now
            _finish(
                session, operation, store, now,
                status="Succeeded", action="store.deleted",
            )
            await session.flush()
    return True

class _ReadyExpired(Exception):
    """Rollback terminal writes if their original allowance elapses."""


def _ready_result(claim, verified):
    if verified is None:
        return None
    # Local imports avoid a cycle with registration and provisioning modules.
    from app.store_jobs import StoreJobClaim
    from app.store_namespace import _opaque
    from app.store_provisioning import StoreProvisioningResult
    from app.store_registration import StoreMCPRegistrations, definitions_for

    if (
        not isinstance(claim, StoreJobClaim)
        or not isinstance(verified, StoreProvisioningResult)
        or not isinstance(verified.claim, StoreJobClaim)
        or not _opaque(verified.namespace_uid)
        or not _opaque(verified.credentials_secret_uid)
        or not isinstance(verified.registrations, StoreMCPRegistrations)
        or verified.access_guarded is not True
    ):
        raise StoreCompletionConflict("A valid provisioning result is required.")
    for name in StoreJobClaim.__dataclass_fields__:
        if name in ("checked_at", "lease_expires_at"):
            continue
        expected, value = getattr(claim, name), getattr(verified.claim, name)
        if type(value) is not type(expected) or value != expected:
            raise StoreCompletionConflict("The provisioning result belongs to another job.")
    registrations = verified.registrations
    ids = (registrations.woo_server_id, registrations.analytics_server_id)
    if any(type(value) is not int or value < 1 for value in ids) or ids[0] == ids[1]:
        raise StoreCompletionConflict("Verified MCP identities are invalid.")
    definitions = definitions_for(claim)
    for definition, slug, url in zip(
        definitions,
        (registrations.woo_slug, registrations.analytics_slug),
        (registrations.woo_url, registrations.analytics_url),
    ):
        if definition.slug != slug or definition.url != url:
            raise StoreCompletionConflict("Verified MCP identities changed.")
    return definitions


def _check_ready_snapshot(operation, store, claim, servers, verified, definitions):
    registrations = verified.registrations
    if (
        store.name != claim.name or store.slug != claim.slug
        or operation.deadline_at != claim.deadline_at
        or store.access_enabled is not True
        or store.namespace_uid != verified.namespace_uid
        or store.credentials_secret_uid != verified.credentials_secret_uid
        or store.mcp_server_id != registrations.woo_server_id
        or store.analytics_mcp_server_id != registrations.analytics_server_id
    ):
        raise StoreCompletionConflict("Verified store ownership changed.")
    by_id = {server.id: server for server in servers}
    for definition, identity in zip(
        definitions, (registrations.woo_server_id, registrations.analytics_server_id),
    ):
        server = by_id.get(identity)
        if (
            server is None or server.slug != definition.slug
            or server.name != definition.name or server.url != definition.url
            or server.enabled or server.status != "Disabled"
            or server.timeout_s != 15 or server.auth_header_name is not None
            or server.credential_encrypted is not None
        ):
            raise StoreCompletionConflict("Verified MCP registration changed.")


def _ready_volumes(claim, verified):
    if verified is None:
        return None
    from app.store_namespace import namespace_for
    from app.store_provisioning import (
        StoreProvisioningVerificationError, _volume_bindings,
    )
    from app.store_volumes import StoreVolumeBinding

    try:
        values = _volume_bindings(
            verified.volumes, namespace_for(claim), verified.namespace_uid,
        )
        # Copy and revalidate the frozen values before any database await.
        fields = StoreVolumeBinding.__dataclass_fields__
        values = tuple(
            StoreVolumeBinding(**{
                name: getattr(item, name) for name in fields
            }) for item in values
        )
        for name in ("claim_uid", "volume_name", "volume_uid"):
            if len({getattr(item, name) for item in values}) != len(values):
                raise ValueError("Store storage identities must be distinct.")
    except (
        StoreProvisioningVerificationError, ValueError, TypeError, AttributeError,
    ):
        raise StoreCompletionConflict(
            "Both verified store volume identities are required.",
        ) from None
    return values


async def _check_ready_volumes(session, store, volumes):
    rows = list((await session.scalars(
        select(StoreVolume)
        .where(StoreVolume.store_id == store.id)
        .order_by(StoreVolume.claim_name)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).all())
    by_name = {row.claim_name: row for row in rows}
    if len(rows) != 2 or set(by_name) != {item.claim_name for item in volumes}:
        raise StoreCompletionConflict("Saved store volume identities are incomplete.")
    for item in volumes:
        row = by_name[item.claim_name]
        if any(
            getattr(row, name) != getattr(item, name)
            for name in (
                "namespace_uid", "claim_uid", "volume_name", "volume_uid", "storage_class",
            )
        ):
            raise StoreCompletionConflict("Verified store volume identity changed.")
