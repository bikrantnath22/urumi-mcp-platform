"""Check rendered Helm security settings without contacting a cluster.

Run from the repository root:
    uv run --no-project --with pyyaml python scripts/verify-chart-security.py
Live CNI enforcement and image startup must also be checked after deployment.
"""

import argparse
import ipaddress
from pathlib import Path
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]


def render(helm, release, chart, namespace, values, overrides=()):
    command = [helm, "template", release, f"charts/{chart}", "-n", namespace]
    for value in values:
        command.extend(("-f", value))
    for override in overrides:
        command.extend(("--set", override))
    output = subprocess.check_output(command, cwd=ROOT, text=True)
    documents = [doc for doc in yaml.safe_load_all(output) if doc]
    names = [(doc["kind"], doc["metadata"]["name"]) for doc in documents]
    assert len(names) == len(set(names)), "Duplicate resources in rendered chart"
    for doc in documents:
        doc["metadata"].setdefault("namespace", namespace)
    return documents


def selected(selector, labels):
    if any(labels.get(key) != value for key, value in selector.get("matchLabels", {}).items()):
        return False
    for expression in selector.get("matchExpressions", []):
        key, operator = expression["key"], expression["operator"]
        values = expression.get("values", [])
        if operator == "In" and labels.get(key) not in values:
            return False
        if operator == "NotIn" and labels.get(key) in values:
            return False
        if operator == "Exists" and key not in labels:
            return False
        if operator == "DoesNotExist" and key in labels:
            return False
    return True


def pod(namespace, app, ip="10.42.0.10", *, namespace_labels=None):
    return {
        "namespace": namespace, "labels": {"app": app}, "ip": ip,
        "namespace_labels": dict(namespace_labels or {}),
    }


def peer_matches(peer, target, namespace):
    if "ipBlock" in peer:
        address = ipaddress.ip_address(target["ip"])
        block = peer["ipBlock"]
        return address in ipaddress.ip_network(block["cidr"]) and not any(
            address in ipaddress.ip_network(item) for item in block.get("except", [])
        )
    if "namespaceSelector" in peer:
        labels = dict(target.get("namespace_labels", {}))
        labels["kubernetes.io/metadata.name"] = target["namespace"]
        if not selected(peer["namespaceSelector"], labels):
            return False
    elif "podSelector" in peer and target["namespace"] != namespace:
        return False
    return selected(peer.get("podSelector", {}), target["labels"])


def permitted(policies, source, destination, port, protocol="TCP"):
    for direction, subject, other, peer_key in (
        ("Ingress", destination, source, "from"),
        ("Egress", source, destination, "to"),
    ):
        applicable = [policy for policy in policies
                      if policy["metadata"]["namespace"] == subject["namespace"]
                      and direction in policy["spec"]["policyTypes"]
                      and selected(policy["spec"]["podSelector"], subject["labels"])]
        if not applicable:
            continue
        def allows(policy):
            for rule in policy["spec"].get(direction.lower(), []):
                ports = rule.get("ports", [])
                if ports and not any(item.get("protocol", "TCP") == protocol
                                     and item.get("port", port) == port for item in ports):
                    continue
                peers = rule.get(peer_key, [])
                if not peers or any(peer_matches(peer, other, policy["metadata"]["namespace"]) for peer in peers):
                    return True
            return False
        if not any(allows(policy) for policy in applicable):
            return False
    return True


def check_workloads(documents):
    accounts = {(doc["metadata"]["namespace"], doc["metadata"]["name"]): doc
                for doc in documents if doc["kind"] == "ServiceAccount"}
    for doc in documents:
        assert doc["kind"] not in {"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"}, "Unexpected Kubernetes API grant"
        if doc["kind"] not in {"Deployment", "StatefulSet", "Job"}:
            continue
        spec = doc["spec"]["template"]["spec"]
        context = spec["securityContext"]
        assert context["runAsNonRoot"] is True and context["runAsUser"] > 0
        assert context["seccompProfile"]["type"] == "RuntimeDefault"
        assert spec["automountServiceAccountToken"] is False
        account = accounts[(doc["metadata"]["namespace"], spec["serviceAccountName"])]
        assert account["automountServiceAccountToken"] is False
        for container in spec.get("initContainers", []) + spec["containers"]:
            security = container["securityContext"]
            assert security["allowPrivilegeEscalation"] is False
            assert "ALL" in security["capabilities"]["drop"]
            assert container["resources"]["requests"] and container["resources"]["limits"]
        if doc["kind"] != "Job":
            for container in spec["containers"]:
                assert container["readinessProbe"] and container["livenessProbe"]


