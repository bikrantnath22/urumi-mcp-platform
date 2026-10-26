"""Administrator setup for an existing Ready store. Defaults to server dry-run.

New worker-managed stores install this automatically. This script never changes
the original demo store, passwords, products, grants or store Helm releases.
"""
import argparse
import json
import re
import subprocess
from urllib.parse import urlsplit
from uuid import UUID

GATE = "traefik.ingress.kubernetes.io/router.middlewares"


def run(args, body=None, missing=False):
    result = subprocess.run(
        ["kubectl", *args], input=body, text=True, capture_output=True, timeout=60,
    )
    if result.returncode:
        raise SystemExit("Kubernetes operation failed; inspect the named resource before retrying.")
    if not result.stdout.strip() and missing:
        return None
    return result.stdout


def remote(platform, payload, code):
    program = "import json\nPAYLOAD = json.loads(" + repr(json.dumps(payload)) + ")\n" + code
    result = run(
        ["-n", platform, "exec", "-i", "deployment/gateway", "-c", "gateway",
         "--", "python", "-"], program,
    )
    return json.loads(result)


READ = """
import asyncio
from uuid import UUID
from sqlalchemy import select, text
from app.database import Session, engine
from app.models import Store, StoreOperation
async def main():
    try:
        async with Session() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            store = await db.get(Store, UUID(PAYLOAD["id"]))
            assert store and store.status == "Ready" and store.desired_state == "present", "A Ready managed store is required."
            assert store.access_enabled and store.deleted_at is None, "Enable the store before configuring its gate."
            assert store.namespace_uid and store.namespace == "urumi-store-" + store.id.hex
            active = await db.scalar(select(StoreOperation.id).where(
                StoreOperation.store_id == store.id,
                StoreOperation.status.in_(("Pending", "Running"))))
            assert active is None, "Wait for the active store job."
            return dict(id=str(store.id), owner_id=store.owner_id, namespace=store.namespace,
                namespace_uid=store.namespace_uid, generation=store.generation,
                host=store.url.split("://", 1)[1], guarded=store.access_guarded)
    finally:
        await engine.dispose()
print(json.dumps(asyncio.run(main())))
"""

