"""Identify store PVs during recovery without requiring a surviving PVC."""

from collections.abc import Mapping
from dataclasses import replace

from app.store_claims import StoreClaimIdentity
from app.store_namespace import StoreNamespace, _opaque
from app.store_volumes import (
    STORE_CLAIM_NAMES, StoreVolumeBinding, StoreVolumeError,
    StoreVolumePending, _volume_name,
)


def _invalid():
    raise StoreVolumeError("Recovered store volume identity could not be verified.")


def _known_records(identity, namespace_uid, known):
    if type(known) is not tuple or len(known) > len(STORE_CLAIM_NAMES):
        _invalid()
    copied = []
    for item in known:
        if type(item) not in (StoreClaimIdentity, StoreVolumeBinding):
            _invalid()
        item = replace(item)
        if item.identity != identity or item.namespace_uid != namespace_uid:
            _invalid()
        copied.append(item)
    for field in ("claim_name", "claim_uid", "volume_name", "volume_uid"):
        values = [
            getattr(item, field, None) for item in copied
            if getattr(item, field, None) is not None
        ]
        if len(values) != len(set(values)):
            _invalid()
    return tuple(copied)


def _linked(item, metadata, reference):
    return (
        metadata["name"] in (item.volume_name, "pvc-" + item.claim_uid)
        or metadata["uid"] == getattr(item, "volume_uid", None)
        or reference.get("uid") == item.claim_uid
    )


def identify_store_volume(identity, *, namespace_uid, document, known=()):
    """Return an identity, or None for an unrelated volume.

    This pure check grants no access and proves neither readiness nor cleanup.
    Released/terminating volumes still exist. Callers must verify live job and
    namespace ownership before I/O and persist recovered identifiers separately.
    """
    if not isinstance(identity, StoreNamespace) or not _opaque(namespace_uid):
        _invalid()
    known = _known_records(identity, namespace_uid, known)
    if document is None:
        raise StoreVolumePending("The volume response is not available.")
    if (
        not isinstance(document, Mapping)
        or document.get("apiVersion") != "v1"
        or document.get("kind") != "PersistentVolume"
    ):
        _invalid()
    metadata, spec = document.get("metadata"), document.get("spec")
    status = document.get("status", {})
    if not all(isinstance(item, Mapping) for item in (metadata, spec, status)):
        _invalid()
    if (
        not _volume_name(metadata.get("name"))
        or not _opaque(metadata.get("uid"))
        or not _opaque(metadata.get("resourceVersion"))
        or metadata.get("namespace") not in (None, "")
    ):
        _invalid()
    reference = spec.get("claimRef")
    if reference is None:
        reference = {}
    if not isinstance(reference, Mapping):
        _invalid()
    related = reference.get("namespace") == identity.name or any(
        _linked(item, metadata, reference) for item in known
    )
    if not related:
        return None

    annotations = metadata.get("annotations", {})
    stamp = metadata.get("deletionTimestamp")
    if (
        not isinstance(annotations, Mapping)
        or annotations.get("pv.kubernetes.io/provisioned-by") != "rancher.io/local-path"
        or (stamp is not None and (not isinstance(stamp, str) or not stamp))
        or reference.get("apiVersion") != "v1"
        or reference.get("kind") != "PersistentVolumeClaim"
        or reference.get("namespace") != identity.name
        or reference.get("name") not in STORE_CLAIM_NAMES
        or not _opaque(reference.get("uid"))
        or spec.get("storageClassName") != "local-path"
        or spec.get("persistentVolumeReclaimPolicy") != "Delete"
        or spec.get("accessModes") != ["ReadWriteOnce"]
        or spec.get("volumeMode", "Filesystem") != "Filesystem"
        or status.get("phase") not in (
            None, "Pending", "Available", "Bound", "Released", "Failed",
        )
    ):
        _invalid()
    result = StoreVolumeBinding(
        identity, namespace_uid, reference["name"], reference["uid"],
        metadata["name"], metadata["uid"],
    )
    for item in known:
        if item.claim_name != result.claim_name:
            if _linked(item, metadata, reference):
                _invalid()
            continue
        if (
            item.claim_uid != result.claim_uid
            or (item.volume_name is not None and item.volume_name != result.volume_name)
            or (
                type(item) is StoreVolumeBinding
                and item.volume_uid != result.volume_uid
            )
        ):
            _invalid()
    return result
