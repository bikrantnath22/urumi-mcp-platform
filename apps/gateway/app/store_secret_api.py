"""Read or create an owned immutable Secret through a live job guard."""

from app.store_credentials import StoreCredentials
from app.store_guarded_kubernetes import StoreKubernetesStopped
from app.store_guarded_secrets import GuardedStoreSecretConnection
from app.store_kube_connection import KubernetesAPIError
from app.store_namespace import StoreNamespaceError
from app.store_namespace_api import StoreNamespaceAPI, StoreNamespacePending
from app.store_secret import STORE_SECRET_NAME, StoreSecretError


class StoreSecretPending(RuntimeError):
    """A conflicting Secret cannot yet be verified; no creation is retried."""


class StoreSecretAPI:
    def __init__(self, connection):
        if not isinstance(connection, GuardedStoreSecretConnection):
            raise ValueError("A claim-checked store Secret connection is required.")
        self._connection = connection
        self._definition = connection.definition
        self._expected_uid = connection.expected_uid
        self._namespaces = StoreNamespaceAPI(
            connection, self._definition.scope,
        )
        self._collection = (
            f"/api/v1/namespaces/{self._definition.identity.name}/secrets"
        )
        self._item = self._collection + "/" + STORE_SECRET_NAME

    async def _active_namespace(self):
        document = await self._namespaces.read_namespace(
            self._definition.identity,
            expected_uid=self._definition.namespace_uid,
        )
        if document is None:
            raise StoreNamespaceError("The saved namespace is missing.")
        if document.get("status", {}).get("phase") != "Active":
            raise StoreNamespacePending("The owned namespace is not active yet.")

    async def read_secret(self):
        """Return the verified response, or None for a missing unbound Secret."""
        await self._active_namespace()
        document = await self._connection.request("GET", self._item)
        if document is None:
            if self._expected_uid is not None:
                raise StoreSecretError(
                    "The saved store Secret is missing; it must not be recreated."
                )
        else:
            self._definition.check(document, expected_uid=self._expected_uid)
        await self._active_namespace()
        return document

    async def ensure_secret(self, *, admin_email):
        """Reuse credentials first. Create once only while no UID is saved."""
        document = await self.read_secret()
        if document is not None:
            return document
        if self._expected_uid is not None:
            raise StoreSecretError("A bound store Secret must not be recreated.")

        credentials = StoreCredentials.generate(
            self._definition.identity, admin_email=admin_email,
        )
        body = self._definition.manifest(credentials)
        await self._active_namespace()
        try:
            document = await self._connection.request(
                "POST", self._collection, body=body,
            )
        except KubernetesAPIError as error:
            if error.status_code != 409:
                raise
            document = await self.read_secret()
            if document is None:
                raise StoreSecretPending(
                    "The conflicting store Secret could not yet be verified."
                ) from None
            return document

        try:
            self._definition.check(document, expected_uid=self._expected_uid)
            await self._active_namespace()
        except StoreKubernetesStopped:
            raise StoreKubernetesStopped(write_may_have_applied=True) from None
        except (
            StoreSecretError, StoreNamespaceError,
            StoreNamespacePending, KubernetesAPIError,
        ):
            raise KubernetesAPIError(
                "The store Secret was created, but final verification did not finish.",
                write_may_have_applied=True,
            ) from None
        return document
