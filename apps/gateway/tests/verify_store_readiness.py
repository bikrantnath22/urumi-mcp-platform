"""Offline rollout/bootstrap checks; no database, subprocess or network calls."""

from copy import deepcopy
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.store_namespace import StoreNamespace
from app.store_readiness import (
    StoreBootstrapFailed, StoreReadinessError, resource_read_paths, resource_ready,
)


IDENTITY = StoreNamespace(uuid4(), 1)
NAMES = ("mysql", "wordpress", "woo-mcp", "mcp-analytics", "store-bootstrap")


def fixture(name):
    job = name == "store-bootstrap"
    kind = "Job" if job else ("StatefulSet" if name == "mysql" else "Deployment")
    metadata = {
        "name": name, "namespace": IDENTITY.name, "uid": "fixture-uid",
        "resourceVersion": "17", "generation": 2,
        "labels": {"app.kubernetes.io/managed-by": "Helm"},
        "annotations": {
            "meta.helm.sh/release-name": "store",
            "meta.helm.sh/release-namespace": IDENTITY.name,
        },
    }
    if job:
        # Helm hooks do not receive the normal release ownership metadata.
        metadata["labels"] = {}
        metadata["annotations"] = {
            "helm.sh/hook": "post-install,post-upgrade",
            "helm.sh/hook-delete-policy": "before-hook-creation",
        }
        spec = {"activeDeadlineSeconds": 900}
        status = {
            "succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}],
        }
    else:
        spec = {"replicas": 1}
        status = {
            "observedGeneration": 2, "replicas": 1, "updatedReplicas": 1,
            "readyReplicas": 1, "availableReplicas": 1, "currentReplicas": 1,
            "currentRevision": "mysql-revision", "updateRevision": "mysql-revision",
            "conditions": [{"type": "Available", "status": "True"}],
        }
    return {
        "apiVersion": "batch/v1" if job else "apps/v1",
        "kind": kind, "metadata": metadata, "spec": spec, "status": status,
    }


def ready(document, name, identity=IDENTITY, seconds=900):
    return resource_ready(document, identity, name, bootstrap_seconds=seconds)


def rejected(operation, kind=StoreReadinessError):
    try:
        operation()
    except kind as error:
        assert "PRIVATE" not in str(error)
        return
    raise AssertionError("Unsafe workload status was accepted")


paths = resource_read_paths(IDENTITY)
assert set(paths) == set(NAMES)
for name in NAMES:
    group = "batch/v1" if name == "store-bootstrap" else "apps/v1"
    assert paths[name].startswith(f"/apis/{group}/namespaces/{IDENTITY.name}/")
    document = fixture(name)
    snapshot = deepcopy(document)
    assert ready(document, name) is True
    assert document == snapshot
    assert ready(None, name) is False
    del document["status"]
    assert ready(document, name) is False
paths.clear()
assert len(resource_read_paths(IDENTITY)) == 5
print("All five fixed resources pass; missing resources/status remain pending and inputs stay unchanged")

for name in NAMES:
    for key, value in (
        ("name", "another-resource"), ("namespace", "platform"),
        ("uid", None), ("resourceVersion", None), ("generation", True),
        ("deletionTimestamp", "2026-10-18T00:00:00Z"),
    ):
        document = fixture(name)
        document["metadata"][key] = value
        rejected(lambda: ready(document, name))
    document = fixture(name)
    document["kind"] = "Secret"
    rejected(lambda: ready(document, name))
    rejected(lambda: ready(fixture(name), name, StoreNamespace(uuid4(), 2)))
    for key, value in (("spec", None), ("status", None), ("metadata", [])):
        document = fixture(name)
        document[key] = value
        rejected(lambda: ready(document, name))
rejected(lambda: ready(None, "another-resource"))
rejected(lambda: resource_read_paths(object()))
print("Wrong namespaces, resource identities, terminating objects and malformed metadata reject verification")

for name in NAMES[:-1]:
    for key, value in (
        ("observedGeneration", 1), ("observedGeneration", 3),
        ("replicas", 0), ("replicas", 2), ("updatedReplicas", 0),
        ("readyReplicas", 0),
    ):
        document = fixture(name)
        document["status"][key] = value
        assert ready(document, name) is False
    for value in (True, -1, "1", 1.0):
        document = fixture(name)
        document["status"]["readyReplicas"] = value
        rejected(lambda: ready(document, name))
    document = fixture(name)
    document["metadata"]["annotations"]["meta.helm.sh/release-namespace"] = "other"
    rejected(lambda: ready(document, name))
    document = fixture(name)
    document["spec"]["replicas"] = 2
    rejected(lambda: ready(document, name))
