"""Check managed MCP destinations in a disposable PostgreSQL schema.

DNS and HTTP are mocked. No Kubernetes, model or store requests are sent.
Run from apps/gateway after loading gateway-local-env.sh.
"""

import asyncio
import re
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

if not Path("app/managed_upstreams.py").is_file():
    raise SystemExit("Run from Wo_MCP/apps/gateway.")
sys.path.insert(0, str(Path.cwd()))

import httpx
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.schema import CreateSchema, DropSchema

from app import managed_upstreams as managed
from app import store_completion as completion
from app import store_jobs as jobs
from app import store_registration as registration
from app.config import database_url
from app.database import engine
from app.models import (
    Base,
    MCPServer,
    Store,
    StoreOperation,
    StoreProvisioningSettings,
    User,
)
from app.upstream_security import (
    DEFAULT_INTERNAL_URLS,
    DestinationPolicy,
    PinnedUpstreamTransport,
    UpstreamPolicyError,
)


OWNER_A, OWNER_B = 98701, 98702


async def check():
    schema = "urumi_managed_dns_test_" + uuid4().hex
    created, isolated = False, None
    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
        created = True

        # One connection proves authorization releases it before DNS.
        isolated = create_async_engine(
            database_url(),
            pool_size=1,
            max_overflow=0,
            pool_timeout=2,
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
            assert await connection.scalar(
                select(func.current_schema())
            ) == schema
            await connection.run_sync(Base.metadata.create_all)

        async with factory.begin() as db:
            db.add(StoreProvisioningSettings(
                id=1, max_concurrent_creations=4,
            ))
            for owner in (OWNER_A, OWNER_B):
                db.add(User(
                    id=owner,
                    email=f"owner-{owner}@fixture.invalid",
                    role="member",
                    active=True,
                    password_hash="fixture-only",
                ))

        policy = DestinationPolicy()
        original_endpoints = policy.internal_endpoints

        async def reset():
            async with factory.begin() as db:
                for model in (StoreOperation, Store, MCPServer):
                    await db.execute(delete(model))

        async def change(model, identity, **values):
            async with factory.begin() as db:
                await db.execute(
                    update(model)
                    .where(model.id == identity)
                    .values(**values)
                )

        async def snapshot():
            async with factory() as db:
                result = {}
                for table in Base.metadata.sorted_tables:
                    rows = await db.execute(
                        select(table).order_by(*table.primary_key.columns)
                    )
                    result[table.name] = [tuple(row) for row in rows]
                return result

        async def seed(*, ready=True, owner=OWNER_A):
            identity = uuid4()
            async with factory.begin() as db:
                now = await db.scalar(select(func.clock_timestamp()))
                store = Store(
                    id=identity,
                    owner_id=owner,
                    name="Assam Tea",
                    slug="tea-" + identity.hex[:16],
                    namespace="urumi-store-" + identity.hex,
                    release_name="store",
                    url=f"http://tea-{identity.hex}.localtest.me",
                    status="Provisioning",
                    desired_state="present",
                    generation=1,
                )
                db.add(store)
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

            claim = await jobs.claim_next(factory)
            assert claim is not None and claim.store_id == identity
            registered = await registration.register_store_mcps(
                factory, claim,
            )
            assert registered is not None

            if ready:
                # Database fixture only; external readiness is not tested.
                assert await completion.finish_ready(factory, claim)
            return claim, registered

        async def dns(host, port):
            assert port == 8000
            assert host.endswith(".svc.cluster.local")
            assert isolated.sync_engine.pool.checkedout() == 0

            async with factory() as db:
                assert await db.scalar(
                    select(func.current_schema())
                ) == schema
            return ["10.43.8.12"]

        async def prepare(url, server_id, *, claim=None, resolver=dns):
            return await managed.prepare_destination(
                url,
                server_id=server_id,
                claim=claim,
                session_factory=factory,
                policy=policy,
                resolver=resolver,
            )

        async def denied(operation, *, message=None):
            try:
                await operation
            except UpstreamPolicyError as error:
                if message is not None:
                    assert str(error) == message, str(error)
                return
            raise AssertionError("An unauthorized destination was allowed.")

        async def rejected_before_dns(url, server_id, *, claim=None):
            resolver = AsyncMock(return_value=["10.43.8.12"])
            await denied(prepare(
                url, server_id, claim=claim, resolver=resolver,
            ))
            resolver.assert_not_awaited()

        with patch(
            "app.upstream_security.resolve_addresses",
            AsyncMock(side_effect=AssertionError(
                "Live upstream DNS is forbidden",
            )),
        ):
            await reset()
            claim, result = await seed()
            before = await snapshot()

            for url, server_id in (
                (result.woo_url, result.woo_server_id),
                (result.analytics_url, result.analytics_server_id),
            ):
                destination = await prepare(url, server_id)
                assert destination.addresses == ("10.43.8.12",)
                assert str(destination.endpoint.url) == url
                await rejected_before_dns(url, server_id, claim=claim)

            with patch.object(managed, "Session", factory):
                await managed.prepare_destination(
                    result.woo_url,
                    server_id=result.woo_server_id,
                    policy=policy,
                    resolver=dns,
                )
            assert await snapshot() == before

            for server_id in (
                None, True, False, 0, -1, "1", 2147483647,
                result.analytics_server_id,
            ):
                await rejected_before_dns(result.woo_url, server_id)

            async with factory.begin() as db:
                shadow = MCPServer(
                    slug="shadow",
                    name="Unlinked duplicate URL",
                    url=result.woo_url,
                    enabled=True,
                    status="Connected",
                    timeout_s=15,
                )
                db.add(shadow)
                await db.flush()
                shadow_id = shadow.id

            await rejected_before_dns(result.woo_url, shadow_id)
            unknown = result.woo_url.replace(
                claim.store_id.hex, uuid4().hex,
            )
            await rejected_before_dns(unknown, result.woo_server_id)

            for invalid in (
                result.woo_url.replace("http:", "https:"),
                result.woo_url.replace(":8000", ":443"),
                result.woo_url.replace(":8000", ":8001"),
                result.woo_url.replace("/mcp", "/admin"),
                result.woo_url.replace(".svc.cluster.local", ".svc"),
                result.woo_url.replace(".svc.cluster.local", ""),
                result.woo_url.replace(
                    ".svc.cluster.local", ".svc.cluster.local.",
                ),
                result.woo_url.replace("woo-mcp.", "wordpress."),
                result.woo_url.upper(),
                result.woo_url + "/",
                result.woo_url + "?token=fixture",
                result.woo_url + "#fixture",
                result.woo_url.replace(
                    "http://", "http://user:fixture@",
                ),
            ):
                await rejected_before_dns(
                    invalid, result.woo_server_id,
                )

            print(
                "Exact store URLs and bound server IDs pass; aliases, "
                "unknown stores and duplicate-URL entries are rejected"
            )

            store_changes = (
                {"status": "Provisioning"},
                {"status": "Failed"},
                {"status": "Deleting"},
                {"status": "Deleted"},
                {"desired_state": "absent"},
                {"deleted_at": func.clock_timestamp()},
                {"release_name": "different"},
                {"namespace": "urumi-store-" + uuid4().hex},
                {"name": "Different store"},
                {"slug": "different-store"},
                {"mcp_server_id": None},
                {"analytics_mcp_server_id": None},
                {
                    "mcp_server_id": result.analytics_server_id,
                    "analytics_mcp_server_id": result.woo_server_id,
                },
            )

            async with factory() as db:
                original_store = await db.get(Store, claim.store_id)
                store_values = {
                    column.name: getattr(original_store, column.name)
                    for column in Store.__table__.columns
                }

            for values in store_changes:
                await change(Store, claim.store_id, **values)
                for url, server_id in (
                    (result.woo_url, result.woo_server_id),
                    (result.analytics_url, result.analytics_server_id),
                ):
                    await rejected_before_dns(url, server_id)
                await change(
                    Store,
                    claim.store_id,
                    **{key: store_values[key] for key in values},
                )

            async with factory() as db:
                original_server = await db.get(
                    MCPServer, result.woo_server_id,
                )
                server_values = {
                    column.name: getattr(original_server, column.name)
                    for column in MCPServer.__table__.columns
                }

            for values in (
                {"enabled": False},
                {"slug": "changed-slug"},
                {"name": "Changed name"},
                {"timeout_s": 10},
                {"url": "https://changed.fixture.invalid/mcp"},
                {"auth_header_name": "Authorization"},
                {"credential_encrypted": "fixture-not-a-real-secret"},
            ):
                await change(MCPServer, result.woo_server_id, **values)
                await rejected_before_dns(
                    result.woo_url, result.woo_server_id,
                )
                await change(
                    MCPServer,
                    result.woo_server_id,
                    **{key: server_values[key] for key in values},
                )

            await change(
                MCPServer, result.woo_server_id, status="Unreachable",
            )
            await prepare(result.woo_url, result.woo_server_id)
            await change(
                MCPServer, result.woo_server_id, status="Connected",
            )

            other, other_result = await seed(owner=OWNER_B)
            await rejected_before_dns(
                result.woo_url, other_result.woo_server_id,
            )

            for column, shared_id, original_id in (
                (
                    "analytics_mcp_server_id",
                    result.woo_server_id,
                    other_result.analytics_server_id,
                ),
                (
                    "mcp_server_id",
                    result.analytics_server_id,
                    other_result.woo_server_id,
                ),
            ):
                await change(
                    Store, other.store_id, **{column: shared_id},
                )
                await rejected_before_dns(
                    result.woo_url, result.woo_server_id,
                )
                await rejected_before_dns(
                    result.analytics_url, result.analytics_server_id,
                )
                await change(
                    Store,
                    other.store_id,
                    status="Deleted",
                    deleted_at=func.clock_timestamp(),
                )
                await rejected_before_dns(
                    result.woo_url, result.woo_server_id,
                )
                await change(
                    Store,
                    other.store_id,
                    **{column: original_id},
                    status="Ready",
                    deleted_at=None,
                )

            await prepare(result.woo_url, result.woo_server_id)
            parallel = await asyncio.gather(
                prepare(
                    result.woo_url,
                    result.woo_server_id,
                    resolver=AsyncMock(return_value=["10.43.8.12"]),
                ),
                prepare(
                    other_result.analytics_url,
                    other_result.analytics_server_id,
                    resolver=AsyncMock(return_value=["10.43.8.13"]),
                ),
            )
            assert parallel[0].endpoint.identity != parallel[1].endpoint.identity
            assert parallel[0].addresses == ("10.43.8.12",)
            assert parallel[1].addresses == ("10.43.8.13",)

            print(
                "Store state, bindings, identity and credential changes fail "
                "closed; cross-store sharing is rejected"
            )

            await reset()
            claim, result = await seed(ready=False)
            await rejected_before_dns(
                result.woo_url, result.woo_server_id,
            )
            before = await snapshot()

            for url, server_id in (
                (result.woo_url, result.woo_server_id),
                (result.analytics_url, result.analytics_server_id),
            ):
                await prepare(url, server_id, claim=claim)
            assert await snapshot() == before

            for values in (
                {"operation_id": uuid4()},
                {"store_id": uuid4()},
                {"lease_token": uuid4()},
                {"owner_id": OWNER_B},
                {"generation": 2},
                {"attempt": claim.attempt + 1},
                {"action": "delete"},
                {"namespace": "urumi-store-" + uuid4().hex},
                {"release_name": "different"},
                {"name": "Changed"},
                {"slug": "changed"},
                {"url": "http://changed.localtest.me"},
                {
                    "deadline_at": claim.deadline_at
                    + timedelta(seconds=60),
                },
            ):
                await rejected_before_dns(
                    result.woo_url,
                    result.woo_server_id,
                    claim=replace(claim, **values),
                )

            await rejected_before_dns(
                result.woo_url,
                result.woo_server_id,
                claim=object(),
            )

            # Snapshot timestamps cannot override the current database lease.
            old_snapshot = replace(
                claim,
                lease_expires_at=claim.checked_at - timedelta(seconds=1),
            )
            await prepare(
                result.woo_url,
                result.woo_server_id,
                claim=old_snapshot,
            )
            assert await jobs.renew_claim(factory, claim) is not None
            await prepare(
                result.woo_url, result.woo_server_id, claim=claim,
            )

            await change(
                StoreOperation,
                claim.operation_id,
                lease_expires_at=func.clock_timestamp()
                - timedelta(seconds=1),
            )
            await rejected_before_dns(
                result.woo_url, result.woo_server_id, claim=claim,
            )

            replacement = await jobs.claim_next(factory)
            assert replacement is not None
            assert replacement.attempt == claim.attempt + 1

            await rejected_before_dns(
                result.woo_url, result.woo_server_id, claim=claim,
            )
            await prepare(
                result.woo_url,
                result.woo_server_id,
                claim=replacement,
            )

            await change(
                StoreOperation,
                claim.operation_id,
                deadline_at=func.clock_timestamp() - timedelta(seconds=1),
            )
            async with factory() as db:
                operation = await db.get(
                    StoreOperation, claim.operation_id,
                )
                overdue = replace(
                    replacement, deadline_at=operation.deadline_at,
                )

            await rejected_before_dns(
                result.woo_url, result.woo_server_id, claim=overdue,
            )

            print(
                "Provisioning requires a live matching claim; renewal, "
                "recovery, forged claims and original deadlines are checked"
            )

            await reset()
            claim, result = await seed()

            for records in (
                [],
                ["not-an-address"],
                ["127.0.0.1"],
                ["169.254.169.254"],
                ["100.100.100.200"],
                ["168.63.129.16"],
                ["172.20.0.2"],
                ["93.184.216.34"],
                ["10.43.8.12", "169.254.169.254"],
                ["10.43.8.12", "93.184.216.34"],
            ):
                await denied(prepare(
                    result.woo_url,
                    result.woo_server_id,
                    resolver=AsyncMock(return_value=records),
                ))

            first = await prepare(
                result.woo_url, result.woo_server_id,
            )
            second = await prepare(
                result.woo_url,
                result.woo_server_id,
                resolver=AsyncMock(return_value=["10.43.8.13"]),
            )
            assert first.addresses == ("10.43.8.12",)
            assert second.addresses == ("10.43.8.13",)
            assert policy.internal_endpoints == original_endpoints

            async def peer(request):
                assert request.url.host == "10.43.8.12"
                assert request.headers["Host"] == (
                    first.endpoint.url.netloc.decode("ascii")
                )
                return httpx.Response(200, json={"fixture": True})

            transport = PinnedUpstreamTransport(
                first, transport=httpx.MockTransport(peer),
            )
            try:
                response = await transport.handle_async_request(
                    httpx.Request(
                        "POST", result.woo_url, json={"fixture": True},
                    )
                )
                assert response.status_code == 200
                await response.aclose()
                await denied(transport.handle_async_request(
                    httpx.Request(
                        "POST", result.analytics_url, json={},
                    )
                ))
            finally:
                await transport.aclose()

            print(
                "DNS is fresh and released from database transactions; "
                "unsafe/mixed answers are rejected and HTTP stays pinned"
            )

            async def race(*, ready, kind):
                await reset()
                claim, result = await seed(ready=ready)
                entered, resume = asyncio.Event(), asyncio.Event()

                async def paused_dns(host, port):
                    assert isolated.sync_engine.pool.checkedout() == 0
                    entered.set()
                    await asyncio.wait_for(resume.wait(), 10)
                    return ["10.43.8.12"]

                task = asyncio.create_task(prepare(
                    result.woo_url,
                    result.woo_server_id,
                    claim=None if ready else claim,
                    resolver=paused_dns,
                ))
                try:
                    await asyncio.wait_for(entered.wait(), 10)

                    if kind == "disable":
                        await change(
                            MCPServer,
                            result.woo_server_id,
                            enabled=False,
                        )
                    elif kind == "delete":
                        await change(
                            Store,
                            claim.store_id,
                            status="Deleting",
                            desired_state="absent",
                        )
                    elif kind == "unbind":
                        await change(
                            Store, claim.store_id, mcp_server_id=None,
                        )
                    elif kind == "remove":
                        async with factory.begin() as db:
                            await db.execute(
                                delete(MCPServer).where(
                                    MCPServer.id == result.woo_server_id,
                                )
                            )
                    elif kind == "identity":
                        await change(
                            MCPServer,
                            result.woo_server_id,
                            name="Changed",
                        )
                    elif kind == "token":
                        await change(
                            StoreOperation,
                            claim.operation_id,
                            lease_token=uuid4(),
                        )
                    elif kind == "lease":
                        await change(
                            StoreOperation,
                            claim.operation_id,
                            lease_expires_at=func.clock_timestamp(),
                        )
                    elif kind == "deadline":
                        await change(
                            StoreOperation,
                            claim.operation_id,
                            deadline_at=func.clock_timestamp(),
                        )
                    elif kind == "generation":
                        await change(
                            Store, claim.store_id, generation=2,
                        )
                    else:
                        raise AssertionError(kind)

                    resume.set()
                    await denied(task, message=managed.DENIED)
                finally:
                    resume.set()
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

            for kind in (
                "disable", "delete", "unbind", "remove", "identity",
            ):
                await race(ready=True, kind=kind)

            for kind in ("token", "lease", "deadline", "generation"):
                await race(ready=False, kind=kind)

            print(
                "Independent database changes during DNS reject deletion, "
                "disabled/removed bindings and lost worker ownership"
            )

            await reset()
            claim, result = await seed()
            entered = asyncio.Event()

            async def cancelled_dns(host, port):
                entered.set()
                await asyncio.Event().wait()

            task = asyncio.create_task(prepare(
                result.woo_url,
                result.woo_server_id,
                resolver=cancelled_dns,
            ))
            try:
                await asyncio.wait_for(entered.wait(), 10)
                task.cancel()
                cancelled = await asyncio.gather(
                    task, return_exceptions=True,
                )
                assert isinstance(cancelled[0], asyncio.CancelledError)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            assert isolated.sync_engine.pool.checkedout() == 0
            await prepare(result.woo_url, result.woo_server_id)

            normal_execute = AsyncSession.execute
            for failure_at in (1, 2):
                calls = 0

                async def failed_execute(
                    db, statement, *args, **kwargs,
                ):
                    nonlocal calls
                    calls += 1
                    if calls == failure_at:
                        # Real PostgreSQL error in the disposable schema.
                        await normal_execute(
                            db, text("SELECT 1 / 0"),
                        )
                    return await normal_execute(
                        db, statement, *args, **kwargs,
                    )

                with patch.object(
                    AsyncSession, "execute", failed_execute,
                ):
                    await denied(
                        prepare(
                            result.woo_url,
                            result.woo_server_id,
                            resolver=AsyncMock(
                                return_value=["10.43.8.12"],
                            ),
                        ),
                        message=(
                            "Managed store authorization storage "
                            "is unavailable."
                        ),
                    )

                assert calls == failure_at
                assert isolated.sync_engine.pool.checkedout() == 0
                await prepare(result.woo_url, result.woo_server_id)

            print(
                "Cancellation releases resources; database failures before "
                "or after DNS return safe errors and allow recovery"
            )

            def forbidden_database():
                raise AssertionError(
                    "Ordinary destinations must not query store storage",
                )

            for url, records in (
                *((url, ["10.43.8.12"]) for url in DEFAULT_INTERNAL_URLS),
                (
                    "https://mcp.fixture.invalid/mcp",
                    ["93.184.216.34"],
                ),
            ):
                destination = await managed.prepare_destination(
                    url,
                    policy=policy,
                    session_factory=forbidden_database,
                    resolver=AsyncMock(return_value=records),
                )
                assert destination.addresses == tuple(records)

            for url in (
                "https://169.254.169.254/mcp",
                "https://kubernetes.default.svc/mcp",
                "http://postgres.platform.svc.cluster.local:5432/mcp",
            ):
                await denied(managed.prepare_destination(
                    url,
                    policy=policy,
                    session_factory=forbidden_database,
                    resolver=AsyncMock(return_value=["10.43.8.12"]),
                ))

            for records in (
                ["127.0.0.1"],
                ["93.184.216.34", "10.43.8.12"],
            ):
                await denied(managed.prepare_destination(
                    "https://mcp.fixture.invalid/mcp",
                    policy=policy,
                    session_factory=forbidden_database,
                    resolver=AsyncMock(return_value=records),
                ))

            await denied(managed.prepare_destination(
                "https://mcp.fixture.invalid/mcp",
                policy=policy,
                claim=claim,
                session_factory=forbidden_database,
                resolver=AsyncMock(return_value=["93.184.216.34"]),
            ))

            assert policy.internal_endpoints == original_endpoints
            print(
                "Existing project endpoints and external HTTPS retain their "
                "original policy; worker claims cannot bypass it"
            )
            print(
                "Managed destination verification passed. "
                "Live accounts/stores/settings were unchanged."
            )
            print(
                "Gateway integration and Kubernetes provisioning "
                "are not enabled by this test."
            )
    finally:
        if isolated is not None:
            await isolated.dispose()
        try:
            if created:
                assert re.fullmatch(
                    r"urumi_managed_dns_test_[0-9a-f]{32}", schema,
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
