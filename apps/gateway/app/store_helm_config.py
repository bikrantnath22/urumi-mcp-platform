"""Fixed Helm inputs for one store; no files, processes or network calls."""

from dataclasses import dataclass
import json
import re

from app.store_helm_plan import plan_store_installation
from app.store_helm_release import MAX_RELEASE_REVISIONS, StoreRelease
from app.store_namespace import StoreNamespace


HELM_EXECUTABLE = "/usr/local/bin/helm"
STORE_CHART = "/opt/urumi/charts/store"
STORE_ENVIRONMENT_VALUES = "/etc/urumi/store-values.yaml"
STORE_MANAGED_VALUES = STORE_CHART + "/values-managed.yaml"
KUBERNETES_SERVER = "https://kubernetes.default.svc"
SERVICE_ACCOUNT_DIRECTORY = "/var/run/secrets/kubernetes.io/serviceaccount"
_CONTEXT = "urumi-store"


@dataclass(frozen=True, slots=True)
class StoreHelmConfig:
    workspace: str
    identity: StoreNamespace

    def __post_init__(self):
        if (
            not isinstance(self.identity, StoreNamespace)
            or not isinstance(self.workspace, str)
            or not re.fullmatch(r"/tmp/urumi-helm-[A-Za-z0-9_-]{8,64}", self.workspace)
        ):
            raise ValueError("Invalid private Helm workspace or store identity.")

    @property
    def kubeconfig_path(self):
        return self.workspace + "/kubeconfig.json"

    def kubeconfig(self):
        """Return fresh config pointing to the projected, rotating token file."""
        return {
            "apiVersion": "v1",
            "kind": "Config",
            "preferences": {},
            "clusters": [{
                "name": "urumi-cluster",
                "cluster": {
                    "server": KUBERNETES_SERVER,
                    "certificate-authority": SERVICE_ACCOUNT_DIRECTORY + "/ca.crt",
                    "insecure-skip-tls-verify": False,
                },
            }],
            "users": [{
                "name": "store-provisioner",
                "user": {"tokenFile": SERVICE_ACCOUNT_DIRECTORY + "/token"},
            }],
            "contexts": [{
                "name": _CONTEXT,
                "context": {
                    "cluster": "urumi-cluster",
                    "user": "store-provisioner",
                    "namespace": self.identity.name,
                },
            }],
            "current-context": _CONTEXT,
        }

    def environment(self):
        """An explicit environment; parent credentials/proxies are not copied."""
        root = self.workspace
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": root,
            "TMPDIR": root + "/tmp",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "KUBECONFIG": self.kubeconfig_path,
            "HELM_DRIVER": "secret",
            "HELM_NAMESPACE": self.identity.name,
            "HELM_CACHE_HOME": root + "/cache",
            "HELM_CONFIG_HOME": root + "/config",
            "HELM_DATA_HOME": root + "/data",
            "HELM_PLUGINS": root + "/plugins",
        }

    def _connection_arguments(self):
        return (
            "--kubeconfig", self.kubeconfig_path,
            "--kube-context", _CONTEXT,
            "--namespace", self.identity.name,
        )

    def list_arguments(self):
        """Include interrupted/failed releases; an empty list is not an error."""
        return (
            "list", "--all", "--filter", "^store$",
            "--max", "2", "--output", "json",
            *self._connection_arguments(),
        )

    def status_arguments(self):
        return (
            "status", self.identity.release_name, "--output", "json",
            *self._connection_arguments(),
        )

    def installation(self, checked, scope):
        """Return (plan, arguments, stdin); the caller still authorizes execution.

        The worker must write the private kubeconfig, create the workspace
        directories, verify actual namespace/Secret ownership, monitor its live
        claim, and bound the whole process by the original provisioning deadline.
        """
        plan = plan_store_installation(checked, scope)
        if plan.identity != self.identity:
            raise ValueError("The Helm configuration belongs to another store.")
        labels = ",".join(
            f"{key}={value}" for key, value in sorted(self.identity.labels.items())
        )
        arguments = (
            "install", self.identity.release_name, STORE_CHART,
            *self._connection_arguments(),
            # Managed defaults first; the environment may size storage/quotas.
            # The per-store plan remains last and owns all identity settings.
            "--values", STORE_MANAGED_VALUES,
            "--values", STORE_ENVIRONMENT_VALUES,
            "--values", "-",
            "--labels", labels,
            # Readiness is checked independently by the renewable worker job.
            # Keeping Helm out of the long bootstrap wait makes restart safe.
            "--wait=false",
            "--timeout", f"{plan.bootstrap_seconds}s",
            "--output", "json", "--hide-notes",
        )
        payload = json.dumps(
            plan.values(), ensure_ascii=True, allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return plan, arguments, payload

    def recovery(self, checked, scope, release):
        """Reconcile this same installation using its original saved values.

        Never force, replace, reset values or delete/re-run a bootstrap Job.
        Only a replacement attempt may reconcile an interrupted managed release.
        """
        plan = plan_store_installation(checked, scope)
        if (
            plan.identity != self.identity or type(release) is not StoreRelease
            or release.identity != self.identity or release.managed_bootstrap is not True
            or release.state != "failed" or checked.claim.attempt < 2
            or type(release.revision) is not int
            or not 1 <= release.revision < MAX_RELEASE_REVISIONS
        ):
            raise ValueError("A verified interrupted managed installation is required.")
        return plan, (
            "upgrade", self.identity.release_name, STORE_CHART,
            *self._connection_arguments(), "--reuse-values", "--wait=false",
            "--history-max", str(MAX_RELEASE_REVISIONS),
            "--timeout", f"{plan.bootstrap_seconds}s",
            "--output", "json", "--hide-notes",
        ), b""
