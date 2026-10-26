"""Fence an abandoned Helm operation before a normal, value-preserving upgrade."""

import asyncio
import base64
from copy import deepcopy
import gzip
import io
import json

from app.store_guarded_secrets import GuardedStoreSecretConnection
from app.store_helm_plan import plan_store_installation
from app.store_helm_release import (
    MAX_RELEASE_REVISIONS, StoreRelease, StoreReleaseError, _decode,
    inspect_store_release,
)
from app.store_job_guard import require_claim
from app.store_kube_connection import KubernetesAPIError
from app.store_namespace import _opaque
from app.store_secret_api import StoreSecretAPI


_MAX_RELEASE_BYTES = 1024 * 1024
_RECOVERY_TIMEOUT_SECONDS = 15.0


def _invalid():
    raise StoreReleaseError("The interrupted Helm operation could not be verified.")


def _unpack(value):
    if type(value) is not str or not 0 < len(value) <= _MAX_RELEASE_BYTES:
        _invalid()
    try:
        compressed = base64.b64decode(base64.b64decode(value, validate=True), validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(_MAX_RELEASE_BYTES + 1)
        return _decode(raw)
    except (ValueError, OSError, EOFError, UnicodeError):
        raise StoreReleaseError("The interrupted Helm operation could not be verified.") from None


def _pack(document):
    raw = json.dumps(document, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > _MAX_RELEASE_BYTES:
        _invalid()
    return base64.b64encode(base64.b64encode(gzip.compress(raw, mtime=0))).decode("ascii")


def interruption_patch(document, plan, observed):
    """Only change an owned pending operation to failed, never to deployed.

    This is a Helm-3 storage-format operation. Every change uses UID, resource
    version, old payload and old status tests in one atomic JSON Patch. A changed
    or completed release rejects the whole patch. The subsequent normal Helm
    upgrade performs reconciliation and records its own deployed status.
    """
    if (
        type(observed) is not StoreRelease or observed.identity != plan.identity
        or observed.managed_bootstrap is not True
        or observed.state not in ("pending-install", "pending-upgrade")
        or type(observed.revision) is not int
        or not 1 <= observed.revision < MAX_RELEASE_REVISIONS
        or type(document) is not dict
        or document.get("apiVersion") != "v1" or document.get("kind") != "Secret"
        or document.get("type") != "helm.sh/release.v1"
    ):
        _invalid()
    metadata, data = document.get("metadata"), document.get("data")
    if type(metadata) is not dict or type(data) is not dict:
        _invalid()
    labels = metadata.get("labels")
    name = f"sh.helm.release.v1.store.v{observed.revision}"
    if (
        metadata.get("name") != name or metadata.get("namespace") != plan.identity.name
        or not _opaque(metadata.get("uid")) or not _opaque(metadata.get("resourceVersion"))
        or metadata.get("deletionTimestamp") is not None or type(labels) is not dict
        or any(labels.get(key) != value for key, value in {
            **plan.identity.labels, "owner": "helm", "name": "store",
            "status": observed.state, "version": str(observed.revision),
        }.items())
    ):
        _invalid()
    release = _unpack(data.get("release"))
    listing = [{
        "name": "store", "namespace": plan.identity.name,
        "revision": str(observed.revision), "chart": "store-0.1.0",
        "app_version": "1.0", "status": observed.state,
    }]
    parsed = inspect_store_release(json.dumps(listing).encode(), json.dumps(release).encode(), plan)
    chart = release.get("chart")
    chart_metadata = chart.get("metadata") if type(chart) is dict else None
    if parsed != observed or type(chart_metadata) is not dict or any(
        chart_metadata.get(key) != value for key, value in {
            "name": "store", "version": "0.1.0", "appVersion": "1.0",
        }.items()
    ):
        _invalid()
    updated = deepcopy(release)
    updated["info"]["status"] = "failed"
    updated["info"]["description"] = "Interrupted provisioning; replacement worker will reconcile."
    return [
        {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
        {"op": "test", "path": "/data/release", "value": data["release"]},
        {"op": "test", "path": "/metadata/labels/status", "value": observed.state},
        {"op": "replace", "path": "/data/release", "value": _pack(updated)},
        {"op": "replace", "path": "/metadata/labels/status", "value": "failed"},
    ]


async def interrupt_pending_installation(factory, claim, connection, scope, observed):
    """A replacement claim may fence one abandoned operation; no retries here.

    The caller renews the lease and has awaited all old subprocess cleanup before
    releasing its previous claim. An expired/replaced claim cannot unlock Helm.
    """
    if claim.attempt < 2:
        _invalid()
    initial = await require_claim(factory, claim)
    plan = plan_store_installation(initial, scope)
    if type(observed) is not StoreRelease or observed.identity != plan.identity:
        _invalid()
    path = (
        f"/api/v1/namespaces/{plan.identity.name}/secrets/"
        f"sh.helm.release.v1.store.v{observed.revision}"
    )
    ownership = StoreSecretAPI(GuardedStoreSecretConnection(
        connection, factory, claim, scope, namespace_uid=plan.namespace_uid,
        credentials_secret_uid=plan.credentials_secret_uid,
    ))

    async def checked():
        await ownership.read_secret()
        return await require_claim(
            factory, claim, expected_namespace_uid=plan.namespace_uid,
            expected_credentials_secret_uid=plan.credentials_secret_uid,
        )

    attempted = False
    try:
        async with asyncio.timeout(min(_RECOVERY_TIMEOUT_SECONDS, initial.remaining_seconds)):
            await checked()
            document = await connection.request("GET", path)
            patch = interruption_patch(document, plan, observed)
            await checked()
            attempted = True
            response = await connection.request("PATCH", path, body=patch)
            await checked()
            if (
                type(response) is not dict
                or response.get("metadata", {}).get("uid") != document["metadata"]["uid"]
                or response.get("metadata", {}).get("labels", {}).get("status") != "failed"
                or response.get("data", {}).get("release") != patch[4]["value"]
            ):
                _invalid()
    except asyncio.CancelledError:
        raise
    except Exception:
        raise KubernetesAPIError(
            "Interrupted Helm recovery could not be confirmed.",
            write_may_have_applied=attempted,
        ) from None
