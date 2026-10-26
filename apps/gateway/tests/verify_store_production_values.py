"""Render production/local Helm profiles offline; no services or credentials used.

From apps/gateway:
    uv run --with pyyaml python tests/verify_store_production_values.py
"""
import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import shutil
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "apps/gateway"))
from app.store_cluster_scope import StoreClusterScope
from app.store_helm_config import (
    StoreHelmConfig, STORE_ENVIRONMENT_VALUES, STORE_MANAGED_VALUES,
)
from app.store_job_guard import CheckedStoreJob
from app.store_helm_release import StoreRelease
from app.store_jobs import StoreJobClaim
from app.store_namespace import StoreNamespace


def merge(base, changes):
    result = deepcopy(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def invoke(helm, chart, namespace, files, overrides=None, *, reject=None, chart_path=None, upgrade=False):
    cmd = [helm, "template", "platform" if chart == "platform" else "store",
           str(chart_path or ROOT / "charts" / chart), "--namespace", namespace,
           "--kube-version", "1.30.0"]
    if upgrade:
        cmd.append("--is-upgrade")
    for name in files:
        cmd += ["--values", str(name)]
    if overrides is not None:
        cmd += ["--values", "-"]
    result = subprocess.run(cmd, input=json.dumps(overrides) if overrides is not None else None,
                            text=True, capture_output=True, timeout=30)
    if reject is not None:
        assert result.returncode != 0, ("Unsafe values were accepted", overrides)
        assert reject.lower() in result.stderr.lower(), result.stderr
        return
    assert result.returncode == 0, result.stderr
    docs = [item for item in yaml.safe_load_all(result.stdout) if item]
    identities = [(d["kind"], d["metadata"]["name"]) for d in docs]
    assert len(identities) == len(set(identities)), "Duplicate resources"
    for doc in docs:
        doc["metadata"].setdefault("namespace", namespace)
    return docs


def one(docs, kind, name):
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


def container_env(deployment):
    return {e["name"]: e for e in deployment["spec"]["template"]["spec"]["containers"][0].get("env", [])}


def verify_tls(ingress, host):
    assert ingress["spec"]["ingressClassName"] == "traefik"
    assert ingress["spec"]["rules"][0]["host"] == host
    assert ingress["spec"]["tls"][0]["hosts"] == [host]
    annotations = ingress["metadata"]["annotations"]
    assert annotations["traefik.ingress.kubernetes.io/router.tls"] == "true"
    assert annotations["traefik.ingress.kubernetes.io/router.entrypoints"] == "websecure"
    assert all(type(value) is str for value in annotations.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helm", default="helm")
    args = parser.parse_args()
    platform_values = ROOT / "charts/platform/values-prod.yaml"
    store_values = ROOT / "charts/store/values-prod.yaml"
    managed_values = ROOT / "charts/store/values-managed.yaml"
    platform = invoke(args.helm, "platform", "platform", [platform_values])
    static = invoke(args.helm, "store", "store", [store_values])
    settings = one(platform, "ConfigMap", "gateway-settings")["data"]
    assert settings["APP_ORIGIN"] == "https://app.urumi.example.com"
    assert settings["COOKIE_SECURE"] == "true"
    assert settings["STORE_BASE_DOMAIN"] == "shops.urumi.example.com"
    assert settings["STORE_URL_SCHEME"] == "https"
    assert not any(d["kind"] == "Deployment" and d["metadata"]["name"] == "store-provisioner" for d in platform)
    assert not any(d["kind"] == "ClusterRoleBinding" for d in platform)
    assert one(platform, "TLSStore", "default")["spec"]["defaultCertificate"]["secretName"] == "urumi-public-tls"
    verify_tls(one(platform, "Ingress", "urumi-app"), "app.urumi.example.com")
    assert one(platform, "Ingress", "urumi-app")["spec"]["tls"][0]["secretName"] == "urumi-public-tls"
    verify_tls(one(static, "Ingress", "wordpress"), "store.urumi.example.com")
    assert "secretName" not in one(static, "Ingress", "wordpress")["spec"]["tls"][0]
    assert container_env(one(static, "Deployment", "woo-mcp"))["PUBLIC_STORE_URL"]["value"] == "https://store.urumi.example.com"
    assert one(platform, "StatefulSet", "postgres")["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"] == "10Gi"
    for docs in (platform, static):
        assert not any(d["kind"] == "Secret" for d in docs), "Values must reference existing Secrets"
        for d in docs:
            if d["kind"] == "Service":
                assert d["spec"].get("type", "ClusterIP") == "ClusterIP"
    print("PASS: independent production files render HTTPS, secure cookies, public names, persistent storage and Secret references")

    enabled = {"storeProvisioner": {"enabled": True,
        "security": {"bindNamespaceRole": True, "bindStorageRole": True},
        "apiServer": {"endpointCidr": "10.20.0.10/32"}}}
    worker_docs = invoke(args.helm, "platform", "platform", [platform_values], enabled)
    worker_cm = one(worker_docs, "ConfigMap", "store-provisioner-platform-settings")["data"]
    profile = yaml.safe_load(worker_cm["store-values.yaml"])
    assert profile["production"]["enabled"] is True
    assert profile["networkPolicy"]["enabled"] is True
    assert profile["provisionerRelease"] == "platform"
    worker_env = container_env(one(worker_docs, "Deployment", "store-provisioner"))
    assert worker_env["PGPASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "gateway-db-credentials"

    scope = StoreClusterScope("platform", "platform")
    stores = []
    with TemporaryDirectory(prefix="production-render-", dir=Path(__file__).parent) as folder:
        environment_file = Path(folder) / "store-values.yaml"
        # The untrusted profile cannot replace per-store identity/credential fields.
        profile.update(storeHost="wrong.urumi.example.com", secretName="wrong-secret",
                       storeAccess={"enabled": False}, wordpressSetup={"enabled": False})
        environment_file.write_text(yaml.safe_dump(profile), encoding="utf-8")
        for index, slug in enumerate(("assamtea", "assam-tea"), 1):
            identity = StoreNamespace(uuid4(), index)
            now = datetime.now(timezone.utc)
            claim = StoreJobClaim(operation_id=uuid4(), store_id=identity.store_id,
                owner_id=index, action="provision", generation=1, attempt=1,
                namespace=identity.name, release_name="store", name="Urumi " + slug,
                slug=slug, url="https://" + slug + "." + settings["STORE_BASE_DOMAIN"],
                deadline_at=now + timedelta(seconds=1200), checked_at=now,
                lease_expires_at=now + timedelta(seconds=60), lease_token=uuid4())
            checked = CheckedStoreJob(claim, "namespace-uid-" + str(index), False,
                time.monotonic() + 50, credentials_secret_uid="secret-uid-" + str(index))
            config = StoreHelmConfig("/tmp/urumi-helm-production" + str(index), identity)
            plan, command, payload = config.installation(checked, scope)
            paths = [command[i+1] for i, arg in enumerate(command) if arg == "--values"]
            assert paths == [STORE_MANAGED_VALUES, STORE_ENVIRONMENT_VALUES, "-"]
            lookup = {STORE_MANAGED_VALUES: managed_values, STORE_ENVIRONMENT_VALUES: environment_file}
            values = json.loads(payload)
            docs = invoke(args.helm, "store", identity.name, [lookup[path] for path in paths[:-1]], values)
            stores.append(docs)
            ingress = one(docs, "Ingress", "wordpress")
            verify_tls(ingress, plan.host)
            assert ingress["metadata"]["annotations"]["traefik.ingress.kubernetes.io/router.middlewares"] == identity.name + "-store-access@kubernetescrd"
            assert "secretName" not in ingress["spec"]["tls"][0]
            quota = one(docs, "ResourceQuota", "managed-store")["spec"]["hard"]
            assert quota["requests.storage"] == "20Gi" and quota["secrets"] == "12"
            limits = one(docs, "LimitRange", "managed-store")["spec"]["limits"]
            assert next(v for v in limits if v["type"] == "PersistentVolumeClaim")["max"]["storage"] == "10Gi"
            assert one(docs, "PersistentVolumeClaim", "wordpress-data")["spec"]["resources"]["requests"]["storage"] == "10Gi"
            mysql_claim = one(docs, "StatefulSet", "mysql")["spec"]["volumeClaimTemplates"][0]["spec"]
            assert mysql_claim["storageClassName"] == "local-path"
            assert mysql_claim["resources"]["requests"]["storage"] == "10Gi"
            env = container_env(one(docs, "Deployment", "wordpress"))
            assert env["URUMI_SETUP_STORE_ID"]["value"] == str(identity.store_id)
            assert env["WORDPRESS_DB_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "store-secrets"
            assert "https://" + plan.host in env["WORDPRESS_CONFIG_EXTRA"]["value"]
            job = one(docs, "Job", "store-bootstrap")
            assert "helm.sh/hook" not in job["metadata"].get("annotations", {})
            assert job["spec"]["activeDeadlineSeconds"] == plan.bootstrap_seconds
            recovered, args_after, body_after = config.recovery(
                replace(checked, claim=replace(claim, attempt=2)), scope,
                StoreRelease(
                    identity, "failed", plan.bootstrap_seconds, managed_bootstrap=True))
            assert "--reuse-values" in args_after and "--values" not in args_after and body_after == b""
            if index == 1:
                malformed = [
                    ({"guardrails": {"quota": {"requests.storage": "19Gi"}}}, "combined storage quota"),
                    ({"guardrails": {"volume": {"max": {"storage": "9Gi"}}}}, "per-volume limits"),
                    ({"db": {"storageClass": "another-class"}}, "local-path"),
                    ({"ingress": {"annotations": {"traefik.ingress.kubernetes.io/router.middlewares": "bypass@kubernetescrd"}}}, "maintenance gate"),
                ]
                for change, reason in malformed:
                    invoke(args.helm, "store", identity.name, [managed_values, environment_file], merge(values, change), reject=reason)
    spec = importlib.util.spec_from_file_location("production_security", ROOT / "scripts/verify-chart-security.py")
    security = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(security)
    # Reuse the network model and workload checks already used by local tests.
    all_docs = worker_docs + static + [d for store in stores for d in store]
    security.check_workloads([d for d in all_docs if d["kind"] not in {
        "Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"}])
    security.check_network(worker_docs + static, "platform", "store")
    policies = [d for d in all_docs if d["kind"] == "NetworkPolicy"]
    worker = security.pod("platform", "store-provisioner")
    worker["labels"]["app.kubernetes.io/instance"] = "platform"
    managed_pods = []
    for docs in stores:
        ns = docs[0]["metadata"]["namespace"]
        labels = {"urumi.ai/managed-by": "store-provisioner", "urumi.ai/store-id": ns.removeprefix("urumi-store-")}
        pods = {name: security.pod(ns, name, namespace_labels=labels) for name in ("wordpress", "mysql", "woo-mcp", "mcp-analytics")}
        managed_pods.append(pods)
        for target in ("woo-mcp", "mcp-analytics"):
            assert security.permitted(policies, security.pod("platform", "gateway"), pods[target], 8000)
            assert security.permitted(policies, worker, pods[target], 8000)
            assert not security.permitted(policies, security.pod("platform", "dashboard"), pods[target], 8000)
        assert security.permitted(policies, worker, pods["wordpress"], 80)
        assert not security.permitted(policies, worker, pods["mysql"], 3306)
    for source, destination in (managed_pods, list(reversed(managed_pods))):
        for caller in ("woo-mcp", "mcp-analytics"):
            assert security.permitted(policies, source[caller], source["wordpress"], 80)
            assert not security.permitted(policies, source[caller], destination["wordpress"], 80)
            assert not security.permitted(policies, source[caller], destination["mysql"], 3306)
    print("PASS: production workloads retain probes/non-root limits; gateway routes work and both stores remain isolated in the policy model")
    print("PASS: actual worker values order renders two scoped HTTPS stores with matching 10Gi PVC limits and 20Gi quotas")
    print("PASS: maintenance gates, identity overrides, durable bootstrap Jobs and saved-values recovery remain enforced")

    platform_bad = [
        ({"gateway": {"cookieSecure": False}}, "secure cookies"),
        ({"ingress": {"scheme": "http"}}, "HTTPS"),
        ({"gateway": {"stores": {"scheme": "http"}}}, "HTTPS"),
        ({"gateway": {"stores": {"baseDomain": "localtest.me"}}}, "public DNS"),
        ({"ingress": {"host": "https://app.urumi.example.com/path"}}, "public DNS"),
        ({"ingress": {"tls": {"enabled": False}}}, "TLS"),
        ({"networkPolicy": {"enabled": False}}, "NetworkPolicies"),
        ({"gateway": {"image": "urumi-gateway:0.2.0"}}, "explicit registry"),
        ({"gateway": {"image": "urumi/gateway:0.2.0"}}, "explicit registry"),
        ({"gateway": {"image": "ghcr.io/urumi-example/gateway:latest"}}, "version tag"),
        ({"gateway": {"trustedProxyCidrs": "0.0.0.0/0"}}, "trusted ingress proxy"),
        ({"ingress": {"annotations": {"wrong-type": True}}}, "string"),
        ({"imagePullSecrets": [{"name": "registry", "password": "not-a-credential"}]}, "not allowed"),
        ({"storeProvisioner": {"profile": {"ingress": {"tls": {"secretName": "missing-in-tenants"}}}}}, "wildcard TLS"),
        ({"storeProvisioner": {"profile": {"guardrails": {"quota": {"requests.storage": "19Gi"}}}}}, "combined storage quota"),
        ({"storeProvisioner": {"enabled": True, "security": {"bindNamespaceRole": True, "bindStorageRole": True}}}, "real exact /32"),
    ]
    for changes, reason in platform_bad:
        invoke(args.helm, "platform", "platform", [platform_values], changes, reject=reason)
    for changes, reason in [
        ({"storeHost": "localtest.me"}, "public store DNS"),
        ({"storeHost": "bad.urumi.example.com/path"}, "public store DNS"),
        ({"storeScheme": "http"}, "HTTPS"),
        ({"ingress": {"tls": {"enabled": False}}}, "TLS"),
        ({"networkPolicy": {"enabled": False}}, "NetworkPolicies"),
        ({"wooMcp": {"image": "woo-mcp:0.2.0"}}, "explicit registry"),
        ({"ingress": {"annotations": {"traefik.ingress.kubernetes.io/router.tls": "false"}}}, "websecure"),
    ]:
        invoke(args.helm, "store", "store", [store_values], changes, reject=reason)
    print("PASS: unsafe public names, cookies/TLS, image references, quotas, annotation types and placeholder API endpoints fail before deployment")

    # Changing namespaces/controller labels is a values-only operation as well.
    notes = "http://mcp-notes.urumi-system.svc.cluster.local:8000/mcp"
    alt = merge(enabled, {"gateway": {"upstreams": {"notes": notes}, "upstreamSecurity": {"internalUrls": [
        "http://woo-mcp.store.svc.cluster.local:8000/mcp",
        "http://mcp-analytics.store.svc.cluster.local:8000/mcp", notes]}},
        "networkPolicy": {"ingressNamespace": "edge", "dnsNamespace": "dns-system"},
        "imagePullSecrets": [{"name": "platform-registry"}]})
    alternative = invoke(args.helm, "platform", "urumi-system", [platform_values], alt)
    assert one(alternative, "ConfigMap", "gateway-settings")["data"]["NOTES_MCP_URL"] == notes
    for d in alternative:
        if d["kind"] == "ServiceAccount":
            assert d["imagePullSecrets"] == [{"name": "platform-registry"}]
    alternate_profile = yaml.safe_load(one(alternative, "ConfigMap", "store-provisioner-platform-settings")["data"]["store-values.yaml"])
    assert alternate_profile["networkPolicy"]["ingressNamespace"] == "edge"
    assert alternate_profile["networkPolicy"]["dnsNamespace"] == "dns-system"
    print("PASS: alternative platform/controller namespaces and existing registry Secret references need values changes only")

    local = invoke(args.helm, "platform", "platform", [ROOT / "charts/platform/values-local.yaml", ROOT / "charts/platform/values-apps-local.yaml"])
    local_settings = one(local, "ConfigMap", "gateway-settings")["data"]
    assert local_settings["APP_ORIGIN"] == "http://app.localtest.me" and local_settings["COOKIE_SECURE"] == "false"
    assert local_settings["STORE_BASE_DOMAIN"] == "localtest.me" and local_settings["STORE_URL_SCHEME"] == "http"
    assert "tls" not in one(local, "Ingress", "urumi-app")["spec"]
    local_store = invoke(args.helm, "store", "store", [ROOT / "charts/store/values-local.yaml"])
    assert "tls" not in one(local_store, "Ingress", "wordpress")["spec"]
    assert "ingressClassName" not in one(local_store, "Ingress", "wordpress")["spec"]
    assert not any(d["kind"] == "TLSStore" for d in local)
    print("PASS: local HTTP defaults and the disabled-worker installation still render")
    # Helm --reuse-values uses the old chart defaults. Render the new templates
    # with those old defaults rather than assuming new keys will be merged in.
    with TemporaryDirectory(prefix="production-legacy-", dir=Path(__file__).parent) as folder:
        for chart, namespace, files in (
            ("platform", "platform", [ROOT / "charts/platform/values-apps-local.yaml"]),
            ("store", "store", [ROOT / "charts/store/values-local.yaml"]),
        ):
            old_chart = Path(folder) / chart
            shutil.copytree(ROOT / "charts" / chart, old_chart)
            defaults = yaml.safe_load((old_chart / "values.yaml").read_text())
            for key in ("production", "imagePullSecrets", "ingress"):
                defaults.pop(key, None)
            if chart == "platform":
                defaults["gateway"].pop("stores", None)
            (old_chart / "values.yaml").write_text(yaml.safe_dump(defaults), encoding="utf-8")
            old_docs = invoke(args.helm, chart, namespace, files, chart_path=old_chart, upgrade=True)
            current_docs = local if chart == "platform" else local_store
            # The settings checksum includes the raw values map; omitted old
            # defaults may change that hash even when the rendered env is equal.
            current_docs = deepcopy(current_docs)
            for collection in (old_docs, current_docs):
                for doc in collection:
                    if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "gateway":
                        doc["spec"]["template"]["metadata"]["annotations"].pop("checksum/settings", None)
            assert old_docs == current_docs, "Old Helm defaults changed existing resources"
            if chart == "store":
                legacy_values = merge(values, {"storeScheme": "http", "storeHost": "fixture.localtest.me"})
                old_managed = invoke(args.helm, "store", identity.name, [managed_values], legacy_values,
                    chart_path=old_chart, upgrade=True)
                current_managed = invoke(args.helm, "store", identity.name, [managed_values], legacy_values, upgrade=True)
                assert old_managed == current_managed, "Old managed Jobs changed during recovery"
    print("PASS: saved releases without the new defaults remain compatible with recovery templates")
    print("Production Helm verification passed. Temporary render files removed; no cluster, database, network or credential access.")


if __name__ == "__main__":
    main()
