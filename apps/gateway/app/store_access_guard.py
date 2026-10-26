"""Verify the exact public maintenance gate through a live provisioning claim."""

import asyncio
from urllib.parse import urlsplit

from app.store_guarded_kubernetes import GuardedKubernetesConnection, StoreKubernetesStopped
from app.store_guarded_secrets import GuardedStoreSecretConnection
from app.store_job_guard import StoreJobClaimLost, StoreJobGuardUnavailable, require_claim
from app.store_kube_connection import KubernetesAPIError, _check_request
from app.store_namespace import _opaque
from app.store_secret_api import StoreSecretAPI

ANNOTATION = "traefik.ingress.kubernetes.io/router.middlewares"


def gate_address(identity, scope):
    return (
        f"http://gateway.{scope.platform_namespace}.svc.cluster.local:8000"
        f"/api/store-access/{identity.store_id}"
    )


def gate_paths(identity):
    return (
        f"/apis/networking.k8s.io/v1/namespaces/{identity.name}/ingresses/wordpress",
        f"/apis/traefik.io/v1alpha1/namespaces/{identity.name}/middlewares/store-access",
    )


def check_gate_documents(identity, scope, host, ingress, middleware):
    """Pure identity/spec validation, also used by the administrator setup script."""
    if not isinstance(host, str) or not host or type(ingress) is not dict or type(middleware) is not dict:
        raise ValueError("Store maintenance gate is unavailable.")
    for document, api, kind, name in (
        (ingress, "networking.k8s.io/v1", "Ingress", "wordpress"),
        (middleware, "traefik.io/v1alpha1", "Middleware", "store-access"),
    ):
        meta = document.get("metadata", {})
        if (document.get("apiVersion") != api or document.get("kind") != kind
                or meta.get("namespace") != identity.name or meta.get("name") != name
                or not _opaque(meta.get("uid")) or not _opaque(meta.get("resourceVersion"))
                or meta.get("deletionTimestamp")):
            raise ValueError("Store maintenance gate identity changed.")
    annotations = ingress["metadata"].get("annotations", {})
    if annotations.get(ANNOTATION) != f"{identity.name}-store-access@kubernetescrd":
        raise ValueError("Store ingress does not use its maintenance gate.")
    rules = ingress.get("spec", {}).get("rules")
    expected = [{
        "host": host,
        "http": {"paths": [{
            "path": "/", "pathType": "Prefix",
            "backend": {"service": {"name": "wordpress", "port": {"number": 80}}},
        }]},
    }]
    if rules != expected:
        raise ValueError("Store ingress route changed.")
    if middleware.get("spec") != {"forwardAuth": {
        "address": gate_address(identity, scope),
        "trustForwardHeader": False,
    }}:
        raise ValueError("Store maintenance gate destination changed.")
    return True


class _GateReads(GuardedKubernetesConnection):
    def __init__(self, connection, factory, claim, scope, *, namespace_uid, credentials_secret_uid):
        if not _opaque(namespace_uid) or not _opaque(credentials_secret_uid):
            raise ValueError("Both saved resource identities are required.")
        super().__init__(connection, factory, claim, scope, namespace_uid=namespace_uid)
        self._secret_uid = credentials_secret_uid

    def _check_target(self, method, path, body):
        _check_request(method, path, body)
        if method != "GET" or body is not None or path not in gate_paths(self._identity):
            raise ValueError("This route is outside the store maintenance gate.")

    async def _checked(self):
        return await require_claim(
            self._factory, self._claim, expected_namespace_uid=self._uid,
            expected_credentials_secret_uid=self._secret_uid,
        )


async def verify_store_access_gate(
    factory, claim, connection, scope, *, namespace_uid, credentials_secret_uid,
):
    reader = _GateReads(
        connection, factory, claim, scope, namespace_uid=namespace_uid,
        credentials_secret_uid=credentials_secret_uid,
    )
    ownership = StoreSecretAPI(GuardedStoreSecretConnection(
        connection, factory, claim, scope, namespace_uid=namespace_uid,
        credentials_secret_uid=credentials_secret_uid,
    ))
    try:
        async with asyncio.timeout(30):
            checked = await reader._checked()
            async with asyncio.timeout(max(0, min(30, checked.remaining_seconds))):
                await ownership.read_secret()
                ingress_path, middleware_path = gate_paths(reader._identity)
                ingress = await reader.request("GET", ingress_path)
                middleware = await reader.request("GET", middleware_path)
                result = check_gate_documents(
                    reader._identity, scope, urlsplit(claim.url).hostname,
                    ingress, middleware,
                )
                await ownership.read_secret()
                await reader._checked()
                if checked.remaining_seconds <= 0:
                    raise StoreJobClaimLost("Store gate observation expired.")
                return result
    except (StoreJobClaimLost, StoreJobGuardUnavailable):
        raise StoreKubernetesStopped(write_may_have_applied=False) from None
    except TimeoutError:
        raise KubernetesAPIError("Store maintenance gate check timed out.") from None
