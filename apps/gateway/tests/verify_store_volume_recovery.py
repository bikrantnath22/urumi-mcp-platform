"""Pure recovery identity checks; no services or filesystem mutations."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.store_claims import StoreClaimIdentity
from app.store_namespace import StoreNamespace
from app.store_volume_recovery import identify_store_volume
from app.store_volumes import (
    STORE_CLAIM_NAMES, StoreVolumeBinding, StoreVolumeError, StoreVolumePending,
)
import verify_store_volumes as data


IDENTITY = data.IDENTITY


def identify(document, known=(), **options):
    return identify_store_volume(
        IDENTITY, namespace_uid="namespace-uid", document=document,
        known=known, **options,
    )


def error(kind, callback):
    try:
        callback()
    except kind as caught:
        assert "PRIVATE" not in str(caught)
        return
    raise AssertionError("Expected " + kind.__name__)


def set_field(document, path, value):
    parent = document
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value


def run():
    for name in STORE_CLAIM_NAMES:
        pvc, document = data.pair(name)
        binding = data.check(name, pvc, document)
        pending = StoreClaimIdentity(
            IDENTITY, "namespace-uid", name, binding.claim_uid,
        )
        named = replace(pending, volume_name=binding.volume_name)
        for known in ((), (pending,), (named,), (binding,)):
            for phase in (None, "Pending", "Available", "Bound", "Released", "Failed"):
                for terminating in (False, True):
                    item = deepcopy(document)
                    item["status"]["phase"] = phase
                    if terminating:
                        item["metadata"]["deletionTimestamp"] = "fixture-time"
                    before = deepcopy(item)
                    recovered = identify(item, known)
                    assert type(recovered) is StoreVolumeBinding and recovered == binding
                    assert item == before
                    try:
                        recovered.volume_uid = "changed"
                    except FrozenInstanceError:
                        pass
                    else:
                        raise AssertionError("Recovered identities must be immutable.")
    print("Both store claims recover immutable PV identities without PVC documents; terminating and Released resources still exist")

    pvc, document = data.pair("wordpress-data")
    binding = data.check("wordpress-data", pvc, document)
    pending = StoreClaimIdentity(
        IDENTITY, "namespace-uid", "wordpress-data", binding.claim_uid,
    )
    unrelated = deepcopy(document)
    unrelated["metadata"].update(name="other-volume", uid="other-volume-uid")
    unrelated["spec"]["claimRef"].update(namespace="another-store", uid="other-claim")
    unrelated["spec"]["storageClassName"] = "other-class"
    assert identify(unrelated) is None and identify(unrelated, (pending,)) is None
    unrelated["spec"].pop("claimRef")
    assert identify(unrelated, (binding,)) is None
    error(StoreVolumePending, lambda: identify(None))
    print("Unrelated volumes are skipped; a missing response cannot prove successful cleanup")

    for path, value in (
        (("apiVersion",), "other/v1"), (("kind",), "PersistentVolumeList"),
        (("metadata",), []), (("spec",), None), (("status",), []),
        (("metadata", "name"), "../unsafe"), (("metadata", "uid"), None),
        (("metadata", "resourceVersion"), "bad version"),
        (("metadata", "namespace"), IDENTITY.name),
        (("metadata", "deletionTimestamp"), False),
        (("metadata", "annotations"), []),
        (("metadata", "annotations", "pv.kubernetes.io/provisioned-by"), "other"),
        (("spec", "storageClassName"), "other-class"),
        (("spec", "persistentVolumeReclaimPolicy"), "Retain"),
        (("spec", "accessModes"), ["ReadWriteMany"]),
        (("spec", "volumeMode"), "Block"),
        (("spec", "claimRef"), []),
        (("spec", "claimRef", "apiVersion"), "other/v1"),
        (("spec", "claimRef", "kind"), "Other"),
        (("spec", "claimRef", "name"), "other-claim"),
        (("spec", "claimRef", "uid"), "bad uid"),
        (("status", "phase"), "Unexpected"),
    ):
        item = deepcopy(document)
        set_field(item, path, value)
        before = deepcopy(item)
        error(StoreVolumeError, lambda: identify(item))
        assert item == before
    print("Malformed metadata, unexpected claim references and unsupported storage policies reject recovery")

    for known in (
        (replace(pending, claim_uid="replacement"),),
        (replace(pending, volume_name="replacement"),),
        (replace(binding, volume_uid="replacement"),),
    ):
        error(StoreVolumeError, lambda: identify(document, known))
    for path, value in (
        (("metadata", "name"), "replacement"),
        (("metadata", "uid"), "replacement"),
        (("spec", "claimRef", "namespace"), "another-store"),
        (("spec", "claimRef", "uid"), "replacement"),
        (("spec", "claimRef", "name"), "data-mysql-0"),
    ):
        item = deepcopy(document)
        set_field(item, path, value)
        error(StoreVolumeError, lambda: identify(item, (binding,)))
    item = deepcopy(document)
    item["metadata"]["name"] = "pvc-" + pending.claim_uid
    item["spec"]["claimRef"].update(namespace="another-store", uid="other-uid")
    error(StoreVolumeError, lambda: identify(item, (pending,)))
    item = deepcopy(document)
    item["spec"]["claimRef"]["name"] = "data-mysql-0"
    error(StoreVolumeError, lambda: identify(item, (pending,)))
    print("Known PVC/PV names and UIDs reject replacements, rebindings and cross-claim identity collisions")

    for known in (
        [pending], (object(),), (pending, pending), (pending, pending, pending),
        (replace(pending, namespace_uid="another-uid"),),
        (replace(pending, identity=StoreNamespace(uuid4(), IDENTITY.owner_id)),),
        (pending, replace(pending, claim_name="data-mysql-0")),
    ):
        error(StoreVolumeError, lambda: identify(document, known))
    print("Saved inventories must be scoped, distinct, validated snapshots")
    print("Volume recovery identity verification passed. No database or Kubernetes calls made.")


if __name__ == "__main__":
    run()
