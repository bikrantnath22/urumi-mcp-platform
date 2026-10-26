"""Identify existing store claims, including partially provisioned storage."""

from collections.abc import Mapping
from dataclasses import dataclass

from app.store_namespace import StoreNamespace, _opaque
from app.store_volumes import (
    STORE_CLAIM_NAMES, StoreVolumeBinding, StoreVolumeError,
    StoreVolumePending, _volume_name,
)


def _invalid():
    raise StoreVolumeError("Managed store claim identity could not be verified.")


@dataclass(frozen=True, slots=True)
class StoreClaimIdentity:
    identity: StoreNamespace
    namespace_uid: str
    claim_name: str
    claim_uid: str
    volume_name: str | None = None
    storage_class: str = "local-path"

    def __post_init__(self):
        if (
            not isinstance(self.identity, StoreNamespace)
            or self.claim_name not in STORE_CLAIM_NAMES
            or not _opaque(self.namespace_uid) or not _opaque(self.claim_uid)
            or (self.volume_name is not None and not _volume_name(self.volume_name))
            or self.storage_class != "local-path"
        ):
            _invalid()


def check_store_claim(
    identity, *, namespace_uid, claim_name, document,
    expected=None, for_cleanup=False,
):
    """Identify a PVC; this is neither a bound-volume nor cleanup proof.

    A missing claim remains pending. Cleanup may observe a terminating or Lost
    claim, but must separately inspect volumes and confirm resource removal.
    The caller must verify the live worker claim and actual namespace UID.
    """
    if (
        not isinstance(identity, StoreNamespace)
        or not _opaque(namespace_uid) or claim_name not in STORE_CLAIM_NAMES
        or type(for_cleanup) is not bool
        or (
            expected is not None
            and type(expected) not in (StoreClaimIdentity, StoreVolumeBinding)
        )
    ):
        _invalid()
    if document is None:
        raise StoreVolumePending("The store claim is not available for identification.")
    if (
        not isinstance(document, Mapping)
        or document.get("apiVersion") != "v1"
        or document.get("kind") != "PersistentVolumeClaim"
    ):
        _invalid()
    metadata, spec = document.get("metadata"), document.get("spec")
    status = document.get("status", {})
    if not all(isinstance(value, Mapping) for value in (metadata, spec, status)):
        _invalid()
    if (
        metadata.get("name") != claim_name
        or metadata.get("namespace") != identity.name
        or not _opaque(metadata.get("uid"))
        or not _opaque(metadata.get("resourceVersion"))
    ):
        _invalid()
    stamp = metadata.get("deletionTimestamp")
    if stamp is not None and (
        not for_cleanup or not isinstance(stamp, str) or not stamp
    ):
        _invalid()
    labels, annotations = metadata.get("labels", {}), metadata.get("annotations", {})
    if not isinstance(labels, Mapping) or not isinstance(annotations, Mapping):
        _invalid()
    if labels.get("app") != ("mysql" if claim_name == "data-mysql-0" else "wordpress"):
        _invalid()
    if claim_name == "wordpress-data" and (
        labels.get("app.kubernetes.io/managed-by") != "Helm"
        or annotations.get("meta.helm.sh/release-name") != identity.release_name
        or annotations.get("meta.helm.sh/release-namespace") != identity.name
    ):
        _invalid()
    if (
        spec.get("storageClassName") != "local-path"
        or spec.get("accessModes") != ["ReadWriteOnce"]
        or spec.get("volumeMode", "Filesystem") != "Filesystem"
    ):
        _invalid()
    phase = status.get("phase")
    name = spec.get("volumeName")
    if name == "":
        name = None
    if (
        phase not in (None, "Pending", "Bound", "Lost")
        or (phase == "Bound" and name is None)
        or (phase == "Lost" and not for_cleanup)
    ):
        _invalid()
    result = StoreClaimIdentity(
        identity, namespace_uid, claim_name, metadata["uid"], name,
    )
    if expected is not None and (
        any(
            getattr(expected, key) != getattr(result, key)
            for key in ("identity", "namespace_uid", "claim_name", "claim_uid", "storage_class")
        )
        or (expected.volume_name is not None and expected.volume_name != result.volume_name)
    ):
        _invalid()
    return result
