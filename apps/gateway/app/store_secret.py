"""Pure definitions for the fixed credentials Secret of one managed store."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from app.store_cluster_scope import StoreClusterScope
from app.store_credentials import StoreCredentialError, StoreCredentials
from app.store_namespace import StoreNamespace, _opaque


STORE_SECRET_NAME = "store-secrets"
NAMESPACE_UID_ANNOTATION = "urumi.ai/namespace-uid"


class StoreSecretError(ValueError):
    """Secret ownership or contents do not match the expected store."""


@dataclass(frozen=True, slots=True)
class CheckedStoreSecret:
    uid: str
    resource_version: str
    credentials: StoreCredentials = field(repr=False)


@dataclass(frozen=True, slots=True)
class StoreSecret:
    identity: StoreNamespace
    scope: StoreClusterScope
    namespace_uid: str

    def __post_init__(self):
        if (
            not isinstance(self.identity, StoreNamespace)
            or not isinstance(self.scope, StoreClusterScope)
            or not _opaque(self.namespace_uid)
        ):
            raise StoreSecretError(
                "A valid store, platform and saved namespace UID are required."
            )

    @property
    def annotations(self):
        return {
            **self.scope.annotations,
            NAMESPACE_UID_ANNOTATION: self.namespace_uid,
        }

    def manifest(self, credentials):
        """Build a new Secret request; the caller must authorize creation."""
        if (
            not isinstance(credentials, StoreCredentials)
            or credentials.identity != self.identity
        ):
            raise StoreSecretError("The credentials belong to another store.")
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": STORE_SECRET_NAME,
                "namespace": self.identity.name,
                "labels": self.identity.labels,
                "annotations": self.annotations,
            },
            "type": "Opaque",
            "immutable": True,
            "data": credentials.secret_data(),
        }

    def check(self, document, *, expected_uid=None):
        """Validate a response without changing or logging its credentials."""
        if expected_uid is not None and not _opaque(expected_uid):
            raise StoreSecretError("The saved Secret UID is invalid.")
        if (
            not isinstance(document, Mapping)
            or document.get("apiVersion") != "v1"
            or document.get("kind") != "Secret"
            or document.get("type") != "Opaque"
            or document.get("immutable") is not True
            or "stringData" in document
        ):
            raise StoreSecretError("The store Secret definition is invalid.")

        metadata = document.get("metadata")
        if not isinstance(metadata, Mapping):
            raise StoreSecretError("The store Secret metadata is invalid.")
        labels = metadata.get("labels")
        annotations = metadata.get("annotations")
        if (
            metadata.get("name") != STORE_SECRET_NAME
            or metadata.get("namespace") != self.identity.name
            or not isinstance(labels, Mapping)
            or not isinstance(annotations, Mapping)
            or any(
                labels.get(key) != value
                for key, value in self.identity.labels.items()
            )
            or any(
                annotations.get(key) != value
                for key, value in self.annotations.items()
            )
            or metadata.get("ownerReferences") not in (None, [])
        ):
            raise StoreSecretError("The store Secret ownership does not match.")
        if metadata.get("deletionTimestamp") is not None:
            raise StoreSecretError("The store Secret is being deleted.")

        uid = metadata.get("uid")
        version = metadata.get("resourceVersion")
        if not _opaque(uid) or not _opaque(version):
            raise StoreSecretError("The store Secret UID or version is missing.")
        if expected_uid is not None and uid != expected_uid:
            raise StoreSecretError("The saved store Secret was replaced.")
        try:
            credentials = StoreCredentials.from_secret_data(
                self.identity, document.get("data"),
            )
        except StoreCredentialError:
            raise StoreSecretError(
                "The stored store credentials are invalid."
            ) from None
        return CheckedStoreSecret(uid, version, credentials)
