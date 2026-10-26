"""Offline Kubernetes transport tests; all HTTP responses are mocked."""

from copy import deepcopy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import json
import ssl
from unittest.mock import patch
from uuid import uuid4

import httpx

from app.store_cluster_scope import StoreClusterScope
from app.store_credentials import StoreCredentials
from app.store_secret import StoreSecret
from app.store_namespace import StoreNamespace
from app.store_kube_connection import KubernetesConnection, KubernetesAPIError

scope = StoreClusterScope("platform", "platform")
identity = StoreNamespace(uuid4(), 1)
path = "/api/v1/namespaces/" + identity.name
binding_path = (
    "/apis/rbac.authorization.k8s.io/v1/namespaces/"
    + identity.name + "/rolebindings"
)
document = scope.namespace_manifest(identity)
document["metadata"].update(uid="test-uid", resourceVersion="4")
document["status"] = {"phase": "Active"}
binding = scope.role_binding_manifest(identity, document, expected_uid="test-uid")
deleted = scope.namespace_delete_options(identity, document, expected_uid="test-uid")


async def check():
    requests = []
    mode = {"status": 200, "content": None, "error": None}
    tokens = iter(("fixture.first.signature", "fixture.second.signature"))
    reader = lambda: next(tokens)

    def handler(request):
        requests.append(request)
        assert request.url.scheme == "https"
        assert request.url.host == "kubernetes.default.svc"
        if mode["error"] is not None:
            raise mode["error"]
        if mode["content"] is not None:
            return httpx.Response(mode["status"], content=mode["content"])
        return httpx.Response(
            mode["status"], json=document,
            headers={"location": "https://example.com/escape"},
        )

    connection = KubernetesConnection(
        reader, ssl.create_default_context(),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await connection.request("GET", path) == document
        assert await connection.request("GET", path) == document
        assert requests[0].headers["authorization"] == "Bearer fixture.first.signature"
        assert requests[1].headers["authorization"] == "Bearer fixture.second.signature"
        connection._token_reader = lambda: "fixture.third.signature"

        for method, target, body in (
            ("GET", "https://example.com/", None),
            ("GET", "/api/v1/namespaces/platform", None),
            ("GET", path + "?watch=1", None),
            ("GET", path + "/../platform", None),
            ("PATCH", path, {}),
            ("GET", "/api/v1/namespaces/urumi-store-" + "0"*32, None),
            ("GET", binding_path.replace(identity.store_id.hex, "0"*32) + "/urumi-store-provisioner", None),
            ("POST", "/api/v1/namespaces", {"kind": "Namespace"}),
            ("DELETE", path, {}),
            ("DELETE", path, {"preconditions": {"uid": "x"}, "propagationPolicy": "Foreground"}),
        ):
            before = len(requests)
            try:
                await connection.request(method, target, body=body)
            except ValueError:
                pass
            else:
                raise AssertionError("Unsafe route/body was accepted")
            assert len(requests) == before
        print("Fixed origin, route restrictions and fresh token reads passed")

        for method, target, body, status in (
            ("POST", "/api/v1/namespaces", scope.namespace_manifest(identity), 201),
            ("POST", binding_path, binding, 201),
            ("DELETE", path, deleted, 202),
            ("GET", binding_path + "/urumi-store-provisioner", None, 200),
        ):
            mode["status"] = status
            assert await connection.request(method, target, body=body) == document
            if body is not None:
                assert json.loads(requests[-1].content) == body
        print("Namespace, fixed RoleBinding and fenced deletion requests passed")

        secret_collection = path + "/secrets"
        secret_item = secret_collection + "/store-secrets"
        credentials = StoreCredentials.generate(
            identity, admin_email="owner@example.test",
        )
        secret_body = StoreSecret(identity, scope, "test-uid").manifest(credentials)
        secret_response = deepcopy(secret_body)
        secret_response["metadata"].update(uid="secret-uid", resourceVersion="5")
        mode.update(
            status=200, content=json.dumps(secret_response).encode("utf-8"),
            error=None,
        )
        result = await connection.request("GET", secret_item)
        assert result == secret_response
        mode["status"] = 201
        result = await connection.request("POST", secret_collection, body=secret_body)
        assert result == secret_response
        assert json.loads(requests[-1].content) == secret_body

        rejected = [
            ("GET", secret_collection, None),
            ("GET", secret_collection + "/another-secret", None),
            ("GET", secret_item + "?watch=1", None),
            ("GET", secret_item.replace(identity.name, "platform"), None),
            ("GET", secret_item.replace(identity.store_id.hex, "0" * 32), None),
            ("GET", secret_item, {}),
            ("PATCH", secret_item, {}),
            ("PUT", secret_item, secret_body),
            ("DELETE", secret_item, {}),
        ]
        for key, value in (
            ("apiVersion", "v2"), ("kind", "ConfigMap"),
            ("type", "kubernetes.io/service-account-token"),
            ("immutable", False), ("immutable", 1), ("data", []),
            ("stringData", {}),
        ):
            altered = deepcopy(secret_body)
            altered[key] = value
            rejected.append(("POST", secret_collection, altered))
        for key, value in (
            ("name", "another-secret"), ("namespace", "platform"),
        ):
            altered = deepcopy(secret_body)
            altered["metadata"][key] = value
            rejected.append(("POST", secret_collection, altered))
        for method, target, body in rejected:
            before = len(requests)
            try:
                await connection.request(method, target, body=body)
            except ValueError:
                pass
            else:
                raise AssertionError("An unsafe Secret operation was accepted.")
            assert len(requests) == before

        mode.update(status=404, content=None)
        assert await connection.request("GET", secret_item) is None
        mode.update(
            status=409,
            content=b"private Secret diagnostic fixture.third.signature",
        )
        before = len(requests)
        try:
            await connection.request("POST", secret_collection, body=secret_body)
        except KubernetesAPIError as error:
            assert error.status_code == 409 and not error.write_may_have_applied
            assert "private Secret" not in str(error)
            assert "signature" not in str(error)
        else:
            raise AssertionError("A Secret creation conflict was accepted.")
        assert len(requests) == before + 1

        mode.update(status=201, content=None, error=httpx.ReadTimeout("private Secret"))
        before = len(requests)
        try:
            await connection.request("POST", secret_collection, body=secret_body)
        except KubernetesAPIError as error:
            assert error.write_may_have_applied and "private Secret" not in str(error)
        else:
            raise AssertionError("An uncertain Secret write was accepted.")
        assert len(requests) == before + 1
        mode.update(status=200, content=None, error=None)
        print("Fixed Secret reads/creation, blocked methods, safe conflicts and uncertain writes passed")

        for status in (301, 302, 307, 308, 401, 403, 409, 429, 500):
            mode.update(status=status, content=b"private upstream error fixture.first.signature")
            before = len(requests)
            try:
                await connection.request("POST", "/api/v1/namespaces", body=scope.namespace_manifest(identity))
            except KubernetesAPIError as error:
                assert error.status_code == status
                assert error.write_may_have_applied == (status >= 500)
                assert "private upstream" not in str(error)
                assert "signature" not in str(error)
            else:
                raise AssertionError("Unexpected API status accepted")
            assert len(requests) == before + 1
        print("Redirects, authorization failures, conflicts and safe errors passed")

        mode.update(status=404, content=None)
        assert await connection.request("GET", path) is None
        assert await connection.request("DELETE", path, body=deleted) is None

        for content in (b"not json", b"[]", b"x" * (1024*1024 + 1)):
            mode.update(status=200, content=content)
            try:
                await connection.request("GET", path)
            except KubernetesAPIError as error:
                assert not error.write_may_have_applied
            else:
                raise AssertionError("Invalid response accepted")

        for error in (httpx.ReadTimeout("private"), httpx.WriteError("private"), TimeoutError("private")):
            mode.update(error=error, content=None, status=201)
            before = len(requests)
            try:
                await connection.request("POST", "/api/v1/namespaces", body=scope.namespace_manifest(identity))
            except KubernetesAPIError as result:
                assert result.write_may_have_applied and "private" not in str(result)
            else:
                raise AssertionError("Uncertain write was accepted")
            assert len(requests) == before + 1
        print("Missing resources, bounded responses and uncertain writes passed")

        original_timeout = asyncio.timeout

        async def slow_handler(request):
            requests.append(request)
            await asyncio.sleep(1)
            return httpx.Response(201, json=document)

        timed = KubernetesConnection(
            lambda: "fixture.timeout.signature", ssl.create_default_context(),
            transport=httpx.MockTransport(slow_handler),
        )
        try:
            before = len(requests)
            with patch(
                "app.store_kube_connection.asyncio.timeout",
                lambda seconds: original_timeout(0.01),
            ):
                try:
                    await timed.request(
                        "POST", "/api/v1/namespaces",
                        body=scope.namespace_manifest(identity),
                    )
                except KubernetesAPIError as error:
                    assert error.write_may_have_applied
                else:
                    raise AssertionError("Timed-out write was accepted")
            assert len(requests) == before + 1
        finally:
            await timed.aclose()
        print("The overall timeout stops an uncertain write without retrying")

        mode["error"] = asyncio.CancelledError()
        try:
            await connection.request("GET", path)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Cancellation swallowed")

        mode.update(error=None, content=None, status=200)
        for token in ("", "one-line", "bad\nheader.signature.value", "a" * 16385):
            connection._token_reader = lambda: token
            before = len(requests)
            try:
                await connection.request("GET", path)
            except KubernetesAPIError as error:
                assert not error.write_may_have_applied
            else:
                raise AssertionError("Invalid token accepted")
            assert len(requests) == before
        print("Cancellation propagates; invalid credentials send no HTTP")
    finally:
        await connection.aclose()
    assert connection._client.is_closed


if __name__ == "__main__":
    asyncio.run(check())
    print("Kubernetes connection verification passed. No live services were called.")
