"""Bounded per-process cache of MCP tool definitions, never tool results."""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import os
from time import monotonic
from typing import Awaitable, Callable

from mcp.types import Tool


def catalog_identity(server) -> str:
    values = [server.id, server.slug, server.name, server.url, server.enabled,
              server.timeout_s, server.auth_header_name, server.credential_encrypted]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class CatalogLookup:
    tools: list[Tool]
    state: str


@dataclass
class _Entry:
    identity: str
    tools: list[Tool]
    expires_at: float
    size: int


class ToolCatalogCache:
    def __init__(self, *, ttl_seconds=60.0, server_slugs=("woo", "analytics", "notes"),
                 max_entries=32, max_bytes=2 * 1024 * 1024, clock=monotonic):
        if not 0 <= ttl_seconds <= 300:
            raise ValueError("Tool catalogue cache TTL must be between 0 and 300 seconds.")
        if not 1 <= max_entries <= 128 or not 1024 <= max_bytes <= 16 * 1024 * 1024:
            raise ValueError("Tool catalogue cache capacity is outside its supported range.")
        self.ttl_seconds = ttl_seconds
        self.server_slugs = frozenset(server_slugs)
        self.max_entries, self.max_bytes, self.clock = max_entries, max_bytes, clock
        self._entries = OrderedDict()
        self._bytes = 0
        self._flights = {}
        self._tasks = set()

    @staticmethod
    def _copy(tools):
        return [tool.model_copy(deep=True) for tool in tools]

    def _remove(self, server_id):
        entry = self._entries.pop(server_id, None)
        if entry is not None:
            self._bytes -= entry.size

    def invalidate(self, server_id):
        self._remove(server_id)
        # A detached read may finish, but cannot repopulate this cache.
        for key in list(self._flights):
            if key[0] == server_id:
                self._flights.pop(key)

    def _prune(self):
        now = self.clock()
        for server_id, entry in list(self._entries.items()):
            if now >= entry.expires_at:
                self._remove(server_id)

    def _store(self, server_id, identity, tools):
        self._prune()
        size = len(json.dumps([tool.model_dump(mode="json", by_alias=True)
                               for tool in tools], separators=(",", ":")).encode())
        self._remove(server_id)
        if size > self.max_bytes:
            return
        while self._entries and (len(self._entries) >= self.max_entries
                                 or self._bytes + size > self.max_bytes):
            self._remove(next(iter(self._entries)))
        self._entries[server_id] = _Entry(identity, self._copy(tools),
                                         self.clock() + self.ttl_seconds, size)
        self._bytes += size

    def _finished(self, task):
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def get(self, server, loader: Callable[[], Awaitable[list[Tool]]], *, refresh=False):
        if refresh:
            self.invalidate(server.id)
        if not self.ttl_seconds or server.slug not in self.server_slugs or not server.enabled:
            return CatalogLookup(self._copy(await loader()), "bypass")
        self._prune()
        identity = catalog_identity(server)
        entry = self._entries.get(server.id)
        if entry is not None and entry.identity == identity:
            self._entries.move_to_end(server.id)
            return CatalogLookup(self._copy(entry.tools), "hit")
        if entry is not None:
            self.invalidate(server.id)
        key = (server.id, identity)
        task = self._flights.get(key)
        if task is not None:
            return CatalogLookup(self._copy(await asyncio.shield(task)), "shared")
        if len(self._tasks) >= self.max_entries:
            return CatalogLookup(self._copy(await loader()), "bypass")

        async def load():
            try:
                tools = await loader()
                if self._flights.get(key) is asyncio.current_task():
                    self._store(server.id, identity, tools)
                return tools
            finally:
                if self._flights.get(key) is asyncio.current_task():
                    self._flights.pop(key)

        task = asyncio.create_task(load())
        self._tasks.add(task)
        self._flights[key] = task
        task.add_done_callback(self._finished)
        return CatalogLookup(self._copy(await asyncio.shield(task)), "miss")

    async def close(self):
        tasks = list(self._tasks)
        self._flights.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._entries.clear()
        self._bytes = 0


catalog_cache = ToolCatalogCache(
    ttl_seconds=float(os.getenv("TOOL_CATALOG_CACHE_TTL_SECONDS", "60")),
    server_slugs=tuple(value.strip() for value in
                       os.getenv("TOOL_CATALOG_CACHE_SERVERS", "woo,analytics,notes").split(",")
                       if value.strip()),
    max_entries=int(os.getenv("TOOL_CATALOG_CACHE_MAX_ENTRIES", "32")),
    max_bytes=int(os.getenv("TOOL_CATALOG_CACHE_MAX_BYTES", str(2 * 1024 * 1024))),
)
