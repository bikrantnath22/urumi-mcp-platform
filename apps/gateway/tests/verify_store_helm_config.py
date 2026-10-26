"""Verify fixed Helm inputs without writing files or contacting any service."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import json
import time
from uuid import uuid4

from app.store_cluster_scope import StoreClusterScope
from app.store_helm_config import (
    HELM_EXECUTABLE, KUBERNETES_SERVER, SERVICE_ACCOUNT_DIRECTORY,
    STORE_CHART, STORE_ENVIRONMENT_VALUES, STORE_MANAGED_VALUES, StoreHelmConfig,
)
from app.store_job_guard import CheckedStoreJob
from app.store_jobs import StoreJobClaim
from app.store_namespace import StoreNamespace


def rejected(operation):
    try:
        operation()
    except ValueError:
        return
    raise AssertionError("An unsafe configuration was accepted")


def fixture(slug, number):
    identity = StoreNamespace(uuid4(), number)
    now = datetime.now(timezone.utc)
    claim = StoreJobClaim(
        operation_id=uuid4(), store_id=identity.store_id, owner_id=number,
        action="provision", generation=1, attempt=1,
        namespace=identity.name, release_name="store",
        name=slug, slug=slug, url=f"http://{slug}.localtest.me",
        deadline_at=now + timedelta(seconds=1200),
        checked_at=now, lease_expires_at=now + timedelta(seconds=60),
        lease_token=uuid4(),
    )
    checked = CheckedStoreJob(
        claim=claim, namespace_uid=f"namespace-uid-{number}",
        for_cleanup=False, stop_at=time.monotonic() + 50,
        credentials_secret_uid=f"secret-uid-{number}",
    )
    config = StoreHelmConfig(f"/tmp/urumi-helm-fixture{number:02d}", identity)
    return config, checked


scope = StoreClusterScope("platform", "platform")
first, first_checked = fixture("assam-tea", 1)
second, second_checked = fixture("tulsi-tea", 2)

for config, checked in ((first, first_checked), (second, second_checked)):
    document = config.kubeconfig()
    cluster = document["clusters"][0]["cluster"]
    assert cluster == {
        "server": KUBERNETES_SERVER,
        "certificate-authority": SERVICE_ACCOUNT_DIRECTORY + "/ca.crt",
        "insecure-skip-tls-verify": False,
    }
    assert document["users"][0]["user"] == {
        "tokenFile": SERVICE_ACCOUNT_DIRECTORY + "/token",
    }
    assert document["contexts"][0]["context"]["namespace"] == config.identity.name
    assert document["current-context"] == "urumi-store"
    json.dumps(document)
    document["users"][0]["user"]["token"] = "fixture"
    assert "token" not in config.kubeconfig()["users"][0]["user"]

    environment = config.environment()
    assert environment["KUBECONFIG"] == config.kubeconfig_path
    assert environment["HELM_DRIVER"] == "secret"
    assert environment["HELM_NAMESPACE"] == config.identity.name
    assert not any("PROXY" in key.upper() for key in environment)
    environment["HELM_DRIVER"] = "changed"
    assert config.environment()["HELM_DRIVER"] == "secret"

    for args in (config.list_arguments(), config.status_arguments()):
        assert isinstance(args, tuple)
        assert args[args.index("--namespace") + 1] == config.identity.name
        assert args[args.index("--kubeconfig") + 1] == config.kubeconfig_path
        assert "--all-namespaces" not in args

    plan, args, payload = config.installation(checked, scope)
    assert args[:3] == ("install", "store", STORE_CHART)
    assert args[args.index("--namespace") + 1] == config.identity.name
    values = [args[i + 1] for i, arg in enumerate(args) if arg == "--values"]
    assert values == [STORE_MANAGED_VALUES, STORE_ENVIRONMENT_VALUES, "-"]
    assert "--wait=false" in args and "--wait-for-jobs" not in args
    assert args[args.index("--timeout") + 1] == f"{plan.bootstrap_seconds}s"
    assert 1100 < plan.bootstrap_seconds <= 1200
    assert config.identity.store_id.hex in args[args.index("--labels") + 1]
    forbidden = {
        "--create-namespace", "--take-ownership", "--replace", "--atomic",
        "--no-hooks", "--kube-token", "--kube-as-user", "--kube-apiserver",
        "--kube-insecure-skip-tls-verify", "--post-renderer", "--repo",
    }
    assert not forbidden.intersection(args)
    data = json.loads(payload)
    assert data == plan.values()
    assert data["secretName"] == "store-secrets"
    assert data["analytics"]["secretName"] == "store-secrets"
    assert data["storeHost"] == checked.claim.slug + ".localtest.me"
    assert data["bootstrap"]["managedJob"] is True
    assert len(payload) <= 64 * 1024

print("Two stores keep separate namespaces, private paths and store addresses")
print("TLS verification settings, token-file references and fixed Helm inputs passed")

from app.store_helm_release import StoreRelease
recoverable = StoreRelease(first.identity, "failed", 1200, 1, True)
resumed = replace(first_checked, claim=replace(first_checked.claim, attempt=2))
_, args, payload = first.recovery(resumed, scope, recoverable)
assert args[:3] == ("upgrade", "store", STORE_CHART)
assert "--reuse-values" in args and "--wait=false" in args
assert "--values" not in args and payload == b""
assert not {"--force", "--take-ownership", "--replace", "--reset-values", "--no-hooks"}.intersection(args)
for checked, release in (
    (first_checked, recoverable),
    (resumed, replace(recoverable, state="deployed")),
    (resumed, replace(recoverable, managed_bootstrap=False)),
    (resumed, replace(recoverable, revision=8)),
    (resumed, replace(recoverable, identity=second.identity)),
):
    rejected(lambda checked=checked, release=release: first.recovery(checked, scope, release))
print("Replacement attempts reconcile only owned interrupted releases without resetting saved values or bootstrap")

rejected(lambda: first.installation(second_checked, scope))
for field in (
    {"for_cleanup": True},
    {"namespace_uid": None},
    {"credentials_secret_uid": None},
    {"stop_at": time.monotonic() - 1},
    {"claim": replace(first_checked.claim, url="https://unexpected.example")},
):
    rejected(lambda field=field: first.installation(replace(first_checked, **field), scope))

for workspace in (
    "", "/tmp", "/tmp/urumi-helm-short", "/tmp/urumi-helm-12345678/",
    "/tmp/urumi-helm-../../platform", "/home/user/.kube",
):
    rejected(lambda workspace=workspace: StoreHelmConfig(workspace, first.identity))

try:
    first.workspace = "/tmp/changed"
except FrozenInstanceError:
    pass
else:
    raise AssertionError("Configuration was mutable")

assert first.kubeconfig_path != second.kubeconfig_path
assert HELM_EXECUTABLE == "/usr/local/bin/helm"
print("Cross-store plans, unsafe paths, missing bindings and expired observations are rejected")
print("Helm configuration verification passed. No files, processes or services were used.")
