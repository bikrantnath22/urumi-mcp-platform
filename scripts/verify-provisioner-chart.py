"""Render the worker and two stores; inspect permissions and both network directions."""

import importlib.util
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "chart_security", ROOT / "scripts/verify-chart-security.py",
)
security = importlib.util.module_from_spec(spec)
spec.loader.exec_module(security)
PLATFORM = ["charts/platform/values-local.yaml", "charts/platform/values-apps-local.yaml"]
STORE = ["charts/store/values-local.yaml", "charts/store/values-managed.yaml"]
# Documentation addresses below are test data, never deployment settings.
ENABLED = [
    "storeProvisioner.enabled=true",
    "storeProvisioner.security.enabled=true",
    "storeProvisioner.security.bindNamespaceRole=true",
    "storeProvisioner.security.bindStorageRole=true",
    "storeProvisioner.adminEmail=owner@example.test",
    "storeProvisioner.apiServer.serviceCidr=192.0.2.10/32",
    "storeProvisioner.apiServer.endpointCidr=192.0.2.11/32",
]


def one(documents, kind, name):
    found = [d for d in documents if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, (kind, name, len(found))
    return found[0]


def checksum(documents):
    return one(documents, "Deployment", "store-provisioner")[
        "spec"]["template"]["metadata"]["annotations"]["checksum/settings"]


def check_runtime(documents, namespace, release, ingress, dns):
    account = "store-provisioner-" + release
    settings = one(documents, "ConfigMap", account + "-settings")["data"]
    profile = yaml.safe_load(settings["store-values.yaml"])
    assert profile["provisionerRelease"] == release
    assert profile["networkPolicy"]["enabled"] is True
    assert profile["networkPolicy"]["ingressNamespace"] == ingress
    assert profile["networkPolicy"]["dnsNamespace"] == dns
    assert settings["STORE_WORKER_ENABLED"] == "true"
    assert settings["STORE_PLATFORM_RELEASE"] == release
    assert settings["PGHOST"] == "postgres" and settings["PGUSER"] == "gateway_app"
    assert settings["UPSTREAM_INTERNAL_MCP_URLS"] == "[]"
    assert all(isinstance(v, str) for v in settings.values())
    body = one(documents, "Deployment", "store-provisioner")["spec"]["template"]["spec"]
    assert body["serviceAccountName"] == account
    assert body["automountServiceAccountToken"] is False
    assert one(documents, "ServiceAccount", account)["automountServiceAccountToken"] is False
    assert body["terminationGracePeriodSeconds"] >= 60
    assert body["securityContext"]["runAsUser"] == 10001
    container, = body["containers"]
    assert container["command"] == ["python", "-m", "app.store_worker_main"]
    assert not container.get("ports") and not container.get("envFrom")
    context = container["securityContext"]
    assert context["readOnlyRootFilesystem"] and not context["allowPrivilegeEscalation"]
    assert context["capabilities"]["drop"] == ["ALL"]
    env = {v["name"]: v["valueFrom"] for v in container["env"]}
    assert set(env) == (set(settings) - {"store-values.yaml"}) | {
        "STORE_PLATFORM_NAMESPACE", "PGPASSWORD",
    }
    assert env["PGPASSWORD"]["secretKeyRef"] == {
        "name": "gateway-db-credentials", "key": "PASSWORD",
    }
    assert env["STORE_PLATFORM_NAMESPACE"]["fieldRef"]["fieldPath"] == "metadata.namespace"
    volumes = {v["name"]: v for v in body["volumes"]}
    assert volumes["tmp"]["emptyDir"]["sizeLimit"] == "256Mi"
    projection = volumes["api-credentials"]["projected"]
    assert projection["defaultMode"] == 0o444
    assert projection["sources"] == [
        {"serviceAccountToken": {"path": "token", "expirationSeconds": 3600}},
        {"configMap": {"name": "kube-root-ca.crt", "items": [
            {"key": "ca.crt", "path": "ca.crt"},
        ]}},
    ]
    role, = [d for d in documents if d["kind"] == "ClusterRole"
             and d["metadata"]["name"].endswith("-storage")]
    assert role["rules"] == [{
        "apiGroups": [""], "resources": ["persistentvolumes"], "verbs": ["get", "list"],
    }]
    binding = one(documents, "ClusterRoleBinding", role["metadata"]["name"])
    assert binding["roleRef"]["name"] == role["metadata"]["name"]
    assert binding["subjects"] == [{
        "kind": "ServiceAccount", "name": account, "namespace": namespace,
    }]

def check_network(documents, namespace, release, dns):
    policies = [d for d in documents if d["kind"] == "NetworkPolicy"]
    worker = security.pod(namespace, "store-provisioner")
    worker["labels"]["app.kubernetes.io/instance"] = release
    platform_rules = [p for p in policies if p["metadata"]["namespace"] == namespace]
    resolver = security.pod(dns, "coredns")
    resolver["labels"] = {"k8s-app": "kube-dns"}
    for protocol in ("TCP", "UDP"):
        assert security.permitted(policies, worker, resolver, 53, protocol)
    for address, port in (("192.0.2.10", 443), ("192.0.2.11", 6443)):
        target = security.pod("api", "server", address)
        assert security.permitted(policies, worker, target, port)
        assert not security.permitted(policies, worker, target, port, "UDP")
        assert not security.permitted(policies, worker, target, port + 1)
    assert security.permitted(policies, worker, security.pod(namespace, "postgres"), 5432)
    for app, port in (("postgres", 8000), ("mcp-notes", 8000), ("gateway", 8000)):
        assert not security.permitted(policies, worker, security.pod(namespace, app), port)
    for address in ("1.1.1.1", "169.254.169.254", "192.0.2.12"):
        assert not security.permitted(
            policies, worker, security.pod("outside", "server", address), 443,
        )
    for other in ("different-platform", "untrusted"):
        assert not security.permitted(
            policies, worker, security.pod(other, "postgres"), 5432,
        )
    for identity in ("a" * 32, "b" * 32):
        store = "urumi-store-" + identity
        labels = {"urumi.ai/managed-by": "store-provisioner", "urumi.ai/store-id": identity}
        store_rules = [p for p in policies if p["metadata"]["namespace"] == store]
        for app, port in (("wordpress", 80), ("woo-mcp", 8000), ("mcp-analytics", 8000)):
            target = security.pod(store, app, namespace_labels=labels)
            assert security.permitted(policies, worker, target, port), (app, "route blocked")
            assert security.permitted(platform_rules, worker, target, port)
            assert security.permitted(store_rules, worker, target, port)
            for wrong_port in (81, 443, 3306, 5432, 8001):
                assert not security.permitted(policies, worker, target, wrong_port)
            assert not security.permitted(policies, worker, target, port, "UDP")
            unclassified = security.pod(store, app)
            assert not security.permitted(platform_rules, worker, unclassified, port)
            for source_namespace, source_release in (
                (namespace, "other-release"), ("other-platform", release),
            ):
                stranger = security.pod(source_namespace, "store-provisioner")
                stranger["labels"]["app.kubernetes.io/instance"] = source_release
                assert not security.permitted(store_rules, stranger, target, port)
        assert not security.permitted(
            policies, worker, security.pod(store, "mysql", namespace_labels=labels), 3306,
        )
    print(f"PASS: {namespace}/{release}: API, database, DNS and both stores; denied routes checked")


def rejected(overrides):
    command = ["helm", "template", "platform", "charts/platform", "-n", "platform"]
    for path in PLATFORM:
        command.extend(("-f", path))
    for value in [*ENABLED, *overrides]:
        command.extend(("--set", value))
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0, ("Unsafe configuration rendered", overrides)


def main():
    baseline = security.render("helm", "platform", "platform", "platform", PLATFORM)
    assert not any(d["kind"] == "Deployment" and d["metadata"]["name"] == "store-provisioner"
                   for d in baseline)
    assert not any(d["kind"] == "ClusterRoleBinding" for d in baseline)
    definitions = security.render("helm", "platform", "platform", "platform", PLATFORM, [
        "storeProvisioner.security.enabled=true",
    ])
    assert any(d["kind"] == "ClusterRole" and d["metadata"]["name"].endswith("-storage")
               for d in definitions)
    assert not any(d["kind"] == "ClusterRoleBinding" for d in definitions)
    for namespace, release, ingress, dns in (
        ("platform", "platform", "kube-system", "kube-system"),
        ("apps-test", "preview", "ingress-test", "dns-test"),
    ):
        common = [f"networkPolicy.ingressNamespace={ingress}", f"networkPolicy.dnsNamespace={dns}"]
        platform = security.render(
            "helm", release, "platform", namespace, PLATFORM, [*ENABLED, *common],
        )
        check_runtime(platform, namespace, release, ingress, dns)
        changed = security.render("helm", release, "platform", namespace, PLATFORM, [
            *ENABLED, *common, "networkPolicy.dnsNamespace=changed-dns",
        ])
        assert checksum(changed) != checksum(platform)
        documents = list(platform)
        for index, identity in enumerate(("a" * 32, "b" * 32), 1):
            documents += security.render("helm", "store", "store", "urumi-store-" + identity, STORE, [
                *common, f"gatewayNamespace={namespace}", f"provisionerRelease={release}",
                f"storeHost=preview-{index}.localtest.me",
            ])
        check_network(documents, namespace, release, dns)
    for setting in (
        "storeProvisioner.security.enabled=false",
        "storeProvisioner.security.bindNamespaceRole=false",
        "storeProvisioner.security.bindStorageRole=false",
        "gateway.enabled=false", "networkPolicy.enabled=false",
        "storeProvisioner.adminEmail=invalid", "storeProvisioner.maxJobs=101",
        "storeProvisioner.pollSeconds=0", "storeProvisioner.apiServer.endpointPort=70000",
        "storeProvisioner.apiServer.serviceCidr=192.0.2.0/24",
        "storeProvisioner.apiServer.endpointCidr=999.0.2.10/32",
    ):
        rejected([setting])
    print("PASS: disabled defaults, explicit bindings, rotating token, private config and invalid settings")
    print("Worker chart verification passed. No cluster resources or services changed.")


if __name__ == "__main__":
    main()
