"""Private, fixed-route Kubernetes transport; no worker starts on import."""

from abc import ABC, abstractmethod
import asyncio
from contextlib import aclosing
import json
from pathlib import Path
import re
import ssl

import httpx

from app.store_volumes import _volume_name
from app.store_volume_pages import is_volume_page_path


_ORIGIN = "https://kubernetes.default.svc"
_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
_MAX_BYTES = 1024 * 1024
_NAMESPACE = r"urumi-store-(?!0{32}(?:/|$))[0-9a-f]{32}"
_NAMESPACE_ITEM = re.compile(rf"/api/v1/namespaces/({_NAMESPACE})")
_BINDING_BASE = (
    r"/apis/rbac\.authorization\.k8s\.io/v1/namespaces/"
    rf"({_NAMESPACE})/rolebindings"
)
_BINDING_COLLECTION = re.compile(_BINDING_BASE)
_BINDING_ITEM = re.compile(_BINDING_BASE + r"/urumi-store-provisioner")
_SECRET_BASE = rf"/api/v1/namespaces/({_NAMESPACE})/secrets"
_SECRET_COLLECTION = re.compile(_SECRET_BASE)
_SECRET_ITEM = re.compile(_SECRET_BASE + r"/store-secrets")
_HELM_RELEASE_ITEM = re.compile(
    _SECRET_BASE + r"/sh\.helm\.release\.v1\.store\.v[1-8]"
)
_WORKLOAD_ITEM = re.compile(
    rf"/apis/(?:apps/v1/namespaces/{_NAMESPACE}/"
    r"(?:statefulsets/mysql|deployments/(?:wordpress|woo-mcp|mcp-analytics))"
    rf"|batch/v1/namespaces/{_NAMESPACE}/jobs/store-bootstrap)"
)
_ACCESS_ITEM = re.compile(
    rf"/apis/(?:networking\.k8s\.io/v1/namespaces/{_NAMESPACE}/ingresses/wordpress"
    rf"|traefik\.io/v1alpha1/namespaces/{_NAMESPACE}/middlewares/store-access)"
)
_CLAIM_ITEM = re.compile(
    rf"/api/v1/namespaces/{_NAMESPACE}/persistentvolumeclaims/"
    r"(?:data-mysql-0|wordpress-data)"
)
_VOLUME_PREFIX = "/api/v1/persistentvolumes/"
_TOKEN = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


class KubernetesAPIError(RuntimeError):
    def __init__(self, message, *, status_code=None, write_may_have_applied=False):
        super().__init__(message)
        self.status_code = status_code
        self.write_may_have_applied = write_may_have_applied


def _token_file(path):
    def read():
        with Path(path).open("rb") as handle:
            return handle.read(16385).decode("ascii").strip()
    return read


def _check_request(method, path, body):
    if not isinstance(path, str):
        raise ValueError("Invalid Kubernetes API route.")
    namespace = _NAMESPACE_ITEM.fullmatch(path)
    bindings = _BINDING_COLLECTION.fullmatch(path)
    binding = _BINDING_ITEM.fullmatch(path)
    secrets = _SECRET_COLLECTION.fullmatch(path)
    secret = _SECRET_ITEM.fullmatch(path)
    helm_release = _HELM_RELEASE_ITEM.fullmatch(path)
    workload = _WORKLOAD_ITEM.fullmatch(path)
    claim = _CLAIM_ITEM.fullmatch(path)
    access = _ACCESS_ITEM.fullmatch(path)
    volume = (
        path.startswith(_VOLUME_PREFIX)
        and _volume_name(path[len(_VOLUME_PREFIX):])
    )
    if (
        method == "GET" and body is None
        and (
            namespace or binding or secret or workload or claim or volume or access
            or helm_release
            or is_volume_page_path(path)
        )
    ):
        return
    if method == "PATCH" and helm_release and type(body) is list and len(body) == 6:
        expected = (
            ("test", "/metadata/uid"), ("test", "/metadata/resourceVersion"),
            ("test", "/data/release"), ("test", "/metadata/labels/status"),
            ("replace", "/data/release"), ("replace", "/metadata/labels/status"),
        )
        if all(
            type(item) is dict and set(item) == {"op", "path", "value"}
            and (item["op"], item["path"]) == pair
            and type(item["value"]) is str and 0 < len(item["value"]) <= _MAX_BYTES
            for item, pair in zip(body, expected)
        ) and (
            body[3]["value"] in ("pending-install", "pending-upgrade")
            and body[5]["value"] == "failed"
        ):
            return
    if method == "POST" and isinstance(body, dict):
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("Kubernetes object metadata is required.")
        if (
            path == "/api/v1/namespaces"
            and body.get("apiVersion") == "v1"
            and body.get("kind") == "Namespace"
            and isinstance(metadata.get("name"), str)
            and re.fullmatch(_NAMESPACE, metadata["name"])
            and metadata.get("namespace") in (None, "")
        ):
            return
        if (
            bindings
            and body.get("apiVersion") == "rbac.authorization.k8s.io/v1"
            and body.get("kind") == "RoleBinding"
            and metadata.get("namespace") == bindings.group(1)
            and metadata.get("name") == "urumi-store-provisioner"
        ):
            return
        if (
            secrets
            and body.get("apiVersion") == "v1"
            and body.get("kind") == "Secret"
            and metadata.get("namespace") == secrets.group(1)
            and metadata.get("name") == "store-secrets"
            and body.get("type") == "Opaque"
            and body.get("immutable") is True
            and isinstance(body.get("data"), dict)
            and "stringData" not in body
        ):
            return
    if method == "DELETE" and namespace and isinstance(body, dict):
        preconditions = body.get("preconditions")
        if (
            set(body) == {"preconditions", "propagationPolicy"}
            and body.get("propagationPolicy") == "Foreground"
            and isinstance(preconditions, dict)
            and set(preconditions) == {"uid", "resourceVersion"}
            and all(
                isinstance(value, str) and 1 <= len(value) <= 256
                and all("!" <= character <= "~" for character in value)
                for value in preconditions.values()
            )
        ):
            return
    raise ValueError("This Kubernetes operation is not permitted by the client.")


