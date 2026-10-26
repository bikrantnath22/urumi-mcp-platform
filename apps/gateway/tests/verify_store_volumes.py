"""Offline checks of managed PVC/PV identities; no services are contacted."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.store_namespace import StoreNamespace
from app.store_volumes import (
    STORE_CLAIM_NAMES, StoreVolumeBinding, StoreVolumeError, StoreVolumePending,
    check_bound_volume,
)


IDENTITY = StoreNamespace(uuid4(), 7)


def pair(name):
    labels = {"app": "mysql" if name == "data-mysql-0" else "wordpress"}
    annotations = {}
    if name == "wordpress-data":
        labels["app.kubernetes.io/managed-by"] = "Helm"
        annotations = {
            "meta.helm.sh/release-name": "store",
            "meta.helm.sh/release-namespace": IDENTITY.name,
        }
    claim = {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": name, "namespace": IDENTITY.name, "uid": "claim-uid",
            "resourceVersion": "1", "labels": labels, "annotations": annotations,
        },
        "spec": {
            "storageClassName": "local-path", "volumeName": "pvc-fixture",
            "accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem",
        },
        "status": {"phase": "Bound"},
    }
    volume = {
        "apiVersion": "v1", "kind": "PersistentVolume",
        "metadata": {
            "name": "pvc-fixture", "uid": "volume-uid", "resourceVersion": "2",
            "annotations": {"pv.kubernetes.io/provisioned-by": "rancher.io/local-path"},
        },
        "spec": {
            "storageClassName": "local-path", "persistentVolumeReclaimPolicy": "Delete",
            "accessModes": ["ReadWriteOnce"], "volumeMode": "Filesystem",
            "claimRef": {
                "apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "namespace": IDENTITY.name, "name": name, "uid": "claim-uid",
            },
        },
        "status": {"phase": "Bound"},
    }
    return claim, volume


def check(name, claim, volume, **options):
    return check_bound_volume(
        IDENTITY, namespace_uid="namespace-uid", claim_name=name,
        claim=claim, volume=volume, **options,
    )


def error(kind, callback):
    try:
        callback()
    except kind as caught:
        assert str(caught) in (
            "Managed store storage is not bound yet.",
            "Managed store volume identity could not be verified.",
        )
        return
    raise AssertionError("Expected " + kind.__name__)


def set_field(document, path, value):
    current = document
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = value

def run():
    for name in STORE_CLAIM_NAMES:
        claim, volume = pair(name)
        before = deepcopy((claim, volume))
        binding = check(name, claim, volume)
        assert isinstance(binding, StoreVolumeBinding)
        assert binding.claim_uid == "claim-uid" and binding.volume_uid == "volume-uid"
        assert check(name, claim, volume, expected=binding) == binding
        assert (claim, volume) == before
        try:
            binding.claim_uid = "changed"
        except FrozenInstanceError:
            pass
        else:
            raise AssertionError("The binding is mutable.")
    print("Both store volumes bind to exact claim UIDs; snapshots are immutable and inputs remain unchanged")

    name = "wordpress-data"
    claim, volume = pair(name)
    for left, right in ((None, volume), (claim, None)):
        error(StoreVolumePending, lambda: check(name, left, right))
    for target, phase in (("claim", "Pending"), ("volume", "Available")):
        claim, volume = pair(name)
        (claim if target == "claim" else volume)["status"]["phase"] = phase
        error(StoreVolumePending, lambda: check(name, claim, volume))
    print("Missing and unbound storage remains pending; it never proves deletion or successful provisioning")

    for target, path, value in (
        ("claim", ("metadata", "namespace"), "another-store"),
        ("claim", ("metadata", "uid"), "replaced-claim"),
        ("claim", ("metadata", "resourceVersion"), None),
        ("claim", ("metadata", "labels", "app"), "mysql"),
        ("claim", ("metadata", "annotations", "meta.helm.sh/release-name"), "other"),
        ("claim", ("spec", "volumeName"), "../unsafe"),
        ("claim", ("spec", "storageClassName"), "other-class"),
        ("claim", ("status", "phase"), "Lost"),
        ("volume", ("metadata", "namespace"), IDENTITY.name),
        ("volume", ("metadata", "name"), "another-volume"),
        ("volume", ("metadata", "deletionTimestamp"), "fixture-time"),
        ("volume", ("spec", "claimRef", "uid"), "another-claim"),
        ("volume", ("spec", "claimRef", "namespace"), "another-store"),
        ("volume", ("spec", "claimRef", "name"), "data-mysql-0"),
        ("volume", ("spec", "persistentVolumeReclaimPolicy"), "Retain"),
        ("volume", ("metadata", "annotations", "pv.kubernetes.io/provisioned-by"), "other"),
        ("volume", ("spec", "accessModes"), ["ReadWriteMany"]),
        ("volume", ("spec", "volumeMode"), "Block"),
    ):
        claim, volume = pair(name)
        set_field(claim if target == "claim" else volume, path, value)
        before = deepcopy((claim, volume))
        error(StoreVolumeError, lambda: check(name, claim, volume))
        assert (claim, volume) == before
    print("Cross-store references, unsafe names, retained storage and changed ownership reject binding")

    claim, volume = pair(name)
    saved = check(name, claim, volume)
    claim["metadata"]["uid"] = volume["spec"]["claimRef"]["uid"] = "new-claim"
    volume["metadata"]["uid"] = "new-volume"
    error(StoreVolumeError, lambda: check(name, claim, volume, expected=saved))
    for changes in (
        {"claim_name": "unexpected"}, {"namespace_uid": None},
        {"volume_name": "x" * 64}, {"volume_name": "pv/other"},
        {"volume_uid": None}, {"storage_class": "other-class"},
    ):
        error(StoreVolumeError, lambda: replace(saved, **changes))
    print("Saved UIDs reject replaced resources; malformed binding snapshots cannot be restored")
    print("Store volume identity checks passed. No database, Kubernetes or filesystem changes made.")


if __name__ == "__main__":
    run()