ARM = """
import asyncio
from uuid import UUID
from sqlalchemy import select, func
from app.database import Session, engine
from app.models import Store, StoreOperation, SecurityEvent
from app.store_access_guard import check_gate_documents
from app.store_cluster_scope import StoreClusterScope
from app.store_namespace import StoreNamespace
from app.store_settings import lock_store_admission
async def main():
    try:
        async with Session() as db:
            await lock_store_admission(db)
            before = PAYLOAD["store"]
            store = await db.scalar(select(Store).where(Store.id == UUID(before["id"]))
                .with_for_update().execution_options(populate_existing=True))
            assert store and store.status == "Ready" and store.desired_state == "present"
            assert store.access_enabled and store.deleted_at is None
            assert (store.namespace_uid, store.generation, store.owner_id) == (
                before["namespace_uid"], before["generation"], before["owner_id"])
            active = await db.scalar(select(StoreOperation.id).where(
                StoreOperation.store_id == store.id,
                StoreOperation.status.in_(("Pending", "Running"))))
            assert active is None
            identity = StoreNamespace(store.id, store.owner_id)
            scope = StoreClusterScope(PAYLOAD["platform"], PAYLOAD["release"])
            scope.check_namespace(identity, PAYLOAD["namespace"], expected_uid=store.namespace_uid)
            check_gate_documents(identity, scope, before["host"], PAYLOAD["ingress"], PAYLOAD["middleware"])
            if not store.access_guarded:
                store.access_guarded = True
                store.access_revision += 1
                store.updated_at = await db.scalar(select(func.clock_timestamp()))
                db.add(SecurityEvent(actor_id=None, target_user_id=store.owner_id,
                    action="store.maintenance_configured", details={"store_id": str(store.id)}))
            await db.commit()
            return {"configured": True}
    finally:
        await engine.dispose()
print(json.dumps(asyncio.run(main())))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store_id", type=UUID)
    parser.add_argument("--platform", default="platform")
    parser.add_argument("--release", default="platform")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    assert re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", args.platform), "Invalid platform namespace."
    assert 1 <= len(args.release) <= 53 and all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", part) for part in args.release.split(".")
    ), "Invalid platform release."
    store = remote(args.platform, {"id": str(args.store_id)}, READ)
    namespace = store["namespace"]
    assert namespace == "urumi-store-" + args.store_id.hex
    assert store["host"] == urlsplit("http://" + store["host"]).hostname

    def read(kind, name):
        value = run(["-n", namespace, "get", kind, name, "--ignore-not-found", "-o", "json"], missing=True)
        return json.loads(value) if value else None

    def owned_namespace():
        document = json.loads(run(["get", "namespace", namespace, "-o", "json"]))
        meta = document["metadata"]
        assert meta["uid"] == store["namespace_uid"] and not meta.get("deletionTimestamp")
        assert document.get("status", {}).get("phase") == "Active"
        for key, value in {
            "urumi.ai/managed-by": "store-provisioner", "urumi.ai/store-id": args.store_id.hex,
            "urumi.ai/owner-id": str(store["owner_id"]),
        }.items():
            assert meta.get("labels", {}).get(key) == value, "Namespace ownership changed."
        for key, value in {
            "urumi.ai/helm-release": "store", "urumi.ai/platform-namespace": args.platform,
            "urumi.ai/platform-release": args.release,
        }.items():
            assert meta.get("annotations", {}).get(key) == value, "Platform ownership changed."
        for mode in ("enforce", "audit", "warn"):
            assert meta["labels"].get("pod-security.kubernetes.io/" + mode) == "restricted"
            assert meta["labels"].get("pod-security.kubernetes.io/" + mode + "-version") == "latest"
        return document

    owned_namespace()
    ingress = read("ingress", "wordpress")
    assert ingress and not ingress["metadata"].get("deletionTimestamp")
    rules = ingress.get("spec", {}).get("rules", [])
    assert len(rules) == 1 and rules[0]["host"] == store["host"]
    assert rules[0]["http"]["paths"] == [{
        "path": "/", "pathType": "Prefix",
        "backend": {"service": {"name": "wordpress", "port": {"number": 80}}},
    }], "Store ingress route changed."
    wanted = namespace + "-store-access@kubernetescrd"
    annotations = ingress["metadata"].get("annotations") or {}
    assert annotations.get(GATE) in (None, wanted), "Existing middleware must be reviewed."
    definition = {
        "apiVersion": "traefik.io/v1alpha1", "kind": "Middleware",
        "metadata": {"name": "store-access", "namespace": namespace},
        "spec": {"forwardAuth": {
            "address": "http://gateway." + args.platform + ".svc.cluster.local:8000/api/store-access/" + str(args.store_id),
            "trustForwardHeader": False,
        }},
    }
    middleware = read("middleware.traefik.io", "store-access")
    if middleware:
        assert middleware.get("spec") == definition["spec"], "Existing maintenance destination changed."
        assert not middleware["metadata"].get("deletionTimestamp")
    else:
        run(["create", "--dry-run=server", "-f", "-"], json.dumps(definition))
    operations = [
        {"op": "test", "path": "/metadata/uid", "value": ingress["metadata"]["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": ingress["metadata"]["resourceVersion"]},
        {"op": "add", "path": "/metadata/annotations", "value": {**annotations, GATE: wanted}},
    ]
    patch = ["-n", namespace, "patch", "ingress", "wordpress", "--type=json", "-p", json.dumps(operations)]
    run([*patch, "--dry-run=server"])
    if not args.apply:
        print("Preview passed for " + namespace + ". No resources or database rows changed.")
        print("Run again with --apply to install this store's gate.")
        return
    if middleware is None:
        run(["create", "-f", "-"], json.dumps(definition))
    if annotations.get(GATE) != wanted:
        run(patch)
    proof = {
        "platform": args.platform, "release": args.release, "store": store,
        "namespace": owned_namespace(), "ingress": read("ingress", "wordpress"),
        "middleware": read("middleware.traefik.io", "store-access"),
    }
    assert remote(args.platform, proof, ARM) == {"configured": True}
    print("Maintenance gate configured for " + namespace + ". Store data and grants were unchanged.")


if __name__ == "__main__":
    main()
