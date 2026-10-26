"""Pure namespace checks; callers must separately validate the live job claim."""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID


MANAGED_BY_LABEL = "urumi.ai/managed-by"
STORE_ID_LABEL = "urumi.ai/store-id"
OWNER_ID_LABEL = "urumi.ai/owner-id"
RELEASE_ANNOTATION = "urumi.ai/helm-release"


class StoreNamespaceError(ValueError):
    """The namespace does not match the expected managed store."""


def _opaque(value):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and all("!" <= character <= "~" for character in value)
    )


@dataclass(frozen=True, slots=True)
class StoreNamespace:
    store_id: UUID
    owner_id: int

    def __post_init__(self):
        if (
            not isinstance(self.store_id, UUID)
            or self.store_id.int == 0
            or type(self.owner_id) is not int
            or not 1 <= self.owner_id <= 2147483647
        ):
            raise StoreNamespaceError("Invalid managed namespace identity.")

    @property
    def name(self):
        return f"urumi-store-{self.store_id.hex}"

    @property
    def release_name(self):
        return "store"

    @property
    def labels(self):
        return {
            MANAGED_BY_LABEL: "store-provisioner",
            STORE_ID_LABEL: self.store_id.hex,
            OWNER_ID_LABEL: str(self.owner_id),
        }

    def manifest(self):
        return {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": self.name,
                "labels": self.labels,
                "annotations": {RELEASE_ANNOTATION: self.release_name},
            },
        }

    def check(self, document, *, expected_uid=None, allow_terminating=False):
        """Check an API response; return its opaque UID and resource version."""
        if type(allow_terminating) is not bool:
            raise StoreNamespaceError("Invalid namespace check options.")
        if expected_uid is not None and not _opaque(expected_uid):
            raise StoreNamespaceError("Invalid expected namespace UID.")
        if (
            not isinstance(document, Mapping)
            or document.get("apiVersion") != "v1"
            or document.get("kind") != "Namespace"
        ):
            raise StoreNamespaceError("Invalid namespace response.")

        metadata = document.get("metadata")
        status = document.get("status", {})
        if not isinstance(metadata, Mapping) or not isinstance(status, Mapping):
            raise StoreNamespaceError("Invalid namespace metadata.")
        labels = metadata.get("labels")
        annotations = metadata.get("annotations")
        if (
            metadata.get("name") != self.name
            or metadata.get("namespace") not in (None, "")
            or not isinstance(labels, Mapping)
            or not isinstance(annotations, Mapping)
            or any(labels.get(key) != value for key, value in self.labels.items())
            or annotations.get(RELEASE_ANNOTATION) != self.release_name
        ):
            raise StoreNamespaceError("Namespace ownership does not match.")

        uid = metadata.get("uid")
        version = metadata.get("resourceVersion")
        if not _opaque(uid) or not _opaque(version):
            raise StoreNamespaceError("Namespace UID or resource version is missing.")
        if expected_uid is not None and uid != expected_uid:
            raise StoreNamespaceError("The namespace was replaced.")
        if not allow_terminating and (
            metadata.get("deletionTimestamp") is not None
            or status.get("phase") == "Terminating"
        ):
            raise StoreNamespaceError("The namespace is being deleted.")
        return uid, version

    def delete_options(self, document, *, expected_uid):
        """Require the stored UID and the version that was just checked."""
        if not _opaque(expected_uid):
            raise StoreNamespaceError("Deletion requires the stored namespace UID.")
        uid, version = self.check(
            document, expected_uid=expected_uid, allow_terminating=True,
        )
        return {
            "preconditions": {"uid": uid, "resourceVersion": version},
            "propagationPolicy": "Foreground",
        }


def namespace_for(claim):
    identity = StoreNamespace(
        getattr(claim, "store_id", None), getattr(claim, "owner_id", None),
    )
    if (
        getattr(claim, "namespace", None) != identity.name
        or getattr(claim, "release_name", None) != identity.release_name
    ):
        raise StoreNamespaceError("The claim namespace does not match its store.")
    return identity
