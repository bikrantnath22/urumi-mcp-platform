"""Cleanup-only volume inventory; observations do not prove disk deletion."""

import asyncio
from dataclasses import dataclass

from app.store_guarded_kubernetes import (
    GuardedKubernetesConnection, StoreKubernetesStopped,
)
from app.store_job_guard import StoreJobClaimLost, StoreJobGuardUnavailable
from app.store_kube_connection import KubernetesAPIError, _check_request
from app.store_namespace import _opaque
from app.store_volume_pages import StoreVolumeInventory
from app.store_volumes import StoreVolumeError, StoreVolumePending


_SCAN_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class StoreVolumeScan:
    namespace_uid: str
    namespace_present: bool
    resource_version: str
    bindings: tuple


class _InventoryReads(GuardedKubernetesConnection):
    def __init__(
        self, connection, factory, claim, scope, *, namespace_uid, known=(),
    ):
        if not _opaque(namespace_uid):
            raise ValueError("A saved namespace UID is required.")
        super().__init__(
            connection, factory, claim, scope,
            namespace_uid=namespace_uid, for_cleanup=True,
        )
        self._pages = StoreVolumeInventory(
            self._identity, namespace_uid=namespace_uid, known=known,
        )
        self._namespace_path = f"/api/v1/namespaces/{self._identity.name}"
        self._absent = False

    def _check_target(self, method, path, body):
        _check_request(method, path, body)
        if method == "GET" and body is None:
            if path == self._namespace_path:
                return
            if not self._pages.complete and path == self._pages.next_path:
                return
        raise ValueError("This inventory read is outside the checked worker job.")

    async def _namespace(self):
        document = await self.request("GET", self._namespace_path)
        if document is None:
            self._absent = True
            return False
        if self._absent:
            raise StoreVolumeError("The store namespace reappeared during cleanup.")
        if document.get("status", {}).get("phase") not in ("Active", "Terminating"):
            raise StoreVolumePending("The store namespace state is not available.")
        return True


async def read_store_volume_inventory(
    session_factory, claim, connection, scope, *, namespace_uid, known=(),
):
    """Return this store's identities after one complete, consistent scan.

    Every page checks the live cleanup lease and the actual namespace UID.
    Missing namespaces are allowed; replacements are rejected. No raw volume
    documents, unrelated identities or continuation tokens leave this helper.
    Empty results are observations only: provisioning can still be in flight.
    The caller owns lease renewal and persists recovered identities separately.
    """
    reader = _InventoryReads(
        connection, session_factory, claim, scope,
        namespace_uid=namespace_uid, known=known,
    )
    try:
        async with asyncio.timeout(_SCAN_TIMEOUT_SECONDS):
            checked = await reader._checked()
            remaining = checked.remaining_seconds
            if remaining <= 0:
                raise StoreJobClaimLost("The inventory allowance elapsed.")
            async with asyncio.timeout(min(_SCAN_TIMEOUT_SECONDS, remaining)):
                await reader._namespace()
                while not reader._pages.complete:
                    document = await reader.request("GET", reader._pages.next_path)
                    reader._pages.accept(document)
                    present = await reader._namespace()
                await reader._checked()
                if checked.remaining_seconds <= 0:
                    raise StoreJobClaimLost("The inventory allowance elapsed.")
                return StoreVolumeScan(
                    namespace_uid, present, reader._pages.resource_version,
                    reader._pages.result(),
                )
    except (StoreJobClaimLost, StoreJobGuardUnavailable):
        raise StoreKubernetesStopped(write_may_have_applied=False) from None
    except TimeoutError:
        raise KubernetesAPIError(
            "The volume inventory exceeded its allowed time.",
            write_may_have_applied=False,
        ) from None
