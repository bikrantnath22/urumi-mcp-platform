"""Read one store's bound PVC/PV identifiers under a live private job claim."""

import asyncio

from app.store_guarded_kubernetes import (
    GuardedKubernetesConnection, StoreKubernetesStopped,
)
from app.store_job_guard import StoreJobClaimLost, StoreJobGuardUnavailable
from app.store_kube_connection import KubernetesAPIError, _check_request
from app.store_namespace import _opaque
from app.store_volumes import (
    STORE_CLAIM_NAMES, StoreVolumeBinding, StoreVolumeError,
    StoreVolumePending, _bound_claim, check_bound_volume,
)


_CHECK_TIMEOUT_SECONDS = 30.0


class _VolumeReads(GuardedKubernetesConnection):
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
        self._volume_path = None

    def _check_target(self, method, path, body):
        _check_request(method, path, body)
        if (
            method == "GET" and body is None
            and path in (self._namespace_path, self._claim_path, self._volume_path)
        ):
            return
        raise ValueError("This storage read is outside the checked worker job.")

    def _select_volume(self, document, expected):
        metadata, name = _bound_claim(
            self._identity, namespace_uid=self._uid,
            claim_name=self._claim_name, claim=document,
        )
        if expected is not None and (
            metadata["uid"] != expected.claim_uid or name != expected.volume_name
        ):
            raise StoreVolumeError("The saved store claim was replaced or rebound.")
        self._volume_path = "/api/v1/persistentvolumes/" + name

    async def _namespace(self):
        document = await self.request("GET", self._namespace_path)
        if document is None:
            raise StoreVolumePending("The saved store namespace is not available.")
        phases = ("Active", "Terminating") if self._cleanup else ("Active",)
        if document.get("status", {}).get("phase") not in phases:
            raise StoreVolumePending("The saved store namespace is not available.")


async def read_store_volume(
    session_factory, claim, connection, scope, *, namespace_uid, claim_name,
    expected=None, for_cleanup=False,
):
    """Return one immutable binding; no persistence, retries or deletion.

    Missing/unbound storage stays pending. Cleanup mode permits the original
    provisioning deadline to pass, but still requires a live worker lease.
    This snapshot cannot prove storage cleanup or authorize a deletion.
    """
    reader = _VolumeReads(
        connection, session_factory, claim, scope,
        namespace_uid=namespace_uid, claim_name=claim_name,
        for_cleanup=for_cleanup,
    )
    if expected is not None and (
        not isinstance(expected, StoreVolumeBinding)
        or expected.identity != reader._identity
        or expected.namespace_uid != namespace_uid
        or expected.claim_name != claim_name
    ):
        raise ValueError("The saved volume binding belongs to a different store claim.")

    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_SECONDS):
            checked = await reader._checked()
            remaining = checked.remaining_seconds
            if remaining <= 0:
                raise StoreJobClaimLost("The storage observation allowance elapsed.")
            async with asyncio.timeout(min(_CHECK_TIMEOUT_SECONDS, remaining)):
                await reader._namespace()
                document = await reader.request("GET", reader._claim_path)
                reader._select_volume(document, expected)
                await reader._namespace()
                volume = await reader.request("GET", reader._volume_path)
                binding = check_bound_volume(
                    reader._identity, namespace_uid=namespace_uid,
                    claim_name=claim_name, claim=document, volume=volume,
                    expected=expected,
                )
                # Reject a claim replaced or rebound while its PV was read.
                latest = await reader.request("GET", reader._claim_path)
                check_bound_volume(
                    reader._identity, namespace_uid=namespace_uid,
                    claim_name=claim_name, claim=latest, volume=volume,
                    expected=binding,
                )
                await reader._namespace()
                await reader._checked()
                if checked.remaining_seconds <= 0:
                    raise StoreJobClaimLost("The storage observation allowance elapsed.")
                return binding
    except (StoreJobClaimLost, StoreJobGuardUnavailable):
        raise StoreKubernetesStopped(write_may_have_applied=False) from None
    except TimeoutError:
        raise KubernetesAPIError(
            "The storage check exceeded its allowed time.",
            write_may_have_applied=False,
        ) from None
