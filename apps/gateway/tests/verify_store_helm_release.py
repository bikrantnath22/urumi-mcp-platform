"""Offline Helm release checks; no subprocess, database or service calls."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.store_cluster_scope import StoreClusterScope
from app.store_helm_plan import StoreHelmPlan
from app.store_helm_release import StoreReleaseError, inspect_store_release
from app.store_namespace import StoreNamespace


def encoded(value):
    return json.dumps(value).encode("utf-8")


def rejected(operation):
    try:
        operation()
    except StoreReleaseError as error:
        assert str(error) == "The stored Helm release could not be verified."
        return
    raise AssertionError("An invalid release was accepted")


identity = StoreNamespace(uuid4(), 1)
scope = StoreClusterScope("platform", "platform")
plan = StoreHelmPlan(
    identity, scope, "namespace-uid", "secret-uid",
    "assam-tea.localtest.me", "http", 1200,
)
listing = [{
    "name": "store", "namespace": identity.name, "revision": "1",
    "chart": "store-0.1.0", "app_version": "1.0", "status": "deployed",
}]
release = {
    "name": "store", "namespace": identity.name, "version": 1,
    "info": {"status": "deployed", "description": "private fixture diagnostic"},
    "config": plan.values(), "manifest": "private fixture manifest",
}
release["config"]["operatorProfile"] = {"private": "fixture"}
original = deepcopy(release)

def read(items=listing, document=release, expected=plan):
    return inspect_store_release(
        encoded(items), encoded(document) if document is not None else None, expected,
    )


absent = read([], None)
assert absent.state == "absent" and absent.bootstrap_seconds is None
for initial, final in (
    ("deployed", "deployed"), ("failed", "failed"),
    ("pending-install", "pending-install"), ("pending-install", "deployed"),
    ("pending-install", "failed"),
):
    items, document = deepcopy(listing), deepcopy(release)
    items[0]["status"], document["info"]["status"] = initial, final
    result = read(items, document)
    assert result.identity == identity and result.state == final
    assert result.bootstrap_seconds == 1200
    assert "private" not in repr(result)
    try:
        result.state = "changed"
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("Observation was mutable")
assert release == original
print("Absent, deployed, failed and interrupted releases are distinguished without writes")

later = StoreHelmPlan(
    identity, scope, "namespace-uid", "secret-uid",
    plan.host, plan.scheme, 90,
)
assert read(expected=later).bootstrap_seconds == 1200
other = StoreHelmPlan(
    StoreNamespace(uuid4(), 2), scope, "other-namespace", "other-secret",
    "tulsi-tea.localtest.me", "http", 1200,
)
rejected(lambda: read(expected=other))
for key, value in (
    ("name", "other"), ("namespace", "platform"), ("revision", "2"),
    ("revision", 1), ("chart", "store-0.2.0"), ("app_version", "2.0"),
    ("status", "unknown"), ("status", []),
):
    items = deepcopy(listing)
    items[0][key] = value
    rejected(lambda items=items: read(items))
for items in (None, {}, [None], listing * 2):
    rejected(lambda items=items: read(items))
rejected(lambda: read([], release))
rejected(lambda: read(listing, None))
print("Store identity, chart, matching bounded revisions and original bootstrap timeout are checked")

for revision in (2, 8):
    items, document = deepcopy(listing), deepcopy(release)
    items[0]["revision"], document["version"] = str(revision), revision
    result = read(items, document)
    assert result.revision == revision and result.managed_bootstrap is True
    document["config"]["bootstrap"]["managedJob"] = False
    rejected(lambda: read(items, document))
items, document = deepcopy(listing), deepcopy(release)
items[0]["revision"], document["version"] = "9", 9
rejected(lambda: read(items, document))
legacy = deepcopy(release)
legacy["config"]["bootstrap"].pop("managedJob")
assert read(document=legacy).managed_bootstrap is False
print("Managed recovery revisions are bounded by the Secret quota; legacy deployed releases remain compatible")

for key, value in (
    ("name", "other"), ("namespace", "other"), ("version", 2),
    ("version", True), ("version", "1"), ("info", None),
    ("config", None),
):
    document = deepcopy(release)
    document[key] = value
    rejected(lambda document=document: read(document=document))
for state in ("unknown", "pending-upgrade", "superseded", None, []):
    document = deepcopy(release)
    document["info"]["status"] = state
    rejected(lambda document=document: read(document=document))
document = deepcopy(release)
document["info"]["status"] = "failed"
rejected(lambda: read(document=document))
for section, key, value in (
    (None, "storeHost", "tulsi-tea.localtest.me"),
    (None, "storeScheme", "https"),
    (None, "secretName", "another-secret"),
    (None, "gatewayNamespace", "other-platform"),
    ("networkPolicy", "enabled", False),
    ("guardrails", "enabled", 1),
    ("analytics", "enabled", False),
    ("analytics", "secretName", "another-secret"),
    ("analytics", "wooBaseUrl", "http://other-store"),
    ("analytics", "wooHostHeader", "tulsi-tea.localtest.me"),
    ("wooMcp", "port", 8000.0),
    ("bootstrap", "activeDeadlineSeconds", True),
    ("bootstrap", "activeDeadlineSeconds", 0),
    ("bootstrap", "activeDeadlineSeconds", 3601),
):
    document = deepcopy(release)
    target = document["config"] if section is None else document["config"][section]
    target[key] = value
    rejected(lambda document=document: read(document=document))
for section in plan.values():
    document = deepcopy(release)
    del document["config"][section]
    rejected(lambda document=document: read(document=document))
print("Changed store overrides, unsafe types and unsupported transitions reject reuse")

for raw in (
    b"", b"null", b"[]", b"{", b"\xff",
    b'{"name":"store","name":"other"}',
    b'{"private":NaN}', b'{"private":Infinity}', b'{"private":1e999}',
    b"[" * 2000 + b"]" * 2000,
    b" " * (1024 * 1024 + 1),
):
    rejected(lambda raw=raw: inspect_store_release(encoded(listing), raw, plan))
rejected(lambda: inspect_store_release("[]", None, plan))
rejected(lambda: inspect_store_release(b"[]", None, object()))
assert "labels" not in release and "chart" not in release
assert not hasattr(read(), "config") and not hasattr(read(), "manifest")

chart = Path(__file__).resolve().parents[3] / "charts/store/Chart.yaml"
lines = chart.read_text(encoding="utf-8").splitlines()
assert all(line in lines for line in (
    "name: store", "version: 0.1.0", 'appVersion: "1.0"',
)), "Update the release check together with the bundled chart version."
print("Malformed/oversized JSON, duplicate keys and non-finite numbers return safe errors")
print("Helm release verification passed. No live services or processes were used.")
