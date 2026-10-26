"""Live provisioner admission checks using one empty temporary namespace."""

import json
import subprocess
import time
from copy import deepcopy
from uuid import uuid4

WORKER = "system:serviceaccount:platform:store-provisioner-platform"
ROLE = "urumi-store-provisioner-platform-platform-e0fe7718e6a3-workloads"
POLICY = ROLE
TEST_KEY = "urumi.ai/admission-test"

def kubectl(args, document=None):
    return subprocess.run(
        ["kubectl", "--request-timeout=15s", *args],
        input=None if document is None else json.dumps(document),
        text=True, capture_output=True, timeout=30,
    )

def require(result, label):
    if result.returncode:
        raise RuntimeError(label + "\n" + result.stderr[-2500:])
    return result.stdout

def get_namespace(name):
    output = require(kubectl([
        "get", "namespace", name, "--ignore-not-found", "-o", "json",
    ]), "Could not inspect temporary namespace.")
    return json.loads(output) if output.strip() else None

def check_binding(label, document, message=None):
    result = kubectl([
        "--as=" + WORKER, "--as-group=system:masters",
        "create", "--dry-run=server",
        "-n", document["metadata"]["namespace"], "-f", "-", "-o", "json",
    ], document)
    if message is None:
        output = json.loads(require(result, label))
        assert output["metadata"]["name"] == document["metadata"]["name"]
    else:
        assert result.returncode != 0, "Invalid binding accepted: " + label
        assert POLICY in result.stderr and message in result.stderr, (
            "Expected policy rejection missing: " + label + "\n" + result.stderr
        )
    print("PASS:", label)

def cleanup(name, marker, expected_uid):
    for _ in range(5):
        current = get_namespace(name)
        if current is None:
            print("PASS: temporary namespace is absent.")
            return
        meta = current["metadata"]
        assert name.startswith("urumi-store-") and len(name) == 44
        assert meta["name"] == name
        assert meta.get("annotations", {}).get(TEST_KEY) == marker, (
            "Cleanup refused: namespace has a different test marker."
        )
        assert meta.get("labels", {}).get("urumi.ai/store-id") == name[12:]
        assert meta.get("labels", {}).get("urumi.ai/managed-by") == "store-provisioner"
        assert expected_uid is None or meta["uid"] == expected_uid, (
            "Cleanup refused: namespace was replaced."
        )
        expected_uid = meta["uid"]
        if meta.get("deletionTimestamp"):
            break
        options = {
            "preconditions": {
                "uid": expected_uid,
                "resourceVersion": meta["resourceVersion"],
            },
            "propagationPolicy": "Foreground",
        }
        result = kubectl([
            "delete", "--raw=/api/v1/namespaces/" + name, "-f", "-",
        ], options)
        if result.returncode == 0:
            break
        if "(Conflict)" not in result.stderr:
            require(result, "Temporary namespace cleanup failed.")
        time.sleep(0.25)
    else:
        raise RuntimeError("Cleanup could not obtain a stable namespace version.")

    for _ in range(60):
        current = get_namespace(name)
        if current is None:
            print("PASS: temporary namespace removed with UID/version preconditions.")
            return
        assert current["metadata"]["uid"] == expected_uid, (
            "Cleanup stopped: a replacement namespace appeared."
        )
        time.sleep(1)
    raise RuntimeError("Temporary namespace is still terminating; no finalizers were removed.")

