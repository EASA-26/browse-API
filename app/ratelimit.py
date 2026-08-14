"""Per-key token-bucket rate limiter.

Redis-backed and atomic via a Lua script for the compose deployment; an
in-process twin for the zero-container mode. LimiterBackend is the contract.
"""
import math
import time
from typing import Protocol

import redis.asyncio as aioredis

# KEYS[1] = bucket key; ARGV = rate (tokens/s), burst, now (ms).
# Returns {allowed, retry_after_seconds}.
TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then tokens = burst end
if ts == nil then ts = now end
tokens = math.min(burst, tokens + (now - ts) / 1000.0 * rate)
local allowed = 0
local retry = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
else
    retry = math.ceil((1 - tokens) / rate)
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, math.ceil(burst / rate) + 60)
return {allowed, retry}
"""


class LimiterBackend(Protocol):
    async def check(self, key_id: int, now_ms: int | None = None) -> tuple[bool, int]: ...


class RateLimiter:
    def __init__(self, client: aioredis.Redis, qps: float, burst: int) -> None:
        self._client = client
        self._qps = qps
        self._burst = max(burst, 1)

    async def check(self, key_id: int, now_ms: int | None = None) -> tuple[bool, int]:
        """Consume one token. Returns (allowed, retry_after_seconds)."""
        if self._qps <= 0:
            return True, 0
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        allowed, retry = await self._client.eval(
            TOKEN_BUCKET_LUA, 1, f"ratelimit:{key_id}", self._qps, self._burst, now_ms
        )
        return bool(allowed), max(int(retry), 1) if not allowed else 0


def retry_after_ceiling(qps: float) -> int:
    """Smallest sensible Retry-After for a given refill rate."""
    return max(1, math.ceil(1 / qps)) if qps > 0 else 1


class MemoryRateLimiter:
    """The same token bucket, in process memory for the zero-container mode.

    The Lua script exists to make read-modify-write atomic across workers;
    inside one asyncio event loop the whole calculation is synchronous, so a
    plain dict is already atomic. Single worker process only -- the Redis
    limiter remains the one that is safe across several.
    """

    def __init__(self, qps: float, burst: int) -> None:
        self._qps = qps
        self._burst = max(burst, 1)
        self._buckets: dict[int, tuple[float, int]] = {}

    async def check(self, key_id: int, now_ms: int | None = None) -> tuple[bool, int]:
        """Consume one token. Returns (allowed, retry_after_seconds)."""
        if self._qps <= 0:
            return True, 0
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        tokens, ts = self._buckets.get(key_id, (float(self._burst), now_ms))
        tokens = min(float(self._burst), tokens + (now_ms - ts) / 1000.0 * self._qps)
        if tokens >= 1:
            self._buckets[key_id] = (tokens - 1, now_ms)
            return True, 0
        self._buckets[key_id] = (tokens, now_ms)
        return False, max(math.ceil((1 - tokens) / self._qps), 1)
