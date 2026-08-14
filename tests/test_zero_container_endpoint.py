"""The /search pipeline with no Redis at all: memory twins carry the request.

Same Env pattern as test_search_endpoint, with get_redis overridden to None --
the signal the zero-container lifespan sends -- and the in-process cache and
limiter installed on app.state exactly where that lifespan puts them.
"""
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_provider_factory, get_redis
from app.cache import MemoryResponseCache
from app.config import ProviderName, Vertical
from app.db import ApiKey
from app.main import app
from app.providers.base import ProviderNotConfiguredError, SearchProvider
from app.ratelimit import MemoryRateLimiter
from app.schemas import SearchRequest

VALID_KEY = "test-key"

BLOCKS = {
    "organic": [{"title": "t", "link": "https://example.com", "position": 1}],
}


class FakeDB:
    def __init__(self, credits: int = 10000) -> None:
        self.credits = credits

    async def fetch_api_key(self, key: str) -> ApiKey | None:
        if key == VALID_KEY:
            return ApiKey(id=1, name="test", credits=self.credits)
        return None

    async def deduct_credits(self, key_id: int, cost: int) -> bool:
        self.credits -= cost
        return True

    async def log_usage(self, key_id: int, vertical: str, credits: int, provider: str,
                        cached: bool, latency_ms: int) -> None:
        return None


class FakeProvider(SearchProvider):
    name = ProviderName.GENXNG

    def __init__(self) -> None:
        self.calls = 0

    async def search(self, request: SearchRequest, vertical: Vertical) -> dict[str, Any]:
        self.calls += 1
        return dict(BLOCKS)


@dataclass
class Env:
    provider: FakeProvider = field(default_factory=FakeProvider)
    db: FakeDB = field(default_factory=FakeDB)

    def install(self) -> None:
        def factory(name: ProviderName) -> SearchProvider:
            if name is ProviderName.GENXNG:
                return self.provider
            raise ProviderNotConfiguredError(f"provider '{name}' is not implemented yet")

        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_redis] = lambda: None
        app.dependency_overrides[get_provider_factory] = lambda: factory
        app.state.cache = MemoryResponseCache()
        app.state.limiter = MemoryRateLimiter(qps=0, burst=1)


@pytest.fixture
def env() -> Iterator[Env]:
    environment = Env()
    environment.install()
    yield environment
    app.dependency_overrides.clear()
    # The app object is shared by every test module; state left behind here
    # would leak into tests that expect the Redis-mode shape.
    del app.state.cache
    del app.state.limiter


client = TestClient(app)


def test_search_answers_without_redis(env: Env) -> None:
    response = client.post("/search", json={"q": "hello"}, headers={"X-API-KEY": VALID_KEY})
    assert response.status_code == 200
    assert response.json()["organic"][0]["title"] == "t"


def test_second_ask_is_served_from_the_memory_cache(env: Env) -> None:
    for _ in range(2):
        response = client.post(
            "/search", json={"q": "cached?"}, headers={"X-API-KEY": VALID_KEY}
        )
        assert response.status_code == 200
    # One provider call, two answers: the second came from memory.
    assert env.provider.calls == 1


def test_memory_limiter_refuses_with_retry_after(env: Env) -> None:
    app.state.limiter = MemoryRateLimiter(qps=0.001, burst=1)
    first = client.post("/search", json={"q": "a"}, headers={"X-API-KEY": VALID_KEY})
    assert first.status_code == 200
    second = client.post("/search", json={"q": "b"}, headers={"X-API-KEY": VALID_KEY})
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) >= 1
