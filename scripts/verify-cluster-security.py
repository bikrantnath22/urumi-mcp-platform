"""Read-only checks for the local store/platform security deployment.

Run in WSL after both Helm releases finish:
    python3 scripts/verify-cluster-security.py
Uses kubectl, existing container tools and read-only MCP calls. No model calls,
permission grants, product changes or database writes are made by this script.
"""

import json
import subprocess


def kubectl(*arguments, input=None, timeout=120):
    result = subprocess.run(
        ["kubectl", *arguments], input=input, text=True,
        capture_output=True, timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "kubectl failed")
    return result.stdout


def objects(namespace, resource):
    return json.loads(kubectl("-n", namespace, "get", resource, "-o", "json"))["items"]


def exec_python(namespace, deployment, source):
    output = kubectl("-n", namespace, "exec", "-i", f"deployment/{deployment}",
                     "-c", deployment, "--", "python", "-", input=source)
    print(output.strip(), flush=True)


def check_runtime():
    runtime_probe = r'''
while read -r field value; do
  case "$field" in Uid:|NoNewPrivs:|CapEff:|Seccomp:) printf '%s %s\n' "$field" "$value";; esac
done < /proc/1/status
if [ -e /var/run/secrets/kubernetes.io/serviceaccount/token ]; then
  echo 'Unexpected Kubernetes service-account token' >&2
  exit 1
fi
'''
    expected = {
        "store": {"mysql": 999, "wordpress": 33, "woo-mcp": 10001, "mcp-analytics": 10001},
        "platform": {"postgres": 999, "gateway": 10001, "dashboard": 101, "mcp-notes": 10001},
    }
    for namespace, apps in expected.items():
        pods = objects(namespace, "pods")
        for app, uid in apps.items():
            active = [pod for pod in pods if pod["metadata"].get("labels", {}).get("app") == app
                      and not pod["metadata"].get("deletionTimestamp")]
            assert active, f"Missing workload: {namespace}/{app}"
            for pod in active:
                name = pod["metadata"]["name"]
                assert pod["status"]["phase"] == "Running", f"Pod not running: {name}"
                assert all(item["ready"] for item in pod["status"]["containerStatuses"]), f"Pod not ready: {name}"
                spec = pod["spec"]
                assert spec["automountServiceAccountToken"] is False
                assert spec["serviceAccountName"] == f"{app}-{namespace}"
                for container in spec["containers"]:
                    output = kubectl("-n", namespace, "exec", name, "-c", container["name"],
                                     "--", "sh", "-c", runtime_probe)
                    fields = dict(line.split(":", 1) for line in output.splitlines())
                    assert {int(value) for value in fields["Uid"].split()} == {uid}, f"Wrong runtime UID: {name}"
                    assert fields["NoNewPrivs"].strip() == "1", f"Privilege escalation enabled: {name}"
                    assert int(fields["CapEff"].strip(), 16) == 0, f"Effective capabilities present: {name}"
                    assert fields["Seccomp"].strip() == "2", f"Seccomp filter missing: {name}"
            print(f"PASS: {namespace}/{app}: ready, UID {uid}, zero capabilities, no API token", flush=True)


def check_accounts():
    expected = {
        "store": ("mysql", "wordpress", "woo-mcp", "mcp-analytics", "store-bootstrap"),
        "platform": ("postgres", "gateway", "dashboard", "mcp-notes"),
    }
    actions = (("get", "pods"), ("list", "secrets"), ("get", "secrets"),
               ("create", "pods"), ("create", "pods/exec"),
               ("patch", "deployments.apps"), ("create", "rolebindings.rbac.authorization.k8s.io"),
               ("create", "namespaces"))
    for namespace, apps in expected.items():
        accounts = {item["metadata"]["name"]: item for item in objects(namespace, "serviceaccounts")}
        for app in apps:
            name = f"{app}-{namespace}"
            assert accounts[name]["automountServiceAccountToken"] is False
            identity = [f"--as=system:serviceaccount:{namespace}:{name}",
                        "--as-group=system:serviceaccounts",
                        f"--as-group=system:serviceaccounts:{namespace}",
                        "--as-group=system:authenticated"]
            for verb, resource in actions:
                result = subprocess.run(
                    ["kubectl", "-n", namespace, "auth", "can-i", verb, resource, *identity],
                    text=True, capture_output=True, timeout=15,
                )
                assert result.returncode == 1 and result.stdout.strip() == "no", (
                    f"RBAC check failed: {namespace}/{name} {verb} {resource}: "
                    f"{result.stdout.strip()} {result.stderr.strip()}"
                )
            print(f"PASS: {namespace}/{name}: checked Kubernetes API actions denied", flush=True)


