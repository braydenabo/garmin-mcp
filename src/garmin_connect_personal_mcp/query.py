"""Read-only SQL over a clean copy of the archive.

Queries never touch the archive itself. Each call builds a throwaway
in-memory database with two tables, filled by the same code the other tools
use, so values, units and "missing is NULL" rules match theirs:

- `daily`: one row per date with data, one column per metric in METRICS.
- `activities`: one row per activity, the fields of garmin_list_activities.

Raw Garmin responses, names, profile photos and GPS coordinates are not in
that database, so no query can return them. On top of that, a SQLite
authorizer allows only SELECT statements reading these two tables, a
progress handler stops long queries, and results are capped at MAX_ROWS.
"""

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from garmin_connect_personal_mcp import activities, metrics

MAX_ROWS = 500
MAX_SQL_CHARS = 5000
TIMEOUT_SECONDS = 2.0

Cell = str | int | float | None

ACTIVITY_COLUMNS: dict[str, tuple[str, str]] = {
    # column -> (SQL type, description)
    "activity_id": ("INTEGER", "Use with garmin_get_activity or garmin_get_activity_stream"),
    "date": ("TEXT", "Local calendar date, YYYY-MM-DD"),
    "start_local": ("TEXT", "Local start time, YYYY-MM-DDTHH:MM"),
    "activity_type": ("TEXT", "e.g. running, trail_running, cycling, open_water_swimming"),
    "name": ("TEXT", "Activity name"),
    "duration_minutes": ("REAL", "Duration"),
    "distance_km": ("REAL", "Distance"),
    "avg_pace_min_per_km": ("REAL", "Average pace, on-foot sports only"),
    "avg_speed_kmh": ("REAL", "Average speed"),
    "avg_hr_bpm": ("REAL", "Average heart rate"),
    "max_hr_bpm": ("REAL", "Maximum heart rate"),
    "calories_kcal": ("REAL", "Calories"),
    "elevation_gain_m": ("REAL", "Elevation gain"),
    "training_load": ("REAL", "Garmin training load"),
    "aerobic_training_effect": ("REAL", "Aerobic training effect, 0-5"),
}


class QueryError(Exception):
    """A query that was refused or failed, with a message saying what to change."""


class QueryResult(BaseModel):
    columns: list[str]
    rows: list[list[Cell]]
    row_count: int
    truncated: bool = Field(description=f"True if there were more than {MAX_ROWS} rows")
    notes: list[str]


def schema_text() -> str:
    """The tables and columns, for the garmin://schema resource and error messages."""
    lines = [
        "Read-only SQLite. Two tables; missing values are NULL, never 0.",
        "",
        "TABLE daily -- one row per local date with any data",
        "  date TEXT -- YYYY-MM-DD",
    ]
    for name, m in metrics.METRICS.items():
        sql_type = "REAL" if m.numeric else "TEXT"
        lines.append(f"  {name} {sql_type} -- {m.description} ({m.unit})")
    lines += ["", "TABLE activities -- one row per activity"]
    for name, (sql_type, description) in ACTIVITY_COLUMNS.items():
        lines.append(f"  {name} {sql_type} -- {description}")
    return "\n".join(lines)


@dataclass
class _Snapshot:
    signature: tuple[int, ...]
    daily: list[tuple[Cell, ...]]
    activities: list[tuple[Cell, ...]]


_cache: dict[Path, _Snapshot] = {}
_cache_lock = threading.Lock()


def _signature(db_path: Path) -> tuple[int, ...]:
    """Changes whenever the archive (or its write-ahead log) does."""
    parts: list[int] = []
    for path in (db_path, db_path.with_name(db_path.name + "-wal")):
        try:
            stat = path.stat()
            parts += [stat.st_mtime_ns, stat.st_size]
        except FileNotFoundError:
            parts += [0, 0]
    return tuple(parts)


def _daily_rows(conn: sqlite3.Connection) -> list[tuple[Cell, ...]]:
    names = list(metrics.METRICS)
    days: dict[str, dict[str, Cell]] = {}
    for dataset in metrics.DATASETS:
        rows = conn.execute(
            "SELECT calendar_date, raw_json FROM daily_raw WHERE dataset = ?", (dataset,)
        ).fetchall()
        for day, raw_json in rows:
            raw = json.loads(raw_json)
            if not metrics.has_data(dataset, raw):
                continue
            values = days.setdefault(day, {})
            for name, metric in metrics.METRICS.items():
                if metric.dataset == dataset:
                    values[name] = metric.extract(raw)
    return [(day, *(days[day].get(n) for n in names)) for day in sorted(days)]


