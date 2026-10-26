"""Check real WordPress startup with a fresh ephemeral volume, without services.

Run from apps/gateway:
    uv run --with pyyaml python tests/verify_store_wordpress_filesystem.py

The configured WordPress image must already be in the local Docker image cache.
Containers have no network. All writes stay in disposable tmpfs mounts.
"""
from pathlib import Path
import subprocess
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parents[3]


def rendered(enabled):
    result = subprocess.run([
        "helm", "template", "store", str(ROOT / "charts/store"),
        "-n", "urumi-store-" + "11111111-1111-4111-8111-111111111111".replace("-", ""),
        "-f", str(ROOT / "charts/store/values-local.yaml"),
        "-f", str(ROOT / "charts/store/values-managed.yaml"),
        "--set", "storeAccess.enabled=true",
        "--set-string", "storeAccess.storeId=11111111-1111-4111-8111-111111111111",
        "--set", "wordpressSetup.enabled=" + str(enabled).lower(),
    ], text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, "Helm rendering failed:\n" + result.stderr
    documents = [item for item in yaml.safe_load_all(result.stdout) if item]
    deployment = next(item for item in documents
        if item["kind"] == "Deployment" and item["metadata"]["name"] == "wordpress")
    return deployment["spec"]["template"]["spec"]


SCRIPT = r"""
set -eu
if [ -e wp-content ]; then
  printf 'PARENT_UID=%s\n' "$(stat -c %u wp-content)"
fi
cat > /tmp/apache2-urumi-startup-check <<'CHECK'
#!/bin/sh
set -eu
test "$(id -u)" = 33
test -s /var/www/html/wp-config.php
test -w /var/www/html/wp-content
test -w /var/www/html/wp-content/plugins
test -w /var/www/html/wp-content/themes
test -r /opt/urumi/mu-plugins/urumi-setup.php
test ! -w /opt/urumi/mu-plugins/urumi-setup.php
php <<'PHP'
<?php
define('ABSPATH', '/var/www/html/');
eval(getenv('WORDPRESS_CONFIG_EXTRA'));
require ABSPATH . 'wp-includes/load.php';
$plugins = wp_get_mu_plugins();
if ($plugins !== array('/opt/urumi/mu-plugins/urumi-setup.php')) {
    fwrite(STDERR, "Unexpected MU-plugin discovery.\n");
    exit(1);
}
echo "WordPress discovers the external read-only setup plugin.\n";
PHP
printf 'URUMI_STARTUP_OK\n'
CHECK
chmod 0700 /tmp/apache2-urumi-startup-check
export PATH="/tmp:$PATH"
exec /usr/local/bin/docker-entrypoint.sh apache2-urumi-startup-check
"""


def startup(image, extra, destination):
    name = "urumi-wp-filesystem-" + uuid4().hex
    source = ROOT / "charts/store/files/wordpress-setup.php"
    command = [
        "docker", "run", "--rm", "--pull=never", "--name", name, "-i",
        "--network", "none", "--read-only", "--user", "33:33",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--workdir", "/var/www/html",
        "--tmpfs", "/var/www/html:rw,mode=0770,uid=33,gid=33,size=256m",
        "--tmpfs", "/tmp:rw,exec,nosuid,nodev,mode=1770,uid=33,gid=33,size=16m",
        "--mount", f"type=bind,source={source},target={destination},readonly",
        "--env", "WORDPRESS_CONFIG_EXTRA=" + extra,
        "--entrypoint", "/bin/sh", image, "-s",
    ]
    try:
        return subprocess.run(command, input=SCRIPT, text=True,
            capture_output=True, timeout=90)
    finally:
        subprocess.run(["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)


def check():
    pod = rendered(True)
    container = next(item for item in pod["containers"] if item["name"] == "wordpress")
    assert pod["securityContext"]["runAsUser"] == 33
    assert pod["securityContext"]["runAsNonRoot"] is True
    setup = [item for item in container["volumeMounts"] if item["name"] == "wordpress-setup"]
    assert setup == [{"name": "wordpress-setup", "mountPath": "/opt/urumi/mu-plugins", "readOnly": True}]
    assert not any(item["mountPath"].startswith("/var/www/html/") for item in container["volumeMounts"])
    env = {item["name"]: item for item in container["env"]}
    extra = env["WORDPRESS_CONFIG_EXTRA"]["value"]
    assert extra.count("define('WPMU_PLUGIN_DIR', '/opt/urumi/mu-plugins');") == 1
    disabled = rendered(False)
    old_container = next(item for item in disabled["containers"] if item["name"] == "wordpress")
    assert not any(item["name"] == "wordpress-setup" for item in old_container["volumeMounts"])
    old_env = {item["name"]: item for item in old_container["env"]}
    assert "WPMU_PLUGIN_DIR" not in old_env["WORDPRESS_CONFIG_EXTRA"]["value"]
    assert "URUMI_SETUP_STORE_ID" not in old_env
    image = container["image"]
    available = subprocess.run(["docker", "image", "inspect", image],
        text=True, capture_output=True, timeout=20)
    assert available.returncode == 0, "First cache the image with: docker pull " + image
    print("Rendered enabled/disabled charts keep the setup mount outside the data volume.")
    broken = startup(image, extra,
        "/var/www/html/wp-content/mu-plugins/urumi-setup.php")
    report = broken.stdout + broken.stderr
    assert broken.returncode != 0, "The old nested mount unexpectedly passed."
    assert "PARENT_UID=0" in report and "Permission denied" in report, report[-6000:]
    assert "URUMI_STARTUP_OK" not in report
    print("The old nested mount reproduces root-owned content directories and startup failure.")
    working = startup(image, extra, "/opt/urumi/mu-plugins/urumi-setup.php")
    report = working.stdout + working.stderr
    assert working.returncode == 0, report[-6000:]
    assert "URUMI_STARTUP_OK" in report
    assert "WordPress discovers the external read-only setup plugin." in report
    print("Fresh non-root startup creates writable plugin/theme directories and discovers the protected setup plugin.")
    print("WordPress filesystem checks passed. Disposable containers were removed; no database, cluster or network calls were made.")


if __name__ == "__main__":
    check()
