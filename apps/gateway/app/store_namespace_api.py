"""Owned namespace operations; callers must hold a live worker claim.

No database transaction may stay open across these Kubernetes requests.
Persist the observed UID before granting access or installing a store.
"""

from collections.abc import Mapping

from app.store_cluster_scope import StoreClusterScope
from app.store_kube_connection import KubernetesAPIError, KubernetesRequester
from app.store_namespace import StoreNamespace, StoreNamespaceError, _opaque


class StoreNamespacePending(RuntimeError):
    """The owned namespace is not ready for the next provisioning step."""


def _identity(value):
    if not isinstance(value, StoreNamespace):
        raise StoreNamespaceError("Invalid managed namespace identity.")
    return value


def _saved_uid(value):
    if not _opaque(value):
        raise StoreNamespaceError("The saved namespace UID is required.")


def _binding_check(document, expected):
    if not isinstance(document, Mapping):
        raise StoreNamespaceError("Invalid worker RoleBinding response.")
    metadata = document.get("metadata")
    if (
        document.get("apiVersion") != expected["apiVersion"]
        or document.get("kind") != "RoleBinding"
        or not isinstance(metadata, Mapping)
    ):
        raise StoreNamespaceError("Invalid worker RoleBinding response.")
    wanted = expected["metadata"]
    labels = metadata.get("labels")
    annotations = metadata.get("annotations")
    role = document.get("roleRef")
    subjects = document.get("subjects")
    if (
        metadata.get("name") != wanted["name"]
        or metadata.get("namespace") != wanted["namespace"]
        or metadata.get("deletionTimestamp") is not None
        or not _opaque(metadata.get("uid"))
        or not _opaque(metadata.get("resourceVersion"))
        or not isinstance(labels, Mapping)
        or not isinstance(annotations, Mapping)
        or any(labels.get(key) != value for key, value in wanted["labels"].items())
        or any(
            annotations.get(key) != value
            for key, value in wanted["annotations"].items()
        )
        or not isinstance(role, Mapping)
        or dict(role) != expected["roleRef"]
        or not isinstance(subjects, list)
        or len(subjects) != 1
        or not isinstance(subjects[0], Mapping)
        or subjects[0].get("apiGroup", "") != ""
        or any(
            subjects[0].get(key) != value
            for key, value in expected["subjects"][0].items()
        )
    ):
        raise StoreNamespaceError("Worker RoleBinding ownership does not match.")
    return document


class StoreNamespaceAPI:
    def __init__(self, connection, scope):
        if (
            not isinstance(connection, KubernetesRequester)
            or not isinstance(scope, StoreClusterScope)
        ):
            raise ValueError("A private Kubernetes connection and scope are required.")
        self.connection = connection
        self.scope = scope

    async def read_namespace(
        self, identity, *, expected_uid=None, allow_terminating=False,
    ):
        identity = _identity(identity)
        if expected_uid is not None:
            _saved_uid(expected_uid)
        if type(allow_terminating) is not bool:
            raise ValueError("The namespace check mode must be a boolean.")
        document = await self.connection.request(
            "GET", f"/api/v1/namespaces/{identity.name}",
        )
        if document is not None:
            self.scope.check_namespace(
                identity, document, expected_uid=expected_uid,
                allow_terminating=allow_terminating,
            )
        return document

    async def ensure_namespace(self, identity, *, expected_uid):
        """Create only when no UID has previously been stored."""
        document = await self.read_namespace(identity, expected_uid=expected_uid)
        if document is not None:
            return document
        if expected_uid is not None:
            raise StoreNamespaceError(
                "The saved namespace is missing; it must not be recreated.",
            )
        try:
            document = await self.connection.request(
                "POST", "/api/v1/namespaces",
                body=self.scope.namespace_manifest(identity),
            )
        except KubernetesAPIError as error:
            if error.status_code != 409:
                raise
            document = await self.read_namespace(identity)
            if document is None:
                raise StoreNamespacePending(
                    "The conflicting namespace could not yet be verified.",
                ) from None
        self.scope.check_namespace(identity, document)
        return document

    async def _active_namespace(self, identity, expected_uid):
        document = await self.read_namespace(identity, expected_uid=expected_uid)
        if document is None:
            raise StoreNamespaceError("The saved namespace is missing.")
        if document.get("status", {}).get("phase") != "Active":
            raise StoreNamespacePending("The owned namespace is not active yet.")
        return document

    async def ensure_role_binding(self, identity, *, expected_uid):
        """Grant only the fixed worker role inside the saved namespace."""
        _saved_uid(expected_uid)
        document = await self._active_namespace(identity, expected_uid)
        expected = self.scope.role_binding_manifest(
            identity, document, expected_uid=expected_uid,
        )
        collection = (
            "/apis/rbac.authorization.k8s.io/v1/namespaces/"
            f"{identity.name}/rolebindings"
        )
        item = collection + "/urumi-store-provisioner"
        binding = await self.connection.request("GET", item)
        if binding is None:
            await self._active_namespace(identity, expected_uid)
            try:
                binding = await self.connection.request(
                    "POST", collection, body=expected,
                )
            except KubernetesAPIError as error:
                if error.status_code != 409:
                    raise
                binding = await self.connection.request("GET", item)
                if binding is None:
                    raise StoreNamespacePending(
                        "The conflicting worker binding could not yet be verified.",
                    ) from None
        _binding_check(binding, expected)
        await self._active_namespace(identity, expected_uid)
        return binding

    async def delete_namespace(self, identity, *, expected_uid):
        """True means namespace absence was observed, not volume cleanup."""
        _saved_uid(expected_uid)
        document = await self.read_namespace(
            identity, expected_uid=expected_uid, allow_terminating=True,
        )
        if document is None:
            return True
        if (
            document["metadata"].get("deletionTimestamp") is not None
            or document.get("status", {}).get("phase") == "Terminating"
        ):
            return False
        options = self.scope.namespace_delete_options(
            identity, document, expected_uid=expected_uid,
        )
        await self.connection.request(
            "DELETE", f"/api/v1/namespaces/{identity.name}", body=options,
        )
        return False
