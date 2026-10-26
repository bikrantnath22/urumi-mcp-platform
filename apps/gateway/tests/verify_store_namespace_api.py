"""Owned namespace API tests; HTTP is mocked and no database is used."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
from copy import deepcopy
import json
import ssl
from uuid import uuid4

import httpx

from app.store_cluster_scope import StoreClusterScope
from app.store_kube_connection import KubernetesAPIError, KubernetesConnection
from app.store_namespace import StoreNamespace, StoreNamespaceError
from app.store_namespace_api import (
    StoreNamespaceAPI, StoreNamespacePending, _binding_check,
)

SCOPE = StoreClusterScope("platform", "platform")
IDENTITY = StoreNamespace(uuid4(), 7)
NS_ITEM = "/api/v1/namespaces/" + IDENTITY.name
RB_COLLECTION = (
    "/apis/rbac.authorization.k8s.io/v1/namespaces/"
    + IDENTITY.name + "/rolebindings"
)
RB_ITEM = RB_COLLECTION + "/urumi-store-provisioner"
NAMESPACE = SCOPE.namespace_manifest(IDENTITY)
NAMESPACE["metadata"].update(uid="namespace-uid", resourceVersion="17")
NAMESPACE["status"] = {"phase": "Active"}
BINDING = SCOPE.role_binding_manifest(
    IDENTITY, NAMESPACE, expected_uid="namespace-uid",
)
BINDING["metadata"].update(uid="binding-uid", resourceVersion="3")
TLS = ssl.create_default_context()


async def scenario(replies, action, *, error=None):
    remaining = list(replies)
    sent = []

    def peer(request):
        assert remaining, "An unexpected additional API request was sent."
        method, path, status, body = remaining.pop(0)
        assert (request.method, request.url.path) == (method, path)
        sent.append((
            method, path, json.loads(request.content) if request.content else None,
        ))
        if isinstance(status, BaseException):
            raise status
        return httpx.Response(status, json=deepcopy(body))

    connection = KubernetesConnection(
        lambda: "fixture.worker.signature", TLS,
        transport=httpx.MockTransport(peer),
    )
    api = StoreNamespaceAPI(connection, SCOPE)
    result = None
    caught = None
    try:
        try:
            result = await action(api)
        except BaseException as exc:
            if error is None or not isinstance(exc, error):
                raise
            caught = exc
        else:
            assert error is None, "An expected rejection did not occur."
        assert not remaining, "Required ownership checks were skipped."
    finally:
        await connection.aclose()
    return result, sent, caught


async def check():
    ensure = lambda api: api.ensure_namespace(IDENTITY, expected_uid=None)
    saved = lambda api: api.ensure_namespace(
        IDENTITY, expected_uid="namespace-uid",
    )
    bind = lambda api: api.ensure_role_binding(
        IDENTITY, expected_uid="namespace-uid",
    )
    remove = lambda api: api.delete_namespace(
        IDENTITY, expected_uid="namespace-uid",
    )
    get_ns = ("GET", NS_ITEM, 200, NAMESPACE)
    missing_ns = ("GET", NS_ITEM, 404, {})
    get_rb = ("GET", RB_ITEM, 200, BINDING)
    missing_rb = ("GET", RB_ITEM, 404, {})

    result, sent, _ = await scenario(
        [missing_ns, ("POST", "/api/v1/namespaces", 201, NAMESPACE)], ensure,
    )
    assert result == NAMESPACE
    assert sent[-1][2] == SCOPE.namespace_manifest(IDENTITY)
    _, sent, _ = await scenario([get_ns], ensure)
    assert all(method == "GET" for method, _, _ in sent)
    await scenario([get_ns], saved)
    await scenario(
        [missing_ns, ("POST", "/api/v1/namespaces", 409, {}), get_ns], ensure,
    )
    await scenario(
        [missing_ns, ("POST", "/api/v1/namespaces", 409, {}), missing_ns],
        ensure, error=StoreNamespacePending,
    )
    await scenario([missing_ns], saved, error=StoreNamespaceError)
    print("Namespace creation, owned reuse, conflicts and missing saved UIDs passed")

    for field, key in (
        ("labels", "urumi.ai/owner-id"),
        ("labels", "urumi.ai/store-id"),
        ("labels", "pod-security.kubernetes.io/enforce"),
        ("annotations", "urumi.ai/platform-release"),
        ("annotations", "urumi.ai/platform-namespace"),
    ):
        changed = deepcopy(NAMESPACE)
        changed["metadata"][field][key] = "wrong"
        await scenario(
            [("GET", NS_ITEM, 200, changed)], ensure, error=StoreNamespaceError,
        )
    replaced = deepcopy(NAMESPACE)
    replaced["metadata"]["uid"] = "replacement-uid"
    await scenario(
        [("GET", NS_ITEM, 200, replaced)], saved, error=StoreNamespaceError,
    )
    await scenario(
        [], lambda api: api.ensure_namespace(IDENTITY, expected_uid=""),
        error=StoreNamespaceError,
    )
    print("Wrong stores, owners, platform scope, Pod Security and replacements reject reuse")

    _, sent, _ = await scenario(
        [get_ns, missing_rb, get_ns, ("POST", RB_COLLECTION, 201, BINDING), get_ns],
        bind,
    )
    expected = SCOPE.role_binding_manifest(
        IDENTITY, NAMESPACE, expected_uid="namespace-uid",
    )
    assert [body for method, _, body in sent if method == "POST"] == [expected]
    _, sent, _ = await scenario([get_ns, get_rb, get_ns], bind)
    assert all(method == "GET" for method, _, _ in sent)
    await scenario(
        [get_ns, missing_rb, get_ns, ("POST", RB_COLLECTION, 409, {}), get_rb, get_ns],
        bind,
    )
    await scenario(
        [get_ns, missing_rb, get_ns, ("POST", RB_COLLECTION, 409, {}), missing_rb],
        bind, error=StoreNamespacePending,
    )
    inactive = deepcopy(NAMESPACE)
    inactive["status"]["phase"] = "Pending"
    await scenario(
        [("GET", NS_ITEM, 200, inactive)], bind, error=StoreNamespacePending,
    )
    print("Fixed RoleBinding creation/reuse, conflict checks and active namespace requirement passed")

    expected = SCOPE.role_binding_manifest(
        IDENTITY, NAMESPACE, expected_uid="namespace-uid",
    )
    for change in (
        lambda doc: doc["metadata"].update(namespace="platform"),
        lambda doc: doc["metadata"].update(uid=None),
        lambda doc: doc["metadata"].update(deletionTimestamp="2026-10-18T00:00:00Z"),
        lambda doc: doc["metadata"]["labels"].update({"urumi.ai/owner-id": "99"}),
        lambda doc: doc["metadata"]["annotations"].update({"urumi.ai/platform-release": "other"}),
        lambda doc: doc["roleRef"].update(name="cluster-admin"),
        lambda doc: doc["subjects"][0].update(name="another-worker"),
        lambda doc: doc["subjects"][0].update(namespace="store"),
        lambda doc: doc["subjects"][0].update(apiGroup="rbac.authorization.k8s.io"),
        lambda doc: doc["subjects"].append(deepcopy(doc["subjects"][0])),
        lambda doc: doc.update(subjects=[]),
    ):
        changed = deepcopy(BINDING)
        change(changed)
        try:
            _binding_check(changed, expected)
        except StoreNamespaceError:
            pass
        else:
            raise AssertionError("An unsafe worker binding was accepted.")
        await scenario(
            [get_ns, ("GET", RB_ITEM, 200, changed)],
            bind, error=StoreNamespaceError,
        )
    await scenario(
        [get_ns, missing_rb, ("GET", NS_ITEM, 200, replaced)],
        bind, error=StoreNamespaceError,
    )
    await scenario(
        [get_ns, missing_rb, get_ns, ("POST", RB_COLLECTION, 201, BINDING),
         ("GET", NS_ITEM, 200, replaced)],
        bind, error=StoreNamespaceError,
    )
    print("Altered bindings are never repaired/adopted; namespace replacements block success")

    result, sent, _ = await scenario(
        [get_ns, ("DELETE", NS_ITEM, 200, {"kind": "Status"})], remove,
    )
    assert result is False
    assert sent[-1][2] == {
        "preconditions": {"uid": "namespace-uid", "resourceVersion": "17"},
        "propagationPolicy": "Foreground",
    }
    result, _, _ = await scenario([missing_ns], remove)
    assert result is True
    terminating = deepcopy(NAMESPACE)
    terminating["metadata"]["deletionTimestamp"] = "2026-10-18T00:00:00Z"
    terminating["status"]["phase"] = "Terminating"
    result, sent, _ = await scenario(
        [("GET", NS_ITEM, 200, terminating)], remove,
    )
    assert result is False and len(sent) == 1
    await scenario(
        [("GET", NS_ITEM, 200, replaced)], remove, error=StoreNamespaceError,
    )
    await scenario(
        [], lambda api: api.delete_namespace(IDENTITY, expected_uid=None),
        error=StoreNamespaceError,
    )
    _, _, caught = await scenario(
        [get_ns, ("DELETE", NS_ITEM, 409, {})], remove, error=KubernetesAPIError,
    )
    assert caught.status_code == 409
    print("Deletion uses fresh UID/version checks; accepted deletion is not absence")

    _, _, caught = await scenario(
        [missing_ns, ("POST", "/api/v1/namespaces", 500, {})],
        ensure, error=KubernetesAPIError,
    )
    assert caught.write_may_have_applied
    _, _, caught = await scenario(
        [get_ns, missing_rb, get_ns,
         ("POST", RB_COLLECTION, httpx.ReadTimeout("fixture"), None)],
        bind, error=KubernetesAPIError,
    )
    assert caught.write_may_have_applied
    await scenario(
        [missing_ns, ("POST", "/api/v1/namespaces", asyncio.CancelledError(), None)],
        ensure, error=asyncio.CancelledError,
    )
    print("Uncertain writes and cancellation propagate without another write")
    print("Owned namespace API verification passed. No live services were called.")


if __name__ == "__main__":
    asyncio.run(check())
