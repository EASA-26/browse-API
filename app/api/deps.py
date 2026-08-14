"""FastAPI dependencies. Tests override these to run without infrastructure."""
from collections.abc import Callable

import redis.asyncio as aioredis
from fastapi import Request

from app.config import ProviderName
from app.db import DatabaseBackend
from app.policy import PolicyGate, get_policy_gate
from app.providers import SearchProvider, get_provider

ProviderFactory = Callable[[ProviderName], SearchProvider]


def get_db(request: Request) -> DatabaseBackend:
    db: DatabaseBackend = request.app.state.db
    return db


def get_redis(request: Request) -> aioredis.Redis | None:
    """The Redis client, or None in the zero-container mode.

    None is a signal, not a failure: the pipeline then uses the in-process
    cache and limiter the lifespan created in its place.
    """
    client: aioredis.Redis | None = request.app.state.redis
    return client


def get_provider_factory() -> ProviderFactory:
    return get_provider


def get_policy() -> PolicyGate:
    return get_policy_gate()
