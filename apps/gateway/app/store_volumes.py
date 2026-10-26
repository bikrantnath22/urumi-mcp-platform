"""Pure identity checks for the local-path volumes of one managed store."""

from collections.abc import Mapping
from dataclasses import dataclass
import re

from app.store_namespace import StoreNamespace, _opaque


STORE_CLAIM_NAMES = ("data-mysql-0", "wordpress-data")


class StoreVolumeError(ValueError):
    """Storage identity or deletion policy does not match the managed store."""


class StoreVolumePending(RuntimeError):
    """The claim or volume is not bound yet; no cleanup success is implied."""


def _invalid():
    raise StoreVolumeError("Managed store volume identity could not be verified.")


def _volume_name(value):
    return (
        isinstance(value, str) and 1 <= len(value) <= 253
        and all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
            for part in value.split(".")
        )
    )


@dataclass(frozen=True, slots=True)
class StoreVolumeBinding:
    identity: StoreNamespace
    namespace_uid: str
    claim_name: str
    claim_uid: str
    volume_name: str
    volume_uid: str
    storage_class: str = "local-path"

    def __post_init__(self):
        if (
            not isinstance(self.identity, StoreNamespace)
            or self.claim_name not in STORE_CLAIM_NAMES
            or not all(_opaque(value) for value in (
                self.namespace_uid, self.claim_uid, self.volume_uid,
            ))
            or not _volume_name(self.volume_name)
            or self.storage_class != "local-path"
        ):
            _invalid()


def _resource(document, kind, name, namespace):
    if document is None:
        raise StoreVolumePending("Managed store storage is not bound yet.")
    if (
        not isinstance(document, Mapping)
        or document.get("apiVersion") != "v1" or document.get("kind") != kind
    ):
        _invalid()
    metadata, spec = document.get("metadata"), document.get("spec")
    status = document.get("status", {})
    if not all(isinstance(value, Mapping) for value in (metadata, spec, status)):
        _invalid()
    if (
        metadata.get("name") != name
        or (metadata.get("namespace") not in (None, "") if namespace is None
            else metadata.get("namespace") != namespace)
        or not _opaque(metadata.get("uid"))
        or not _opaque(metadata.get("resourceVersion"))
        or metadata.get("deletionTimestamp") is not None
    ):
        _invalid()
    return metadata, spec, status

def _bound_claim(identity, *, namespace_uid, claim_name, claim):
    """Validate ownership before a claim's volume name can authorize a read."""
    if (
        not isinstance(identity, StoreNamespace) or not _opaque(namespace_uid)
        or claim_name not in STORE_CLAIM_NAMES
    ):
        _invalid()
    metadata, spec, status = _resource(
        claim, "PersistentVolumeClaim", claim_name, identity.name,
    )
    labels = metadata.get("labels", {})
    if not isinstance(labels, Mapping):
        _invalid()
    if labels.get("app") != ("mysql" if claim_name == "data-mysql-0" else "wordpress"):
        _invalid()
    if claim_name == "wordpress-data":
        annotations = metadata.get("annotations", {})
        if (
            not isinstance(annotations, Mapping)
            or labels.get("app.kubernetes.io/managed-by") != "Helm"
            or annotations.get("meta.helm.sh/release-name") != "store"
            or annotations.get("meta.helm.sh/release-namespace") != identity.name
        ):
            _invalid()
    if spec.get("storageClassName") != "local-path":
        _invalid()
    if status.get("phase") in (None, "Pending"):
        raise StoreVolumePending("Managed store storage is not bound yet.")
    if (
        status.get("phase") != "Bound" or not _volume_name(spec.get("volumeName"))
        or spec.get("accessModes") != ["ReadWriteOnce"]
        or spec.get("volumeMode", "Filesystem") != "Filesystem"
    ):
        _invalid()
    return metadata, spec["volumeName"]


def check_bound_volume(
    identity, *, namespace_uid, claim_name, claim, volume, expected=None,
):
    """Return verified identifiers only; no I/O or proof of cleanup."""
    if expected is not None and not isinstance(expected, StoreVolumeBinding):
        _invalid()
    metadata, volume_name = _bound_claim(
        identity, namespace_uid=namespace_uid, claim_name=claim_name, claim=claim,
    )
    pv_metadata, pv_spec, pv_status = _resource(
        volume, "PersistentVolume", volume_name, None,
    )
    reference = pv_spec.get("claimRef", {})
    annotations = pv_metadata.get("annotations", {})
    if (
        not isinstance(reference, Mapping) or not isinstance(annotations, Mapping)
        or reference.get("apiVersion") != "v1"
        or reference.get("kind") != "PersistentVolumeClaim"
        or reference.get("namespace") != identity.name
        or reference.get("name") != claim_name
        or reference.get("uid") != metadata["uid"]
        or pv_spec.get("storageClassName") != "local-path"
        or pv_spec.get("persistentVolumeReclaimPolicy") != "Delete"
        or annotations.get("pv.kubernetes.io/provisioned-by") != "rancher.io/local-path"
        or pv_spec.get("accessModes") != ["ReadWriteOnce"]
        or pv_spec.get("volumeMode", "Filesystem") != "Filesystem"
    ):
        _invalid()
    if pv_status.get("phase") in (None, "Pending", "Available"):
        raise StoreVolumePending("Managed store storage is not bound yet.")
    if pv_status.get("phase") != "Bound":
        _invalid()
    binding = StoreVolumeBinding(
        identity, namespace_uid, claim_name, metadata["uid"],
        pv_metadata["name"], pv_metadata["uid"],
    )
    if expected is not None and binding != expected:
        _invalid()
    return binding
