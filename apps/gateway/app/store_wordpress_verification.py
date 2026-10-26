"""Private read-only WordPress probes for one provisioned managed store."""

import asyncio
from contextlib import aclosing
import json

import httpx

from app.store_guarded_kubernetes import StoreKubernetesStopped
from app.store_guarded_secrets import GuardedStoreSecretConnection
from app.store_helm_plan import plan_store_installation
from app.store_job_guard import (
    StoreJobClaimLost, StoreJobGuardUnavailable, require_claim,
)
from app.store_namespace import _opaque
from app.store_secret_api import StoreSecretAPI
from app.upstream_security import (
    DestinationPolicy, PinnedUpstreamTransport, PreparedDestination,
    destination_policy, parse_endpoint,
)


_CURRENCY_PATH = "/wp-json/wc/v3/settings/general/woocommerce_currency"
_MEDIA_PATH = "/wp-json/wp/v2/users/me"
_CHECK_TIMEOUT_SECONDS = 30.0


class StoreWordPressVerificationError(RuntimeError):
    """Probe failure; credentials and upstream diagnostics stay private."""


def _invalid():
    raise StoreWordPressVerificationError(
        "Storefront or store credentials could not be verified.",
    )


def _object(payload):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _invalid()
            result[key] = value
        return result

    def constant(value):
        _invalid()

    result = json.loads(
        payload, object_pairs_hook=pairs, parse_constant=constant,
    )
    if type(result) is not dict:
        _invalid()
    return result


async def _get(destination, *, auth, protocol, html=False):
    headers = {
        "X-Forwarded-Proto": protocol,
        "Accept-Encoding": "identity",
        "Accept": "text/html" if html else "application/json",
    }
    limit = 1024 * 1024 if html else 64 * 1024
    transport = PinnedUpstreamTransport(destination)
    async with httpx.AsyncClient(
        transport=transport, auth=auth, headers=headers, timeout=10.0,
        trust_env=False, follow_redirects=False,
    ) as client:
        async with client.stream("GET", str(destination.endpoint.url)) as response:
            kind = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if (
                response.status_code != 200
                or kind != ("text/html" if html else "application/json")
                or response.headers.get("content-encoding", "identity") != "identity"
            ):
                _invalid()
            payload = bytearray()
            async with aclosing(response.aiter_raw(chunk_size=16384)) as chunks:
                async for chunk in chunks:
                    if len(payload) + len(chunk) > limit:
                        _invalid()
                    payload.extend(chunk)
    return bytes(payload)

async def verify_store_wordpress(
    session_factory, claim, connection, scope, *,
    namespace_uid, credentials_secret_uid,
):
    """Return four flags, without data writes, retries or Ready completion.

    The caller renews the lease. This probes the internal WordPress Service with
    its public Host header; a live public Ingress check remains separate.
    Reading with the RW key confirms authentication, not write permission.
    """
    if not _opaque(namespace_uid) or not _opaque(credentials_secret_uid):
        raise ValueError("Saved namespace and Secret UIDs are required.")
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
            plan = plan_store_installation(observation, scope)
            async with asyncio.timeout(min(
                _CHECK_TIMEOUT_SECONDS, observation.remaining_seconds,
            )):
                document = await ownership.read_secret()
                credentials = guarded.definition.check(
                    document, expected_uid=credentials_secret_uid,
                ).credentials
                values = credentials._values
                service = (
                    f"http://wordpress.{plan.identity.name}.svc.cluster.local/"
                )
                policy = DestinationPolicy(
                    internal_urls=(service,),
                    internal_cidrs=tuple(
                        str(network) for network in destination_policy.internal_networks
                    ),
                )
                await checked()
                resolved = await policy.prepare(service)

                async def probe(path, auth=None):
                    # Recheck actual resources after DNS and before sending credentials.
                    await ownership.read_secret()
                    await checked()
                    # Only the owned Service is resolved. Pin its validated IPs while
                    # sending WordPress's canonical public Host; never resolve that host.
                    target = PreparedDestination(
                        parse_endpoint(f"http://{plan.host}{path}"),
                        resolved.addresses,
                    )
                    payload = await _get(
                        target, auth=auth,
                        protocol=plan.scheme if path == "/" else "https",
                        html=path == "/",
                    )
                    await checked()
                    return payload

                html = await probe("/")
                if b"<html" not in html[:4096].lower():
                    _invalid()
                currencies = []
                for prefix in ("WC_RW", "WC_RO"):
                    item = _object(await probe(
                        _CURRENCY_PATH,
                        (values[prefix + "_KEY"], values[prefix + "_SECRET"]),
                    ))
                    currency = item.get("value")
                    if (
                        item.get("id") != "woocommerce_currency"
                        or type(currency) is not str
                        or not 1 <= len(currency) <= 16
                        or currency != currency.strip()
                    ):
                        _invalid()
                    currencies.append(currency)
                if currencies[0] != currencies[1]:
                    _invalid()
                media = _object(await probe(
                    _MEDIA_PATH,
                    (values["WP_MEDIA_USER"], values["WP_MEDIA_APP_PASSWORD"]),
                ))
                if (
                    type(media.get("id")) is not int or media["id"] < 1
                    or media.get("slug") != values["WP_MEDIA_USER"]
                ):
                    _invalid()
                await ownership.read_secret()
                await checked()
                if observation.remaining_seconds <= 0:
                    raise StoreJobClaimLost("The WordPress observation allowance elapsed.")
                return {
                    "storefront": True, "woo_rw_key_read": True,
                    "woo_ro_key_read": True, "media_login": True,
                }
    except (StoreJobClaimLost, StoreJobGuardUnavailable):
        raise StoreKubernetesStopped(write_may_have_applied=False) from None
    except Exception:
        raise StoreWordPressVerificationError(
            "Storefront or store credentials could not be verified.",
        ) from None
