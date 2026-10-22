import sqlite3
from datetime import date
from pathlib import Path

import pytest

from garmin_connect_personal_mcp import db

DAY = date(2026, 1, 15)
SYNTHETIC_SLEEP = {"calendarDate": "2026-01-15", "sleepTimeSeconds": 27000, "note": "café"}


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return db.connect(tmp_path / "garmin.db")


def test_connect_creates_and_migrates(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "garmin.db"
    conn = db.connect(path)
    assert path.exists()
    assert db.schema_version(conn) == db.SCHEMA_VERSION
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"daily_raw", "activities", "sync_log"} <= tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "garmin.db"
    db.connect(path).close()
    conn = db.connect(path)
    assert db.schema_version(conn) == db.SCHEMA_VERSION


def test_refuses_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "garmin.db"
    conn = db.connect(path)
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="Upgrade the package"):
        db.connect(path)


def test_failed_migration_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "garmin.db"
    db.connect(path).close()
    broken = [*db.MIGRATIONS, "CREATE TABLE extra (x); THIS IS NOT SQL;"]
    monkeypatch.setattr(db, "MIGRATIONS", broken)
    monkeypatch.setattr(db, "SCHEMA_VERSION", len(broken))
    with pytest.raises(sqlite3.Error):
        db.connect(path)
    monkeypatch.undo()
    conn = db.connect(path)
    assert db.schema_version(conn) == db.SCHEMA_VERSION
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='extra'").fetchone() is None


def test_daily_round_trip_and_upsert(conn: sqlite3.Connection) -> None:
    assert db.get_daily(conn, "sleep", DAY) is None
    db.upsert_daily(conn, "sleep", DAY, SYNTHETIC_SLEEP)
    assert db.get_daily(conn, "sleep", DAY) == SYNTHETIC_SLEEP
    db.upsert_daily(conn, "sleep", DAY, {"sleepTimeSeconds": 1})
    assert db.get_daily(conn, "sleep", DAY) == {"sleepTimeSeconds": 1}
    assert conn.execute("SELECT COUNT(*) FROM daily_raw").fetchone()[0] == 1


@pytest.mark.parametrize("bad", ["", "Sleep", "sleep; DROP TABLE x", "1sleep"])
def test_rejects_bad_dataset_names(conn: sqlite3.Connection, bad: str) -> None:
    with pytest.raises(ValueError):
        db.upsert_daily(conn, bad, DAY, {})


def test_activity_upsert_keeps_fit_path(conn: sqlite3.Connection) -> None:
    db.upsert_activity(conn, 1001, {"v": 1}, calendar_date=DAY, activity_type="running")
    assert db.activities_missing_fit(conn) == [1001]
    db.set_fit_path(conn, 1001, "1001.zip")
    db.upsert_activity(conn, 1001, {"v": 2}, calendar_date=DAY, activity_type="running")
    row = conn.execute("SELECT * FROM activities WHERE activity_id = 1001").fetchone()
    assert row["fit_path"] == "1001.zip"
    assert row["raw_json"] == '{"v":2}'
    assert row["calendar_date"] == "2026-01-15"
    assert db.activities_missing_fit(conn) == []


def test_last_synced_date_ignores_errors(conn: sqlite3.Connection) -> None:
    assert db.last_synced_date(conn, "sleep") is None
    db.record_sync(conn, "sleep", date(2026, 1, 14), "ok")
    db.record_sync(conn, "sleep", date(2026, 1, 15), "empty")
    db.record_sync(conn, "sleep", date(2026, 1, 16), "error", "HTTP 500")
    assert db.last_synced_date(conn, "sleep") == date(2026, 1, 15)
    db.record_sync(conn, "sleep", date(2026, 1, 16), "ok")
    assert db.last_synced_date(conn, "sleep") == date(2026, 1, 16)


def test_record_sync_rejects_unknown_status(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.record_sync(conn, "sleep", DAY, "maybe")


def test_readonly_connection_cannot_write(tmp_path: Path) -> None:
    path = tmp_path / "garmin.db"
    conn = db.connect(path)
    db.upsert_daily(conn, "sleep", DAY, SYNTHETIC_SLEEP)
    conn.commit()
    ro = db.connect_readonly(path)
    assert db.get_daily(ro, "sleep", DAY) == SYNTHETIC_SLEEP
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM daily_raw")


def test_readonly_missing_archive_explains_fix(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="init"):
        db.connect_readonly(tmp_path / "missing.db")


def test_last_sync_time_ignores_errors(conn: sqlite3.Connection) -> None:
    assert db.last_sync_time(conn) is None
    db.record_sync(conn, "sleep", DAY, "error", "HTTP 500")
    assert db.last_sync_time(conn) is None
    db.record_sync(conn, "hrv", DAY, "empty")
    assert db.last_sync_time(conn) is not None
