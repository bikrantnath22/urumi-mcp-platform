"""Managed-store integration through the real MCP SDK and isolated PostgreSQL.

The protocol peer and DNS are mocked. No model, WordPress or Kubernetes calls.
Run from apps/gateway after loading gateway-local-env.sh.
"""

import asyncio
from contextlib import ExitStack
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import re
import sys
from unittest.mock import patch
from uuid import uuid4

if not Path("app/managed_upstreams.py").is_file():
    raise SystemExit("Run from Wo_MCP/apps/gateway.")
sys.path.insert(0, str(Path.cwd()))

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app import core, managed_upstreams, permissions, rate_limits, registry
from app import store_completion, store_jobs, store_registration, upstreams
from app.catalog_cache import ToolCatalogCache
from app.config import database_url
from app.database import engine
from app.models import (
    AuditLog, Base, MCPServer, Store, StoreOperation,
    StoreProvisioningSettings, ToolGrant, UsageLimitSettings, User,
)
from app.upstream_security import (
    DestinationPolicy, PinnedUpstreamTransport, parse_endpoint,
)


MEMBER, OTHER, ADMIN = 98801, 98802, 98803


async def check():
    schema = "urumi_managed_sdk_test_" + uuid4().hex
    created, isolated, cache = False, None, None
    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
        created = True

        isolated = create_async_engine(
            database_url(), pool_size=4, max_overflow=0, pool_timeout=5,
            hide_parameters=True,
            connect_args={
                "connect_timeout": 5,
                "options": (
                    f"-c search_path={schema} -c timezone=UTC "
                    "-c statement_timeout=10000 -c lock_timeout=3000"
                ),
            },
        )
        factory = async_sessionmaker(isolated, expire_on_commit=False)

        async with isolated.begin() as connection:
            assert await connection.scalar(select(func.current_schema())) == schema
            await connection.run_sync(Base.metadata.create_all)

        async with factory.begin() as db:
            db.add(StoreProvisioningSettings(id=1, max_concurrent_creations=4))
            db.add(UsageLimitSettings(id=1, tool_per_minute=1000))
            for identity in (MEMBER, OTHER, ADMIN):
                db.add(User(
                    id=identity,
                    email=f"user-{identity}@fixture.invalid",
                    role="admin" if identity == ADMIN else "member",
                    active=True,
                    password_hash="fixture-only",
                ))

        async def seed(owner, *, ready=True):
            identity = uuid4()
            async with factory.begin() as db:
                now = await db.scalar(select(func.clock_timestamp()))
                db.add(Store(
                    id=identity,
                    owner_id=owner,
                    name="SDK fixture store",
                    slug="tea-" + identity.hex[:16],
                    namespace="urumi-store-" + identity.hex,
                    release_name="store",
                    url=f"http://tea-{identity.hex}.localtest.me",
                    status="Provisioning",
                    desired_state="present",
                    generation=1,
                ))
                await db.flush()
                db.add(StoreOperation(
                    id=uuid4(),
                    store_id=identity,
                    requested_by_id=owner,
                    request_key=uuid4(),
                    request_fingerprint="a" * 64,
                    action="provision",
                    store_generation=1,
                    status="Pending",
                    attempts=0,
                    available_at=now - timedelta(seconds=1),
                    deadline_at=now + timedelta(seconds=1200),
                ))

            claim = await store_jobs.claim_next(factory, lease_seconds=300)
            assert claim is not None and claim.store_id == identity
            registered = await store_registration.register_store_mcps(
                factory, claim,
            )
            assert registered is not None

            if ready:
                # Fixture state only; external readiness is not exercised.
                assert await store_completion.finish_ready(factory, claim)
            return claim, registered

        a, first = await seed(MEMBER)
        b, second = await seed(OTHER)
        c, draft = await seed(MEMBER, ready=False)

        peers = {}
        slugs = []
        for registered in (first, second, draft):
            for url, slug in (
                (registered.woo_url, registered.woo_slug),
                (registered.analytics_url, registered.analytics_slug),
            ):
                host = parse_endpoint(url).host
                peers[host] = f"10.43.8.{10 + len(peers)}"
                slugs.append(slug)

        # Include managed slugs explicitly to exercise warm-cache guards.
        cache = ToolCatalogCache(
            ttl_seconds=300, server_slugs=tuple(slugs),
        )
        requests, executions = [], []

        async def resolve(host, port):
            assert host in peers and port == 8000
            return [peers[host]]

        async def peer(request):
            requests.append((str(request.url), request.method))
            host_header = request.headers.get("Host", "")
            host, separator, port = host_header.rpartition(":")
            assert separator and port == "8000" and host in peers
            assert request.url.host == peers[host]
            assert "Authorization" not in request.headers

            if request.method != "POST":
                return httpx.Response(405)

            body = json.loads(await request.aread())
            method = body["method"]

            if method.startswith("notifications/"):
                return httpx.Response(202)

            if method == "initialize":
                result = {
                    "protocolVersion": body["params"]["protocolVersion"],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "Managed fixture",
                        "version": "1.0",
                    },
                }
            elif method == "tools/list":
                result = {"tools": [
                    {
                        "name": name,
                        "description": "Read fixture data",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "index": {"type": "integer"},
                            },
                        },
                    }
                    for name in ("fixture_read", "fixture_other")
                ]}
            elif method == "tools/call":
                arguments = body["params"].get("arguments", {})
                executions.append((
                    host, body["params"]["name"], arguments,
                ))
                result = {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "host": host,
                            "arguments": arguments,
                        }),
                    }],
                    "isError": False,
                }
            else:
                raise AssertionError(
                    f"Unexpected fixture method: {method}",
                )

            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": result,
                },
            )

        def transport(destination):
            return PinnedUpstreamTransport(
                destination,
                transport=httpx.MockTransport(peer),
            )

        async def blocked(operation):
            before = len(requests)
            try:
                await operation
            except upstreams.UpstreamError:
                assert len(requests) == before
                return
            raise AssertionError(
                "An unauthorized SDK connection was allowed.",
            )

        async def change(model, identity, **values):
            async with factory.begin() as db:
                await db.execute(
                    update(model)
                    .where(model.id == identity)
                    .values(**values)
                )

        async def permit():
            async with factory() as db:
                admin = await db.get(User, ADMIN)
                return await permissions.grant_permission(
                    MEMBER,
                    permissions.PermissionBody(
                        name=first.woo_slug + "__fixture_read",
                    ),
                    admin,
                    db,
                )

        with ExitStack() as stack:
            # All session factories below use only the temporary schema.
            for module in (
                core, registry, permissions,
                rate_limits, managed_upstreams,
            ):
                stack.enter_context(
                    patch.object(module, "Session", factory)
                )

            for module in (core, registry):
                stack.enter_context(
                    patch.object(module, "catalog_cache", cache)
                )

            stack.enter_context(patch.object(
                upstreams, "destination_policy", DestinationPolicy(),
            ))
            stack.enter_context(patch(
                "app.upstream_security.resolve_addresses",
                side_effect=resolve,
            ))
            stack.enter_context(patch.object(
                upstreams,
                "PinnedUpstreamTransport",
                side_effect=transport,
            ))

            try:
                tools = await upstreams.discover_tools(
                    first.woo_url,
                    server_id=first.woo_server_id,
                )
                assert {tool.name for tool in tools} == {
                    "fixture_read", "fixture_other",
                }

                value = await upstreams.call_tool(
                    first.woo_url,
                    "fixture_read",
                    {"index": 1},
                    server_id=first.woo_server_id,
                )
                assert not value.isError
                assert json.loads(value.content[0].text)["host"] == (
                    parse_endpoint(first.woo_url).host
                )

                for identity in (
                    None,
                    first.analytics_server_id,
                    second.woo_server_id,
                ):
                    await blocked(upstreams.discover_tools(
                        first.woo_url, server_id=identity,
                    ))
                    await blocked(upstreams.call_tool(
                        first.woo_url,
                        "fixture_read",
                        {},
                        server_id=identity,
                    ))

                print(
                    "Real SDK discovery and reads use the exact bound server "
                    "ID; missing or mismatched IDs send no HTTP"
                )

                for url, server_id in (
                    (draft.woo_url, draft.woo_server_id),
                    (draft.analytics_url, draft.analytics_server_id),
                ):
                    await blocked(upstreams.discover_tools(
                        url, server_id=server_id,
                    ))
                    found = await upstreams.discover_tools(
                        url, server_id=server_id, store_claim=c,
                    )
                    assert len(found) == 2
                    await blocked(upstreams.discover_tools(
                        url,
                        server_id=server_id,
                        store_claim=replace(c, lease_token=uuid4()),
                    ))

                before = len(requests)
                try:
                    await upstreams.call_tool(
                        draft.woo_url,
                        "fixture_read",
                        {},
                        server_id=draft.woo_server_id,
                        store_claim=c,
                    )
                except TypeError:
                    pass
                else:
                    raise AssertionError(
                        "Tool execution accepted a worker claim.",
                    )
                assert len(requests) == before

                await change(
                    StoreOperation,
                    c.operation_id,
                    lease_expires_at=func.clock_timestamp()
                    - timedelta(seconds=1),
                )
                await blocked(upstreams.discover_tools(
                    draft.woo_url,
                    server_id=draft.woo_server_id,
                    store_claim=c,
                ))

                print(
                    "Worker discovery reaches disabled draft registrations "
                    "only with a live claim; tool execution has no worker bypass"
                )

                async with factory() as db:
                    member = await db.get(User, MEMBER)
                    other = await db.get(User, OTHER)
                    admin = await db.get(User, ADMIN)

                assert await core.list_gateway_tools(member) == []
                assert await core.list_gateway_tools(other) == []

                all_names = {
                    tool.name
                    for tool in await core.list_gateway_tools(admin)
                }
                expected = {
                    slug + "__" + name
                    for slug in (
                        first.woo_slug,
                        first.analytics_slug,
                        second.woo_slug,
                        second.analytics_slug,
                    )
                    for name in ("fixture_read", "fixture_other")
                }
                assert all_names == expected

                await permit()
                expected_member = [first.woo_slug + "__fixture_read"]
                before = len(requests)

                assert [
                    tool.name
                    for tool in await core.list_gateway_tools(member)
                ] == expected_member
                assert len(requests) == before

                for name in (
                    first.woo_slug + "__fixture_other",
                    second.woo_slug + "__fixture_read",
                ):
                    value = await core.execute_gateway_tool(
                        member, name, {}, source="mcp",
                    )
                    assert value.isError and len(requests) == before

                arguments = {
                    "index": 7,
                    "user_id": ADMIN,
                    "server_id": second.woo_server_id,
                }
                value = await core.execute_gateway_tool(
                    member,
                    expected_member[0],
                    arguments,
                    source="mcp",
                )
                assert not value.isError
                assert executions[-1] == (
                    parse_endpoint(first.woo_url).host,
                    "fixture_read",
                    arguments,
                )

                async with factory() as db:
                    audit = await db.scalar(
                        select(AuditLog)
                        .order_by(AuditLog.id.desc())
                        .limit(1)
                    )
                    saved_audit_id = audit.id
                    assert audit.user_id == MEMBER
                    assert audit.server_id == first.woo_server_id
                    assert audit.outcome == "success"
                    assert audit.source == "mcp"

                print(
                    "Store ownership grants no automatic tools; scoped member "
                    "permissions and audited caller identity survive SDK execution"
                )

                refreshed = await registry.refresh_server(
                    first.analytics_server_id,
                )
                assert refreshed["server"]["status"] == "Connected"
                assert len(refreshed["tools"]) == 2

                async with factory() as db:
                    admin_for_revoke = await db.get(User, ADMIN)
                    await permissions.revoke_permission(
                        MEMBER,
                        permissions.PermissionBody(
                            name=expected_member[0],
                        ),
                        admin_for_revoke,
                        db,
                    )

                before = len(requests)
                assert await core.list_gateway_tools(member) == []
                assert (await core.execute_gateway_tool(
                    member,
                    expected_member[0],
                    {},
                    source="chat",
                )).isError
                assert len(requests) == before

                await permit()
                assert [
                    tool.name
                    for tool in await core.list_gateway_tools(member)
                ] == expected_member

                print(
                    "Admin health checks and permission verification forward "
                    "managed IDs; revocation defeats a warm catalogue"
                )

                # Leave enabled=True to test the independent store guard.
                await change(
                    Store,
                    a.store_id,
                    status="Deleting",
                    desired_state="absent",
                )
                before = len(requests)

                assert await core.list_gateway_tools(member) == []
                assert (await core.execute_gateway_tool(
                    member,
                    expected_member[0],
                    {},
                    source="mcp",
                )).isError
                assert len(requests) == before

                assert {
                    tool.name
                    for tool in await core.list_gateway_tools(admin)
                } == {
                    slug + "__" + name
                    for slug in (
                        second.woo_slug,
                        second.analytics_slug,
                    )
                    for name in ("fixture_read", "fixture_other")
                }

                await change(
                    Store,
                    a.store_id,
                    status="Ready",
                    desired_state="present",
                )
                assert [
                    tool.name
                    for tool in await core.list_gateway_tools(member)
                ] == expected_member

                print(
                    "Cached definitions cannot expose a deleting store; "
                    "the other store remains discoverable"
                )

                # Database-only deletion fixture; no real resources existed.
                async with factory.begin() as db:
                    now = await db.scalar(select(func.clock_timestamp()))
                    stale_server = await db.get(
                        MCPServer, first.woo_server_id,
                    )
                    db.expunge(stale_server)
                    assert stale_server.enabled

                    await db.execute(
                        update(Store)
                        .where(Store.id == a.store_id)
                        .values(
                            status="Deleting",
                            desired_state="absent",
                            generation=2,
                        )
                    )
                    await db.execute(
                        update(MCPServer)
                        .where(MCPServer.id.in_((
                            first.woo_server_id,
                            first.analytics_server_id,
                        )))
                        .values(enabled=False, status="Disabled")
                    )
                    db.add(StoreOperation(
                        id=uuid4(),
                        store_id=a.store_id,
                        requested_by_id=MEMBER,
                        request_key=uuid4(),
                        request_fingerprint="b" * 64,
                        action="delete",
                        store_generation=2,
                        status="Pending",
                        attempts=0,
                        available_at=now,
                        deadline_at=now + timedelta(seconds=1200),
                    ))

                deletion = await store_jobs.claim_next(
                    factory, lease_seconds=300,
                )
                assert deletion is not None
                assert deletion.action == "delete"
                assert deletion.store_id == a.store_id
                assert await store_completion.finish_deleted(
                    factory, deletion,
                )

                before = len(requests)
                assert await core.discover_one(stale_server) == []
                assert await core.list_gateway_tools(member) == []
                assert (await core.execute_gateway_tool(
                    member,
                    expected_member[0],
                    {},
                    source="mcp",
                )).isError

                assert {
                    tool.name
                    for tool in await core.list_gateway_tools(admin)
                } == {
                    slug + "__" + name
                    for slug in (
                        second.woo_slug,
                        second.analytics_slug,
                    )
                    for name in ("fixture_read", "fixture_other")
                }
                assert len(requests) == before

                async with factory() as db:
                    saved = await db.get(AuditLog, saved_audit_id)
                    assert saved.user_id == MEMBER
                    assert saved.server_id is None
                    assert saved.server_slug == first.woo_slug

                    assert await db.scalar(
                        select(func.count())
                        .select_from(ToolGrant)
                        .where(ToolGrant.user_id == MEMBER)
                    ) == 0

                    assert await db.get(
                        MCPServer, second.woo_server_id,
                    ) is not None

                print(
                    "Deleted registrations and grants disappear; stale "
                    "SDK/catalogue access is blocked and historical audits survive"
                )
                print(
                    "Managed SDK integration passed. Live accounts/stores/"
                    "settings and upstream services were unchanged."
                )
                print(
                    "Real Kubernetes provisioning and resource cleanup "
                    "are not exercised."
                )
            finally:
                await cache.close()
    finally:
        if cache is not None:
            await cache.close()
        if isolated is not None:
            await isolated.dispose()
        try:
            if created:
                assert re.fullmatch(
                    r"urumi_managed_sdk_test_[0-9a-f]{32}", schema,
                )
                async with engine.begin() as connection:
                    await connection.execute(
                        DropSchema(schema, cascade=True),
                    )
                print("Temporary PostgreSQL schema removed.")
        finally:
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 180))
