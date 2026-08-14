"""Response cache. Key = hash of the canonical request parameters.

Two backends behind one contract: Redis for the compose deployment, an
in-process store for the zero-container mode. CacheBackend is the contract.
"""
import hashlib
import json
import time
from typing import Any, Protocol

import redis.asyncio as aioredis

from app.config import Vertical
from app.schemas import SearchRequest


class CacheBackend(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def set(self, key: str, value: dict[str, Any], ttl: int) -> None: ...


def build_cache_key(vertical: Vertical, request: SearchRequest) -> str:
    canonical = json.dumps(
        {
            "vertical": vertical.value,
            "q": request.q,
            "gl": request.gl,
            "hl": request.hl,
            "location": request.location,
            "num": request.num,
            "page": request.page,
            "tbs": request.tbs,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"cache:{vertical.value}:{digest}"


class ResponseCache:
    def __init__(self, client: aioredis.Redis) -> None:
        self._client = client

    async def get(self, key: str) -> dict[str, Any] | None:
        payload = await self._client.get(key)
        if payload is None:
            return None
        loaded: dict[str, Any] = json.loads(payload)
        return loaded

    async def set(self, key: str, value: dict[str, Any], ttl: int) -> None:
        await self._client.set(key, json.dumps(value), ex=ttl)


class MemoryResponseCache:
    """In-process cache for the zero-container mode.

    Values round-trip through JSON exactly as they do in Redis, so cached
    entries can never alias live response dicts -- the endpoint mutates what
    it gets back, and a shared reference would poison the cache. Correct for
    a single worker process, which is what a Scheduled-Task uvicorn is.
    """

    def __init__(self, max_entries: int = 512, max_entry_bytes: int = 128 * 1024) -> None:
        self._store: dict[str, tuple[float, str]] = {}
        self._max = max_entries
        self._max_entry_bytes = max_entry_bytes

    async def get(self, key: str) -> dict[str, Any] | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if time.monotonic() >= expires_at:
            del self._store[key]
            return None
        loaded: dict[str, Any] = json.loads(payload)
        return loaded

    async def set(self, key: str, value: dict[str, Any], ttl: int) -> None:
        payload = json.dumps(value)
        if len(payload) > self._max_entry_bytes:
            # Skipping is correct best-effort caching: the next ask is a miss,
            # and the worst case stays bounded at max_entries * max_entry_bytes.
            return
        if len(self._store) >= self._max and key not in self._store:
            # Evict whatever expires soonest; with a cap this small the point
            # is bounding memory, not perfect LRU.
            oldest = min(self._store, key=lambda k: self._store[k][0])
            del self._store[oldest]
        self._store[key] = (time.monotonic() + ttl, payload)
