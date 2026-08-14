"""Liveness and dependency health checks."""
import asyncio
import sqlite3
from typing import Literal
from urllib.parse import urlparse

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter
from pydantic import BaseModel

from app.config import get_settings
from app.db_sqlite import sqlite_path_from_dsn

router = APIRouter(tags=["health"])

# "disabled" is the zero-container mode saying a dependency is absent on
# purpose -- absent is healthy there, and reporting "error" forever would
# train people to ignore this endpoint.
DepStatus = Literal["ok", "error", "disabled"]


class HealthResponse(BaseModel):
    status: Literal["ok"]


class DepsResponse(BaseModel):
    status: Literal["ok", "degraded"]
    redis: DepStatus
    # Reports the SQL datastore, whichever backend serves it; the field keeps
    # its historical name so existing monitors do not need migrating.
    postgres: DepStatus
    genxng: DepStatus


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Liveness probe: the process is up and serving."""
    return HealthResponse(status="ok")


async def _check_redis(url: str) -> DepStatus:
    client: aioredis.Redis = aioredis.from_url(url, socket_connect_timeout=3)
    try:
        await client.ping()
        return "ok"
    except Exception:
        return "error"
    finally:
        await client.aclose()


async def _check_tcp(host: str, port: int) -> DepStatus:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=3)
        writer.close()
        await writer.wait_closed()
        return "ok"
    except Exception:
        return "error"


async def _check_searxng(base_url: str) -> DepStatus:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{base_url.rstrip('/')}/healthz")
        return "ok" if response.status_code == 200 else "error"
    except Exception:
        return "error"


def _check_sqlite_sync(dsn: str) -> DepStatus:
    try:
        # Read-only via the URI form: a probe must never create the very file
        # it claims to be checking, and mode=ro errors on an absent database
        # instead of conjuring an empty one and calling it healthy.
        conn = sqlite3.connect(f"file:{sqlite_path_from_dsn(dsn)}?mode=ro", uri=True, timeout=3)
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
        return "ok"
    except Exception:
        return "error"


async def _check_database(dsn: str) -> DepStatus:
    if dsn.startswith("sqlite:"):
        return await asyncio.to_thread(_check_sqlite_sync, dsn)
    parsed = urlparse(dsn)
    return await _check_tcp(parsed.hostname or "postgres", parsed.port or 5432)


@router.get("/health/deps", response_model=DepsResponse)
async def health_deps() -> DepsResponse:
    """Readiness probe: checks redis, the SQL datastore and genxng."""
    settings = get_settings()
    if settings.redis_disabled:
        redis_status: DepStatus = "disabled"
        postgres_status, genxng_status = await asyncio.gather(
            _check_database(settings.database_url),
            _check_searxng(settings.searxng_url),
        )
    else:
        redis_status, postgres_status, genxng_status = await asyncio.gather(
            _check_redis(settings.redis_url),
            _check_database(settings.database_url),
            _check_searxng(settings.searxng_url),
        )
    statuses = (redis_status, postgres_status, genxng_status)
    return DepsResponse(
        status="ok" if all(s in {"ok", "disabled"} for s in statuses) else "degraded",
        redis=redis_status,
        postgres=postgres_status,
        genxng=genxng_status,
    )
