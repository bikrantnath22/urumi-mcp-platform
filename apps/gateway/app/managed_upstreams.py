"""Authorize store-bound MCP connections without broadening the private URL policy."""

import re
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased

from app.database import Session
from app.managed_servers import is_managed_destination
from app.models import MCPServer, Store, StoreOperation
from app.store_jobs import StoreJobClaim
from app.store_registration import definitions_for
from app.upstream_security import (
    DestinationPolicy,
    UpstreamPolicyError,
    destination_policy,
)


MANAGED_URL = re.compile(
    r"http://(woo-mcp|mcp-analytics)\.urumi-store-([0-9a-f]{32})"
    r"\.svc\.cluster\.local:8000/mcp"
)
DENIED = "Managed store MCP endpoint is unavailable."


def _identity(value):
    match = MANAGED_URL.fullmatch(value)
    if match is None:
        raise UpstreamPolicyError(DENIED)
    service, identity = match.groups()
    return service, UUID(hex=identity)


def _authorization_query(service, identity, value, server_id, claim):
    link = (
        Store.mcp_server_id
        if service == "woo-mcp"
        else Store.analytics_mcp_server_id
    )
    other = aliased(Store)
    shared = (
        select(other.id)
        .where(
            other.id != Store.id,
            or_(
                other.mcp_server_id.in_((
                    Store.mcp_server_id, Store.analytics_mcp_server_id,
                )),
                other.analytics_mcp_server_id.in_((
                    Store.mcp_server_id, Store.analytics_mcp_server_id,
                )),
            ),
        )
        .correlate(Store)
        .exists()
    )
    statement = (
        select(Store, MCPServer)
        .select_from(Store)
        .join(MCPServer, MCPServer.id == link)
        .where(
            Store.id == identity,
            Store.namespace == f"urumi-store-{identity.hex}",
            Store.release_name == "store",
            Store.desired_state == "present",
            Store.deleted_at.is_(None),
            Store.mcp_server_id.is_not(None),
            Store.analytics_mcp_server_id.is_not(None),
            Store.mcp_server_id != Store.analytics_mcp_server_id,
            ~shared,
            MCPServer.id == server_id,
            MCPServer.url == value,
            MCPServer.timeout_s == 15,
            MCPServer.auth_header_name.is_(None),
            MCPServer.credential_encrypted.is_(None),
        )
    )
    if claim is None:
        return statement.where(
            Store.status == "Ready",
            Store.access_enabled.is_(True),
            MCPServer.enabled.is_(True),
        )

    if (
        not isinstance(claim, StoreJobClaim)
        or claim.action != "provision"
        or claim.store_id != identity
    ):
        raise UpstreamPolicyError(DENIED)

    return (
        statement.join(
            StoreOperation, StoreOperation.store_id == Store.id,
        )
        .where(
            Store.status == "Provisioning",
            Store.owner_id == claim.owner_id,
            Store.generation == claim.generation,
            Store.namespace == claim.namespace,
            Store.release_name == claim.release_name,
            Store.name == claim.name,
            Store.slug == claim.slug,
            Store.url == claim.url,
            MCPServer.enabled.is_(False),
            MCPServer.status == "Disabled",
            StoreOperation.id == claim.operation_id,
            StoreOperation.action == "provision",
            StoreOperation.store_generation == Store.generation,
            StoreOperation.status == "Running",
            StoreOperation.attempts == claim.attempt,
            StoreOperation.lease_token == claim.lease_token,
            StoreOperation.finished_at.is_(None),
            StoreOperation.deadline_at == claim.deadline_at,
            StoreOperation.deadline_at > func.clock_timestamp(),
            StoreOperation.lease_expires_at > func.clock_timestamp(),
        )
    )


async def authorize_managed_endpoint(
    value, *, server_id, session_factory=None, claim=None,
):
    """Recheck database bindings; release the connection before DNS or HTTP."""
    service, identity = _identity(value)
    if type(server_id) is not int or server_id < 1:
        raise UpstreamPolicyError(DENIED)
    statement = _authorization_query(
        service, identity, value, server_id, claim,
    )
    factory = Session if session_factory is None else session_factory
    try:
        async with factory() as session:
            row = (await session.execute(statement)).one_or_none()
            if row is None:
                raise UpstreamPolicyError(DENIED)
            store, server = row
            expected = next(
                definition
                for definition in definitions_for(SimpleNamespace(
                    store_id=store.id,
                    namespace=store.namespace,
                    release_name=store.release_name,
                    name=store.name,
                    slug=store.slug,
                ))
                if definition.service == service
            )
            if server.slug != expected.slug or server.name != expected.name:
                raise UpstreamPolicyError(DENIED)
    except SQLAlchemyError:
        raise UpstreamPolicyError(
            "Managed store authorization storage is unavailable.",
        ) from None
    except ValueError as error:
        if isinstance(error, UpstreamPolicyError):
            raise
        raise UpstreamPolicyError(DENIED) from None


async def prepare_destination(
    value, *, server_id=None, policy=destination_policy, session_factory=None,
    claim=None, resolver=None,
):
    """Use a per-connection exact exception after database authorization."""
    if not is_managed_destination(value):
        if claim is not None:
            raise UpstreamPolicyError(DENIED)
        return await policy.prepare(value, resolver=resolver)

    await authorize_managed_endpoint(
        value, server_id=server_id, session_factory=session_factory, claim=claim,
    )
    scoped_policy = DestinationPolicy(
        internal_urls=(value,),
        internal_cidrs=tuple(str(network) for network in policy.internal_networks),
    )
    destination = await scoped_policy.prepare(value, resolver=resolver)

    # Recheck changes or lease expiry that occurred during DNS resolution.
    # Authorization is never stored in the catalogue cache.
    await authorize_managed_endpoint(
        value, server_id=server_id, session_factory=session_factory, claim=claim,
    )
    return destination
