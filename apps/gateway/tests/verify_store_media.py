"""Exercise the media helper with fake WordPress/storage inside the PHP runtime."""

import base64
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[3]
helper = (root / "charts/store/files/media.php").read_text(encoding="utf-8")
fixture = Path(__file__).with_name("store_media_fixture.php").read_text(encoding="utf-8")
source_marker = "__URUMI_MEDIA_SOURCE_BASE64__"
mode_marker = "__URUMI_MEDIA_HASH_MODE__"
assert helper.startswith("<?php"), "Unexpected media helper."
assert fixture.startswith("<?php"), "Unexpected fixture."
assert fixture.count(source_marker) == fixture.count(mode_marker) == 1, "Incomplete fixture."
encoded = base64.b64encode(helper.encode("utf-8")).decode("ascii")

for mode in ("current", "legacy"):
    program = fixture.replace(source_marker, encoded).replace(mode_marker, mode)
    result = subprocess.run(
        ["kubectl", "-n", "store", "exec", "-i", "deployment/wordpress",
         "--", "php", "-n", "/dev/stdin"],
        input=program, text=True, timeout=60,
    )
    if result.returncode:
        raise SystemExit(result.returncode)
