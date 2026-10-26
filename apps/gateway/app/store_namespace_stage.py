"""Private namespace stages; these do not install a store or mark it ready."""

from app.store_guarded_kubernetes import GuardedKubernetesConnection
from app.store_job_guard import StoreJobClaimLost, require_claim
from app.store_namespace import namespace_for
from app.store_namespace_api import StoreNamespaceAPI
from app.store_namespace_binding import bind_namespace


def _api(session_factory, claim, connection, scope, uid, *, cleanup=False):
    guarded = GuardedKubernetesConnection(
        connection, session_factory, claim, scope,
        namespace_uid=uid, for_cleanup=cleanup,
    )
    return StoreNamespaceAPI(guarded, scope)


async def _save_uid(session_factory, claim, document, *, cleanup=False):
    uid = await bind_namespace(
        session_factory, claim, document, for_cleanup=cleanup,
    )
    if uid is None:
        raise StoreJobClaimLost(
            "The worker claim was lost before namespace ownership was saved.",
        )
    return uid


async def prepare_namespace(session_factory, claim, connection, scope):
    """Return the saved UID after verifying the fixed worker RoleBinding.

    The caller owns the shared connection and lease renewal. An interrupted
    attempt may leave resources behind; recovery must inspect them first.
    """
    checked = await require_claim(session_factory, claim)
    identity = namespace_for(checked.claim)
    uid = checked.namespace_uid
    api = _api(session_factory, checked.claim, connection, scope, uid)
    document = await api.ensure_namespace(identity, expected_uid=uid)

    # Commit the observed UID before allowing any RoleBinding request.
    uid = await _save_uid(session_factory, checked.claim, document)
    api = _api(session_factory, checked.claim, connection, scope, uid)
    await api.ensure_role_binding(identity, expected_uid=uid)
    await require_claim(
        session_factory, checked.claim, expected_namespace_uid=uid,
    )
    return uid


async def cleanup_namespace(session_factory, claim, connection, scope):
    """Perform one cleanup pass; True means observed namespace absence only.

    A terminating namespace returns False. The caller schedules another pass.
    Volume cleanup and the final store state must be verified separately.
    Cleanup still requires a live lease, even after the provisioning deadline.
    """
    checked = await require_claim(session_factory, claim, for_cleanup=True)
    identity = namespace_for(checked.claim)
    uid = checked.namespace_uid

    if uid is None:
        api = _api(
            session_factory, checked.claim, connection, scope, None,
            cleanup=True,
        )
        document = await api.read_namespace(identity, allow_terminating=True)
        if document is None:
            return True
        uid = await _save_uid(
            session_factory, checked.claim, document, cleanup=True,
        )

    api = _api(
        session_factory, checked.claim, connection, scope, uid,
        cleanup=True,
    )
    return await api.delete_namespace(identity, expected_uid=uid)