def _activity_rows(conn: sqlite3.Connection) -> list[tuple[Cell, ...]]:
    out: list[tuple[Cell, ...]] = []
    rows = conn.execute(
        "SELECT activity_id, calendar_date, raw_json FROM activities ORDER BY activity_id"
    ).fetchall()
    for activity_id, day, raw_json in rows:
        raw = json.loads(raw_json)
        s = activities.summarize(activity_id, raw if isinstance(raw, dict) else {})
        out.append(
            (
                s.activity_id,
                day,
                s.start_local,
                s.activity_type,
                s.name,
                s.duration_minutes,
                s.distance_km,
                s.avg_pace_min_per_km,
                s.avg_speed_kmh,
                s.avg_hr_bpm,
                s.max_hr_bpm,
                s.calories_kcal,
                s.elevation_gain_m,
                s.training_load,
                s.aerobic_training_effect,
            )
        )
    return out


def _snapshot(archive: sqlite3.Connection, db_path: Path) -> _Snapshot:
    signature = _signature(db_path)
    with _cache_lock:
        cached = _cache.get(db_path)
        if cached is not None and cached.signature == signature:
            return cached
    snapshot = _Snapshot(signature, _daily_rows(archive), _activity_rows(archive))
    with _cache_lock:
        _cache[db_path] = snapshot
    return snapshot


def _clean_database(snapshot: _Snapshot) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    daily_cols = ["date TEXT PRIMARY KEY"] + [
        f"{name} {'REAL' if m.numeric else 'TEXT'}" for name, m in metrics.METRICS.items()
    ]
    conn.execute(f"CREATE TABLE daily ({', '.join(daily_cols)})")
    activity_cols = [f"{name} {sql_type}" for name, (sql_type, _) in ACTIVITY_COLUMNS.items()]
    conn.execute(f"CREATE TABLE activities ({', '.join(activity_cols)})")
    if snapshot.daily:
        marks = ", ".join("?" * (len(metrics.METRICS) + 1))
        conn.executemany(f"INSERT INTO daily VALUES ({marks})", snapshot.daily)  # noqa: S608
    if snapshot.activities:
        marks = ", ".join("?" * len(ACTIVITY_COLUMNS))
        conn.executemany(f"INSERT INTO activities VALUES ({marks})", snapshot.activities)  # noqa: S608
    conn.commit()
    conn.execute("PRAGMA query_only = ON")
    return conn


def _authorizer(action: int, arg1: str | None, arg2: str | None, db: str | None, _: Any) -> int:
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        # arg1 is the table or CTE name. The database holds only the clean tables, so the
        # only other things to read are SQLite's own catalog tables.
        return (
            sqlite3.SQLITE_DENY if (arg1 or "").lower().startswith("sqlite_") else sqlite3.SQLITE_OK
        )
    if action in (sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def run(archive: sqlite3.Connection, db_path: Path, sql: str) -> QueryResult:
    """Run one read-only SELECT against the clean tables."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise QueryError("Empty query. Write one SELECT statement; see garmin://schema.")
    if len(text) > MAX_SQL_CHARS:
        raise QueryError(f"The query is over {MAX_SQL_CHARS} characters; simplify it.")

    conn = _clean_database(_snapshot(archive, db_path))
    deadline = time.monotonic() + TIMEOUT_SECONDS
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    conn.set_authorizer(_authorizer)
    try:
        cursor = conn.execute(text)
        rows = cursor.fetchmany(MAX_ROWS + 1)
        columns = [d[0] for d in cursor.description or ()]
    except sqlite3.ProgrammingError as exc:
        if "one statement" in str(exc):
            raise QueryError("Only one statement per call.") from None
        raise QueryError(f"SQL error: {exc}") from None
    except sqlite3.DatabaseError as exc:
        message = str(exc)
        if "interrupted" in message:
            raise QueryError(
                f"The query took over {TIMEOUT_SECONDS:g} seconds and was stopped. "
                "Narrow it with a WHERE on date, or aggregate."
            ) from None
        if "not authorized" in message or "prohibited" in message:
            raise QueryError(
                "Only SELECT statements reading the daily and activities tables are allowed."
            ) from None
        raise QueryError(
            f"SQL error: {message}. Tables and columns: see garmin://schema."
        ) from None
    finally:
        conn.close()

    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    notes = []
    if truncated:
        notes.append(f"Only the first {MAX_ROWS} rows are shown. Aggregate, or add a LIMIT.")
    if not rows:
        notes.append("No rows. garmin_describe_data shows which dates have data.")
    return QueryResult(
        columns=columns,
        rows=[list(r) for r in rows],
        row_count=len(rows),
        truncated=truncated,
        notes=notes,
    )
