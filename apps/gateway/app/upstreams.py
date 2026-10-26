import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, Tool

from app.managed_upstreams import prepare_destination
from app.upstream_security import (
    PinnedUpstreamTransport,
    UpstreamPolicyError,
    destination_policy,
    policy_failure,
)


class UpstreamError(Exception):
    """Safe error message that can be returned to gateway clients."""


@asynccontextmanager
async def upstream_session(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout_s: float = 15,
    server_id: int | None = None,
    store_claim=None,
):
    if not 1 <= timeout_s <= 60:
        raise ValueError("Upstream timeout must be between 1 and 60 seconds.")

    try:
        async with asyncio.timeout(timeout_s):
            destination = await prepare_destination(
                url,
                server_id=server_id,
                claim=store_claim,
                policy=destination_policy,
            )
            async with httpx.AsyncClient(
                transport=PinnedUpstreamTransport(destination),
                headers=headers or {},
                timeout=httpx.Timeout(
                    timeout_s,
                    connect=min(5, timeout_s),
                ),
                follow_redirects=False,
                trust_env=False,
            ) as http_client:
                async with streamable_http_client(
                    url,
                    http_client=http_client,
                ) as (read_stream, write_stream, _):
                    async with ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(seconds=timeout_s),
                    ) as session:
                        await session.initialize()
                        yield session
    except TimeoutError:
        raise UpstreamError(
            f"Upstream request exceeded {timeout_s:g} seconds."
        ) from None
    except UpstreamError:
        raise
    except UpstreamPolicyError as error:
        raise UpstreamError(str(error)) from None
    except Exception as error:
        # Avoid exposing credentials, URLs, or internal exception details.
        raise UpstreamError(
            policy_failure(error) or "Upstream connection or MCP request failed."
        ) from None


async def discover_tools(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout_s: float = 15,
    server_id: int | None = None,
    store_claim=None,
) -> list[Tool]:
    async with upstream_session(
        url,
        headers=headers,
        timeout_s=timeout_s,
        server_id=server_id,
        store_claim=store_claim,
    ) as session:
        tools: list[Tool] = []
        seen_names: set[str] = set()
        seen_cursors: set[str] = set()
        cursor = None

        for _ in range(20):
            page = await session.list_tools(cursor=cursor)

            for tool in page.tools:
                if tool.name in seen_names:
                    raise UpstreamError(
                        "Upstream returned duplicate tool names."
                    )
                seen_names.add(tool.name)
                tools.append(tool)

            if len(tools) > 1000:
                raise UpstreamError(
                    "Upstream exceeded the 1000-tool discovery limit."
                )

            cursor = page.nextCursor
            if cursor is None:
                return tools

            if cursor in seen_cursors:
                raise UpstreamError(
                    "Upstream returned a repeated pagination cursor."
                )
            seen_cursors.add(cursor)

        raise UpstreamError(
            "Upstream exceeded the discovery page limit."
        )


async def call_tool(
    url: str,
    tool_name: str,
    arguments: dict,
    *,
    headers: dict[str, str] | None = None,
    timeout_s: float = 15,
    server_id: int | None = None,
) -> CallToolResult:
    async with upstream_session(
        url,
        headers=headers,
        timeout_s=timeout_s,
        server_id=server_id,
    ) as session:
        return await session.call_tool(
            tool_name,
            arguments=arguments,
        )