for name in NAMES[1:-1]:
    for key, value in (("availableReplicas", 0), ("unavailableReplicas", 1)):
        document = fixture(name)
        document["status"][key] = value
        assert ready(document, name) is False
    document = fixture(name)
    document["status"]["conditions"].append({"type": "ReplicaFailure", "status": "True"})
    assert ready(document, name) is False
for key, value in (("currentRevision", ""), ("updateRevision", "old-revision"), ("currentReplicas", 0)):
    document = fixture("mysql")
    document["status"][key] = value
    assert ready(document, "mysql") is False
print("Current generations, replica counts, Helm ownership and MySQL revisions must match")

job = "store-bootstrap"
for conditions in (
    [], [{"type": "SuccessCriteriaMet", "status": "True"}],
    [{"type": "Complete", "status": "False"}],
):
    document = fixture(job)
    document["status"]["conditions"] = conditions
    assert ready(document, job) is False
for key, value in (("active", 1), ("terminating", 1), ("succeeded", 0)):
    document = fixture(job)
    document["status"][key] = value
    assert ready(document, job) is False
for condition in ("Failed", "FailureTarget"):
    document = fixture(job)
    document["status"]["conditions"].append({
        "type": condition, "status": "True", "message": "PRIVATE DIAGNOSTIC",
    })
    rejected(lambda: ready(document, job), StoreBootstrapFailed)
document = fixture(job)
document["status"]["failed"] = 2
assert ready(document, job) is True
for key, value in (
    ("activeDeadlineSeconds", 901), ("activeDeadlineSeconds", True),
    ("completions", 2), ("parallelism", 2), ("suspend", True),
):
    document = fixture(job)
    document["spec"][key] = value
    rejected(lambda: ready(document, job))
rejected(lambda: ready(fixture(job), job, seconds=True))
print("Bootstrap needs terminal completion; failures, active pods and changed timeouts cannot pass")

for conditions in (
    None, {}, [None], [{"type": "Complete", "status": True}],
    [{"type": "Complete", "status": "True"}] * 2,
    [{"type": "Complete", "status": "True"}] * 129,
):
    document = fixture(job)
    document["status"]["conditions"] = conditions
    rejected(lambda: ready(document, job))
assert set(resource_read_paths(IDENTITY).values()) == {
    f"/apis/apps/v1/namespaces/{IDENTITY.name}/statefulsets/mysql",
    f"/apis/apps/v1/namespaces/{IDENTITY.name}/deployments/wordpress",
    f"/apis/apps/v1/namespaces/{IDENTITY.name}/deployments/woo-mcp",
    f"/apis/apps/v1/namespaces/{IDENTITY.name}/deployments/mcp-analytics",
    f"/apis/batch/v1/namespaces/{IDENTITY.name}/jobs/store-bootstrap",
}
print("Fixed GET paths and safe condition handling passed")
managed = fixture("store-bootstrap")
managed["metadata"]["labels"] = {"app.kubernetes.io/managed-by": "Helm"}
managed["metadata"]["annotations"] = {
    "meta.helm.sh/release-name": "store",
    "meta.helm.sh/release-namespace": IDENTITY.name,
}
assert resource_ready(managed, IDENTITY, "store-bootstrap", bootstrap_seconds=900, managed_bootstrap=True)
rejected(lambda: ready(managed, "store-bootstrap"))
rejected(lambda: resource_ready(fixture("store-bootstrap"), IDENTITY, "store-bootstrap", bootstrap_seconds=900, managed_bootstrap=True))
for key, value in (("meta.helm.sh/release-namespace", "other"), ("helm.sh/hook", "post-install")):
    changed = deepcopy(managed)
    changed["metadata"]["annotations"][key] = value
    rejected(lambda: resource_ready(changed, IDENTITY, "store-bootstrap", bootstrap_seconds=900, managed_bootstrap=True))
for field in ("active", "terminating"):
    changed = deepcopy(managed)
    changed["status"][field] = 1
    assert not resource_ready(changed, IDENTITY, "store-bootstrap", bootstrap_seconds=900, managed_bootstrap=True)
print("Durable bootstrap requires Helm ownership and terminal completion; legacy hooks remain independently checked")
print("Workload readiness verification passed. No live services or processes were used.")
