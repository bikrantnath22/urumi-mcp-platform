"""Cleanup inventory integration; claim storage and every HTTP reply are mocked."""

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from app import store_volume_inventory as module
from app.store_claims import StoreClaimIdentity
from app.store_guarded_kubernetes import StoreKubernetesStopped
from app.store_job_guard import StoreJobClaimLost, StoreJobGuardUnavailable
from app.store_kube_connection import KubernetesAPIError
from app.store_namespace import StoreNamespaceError
from app.store_volume_pages import volume_page_path
from app.store_volumes import STORE_CLAIM_NAMES, StoreVolumeError, StoreVolumePending
from verify_store_volume_api import Fixture as BaseFixture, SCOPE, error
from verify_store_volume_pages import volume, page


class Fixture(BaseFixture):
    def __init__(self):
        super().__init__()
        self.pages = [page([volume(name) for name in STORE_CLAIM_NAMES])]
        for item in self.pages[0]["items"]:
            item.pop("apiVersion")
            item.pop("kind")
        self.page_count = 0

    async def require(self, factory, claim, **options):
        assert options["for_cleanup"] is True
        return await super().require(factory, claim, **options)

    async def peer(self, request):
        target = request.url.raw_path.decode("ascii")
        assert request.method == "GET" and not request.content
        self.calls.append(target)
        if target == self.ns_path:
            item = deepcopy(self.namespace)
            return httpx.Response(200 if item is not None else 404, json=item or {})
        index = self.page_count
        expected = volume_page_path(
            "" if index == 0 else self.pages[index - 1]["metadata"]["continue"]
        )
        assert target == expected and index < len(self.pages)
        self.page_count += 1
        self.active = True
        self.started.set()
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            item = deepcopy(self.pages[index])
            if self.after:
                callback, self.after = self.after, None
                callback()
            if self.status != 200:
                return httpx.Response(self.status, json={"message": "PRIVATE HTTP"})
            return httpx.Response(404 if item is None else 200, json=item or {})
        finally:
            self.active = False

    async def run(self, callback=None, **options):
        if callback is None:
            callback = lambda raw: module.read_store_volume_inventory(
                self.factory, self.claim, raw, SCOPE,
                namespace_uid="namespace-uid", **options,
            )
        return await super().run(callback)


