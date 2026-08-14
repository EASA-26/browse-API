"""SQL datastore: the DatabaseBackend contract, the asyncpg implementation, and
the create_database factory (sqlite:// DSNs get the zero-container backend)."""
import secrets as pysecrets
from typing import Protocol

import asyncpg
from pydantic import BaseModel

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS api_keys (
    id SERIAL PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    credits INTEGER NOT NULL DEFAULT 10000,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS usage_log (
    id BIGSERIAL PRIMARY KEY,
    api_key_id INTEGER NOT NULL REFERENCES api_keys(id),
    vertical TEXT NOT NULL,
    credits INTEGER NOT NULL,
    provider TEXT NOT NULL,
    cached BOOLEAN NOT NULL,
    latency_ms INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS usage_log_key_created_idx
    ON usage_log (api_key_id, created_at);
"""


class ApiKey(BaseModel):
    id: int
    name: str
    credits: int


class DatabaseBackend(Protocol):
    """What the app needs from a datastore -- four queries and a lifecycle.

    The endpoint tests have always duck-typed this surface with a FakeDB; the
    protocol writes that contract down so a second backend can implement it
    honestly under mypy strict.
    """

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def fetch_api_key(self, key: str) -> ApiKey | None: ...

    async def create_api_key(self, name: str, credits: int = 10000) -> str: ...

    async def deduct_credits(self, key_id: int, cost: int) -> bool: ...

    async def log_usage(
        self,
        key_id: int,
        vertical: str,
        credits: int,
        provider: str,
        cached: bool,
        latency_ms: int,
    ) -> None: ...


class Database:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10)
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database.connect() was not called")
        return self._pool

    async def fetch_api_key(self, key: str) -> ApiKey | None:
        row = await self.pool.fetchrow(
            "SELECT id, name, credits FROM api_keys WHERE key = $1 AND active", key
        )
        if row is None:
            return None
        return ApiKey(id=row["id"], name=row["name"], credits=row["credits"])

    async def create_api_key(self, name: str, credits: int = 10000) -> str:
        key = pysecrets.token_urlsafe(32)
        await self.pool.execute(
            "INSERT INTO api_keys (key, name, credits) VALUES ($1, $2, $3)", key, name, credits
        )
        return key

    async def deduct_credits(self, key_id: int, cost: int) -> bool:
        """Atomically deduct; refuses to take the balance below zero."""
        result: str = await self.pool.execute(
            "UPDATE api_keys SET credits = credits - $2 WHERE id = $1 AND credits >= $2",
            key_id,
            cost,
        )
        return result == "UPDATE 1"

    async def log_usage(
        self,
        key_id: int,
        vertical: str,
        credits: int,
        provider: str,
        cached: bool,
        latency_ms: int,
    ) -> None:
        await self.pool.execute(
            "INSERT INTO usage_log (api_key_id, vertical, credits, provider, cached, latency_ms)"
            " VALUES ($1, $2, $3, $4, $5, $6)",
            key_id,
            vertical,
            credits,
            provider,
            cached,
            latency_ms,
        )


def create_database(dsn: str) -> DatabaseBackend:
    """The backend a DSN names: sqlite:// gets SQLite, everything else Postgres.

    SQLite is the zero-container mode for hosts that cannot run Docker at all
    (no nested virtualization); the Postgres path is untouched and remains the
    default for the compose deployment.
    """
    if dsn.startswith("sqlite:"):
        from app.db_sqlite import SqliteDatabase

        return SqliteDatabase(dsn)
    return Database(dsn)
