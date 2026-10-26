"""Prepare and bind store credentials before any workload installation."""

from app.store_guarded_secrets import GuardedStoreSecretConnection
from app.store_job_guard import StoreJobClaimLost, require_claim
from app.store_namespace import StoreNamespaceError
from app.store_secret_api import StoreSecretAPI
from app.store_secret_binding import bind_store_secret


def _api(session_factory, claim, connection, scope, namespace_uid, secret_uid):
    guarded = GuardedStoreSecretConnection(
        connection, session_factory, claim, scope,
        namespace_uid=namespace_uid,
        credentials_secret_uid=secret_uid,
    )
    return StoreSecretAPI(guarded)


async def prepare_secret(
    session_factory, claim, connection, scope, *, admin_email,
):
    """Return the saved Secret UID after binding and a guarded readback.

    The namespace stage must have completed first. The caller owns the shared
    connection and lease renewal. This stage installs no workloads, does not
    mark the store Ready, and returns no passwords or API keys.
    """
    checked = await require_claim(session_factory, claim)
    namespace_uid = checked.namespace_uid
    if namespace_uid is None:
        raise StoreNamespaceError(
            "Save the namespace UID before preparing store credentials."
        )

    api = _api(
        session_factory, checked.claim, connection, scope,
        namespace_uid, checked.credentials_secret_uid,
    )
    document = await api.ensure_secret(admin_email=admin_email)
    uid = await bind_store_secret(
        session_factory, checked.claim, document,
        scope=scope, namespace_uid=namespace_uid,
    )
    del document
    if uid is None:
        raise StoreJobClaimLost(
            "The worker claim was lost before Secret ownership was saved."
        )

    # Use a new view pinned to the committed UID; the unbound view is finished.
    api = _api(
        session_factory, checked.claim, connection, scope,
        namespace_uid, uid,
    )
    await api.read_secret()
    await require_claim(
        session_factory, checked.claim,
        expected_namespace_uid=namespace_uid,
        expected_credentials_secret_uid=uid,
    )
    return uid
