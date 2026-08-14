"""Health endpoint tests (no infrastructure required)."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["service"] == "gen-api"


async def test_sqlite_probe_is_read_only_and_honest(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The probe must not create the file it is checking, and absent is error."""
    from app.api.health import _check_database, _check_sqlite_sync

    missing = tmp_path / "nope" / "gen.db"
    assert _check_sqlite_sync(f"sqlite:///{missing}") == "error"
    assert not missing.exists()

    import sqlite3

    real = tmp_path / "gen.db"
    sqlite3.connect(real).close()
    assert _check_sqlite_sync(f"sqlite:///{real}") == "ok"
    # Routing: a sqlite DSN must never reach the TCP probe.
    assert await _check_database(f"sqlite:///{real}") == "ok"


def test_deps_reports_redis_disabled_in_zero_container_mode(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """memory:// makes redis 'disabled', and disabled counts as healthy."""
    import sqlite3

    from app.config import get_settings

    monkeypatch.setenv("REDIS_URL", "memory://")
    tmp = get_settings.cache_clear  # lru_cache: env changes need a fresh read
    tmp()
    try:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            db_file = Path(td) / "gen.db"
            sqlite3.connect(db_file).close()
            monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
            get_settings.cache_clear()
            response = client.get("/health/deps")
            assert response.status_code == 200
            payload = response.json()
            assert payload["redis"] == "disabled"
            assert payload["postgres"] == "ok"
            # genxng may be up or down on the test machine; disabled+ok must
            # not be what fails the roll-up.
            if payload["genxng"] == "ok":
                assert payload["status"] == "ok"
    finally:
        get_settings.cache_clear()