async def wait_started(task, signal):
    waiter = asyncio.create_task(signal.wait())
    try:
        done, _ = await asyncio.wait(
            (task, waiter), timeout=5, return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            await task
            raise AssertionError("The operation ended before the HTTP pause.")
        assert waiter in done, "The HTTP pause was not reached."
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def check():
    baseline = asyncio.all_tasks()
    f = Fixture()
    unrelated = volume("wordpress-data")
    unrelated["metadata"].update(name="other-volume", uid="other-volume")
    unrelated["spec"]["claimRef"].update(namespace="other-store", uid="other-claim")
    unrelated["spec"]["private_path"] = "PRIVATE UNRELATED DATA"
    f.pages = [
        page([unrelated], token="next/a+b="),
        page([volume("wordpress-data")], token="last"),
        page([volume("data-mysql-0")]),
    ]
    before = deepcopy((f.pages, f.namespace))
    result = await f.run()
    assert result.namespace_present and result.namespace_uid == "namespace-uid"
    assert result.resource_version == "42"
    assert tuple(item.claim_name for item in result.bindings) == STORE_CLAIM_NAMES
    assert (f.pages, f.namespace) == before and "PRIVATE" not in repr(result)
    assert f.calls == [
        f.ns_path, volume_page_path(), f.ns_path,
        volume_page_path("next/a+b="), f.ns_path,
        volume_page_path("last"), f.ns_path,
    ]
    assert f.checks >= 2 * len(f.calls) and f.closed
    try:
        result.bindings = ()
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("The scan result is mutable.")
    print("All pages are guarded; only immutable store identities escape and unrelated data stays private")

    for state in ("terminating", "absent", "disappears"):
        f = Fixture()
        if state == "terminating":
            f.namespace["status"]["phase"] = "Terminating"
            f.namespace["metadata"]["deletionTimestamp"] = "fixture-time"
        elif state == "absent":
            f.namespace = None
        else:
            f.after = lambda: setattr(f, "namespace", None)
        result = await f.run()
        assert result.namespace_present == (state == "terminating")
        assert len(result.bindings) == 2
    f = Fixture()
    f.namespace = None
    f.pages = [page()]
    result = await f.run()
    assert not result.namespace_present and result.bindings == ()
    assert not hasattr(result, "cleanup_complete")
    print("Terminating or absent namespaces allow recovery; empty scans never declare cleanup complete")

    f = Fixture()
    async def forbidden(raw):
        reader = module._InventoryReads(
            raw, f.factory, f.claim, SCOPE, namespace_uid="namespace-uid",
        )
        for target in (
            "/api/v1/namespaces/platform", f.pvc_path, f.pv_path,
            "/api/v1/persistentvolumes", volume_page_path("unissued"),
            volume_page_path() + "&watch=true",
        ):
            await error(ValueError, reader.request("GET", target))
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            await error(ValueError, reader.request(method, volume_page_path()))
        await error(ValueError, reader.request("GET", volume_page_path(), body={}))
        reader._pages.accept(page(token="issued"))
        await error(ValueError, reader.request("GET", volume_page_path()))
    await f.run(forbidden)
    assert not f.calls and f.checks == 0
    async def invalid(raw):
        return await module.read_store_volume_inventory(
            f.factory, f.claim, raw, SCOPE, namespace_uid=None,
        )
    await error(ValueError, f.run(invalid))
    print("Other stores, item reads, unissued pages, old cursors and all writes are blocked before access")

    for failure in (
        StoreJobClaimLost("PRIVATE CLAIM"), StoreJobGuardUnavailable("PRIVATE STORE"),
    ):
        f = Fixture()
        f.failure = failure
        caught = await error(StoreKubernetesStopped, f.run())
        assert not caught.write_may_have_applied and not f.calls
    f = Fixture()
    f.remaining = 0
    await error(StoreKubernetesStopped, f.run())
    assert not f.calls
    f = Fixture()
    f.after = lambda: setattr(f, "lost", True)
    await error(StoreKubernetesStopped, f.run())
    assert f.page_count == 1 and f.closed
    for change in ("uid", "owner", "reappear", "phase"):
        f = Fixture()
        original = deepcopy(f.namespace)
        if change == "uid":
            f.after = lambda: f.namespace["metadata"].update(uid="replacement")
        elif change == "owner":
            f.after = lambda: f.namespace["metadata"]["labels"].update({"urumi.ai/owner-id": "99"})
        elif change == "phase":
            f.after = lambda: f.namespace["status"].update(phase="Unknown")
        else:
            f.namespace = None
            f.after = lambda: setattr(f, "namespace", original)
        await error((StoreNamespaceError, StoreVolumeError, StoreVolumePending), f.run())
        assert f.page_count == 1
    f = Fixture()
    f.claim = replace(
        f.claim, action="delete", deadline_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    assert len((await f.run()).bindings) == 2
    print("Live cleanup leases remain required after deadlines; lost ownership and namespace replacements reject results")

    for issue in ("version", "token", "missing", "malformed", "replacement"):
        f = Fixture()
        f.pages = [page([volume("data-mysql-0")], token="second"), page()]
        options = {}
        if issue == "version":
            f.pages[1]["metadata"]["resourceVersion"] = "changed"
        elif issue == "token":
            f.pages[1]["metadata"]["continue"] = "second"
        elif issue == "missing":
            f.pages[1] = None
        elif issue == "malformed":
            f.pages[1]["items"] = None
        else:
            from app.store_namespace import namespace_for
            options["known"] = (StoreClaimIdentity(
                namespace_for(f.claim), "namespace-uid", "data-mysql-0", "old",
            ),)
        await error(StoreVolumeError, f.run(**options))
        assert f.page_count <= 2 and f.closed
    for status in (403, 410, 503):
        f = Fixture()
        f.status = status
        caught = await error(KubernetesAPIError, f.run())
        assert caught.status_code == status and not caught.write_may_have_applied
        assert f.page_count == 1 and f.closed
    print("Incomplete pages, replaced saved claims and expired cursors return no partial success or retries")

    f = Fixture()
    f.delay = 1
    with patch.object(module, "_SCAN_TIMEOUT_SECONDS", 0.1):
        caught = await error(KubernetesAPIError, f.run())
    assert not caught.write_may_have_applied and f.closed and not f.active
    f = Fixture()
    f.delay = 1
    task = asyncio.create_task(f.run())
    try:
        await wait_started(task, f.started)
        task.cancel()
        await error(asyncio.CancelledError, task)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert f.closed and not f.active and f.page_count == 1
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)
    print("Timeouts and cancellation close HTTP work without retries or remaining tasks")
    print("Guarded inventory verification passed. No live services, permissions or data were changed.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), 30))
