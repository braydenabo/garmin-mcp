"""The MCP server, exercised through the SDK's own client."""

import json
import os
import sys
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client, StdioServerParameters

from garmin_connect_personal_mcp import config, db, metrics
from garmin_connect_personal_mcp.server import build_server

DAY = date(2026, 3, 10)


def run[T](fn: Callable[[Client], Awaitable[T]], paths: config.DataPaths | None = None) -> T:
    server = build_server(paths or config.data_paths())

    async def main() -> T:
        async with Client(server) as client:
            return await fn(client)

    return anyio.run(main)


def call(name: str, args: dict[str, Any] | None = None) -> Any:
    async def go(client: Client) -> Any:
        result = await client.call_tool(name, args or {})
        assert not result.is_error, result.content
        # The text copy must match the structured content for text-only clients.
        assert json.loads(result.content[0].text) == result.structured_content  # type: ignore[union-attr]
        return result.structured_content

    return run(go)


def seed() -> None:
    conn = db.connect(config.data_paths().db)
    db.upsert_daily(
        conn, "sleep", DAY, {"calendarDate": DAY.isoformat(), "sleepTimeSeconds": 27000}
    )
    db.record_sync(conn, "sleep", DAY, "ok")
    db.upsert_activity(conn, 1, {"activityId": 1}, calendar_date=DAY, activity_type="running")
    conn.commit()
    conn.close()


def test_tools_are_listed_with_annotations_and_schemas() -> None:
    async def go(client: Client) -> Any:
        return (await client.list_tools()).tools

    tools = {t.name: t for t in run(go)}
    describe = tools["garmin_describe_data"]
    assert describe.annotations is not None
    assert describe.annotations.read_only_hint is True
    assert describe.annotations.open_world_hint is False
    assert describe.output_schema is not None
    assert describe.input_schema["properties"] == {}


def test_server_instructions_point_to_describe_data() -> None:
    server = build_server(config.data_paths())
    assert server.instructions is not None
    assert "garmin_describe_data" in server.instructions
    assert "null, never zero" in server.instructions


def test_describe_without_archive() -> None:
    result = call("garmin_describe_data")
    assert result["archive_exists"] is False
    assert result["datasets"] == []
    assert any("sync" in note for note in result["notes"])
    assert len(result["metrics"]) == len(metrics.METRICS)


def test_describe_with_data() -> None:
    seed()
    result = call("garmin_describe_data")
    assert result["archive_exists"] is True
    sleep = next(d for d in result["datasets"] if d["dataset"] == "sleep")
    assert sleep["days_with_data"] == 1 and sleep["first_date"] == DAY.isoformat()
    assert result["activities_count"] == 1
    hours = next(m for m in result["metrics"] if m["name"] == "sleep_hours")
    assert hours == {
        "name": "sleep_hours",
        "dataset": "sleep",
        "unit": "hours",
        "description": "Total sleep",
        "numeric": True,
    }


@pytest.mark.skipif(sys.platform == "win32", reason="stdio subprocess test is POSIX-only for now")
def test_serve_over_stdio(isolated_home: Path) -> None:
    """Launch `serve` the way an MCP client does and talk to it over stdin/stdout."""
    seed()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "garmin_connect_personal_mcp", "serve"],
        env={
            "HOME": str(isolated_home),
            "GARMIN_MCP_DATA_DIR": os.environ["GARMIN_MCP_DATA_DIR"],
            "PATH": os.environ.get("PATH", ""),
        },
    )

    async def main() -> Any:
        async with Client(params, read_timeout_seconds=30) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            result = await client.call_tool("garmin_describe_data", {})
            return names, result.structured_content

    names, described = anyio.run(main)
    assert "garmin_describe_data" in names
    assert described["archive_exists"] is True


# --- garmin_get_day and garmin_get_trend


TODAY = date(2026, 3, 15)  # a Sunday


def call_at(name: str, args: dict[str, Any] | None = None) -> Any:
    """Call a tool with 'today' fixed to TODAY. Returns (is_error, structured or text)."""
    server = build_server(config.data_paths(), today=lambda: TODAY)

    async def main() -> Any:
        async with Client(server) as client:
            result = await client.call_tool(name, args or {})
            if result.is_error:
                return True, result.content[0].text  # type: ignore[union-attr]
            return False, result.structured_content

    return anyio.run(main)


def store_sleep(conn: Any, when: date, hours: float | None) -> None:
    seconds = None if hours is None else int(hours * 3600)
    db.upsert_daily(
        conn, "sleep", when, {"calendarDate": when.isoformat(), "sleepTimeSeconds": seconds}
    )
    db.record_sync(conn, "sleep", when, "ok")


