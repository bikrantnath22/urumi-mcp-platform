"""Private temporary Helm files; projected credentials remain in their mount."""

from contextlib import contextmanager
import json
import os
from tempfile import TemporaryDirectory

from app.store_helm_config import StoreHelmConfig
from app.store_namespace import StoreNamespace


class HelmWorkspaceError(RuntimeError):
    """The worker cannot prepare or remove its private local files."""


def _prepare(config):
    root = config.workspace
    os.chmod(root, 0o700)
    for name in ("tmp", "cache", "config", "data", "plugins"):
        path = root + "/" + name
        os.mkdir(path, 0o700)
        os.chmod(path, 0o700)

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(config.kubeconfig_path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            descriptor = None
            os.fchmod(handle.fileno(), 0o600)
            json.dump(config.kubeconfig(), handle, ensure_ascii=True)
            handle.write("\n")
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def helm_workspace(identity):
    """Yield one prepared config; remove its files when the caller exits.

    The caller must await all Helm subprocess cleanup before leaving this
    context, including during cancellation. This helper starts no process,
    authorizes no job and never reads or copies the projected token.
    """
    if not isinstance(identity, StoreNamespace):
        raise ValueError("A managed store identity is required.")
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("Helm workspaces require Linux/POSIX.")

    try:
        temporary = TemporaryDirectory(prefix="urumi-helm-", dir="/tmp")
    except OSError:
        raise HelmWorkspaceError("Private Helm storage is unavailable.") from None

    try:
        try:
            config = StoreHelmConfig(temporary.name, identity)
            _prepare(config)
        except (OSError, TypeError, ValueError):
            raise HelmWorkspaceError(
                "The private Helm workspace could not be prepared.",
            ) from None
        yield config
    finally:
        try:
            temporary.cleanup()
        except OSError:
            raise HelmWorkspaceError(
                "Private Helm workspace cleanup could not be confirmed.",
            ) from None
