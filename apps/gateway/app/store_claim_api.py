"""Read one fixed store PVC, including Pending claims, under a live job."""

import asyncio

from app.store_claims import StoreClaimIdentity, check_store_claim
from app.store_guarded_kubernetes import (
    GuardedKubernetesConnection, StoreKubernetesStopped,
)
from app.store_job_guard import StoreJobClaimLost, StoreJobGuardUnavailable
from app.store_kube_connection import KubernetesAPIError, _check_request
from app.store_namespace import _opaque
from app.store_volumes import (
    STORE_CLAIM_NAMES, StoreVolumeBinding, StoreVolumePending,
)


_CHECK_TIMEOUT_SECONDS = 30.0


class _ClaimReads(GuardedKubernetesConnection):
    def __init__(
        self, connection, factory, claim, scope, *,
        namespace_uid, claim_name, for_cleanup=False,
    ):
        if not _opaque(namespace_uid) or claim_name not in STORE_CLAIM_NAMES:
            raise ValueError("A saved namespace UID and fixed store claim are required.")
        super().__init__(
            connection, factory, claim, scope,
            namespace_uid=namespace_uid, for_cleanup=for_cleanup,
        )
        self._namespace_path = f"/api/v1/namespaces/{self._identity.name}"
        self._claim_path = self._namespace_path + "/persistentvolumeclaims/" + claim_name
        self._claim_name = claim_name

    def _check_target(self, method, path, body):
        _check_request(method, path, body)
        if (
            method == "GET" and body is None
            and path in (self._namespace_path, self._claim_path)
        ):
            return
        raise ValueError("This claim read is outside the checked worker job.")

    async def _namespace(self):
        document = await self.request("GET", self._namespace_path)
        if document is None:
            raise StoreVolumePending("The saved store namespace is not available.")
        phases = ("Active", "Terminating") if self._cleanup else ("Active",)
        if document.get("status", {}).get("phase") not in phases:
            raise StoreVolumePending("The saved store namespace is not available.")

    def _observe(self, document, expected):
        return check_store_claim(
            self._identity, namespace_uid=self._uid,
            claim_name=self._claim_name, document=document,
            expected=expected, for_cleanup=self._cleanup,
        )


async def read_store_claim(
    session_factory, claim, connection, scope, *, namespace_uid, claim_name,
    expected=None, for_cleanup=False,
):
    """Return identity only; no persistence, PV reads, writes or cleanup proof."""
    reader = _ClaimReads(
        connection, session_factory, claim, scope,
        namespace_uid=namespace_uid, claim_name=claim_name,
        for_cleanup=for_cleanup,
    )
    if expected is not None and (
        type(expected) not in (StoreClaimIdentity, StoreVolumeBinding)
        or expected.identity != reader._identity
        or expected.namespace_uid != namespace_uid
        or expected.claim_name != claim_name
    ):
        raise ValueError("The saved identity belongs to a different store claim.")

    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_SECONDS):
            checked = await reader._checked()
            remaining = checked.remaining_seconds
            if remaining <= 0:
                raise StoreJobClaimLost("The claim observation allowance elapsed.")
            async with asyncio.timeout(min(_CHECK_TIMEOUT_SECONDS, remaining)):
                await reader._namespace()
                document = await reader.request("GET", reader._claim_path)
                first = reader._observe(document, expected)
                await reader._namespace()
                document = await reader.request("GET", reader._claim_path)
                latest = reader._observe(document, first)
                await reader._namespace()
                await reader._checked()
                if checked.remaining_seconds <= 0:
                    raise StoreJobClaimLost("The claim observation allowance elapsed.")
                return latest
    except (StoreJobClaimLost, StoreJobGuardUnavailable):
        raise StoreKubernetesStopped(write_may_have_applied=False) from None
    except TimeoutError:
        raise KubernetesAPIError(
            "The store claim check exceeded its allowed time.",
            write_may_have_applied=False,
        ) from None