def check_network(documents, platform, store, ingress="kube-system", dns="kube-system",
                  *, namespace_labels=None):
    policies = [doc for doc in documents if doc["kind"] == "NetworkPolicy"]
    for namespace in (platform, store):
        deny = next(doc for doc in policies if doc["metadata"]["namespace"] == namespace
                    and doc["metadata"]["name"] == "default-deny")
        assert deny["spec"]["podSelector"] == {}
        assert set(deny["spec"]["policyTypes"]) == {"Ingress", "Egress"}
        assert not deny["spec"].get("ingress") and not deny["spec"].get("egress")
    apps = {name: pod(
        namespace, name, namespace_labels=(namespace_labels or {}).get(namespace),
    ) for namespace, names in (
        (platform, ("gateway", "dashboard", "postgres", "mcp-notes")),
        (store, ("wordpress", "mysql", "woo-mcp", "mcp-analytics", "store-bootstrap")),
    ) for name in names}
    ports = {"gateway": 8000, "dashboard": 8080, "postgres": 5432, "mcp-notes": 8000,
             "wordpress": 80, "mysql": 3306, "woo-mcp": 8000, "mcp-analytics": 8000, "store-bootstrap": 8080}
    allowed = {("gateway", target) for target in ("postgres", "mcp-notes", "woo-mcp", "mcp-analytics")}
    allowed |= {("mcp-notes", "postgres"), ("wordpress", "mysql"),
                ("woo-mcp", "wordpress"), ("mcp-analytics", "wordpress"),
                ("store-bootstrap", "wordpress"), ("store-bootstrap", "mysql")}
    if any(doc["metadata"]["namespace"] == store and doc["metadata"]["name"] == "wordpress-to-setup-gateway" for doc in policies):
        allowed.add(("wordpress", "gateway"))
    for source, source_pod in apps.items():
        for target, target_pod in apps.items():
            if source == target:
                continue  # Local traffic and reply packets are outside this model.
            actual = permitted(policies, source_pod, target_pod, ports[target])
            assert actual == ((source, target) in allowed), f"Unexpected route: {source} -> {target}: {actual}"
    traefik = pod(ingress, "traefik")
    traefik["labels"] = {"app.kubernetes.io/name": "traefik"}
    for target in ("wordpress", "gateway", "dashboard"):
        assert permitted(policies, traefik, apps[target], ports[target]), f"Ingress blocked: {target}"
    resolver = pod(dns, "coredns")
    resolver["labels"] = {"k8s-app": "kube-dns"}
    for name, source in apps.items():
        for protocol in ("TCP", "UDP"):
            assert permitted(policies, source, resolver, 53, protocol), f"DNS blocked: {name}"
        for address in ("1.1.1.1", "2606:4700:4700::1111"):
            target = pod("external", "public", address)
            assert permitted(policies, source, target, 443) == (name in {"gateway", "wordpress", "store-bootstrap"})
            assert not permitted(policies, source, target, 80), f"Unexpected public HTTP: {name}"
        for address in ("169.254.169.254", "100.100.100.200", "168.63.129.16", "172.20.0.2", "10.43.0.1", "fd00:ec2::254"):
            assert not permitted(policies, source, pod("external", "private", address), 443), f"Private egress allowed: {name} -> {address}"
    assert not permitted(policies, pod("untrusted", "gateway"), apps["woo-mcp"], 8000)
    assert not permitted(policies, pod(platform, "untrusted"), apps["woo-mcp"], 8000)



