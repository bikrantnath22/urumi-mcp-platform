"""Keep store-generated MCP identities under the store lifecycle."""

import re

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.models import MCPServer, Store
from app.store_settings import lock_store_admission
from app.upstream_security import UpstreamPolicyError, parse_endpoint


RESERVED_SLUG = re.compile(
    r"(?:woo|analytics)-[a-z0-9][a-z0-9-]{0,7}-[0-9a-f]{12}"
)
MANAGED_HOST = re.compile(
    r"[^.]+\.urumi-store-[0-9a-f]{32}(?:\.svc(?:\.cluster\.local)?)?"
)


def is_managed_destination(url):
    try:
        host = parse_endpoint(url).host
    except UpstreamPolicyError:
        return False
    return MANAGED_HOST.fullmatch(host) is not None


def reject_reserved_identity(slug, url):
    if RESERVED_SLUG.fullmatch(slug) or is_managed_destination(url):
        raise HTTPException(
            409,
            "This MCP name or URL is reserved for store provisioning.",
        )


async def lock_manual_registry(session):
    try:
        await lock_store_admission(session)
    except SQLAlchemyError:
        await session.rollback()
        raise HTTPException(
            503, "Server settings storage is unavailable.",
        ) from None


async def load_manual_server(session, server_id, *, lock):
    """Use lock=True before any mutation; preliminary reads use lock=False."""
    try:
        if lock:
            await lock_manual_registry(session)

        store_id = await session.scalar(
            select(Store.id).where(or_(
                Store.mcp_server_id == server_id,
                Store.analytics_mcp_server_id == server_id,
            )).limit(1)
        )
        if store_id is not None:
            raise HTTPException(
                409,
                "This MCP server is managed by a store. Use Stores to manage it.",
            )

        statement = select(MCPServer).where(MCPServer.id == server_id)
        if lock:
            statement = statement.with_for_update()
        server = await session.scalar(
            statement.execution_options(populate_existing=True)
        )
        if server is None:
            raise HTTPException(404, "Server not found.")
        if is_managed_destination(server.url):
            raise HTTPException(
                409,
                "This MCP server is managed by a store. Use Stores to manage it.",
            )
        return server
    except SQLAlchemyError:
        await session.rollback()
        raise HTTPException(
            503, "Server settings storage is unavailable.",
        ) from None
