import os
from datetime import datetime, timezone

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.database import Session
from app.catalog_cache import catalog_cache
from app.models import MCPServer, RegistryBootstrap
from app.security import decrypt_credential
from app.upstreams import UpstreamError, discover_tools


def public_server(server: MCPServer) -> dict:
    return {
        "id": server.id,
        "slug": server.slug,
        "name": server.name,
        "url": server.url,
        "enabled": server.enabled,
        "timeout_s": server.timeout_s,
        "status": server.status,
        "status_reason": server.status_reason,
        "last_checked_at": server.last_checked_at,
        "auth_header_name": server.auth_header_name,
        "has_credential": bool(server.credential_encrypted),
    }


def upstream_headers(server: MCPServer) -> dict[str, str]:
    if not server.credential_encrypted:
        return {}

    if not server.auth_header_name:
        raise UpstreamError("Upstream credential header is missing.")

    try:
        value = decrypt_credential(server.credential_encrypted)
    except Exception:
        raise UpstreamError(
            "Upstream credential could not be decrypted."
        ) from None

    return {server.auth_header_name: value}


async def bootstrap_servers() -> None:
    defaults = (
        (
            "woo",
            "WooCommerce",
            os.getenv(
                "WOO_MCP_URL",
                "http://woo-mcp.store.svc.cluster.local:8000/mcp",
            ),
        ),
        (
            "analytics",
            "Sales Analytics",
            os.getenv(
                "ANALYTICS_MCP_URL",
                "http://mcp-analytics.store.svc.cluster.local:8000/mcp",
            ),
        ),
        (
            "notes",
            "Team Notes",
            os.getenv(
                "NOTES_MCP_URL",
                "http://mcp-notes.platform.svc.cluster.local:8000/mcp",
            ),
        ),
    )

    async with Session() as session:
        # Serialize initial setup across gateway processes.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(714202601)")
        )

        if await session.get(RegistryBootstrap, 1) is not None:
            return

        for slug, name, url in defaults:
            await session.execute(
                insert(MCPServer)
                .values(
                    slug=slug,
                    name=name,
                    url=url,
                    enabled=True,
                    timeout_s=15.0,
                    status="Unreachable",
                    status_reason="Not checked yet.",
                )
                .on_conflict_do_nothing(index_elements=["slug"])
            )

        session.add(RegistryBootstrap(id=1))
        await session.commit()


async def list_servers() -> list[dict]:
    async with Session() as session:
        servers = await session.scalars(
            select(MCPServer).order_by(MCPServer.id)
        )
        return [public_server(server) for server in servers]


async def refresh_server(server_id: int) -> dict | None:
    catalog_cache.invalidate(server_id)
    # Release the database connection before waiting on the upstream.
    async with Session() as session:
        server = await session.get(MCPServer, server_id)

    if server is None:
        return None

    if not server.enabled:
        return {
            "server": {
                **public_server(server),
                "status": "Disabled",
                "status_reason": None,
            },
            "tools": [],
        }

    try:
        async def load():
            return await discover_tools(
                server.url,
                server_id=server.id,
                headers=upstream_headers(server),
                timeout_s=server.timeout_s,
            )
        lookup = await catalog_cache.get(server, load, refresh=True)
        tools = lookup.tools
        status = "Connected"
        reason = None
    except UpstreamError as exc:
        catalog_cache.invalidate(server_id)
        tools = []
        status = "Unreachable"
        reason = str(exc)

    async with Session() as session:
        # An older check must not overwrite a concurrent admin change.
        result = await session.execute(
            update(MCPServer)
            .where(
                MCPServer.id == server.id,
                MCPServer.enabled.is_(True),
                MCPServer.url == server.url,
                MCPServer.timeout_s == server.timeout_s,
                MCPServer.auth_header_name == server.auth_header_name,
                MCPServer.credential_encrypted
                == server.credential_encrypted,
            )
            .values(
                status=status,
                status_reason=reason,
                last_checked_at=datetime.now(timezone.utc),
            )
            .returning(MCPServer)
        )
        updated = result.scalar_one_or_none()
        await session.commit()

        if updated is None:
            catalog_cache.invalidate(server_id)
            current = await session.get(MCPServer, server_id)
            if current is None:
                return None
            return {"server": public_server(current), "tools": []}

        return {
            "server": public_server(updated),
            "tools": [
                tool.model_dump(mode="json", by_alias=True)
                for tool in tools
            ],
        }
