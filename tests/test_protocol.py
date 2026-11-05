"""Every tool, called the way an MCP client calls it: `serve` as a subprocess over stdio.

The server runs against a synthetic archive with a FIT file. Each tool's result
must match its declared output schema and carry the same JSON as text, for
clients that only read text. A new tool fails this test until it has a call here.
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import anyio
import jsonschema
import pytest
from fitfiles import simple_run, zipped
from mcp import Client, StdioServerParameters

from garmin_connect_personal_mcp import config, db

TODAY = date.today()

# One representative call per tool. garmin_sync has no API key here, so it must fail
# with an actionable message and without contacting Garmin.
CALLS: dict[str, dict[str, Any]] = {
    "garmin_describe_data": {},
    "garmin_get_day": {"date": "yesterday"},
    "garmin_get_trend": {"metric": "sleep_hours", "bucket": "week"},
    "garmin_get_baseline": {"metric": "resting_hr_bpm", "date": "yesterday", "days": 14},
    "garmin_compare_periods": {"metric": "steps", "start": "today", "end": "today"},
    "garmin_list_activities": {"limit": 5},
    "garmin_get_activity": {"activity_id": 1},
    "garmin_get_activity_stream": {"activity_id": 1, "interval_seconds": 5},
    "garmin_query": {"sql": "SELECT count(*) AS days, avg(steps) FROM daily"},
    "garmin_show_chart": {"metric": "steps"},
    "garmin_get_chart_data": {"metric": "steps"},
    "garmin_sync": {},
}
EXPECTED_ERRORS = {"garmin_sync": "API key"}


def seed() -> None:
    paths = config.data_paths()
    conn = db.connect(paths.db)
    for n in range(1, 21):
        day = TODAY - timedelta(days=n)
        db.upsert_daily(conn, "sleep", day, {"sleepTimeSeconds": 27000 + 60 * n})
        db.upsert_daily(
            conn,
            "daily_summary",
            day,
            {
                "heartRate": {"minValue": 44, "restingValue": 50 + n % 4},
                "movement": {"steps": {"value": 7000 + 100 * n}},
            },
        )
        for dataset in ("sleep", "daily_summary"):
            db.record_sync(conn, dataset, day, "ok")
    db.upsert_activity(
        conn,
        1,
        {"activityId": 1, "activityType": {"typeKey": "running"}, "distance": 5000.0},
        calendar_date=TODAY - timedelta(days=2),
        activity_type="running",
    )
    paths.fit_dir.mkdir(parents=True, exist_ok=True)
    (paths.fit_dir / "1.zip").write_bytes(zipped(simple_run(seconds=60)))
    db.set_fit_path(conn, 1, "1.zip")
    conn.commit()
    conn.close()


@pytest.mark.skipif(sys.platform == "win32", reason="stdio subprocess test is POSIX-only for now")
def test_every_tool_over_stdio(isolated_home: Path) -> None:
    seed()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "garmin_connect_personal_mcp", "serve", "--no-auto-sync"],
        env={
            "HOME": str(isolated_home),
            "GARMIN_MCP_DATA_DIR": os.environ["GARMIN_MCP_DATA_DIR"],
            "PATH": os.environ.get("PATH", ""),
        },
    )

    async def main() -> tuple[dict[str, Any], dict[str, Any]]:
        async with Client(params, read_timeout_seconds=60) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            results = {}
            for name in tools:
                results[name] = await client.call_tool(name, CALLS.get(name, {}))
            return tools, results

    tools, results = anyio.run(main)
    assert set(tools) == set(CALLS), "add a call for each new tool to CALLS"

    for name, result in results.items():
        text = result.content[0].text  # type: ignore[union-attr]
        if name in EXPECTED_ERRORS:
            assert result.is_error and EXPECTED_ERRORS[name] in text, (name, text)
            continue
        assert not result.is_error, (name, text)
        schema = tools[name].output_schema
        assert schema is not None, f"{name} declares no output schema"
        jsonschema.validate(result.structured_content, schema)
        if name != "garmin_show_chart":  # its text is a summary for the model, by design
            assert json.loads(text) == result.structured_content, name

    stream = results["garmin_get_activity_stream"].structured_content
    assert stream["point_count"] == 12 and stream["heart_rate_bpm"]
    assert results["garmin_query"].structured_content["rows"][0][0] == 20