def check_managed_network(helm, base_documents, platform, ingress, dns, store_values):
    documents = list(base_documents)
    namespace_labels = {}

    for index, identity in enumerate(("a" * 32, "b" * 32), 1):
        namespace = "urumi-store-" + identity
        namespace_labels[namespace] = {
            "urumi.ai/managed-by": "store-provisioner",
            "urumi.ai/store-id": identity,
        }
        documents += render(
            helm, "store", "store", namespace,
            [*store_values, "charts/store/values-managed.yaml"],
            [
                f"gatewayNamespace={platform}",
                f"networkPolicy.ingressNamespace={ingress}",
                f"networkPolicy.dnsNamespace={dns}",
                f"storeHost=managed-{index}.localtest.me",
                "wordpressSetup.enabled=true",
                "storeAccess.enabled=true",
                "storeAccess.storeId=" + "-".join((identity[:8], identity[8:12], identity[12:16], identity[16:20], identity[20:])),
            ],
        )

    check_workloads(documents)
    for namespace in namespace_labels:
        check_network(
            documents, platform, namespace, ingress, dns,
            namespace_labels=namespace_labels,
        )

    policies = [doc for doc in documents if doc["kind"] == "NetworkPolicy"]
    gateway_policies = [
        doc for doc in policies if doc["metadata"]["namespace"] == platform
    ]
    gateway = pod(platform, "gateway")
    ports = {
        "woo-mcp": 8000, "mcp-analytics": 8000,
        "wordpress": 80, "mysql": 3306, "store-bootstrap": 8080,
    }
    stores = [
        {
            app: pod(namespace, app, namespace_labels=labels)
            for app in ports
        }
        for namespace, labels in namespace_labels.items()
    ]

    for namespace, labels in namespace_labels.items():
        wordpress = pod(namespace, "wordpress", namespace_labels=labels)
        assert permitted(policies, wordpress, gateway, 8000)
        assert not permitted(policies, wordpress, gateway, 8001)
        assert not permitted(policies, wordpress, gateway, 8000, "UDP")
        assert not permitted(policies, pod(namespace, "wordpress", namespace_labels={}), gateway, 8000)
        assert not permitted(policies, pod(namespace, "store-bootstrap", namespace_labels=labels), gateway, 8000)
        for app in ("woo-mcp", "mcp-analytics"):
            target = pod(namespace, app, namespace_labels=labels)
            assert permitted(policies, gateway, target, 8000)

            # Check gateway egress independently of destination ingress.
            assert permitted(gateway_policies, gateway, target, 8000)
            for port in (80, 443, 3306, 5432, 8001):
                assert not permitted(gateway_policies, gateway, target, port)
            assert not permitted(
                gateway_policies, gateway, target, 8000, "UDP",
            )

            for incomplete in (
                {},
                {"urumi.ai/managed-by": "store-provisioner"},
                {"urumi.ai/store-id": labels["urumi.ai/store-id"]},
                {
                    "urumi.ai/managed-by": "different-controller",
                    "urumi.ai/store-id": labels["urumi.ai/store-id"],
                },
            ):
                unclassified = pod(
                    namespace, app, namespace_labels=incomplete,
                )
                assert not permitted(
                    gateway_policies, gateway, unclassified, 8000,
                )

            for source in (
                pod(platform, "dashboard"), pod(platform, "untrusted"),
            ):
                assert not permitted(
                    gateway_policies, source, target, 8000,
                )
            assert not permitted(
                policies, pod("untrusted", "gateway"), target, 8000,
            )

        for app in ("wordpress", "mysql", "store-bootstrap", "untrusted"):
            target = pod(namespace, app, namespace_labels=labels)
            assert not permitted(
                gateway_policies, gateway, target, 8000,
            )

    for source_store, target_store in (
        (stores[0], stores[1]), (stores[1], stores[0]),
    ):
        for source in source_store.values():
            for app, destination in target_store.items():
                assert not permitted(
                    policies, source, destination, ports[app],
                ), f"Cross-store route allowed: {source['labels']} -> {app}"

    print(
        f"PASS: {platform}: two managed stores retain required routes; "
        "gateway labels/pods/ports and cross-store isolation checked"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helm", default="helm")
    args = parser.parse_args()
    store_values = ["charts/store/values-local.yaml"]
    platform_values = ["charts/platform/values-local.yaml", "charts/platform/values-apps-local.yaml"]
    for platform, store, ingress, dns in (
        ("platform", "store", "kube-system", "kube-system"),
        ("apps-test", "shop-test", "ingress-test", "dns-test"),
    ):
        common = [f"networkPolicy.ingressNamespace={ingress}", f"networkPolicy.dnsNamespace={dns}"]
        documents = render(args.helm, "store", "store", store, store_values, [*common, f"gatewayNamespace={platform}"])
        documents += render(args.helm, "platform", "platform", platform, platform_values, [*common, f"networkPolicy.storeNamespace={store}"])
        check_workloads(documents)
        check_network(documents, platform, store, ingress, dns)
        check_managed_network(args.helm, documents, platform, ingress, dns, store_values)
        print(f"PASS: {platform}/{store}: non-root, tokenless accounts, required routes, denied routes, DNS and public HTTPS")
    minimal = render(args.helm, "platform", "platform", "platform", ["charts/platform/values-local.yaml"])
    check_workloads(minimal)
    assert not any(
        doc["kind"] == "NetworkPolicy"
        and doc["metadata"]["name"] == "gateway-to-managed-store-mcps"
        for doc in minimal
    )
    disabled = render(
        args.helm, "platform", "platform", "platform", platform_values,
        ["networkPolicy.enabled=false"],
    )
    assert not any(
        doc["kind"] == "NetworkPolicy"
        and doc["metadata"]["name"] == "gateway-to-managed-store-mcps"
        for doc in disabled
    )
    print("PASS: managed-store egress is absent without a gateway or with network policies disabled")
    print("PASS: database/notes-only platform chart still renders")
    print("Rendered chart checks passed. Live network enforcement and container startup remain separate checks.")


if __name__ == "__main__":
    main()