class KubernetesRequester(ABC):
    @abstractmethod
    async def request(self, method, path, *, body=None):
        raise NotImplementedError


class KubernetesConnection(KubernetesRequester):
    def __init__(self, token_reader, tls_context, *, transport=None):
        if (
            not callable(token_reader)
            or not isinstance(tls_context, ssl.SSLContext)
            or tls_context.verify_mode != ssl.CERT_REQUIRED
            or not tls_context.check_hostname
        ):
            raise ValueError("Verified Kubernetes TLS and a token reader are required.")
        self._token_reader = token_reader
        self._client = httpx.AsyncClient(
            base_url=_ORIGIN,
            verify=tls_context,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(10, connect=5, pool=5),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            transport=transport,
        )

    @classmethod
    def in_cluster(cls, *, token_path=_TOKEN_PATH, ca_path=_CA_PATH):
        try:
            context = ssl.create_default_context(cafile=str(ca_path))
        except (OSError, ssl.SSLError, ValueError):
            raise KubernetesAPIError(
                "The Kubernetes CA certificate is unavailable.",
            ) from None
        return cls(_token_file(token_path), context)

    async def aclose(self):
        await self._client.aclose()

    async def request(self, method, path, *, body=None):
        """Fixed namespace operations and read-only workload/storage routes."""
        _check_request(method, path, body)
        try:
            payload = None if body is None else json.dumps(
                body, ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ValueError("Invalid Kubernetes request body.") from None
        if payload is not None and len(payload) > _MAX_BYTES:
            raise ValueError("The Kubernetes request body is too large.")
        try:
            token = self._token_reader()
            if (
                not isinstance(token, str)
                or not 1 <= len(token) <= 16384
                or not _TOKEN.fullmatch(token)
            ):
                raise ValueError("Invalid worker token.")
        except Exception:
            raise KubernetesAPIError(
                "The worker Kubernetes credential is unavailable.",
            ) from None

        writing = method != "GET"
        status = None
        try:
            async with asyncio.timeout(15):
                async with self._client.stream(
                    method, path,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                        "Content-Type": (
                            "application/json-patch+json" if method == "PATCH"
                            else "application/json"
                        ),
                    },
                    content=payload,
                ) as response:
                    status = response.status_code
                    if status == 404 and method in ("GET", "DELETE"):
                        return None
                    accepted = {
                        "GET": {200}, "POST": {201}, "DELETE": {200, 202},
                        "PATCH": {200},
                    }[method]
                    if status not in accepted:
                        raise KubernetesAPIError(
                            "Kubernetes rejected the operation.",
                            status_code=status,
                            write_may_have_applied=(
                                writing and (status >= 500 or status == 408)
                            ),
                        )
                    content = bytearray()
                    async with aclosing(response.aiter_bytes()) as chunks:
                        async for chunk in chunks:
                            if len(content) + len(chunk) > _MAX_BYTES:
                                raise ValueError("Oversized API response.")
                            content.extend(chunk)
                    document = json.loads(content)
                    if not isinstance(document, dict):
                        raise ValueError("Invalid API response.")
                    return document
        except KubernetesAPIError:
            raise
        except (
            httpx.HTTPError, TimeoutError, ValueError, UnicodeError, RuntimeError,
        ):
            raise KubernetesAPIError(
                "The Kubernetes operation could not be confirmed.",
                status_code=status,
                write_may_have_applied=writing,
            ) from None