def test_get_day_statuses() -> None:
    conn = db.connect(config.data_paths().db)
    store_sleep(conn, TODAY, 7.5)
    db.record_sync(conn, "hrv", TODAY, "empty")
    db.record_sync(conn, "daily_summary", TODAY, "error", "HTTP 503")
    db.upsert_activity(conn, 5, {"activityId": 5}, calendar_date=TODAY, activity_type="cycling")
    conn.commit()
    error, day = call_at("garmin_get_day", {"date": "today"})
    assert not error
    assert day["date"] == TODAY.isoformat()
    assert day["status"] == {"daily_summary": "sync_failed", "hrv": "no_data", "sleep": "data"}
    assert day["sleep"]["sleep_hours"] == 7.5
    assert day["hrv"] is None and day["daily_summary"] is None
    assert day["activities"] == [
        {"activity_id": 5, "activity_type": "cycling", "start_time_utc": None}
    ]
    assert any("garmin_sync retries" in n for n in day["notes"])


def test_get_day_not_synced_suggests_sync() -> None:
    db.connect(config.data_paths().db).close()
    error, day = call_at("garmin_get_day", {"date": "yesterday"})
    assert not error
    assert day["date"] == "2026-03-14"
    assert set(day["status"].values()) == {"not_synced"}
    assert "Call garmin_sync" in day["notes"][0]


def test_get_day_not_worn() -> None:
    conn = db.connect(config.data_paths().db)
    store_sleep(conn, TODAY, None)
    conn.commit()
    _, day = call_at("garmin_get_day", {"date": TODAY.isoformat()})
    assert day["status"]["sleep"] == "no_data"
    assert any("wasn't worn" in n for n in day["notes"])


@pytest.mark.parametrize("value", ["15/03/2026", "tomorrow", ""])
def test_get_day_rejects_bad_dates(value: str) -> None:
    db.connect(config.data_paths().db).close()
    error, text = call_at("garmin_get_day", {"date": value})
    assert error and "YYYY-MM-DD" in text


def test_tools_without_archive_suggest_sync() -> None:
    for name, args in [("garmin_get_day", {}), ("garmin_get_trend", {"metric": "sleep_hours"})]:
        error, text = call_at(name, args)
        assert error and "sync" in text


def test_get_trend_daily_defaults_to_last_30_days() -> None:
    conn = db.connect(config.data_paths().db)
    for i in range(40):
        when = TODAY - timedelta(days=i)
        store_sleep(conn, when, None if i % 10 == 0 else 6 + (i % 3))
    conn.commit()
    error, trend = call_at("garmin_get_trend", {"metric": "sleep_hours"})
    assert not error
    assert trend["start"] == (TODAY - timedelta(days=29)).isoformat()
    assert trend["end"] == TODAY.isoformat()
    assert trend["unit"] == "hours"
    assert trend["stats"]["days_with_data"] == 27  # 30 days minus 3 not worn
    assert trend["days_without_data"] == 3
    assert trend["stats"]["min"] == 6.0 and trend["stats"]["max"] == 8.0
    assert trend["points"][0]["period_start"] == (TODAY - timedelta(days=29)).isoformat()


def test_get_trend_weekly_means() -> None:
    conn = db.connect(config.data_paths().db)
    monday = date(2026, 3, 9)
    for i, hours in enumerate([6, 8, 7]):
        store_sleep(conn, monday + timedelta(days=i), hours)
    store_sleep(conn, monday - timedelta(days=1), 9)  # the previous week's Sunday
    conn.commit()
    _, trend = call_at(
        "garmin_get_trend",
        {"metric": "sleep_hours", "start": "2026-03-01", "end": "2026-03-15", "bucket": "week"},
    )
    assert trend["points"] == [
        {"period_start": "2026-03-02", "value": 9.0, "days_with_data": 1},
        {"period_start": "2026-03-09", "value": 7.0, "days_with_data": 3},
    ]
    assert trend["stats"]["first"] == 9.0 and trend["stats"]["last"] == 7.0


def test_get_trend_errors_guide_the_model() -> None:
    db.connect(config.data_paths().db).close()
    error, text = call_at("garmin_get_trend", {"metric": "resting_heart_rate"})
    assert error and "resting_hr_bpm" in text
    error, text = call_at("garmin_get_trend", {"metric": "hrv_status"})  # not numeric
    assert error and "Unknown metric" in text
    error, text = call_at(
        "garmin_get_trend", {"metric": "steps", "start": "2026-03-10", "end": "2026-03-01"}
    )
    assert error and "before start" in text
    error, text = call_at("garmin_get_trend", {"metric": "steps", "start": "2024-01-01"})
    assert error and "bucket='week'" in text
    error, _ = call_at(
        "garmin_get_trend", {"metric": "steps", "start": "2024-01-01", "bucket": "week"}
    )
    assert not error


def test_get_trend_with_no_data_in_range() -> None:
    db.connect(config.data_paths().db).close()
    _, trend = call_at("garmin_get_trend", {"metric": "steps"})
    assert trend["points"] == [] and trend["stats"]["mean"] is None
    assert "garmin_describe_data" in trend["notes"][0]


def test_get_trend_metric_is_an_enum_in_the_schema() -> None:
    async def go(client: Client) -> Any:
        return {t.name: t for t in (await client.list_tools()).tools}

    schema = run(go)["garmin_get_trend"].input_schema
    assert schema["properties"]["metric"]["enum"] == metrics.NUMERIC_METRICS
    assert schema["properties"]["bucket"]["enum"] == ["day", "week"]
    assert schema["required"] == ["metric"]
