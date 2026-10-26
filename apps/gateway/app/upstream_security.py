"""Outbound MCP destination policy and DNS-pinned HTTP transport.

Only the operator's exact internal MCP endpoints may use private addresses.
External servers use HTTPS/443 and must resolve exclusively to public addresses.
No credentials are sent until validation succeeds. The transport connects to the
validated numeric address, preserving the original Host and TLS server name.
"""
import asyncio
from dataclasses import dataclass
import ipaddress
import json
import os
import re
import socket
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx


DEFAULT_INTERNAL_URLS = (
    "http://woo-mcp.store.svc.cluster.local:8000/mcp",
    "http://mcp-analytics.store.svc.cluster.local:8000/mcp",
    "http://mcp-notes.platform.svc.cluster.local:8000/mcp",
)
DEFAULT_INTERNAL_CIDRS = "10.42.0.0/16,10.43.0.0/16"
BLOCKED_IPV6_TRANSLATION = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)
BLOCKED_PLATFORM_ADDRESSES = frozenset(ipaddress.ip_address(value) for value in (
    "100.100.100.200", "168.63.129.16", "fd00:ec2::254",
))


class UpstreamPolicyError(ValueError):
    """A safe, credential-free explanation of a rejected destination."""


@dataclass(frozen=True)
class Endpoint:
    url: httpx.URL
    scheme: str
    host: str
    port: int
    path: bytes

    @property
    def identity(self):
        return self.scheme, self.host, self.port, self.path


def parse_endpoint(value: str) -> Endpoint:
    value = value.strip()
    try:
        parsed = urlsplit(value)
        if (
            not value or len(value) > 2048
            or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)
            or "\\" in value or "?" in value or "#" in value
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or "%" in parsed.hostname
        ):
            raise ValueError
        parsed.port  # Validate the port before HTTPX normalizes it.
        url = httpx.URL(value)
        host = url.raw_host.decode("ascii").lower().rstrip(".")
        if not host or url.userinfo or url.query or url.fragment:
            raise ValueError
        # A hostname must have ordinary DNS labels; numeric literals are checked
        # separately. This rejects legacy decimal/octal IPv4 spellings via DNS.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if len(host) > 253 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            ):
                raise ValueError
        port = url.port or (443 if url.scheme == "https" else 80)
        return Endpoint(url, url.scheme, host, port, url.raw_path)
    except (ValueError, UnicodeError, httpx.InvalidURL):
        raise UpstreamPolicyError(
            "Use an HTTP(S) MCP URL without credentials, query parameters, fragments or invalid characters."
        ) from None


def checked_address(value: str):
    try:
        if "%" in value:
            raise ValueError
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if (address in BLOCKED_PLATFORM_ADDRESSES
            or address.is_unspecified or address.is_link_local or address.is_multicast
            or address.is_reserved):
            raise ValueError
        if isinstance(address, ipaddress.IPv6Address) and (
            address.sixtofour is not None or address.teredo is not None
            or address.is_site_local
            or any(address in network for network in BLOCKED_IPV6_TRANSLATION)
        ):
            raise ValueError
        return address
    except ValueError:
        raise UpstreamPolicyError("Upstream destination is blocked by gateway policy.") from None


async def resolve_addresses(host: str, port: int) -> list[str]:
    try:
        async with asyncio.timeout(2):
            records = await asyncio.get_running_loop().getaddrinfo(
                host, port, family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
            )
    except (OSError, TimeoutError):
        raise UpstreamPolicyError("Upstream DNS resolution failed or timed out.") from None
    return list(dict.fromkeys(record[4][0] for record in records))


Resolver = Callable[[str, int], Awaitable[list[str]]]


