"""Pure Kubernetes identities; callers must separately validate live job claims."""

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import re

from app.store_namespace import StoreNamespace, StoreNamespaceError


_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")


@dataclass(frozen=True, slots=True)
class StoreClusterScope:
    platform_namespace: str
    platform_release: str

    def __post_init__(self):
        namespace = self.platform_namespace
        release = self.platform_release
        if (
            not isinstance(namespace, str)
            or not 1 <= len(namespace) <= 63
            or not _LABEL.fullmatch(namespace)
            or not isinstance(release, str)
            or not 1 <= len(release) <= 53
            or any(not _LABEL.fullmatch(part) for part in release.split("."))
        ):
            raise StoreNamespaceError("Invalid platform namespace or release.")

    @property
    def service_account(self):
        name = f"store-provisioner-{self.platform_release}"
        if len(name) > 63:
            suffix = sha256(name.encode("utf-8")).hexdigest()[:12]
            return f"{name[:50].removesuffix('-')}-{suffix}"
        return name

    @property
    def workload_role(self):
        scope = f"{self.platform_namespace}/{self.platform_release}"
        suffix = sha256(scope.encode("utf-8")).hexdigest()[:12]
        return (
            f"urumi-store-provisioner-{self.platform_namespace}-"
            f"{self.platform_release}-{suffix}-workloads"
        )

    @property
    def annotations(self):
        return {
            "urumi.ai/helm-release": "store",
            "urumi.ai/platform-namespace": self.platform_namespace,
            "urumi.ai/platform-release": self.platform_release,
        }

    @property
    def pod_security_labels(self):
        labels = {}
        for mode in ("enforce", "audit", "warn"):
            labels[f"pod-security.kubernetes.io/{mode}"] = "restricted"
            labels[f"pod-security.kubernetes.io/{mode}-version"] = "latest"
        return labels

    @staticmethod
    def _identity(identity):
        if not isinstance(identity, StoreNamespace):
            raise StoreNamespaceError("Invalid managed namespace identity.")
        return identity

    def namespace_manifest(self, identity):
        document = self._identity(identity).manifest()
        document["metadata"]["annotations"].update(self.annotations)
        document["metadata"]["labels"].update(self.pod_security_labels)
        return document

    def check_namespace(
        self, identity, document, *, expected_uid=None, allow_terminating=False,
    ):
        identity = self._identity(identity)
        result = identity.check(
            document,
            expected_uid=expected_uid,
            allow_terminating=allow_terminating,
        )
        metadata = document["metadata"]
        if (
            any(
                metadata["annotations"].get(key) != value
                for key, value in self.annotations.items()
            )
            or any(
                metadata["labels"].get(key) != value
                for key, value in self.pod_security_labels.items()
            )
        ):
            raise StoreNamespaceError(
                "Namespace platform ownership or Pod Security does not match."
            )
        return result

    def namespace_delete_options(self, identity, document, *, expected_uid):
        self.check_namespace(
            identity, document, expected_uid=expected_uid, allow_terminating=True,
        )
        return identity.delete_options(document, expected_uid=expected_uid)

    def role_binding_manifest(self, identity, document, *, expected_uid):
        self.check_namespace(identity, document, expected_uid=expected_uid)
        status = document.get("status")
        if not isinstance(status, Mapping) or status.get("phase") != "Active":
            raise StoreNamespaceError("The namespace is not active.")
        if expected_uid is None:
            raise StoreNamespaceError("The saved namespace UID is required.")
        return {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {
                "name": "urumi-store-provisioner",
                "namespace": identity.name,
                "labels": identity.labels,
                "annotations": self.annotations,
            },
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": self.workload_role,
            },
            "subjects": [{
                "kind": "ServiceAccount",
                "name": self.service_account,
                "namespace": self.platform_namespace,
            }],
        }
