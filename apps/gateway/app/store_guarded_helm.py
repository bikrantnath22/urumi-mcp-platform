"""Private Helm calls under a live store claim and verified resource identities."""

import asyncio

from app.store_cluster_scope import StoreClusterScope
from app.store_guarded_secrets import GuardedStoreSecretConnection
from app.store_helm_config import HELM_EXECUTABLE
from app.store_helm_plan import plan_store_installation
from app.store_helm_release import StoreRelease, inspect_store_release
from app.store_helm_process import HelmProcessError, run_helm_process
from app.store_helm_workspace import helm_workspace
from app.store_job_guard import StoreJobClaimLost, require_claim
from app.store_jobs import StoreJobClaim
from app.store_kube_connection import KubernetesConnection
from app.store_lease_runner import _stop_tasks
from app.store_namespace import _opaque, namespace_for
from app.store_secret_api import StoreSecretAPI


_CHECK_SECONDS = 5.0
_CHECK_TIMEOUT_SECONDS = 10.0
_READ_TIMEOUT_SECONDS = 15.0


class StoreHelmStopped(StoreJobClaimLost):
    def __init__(self, *, write_may_have_applied):
        super().__init__(
            "Store ownership could not be confirmed; Helm work stopped.",
        )
        self.write_may_have_applied = write_may_have_applied


class GuardedStoreHelm:
    """Fixed commands only; the caller owns the shared client and lease renewal."""

    def __init__(
        self, connection, session_factory, claim, scope, *,
        namespace_uid, credentials_secret_uid,
    ):
        if (
            not isinstance(connection, KubernetesConnection)
            or not callable(session_factory)
            or not isinstance(claim, StoreJobClaim)
            or claim.action != "provision"
            or not isinstance(scope, StoreClusterScope)
            or not _opaque(namespace_uid)
            or not _opaque(credentials_secret_uid)
        ):
            raise ValueError("Invalid guarded Helm configuration.")
        self._connection = connection
        self._factory = session_factory
        self._claim = claim
        self._scope = scope
        self._identity = namespace_for(claim)
        self._namespace_uid = namespace_uid
        self._secret_uid = credentials_secret_uid

    async def _checked(self):
        async with asyncio.timeout(_CHECK_TIMEOUT_SECONDS):
            return await require_claim(
                self._factory, self._claim,
                expected_namespace_uid=self._namespace_uid,
                expected_credentials_secret_uid=self._secret_uid,
            )

    async def _preflight(self, previous):
        allowance = min(_CHECK_TIMEOUT_SECONDS, previous.remaining_seconds)
        if allowance <= 0:
            raise StoreJobClaimLost("The checked Helm allowance elapsed.")
        async with asyncio.timeout(allowance):
            checked = await self._checked()
            connection = GuardedStoreSecretConnection(
                self._connection, self._factory, checked.claim, self._scope,
                namespace_uid=self._namespace_uid,
                credentials_secret_uid=self._secret_uid,
            )
            # Reads verify the actual active namespace and immutable Secret.
            # Do not retain or return the Secret response or its credentials.
            await StoreSecretAPI(connection).read_secret()
            if checked.remaining_seconds <= 0:
                raise StoreJobClaimLost("The checked Helm allowance elapsed.")
            return await self._checked()

    async def list_releases(self):
        return await self._run("list")

    async def read_release(self):
        return await self._run("status")

    async def install(self):
        return await self._run("install")

    async def recover(self, release):
        if type(release) is not StoreRelease:
            raise ValueError("A verified interrupted release is required.")
        checked = await self._checked()
        plan = plan_store_installation(checked, self._scope)
        actual = inspect_store_release(
            await self.list_releases(), await self.read_release(), plan,
        )
        if actual != release:
            raise ValueError("The interrupted Helm observation changed before reconciliation.")
        return await self._run("upgrade", release=release)

    async def _run(self, operation, *, release=None):
        """Return bounded bytes; no retries, store finalization or readiness.

        The enclosing run_with_lease callback renews the lease. These checks
        observe ownership without extending that lease or the original deadline.
        Process cleanup finishes before the private workspace can be removed.
        """
        if operation not in ("list", "status", "install", "upgrade"):
            raise ValueError("Unsupported private Helm command.")
        if operation == "upgrade" and type(release) is not StoreRelease:
            raise ValueError("A verified interrupted release is required.")
        writing = operation in ("install", "upgrade")
        attempted = False
        try:
            checked = await self._checked()
            with helm_workspace(self._identity) as config:
                checked = await self._preflight(checked)
                if writing:
                    if operation == "install":
                        plan, arguments, payload = config.installation(checked, self._scope)
                    else:
                        plan, arguments, payload = config.recovery(
                            checked, self._scope, release,
                        )
                    timeout = plan.bootstrap_seconds
                else:
                    arguments = (
                        config.list_arguments() if operation == "list"
                        else config.status_arguments()
                    )
                    payload = b""
                    timeout = min(_READ_TIMEOUT_SECONDS, checked.remaining_seconds)

                async def monitor():
                    nonlocal checked
                    while True:
                        remaining = checked.remaining_seconds
                        if remaining <= 0:
                            raise StoreJobClaimLost("The checked Helm allowance elapsed.")
                        await asyncio.sleep(min(_CHECK_SECONDS, remaining))
                        checked = await self._preflight(checked)

                tasks = []
                try:
                    attempted = True
                    command = asyncio.create_task(run_helm_process(
                        HELM_EXECUTABLE, arguments, env=config.environment(),
                        payload=payload, timeout_seconds=timeout, writing=writing,
                    ))
                    tasks.append(command)
                    watching = asyncio.create_task(monitor())
                    tasks.append(watching)
                    await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED,
                    )
                    if watching.done():
                        watching.result()
                        raise StoreJobClaimLost("Helm ownership monitoring stopped.")
                    result = command.result()
                    await _stop_tasks([watching])
                    if not watching.cancelled():
                        watching.result()
                    checked = await self._preflight(checked)
                    return result
                finally:
                    await _stop_tasks(tasks)
        except asyncio.CancelledError:
            raise
        except HelmProcessError:
            raise
        except Exception:
            raise StoreHelmStopped(
                write_may_have_applied=writing and attempted,
            ) from None
