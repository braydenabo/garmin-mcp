"""garmin_query: read-only SQL over clean tables, and the guards around it."""

import json
import time
from datetime import date, timedelta
from typing import Any

import anyio
import pytest
from mcp import Client

from garmin_connect_personal_mcp import config, db, query
from garmin_connect_personal_mcp.server import build_server

DAY = date(2026, 3, 1)


def seed(days: int = 3) -> None:
    conn = db.connect(config.data_paths().db)
    for n in range(days):
        day = DAY + timedelta(days=n)
        db.upsert_daily(conn, "sleep", day, {"sleepTimeSeconds": 7 * 3600 + n * 1800})
        db.upsert_daily(
            conn,
            "daily_summary",
            day,
            {
                "heartRate": {"minValue": 45, "restingValue": 50 + n},
                "movement": {"steps": {"value": 8000 + 1000 * n}},
                "stress": {"avgValue": -1},  # Garmin's "not enough data" code
            },
        )
    db.upsert_daily(conn, "sleep", DAY + timedelta(days=days), {"sleepTimeSeconds": 0})  # not worn
    db.upsert_activity(
        conn,
        11,
        {
            "activityId": 11,
            "activityName": "Easy run",
            "activityType": {"typeKey": "running"},
            "startTimeLocal": "2026-03-01 07:00:00",
            "distance": 5000.0,
            "duration": 1800.0,
            "averageSpeed": 2.7778,
            "ownerFullName": "Pat Example",
            "startLatitude": 40.12345,
            "startLongitude": -75.12345,
        },
        calendar_date=DAY,
        activity_type="running",
    )
    conn.commit()
    conn.close()


def call(sql: str) -> tuple[bool, Any, str]:
    async def go() -> tuple[bool, Any, str]:
        async with Client(build_server(config.data_paths())) as client:
            result = await client.call_tool("garmin_query", {"sql": sql})
            text = result.content[0].text  # type: ignore[union-attr]
            return result.is_error, result.structured_content, text

    return anyio.run(go)


def ok(sql: str) -> dict[str, Any]:
    error, result, text = call(sql)
    assert not error, text
    assert json.loads(text) == result
    return result  # type: ignore[no-any-return]


# --- answers


def test_daily_table_uses_the_same_rules_as_the_other_tools() -> None:
    seed()
    result = ok("SELECT date, sleep_hours, resting_hr_bpm, steps, stress_avg FROM daily")
    assert result["columns"] == ["date", "sleep_hours", "resting_hr_bpm", "steps", "stress_avg"]
    assert result["rows"] == [
        ["2026-03-01", 7.0, 50, 8000, None],  # -1 stress is NULL, not -1 or 0
        ["2026-03-02", 7.5, 51, 9000, None],
        ["2026-03-03", 8.0, 52, 10000, None],
    ]  # the no-wear day isn't a row
    assert result["row_count"] == 3 and result["truncated"] is False


def test_aggregates_and_ctes() -> None:
    seed()
    result = ok(
        "WITH s AS (SELECT steps FROM daily) SELECT avg(steps) AS avg_steps, count(*) FROM s"
    )
    assert result["rows"] == [[9000.0, 3]]


def test_activities_table_has_no_personal_details() -> None:
    seed()
    result = ok("SELECT * FROM activities")
    row = dict(zip(result["columns"], result["rows"][0], strict=True))
    assert row["activity_id"] == 11 and row["distance_km"] == 5.0
    assert row["avg_pace_min_per_km"] == 6.0 and row["date"] == "2026-03-01"
    assert row["name"] == "Easy run"
    _, _, text = call("SELECT * FROM activities")
    assert "Pat Example" not in text and "40.12" not in text and "-75.12" not in text


def test_rows_are_capped() -> None:
    seed()
    result = ok(
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 600) "
        "SELECT i FROM n"
    )
    assert result["row_count"] == query.MAX_ROWS and result["truncated"] is True
    assert "first 500 rows" in result["notes"][0]


def test_no_rows_points_to_describe_data() -> None:
    seed()
    assert "garmin_describe_data" in ok("SELECT * FROM daily WHERE date > '2030'")["notes"][0]


def test_trailing_semicolon_is_fine() -> None:
    seed()
    assert ok("SELECT count(*) FROM daily;")["rows"] == [[3]]


# --- guards


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM daily",
        "UPDATE daily SET steps = 0",
        "INSERT INTO daily (date) VALUES ('2030-01-01')",
        "DROP TABLE daily",
        "CREATE TABLE x (a)",
        "PRAGMA table_info(daily)",
        "ATTACH DATABASE 'other.db' AS other",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM daily_raw",
    ],
)
def test_only_selects_on_the_clean_tables(sql: str) -> None:
    seed()
    error, _, text = call(sql)
    assert error, sql
    assert "allowed" in text or "SQL error" in text


def test_raw_archive_tables_do_not_exist_here() -> None:
    seed()
    error, _, text = call("SELECT raw_json FROM activities")
    assert error and "garmin://schema" in text


def test_one_statement_per_call() -> None:
    seed()
    error, _, text = call("SELECT 1; SELECT 2")
    assert error and "one statement" in text


def test_long_queries_are_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    seed()
    monkeypatch.setattr(query, "TIMEOUT_SECONDS", 0.2)
    started = time.monotonic()
    error, _, text = call(
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n"
    )
    assert error and "stopped" in text
    assert time.monotonic() - started < 10


def test_empty_and_oversized() -> None:
    seed()
    error, _, text = call("  ")
    assert error and "Empty query" in text
    error, _, text = call("SELECT '" + "x" * 5001 + "'")
    assert error and "over 5000 characters" in text


def test_archive_is_never_modified() -> None:
    seed()
    path = config.data_paths().db
    before = path.read_bytes()
    call("DELETE FROM daily")
    call("SELECT * FROM daily")
    assert path.read_bytes() == before


def test_results_follow_new_syncs() -> None:
    seed(days=1)
    assert ok("SELECT count(*) FROM daily")["rows"] == [[1]]
    conn = db.connect(config.data_paths().db)
    db.upsert_daily(conn, "sleep", DAY + timedelta(days=5), {"sleepTimeSeconds": 25000})
    conn.commit()
    conn.close()
    assert ok("SELECT count(*) FROM daily")["rows"] == [[2]]


def test_without_archive_suggests_sync() -> None:
    error, _, text = call("SELECT 1")
    assert error and "sync" in text


# --- schema


def test_schema_lists_every_column_with_units() -> None:
    text = query.schema_text()
    assert "TABLE daily" in text and "TABLE activities" in text
    assert "sleep_hours REAL -- Total sleep (hours)" in text
    assert "hrv_status TEXT" in text and "avg_pace_min_per_km REAL" in text


def test_schema_resource_only_with_the_query_toolset() -> None:
    async def uris(toolsets: tuple[str, ...]) -> set[str]:
        server = build_server(config.data_paths(), toolsets=toolsets)
        async with Client(server) as client:
            return {str(r.uri) for r in (await client.list_resources()).resources}

    assert "garmin://schema" in anyio.run(uris, ("query",))
    assert "garmin://schema" not in anyio.run(uris, ("daily",))

    async def read() -> str:
        async with Client(build_server(config.data_paths())) as client:
            result = await client.read_resource("garmin://schema")
            return result.contents[0].text  # type: ignore[union-attr]

    assert anyio.run(read) == query.schema_text()
