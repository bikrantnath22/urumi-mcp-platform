"""Real Helm rendering checks; no cluster, database or store changes."""

from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from app.store_cluster_scope import StoreClusterScope
from app.store_helm_plan import StoreHelmPlan
from app.store_namespace import StoreNamespace
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[3]


def rendered(identity, *, managed, upgrade=False):
    command = [
        "helm", "template", "store", str(ROOT / "charts/store"), "-n", identity.name,
        "-f", str(ROOT / "charts/store/values-local.yaml"),
        "-f", str(ROOT / "charts/store/values-managed.yaml"),
        "--set", "bootstrap.managedJob=" + str(managed).lower(),
    ]
    if upgrade:
        command.append("--is-upgrade")
    result = subprocess.run(command, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, "Chart rendering failed: " + result.stderr
    return {
        (item["kind"], item["metadata"]["name"]): item
        for item in yaml.safe_load_all(result.stdout) if item
    }


for owner in (7, 8):
    identity = StoreNamespace(uuid4(), owner)
    legacy = rendered(identity, managed=False)
    managed = rendered(identity, managed=True)
    reconciled = rendered(identity, managed=True, upgrade=True)
    assert managed == reconciled, "Recovery changed an existing resource or immutable Job template."
    assert set(legacy) == set(managed)
    job_key = ("Job", "store-bootstrap")
    assert managed[job_key]["spec"] == legacy[job_key]["spec"]
    assert not managed[job_key]["metadata"].get("annotations")
    assert legacy[job_key]["metadata"]["annotations"] == {
        "helm.sh/hook": "post-install,post-upgrade",
        "helm.sh/hook-delete-policy": "before-hook-creation",
    }
    assert all(managed[key] == legacy[key] for key in managed if key != job_key)
    plan = StoreHelmPlan(
        identity, StoreClusterScope("platform", "platform"), "namespace-uid", "secret-uid",
        "fixture.localtest.me", "http", 1200, "Fixture store",
    )
    assert plan.values()["bootstrap"] == {"activeDeadlineSeconds": 1200, "managedJob": True}
print("Two stores render durable bootstrap Jobs; reconciliation preserves every resource and the original Job specification")
print("Legacy hook mode is unchanged; managed installations opt into durable Jobs explicitly")

invalid = subprocess.run([
    "helm", "template", "store", str(ROOT / "charts/store"),
    "--set-string", "bootstrap.managedJob=false",
], text=True, capture_output=True, timeout=30)
assert invalid.returncode != 0 and "bootstrap.managedJob must be a boolean" in invalid.stderr
print("Invalid bootstrap modes fail before an installation can start")
print("Recovery chart verification passed. No database or Kubernetes calls were made.")
