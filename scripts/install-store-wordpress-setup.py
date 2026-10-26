"""Preview/install WordPress password setup on one existing Ready managed store.

Never upgrades the store's Helm release, reads credentials, or changes passwords.
The store title is synchronized when its owner later saves their chosen password.
"""
import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/gateway"))
from app.store_cluster_scope import StoreClusterScope
from app.store_namespace import StoreNamespace

spec = importlib.util.spec_from_file_location("access_setup", ROOT / "scripts/install-store-access.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
run, remote = helpers.run, helpers.remote
READ = helpers.READ.replace(
    'return dict(id=str(store.id), owner_id=store.owner_id, namespace=store.namespace,',
    'return dict(id=str(store.id), owner_id=store.owner_id, namespace=store.namespace, name=store.name,',
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store_id", type=UUID)
    parser.add_argument("--platform", default="platform")
    parser.add_argument("--release", default="platform")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    scope = StoreClusterScope(args.platform, args.release)
    before = remote(args.platform, {"id": str(args.store_id)}, READ)
    identity = StoreNamespace(args.store_id, before["owner_id"])
    namespace = identity.name
    assert before["namespace"] == namespace and before["guarded"], "A gated Ready store is required."
    assert isinstance(before["name"], str) and before["name"].isprintable() and 1 <= len(before["name"]) <= 100

    def owned():
        current = remote(args.platform, {"id": str(args.store_id)}, READ)
        assert current == before, "Store changed; preview again."
        document = json.loads(run(["get", "namespace", namespace, "-o", "json"]))
        scope.check_namespace(identity, document, expected_uid=before["namespace_uid"])

    def read(kind, name):
        raw = run(["-n", namespace, "get", kind, name, "--ignore-not-found", "-o", "json"], missing=True)
        return json.loads(raw) if raw else None

    owned()
    deployment = read("deployment", "wordpress")
    assert deployment and not deployment["metadata"].get("deletionTimestamp")
    assert deployment["metadata"].get("annotations", {}).get("meta.helm.sh/release-name") == "store"
    pod = deployment["spec"]["template"]["spec"]
    containers = pod["containers"]
    assert len(containers) == 1 and containers[0]["name"] == "wordpress", "Unexpected WordPress workload."
    container = containers[0]
    assert any(m.get("name") == "wordpress-data" and m.get("mountPath") == "/var/www/html"
        for m in container.get("volumeMounts", [])), "Store data mount changed."
    source = (ROOT / "charts/store/files/wordpress-setup.php").read_text(encoding="utf-8")
    assert source.startswith("<?php") and "login_form_urumi_setup" in source
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "store-wordpress-setup", "namespace": namespace,
            "annotations": {"urumi.ai/store-id": str(args.store_id)}},
        "data": {"urumi-setup.php": source}}
    existing = read("configmap", "store-wordpress-setup")
    if existing:
        assert not existing["metadata"].get("deletionTimestamp")
        assert existing["metadata"].get("annotations", {}).get("urumi.ai/store-id") == str(args.store_id)
        data = existing.get("data")
        previous = (isinstance(data, dict) and set(data) == {"urumi-setup.php"}
            and isinstance(data["urumi-setup.php"], str)
            and hashlib.sha256(data["urumi-setup.php"].encode("utf-8")).hexdigest()
                == "24784ab858912098620536adebe97c81e2d4e79139270a5993555635479da1c0")
        assert data == cm["data"] or previous, "An existing setup plugin differs; review it first."
    cm_patch = None
    if existing and existing.get("data") != cm["data"]:
        cm_patch = ["-n", namespace, "patch", "configmap", "store-wordpress-setup",
            "--type=json", "-p", json.dumps([
                {"op": "test", "path": "/metadata/uid", "value": existing["metadata"]["uid"]},
                {"op": "test", "path": "/metadata/resourceVersion", "value": existing["metadata"]["resourceVersion"]},
                {"op": "replace", "path": "/data", "value": cm["data"]},
            ])]
    policy = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
        "metadata": {"name": "wordpress-to-setup-gateway", "namespace": namespace},
        "spec": {"podSelector": {"matchLabels": {"app": "wordpress"}},
            "policyTypes": ["Egress"], "egress": [{"to": [{
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": args.platform}},
                "podSelector": {"matchLabels": {"app": "gateway"}}}],
                "ports": [{"protocol": "TCP", "port": 8000}]}]}}
    old_policy = read("networkpolicy", "wordpress-to-setup-gateway")
    if old_policy:
        assert old_policy["spec"] == policy["spec"] and not old_policy["metadata"].get("deletionTimestamp")
    template = deepcopy(deployment["spec"]["template"])
    wanted_env = {
        "URUMI_SETUP_STORE_ID": {"value": str(args.store_id)},
        "URUMI_SETUP_ENDPOINT": {"value": "http://gateway." + args.platform + ".svc.cluster.local:8000/api/store-wordpress-setup/redeem"},
        "URUMI_SETUP_ADMIN_USER": {"valueFrom": {"secretKeyRef": {"name": "store-secrets", "key": "WP_ADMIN_USER"}}},
        "URUMI_STORE_NAME": {"value": before["name"]},
    }
    current = template["spec"]["containers"][0]
    env = current.setdefault("env", [])
    for key, definition in wanted_env.items():
        matches = [item for item in env if item["name"] == key]
        assert len(matches) <= 1
        desired = {"name": key, **definition}
        if matches:
            assert matches[0] == desired, "Existing setup configuration differs."
        else:
            env.append(desired)
    legacy_mount = {"name": "wordpress-setup", "mountPath": "/var/www/html/wp-content/mu-plugins/urumi-setup.php",
        "subPath": "urumi-setup.php", "readOnly": True}
    external_mount = {"name": "wordpress-setup", "mountPath": "/opt/urumi/mu-plugins", "readOnly": True}
    installed_mounts = [item for item in current.get("volumeMounts", []) if item["name"] == "wordpress-setup"]
    assert len(installed_mounts) <= 1
    assert not installed_mounts or installed_mounts[0] in (legacy_mount, external_mount), "Existing setup mount differs."
    # Already working legacy stores retain their existing MU-plugin directory.
    mount = installed_mounts[0] if installed_mounts else external_mount
    if mount == external_mount:
        define = "define('WPMU_PLUGIN_DIR', '/opt/urumi/mu-plugins');"
        extras = [item for item in env if item["name"] == "WORDPRESS_CONFIG_EXTRA"]
        assert len(extras) <= 1
        if extras:
            extra = extras[0]
            assert set(extra) == {"name", "value"} and isinstance(extra["value"], str), "WordPress extra configuration differs."
            if "WPMU_PLUGIN_DIR" in extra["value"]:
                assert define in extra["value"].splitlines() and extra["value"].count("WPMU_PLUGIN_DIR") == 1, "Existing MU-plugin directory differs."
            else:
                extra["value"] = extra["value"].rstrip() + "\n" + define + "\n"
        else:
            env.append({"name": "WORDPRESS_CONFIG_EXTRA", "value": define + "\n"})
    volumes = template["spec"].setdefault("volumes", [])
    volume = {"name": "wordpress-setup", "configMap": {"name": "store-wordpress-setup", "defaultMode": 292}}
    for items, expected in ((current.setdefault("volumeMounts", []), mount), (volumes, volume)):
        matches = [item for item in items if item["name"] == "wordpress-setup"]
        if matches:
            assert matches == [expected], "Existing setup mount differs."
        else:
            if items is current["volumeMounts"]:
                assert not any(item.get("mountPath") == mount["mountPath"] for item in items)
            items.append(expected)
    template["metadata"].setdefault("annotations", {})["checksum/wordpress-setup"] = hashlib.sha256(source.encode()).hexdigest()
    operations = [
        {"op": "test", "path": "/metadata/uid", "value": deployment["metadata"]["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": deployment["metadata"]["resourceVersion"]},
        {"op": "replace", "path": "/spec/template", "value": template},
    ]
    patch = ["-n", namespace, "patch", "deployment", "wordpress", "--type=json", "-p", json.dumps(operations)]
    for old, document in ((existing, cm), (old_policy, policy)):
        if old is None:
            run(["create", "--dry-run=server", "-f", "-"], json.dumps(document))
    if template != deployment["spec"]["template"]:
        run([*patch, "--dry-run=server"])
    if cm_patch is not None:
        run([*cm_patch, "--dry-run=server"])
    if not args.apply:
        print("Preview passed for " + namespace + ". No changes made. Run with --apply to install.")
        return
    for old, document in ((existing, cm), (old_policy, policy)):
        if old is None:
            owned()
            run(["create", "-f", "-"], json.dumps(document))
    if cm_patch is not None:
        owned()
        run(cm_patch)
    owned()
    if template != deployment["spec"]["template"]:
        run(patch)
    run(["-n", namespace, "rollout", "status", "deployment/wordpress", "--timeout=180s"])
    print("WordPress setup installed. Passwords are unchanged. The owner can now use Set WordPress password.")


if __name__ == "__main__":
    main()
