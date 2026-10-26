import os
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[3]
source = (root / "charts/store/files/bootstrap.sh").read_text(encoding="utf-8")
settings = (root / "charts/store/files/seed.sh").read_text(encoding="utf-8")
wait = "until [ -f /var/www/html/wp-config.php ]; do sleep 3; done"
assert source.count(wait) == 1
assert source.count(". /scripts/seed.sh") == 1
assert source.count("$WP eval-file /scripts/keys.php") == 1
assert source.count("$WP eval-file /scripts/media.php") == 1

# Change only the in-memory test copy. All WP/PHP commands below are mocked.
script = source.replace(wait, ":", 1)
script = script.replace(". /scripts/seed.sh", "seed_fixture", 1)
prefix = r'''
wp() { :; }
php() {
    if [ "$1" = "-r" ]; then return 0; fi
    shift 4
    printf 'TRACE:%s\n' "$*" >&2
    if [ -n "$FAIL" ]; then
        case "$*" in "$FAIL"*) return 7 ;; esac
    fi
    case "$*" in
        "core is-installed")
            [ "$CORE_INSTALLED" = 1 ]; return $? ;;
        "plugin is-installed woocommerce")
            [ "$PLUGIN_PRESENT" = 1 ]; return $? ;;
        "plugin get woocommerce --field=version")
            printf '%s\n' "$INSTALLED_VERSION" ;;
        "wc shipping_zone_method list"*)
            if [ "$EXISTING" = 1 ]; then printf '11\n'; fi ;;
    esac
    return 0
}
seed_fixture() {
    printf 'TRACE:seed\n' >&2
    [ "$FAIL" != seed ] || return 1
__STORE_SETTINGS_SOURCE__
}
'''.replace("__STORE_SETTINGS_SOURCE__", settings)
shell = "/bin/sh"
subprocess.run(
    [shell, "-n"], input=source, text=True,
    capture_output=True, check=True, timeout=10,
)

def run(failure="", existing=False, *, plugin_present=False,
        version="11.2.1", pinned_version="11.2.1", core_installed=True, store_name="Assam Guwahati Tea"):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "WP_ADMIN_USER": "fixture_admin",
        "WP_ADMIN_PASSWORD": "fixture-only-not-a-real-password",
        "WP_ADMIN_EMAIL": "owner@example.test",
        "STORE_URL": "http://fixture.localtest.me",
        "STORE_NAME": store_name,
        "CORE_INSTALLED": "1" if core_installed else "0",
        "WOOCOMMERCE_VERSION": pinned_version,
        "PLUGIN_PRESENT": "1" if plugin_present else "0",
        "INSTALLED_VERSION": version,
        "FAIL": failure,
        "EXISTING": "1" if existing else "0",
    }
    result = subprocess.run(
        [shell, "-s"], input=prefix + script, env=env,
        text=True, capture_output=True, timeout=10,
    )
    calls = [
        line.removeprefix("TRACE:")
        for line in result.stderr.splitlines() if line.startswith("TRACE:")
    ]
    return result, calls

def check_setup_order(calls):
    steps = ["eval-file /scripts/keys.php", "eval-file /scripts/media.php", "seed"]
    assert all(calls.count(step) == 1 for step in steps)
    positions = [calls.index(step) for step in steps]
    assert positions == sorted(positions), "Incorrect bootstrap order"

for failure in (
    "plugin install woocommerce",
    "plugin get woocommerce",
    "plugin activate woocommerce",
    "wc tool run install_pages",
    "wc shipping_zone_method list",
    "wc shipping_zone_method create",
    "option update blogname",
    "option update woocommerce_currency",
    "option update woocommerce_enable_guest_checkout",
    "eval-file /scripts/keys.php",
    "eval-file /scripts/media.php",
    "seed",
):
    result, calls = run(failure)
    assert result.returncode != 0, (failure, "failure was hidden")
    assert calls, (failure, "shell fixture did not run", result.stderr)
    assert calls[-1].startswith(failure), (failure, "later work continued")
    assert "[bootstrap] done" not in result.stdout

print("PASS: failed setup, lookups, writes, key/media setup and store configuration stop continuation")

result, calls = run()
assert result.returncode == 0, result.stderr
assert calls.count("plugin install woocommerce --version=11.2.1") == 1
assert calls.index("plugin get woocommerce --field=version") < calls.index("plugin activate woocommerce")
check_setup_order(calls)
assert "[bootstrap] done" in result.stdout
assert sum(c.startswith("wc shipping_zone_method create") for c in calls) == 1
assert not any(c.startswith("wc product ") for c in calls)
assert "option update woocommerce_currency INR" in calls
assert "option update woocommerce_enable_guest_checkout yes" in calls
assert "option update woocommerce_coming_soon no" in calls
assert "wc payment_gateway update cod --enabled=true --user=fixture_admin" in calls

result, calls = run(existing=True, plugin_present=True)
assert result.returncode == 0, result.stderr
check_setup_order(calls)
assert not any(c.startswith("wc shipping_zone_method create") for c in calls)
assert not any(c.startswith("wc product ") for c in calls)
print("PASS: fresh stores and retries send no product reads or writes")
print("PASS: shipping, checkout, site settings and key/media setup remain configured")
print("PASS: keys and media setup run once, before store configuration")
assert not any(c.startswith("plugin install woocommerce") for c in calls)
result, calls = run(plugin_present=True, version="0.0.0")
assert result.returncode != 0
assert not any(c.startswith("plugin install woocommerce") for c in calls)
assert "plugin activate woocommerce" not in calls
assert "seed" not in calls
result, calls = run(pinned_version="")
assert result.returncode != 0 and calls == []
print("PASS: WooCommerce installs the pinned version; matching retries reuse it")
print("PASS: missing configuration and conflicting installed versions stop before activation")
result, calls = run(core_installed=False)
assert result.returncode == 0, result.stderr
assert any('core install ' in call and '--title=Assam Guwahati Tea' in call for call in calls)
result, calls = run(store_name="")
assert result.returncode != 0 and calls == []
print("PASS: a custom store name reaches WordPress installation; missing names stop before writes")
print("Mocked shell checks passed. No WordPress, database or Kubernetes calls made.")
