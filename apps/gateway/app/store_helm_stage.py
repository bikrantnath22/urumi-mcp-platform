"""Install or recover one owned release under a live worker claim."""

import asyncio

from app.store_guarded_helm import GuardedStoreHelm
from app.store_helm_plan import plan_store_installation
from app.store_helm_release import _decode, inspect_store_release
from app.store_helm_recovery import interrupt_pending_installation
from app.store_job_guard import StoreJobClaimLost, require_claim


class StoreInstallationNeedsRecovery(RuntimeError):
    def __init__(self, state):
        super().__init__(
            "Store installation needs cleanup and recovery before continuing.",
        )
        self.state = state
        self.write_may_have_applied = True


class StoreInstallationUncertain(StoreJobClaimLost):
    def __init__(self):
        super().__init__(
            "The installation outcome could not be confirmed; recovery must inspect it.",
        )
        self.write_may_have_applied = True


async def prepare_installation(session_factory, claim, connection, scope):
    """Return a verified deployed release; this does not mark the store Ready.

    Namespace and Secret stages must finish first. The caller owns connection
    cleanup and lease renewal. A replacement attempt can reconcile an interrupted
    managed release using its saved values. This never deletes resources, resets
    the bootstrap Job, changes the original deadline or finalizes a store.
    """
    checked = await require_claim(session_factory, claim)
    plan = plan_store_installation(checked, scope)
    namespace_uid = plan.namespace_uid
    secret_uid = plan.credentials_secret_uid
    helm = GuardedStoreHelm(
        connection, session_factory, checked.claim, scope,
        namespace_uid=namespace_uid, credentials_secret_uid=secret_uid,
    )

    async def observe():
        current = await require_claim(
            session_factory, checked.claim,
            expected_namespace_uid=namespace_uid,
            expected_credentials_secret_uid=secret_uid,
        )
        expected = plan_store_installation(current, scope)
        listing = await helm.list_releases()
        status = None if _decode(listing) == [] else await helm.read_release()
        result = inspect_store_release(listing, status, expected)
        await require_claim(
            session_factory, checked.claim,
            expected_namespace_uid=namespace_uid,
            expected_credentials_secret_uid=secret_uid,
        )
        return result

    release = await observe()
    if release.state == "absent":
        try:
            # Ignore command stdout: read the persisted release independently.
            await helm.install()
            release = await observe()
        except asyncio.CancelledError:
            # Guarded Helm awaits subprocess cleanup before cancellation escapes.
            raise
        except Exception:
            # An accepted write may survive any failure of the later checks.
            # Leave resources for a fresh inspection; never retry here.
            raise StoreInstallationUncertain() from None

    if release.state != "deployed":
        if (
            release.state not in ("failed", "pending-install", "pending-upgrade")
            or not release.managed_bootstrap or checked.claim.attempt < 2
        ):
            raise StoreInstallationNeedsRecovery(release.state)
        previous = release
        try:
            if release.state in ("pending-install", "pending-upgrade"):
                await interrupt_pending_installation(
                    session_factory, checked.claim, connection, scope, release,
                )
                release = await observe()
            if release.state != "failed":
                raise StoreInstallationNeedsRecovery(release.state)
            await helm.recover(release)
            release = await observe()
            if (
                release.state != "deployed"
                or release.revision != previous.revision + 1
                or release.bootstrap_seconds != previous.bootstrap_seconds
            ):
                raise StoreInstallationNeedsRecovery(release.state)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise StoreInstallationUncertain() from None
    return release
