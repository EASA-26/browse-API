"""The zero-container twins: same contracts as the Redis-backed originals."""
from app.cache import MemoryResponseCache, build_cache_key
from app.config import Vertical
from app.ratelimit import MemoryRateLimiter
from app.schemas import SearchRequest


async def test_memory_cache_round_trip_and_miss() -> None:
    cache = MemoryResponseCache()
    key = build_cache_key(Vertical.SEARCH, SearchRequest(q="x"))
    assert await cache.get(key) is None
    value = {"blocks": {"organic": [{"title": "t", "link": "https://x", "position": 1}]}}
    await cache.set(key, value, ttl=60)
    assert await cache.get(key) == value


async def test_memory_cache_never_aliases_what_it_returns() -> None:
    """The endpoint mutates responses; a shared reference would poison the cache."""
    cache = MemoryResponseCache()
    await cache.set("k", {"blocks": {"searchMeta": {"cached": False}}}, ttl=60)
    first = await cache.get("k")
    assert first is not None
    first["blocks"]["searchMeta"]["cached"] = True
    second = await cache.get("k")
    assert second is not None
    assert second["blocks"]["searchMeta"]["cached"] is False


async def test_memory_cache_expires() -> None:
    cache = MemoryResponseCache()
    await cache.set("k", {"blocks": {}}, ttl=0)
    assert await cache.get("k") is None


async def test_memory_cache_bounds_its_size() -> None:
    cache = MemoryResponseCache(max_entries=2)
    await cache.set("a", {"blocks": {}}, ttl=10)
    await cache.set("b", {"blocks": {}}, ttl=20)
    await cache.set("c", {"blocks": {}}, ttl=30)
    held = [k for k in ("a", "b", "c") if await cache.get(k) is not None]
    assert len(held) == 2
    assert "a" not in held  # soonest expiry went first


async def test_memory_limiter_allows_burst_then_refuses() -> None:
    limiter = MemoryRateLimiter(qps=1, burst=2)
    now = 1_000_000
    assert await limiter.check(1, now_ms=now) == (True, 0)
    assert await limiter.check(1, now_ms=now) == (True, 0)
    allowed, retry_after = await limiter.check(1, now_ms=now)
    assert allowed is False
    assert retry_after >= 1


async def test_memory_limiter_refills_over_time() -> None:
    limiter = MemoryRateLimiter(qps=1, burst=1)
    now = 1_000_000
    assert (await limiter.check(1, now_ms=now))[0] is True
    assert (await limiter.check(1, now_ms=now))[0] is False
    # One second later one token has returned.
    assert (await limiter.check(1, now_ms=now + 1000))[0] is True


async def test_memory_limiter_keys_are_independent() -> None:
    limiter = MemoryRateLimiter(qps=1, burst=1)
    now = 1_000_000
    assert (await limiter.check(1, now_ms=now))[0] is True
    assert (await limiter.check(2, now_ms=now))[0] is True


async def test_memory_limiter_disabled_when_qps_zero() -> None:
    limiter = MemoryRateLimiter(qps=0, burst=1)
    for _ in range(10):
        assert await limiter.check(1) == (True, 0)


async def test_memory_limiter_atomic_under_concurrent_tasks() -> None:
    """A thousand concurrent checks against a 100-token burst: exactly 100 pass.

    There is no await between the bucket read and write, so the event loop is
    the atomicity -- this pins that property against future refactors.
    """
    import asyncio

    limiter = MemoryRateLimiter(qps=0.000001, burst=100)
    now = 1_000_000
    results = await asyncio.gather(*(limiter.check(1, now_ms=now) for _ in range(1000)))
    assert sum(1 for allowed, _ in results if allowed) == 100
