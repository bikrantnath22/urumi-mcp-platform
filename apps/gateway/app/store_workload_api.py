"""Collect one store's workload readiness through bounded, claim-checked reads."""

import asyncio

from app.store_guarded_kubernetes import (
    GuardedKubernetesConnection, StoreKubernetesStopped,
)
from app.store_guarded_secrets import GuardedStoreSecretConnection
from app.store_job_guard import (
    StoreJobClaimLost, StoreJobGuardUnavailable, require_claim,
)
from app.store_kube_connection import KubernetesAPIError, _check_request
from app.store_namespace import _opaque
from app.store_readiness import resource_read_paths, resource_ready
from app.store_secret_api import StoreSecretAPI


_CHECK_TIMEOUT_SECONDS = 30.0


class _WorkloadReads(GuardedKubernetesConnection):
    """Internal reads; check_workloads brackets collection with resource checks."""

    def __init__(
        self, connection, factory, claim, scope, *,
        namespace_uid, credentials_secret_uid,
    ):
        if not _opaque(namespace_uid) or not _opaque(credentials_secret_uid):
            raise ValueError("Saved namespace and Secret UIDs are required.")
        super().__init__(
            connection, factory, claim, scope, namespace_uid=namespace_uid,
        )
        self._secret_uid = credentials_secret_uid
        self._paths = resource_read_paths(self._identity)

    def _check_target(self, method, path, body):
        _check_request(method, path, body)
        if method != "GET" or body is not None or path not in self._paths.values():
            raise ValueError("This read is outside the checked store workload scope.")

    async def _checked(self):
        return await require_claim(
            self._factory, self._claim,
            expected_namespace_uid=self._uid,
            expected_credentials_secret_uid=self._secret_uid,
        )

async def check_workloads(
    session_factory, claim, connection, scope, *,
    namespace_uid, credentials_secret_uid, bootstrap_seconds, managed_bootstrap=False,
):
    """Return five readiness booleans; no polling, writes or finalization.

    Call after inspecting the deployed release. Use its saved bootstrap timeout.
    The caller owns the shared connection and lease renewal. A successful check
    still needs storefront/MCP probes before the store may become Ready.
    """
    if type(bootstrap_seconds) is not int or not 1 <= bootstrap_seconds <= 3600:
        raise ValueError("A valid saved bootstrap timeout is required.")
    if type(managed_bootstrap) is not bool:
        raise ValueError("A valid bootstrap ownership mode is required.")
    reader = _WorkloadReads(
        connection, session_factory, claim, scope,
        namespace_uid=namespace_uid, credentials_secret_uid=credentials_secret_uid,
    )
    secret_reader = GuardedStoreSecretConnection(
        connection, session_factory, claim, scope,
        namespace_uid=namespace_uid, credentials_secret_uid=credentials_secret_uid,
    )
    ownership = StoreSecretAPI(secret_reader)
    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_SECONDS):
            checked = await reader._checked()
            remaining = checked.remaining_seconds
            if remaining <= 0:
                raise StoreJobClaimLost("The workload observation allowance elapsed.")
            async with asyncio.timeout(min(_CHECK_TIMEOUT_SECONDS, remaining)):
                # These reads verify the actual active namespace and immutable Secret.
                await ownership.read_secret()
                result = {}
                for name, path in reader._paths.items():
                    document = await reader.request("GET", path)
                    result[name] = resource_ready(
                        document, reader._identity, name,
                        bootstrap_seconds=bootstrap_seconds,
                        managed_bootstrap=managed_bootstrap,
                    )
                await ownership.read_secret()
                await reader._checked()
                if checked.remaining_seconds <= 0:
                    raise StoreJobClaimLost("The workload observation allowance elapsed.")
                return result
    except (StoreJobClaimLost, StoreJobGuardUnavailable):
        raise StoreKubernetesStopped(write_may_have_applied=False) from None
    except TimeoutError:
        raise KubernetesAPIError(
            "The workload check exceeded its allowed time.",
            write_may_have_applied=False,
        ) from None
