"""MCP verification control flow; ownership, HTTP and discovery are mocked."""

import asyncio
from dataclasses import replace
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
# This process-local value is unused: all ownership storage is mocked.
os.environ["PGPASSWORD"] = "isolated-mcp-verification-unused"

from app import store_mcp_verification as module
from app.store_guarded_kubernetes import StoreKubernetesStopped
from app.store_job_guard import StoreJobClaimLost
from app.store_registration import StoreMCPRegistrations, definitions_for
from app.store_namespace import StoreNamespaceError
from app.store_kube_connection import KubernetesAPIError
import verify_store_workload_api as workloads
import verify_store_guarded_secrets as fixtures


class Fixture(workloads.Fixture):
    def __init__(self):
        super().__init__()
        definitions = definitions_for(fixtures.CLAIM)
        woo, analytics = definitions
        self.registrations = StoreMCPRegistrations(
            101, woo.slug, woo.url, 102, analytics.slug, analytics.url,
        )
        self.expected = {
            woo.url: (101, ["list_products", "create_product", "list_orders", "get_order"]),
            analytics.url: (102, ["sales_summary", "order_counts", "top_products"]),
        }
        self.discoveries = []
        self.after_discovery = None
        self.bad_tools = self.discovery_error = False
        self.discovery_delay = 0
        self.discovery_started = asyncio.Event()
        self.discovery_active = False

    async def discover(self, url, **kwargs):
        assert url in self.expected
        identity, names = self.expected[url]
        assert kwargs["server_id"] == identity
        assert kwargs["store_claim"] == fixtures.CLAIM
        assert 1 <= kwargs["timeout_s"] <= 15
        assert set(kwargs) == {"server_id", "store_claim", "timeout_s"}
        self.discoveries.append(url)
        self.discovery_started.set()
        self.discovery_active = True
        try:
            if self.discovery_delay:
                await asyncio.sleep(self.discovery_delay)
            if self.after_discovery is not None:
                self.after_discovery()
            if self.discovery_error:
                raise RuntimeError("PRIVATE UPSTREAM ERROR")
            if self.bad_tools:
                names = names[:-1]
            return [SimpleNamespace(name=name) for name in names]
        finally:
            self.discovery_active = False

    async def verify(self, registrations=None, **changes):
        options = {
            "namespace_uid": "namespace-uid", "credentials_secret_uid": "secret-uid",
        }
        options.update(changes)
        async def work(raw):
            with patch.object(module, "require_claim", self.require), patch.object(
                module, "discover_tools", self.discover,
            ):
                return await module.verify_store_mcps(
                    self.factory, fixtures.CLAIM, raw, fixtures.SCOPE,
                    self.registrations if registrations is None else registrations,
                    **options,
                )
        return await self.run(callback=work)


async def error(kind, operation):
    try:
        await operation
    except kind as caught:
        assert "PRIVATE" not in str(caught)
        return caught
    raise AssertionError("Expected " + kind.__name__)


async def check():
    baseline = asyncio.all_tasks()
    f = Fixture()
    assert await f.verify() == {"woo-mcp": 4, "mcp-analytics": 3}
    assert f.discoveries == list(f.expected)
    assert all(method == "GET" for method, path, body in f.requests)
    assert f.closed and not f.discovery_active
    assert await f.verify() == {"woo-mcp": 4, "mcp-analytics": 3}
    print("Both store IDs and live claims reach discovery; only counts return and no tools are executed")

    for change in (
        {"woo_server_id": True}, {"analytics_server_id": 101},
        {"woo_slug": "other"}, {"analytics_url": "http://other-store/mcp"},
    ):
        f = Fixture()
        await error(module.StoreMCPVerificationError, f.verify(
            replace(f.registrations, **change),
        ))
        assert not f.discoveries and not f.requests
    f = Fixture()
    await error(ValueError, f.verify(namespace_uid=None))
    assert not f.discoveries and not f.requests
    f = Fixture()
    f.bad_tools = True
    await error(module.StoreMCPVerificationError, f.verify())
    assert len(f.discoveries) == 1
    f = Fixture()
    f.discovery_error = True
    await error(module.StoreMCPVerificationError, f.verify())
    assert len(f.discoveries) == 1
    print("Changed registrations, missing core tools and discovery failures stop without retries")

    f = Fixture()
    f.before = StoreJobClaimLost("PRIVATE CLAIM")
    caught = await error(StoreKubernetesStopped, f.verify())
    assert not caught.write_may_have_applied and not f.discoveries
    f = Fixture()
    f.after_discovery = lambda: setattr(f, "stored_uid", "replacement")
    await error(StoreKubernetesStopped, f.verify())
    assert len(f.discoveries) == 1
    for kind, change in (
        (StoreNamespaceError, lambda f: f.namespace["metadata"].update(uid="replacement")),
        (KubernetesAPIError, lambda f: f.secret["metadata"].update(uid="replacement")),
    ):
        f = Fixture()
        f.after_discovery = lambda f=f, change=change: change(f)
        await error(module.StoreMCPVerificationError, f.verify())
        assert f.closed
    print("Lost claims and changed actual resources cannot produce a successful verification")

    f = Fixture()
    f.discovery_delay = 1
    with patch.object(module, "_CHECK_TIMEOUT_SECONDS", 0.03):
        await error(module.StoreMCPVerificationError, f.verify())
    assert len(f.discoveries) == 1 and f.closed and not f.discovery_active
    f = Fixture()
    f.discovery_delay = 1
    task = asyncio.create_task(f.verify())
    await f.discovery_started.wait()
    task.cancel()
    await error(asyncio.CancelledError, task)
    assert f.closed and not f.discovery_active and len(f.discoveries) == 1
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)
    print("Timeouts and cancellation stop discovery without leaking diagnostics or tasks")
    print("Store MCP verification passed. No live services or database calls were made.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 30))
