"""Render two named managed stores; the original demo retains its default title."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.store_cluster_scope import StoreClusterScope
from app.store_credentials import StoreCredentials
from app.store_helm_plan import plan_store_installation
from app.store_job_guard import CheckedStoreJob
from app.store_jobs import StoreJobClaim
from app.store_namespace import StoreNamespace

root = Path(__file__).resolve().parents[3]
scope = StoreClusterScope("platform", "platform")
for index, (slug, name) in enumerate((("assamtea", "urumi"), ("assam-tea", "Assam Guwahati Tea"))):
    identity = StoreNamespace(uuid4(), index + 1)
    now = datetime.now(timezone.utc)
    claim = StoreJobClaim(operation_id=uuid4(), store_id=identity.store_id,
        owner_id=identity.owner_id, action="provision", generation=1, attempt=1,
        namespace=identity.name, release_name="store", name=name, slug=slug,
        url=f"http://{slug}.localtest.me", deadline_at=now + timedelta(seconds=1200),
        checked_at=now, lease_expires_at=now + timedelta(seconds=60), lease_token=uuid4())
    checked = CheckedStoreJob(claim, "namespace-uid", False, time.monotonic() + 50,
        credentials_secret_uid="secret-uid")
    plan = plan_store_installation(checked, scope)
    assert plan.values()["storeName"] == name and plan.values()["wordpressSetup"] == {"enabled": True}
    credentials = StoreCredentials.generate(identity, admin_email="owner@example.test")
    assert credentials._values["WP_ADMIN_USER"] == "admin"
    legacy = dict(credentials._values)
    legacy["WP_ADMIN_USER"] = "admin_" + identity.store_id.hex
    assert StoreCredentials(identity, legacy)._values["WP_ADMIN_USER"] == legacy["WP_ADMIN_USER"]
    command = ["helm", "template", "store", str(root / "charts/store"), "-n", identity.name,
        "-f", str(root / "charts/store/values-local.yaml"),
        "-f", str(root / "charts/store/values-managed.yaml"), "-f", "-"]
    result = subprocess.run(command, input=json.dumps(plan.values()), text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    documents = [item for item in yaml.safe_load_all(result.stdout) if item]
    job = next(item for item in documents if item["kind"] == "Job")
    env = {item["name"]: item for item in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["STORE_NAME"]["value"] == name
    wordpress = next(item for item in documents if item["kind"] == "Deployment" and item["metadata"]["name"] == "wordpress")
    container = wordpress["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item for item in container["env"]}
    assert env["URUMI_STORE_NAME"]["value"] == name
    assert env["URUMI_SETUP_STORE_ID"]["value"] == str(identity.store_id)
    assert env["URUMI_SETUP_ADMIN_USER"]["valueFrom"]["secretKeyRef"]["key"] == "WP_ADMIN_USER"
    assert {"name": "wordpress-setup", "mountPath": "/opt/urumi/mu-plugins", "readOnly": True} in container["volumeMounts"]
    assert not any(mount["mountPath"].startswith("/var/www/html/") for mount in container["volumeMounts"])
    extra = env["WORDPRESS_CONFIG_EXTRA"]["value"]
    assert extra.count("define('WPMU_PLUGIN_DIR', '/opt/urumi/mu-plugins');") == 1
    assert wordpress["spec"]["template"]["spec"]["securityContext"]["runAsUser"] == 33
    cm = next(item for item in documents if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "store-wordpress-setup")
    assert cm["data"]["urumi-setup.php"].startswith("<?php")
    assert any(item["kind"] == "NetworkPolicy" and item["metadata"]["name"] == "wordpress-to-setup-gateway" for item in documents)
    for value in ("", "x" * 101, "bad\nname"):
        try:
            plan_store_installation(replace(checked, claim=replace(claim, name=value)), scope)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid store name accepted")
print("Two store names reach WordPress and bootstrap; both website address formats remain valid.")
print("New usernames are admin; legacy usernames and scoped setup mounts remain compatible.")
print("Branding chart checks passed. No database or cluster calls were made.")
