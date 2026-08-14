"""SQLite backend for zero-container deployments.

Same four queries as the Postgres backend, running on the standard library's
sqlite3 so the Windows-native mode adds no dependencies at all. One connection,
serialized by a lock and driven through asyncio.to_thread: the write volume is
one UPDATE and one INSERT per request, which a single WAL-mode connection
absorbs without a pool.
"""
import asyncio
import secrets as pysecrets
import sqlite3
import threading
from pathlib import Path

from app.db import ApiKey

SCHEMA_SQL_SQLITE = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    credits INTEGER NOT NULL DEFAULT 10000,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key_id INTEGER NOT NULL REFERENCES api_keys(id),
    vertical TEXT NOT NULL,
    credits INTEGER NOT NULL,
    provider TEXT NOT NULL,
    cached INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS usage_log_key_created_idx
    ON usage_log (api_key_id, created_at);
"""


# Relative DSN paths anchor here rather than to the working directory: a
# Windows Scheduled Task starts in System32 unless somebody remembers the
# "Start in" box, and the admin CLI runs from wherever the operator happens to
# stand -- two processes quietly using two different database files, keys
# minted in one and refused by the other. One anchor, no divergence.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def sqlite_path_from_dsn(dsn: str) -> str:
    """The file path inside a sqlite DSN, ':memory:' included.

    Accepts sqlite:///relative/or/absolute and sqlite://:memory: shapes; the
    triple-slash form is the documented one. Relative paths resolve against
    the project root, never the process working directory.
    """
    raw = dsn
    for prefix in ("sqlite:///", "sqlite://"):
        if dsn.startswith(prefix):
            raw = dsn[len(prefix):] or ":memory:"
            break
    if raw == ":memory:" or Path(raw).is_absolute():
        return raw
    return str(_PROJECT_ROOT / raw)


class SqliteDatabase:
    """Drop-in for Database when the DSN scheme is sqlite."""

    def __init__(self, dsn: str) -> None:
        self._path = sqlite_path_from_dsn(dsn)
        self._conn: sqlite3.Connection | None = None
        # sqlite3 connections are not safe for concurrent use from multiple
        # threads; to_thread may run on any executor thread, so every
        # operation takes this lock.
        self._lock = threading.Lock()

    async def connect(self) -> None:
        await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        if self._path != ":memory:":
            parent = Path(self._path).resolve().parent
            parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        if self._path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_SQL_SQLITE)
        conn.commit()
        self._conn = conn

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteDatabase.connect() was not called")
        return self._conn

    async def fetch_api_key(self, key: str) -> ApiKey | None:
        def query() -> ApiKey | None:
            with self._lock:
                row = self.conn.execute(
                    "SELECT id, name, credits FROM api_keys WHERE key = ? AND active = 1",
                    (key,),
                ).fetchone()
            if row is None:
                return None
            return ApiKey(id=row[0], name=row[1], credits=row[2])

        return await asyncio.to_thread(query)

    async def create_api_key(self, name: str, credits: int = 10000) -> str:
        key = pysecrets.token_urlsafe(32)

        def insert() -> None:
            # The connection context manager commits on success and rolls back
            # on exception. An explicit commit() would leave a failed write's
            # transaction open, pinning the WAL write lock until the next
            # successful write -- indefinitely on an idle server.
            with self._lock, self.conn:
                self.conn.execute(
                    "INSERT INTO api_keys (key, name, credits) VALUES (?, ?, ?)",
                    (key, name, credits),
                )

        await asyncio.to_thread(insert)
        return key

    async def deduct_credits(self, key_id: int, cost: int) -> bool:
        """Atomically deduct; refuses to take the balance below zero.

        Success is the cursor's rowcount rather than the command-tag string
        asyncpg returns -- the one behavioural seam between the two backends.
        """

        def update() -> bool:
            with self._lock, self.conn:
                cursor = self.conn.execute(
                    "UPDATE api_keys SET credits = credits - ? WHERE id = ? AND credits >= ?",
                    (cost, key_id, cost),
                )
                return cursor.rowcount == 1

        return await asyncio.to_thread(update)

    async def log_usage(
        self,
        key_id: int,
        vertical: str,
        credits: int,
        provider: str,
        cached: bool,
        latency_ms: int,
    ) -> None:
        def insert() -> None:
            with self._lock, self.conn:
                self.conn.execute(
                    "INSERT INTO usage_log (api_key_id, vertical, credits, provider,"
                    " cached, latency_ms) VALUES (?, ?, ?, ?, ?, ?)",
                    (key_id, vertical, credits, provider, int(cached), latency_ms),
                )

        await asyncio.to_thread(insert)
