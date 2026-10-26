"""Pure checks for the fixed store workloads; no service calls or state changes."""

from app.store_namespace import StoreNamespace, _opaque


_RESOURCES = {
    "mysql": ("apps/v1", "StatefulSet", "statefulsets"),
    "wordpress": ("apps/v1", "Deployment", "deployments"),
    "woo-mcp": ("apps/v1", "Deployment", "deployments"),
    "mcp-analytics": ("apps/v1", "Deployment", "deployments"),
    "store-bootstrap": ("batch/v1", "Job", "jobs"),
}


class StoreReadinessError(ValueError):
    """A resource cannot safely be checked for this store."""


class StoreBootstrapFailed(StoreReadinessError):
    """Bootstrap reported failure; raw Kubernetes diagnostics stay private."""


def _invalid():
    raise StoreReadinessError("Store workload status could not be verified.")


def resource_read_paths(identity):
    if not isinstance(identity, StoreNamespace):
        _invalid()
    return {
        name: f"/apis/{api}/namespaces/{identity.name}/{plural}/{name}"
        for name, (api, kind, plural) in _RESOURCES.items()
    }


def _count(mapping, key, default=0):
    value = mapping.get(key, default)
    if type(value) is not int or value < 0:
        _invalid()
    return value


def _conditions(status):
    values = status.get("conditions", [])
    if type(values) is not list or len(values) > 128:
        _invalid()
    result = {}
    for condition in values:
        if type(condition) is not dict:
            _invalid()
        name, value = condition.get("type"), condition.get("status")
        if (
            not _opaque(name) or name in result
            or value not in ("True", "False", "Unknown")
        ):
            _invalid()
        result[name] = value
    return result


def resource_ready(
    document, identity, name, *, bootstrap_seconds=None, managed_bootstrap=False,
):
    """Inspect one fresh GET response; None/missing status means still pending.

    The caller must verify its live claim, active namespace and saved Secret UID
    around collection. This only checks workload rollout and bootstrap completion;
    storefront and MCP connectivity still require separate probes.
    """
    paths = resource_read_paths(identity)
    if not isinstance(name, str) or name not in paths:
        _invalid()
    if name == "store-bootstrap" and (
        type(bootstrap_seconds) is not int or not 1 <= bootstrap_seconds <= 3600
        or type(managed_bootstrap) is not bool
    ):
        _invalid()
    if document is None:
        return False
    if type(document) is not dict:
        _invalid()
    api, kind, plural = _RESOURCES[name]
    metadata = document.get("metadata")
    spec = document.get("spec")
    status = document.get("status", {})
    if (
        document.get("apiVersion") != api or document.get("kind") != kind
        or type(metadata) is not dict or type(spec) is not dict
        or type(status) is not dict
    ):
        _invalid()
    if (
        metadata.get("name") != name or metadata.get("namespace") != identity.name
        or not _opaque(metadata.get("uid"))
        or not _opaque(metadata.get("resourceVersion"))
        or metadata.get("deletionTimestamp") is not None
    ):
        _invalid()
    generation = _count(metadata, "generation")
    annotations = metadata.get("annotations", {})
    if generation < 1 or type(annotations) is not dict:
        _invalid()
    conditions = _conditions(status)
    if kind == "Job":
        if managed_bootstrap:
            labels = metadata.get("labels", {})
            if (
                type(labels) is not dict
                or labels.get("app.kubernetes.io/managed-by") != "Helm"
                or annotations.get("meta.helm.sh/release-name") != identity.release_name
                or annotations.get("meta.helm.sh/release-namespace") != identity.name
                or "helm.sh/hook" in annotations
                or "helm.sh/hook-delete-policy" in annotations
            ):
                _invalid()
        elif (
            annotations.get("helm.sh/hook") != "post-install,post-upgrade"
            or annotations.get("helm.sh/hook-delete-policy") != "before-hook-creation"
        ):
            _invalid()
        if (
            _count(spec, "activeDeadlineSeconds") != bootstrap_seconds
            or _count(spec, "completions", 1) != 1
            or _count(spec, "parallelism", 1) != 1
            or spec.get("suspend", False) is not False
        ):
            _invalid()
        if any(conditions.get(key) == "True" for key in ("Failed", "FailureTarget")):
            raise StoreBootstrapFailed("Store bootstrap did not finish successfully.")
        return (
            conditions.get("Complete") == "True"
            and _count(status, "succeeded") == 1
            and _count(status, "active") == 0
            and _count(status, "terminating") == 0
        )

    labels = metadata.get("labels", {})
    if (
        type(labels) is not dict
        or labels.get("app.kubernetes.io/managed-by") != "Helm"
        or annotations.get("meta.helm.sh/release-name") != identity.release_name
        or annotations.get("meta.helm.sh/release-namespace") != identity.name
        or _count(spec, "replicas") != 1
    ):
        _invalid()
    counts = ["replicas", "updatedReplicas", "readyReplicas"]
    if kind == "Deployment":
        counts.append("availableReplicas")
        if spec.get("paused", False) is not False:
            _invalid()
        extra = (
            _count(status, "unavailableReplicas") == 0
            and conditions.get("Available") == "True"
            and conditions.get("Progressing") != "False"
            and conditions.get("ReplicaFailure") != "True"
        )
    else:
        counts.append("currentReplicas")
        extra = (
            _opaque(status.get("currentRevision"))
            and status.get("currentRevision") == status.get("updateRevision")
        )
    numbers = [_count(status, key) for key in counts]
    observed = _count(status, "observedGeneration")
    return observed == generation and all(value == 1 for value in numbers) and extra