class DestinationPolicy:
    def __init__(self, *, internal_urls=DEFAULT_INTERNAL_URLS,
                 internal_cidrs=DEFAULT_INTERNAL_CIDRS.split(",")):
        self.internal_endpoints = frozenset(parse_endpoint(url).identity for url in internal_urls)
        self.internal_networks = tuple(ipaddress.ip_network(value.strip()) for value in internal_cidrs if value.strip())
        if any(network.prefixlen == 0 for network in self.internal_networks):
            raise ValueError("Internal MCP networks must be explicit CIDRs, not a default route.")

    @classmethod
    def from_environment(cls):
        raw_urls = os.getenv("UPSTREAM_INTERNAL_MCP_URLS")
        urls = json.loads(raw_urls) if raw_urls is not None else DEFAULT_INTERNAL_URLS
        if not isinstance(urls, list | tuple) or not all(isinstance(url, str) for url in urls):
            raise ValueError("UPSTREAM_INTERNAL_MCP_URLS must be a JSON array of exact MCP URLs.")
        return cls(internal_urls=urls, internal_cidrs=os.getenv(
            "UPSTREAM_INTERNAL_MCP_CIDRS", DEFAULT_INTERNAL_CIDRS,
        ).split(","))

    def validate_address(self, endpoint: Endpoint, value: str) -> str:
        address = checked_address(value)
        if endpoint.identity in self.internal_endpoints:
            allowed = any(address.version == network.version and address in network
                          for network in self.internal_networks)
        else:
            allowed = address.is_global and not address.is_loopback
        if not allowed:
            raise UpstreamPolicyError("Upstream destination is blocked by gateway policy.")
        return str(address)

    def validate_url(self, value: str) -> Endpoint:
        endpoint = parse_endpoint(value)
        if endpoint.identity not in self.internal_endpoints:
            if endpoint.scheme != "https" or endpoint.port != 443:
                raise UpstreamPolicyError(
                    "External MCP servers must use HTTPS on port 443. Internal endpoints require operator configuration."
                )
            host = endpoint.host
            if ("." not in host and ":" not in host) or host in {"localhost", "localhost.localdomain"} or host.endswith(
                (".localhost", ".localdomain", ".local", ".internal", ".svc", ".cluster.local")
            ):
                raise UpstreamPolicyError("Upstream destination is blocked by gateway policy.")
        try:
            literal = ipaddress.ip_address(endpoint.host)
        except ValueError:
            pass
        else:
            self.validate_address(endpoint, str(literal))
        return endpoint

    async def prepare(self, value: str, *, resolver: Resolver | None = None):
        endpoint = self.validate_url(value)
        try:
            literal = ipaddress.ip_address(endpoint.host)
        except ValueError:
            records = await (resolver or resolve_addresses)(endpoint.host, endpoint.port)
        else:
            records = [str(literal)]
        if not records:
            raise UpstreamPolicyError("Upstream DNS returned no usable addresses.")
        # Reject the entire answer set if even one A/AAAA address is disallowed.
        addresses = tuple(dict.fromkeys(self.validate_address(endpoint, address) for address in records))
        addresses = tuple(sorted(addresses, key=lambda value: ipaddress.ip_address(value).version))
        return PreparedDestination(endpoint, addresses)


@dataclass(frozen=True)
class PreparedDestination:
    endpoint: Endpoint
    addresses: tuple[str, ...]


class PinnedUpstreamTransport(httpx.AsyncBaseTransport):
    def __init__(self, destination: PreparedDestination, *, transport=None):
        self.destination = destination
        self.transport = transport if transport is not None else httpx.AsyncHTTPTransport(
            trust_env=False, retries=0,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        endpoint = self.destination.endpoint
        if parse_endpoint(str(request.url)).identity != endpoint.identity:
            raise UpstreamPolicyError("Upstream attempted to leave its configured MCP endpoint.")
        if request.method not in {"POST", "GET", "DELETE"}:
            raise UpstreamPolicyError("Upstream request method is blocked by gateway policy.")
        headers = request.headers.copy()
        headers["Host"] = endpoint.url.netloc.decode("ascii")
        extensions = dict(request.extensions)
        extensions.pop("target", None)
        extensions["sni_hostname"] = endpoint.host
        # A connect failure occurs before an HTTP body is sent. Never retry
        # read/write/protocol failures, which could repeat a modifying tool.
        for index, address in enumerate(self.destination.addresses):
            pinned = httpx.Request(
                request.method, request.url.copy_with(host=address),
                headers=headers, stream=request.stream, extensions=extensions,
            )
            try:
                response = await self.transport.handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if index + 1 == len(self.destination.addresses):
                    raise
                continue
            if 300 <= response.status_code < 400:
                await response.aclose()
                raise UpstreamPolicyError("Upstream redirects are blocked; configure the final MCP endpoint URL.")
            return response
        raise UpstreamPolicyError("Upstream DNS returned no usable addresses.")

    async def aclose(self):
        await self.transport.aclose()


destination_policy = DestinationPolicy.from_environment()


def policy_failure(error: BaseException) -> str | None:
    """The MCP task group may wrap a transport rejection in an ExceptionGroup."""
    pending, seen = [error], set()
    while pending and len(seen) < 32:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, UpstreamPolicyError):
            return str(current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return None
