"""Exercise existing-store setup with fake kubectl and gateway preflight."""
from copy import deepcopy
import importlib.util
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root / "apps/gateway"))
from app.store_cluster_scope import StoreClusterScope
from app.store_namespace import StoreNamespace


def check():
    path = root / "scripts/install-store-wordpress-setup.py"
    spec = importlib.util.spec_from_file_location("wordpress_setup_install_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    identity = StoreNamespace(uuid4(), 7)
    scope = StoreClusterScope("platform", "platform")

    class Fixture:
        def __init__(self):
            self.info = dict(id=str(identity.store_id), name="urumi", owner_id=7,
                namespace=identity.name, namespace_uid="namespace-uid", generation=1,
                host="assamtea.localtest.me", guarded=True)
            self.namespace = scope.namespace_manifest(identity)
            self.namespace["metadata"].update(uid="namespace-uid", resourceVersion="17")
            self.namespace["status"] = {"phase": "Active"}
            self.deployment = {"apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"uid": "deployment-uid", "resourceVersion": "3",
                    "annotations": {"meta.helm.sh/release-name": "store"}},
                "spec": {"template": {"metadata": {"labels": {"app": "wordpress"}},
                    "spec": {"containers": [{"name": "wordpress", "image": "fixture-wordpress",
                        "env": [{"name": "UNCHANGED", "value": "fixture"}],
                        "volumeMounts": [{"name": "wordpress-data", "mountPath": "/var/www/html"}]}],
                        "volumes": [{"name": "wordpress-data",
                            "persistentVolumeClaim": {"claimName": "wordpress-data"}}]}}}}
            self.cm = None
            self.policy = None
            self.writes = []
            self.change_before_patch = False

        def remote(self, platform, payload, code):
            assert platform == "platform" and payload == {"id": str(identity.store_id)}
            assert code == module.READ and "name=store.name" in code
            return deepcopy(self.info)

        def run(self, args, body=None, missing=False):
            if args[:2] == ["get", "namespace"]:
                return json.dumps(self.namespace)
            if args[:3] == ["-n", identity.name, "get"]:
                item = {"deployment": self.deployment, "configmap": self.cm,
                    "networkpolicy": self.policy}[args[3]]
                return json.dumps(item) if item else None
            if args[0] == "create":
                document = json.loads(body)
                assert document["metadata"]["namespace"] == identity.name
                if "--dry-run=server" not in args:
                    self.writes.append(document["kind"])
                    if document["kind"] == "ConfigMap":
                        self.cm = document
                        self.cm["metadata"].update(uid="configmap-uid", resourceVersion="5")
                    else: self.policy = document
                return "{}"
            if args[:4] == ["-n", identity.name, "patch", "configmap"]:
                changes = json.loads(args[args.index("-p") + 1])
                assert changes[0] == {"op": "test", "path": "/metadata/uid", "value": "configmap-uid"}
                assert changes[1] == {"op": "test", "path": "/metadata/resourceVersion", "value": self.cm["metadata"]["resourceVersion"]}
                assert changes[2]["op"] == "replace" and changes[2]["path"] == "/data"
                if "--dry-run=server" not in args:
                    self.writes.append("ConfigMapPatch")
                    self.cm["data"] = changes[2]["value"]
                    self.cm["metadata"]["resourceVersion"] = "6"
                return "{}"
            if args[:4] == ["-n", identity.name, "patch", "deployment"]:
                changes = json.loads(args[args.index("-p") + 1])
                assert changes[0] == {"op": "test", "path": "/metadata/uid", "value": "deployment-uid"}
                assert changes[1]["path"] == "/metadata/resourceVersion"
                assert changes[2]["path"] == "/spec/template"
                if "--dry-run=server" not in args:
                    if self.change_before_patch:
                        raise SystemExit("Simulated resource version conflict")
                    self.writes.append("Deployment")
                    self.deployment["spec"]["template"] = changes[2]["value"]
                    self.deployment["metadata"]["resourceVersion"] = "4"
                return "{}"
            if args[:3] == ["-n", identity.name, "rollout"]:
                return "{}"
            raise AssertionError("Unexpected operation " + repr(args))

        def invoke(self, apply=False):
            argv = [str(path), str(identity.store_id)] + (["--apply"] if apply else [])
            with patch.object(sys, "argv", argv), patch.object(module, "run", self.run), patch.object(module, "remote", self.remote):
                module.main()

    fixture = Fixture()
    original = deepcopy(fixture.deployment)
    fixture.invoke()
    assert fixture.writes == [] and fixture.cm is None
    fixture.invoke(True)
    assert fixture.writes == ["ConfigMap", "NetworkPolicy", "Deployment"]
    assert fixture.deployment["spec"]["template"]["spec"]["containers"][0]["image"] == "fixture-wordpress"
    assert fixture.deployment["spec"]["template"]["spec"]["containers"][0]["env"][0] == {"name": "UNCHANGED", "value": "fixture"}
    current = fixture.deployment["spec"]["template"]["spec"]["containers"][0]
    external_mount = {"name": "wordpress-setup", "mountPath": "/opt/urumi/mu-plugins", "readOnly": True}
    assert external_mount in current["volumeMounts"]
    assert not any(item["mountPath"].startswith("/var/www/html/") for item in current["volumeMounts"])
    extra = next(item["value"] for item in current["env"] if item["name"] == "WORDPRESS_CONFIG_EXTRA")
    assert extra.count("define('WPMU_PLUGIN_DIR', '/opt/urumi/mu-plugins');") == 1
    fixture.writes.clear()
    fixture.invoke(True)
    assert fixture.writes == []
    old_plugin = (root / "charts/store/files/wordpress-setup.php").read_text(encoding="utf-8")
    old_plugin = old_plugin.replace("/^.{6,16}$/usD", "/^.{12,128}$/usD")
    old_plugin = old_plugin.replace("Choose a password of 6–16 characters.", "Choose a password of 12–128 characters.")
    old_plugin = old_plugin.replace('minlength="6" maxlength="16"', 'minlength="12" maxlength="128"')
    fixture.cm["data"]["urumi-setup.php"] = old_plugin
    fixture.deployment["spec"]["template"]["metadata"]["annotations"]["checksum/wordpress-setup"] = hashlib.sha256(old_plugin.encode()).hexdigest()
    fixture.invoke()
    assert fixture.writes == []
    fixture.invoke(True)
    assert fixture.writes == ["ConfigMapPatch", "Deployment"]
    fixture.writes.clear()
    fixture.invoke(True)
    assert fixture.writes == []
    for case in ("namespace", "owner", "gate", "deployment", "plugin", "mu_directory", "mount", "extra_reference"):
        f = Fixture()
        if case == "namespace": f.namespace["metadata"]["uid"] = "replaced"
        if case == "owner": f.namespace["metadata"]["labels"]["urumi.ai/owner-id"] = "8"
        if case == "gate": f.info["guarded"] = False
        if case == "deployment": f.deployment["spec"]["template"]["spec"]["containers"][0]["name"] = "other"
        if case == "plugin": f.cm = {"metadata": {"annotations": {"urumi.ai/store-id": str(identity.store_id)}}, "data": {"urumi-setup.php": "other"}}
        if case == "mu_directory":
            f.deployment["spec"]["template"]["spec"]["containers"][0]["env"].append(
                {"name": "WORDPRESS_CONFIG_EXTRA", "value": "define('WPMU_PLUGIN_DIR', '/other');"})
        if case == "mount":
            f.deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"].append(
                {"name": "wordpress-setup", "mountPath": "/other", "readOnly": True})
        if case == "extra_reference":
            f.deployment["spec"]["template"]["spec"]["containers"][0]["env"].append(
                {"name": "WORDPRESS_CONFIG_EXTRA", "valueFrom": {"configMapKeyRef": {"name": "other", "key": "extra"}}})
        try:
            f.invoke(True)
        except (AssertionError, ValueError):
            pass
        else:
            raise AssertionError("Unsafe installation accepted: " + case)
        assert not f.writes
    # A previously installed legacy mount keeps the default MU-plugin directory.
    legacy = Fixture()
    legacy.deployment = deepcopy(fixture.deployment)
    legacy.cm, legacy.policy = deepcopy(fixture.cm), deepcopy(fixture.policy)
    container = legacy.deployment["spec"]["template"]["spec"]["containers"][0]
    container["volumeMounts"] = [
        item if item["name"] != "wordpress-setup" else {
            "name": "wordpress-setup",
            "mountPath": "/var/www/html/wp-content/mu-plugins/urumi-setup.php",
            "subPath": "urumi-setup.php", "readOnly": True,
        } for item in container["volumeMounts"]
    ]
    container["env"] = [item for item in container["env"] if item["name"] != "WORDPRESS_CONFIG_EXTRA"]
    legacy.invoke(True)
    assert legacy.writes == []
    # Existing WP extra configuration survives external-plugin setup and replay.
    configured = Fixture()
    configured.deployment["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "WORDPRESS_CONFIG_EXTRA", "value": "define('FS_METHOD', 'direct');\n"})
    configured.invoke(True)
    environment = configured.deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    extra = next(item["value"] for item in environment if item["name"] == "WORDPRESS_CONFIG_EXTRA")
    assert extra.startswith("define('FS_METHOD', 'direct');\n")
    assert extra.count("WPMU_PLUGIN_DIR") == 1
    configured.writes.clear()
    configured.invoke(True)
    assert configured.writes == []
    print("External setup mounts preserve writable content paths; legacy mounts and extra configuration remain compatible.")
    f = Fixture()
    f.change_before_patch = True
    try:
        f.invoke(True)
    except SystemExit:
        pass
    else:
        raise AssertionError("Concurrent deployment change was accepted")
    assert f.writes == ["ConfigMap", "NetworkPolicy"]
    assert f.deployment == original
    f.change_before_patch = False
    f.writes.clear()
    f.invoke(True)
    assert f.writes == ["Deployment"]
    print("Preview is read-only; installation is scoped, fenced and repeatable; accepted partial progress recovers.")
    print("Replaced namespaces, wrong owners and changed plugin/workload definitions stop safely.")
    print("WordPress installer checks passed. All Kubernetes and gateway operations were fixtures.")


if __name__ == "__main__":
    check()
