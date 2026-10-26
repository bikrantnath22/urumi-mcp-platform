"""Build per-store Helm overrides; no database, subprocess or Kubernetes calls."""

from dataclasses import dataclass
import math
import re
from urllib.parse import urlsplit

from app.store_cluster_scope import StoreClusterScope
from app.store_job_guard import CheckedStoreJob
from app.store_namespace import StoreNamespace, _opaque, namespace_for
from app.store_secret import STORE_SECRET_NAME


class StoreHelmPlanError(ValueError):
    """A managed store cannot safely be mapped to chart settings."""


@dataclass(frozen=True, slots=True)
class StoreHelmPlan:
    identity: StoreNamespace
    scope: StoreClusterScope
    namespace_uid: str
    credentials_secret_uid: str
    host: str
    scheme: str
    bootstrap_seconds: int
    store_name: str = "Urumi Co."

    def values(self):
        """Fresh overrides; credentials remain in the existing Kubernetes Secret."""
        return {
            "storeHost": self.host,
            "storeName": self.store_name,
            "wordpressSetup": {"enabled": True},
            "storeScheme": self.scheme,
            "secretName": STORE_SECRET_NAME,
            "gatewayNamespace": self.scope.platform_namespace,
            "networkPolicy": {"enabled": True},
            "guardrails": {"enabled": True},
            "storeAccess": {"enabled": True, "storeId": str(self.identity.store_id)},
            "bootstrap": {
                "activeDeadlineSeconds": self.bootstrap_seconds,
                "managedJob": True,
            },
            "wooMcp": {"port": 8000},
            "analytics": {
                "enabled": True,
                "port": 8000,
                "secretName": STORE_SECRET_NAME,
                "wooBaseUrl": "http://wordpress",
                "wooHostHeader": self.host,
            },
        }


def plan_store_installation(checked, scope):
    """Use after namespace/Secret binding; recheck the live claim before execution.

    The caller must also load the managed-store quota/profile values. A plan is
    configuration, not permission to run Helm or proof that the store is ready.
    """
    if (
        not isinstance(checked, CheckedStoreJob)
        or not isinstance(scope, StoreClusterScope)
        or checked.for_cleanup
        or checked.claim.action != "provision"
        or checked.remaining_seconds <= 0
        or not _opaque(checked.namespace_uid)
        or not _opaque(checked.credentials_secret_uid)
    ):
        raise StoreHelmPlanError("A checked provisioning job with saved UIDs is required.")

    claim = checked.claim
    identity = namespace_for(claim)
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if (
        not isinstance(claim.name, str)
        or not 1 <= len(claim.name) <= 100
        or not claim.name.strip() or not claim.name.isprintable()
        or not isinstance(claim.slug, str)
        or not 1 <= len(claim.slug) <= 32
        or not re.fullmatch(label, claim.slug)
        or not isinstance(claim.url, str)
    ):
        raise StoreHelmPlanError("Invalid managed store address.")

    try:
        address = urlsplit(claim.url)
        host = address.hostname or ""
        base = host.removeprefix(claim.slug + ".")
        local = base == "localtest.me" or base.endswith(".localtest.me")
        valid = (
            len(host) <= 253
            and len(base) <= 200
            and host.startswith(claim.slug + ".")
            and re.fullmatch(rf"(?:{label}\.)+[a-z](?:[a-z0-9-]{{0,61}}[a-z0-9])?", base)
            and address.scheme in ("http", "https")
            and (address.scheme == "https" or local)
            and claim.url == f"{address.scheme}://{host}"
        )
        deadline = (claim.deadline_at - claim.checked_at).total_seconds()
        lease = (claim.lease_expires_at - claim.checked_at).total_seconds()
        elapsed = max(0.0, min(deadline, lease) - checked.remaining_seconds)
        seconds = math.floor(deadline - elapsed)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise StoreHelmPlanError("Invalid managed store address or deadline.") from None

    if not valid or lease <= 0 or not 1 <= seconds <= 3600:
        raise StoreHelmPlanError("Invalid managed store address or remaining deadline.")
    return StoreHelmPlan(
        identity, scope, checked.namespace_uid, checked.credentials_secret_uid,
        host, address.scheme, seconds, claim.name,
    )
