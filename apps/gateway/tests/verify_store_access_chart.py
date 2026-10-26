"""Render maintenance gates for two stores and keep the original demo independent."""
import json
from pathlib import Path
import subprocess
from uuid import uuid4
import yaml

root = Path(__file__).resolve().parents[3]


def render(namespace, values):
    result = subprocess.run([
        "helm", "template", "store", str(root / "charts/store"), "-n", namespace,
        "-f", str(root / "charts/store/values-local.yaml"),
        "-f", str(root / "charts/store/values-managed.yaml"), "-f", "-",
    ], input=json.dumps(values), text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, "Store chart did not render."
    return [item for item in yaml.safe_load_all(result.stdout) if item]


for index in range(2):
    sid = uuid4()
    namespace = "urumi-store-" + sid.hex
    host = "fixture-" + str(index) + ".localtest.me"
    documents = render(namespace, {
        "storeHost": host, "gatewayNamespace": "platform",
        "storeAccess": {"enabled": True, "storeId": str(sid)},
    })
    middleware = next(item for item in documents if item["kind"] == "Middleware")
    ingress = next(item for item in documents if item["kind"] == "Ingress")
    assert middleware["metadata"]["namespace"] == namespace
    assert middleware["spec"] == {"forwardAuth": {
        "address": "http://gateway.platform.svc.cluster.local:8000/api/store-access/" + str(sid),
        "trustForwardHeader": False,
    }}
    assert ingress["metadata"]["annotations"]["traefik.ingress.kubernetes.io/router.middlewares"] == namespace + "-store-access@kubernetescrd"
    assert ingress["spec"]["rules"][0]["host"] == host
demo = render("store", {})
assert not any(item["kind"] == "Middleware" for item in demo)
ingress = next(item for item in demo if item["kind"] == "Ingress")
assert "traefik.ingress.kubernetes.io/router.middlewares" not in ingress["metadata"].get("annotations", {})
print("Two managed stores use separate gates; the original store chart remains independent.")
print("Chart gate checks passed. No cluster or database calls were made.")
