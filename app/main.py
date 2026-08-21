"""gen-api: self-hosted Gen search API.

search/images/news/videos/autocomplete are served by GenXNG (the on-prem
metasearch backend); remaining verticals answer 501 until the commercial
provider is configured.
"""
import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.metrics import router as metrics_router
from app.api.verticals import router as verticals_router
from app.cache import MemoryResponseCache
from app.config import get_settings
from app.db import create_database
from app.engine_health import get_engine_monitor, probe_loop
from app.logging_config import setup_logging
from app.policy import PolicyBlockedError
from app.ratelimit import MemoryRateLimiter
from app.tls import trust_system_certificates

setup_logging(get_settings().log_level)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # Before any provider builds a client. Done here rather than at import so
    # the test suite, which never opens a real socket, is unaffected.
    if settings.system_trust_store:
        trust_system_certificates()
    db = create_database(settings.database_url)
    await db.connect()
    app.state.db = db
    if settings.redis_disabled:
        # Zero-container mode: no Redis process exists. The cache and limiter
        # live here for the lifetime of the app, exactly as the Redis client
        # otherwise would -- per-request state has to outlive requests.
        logger.info(
            "zero-container mode: in-process cache and rate limiter "
            "(single worker only)"
        )
        app.state.redis = None
        app.state.cache = MemoryResponseCache()
        app.state.limiter = MemoryRateLimiter(
            settings.rate_limit_qps, settings.rate_limit_burst
        )
    else:
        app.state.redis = aioredis.from_url(settings.redis_url)
    probe_task: asyncio.Task[None] | None = None
    if settings.engine_probe_interval > 0:
        probe_task = asyncio.create_task(
            probe_loop(get_engine_monitor(settings), settings.engine_probe_interval)
        )
    yield
    if probe_task is not None:
        probe_task.cancel()
        with suppress(asyncio.CancelledError):
            await probe_task
    if app.state.redis is not None:
        await app.state.redis.aclose()
    await db.close()


app = FastAPI(
    title="gen-api",
    version="0.1.0",
    description=(
        "Self-hosted Gen web-search API with a pluggable acquisition "
        "layer (GenXNG / commercial search API / direct scrape)."
    ),
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(metrics_router)
app.include_router(verticals_router)


@app.exception_handler(PolicyBlockedError)
async def policy_blocked_handler(request: Request, exc: PolicyBlockedError) -> JSONResponse:
    """Sensitive queries fail closed: 403 with a machine-readable reason code."""
    return JSONResponse(
        status_code=403,
        content={
            "detail": "query blocked by policy",
            "policyBlocked": True,
            "category": exc.category,
        },
    )


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    settings = get_settings()
    return {"service": "gen-api", "env": settings.app_env, "docs": "/docs"}
