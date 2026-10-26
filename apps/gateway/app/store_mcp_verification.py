"""Private discovery checks for a provisioned store's two disabled MCP entries."""

import asyncio

from app.store_guarded_kubernetes import StoreKubernetesStopped
from app.store_guarded_secrets import GuardedStoreSecretConnection
from app.store_job_guard import (
    StoreJobClaimLost, StoreJobGuardUnavailable, require_claim,
)
from app.store_registration import StoreMCPRegistrations, definitions_for
from app.store_secret_api import StoreSecretAPI
from app.upstreams import discover_tools


_REQUIRED = {
    "woo-mcp": frozenset(("list_products", "create_product", "list_orders", "get_order")),
    "mcp-analytics": frozenset(("sales_summary", "order_counts", "top_products")),
}
_CHECK_TIMEOUT_SECONDS = 30.0


class StoreMCPVerificationError(RuntimeError):
    """Discovery failed; internal diagnostics and tool descriptions stay private."""


def _invalid():
    raise StoreMCPVerificationError("Store MCP discovery could not be verified.")


async def verify_store_mcps(
    session_factory, claim, connection, scope, registrations, *,
    namespace_uid, credentials_secret_uid,
):
    """Return service/tool counts after guarded discovery; do not mark Ready.

    Register both draft entries first. The caller owns the connection and lease
    renewal. No tools are executed and no member permissions are granted.
    Storefront/backend checks and final completion remain separate.
    """
    if not isinstance(registrations, StoreMCPRegistrations):
        _invalid()
    ids = (registrations.woo_server_id, registrations.analytics_server_id)
    if any(type(value) is not int or value < 1 for value in ids) or ids[0] == ids[1]:
        _invalid()
    definitions = definitions_for(claim)
    for definition, slug, url in zip(
        definitions,
        (registrations.woo_slug, registrations.analytics_slug),
        (registrations.woo_url, registrations.analytics_url),
    ):
        if definition.slug != slug or definition.url != url:
            _invalid()

    guarded = GuardedStoreSecretConnection(
        connection, session_factory, claim, scope,
        namespace_uid=namespace_uid, credentials_secret_uid=credentials_secret_uid,
    )
    ownership = StoreSecretAPI(guarded)

    async def checked():
        return await require_claim(
            session_factory, claim,
            expected_namespace_uid=namespace_uid,
            expected_credentials_secret_uid=credentials_secret_uid,
        )

    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_SECONDS):
            observation = await checked()
            remaining = observation.remaining_seconds
            if remaining <= 0:
                raise StoreJobClaimLost("The MCP observation allowance elapsed.")
            async with asyncio.timeout(min(_CHECK_TIMEOUT_SECONDS, remaining)):
                await ownership.read_secret()
                result = {}
                for definition, server_id in zip(definitions, ids):
                    current = await checked()
                    allowance = min(15.0, current.remaining_seconds)
                    if allowance < 1:
                        raise StoreJobClaimLost("The MCP observation allowance elapsed.")
                    tools = await discover_tools(
                        definition.url, timeout_s=allowance,
                        server_id=server_id, store_claim=current.claim,
                    )
                    await checked()
                    if type(tools) is not list or not 1 <= len(tools) <= 1000:
                        _invalid()
                    names = [getattr(tool, "name", None) for tool in tools]
                    if (
                        any(type(name) is not str for name in names)
                        or len(set(names)) != len(names)
                        or not _REQUIRED[definition.service].issubset(names)
                    ):
                        _invalid()
                    result[definition.service] = len(names)
                await ownership.read_secret()
                await checked()
                if observation.remaining_seconds <= 0:
                    raise StoreJobClaimLost("The MCP observation allowance elapsed.")
                return result
    except (StoreJobClaimLost, StoreJobGuardUnavailable):
        raise StoreKubernetesStopped(write_may_have_applied=False) from None
    except Exception:
        raise StoreMCPVerificationError(
            "Store MCP discovery could not be verified.",
        ) from None