def check_network_and_tools():
    for namespace in ("store", "platform"):
        policies = objects(namespace, "networkpolicies")
        deny = next(item for item in policies if item["metadata"]["name"] == "default-deny")
        assert deny["spec"]["podSelector"] == {}
        assert set(deny["spec"]["policyTypes"]) == {"Ingress", "Egress"}
        assert not deny["spec"].get("ingress") and not deny["spec"].get("egress")
    exec_python("platform", "gateway", r'''
import asyncio
import errno
import socket
import httpx
from app.upstreams import call_tool, discover_tools

async def check():
    for name, url, tool, arguments in (
        ("WooCommerce", "http://woo-mcp.store.svc.cluster.local:8000/mcp", "list_products", {"per_page": 1}),
        ("Analytics", "http://mcp-analytics.store.svc.cluster.local:8000/mcp", "order_counts", {}),
        ("Notes", "http://mcp-notes.platform.svc.cluster.local:8000/mcp", "list_notes", {"limit": 1}),
    ):
        tools = await discover_tools(url)
        result = await call_tool(url, tool, arguments)
        assert not result.isError, f"Read-only tool failed: {name}"
        print(f"PASS: {name}: {len(tools)} tools discovered; {tool} succeeded")
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.get("https://api.groq.com/openai/v1/models")
        print(f"PASS: public HTTPS and TLS reach Groq (HTTP {response.status_code}); no API key or completion sent")
    for name, host, port in (
        ("WordPress", "wordpress.store.svc.cluster.local", 80),
        ("MySQL", "mysql.store.svc.cluster.local", 3306),
        ("Kubernetes API", "kubernetes.default.svc.cluster.local", 443),
    ):
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        try:
            connection = socket.create_connection((host, port), timeout=3)
        except OSError as error:
            assert isinstance(error, TimeoutError) or error.errno in {
                errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EACCES, errno.EPERM,
            }, f"Unexpected connection failure: {name}: {type(error).__name__}"
            print(f"PASS: direct gateway -> {name} blocked")
        else:
            connection.close()
            raise RuntimeError(f"Network isolation failed: gateway reached {name}")

asyncio.run(check())
''')
    # Destinations have just passed positive gateway discovery and real read calls.
    # Test both Service and Pod IPs so service translation cannot mask a bypass.
    endpoints = []
    for namespace, app, port in (("store", "woo-mcp", 8000), ("platform", "mcp-notes", 8000),
                                 ("platform", "postgres", 5432), ("store", "mysql", 3306)):
        target = next(item for item in objects(namespace, "pods")
                      if item["metadata"].get("labels", {}).get("app") == app
                      and not item["metadata"].get("deletionTimestamp"))
        endpoints.extend(((f"{app} service", f"{app}.{namespace}.svc.cluster.local", port),
                          (f"{app} pod", target["status"]["podIP"], port)))
    exec_python("store", "mcp-analytics", "endpoints = " + repr(endpoints) + r'''
import errno
import socket
import httpx

response = httpx.get("http://wordpress/", headers={"Host": "store.localtest.me"}, timeout=15, trust_env=False)
assert response.status_code == 200, "Analytics could not reach WordPress"
print("PASS: Analytics DNS and permitted WordPress connection work")
for name, host, port in endpoints:
    socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    try:
        connection = socket.create_connection((host, port), timeout=3)
    except OSError as error:
        assert isinstance(error, TimeoutError) or error.errno in {
            errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EACCES, errno.EPERM,
        }, f"Unexpected connection failure: {name}: {type(error).__name__}"
        print(f"PASS: Analytics -> {name} blocked")
    else:
        connection.close()
        raise RuntimeError(f"Network isolation failed: Analytics reached {name}")
''')


if __name__ == "__main__":
    check_runtime()
    check_accounts()
    check_network_and_tools()
    print("Local security verification passed. No model completions or data changes were requested.", flush=True)
