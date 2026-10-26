"""Offline checks for a Kubernetes connection bound to one private store job."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import ssl
import time
from unittest.mock import patch
from uuid import uuid4

import httpx

from app import store_guarded_kubernetes as guarded
from app.store_cluster_scope import StoreClusterScope
from app.store_job_guard import CheckedStoreJob, StoreJobClaimLost, StoreJobGuardUnavailable
from app.store_jobs import StoreJobClaim
from app.store_kube_connection import KubernetesConnection, KubernetesAPIError
from app.store_namespace import StoreNamespace, StoreNamespaceError
from app.store_namespace_api import StoreNamespaceAPI

SCOPE = StoreClusterScope("platform", "platform")
IDENTITY = StoreNamespace(uuid4(), 7)
NOW = datetime.now(timezone.utc)
CLAIM = StoreJobClaim(
    operation_id=uuid4(), store_id=IDENTITY.store_id, owner_id=7,
    action="provision", generation=1, attempt=1, namespace=IDENTITY.name,
    release_name="store", name="Fixture tea", slug="fixture-tea",
    url="http://fixture-tea.localtest.me", deadline_at=NOW + timedelta(minutes=20),
    checked_at=NOW, lease_expires_at=NOW + timedelta(seconds=60), lease_token=uuid4(),
)
NAMESPACE = SCOPE.namespace_manifest(IDENTITY)
NAMESPACE["metadata"].update(uid="namespace-uid", resourceVersion="17")
NAMESPACE["status"] = {"phase": "Active"}
BINDING = SCOPE.role_binding_manifest(IDENTITY, NAMESPACE, expected_uid="namespace-uid")
BINDING["metadata"].update(uid="binding-uid", resourceVersion="3")
NS_ITEM = "/api/v1/namespaces/" + IDENTITY.name
RB_COLLECTION = (
    "/apis/rbac.authorization.k8s.io/v1/namespaces/"
    + IDENTITY.name + "/rolebindings"
)
RB_ITEM = RB_COLLECTION + "/urumi-store-provisioner"
TLS = ssl.create_default_context()


class Fixture:
    def __init__(self, *, uid=None, cleanup=False):
        self.uid, self.cleanup = uid, cleanup
        self.guard_calls = 0
        self.requests = []
        self.before = None
        self.after = None
        self.reply = NAMESPACE
        self.status = 200
        self.remaining = 30
        self.delay = 0
        self.mutate = None

    def factory(self):
        raise AssertionError("Mocked guard must not open a database.")

    async def require(self, factory, claim, *, for_cleanup, expected_namespace_uid):
        self.guard_calls += 1
        assert factory == self.factory and claim == CLAIM
        assert for_cleanup == self.cleanup and expected_namespace_uid == self.uid
        if self.guard_calls == 1 and self.before is not None:
            raise self.before
        if self.guard_calls > 1 and self.after is not None:
            raise self.after
        if self.mutate:
            self.mutate()
            self.mutate = None
        return CheckedStoreJob(
            claim=CLAIM, namespace_uid=self.uid, for_cleanup=self.cleanup,
            stop_at=time.monotonic() + self.remaining,
        )

    async def peer(self, request):
        self.requests.append((
            request.method, request.url.path,
            json.loads(request.content) if request.content else None,
        ))
        if self.delay:
            await asyncio.sleep(self.delay)
        return httpx.Response(self.status, json=deepcopy(self.reply))

    def connection(self, raw):
        return guarded.GuardedKubernetesConnection(
            raw, self.factory, CLAIM, SCOPE,
            namespace_uid=self.uid, for_cleanup=self.cleanup,
        )


async def error(expected, operation):
    try:
        await operation
    except expected as caught:
        return caught
    raise AssertionError(f"Expected {expected.__name__}")


async def scenario(fixture, operation):
    raw = KubernetesConnection(
        lambda: "fixture.worker.signature", TLS,
        transport=httpx.MockTransport(fixture.peer),
    )
    try:
        with patch.object(guarded, "require_claim", fixture.require):
            return await operation(fixture.connection(raw))
    finally:
        await raw.aclose()


async def check():
    fixture = Fixture()
    fixture.status = 201
    body = SCOPE.namespace_manifest(IDENTITY)
    result = await scenario(fixture, lambda connection: connection.request(
        "POST", "/api/v1/namespaces", body=body,
    ))
    assert result == NAMESPACE and fixture.guard_calls == 2
    assert fixture.requests[0][2] == body

    fixture = Fixture(uid="namespace-uid")
    result = await scenario(fixture, lambda connection: StoreNamespaceAPI(
        connection, SCOPE,
    ).ensure_namespace(IDENTITY, expected_uid="namespace-uid"))
    assert result == NAMESPACE and fixture.guard_calls == 2
    print("Namespace helper accepts the guarded interface; every successful request checks twice")

    for failure in (StoreJobClaimLost("fixture"), StoreJobGuardUnavailable("private")):
        fixture = Fixture()
        fixture.before = failure
        stopped = await error(
            guarded.StoreKubernetesStopped,
            scenario(fixture, lambda connection: connection.request("GET", NS_ITEM)),
        )
        assert not stopped.write_may_have_applied and not fixture.requests
    fixture = Fixture()
    fixture.remaining = 0
    await error(
        guarded.StoreKubernetesStopped,
        scenario(fixture, lambda connection: connection.request("GET", NS_ITEM)),
    )
    assert not fixture.requests
    print("Lost claims, unavailable ownership storage and expired allowances send no HTTP")

    for uid, cleanup, method, target, body in (
        (None, False, "GET", "/api/v1/namespaces/urumi-store-" + uuid4().hex, None),
        (None, True, "POST", "/api/v1/namespaces", SCOPE.namespace_manifest(IDENTITY)),
        ("namespace-uid", False, "POST", "/api/v1/namespaces", SCOPE.namespace_manifest(IDENTITY)),
        (None, False, "GET", RB_ITEM, None),
        ("namespace-uid", True, "POST", RB_COLLECTION, SCOPE.role_binding_manifest(
            IDENTITY, NAMESPACE, expected_uid="namespace-uid")),
        ("namespace-uid", False, "DELETE", NS_ITEM, {
            "preconditions": {"uid": "namespace-uid", "resourceVersion": "17"},
            "propagationPolicy": "Foreground",
        }),
        ("namespace-uid", True, "DELETE", NS_ITEM, {
            "preconditions": {"uid": "different-uid", "resourceVersion": "17"},
            "propagationPolicy": "Foreground",
        }),
    ):
        fixture = Fixture(uid=uid, cleanup=cleanup)
        await error(ValueError, scenario(fixture, lambda connection: connection.request(
            method, target, body=body,
        )))
        assert fixture.guard_calls == 0 and not fixture.requests

    fixture = Fixture(uid="namespace-uid")
    fixture.status, fixture.reply = 201, BINDING
    body = SCOPE.role_binding_manifest(IDENTITY, NAMESPACE, expected_uid="namespace-uid")
    await scenario(fixture, lambda connection: connection.request("POST", RB_COLLECTION, body=body))
    bad = deepcopy(body)
    bad["roleRef"]["name"] = "cluster-admin"
    fixture = Fixture(uid="namespace-uid")
    await error(ValueError, scenario(fixture, lambda connection: connection.request(
        "POST", RB_COLLECTION, body=bad,
    )))
    assert not fixture.requests
    fixture = Fixture(uid="namespace-uid", cleanup=True)
    await scenario(fixture, lambda connection: connection.request(
        "DELETE", NS_ITEM, body={
            "preconditions": {"uid": "namespace-uid", "resourceVersion": "17"},
            "propagationPolicy": "Foreground",
        },
    ))
    fixture = Fixture(uid="namespace-uid")
    fixture.reply = deepcopy(NAMESPACE)
    fixture.reply["metadata"]["uid"] = "replacement-uid"
    await error(StoreNamespaceError, scenario(
        fixture, lambda connection: connection.request("GET", NS_ITEM),
    ))
    print("Cross-store routes, unsafe grants, replaced namespaces and invalid modes are rejected")

    for writing in (False, True):
        fixture = Fixture()
        fixture.after = StoreJobClaimLost("fixture")
        fixture.status = 201 if writing else 200
        stopped = await error(guarded.StoreKubernetesStopped, scenario(
            fixture, lambda connection: connection.request(
                "POST" if writing else "GET",
                "/api/v1/namespaces" if writing else NS_ITEM,
                body=SCOPE.namespace_manifest(IDENTITY) if writing else None,
            ),
        ))
        assert stopped.write_may_have_applied == writing
        assert len(fixture.requests) == 1 and fixture.guard_calls == 2
    fixture = Fixture()
    fixture.remaining, fixture.delay, fixture.status = 0.02, 1, 201
    stopped = await error(KubernetesAPIError, scenario(
        fixture, lambda connection: connection.request(
            "POST", "/api/v1/namespaces", body=SCOPE.namespace_manifest(IDENTITY),
        ),
    ))
    assert stopped.write_may_have_applied and len(fixture.requests) == 1
    print("Lost ownership after HTTP and request timeouts preserve uncertain-write status")

    fixture = Fixture()
    fixture.before = asyncio.CancelledError()
    await error(asyncio.CancelledError, scenario(
        fixture, lambda connection: connection.request("GET", NS_ITEM),
    ))
    assert not fixture.requests

    fixture = Fixture()
    fixture.delay, fixture.status = 1, 201
    task = asyncio.create_task(scenario(fixture, lambda connection: connection.request(
        "POST", "/api/v1/namespaces", body=SCOPE.namespace_manifest(IDENTITY),
    )))
    try:
        for _ in range(100):
            if fixture.requests:
                break
            await asyncio.sleep(0.01)
        assert len(fixture.requests) == 1
        task.cancel()
        await error(asyncio.CancelledError, task)
        assert fixture.guard_calls == 1 and len(fixture.requests) == 1
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    fixture = Fixture()
    fixture.status = 201
    body = SCOPE.namespace_manifest(IDENTITY)
    expected = deepcopy(body)
    fixture.mutate = lambda: body["metadata"]["labels"].update({"urumi.ai/owner-id": "99"})
    await scenario(fixture, lambda connection: connection.request(
        "POST", "/api/v1/namespaces", body=body,
    ))
    assert fixture.requests[0][2] == expected
    print("Cancellation propagates; caller mutations cannot alter a checked write")
    print("Guarded Kubernetes fixture checks passed. No live services were called.")


if __name__ == "__main__":
    asyncio.run(check())
