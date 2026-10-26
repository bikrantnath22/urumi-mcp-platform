"""Prepare one managed store; the worker commits Ready after this returns."""

import asyncio
from dataclasses import dataclass, field

from app.store_access_guard import verify_store_access_gate
from app.store_helm_release import StoreRelease
from app.store_helm_stage import prepare_installation
from app.store_job_guard import StoreJobClaimLost, require_claim
from app.store_jobs import StoreJobClaim
from app.store_mcp_verification import verify_store_mcps
from app.store_namespace import _opaque, namespace_for
from app.store_namespace_stage import prepare_namespace
from app.store_readiness import resource_read_paths
from app.store_registration import StoreMCPRegistrations, register_store_mcps
from app.store_secret_stage import prepare_secret
from app.store_volume_api import read_store_volume
from app.store_volume_stage import prepare_store_volumes
from app.store_volumes import STORE_CLAIM_NAMES, StoreVolumeBinding
from app.store_wordpress_verification import verify_store_wordpress
from app.store_workload_api import check_workloads


_WORKLOAD_POLL_SECONDS = 5.0


class StoreProvisioningPending(RuntimeError):
    """Resources remain pending; leave the operation for another guarded pass."""


class StoreProvisioningVerificationError(RuntimeError):
    """A stage returned an incomplete or invalid verification result."""


@dataclass(frozen=True, slots=True)
class StoreProvisioningResult:
    """Private result for this job; not authorization to finish another job."""

    claim: StoreJobClaim = field(repr=False)
    namespace_uid: str
    credentials_secret_uid: str
    registrations: StoreMCPRegistrations
    volumes: tuple[StoreVolumeBinding, ...]
    access_guarded: bool = False


def _flags(result, names):
    if (
        type(result) is not dict or set(result) != set(names)
        or any(type(value) is not bool for value in result.values())
    ):
        raise StoreProvisioningVerificationError(
            "Store verification returned an invalid result.",
        )
    return all(result.values())


def _volume_bindings(result, identity, namespace_uid):
    if (
        type(result) is not tuple or len(result) != len(STORE_CLAIM_NAMES)
        or any(type(item) is not StoreVolumeBinding for item in result)
        or tuple(item.claim_name for item in result) != STORE_CLAIM_NAMES
        or any(
            item.identity != identity or item.namespace_uid != namespace_uid
            for item in result
        )
    ):
        raise StoreProvisioningVerificationError(
            "Both verified store volume bindings are required.",
        )
    return result


async def provision_store(
    session_factory, claim, connection, scope, *, admin_email,
):
    """Perform one provisioning pass while the caller renews the same lease.

    Use as worker preparation, inside run_with_lease. Existing stages reuse
    accepted resources. Writes are not retried within this pass. Read-only
    workload polling retains the renewable claim and its creation slot.
    Registrations stay disabled; completion, cleanup and deletion are separate.
    """
    if not isinstance(claim, StoreJobClaim) or claim.action != "provision":
        raise ValueError("A private provisioning claim is required.")
    identity = namespace_for(claim)
    await require_claim(session_factory, claim)
    namespace_uid = await prepare_namespace(
        session_factory, claim, connection, scope,
    )
    if not _opaque(namespace_uid):
        raise StoreProvisioningVerificationError("Saved namespace ownership is missing.")
    await require_claim(
        session_factory, claim, expected_namespace_uid=namespace_uid,
    )
    secret_uid = await prepare_secret(
        session_factory, claim, connection, scope, admin_email=admin_email,
    )
    if not _opaque(secret_uid):
        raise StoreProvisioningVerificationError("Saved credential ownership is missing.")

    async def checked():
        return await require_claim(
            session_factory, claim,
            expected_namespace_uid=namespace_uid,
            expected_credentials_secret_uid=secret_uid,
        )

    await checked()
    release = await prepare_installation(
        session_factory, claim, connection, scope,
    )
    if (
        not isinstance(release, StoreRelease)
        or release.identity != identity or release.state != "deployed"
        or type(release.bootstrap_seconds) is not int
        or not 1 <= release.bootstrap_seconds <= 3600
    ):
        raise StoreProvisioningVerificationError("A verified deployed release is required.")

    options = {
        "namespace_uid": namespace_uid, "credentials_secret_uid": secret_uid,
    }

    async def workloads(*, wait=False):
        while True:
            await checked()
            flags = await check_workloads(
                session_factory, claim, connection, scope, **options,
                bootstrap_seconds=release.bootstrap_seconds,
                managed_bootstrap=release.managed_bootstrap,
            )
            if _flags(flags, resource_read_paths(identity)):
                return
            if not wait:
                raise StoreProvisioningPending("Store workloads are not ready yet.")
            # Keep the renewable claim and its global admission slot while
            # Kubernetes runs bootstrap. Cancellation stops polling, not the Job.
            current = await checked()
            remaining = current.remaining_seconds
            if remaining <= 0:
                raise StoreJobClaimLost("The workload observation allowance elapsed.")
            await asyncio.sleep(min(_WORKLOAD_POLL_SECONDS, remaining))

    await workloads(wait=release.managed_bootstrap)
    await checked()
    volumes = _volume_bindings(
        await prepare_store_volumes(session_factory, claim, connection, scope),
        identity, namespace_uid,
    )
    await checked()
    flags = await verify_store_wordpress(
        session_factory, claim, connection, scope, **options,
    )
    if not _flags(flags, (
        "storefront", "woo_rw_key_read", "woo_ro_key_read", "media_login",
    )):
        raise StoreProvisioningVerificationError("WordPress verification did not pass.")

    await checked()
    registrations = await register_store_mcps(session_factory, claim)
    if registrations is None:
        raise StoreJobClaimLost("The worker claim was lost before MCP registration.")
    if not isinstance(registrations, StoreMCPRegistrations):
        raise StoreProvisioningVerificationError("Store MCP registrations are missing.")
    await checked()
    counts = await verify_store_mcps(
        session_factory, claim, connection, scope, registrations, **options,
    )
    if (
        type(counts) is not dict or set(counts) != {"woo-mcp", "mcp-analytics"}
        or any(type(value) is not int for value in counts.values())
        or not 4 <= counts["woo-mcp"] <= 1000
        or not 3 <= counts["mcp-analytics"] <= 1000
    ):
        raise StoreProvisioningVerificationError("MCP discovery verification did not pass.")

    # Recheck workloads and saved storage after the service probes.
    await workloads()
    for binding in volumes:
        current = await read_store_volume(
            session_factory, claim, connection, scope,
            namespace_uid=namespace_uid, claim_name=binding.claim_name,
            expected=binding,
        )
        if type(current) is not StoreVolumeBinding or current != binding:
            raise StoreProvisioningVerificationError(
                "Store storage changed after service verification.",
            )
    await checked()
    if await verify_store_access_gate(
        session_factory, claim, connection, scope, **options,
    ) is not True:
        raise StoreProvisioningVerificationError("Store maintenance gate was not verified.")
    await checked()
    return StoreProvisioningResult(
        claim, namespace_uid, secret_uid, registrations, volumes, True,
    )

async def finish_provisioning(session_factory, claim, result):
    """Commit Ready after run_with_lease has stopped its renewal task.

    This performs no external requests. The completion transaction rechecks
    the private result, live claim, saved UIDs, volumes and disabled registrations.
    """
    from app.store_completion import finish_ready

    if not isinstance(result, StoreProvisioningResult):
        raise ValueError("A verified provisioning result is required.")
    return await finish_ready(session_factory, claim, verified=result)
