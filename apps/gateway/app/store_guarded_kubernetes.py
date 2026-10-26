"""Bind a shared Kubernetes connection to one checked private worker job."""

import asyncio
from copy import deepcopy

from app.store_cluster_scope import StoreClusterScope
from app.store_job_guard import (
    StoreJobClaimLost, StoreJobGuardUnavailable, require_claim,
)
from app.store_jobs import StoreJobClaim
from app.store_kube_connection import (
    KubernetesAPIError, KubernetesConnection, KubernetesRequester, _check_request,
)
from app.store_namespace import _opaque, namespace_for


class StoreKubernetesStopped(StoreJobClaimLost):
    def __init__(self, *, write_may_have_applied):
        super().__init__("The worker claim could not be confirmed; Kubernetes work stopped.")
        self.write_may_have_applied = write_may_have_applied


class GuardedKubernetesConnection(KubernetesRequester):
    """A per-job view; the worker owns and closes the shared raw connection."""

    def __init__(
        self, connection, session_factory, claim, scope, *,
        namespace_uid, for_cleanup=False,
    ):
        if (
            not isinstance(connection, KubernetesConnection)
            or not callable(session_factory)
            or not isinstance(claim, StoreJobClaim)
            or not isinstance(scope, StoreClusterScope)
            or type(for_cleanup) is not bool
            or (namespace_uid is not None and not _opaque(namespace_uid))
        ):
            raise ValueError("Invalid guarded Kubernetes configuration.")
        if claim.action not in ("provision", "delete") or (
            claim.action == "delete" and not for_cleanup
        ):
            raise ValueError("Delete work requires cleanup mode.")
        self._connection = connection
        self._factory = session_factory
        self._claim = claim
        self._scope = scope
        self._identity = namespace_for(claim)
        self._uid = namespace_uid
        self._cleanup = for_cleanup

    def _check_target(self, method, path, body):
        _check_request(method, path, body)
        namespace_item = f"/api/v1/namespaces/{self._identity.name}"
        binding_collection = (
            "/apis/rbac.authorization.k8s.io/v1/namespaces/"
            f"{self._identity.name}/rolebindings"
        )
        if path == "/api/v1/namespaces" and method == "POST":
            if (
                not self._cleanup and self._uid is None
                and body == self._scope.namespace_manifest(self._identity)
            ):
                return
        elif path == namespace_item:
            if method == "GET":
                return
            if (
                method == "DELETE" and self._cleanup and self._uid is not None
                and body["preconditions"]["uid"] == self._uid
            ):
                return
        elif self._uid is not None:
            if path == binding_collection + "/urumi-store-provisioner" and method == "GET":
                return
            if path == binding_collection and method == "POST" and not self._cleanup:
                metadata = body["metadata"]
                labels = metadata.get("labels")
                annotations = metadata.get("annotations")
                if (
                    isinstance(labels, dict)
                    and isinstance(annotations, dict)
                    and all(
                        labels.get(key) == value
                        for key, value in self._identity.labels.items()
                    )
                    and all(
                        annotations.get(key) == value
                        for key, value in self._scope.annotations.items()
                    )
                    and body.get("roleRef") == {
                        "apiGroup": "rbac.authorization.k8s.io",
                        "kind": "ClusterRole",
                        "name": self._scope.workload_role,
                    }
                    and body.get("subjects") == [{
                        "kind": "ServiceAccount",
                        "name": self._scope.service_account,
                        "namespace": self._scope.platform_namespace,
                    }]
                ):
                    return
        raise ValueError("This operation does not belong to the checked worker job.")

    async def _checked(self):
        return await require_claim(
            self._factory, self._claim, for_cleanup=self._cleanup,
            expected_namespace_uid=self._uid,
        )

    async def request(self, method, path, *, body=None):
        # Keep caller mutations during database awaits out of the HTTP request.
        body = deepcopy(body)
        self._check_target(method, path, body)
        attempted = False
        writing = method != "GET"
        try:
            checked = await self._checked()
            remaining = checked.remaining_seconds
            if remaining <= 0:
                raise StoreJobClaimLost("The checked job allowance has elapsed.")
            async with asyncio.timeout(min(15.0, remaining)):
                attempted = True
                result = await self._connection.request(method, path, body=body)
                if result is not None and method in ("GET", "POST") and path in (
                    "/api/v1/namespaces", f"/api/v1/namespaces/{self._identity.name}",
                ):
                    self._scope.check_namespace(
                        self._identity, result, expected_uid=self._uid,
                        allow_terminating=self._cleanup,
                    )
            if checked.remaining_seconds <= 0:
                raise StoreJobClaimLost("The checked job allowance has elapsed.")
            await self._checked()
            if checked.remaining_seconds <= 0:
                raise StoreJobClaimLost("The checked job allowance has elapsed.")
            return result
        except (StoreJobClaimLost, StoreJobGuardUnavailable):
            raise StoreKubernetesStopped(
                write_may_have_applied=writing and attempted,
            ) from None
        except TimeoutError:
            raise KubernetesAPIError(
                "The Kubernetes request exceeded its worker allowance.",
                write_may_have_applied=writing and attempted,
            ) from None
