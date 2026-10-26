import asyncio
import logging
from datetime import datetime, timezone
from time import perf_counter

from mcp.types import CallToolResult, TextContent, Tool
from sqlalchemy import select, update

from app.rate_limits import RateLimitExceeded, enforce_tool
from app.database import Session
from app.observability import log as telemetry
from app.catalog_cache import catalog_cache, catalog_identity
from app.models import AuditLog, MCPServer, ToolGrant, User
from app.managed_servers import is_managed_destination
from app.managed_upstreams import authorize_managed_endpoint
from app.upstream_security import UpstreamPolicyError
from app.registry import upstream_headers
from app.upstreams import UpstreamError, call_tool, discover_tools


logger = logging.getLogger(__name__)
discovery_gate = asyncio.Semaphore(8)


def error_result(message: str) -> CallToolResult:
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=message)],
    )


async def mark_server(
    server: MCPServer,
    status: str,
    reason: str | None,
) -> bool:
    async with Session() as session:
        result = await session.execute(
            update(MCPServer)
            .where(
                MCPServer.id == server.id,
                MCPServer.slug == server.slug,
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
            .returning(MCPServer.id)
        )
        updated_id = result.scalar_one_or_none()
        await session.commit()
        return updated_id is not None


async def discover_one(server: MCPServer) -> list[Tool]:
    async def load():
        async with discovery_gate:
            return await discover_tools(
                server.url,
                server_id=server.id,
                headers=upstream_headers(server),
                timeout_s=min(3.0, server.timeout_s),
            )

    try:
        # Recheck store authorization even when tool definitions are cached.
        if is_managed_destination(server.url):
            await authorize_managed_endpoint(server.url, server_id=server.id)
        lookup = await catalog_cache.get(server, load)
        qualified = []
        for tool in lookup.tools:
            name = f"{server.slug}__{tool.name}"
            if len(name) > 128:
                raise UpstreamError("An upstream tool name exceeds the gateway limit.")
            qualified.append(tool.model_copy(update={
                "name": name,
                "description": f"[{server.name}] {tool.description or tool.name}",
            }))
        # A cache hit is not a new health check. Preserve last_checked_at.
        if lookup.state != "hit":
            unchanged = await mark_server(server, "Connected", None)
            if not unchanged:
                catalog_cache.invalidate(server.id)
                return []
        return qualified
    except (UpstreamError, UpstreamPolicyError) as exc:
        catalog_cache.invalidate(server.id)
        await mark_server(server, "Unreachable", str(exc))
        return []


async def list_gateway_tools(user: User) -> list[Tool]:
    # Use database identity, never client-supplied role or permissions.
    async with Session() as session:
        current = await session.get(User, user.id)
        if (not current or not current.active or not current.password_hash
            or current.session_version != (getattr(user, "session_version", 0) or 0)):
            return []
        query = select(MCPServer).where(MCPServer.enabled.is_(True))
        if current.role != "admin":
            permitted_servers = select(ToolGrant.server_id).where(
                ToolGrant.user_id == current.id
            )
            query = query.where(MCPServer.id.in_(permitted_servers))
        servers = list(await session.scalars(query.order_by(MCPServer.id)))

    results = await asyncio.gather(
        *(discover_one(server) for server in servers),
        return_exceptions=True,
    )

    # Recheck after discovery so a concurrent revoke removes advertised tools.
    async with Session() as session:
        current = await session.get(User, user.id)
        if (not current or not current.active or not current.password_hash
            or current.session_version != (getattr(user, "session_version", 0) or 0)):
            return []
        is_admin = current.role == "admin"
        latest_servers = {
            row.id: catalog_identity(row)
            for row in await session.scalars(
                select(MCPServer).where(MCPServer.enabled.is_(True),
                                        MCPServer.id.in_([server.id for server in servers]))
            )
        }
        permitted = set()
        if not is_admin:
            permitted = {
                (server_id, tool_name)
                for server_id, tool_name in (
                    await session.execute(
                        select(ToolGrant.server_id, ToolGrant.tool_name)
                        .where(ToolGrant.user_id == current.id)
                    )
                ).all()
            }

    tools = []
    for server, result in zip(servers, results):
        if isinstance(result, BaseException):
            logger.warning(
                "Discovery failed for %s (%s)",
                server.slug,
                type(result).__name__,
            )
        elif latest_servers.get(server.id) == catalog_identity(server):
            tools.extend(
                tool for tool in result
                if is_admin
                or (server.id, tool.name.partition("__")[2]) in permitted
            )
    return sorted(tools, key=lambda tool: tool.name)


async def finish_audit(
    audit_id: int,
    started: float,
    outcome: str,
    error: str | None,
):
    duration_ms = max(0, int((perf_counter() - started) * 1000))

    async with Session() as session:
        await session.execute(
            update(AuditLog)
            .where(AuditLog.id == audit_id)
            .values(
                duration_ms=duration_ms,
                outcome=outcome,
                error=error,
            )
        )
        await session.commit()
    telemetry.info("tool_call_finished", extra={"audit_id": audit_id, "outcome": outcome, "duration_ms": duration_ms})


async def execute_gateway_tool(
    user: User,
    tool_name: str,
    arguments: dict,
    *,
    source: str,
) -> CallToolResult:
    if source not in {"mcp", "chat", "dashboard"}:
        raise ValueError("Invalid audit source.")

    started = perf_counter()
    limit_error = None
    try:
        await enforce_tool(user.id)
    except RateLimitExceeded as exc:
        limit_error = exc
    slug, separator, upstream_name = tool_name.partition("__")
    denied_reason = None

    async with Session() as session:
        current = await session.get(User, user.id)
        server = None

        if (not current or not current.active or not current.password_hash
            or current.session_version != (getattr(user, "session_version", 0) or 0)):
            denied_reason = "Account is unavailable."
        elif (
            not separator
            or not slug
            or not upstream_name
            or len(tool_name) > 128
        ):
            denied_reason = "Use a tool name in server__tool format."
        elif not isinstance(arguments, dict):
            denied_reason = "Tool arguments must be an object."
        else:
            server = await session.scalar(
                select(MCPServer).where(MCPServer.slug == slug)
            )
            if server is None:
                denied_reason = "Tool server was not found."
            elif not server.enabled:
                denied_reason = "Tool server is disabled."
            elif current.role != "admin":
                grant = await session.scalar(
                    select(ToolGrant.user_id).where(
                        ToolGrant.user_id == current.id,
                        ToolGrant.server_id == server.id,
                        ToolGrant.tool_name == upstream_name,
                    )
                )
                if grant is None:
                    denied_reason = "You do not have permission to use this tool."

        if limit_error is not None:
            denied_reason = str(limit_error)

        audit = AuditLog(
            user_id=current.id if current else None,
            server_id=server.id if server else None,
            server_slug=slug[:32] if separator else None,
            tool=tool_name[:256],
            source=source,
            duration_ms=0,
            outcome="denied" if denied_reason else "error",
            error=denied_reason or (
                "Execution started; a final outcome has not been recorded."
            ),
        )
        session.add(audit)
        # Persist the call record before invoking a potentially modifying tool.
        await session.commit()
        audit_id = audit.id

    telemetry.info("tool_call_recorded", extra={"audit_id": audit_id, "server_id": audit.server_id})
    if denied_reason:
        await finish_audit(audit_id, started, "denied", denied_reason)
        if limit_error is not None:
            raise limit_error
        return error_result(denied_reason)

    outcome = "error"
    audit_error = "Execution ended before a final outcome was recorded."

    try:
        try:
            result = await call_tool(
                server.url,
                upstream_name,
                arguments,
                server_id=server.id,
                headers=upstream_headers(server),
                timeout_s=server.timeout_s,
            )
        except UpstreamError as exc:
            catalog_cache.invalidate(server.id)
            audit_error = str(exc)
            await mark_server(server, "Unreachable", audit_error)
            return error_result(
                f"{audit_error} Execution may have occurred; "
                "verify the outcome before retrying."
            )

        await mark_server(server, "Connected", None)

        if result.isError:
            audit_error = "Upstream tool reported an error."
        else:
            outcome = "success"
            audit_error = None

        return result
    finally:
        await finish_audit(audit_id, started, outcome, audit_error)
