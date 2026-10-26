import asyncio
import json
import logging
from time import perf_counter
from urllib.parse import urlsplit

from fastapi import HTTPException
from mcp import types
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import (
    TransportSecurityMiddleware,
    TransportSecuritySettings,
)
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.rate_limits import RateLimitExceeded, LimitsUnavailable
from app.auth import authenticate_api_key
from app.core import error_result, execute_gateway_tool, list_gateway_tools
from app.database import Session
from app.models import APIKey, AuditLog, MCPServer, User
from app.security import APP_ORIGIN, token_hash


logger = logging.getLogger(__name__)

mcp_server = Server(
    "Urumi Gateway",
    version="0.1.0",
    instructions="Tool names use server__tool. Calls execute as the API-key owner.",
)

security_settings = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        "127.0.0.1",
        "127.0.0.1:*",
        "localhost",
        "localhost:*",
        "[::1]",
        "[::1]:*",
        urlsplit(APP_ORIGIN).netloc,
    ],
    allowed_origins=[APP_ORIGIN],
)
transport_security = TransportSecurityMiddleware(security_settings)

mcp_sessions = StreamableHTTPSessionManager(
    app=mcp_server,
    stateless=True,
    json_response=True,
    security_settings=security_settings,
    max_request_body_size=4 * 1024 * 1024,
)


def request_user() -> User:
    request = mcp_server.request_context.request
    user = request.scope.get("urumi_user") if request is not None else None
    if not isinstance(user, User):
        raise RuntimeError("Authenticated user context is unavailable.")
    return user


@mcp_server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return await list_gateway_tools(request_user())


async def handle_call_tool(
    request: types.CallToolRequest,
) -> types.ServerResult:
    try:
        result = await execute_gateway_tool(
            request_user(),
            request.params.name,
            request.params.arguments
            if request.params.arguments is not None
            else {},
            source="mcp",
        )
    except RateLimitExceeded as exc:
        result = types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=f"{exc} Try again after {exc.retry_after} seconds. This tool was not executed.")],
            structuredContent={"rate_limited": True, "retry_after_s": exc.retry_after, "executed": False})
    except LimitsUnavailable:
        result = error_result("Usage-limit storage is unavailable. This tool was not executed.")
    except Exception as exc:
        logger.error("Gateway execution failed (%s)", type(exc).__name__)
        result = error_result(
            "Gateway execution failed. Execution may have occurred; "
            "check the audit log before retrying."
        )
    return types.ServerResult(result)


# Dispatch directly to the audited core. Upstreams validate tool arguments.
# Avoid automatic discovery of every server before each uncached tool call.
mcp_server.request_handlers[types.CallToolRequest] = handle_call_tool


async def audit_denied_request(
    request: Request,
    raw_key: str,
    reason: str,
):
    if request.method != "POST":
        return

    started = perf_counter()
    try:
        body = bytearray()
        async with asyncio.timeout(1):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 4 * 1024 * 1024:
                    return

        message = json.loads(body)
        if not isinstance(message, dict) or message.get("method") != "tools/call":
            return

        params = message.get("params")
        if not isinstance(params, dict):
            return

        name = params.get("name")
        if not isinstance(name, str):
            return

        slug, separator, _ = name.partition("__")

        async with Session() as session:
            user_id = None
            if raw_key.startswith("urm_") and len(raw_key) <= 200:
                user_id = await session.scalar(
                    select(APIKey.user_id).where(
                        APIKey.key_hash == token_hash(raw_key)
                    )
                )

            server_id = None
            if separator:
                server_id = await session.scalar(
                    select(MCPServer.id).where(MCPServer.slug == slug)
                )

            session.add(
                AuditLog(
                    user_id=user_id,
                    server_id=server_id,
                    server_slug=slug[:32] if separator else None,
                    tool=name[:256],
                    source="mcp",
                    outcome="denied",
                    duration_ms=max(
                        0, int((perf_counter() - started) * 1000)
                    ),
                    error=reason,
                )
            )
            await session.commit()
    except Exception as exc:
        logger.warning(
            "Denied-request audit failed (%s)", type(exc).__name__
        )


class GatewayMCP:
    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)

        invalid_transport = await transport_security.validate_request(
            request,
            is_post=request.method == "POST",
        )
        if invalid_transport is not None:
            await invalid_transport(scope, receive, send)
            return

        scheme, separator, raw_key = request.headers.get(
            "authorization", ""
        ).partition(" ")
        raw_key = raw_key.strip()

        try:
            if not separator or scheme.lower() != "bearer":
                raise HTTPException(401, "A Bearer API key is required.")

            user = await authenticate_api_key(raw_key)
        except HTTPException as exc:
            await audit_denied_request(request, raw_key, str(exc.detail))
            response = JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        except Exception as exc:
            logger.error(
                "MCP authentication failed (%s)", type(exc).__name__
            )
            response = JSONResponse(
                {"detail": "Authentication service is unavailable."},
                status_code=503,
            )
            await response(scope, receive, send)
            return

        scope["urumi_user"] = user
        await mcp_sessions.handle_request(scope, receive, send)


mcp_app = GatewayMCP()
