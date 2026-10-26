"""Run fake WordPress/storage in a PHP runtime. No real database/HTTP calls."""
import argparse
import base64
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[3]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--php", help="Local PHP executable; otherwise uses the original demo pod runtime.")
args = parser.parse_args()
source = (root / "charts/store/files/wordpress-setup.php").read_text(encoding="utf-8")
fixture = Path(__file__).with_name("store_wordpress_setup_fixture.php").read_text(encoding="utf-8")
marker = "__URUMI_SETUP_SOURCE_BASE64__"
assert fixture.count(marker) == 1 and source.startswith("<?php")
program = fixture.replace(marker, base64.b64encode(source.encode()).decode())
command = [args.php, "-n"] if args.php else [
    "kubectl", "-n", "store", "exec", "-i", "deployment/wordpress", "--", "php", "-n", "/dev/stdin",
]
result = subprocess.run(command, input=program, text=True, capture_output=True, timeout=60)
print(result.stdout, end="")
if result.returncode:
    print(result.stderr, end="")
    raise SystemExit(result.returncode)
