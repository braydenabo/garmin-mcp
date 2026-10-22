"""The local SQLite archive.

Raw API responses are the source of truth: each one is stored exactly as
Garmin returned it, keyed by dataset and local calendar date (or activity ID).
Typed columns and views are derived from that JSON in later migrations, so a
new field never needs a re-download.

The schema version lives in `PRAGMA user_version`. To change the schema, append
a script to MIGRATIONS; never edit one that has shipped.
"""

import json
import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

MIGRATIONS: list[str] = [
    # 1: raw archive
    """
    CREATE TABLE daily_raw (
        dataset       TEXT NOT NULL,
        calendar_date TEXT NOT NULL,  -- local date, YYYY-MM-DD
        raw_json      TEXT NOT NULL,
        fetched_at    TEXT NOT NULL,  -- UTC ISO 8601
        PRIMARY KEY (dataset, calendar_date)
    ) WITHOUT ROWID;

    CREATE TABLE activities (
        activity_id    INTEGER PRIMARY KEY,
        start_time_utc TEXT,
        calendar_date  TEXT,
        activity_type  TEXT,
        raw_json       TEXT NOT NULL,
        fit_path       TEXT,          -- relative to the data dir's fit/ folder
        fetched_at     TEXT NOT NULL
    );
    CREATE INDEX activities_by_date ON activities (calendar_date);

    CREATE TABLE sync_log (
        dataset       TEXT NOT NULL,
        calendar_date TEXT NOT NULL,
        status        TEXT NOT NULL CHECK (status IN ('ok', 'empty', 'error')),
        error         TEXT,
        synced_at     TEXT NOT NULL,
        PRIMARY KEY (dataset, calendar_date)
    ) WITHOUT ROWID;
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)
_DATASET_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the archive for writing, and migrate it."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def connect_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing archive so that no statement can modify it."""
    if not path.exists():
        raise FileNotFoundError(f"No archive at {path}. Run `garmin-connect-personal-mcp init`.")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    return version


def migrate(conn: sqlite3.Connection) -> None:
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"The archive is schema version {current}, but this version of "
            f"garmin-connect-personal-mcp only knows up to {SCHEMA_VERSION}. Upgrade the package."
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        script = MIGRATIONS[version - 1]
        # executescript commits first, so wrap each migration in its own transaction.
        try:
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.rollback()
            raise


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _dataset(name: str) -> str:
    if not _DATASET_NAME.match(name):
        raise ValueError(f"Invalid dataset name: {name!r}")
    return name


def _dumps(raw: Any) -> str:
    return json.dumps(raw, ensure_ascii=False, separators=(",", ":"))


def upsert_daily(conn: sqlite3.Connection, dataset: str, day: date, raw: Any) -> None:
    conn.execute(
        """
        INSERT INTO daily_raw (dataset, calendar_date, raw_json, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (dataset, calendar_date)
        DO UPDATE SET raw_json = excluded.raw_json, fetched_at = excluded.fetched_at
        """,
        (_dataset(dataset), day.isoformat(), _dumps(raw), _now()),
    )


def get_daily(conn: sqlite3.Connection, dataset: str, day: date) -> Any | None:
    row = conn.execute(
        "SELECT raw_json FROM daily_raw WHERE dataset = ? AND calendar_date = ?",
        (_dataset(dataset), day.isoformat()),
    ).fetchone()
    return None if row is None else json.loads(row["raw_json"])


def upsert_activity(
    conn: sqlite3.Connection,
    activity_id: int,
    raw: Any,
    *,
    start_time_utc: str | None = None,
    calendar_date: date | None = None,
    activity_type: str | None = None,
) -> None:
    """Insert or refresh an activity. Keeps any FIT path already recorded."""
    conn.execute(
        """
        INSERT INTO activities
            (activity_id, start_time_utc, calendar_date, activity_type, raw_json, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (activity_id) DO UPDATE SET
            start_time_utc = excluded.start_time_utc,
            calendar_date  = excluded.calendar_date,
            activity_type  = excluded.activity_type,
            raw_json       = excluded.raw_json,
            fetched_at     = excluded.fetched_at
        """,
        (
            activity_id,
            start_time_utc,
            None if calendar_date is None else calendar_date.isoformat(),
            activity_type,
            _dumps(raw),
            _now(),
        ),
    )


def set_fit_path(conn: sqlite3.Connection, activity_id: int, fit_path: str) -> None:
    conn.execute(
        "UPDATE activities SET fit_path = ? WHERE activity_id = ?", (fit_path, activity_id)
    )


def activities_missing_fit(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        "SELECT activity_id FROM activities WHERE fit_path IS NULL ORDER BY activity_id"
    ).fetchall()
    return [row["activity_id"] for row in rows]


def record_sync(
    conn: sqlite3.Connection, dataset: str, day: date, status: str, error: str | None = None
) -> None:
    conn.execute(
        """
        INSERT INTO sync_log (dataset, calendar_date, status, error, synced_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (dataset, calendar_date) DO UPDATE SET
            status = excluded.status, error = excluded.error, synced_at = excluded.synced_at
        """,
        (_dataset(dataset), day.isoformat(), status, error, _now()),
    )


def last_synced_date(conn: sqlite3.Connection, dataset: str) -> date | None:
    """The latest date this dataset synced without error, or None if never."""
    row = conn.execute(
        "SELECT MAX(calendar_date) AS d FROM sync_log WHERE dataset = ? AND status != 'error'",
        (_dataset(dataset),),
    ).fetchone()
    return None if row["d"] is None else date.fromisoformat(row["d"])


def last_sync_time(conn: sqlite3.Connection) -> str | None:
    """When any dataset last synced without error (UTC ISO 8601), or None if never."""
    row = conn.execute(
        "SELECT MAX(synced_at) AS t FROM sync_log WHERE status != 'error'"
    ).fetchone()
    return None if row["t"] is None else str(row["t"])
