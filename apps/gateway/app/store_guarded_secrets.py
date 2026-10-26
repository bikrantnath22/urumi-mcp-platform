"""Private, claim-checked access to one store's credentials Secret."""

from copy import deepcopy

from app.store_credentials import StoreCredentialError, StoreCredentials
from app.store_guarded_kubernetes import GuardedKubernetesConnection
from app.store_job_guard import require_claim
from app.store_kube_connection import KubernetesAPIError, _check_request
from app.store_namespace import _opaque
from app.store_secret import STORE_SECRET_NAME, StoreSecret, StoreSecretError


class GuardedStoreSecretConnection(GuardedKubernetesConnection):
    """Reuse lease enforcement with only namespace reads and fixed Secret access."""

    def __init__(
        self, connection, session_factory, claim, scope, *,
        namespace_uid, credentials_secret_uid,
    ):
        if not _opaque(namespace_uid) or (
            credentials_secret_uid is not None
            and not _opaque(credentials_secret_uid)
        ):
            raise ValueError("Valid saved namespace and Secret identities are required.")
        super().__init__(
            connection, session_factory, claim, scope,
            namespace_uid=namespace_uid,
        )
        self._secret_uid = credentials_secret_uid
        self._secret = StoreSecret(self._identity, scope, namespace_uid)
        self._namespace_item = f"/api/v1/namespaces/{self._identity.name}"
        self._secret_collection = self._namespace_item + "/secrets"
        self._secret_item = self._secret_collection + "/" + STORE_SECRET_NAME

    @property
    def definition(self):
        """Immutable store, platform and namespace identity; no credentials."""
        return self._secret

    @property
    def expected_uid(self):
        """The saved Secret UID, or None while it is still unbound."""
        return self._secret_uid

    def _check_target(self, method, path, body):
        _check_request(method, path, body)
        if method == "GET" and path in (
            self._namespace_item, self._secret_item,
        ):
            return
        if (
            method == "POST"
            and path == self._secret_collection
            and self._secret_uid is None
        ):
            try:
                credentials = StoreCredentials.from_secret_data(
                    self._identity, body.get("data"),
                )
            except StoreCredentialError:
                raise StoreSecretError(
                    "The store Secret creation data is invalid."
                ) from None
            if body != self._secret.manifest(credentials):
                raise StoreSecretError(
                    "The store Secret creation ownership does not match."
                )
            return
        raise ValueError("This operation is outside the store Secret scope.")

    async def _checked(self):
        return await require_claim(
            self._factory, self._claim,
            expected_namespace_uid=self._uid,
            expected_credentials_secret_uid=self._secret_uid,
        )

    async def request(self, method, path, *, body=None):
        # Retain the validated request across awaits, isolated from its caller.
        body = deepcopy(body)
        result = await super().request(method, path, body=body)
        if path not in (self._secret_collection, self._secret_item):
            return result
        if result is None:
            if method == "GET" and self._secret_uid is None:
                return None
            if method == "GET":
                raise StoreSecretError(
                    "The saved store Secret is missing; it must not be recreated."
                )
            raise KubernetesAPIError(
                "The Secret creation response could not be verified.",
                write_may_have_applied=True,
            )
        try:
            self._secret.check(result, expected_uid=self._secret_uid)
            if method == "POST" and result.get("data") != body.get("data"):
                raise StoreSecretError(
                    "The created Secret credentials do not match the request."
                )
        except StoreSecretError:
            raise KubernetesAPIError(
                "The store Secret response could not be verified.",
                write_may_have_applied=method != "GET",
            ) from None
        return result
