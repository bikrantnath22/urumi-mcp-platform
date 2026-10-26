"""Private worker registration of the two MCP services for a managed store."""

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.models import MCPServer, SecurityEvent
from app.store_completion import StoreCompletionConflict, _registrations
from app.store_jobs import _locked_claim


@dataclass(frozen=True, slots=True)
class ManagedMCPDefinition:
    service: str
    slug: str
    name: str
    url: str


@dataclass(frozen=True, slots=True)
class StoreMCPRegistrations:
    woo_server_id: int
    woo_slug: str
    woo_url: str
    analytics_server_id: int
    analytics_slug: str
    analytics_url: str


class _RegistrationExpired(Exception):
    """Force transaction rollback when the clock expires after a write."""


def definitions_for(claim):
    """Derive exact internal endpoints and bounded, store-specific tool prefixes."""
    if (
        not isinstance(claim.store_id, UUID)
        or claim.namespace != f"urumi-store-{claim.store_id.hex}"
        or claim.release_name != "store"
        or not isinstance(claim.slug, str)
        or not 1 <= len(claim.slug) <= 32
        or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?", claim.slug) is None
        or not isinstance(claim.name, str)
        or not 1 <= len(claim.name) <= 100
        or not claim.name.strip()
    ):
        raise ValueError("Invalid managed store identity.")

    label = claim.slug[:8].rstrip("-")
    suffix = claim.store_id.hex[:12]
    return tuple(
        ManagedMCPDefinition(
            service=service,
            slug=f"{prefix}-{label}-{suffix}",
            name=f"{claim.name[:80]} / {title}",
            url=f"http://{service}.{claim.namespace}.svc.cluster.local:8000/mcp",
        )
        for service, prefix, title in (
            ("woo-mcp", "woo", "WooCommerce"),
            ("mcp-analytics", "analytics", "Analytics"),
        )
    )


def _snapshot(servers, definitions):
    woo, analytics = definitions
    by_url = {server.url: server for server in servers}
    return StoreMCPRegistrations(
        woo_server_id=by_url[woo.url].id,
        woo_slug=woo.slug,
        woo_url=woo.url,
        analytics_server_id=by_url[analytics.url].id,
        analytics_slug=analytics.slug,
        analytics_url=analytics.url,
    )


async def register_store_mcps(session_factory, claim):
    """Commit both disabled registrations together while the claim and deadline are valid."""
    if claim.action != "provision":
        raise ValueError("MCP registration requires a provision claim.")
    definitions = definitions_for(claim)

    try:
        async with session_factory() as session:
            async with session.begin():
                locked = await _locked_claim(session, claim)
                if locked is None:
                    return None
                operation, store, _ = locked
                if store.slug != claim.slug or store.name != claim.name:
                    raise StoreCompletionConflict("Managed store identity changed.")

                existing = await _registrations(
                    session, store, claim, require_both=False,
                )
                now = await session.scalar(select(func.clock_timestamp()))
                if operation.lease_expires_at <= now or operation.deadline_at <= now:
                    return None

                if existing:
                    if len(existing) != 2:
                        raise StoreCompletionConflict("Managed MCP registrations are incomplete.")
                    by_url = {server.url: server for server in existing}
                    for definition in definitions:
                        server = by_url.get(definition.url)
                        if (
                            server is None
                            or server.slug != definition.slug
                            or server.name != definition.name
                            or server.enabled
                            or server.status != "Disabled"
                            or server.timeout_s != 15
                            or server.auth_header_name is not None
                            or server.credential_encrypted is not None
                        ):
                            raise StoreCompletionConflict("Managed MCP registration identity changed.")
                    result = _snapshot(existing, definitions)
                else:
                    collisions = list((await session.scalars(
                        select(MCPServer).where(
                            or_(
                                MCPServer.slug.in_([item.slug for item in definitions]),
                                MCPServer.url.in_([item.url for item in definitions]),
                            )
                        )
                    )).all())
                    if collisions:
                        raise StoreCompletionConflict("A managed MCP name or endpoint is already in use.")

                    servers = [
                        MCPServer(
                            slug=item.slug, name=item.name, url=item.url,
                            enabled=False, timeout_s=15, status="Disabled",
                            status_reason="Store setup is in progress.",
                        )
                        for item in definitions
                    ]
                    session.add_all(servers)
                    await session.flush()
                    store.mcp_server_id = servers[0].id
                    store.analytics_mcp_server_id = servers[1].id
                    store.updated_at = now
                    session.add(SecurityEvent(
                        actor_id=None,
                        target_user_id=store.owner_id,
                        action="store.mcp_registered",
                        details={
                            "store_id": str(store.id),
                            "operation_id": str(operation.id),
                            "generation": operation.store_generation,
                            "woo_server_id": servers[0].id,
                            "analytics_server_id": servers[1].id,
                        },
                    ))
                    await session.flush()
                    result = _snapshot(servers, definitions)

                # Any lock/flush delay must not commit work after expiry.
                now = await session.scalar(select(func.clock_timestamp()))
                if operation.lease_expires_at <= now or operation.deadline_at <= now:
                    raise _RegistrationExpired()
    except _RegistrationExpired:
        return None
    except IntegrityError:
        raise StoreCompletionConflict("Managed MCP registration could not be saved.") from None

    return result