def check_namespace_guards(name, uid):
    namespace_policy = ROLE.removesuffix("-workloads") + "-namespaces"
    document = get_namespace(name)
    assert document["metadata"]["uid"] == uid
    result = kubectl([
        "--as=" + WORKER, "--as-group=system:masters",
        "replace", "--dry-run=server", "-f", "-", "-o", "json",
    ], document)
    assert result.returncode != 0 and namespace_policy in result.stderr
    assert "The store provisioner may only create or delete namespaces." in result.stderr
    print("PASS: namespace update rejected by admission.")

    for with_version in (True, False):
        document = get_namespace(name)
        meta = document["metadata"]
        assert meta["uid"] == uid and not meta.get("deletionTimestamp")
        options = {
            "preconditions": {"uid": uid},
            "propagationPolicy": "Foreground",
            "dryRun": ["All"],
        }
        if with_version:
            options["preconditions"]["resourceVersion"] = meta["resourceVersion"]
        result = kubectl([
            "--as=" + WORKER, "--as-group=system:masters",
            "delete", "--raw=/api/v1/namespaces/" + name + "?dryRun=All",
            "-f", "-",
        ], options)
        if with_version:
            require(result, "Valid namespace deletion dry-run failed.")
            print("PASS: namespace deletion with UID/version accepted in dry-run.")
        else:
            assert result.returncode != 0 and namespace_policy in result.stderr, result.stderr
            assert "Deletion requires the observed namespace UID and resource version." in result.stderr
            print("PASS: namespace deletion without version rejected by admission.")
        current = get_namespace(name)
        assert current["metadata"]["uid"] == uid
        assert not current["metadata"].get("deletionTimestamp"), (
            "A namespace deletion dry-run unexpectedly changed the fixture."
        )

def main():
    identity = uuid4().hex
    marker = uuid4().hex
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
        TEST_KEY: marker,
    }
    namespace = {
        "apiVersion": "v1", "kind": "Namespace",
        "metadata": {"name": name, "labels": labels, "annotations": annotations},
    }
    assert get_namespace(name) is None, "Temporary name already exists."
    uid = None
    try:
        output = require(kubectl(["create", "-f", "-", "-o", "json"], namespace),
                         "Temporary namespace creation failed.")
        created = json.loads(output)
        assert created["metadata"]["annotations"][TEST_KEY] == marker
        uid = created["metadata"]["uid"]
        print("Temporary namespace:", name)
        base = {
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
                "kind": "ClusterRole", "name": ROLE,
            },
            "subjects": [{
                "kind": "ServiceAccount", "name": "store-provisioner-platform",
                "namespace": "platform",
            }],
        }
        check_binding("correct worker binding accepted in dry-run", base)
        role_error = "Only the designated store workload role may be bound."
        subject_error = "The binding must grant access only to this platform's provisioner."
        ownership_error = "The binding ownership must match its managed namespace."
        cases = [
            ("different binding name rejected", ("metadata", "name"), "unexpected-binding", role_error),
            ("cluster-admin binding rejected", ("roleRef", "name"), "cluster-admin", role_error),
            ("different service account rejected", ("subjects", 0, "name"), "default", subject_error),
            ("different subject namespace rejected", ("subjects", 0, "namespace"), "store", subject_error),
            ("empty subjects rejected", ("subjects",), [], subject_error),
            ("additional subject rejected", ("subjects",),
             base["subjects"] + [{"kind": "ServiceAccount", "name": "other-worker", "namespace": "platform"}],
             subject_error),
            ("different owner label rejected",
             ("metadata", "labels", "urumi.ai/owner-id"), "2", ownership_error),
            ("missing binding annotations rejected", ("metadata", "annotations"), {}, ownership_error),
        ]
        for label, path, value, message in cases:
            document = deepcopy(base)
            target = document
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            check_binding(label, document, message)
        for target_namespace in ("platform", "kube-system", "store"):
            document = deepcopy(base)
            document["metadata"]["namespace"] = target_namespace
            check_binding(
                "binding into " + target_namespace + " rejected", document,
                "The target must be an active managed store namespace.",
            )
        check_namespace_guards(name, uid)
        bindings = json.loads(require(kubectl([
            "get", "rolebindings", "-n", name, "-o", "json",
        ]), "Could not check persisted RoleBindings."))
        assert bindings["items"] == [], "A RoleBinding unexpectedly persisted."
        print("PASS: no RoleBinding or worker permission was saved.")
    finally:
        cleanup(name, marker, uid)
    print("Provisioner admission verification passed. No storefront, model or PostgreSQL calls were made.")

if __name__ == "__main__":
    main()
