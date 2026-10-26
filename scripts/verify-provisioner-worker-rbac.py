"""Run after the namespace-role grant; verify worker scope with temporary resources."""

import importlib.util
import json
from pathlib import Path
import time
from uuid import uuid4

spec = importlib.util.spec_from_file_location(
    "provisioner_admission_helpers",
    Path(__file__).with_name("verify-provisioner-admission.py"),
)
assert spec is not None and spec.loader is not None
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
admin_call = helpers.kubectl

def worker_call(arguments, document=None):
    assert not any(value.startswith("--as") for value in arguments)
    return admin_call(["--as=" + helpers.WORKER, *arguments], document)

helpers.kubectl = worker_call

def permitted(expected, arguments):
    for _ in range(12):
        result = worker_call(["auth", "can-i", *arguments])
        actual = result.stdout.strip()
        if actual == expected:
            print("PASS:", " ".join(arguments), "->", actual)
            return
        if actual not in ("yes", "no"):
            break
        time.sleep(0.25)
    raise AssertionError(
        f"Expected {expected} for {' '.join(arguments)}; got {actual!r}\n"
        + result.stderr
    )

def rejected(arguments, document, policy, message):
    result = worker_call(arguments, document)
    assert result.returncode != 0, "An invalid request was accepted."
    assert policy in result.stderr and message in result.stderr, result.stderr

def create(document, dry_run=False):
    arguments = ["create", "-f", "-", "-o", "json"]
    if document["metadata"].get("namespace"):
        arguments += ["-n", document["metadata"]["namespace"]]
    if dry_run:
        arguments.append("--dry-run=server")
    output = helpers.require(worker_call(arguments, document), "Worker create failed.")
    return json.loads(output)

def main():
    identity, marker = uuid4().hex, uuid4().hex
    name = "urumi-store-" + identity
    labels = {
        "urumi.ai/managed-by": "store-provisioner",
        "urumi.ai/store-id": identity,
        "urumi.ai/owner-id": "1",
    }
    for mode in ("enforce", "audit", "warn"):
        labels["pod-security.kubernetes.io/" + mode] = "restricted"
        labels["pod-security.kubernetes.io/" + mode + "-version"] = "latest"
    annotations = {
        "urumi.ai/helm-release": "store",
        "urumi.ai/platform-namespace": "platform",
        "urumi.ai/platform-release": "platform",
        helpers.TEST_KEY: marker,
    }
    namespace = {
        "apiVersion": "v1", "kind": "Namespace",
        "metadata": {"name": name, "labels": labels, "annotations": annotations},
    }
    invalid = {
        **namespace,
        "metadata": {**namespace["metadata"], "name": "urumi-probe-" + identity},
    }
    rejected(
        ["create", "--dry-run=server", "-f", "-", "-o", "json"], invalid,
        helpers.ROLE.removesuffix("-workloads") + "-namespaces",
        "The namespace name and managed store identity must match.",
    )
    print("PASS: real worker cannot create an unrelated namespace.")
    assert helpers.get_namespace(name) is None
    uid = None
    try:
        created = create(namespace)
        uid = created["metadata"]["uid"]
        assert created["metadata"]["annotations"][helpers.TEST_KEY] == marker
        print("Temporary worker-created namespace:", name)
        permitted("no", ["create", "configmaps", "-n", name])
        binding = {
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
            "metadata": {
                "name": "urumi-store-provisioner", "namespace": name,
                "labels": {key: labels[key] for key in (
                    "urumi.ai/managed-by", "urumi.ai/store-id", "urumi.ai/owner-id",
                )},
                "annotations": dict(annotations),
            },
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole", "name": helpers.ROLE,
            },
            "subjects": [{
                "kind": "ServiceAccount", "name": "store-provisioner-platform",
                "namespace": "platform",
            }],
        }
        forbidden = {
            **binding,
            "metadata": {**binding["metadata"], "namespace": "platform"},
        }
        rejected(
            ["create", "--dry-run=server", "-n", "platform", "-f", "-", "-o", "json"],
            forbidden, helpers.ROLE,
            "The target must be an active managed store namespace.",
        )
        print("PASS: real worker cannot bind its workload role into platform.")
        create(binding)
        print("PASS: real worker created only its fixed store RoleBinding.")
        for arguments in (
            ["create", "configmaps", "-n", name],
            ["get", "configmaps", "-n", name],
            ["delete", "configmaps", "-n", name],
            ["create", "secrets", "-n", name],
            ["create", "deployments", "-n", name],
            ["create", "jobs", "-n", name],
            ["get", "pods", "-n", name],
        ):
            permitted("yes", arguments)
        for arguments in (
            ["get", "secrets", "-n", "platform"],
            ["get", "secrets", "-n", "store"],
            ["get", "secrets", "-n", "urumi-store-" + uuid4().hex],
            ["create", "deployments", "-n", "platform"],
            ["create", "pods", "-n", name],
            ["create", "pods", "--subresource=exec", "-n", name],
            ["create", "serviceaccounts", "--subresource=token", "-n", name],
            ["create", "roles", "-n", name],
            ["update", "rolebindings/urumi-store-provisioner", "-n", name],
            ["patch", "namespaces"],
        ):
            permitted("no", arguments)
        configmap = {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "rbac-probe", "namespace": name},
            "data": {"probe": "ok"},
        }
        create(configmap)
        saved = json.loads(helpers.require(worker_call([
            "get", "configmap", "rbac-probe", "-n", name, "-o", "json",
        ]), "Worker read failed."))
        assert saved["data"] == {"probe": "ok"}
        print("PASS: worker created and read its temporary ConfigMap.")
        create({
            "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "rbac-probe", "namespace": name},
            "stringData": {"probe": "verification-only"},
        }, dry_run=True)
        create({
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "rbac-probe", "namespace": name},
            "spec": {
                "replicas": 0,
                "selector": {"matchLabels": {"app": "rbac-probe"}},
                "template": {
                    "metadata": {"labels": {"app": "rbac-probe"}},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "securityContext": {
                            "runAsNonRoot": True, "runAsUser": 10001,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [{
                            "name": "probe", "image": "woo-mcp:0.1.5",
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }],
                    },
                },
            },
        }, dry_run=True)
        print("PASS: store Secret and Deployment creation work in dry-run.")
    finally:
        try:
            helpers.cleanup(name, marker, uid)
        except Exception:
            print("Worker cleanup failed; attempting UID-checked administrator cleanup.")
            helpers.kubectl = admin_call
            try:
                helpers.cleanup(name, marker, uid)
            finally:
                helpers.kubectl = worker_call
            raise
    permitted("no", ["create", "configmaps", "-n", name])
    print("Real worker RBAC verification passed; temporary namespace and binding removed.")
    print("No worker pod, real Secret or application store was created.")
    print("No model or PostgreSQL calls were made.")

if __name__ == "__main__":
    main()
