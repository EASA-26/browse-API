"""SQLite backend: the same four queries, the same behaviour (no infrastructure)."""
import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.db import create_database
from app.db_sqlite import _PROJECT_ROOT, SqliteDatabase, sqlite_path_from_dsn


def test_dsn_path_parsing() -> None:
    assert sqlite_path_from_dsn("sqlite:///:memory:") == ":memory:"
    assert sqlite_path_from_dsn("sqlite://") == ":memory:"
    absolute = sqlite_path_from_dsn("sqlite:///C:/data/gen.db")
    assert Path(absolute) == Path("C:/data/gen.db")
    # Relative paths anchor to the project root, never the working directory:
    # a Scheduled Task starts in System32, and the CLI starts wherever the
    # operator stands -- both must open the same file.
    anchored = Path(sqlite_path_from_dsn("sqlite:///./data/gen.db"))
    assert anchored.is_absolute()
    assert anchored == _PROJECT_ROOT / "data/gen.db"


def test_factory_picks_backend_by_scheme() -> None:
    assert isinstance(create_database("sqlite:///:memory:"), SqliteDatabase)
    # The Postgres path must be untouched by the factory's existence.
    from app.db import Database

    assert isinstance(create_database("postgresql://gen:gen@postgres:5432/gen"), Database)


async def test_key_lifecycle_roundtrip() -> None:
    db = SqliteDatabase("sqlite:///:memory:")
    await db.connect()
    try:
        key = await db.create_api_key("test-consumer", credits=100)
        fetched = await db.fetch_api_key(key)
        assert fetched is not None
        assert fetched.name == "test-consumer"
        assert fetched.credits == 100
        assert await db.fetch_api_key("not-a-key") is None
    finally:
        await db.close()


async def test_deduct_refuses_to_go_below_zero() -> None:
    """The atomic no-negative-balance guarantee, the backend's hardest promise."""
    db = SqliteDatabase("sqlite:///:memory:")
    await db.connect()
    try:
        key = await db.create_api_key("small-balance", credits=3)
        fetched = await db.fetch_api_key(key)
        assert fetched is not None
        assert await db.deduct_credits(fetched.id, 2) is True
        # Balance is now 1; a cost of 2 must refuse rather than overdraw.
        assert await db.deduct_credits(fetched.id, 2) is False
        remaining = await db.fetch_api_key(key)
        assert remaining is not None
        assert remaining.credits == 1
    finally:
        await db.close()


async def test_usage_log_write(tmp_path: Path) -> None:
    """Logging into a real file, so the schema-on-boot path is exercised too."""
    db = SqliteDatabase(f"sqlite:///{tmp_path}/gen.db")
    await db.connect()
    try:
        key = await db.create_api_key("logger")
        fetched = await db.fetch_api_key(key)
        assert fetched is not None
        await db.log_usage(fetched.id, "search", 1, "genxng", False, 231)
        rows = db.conn.execute("SELECT vertical, provider, cached FROM usage_log").fetchall()
        assert rows == [("search", "genxng", 0)]
    finally:
        await db.close()


async def test_schema_survives_reconnect(tmp_path: Path) -> None:
    """CREATE IF NOT EXISTS on every boot, exactly like the Postgres backend."""
    dsn = f"sqlite:///{tmp_path}/gen.db"
    first = SqliteDatabase(dsn)
    await first.connect()
    key = await first.create_api_key("persistent")
    await first.close()

    second = SqliteDatabase(dsn)
    await second.connect()
    try:
        fetched = await second.fetch_api_key(key)
        assert fetched is not None
        assert fetched.name == "persistent"
    finally:
        await second.close()


async def test_concurrent_deducts_never_overdraw() -> None:
    """Twenty concurrent deductions against ten credits: exactly ten succeed.

    to_thread gives real multi-thread contention on the single locked
    connection -- the concurrency the docstring promises, exercised rather
    than asserted.
    """
    db = SqliteDatabase("sqlite:///:memory:")
    await db.connect()
    try:
        key = await db.create_api_key("contended", credits=10)
        fetched = await db.fetch_api_key(key)
        assert fetched is not None
        results = await asyncio.gather(
            *(db.deduct_credits(fetched.id, 1) for _ in range(20))
        )
        assert sum(results) == 10
        remaining = await db.fetch_api_key(key)
        assert remaining is not None
        assert remaining.credits == 0
    finally:
        await db.close()


async def test_failed_write_does_not_poison_the_connection() -> None:
    """A refused INSERT must roll back, not pin the WAL write lock forever."""
    db = SqliteDatabase("sqlite:///:memory:")
    await db.connect()
    try:
        first_key = await db.create_api_key("first")

        def clash() -> None:
            # A UNIQUE violation, through the same lock-and-context pattern the
            # write methods use.
            with db._lock, db.conn:
                db.conn.execute(
                    "INSERT INTO api_keys (key, name, credits) VALUES (?, ?, ?)",
                    (first_key, "dup", 1),
                )

        with pytest.raises(sqlite3.IntegrityError):
            await asyncio.to_thread(clash)
        assert not db.conn.in_transaction
        # The connection still serves writes.
        await db.create_api_key("second")
    finally:
        await db.close()
