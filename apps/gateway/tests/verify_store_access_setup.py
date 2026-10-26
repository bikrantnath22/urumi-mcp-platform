"""Exercise existing-store gate setup with fixture kubectl and database replies."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root / "apps/gateway"))
from app.store_access_guard import check_gate_documents
from app.store_cluster_scope import StoreClusterScope
from app.store_namespace import StoreNamespace


def check(script_source=None):
    path = root / "scripts/install-store-access.py"
    spec = importlib.util.spec_from_loader("store_access_setup_fixture", loader=None)
    script = importlib.util.module_from_spec(spec)
    script.__file__ = str(path)
    exec(compile(script_source or path.read_text(encoding="utf-8"), str(path), "exec"), script.__dict__)
    identity, scope = StoreNamespace(uuid4(), 7), StoreClusterScope("platform", "platform")

    class Fixture:
        def __init__(self):
            self.info = dict(id=str(identity.store_id), owner_id=7, namespace=identity.name,
                namespace_uid="namespace-uid", generation=1, host="fixture.localtest.me", guarded=False)
            self.namespace = scope.namespace_manifest(identity)
            self.namespace["metadata"].update(uid="namespace-uid", resourceVersion="17")
            self.namespace["status"] = {"phase": "Active"}
            self.ingress = {
                "apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
                "metadata": {"name": "wordpress", "namespace": identity.name,
                    "uid": "ingress-uid", "resourceVersion": "2",
                    "annotations": {"meta.helm.sh/release-name": "store"}},
                "spec": {"rules": [{"host": "fixture.localtest.me", "http": {"paths": [{
                    "path": "/", "pathType": "Prefix",
                    "backend": {"service": {"name": "wordpress", "port": {"number": 80}}},
                }]}}]},
            }
            self.middleware = None
            self.writes, self.armed = [], False

        def remote(self, platform, payload, code):
            assert platform == "platform"
            if code == script.READ:
                assert payload == {"id": str(identity.store_id)}
                return deepcopy(self.info)
            assert code == script.ARM
            assert payload["store"] == self.info
            scope.check_namespace(identity, payload["namespace"], expected_uid="namespace-uid")
            assert check_gate_documents(identity, scope, self.info["host"],
                payload["ingress"], payload["middleware"])
            self.armed = True
            return {"configured": True}

        def run(self, args, body=None, missing=False):
            if args[:2] == ["get", "namespace"]:
                return json.dumps(self.namespace)
            if args[:3] == ["-n", identity.name, "get"]:
                value = self.ingress if args[3] == "ingress" else self.middleware
                return json.dumps(value) if value is not None else None
            if args[0] == "create":
                document = json.loads(body)
                assert document["metadata"]["namespace"] == identity.name
                assert document["kind"] == "Middleware"
                if "--dry-run=server" not in args:
                    assert self.middleware is None
                    self.writes.append("middleware")
                    self.middleware = document
                    self.middleware["metadata"].update(uid="middleware-uid", resourceVersion="3")
                return "{}"
            if args[:4] == ["-n", identity.name, "patch", "ingress"]:
                operations = json.loads(args[args.index("-p") + 1])
                assert operations[0]["value"] == self.ingress["metadata"]["uid"]
                assert operations[1]["value"] == self.ingress["metadata"]["resourceVersion"]
                if "--dry-run=server" not in args:
                    self.writes.append("ingress")
                    self.ingress["metadata"]["annotations"] = operations[2]["value"]
                    self.ingress["metadata"]["resourceVersion"] = "4"
                return "{}"
            raise AssertionError("Unexpected kubectl operation: " + repr(args))

        def run_main(self, apply=False):
            argv = ["install-store-access.py", str(identity.store_id)] + (["--apply"] if apply else [])
            with patch.object(sys, "argv", argv), patch.object(script, "remote", self.remote), patch.object(script, "run", self.run):
                script.main()

    f = Fixture()
    f.run_main()
    assert not f.writes and not f.armed and f.middleware is None
    f.run_main(apply=True)
    assert f.writes == ["middleware", "ingress"] and f.armed
    before = deepcopy((f.namespace, f.ingress, f.middleware))
    f.writes.clear()
    f.run_main(apply=True)
    assert not f.writes and (f.namespace, f.ingress, f.middleware) == before
    for alter in ("uid", "owner", "platform", "demo", "middleware", "annotation"):
        f = Fixture()
        if alter == "uid": f.namespace["metadata"]["uid"] = "replaced"
        if alter == "owner": f.namespace["metadata"]["labels"]["urumi.ai/owner-id"] = "99"
        if alter == "platform": f.namespace["metadata"]["annotations"]["urumi.ai/platform-release"] = "other"
        if alter == "demo": f.info["namespace"] = "store"
        if alter == "middleware": f.middleware = {"metadata": {}, "spec": {"forwardAuth": {"address": "http://other"}}}
        if alter == "annotation": f.ingress["metadata"]["annotations"][script.GATE] = "other@kubernetescrd"
        try:
            f.run_main(apply=True)
        except AssertionError:
            pass
        else:
            raise AssertionError("Changed ownership or route was accepted")
        assert not f.writes and not f.armed
    print("Setup defaults to dry-run, checks owned identities before writes and preserves matching gates on replay.")
    print("Store gate setup fixtures passed. No kubectl, database or live services were used.")


if __name__ == "__main__":
    check()
