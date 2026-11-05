"""Baselines against the user's own history, and the garmin_get_baseline tool."""

import json
from datetime import date, timedelta
from typing import Any

import anyio
import pytest
from mcp import Client

from garmin_connect_personal_mcp import analysis, config, db
from garmin_connect_personal_mcp.server import build_server

TODAY = date(2026, 3, 31)


def compare(value: float | None, history: list[float], **kwargs: Any) -> analysis.Baseline:
    return analysis.compare(
        "resting_hr_bpm",
        "bpm",
        "Resting heart rate",
        kwargs.pop("day", TODAY - timedelta(1)),
        kwargs.pop("days", 30),
        value,
        history,
        today=TODAY,
    )


# --- the statistics


def test_baseline_window_excludes_the_day() -> None:
    assert analysis.baseline_window(date(2026, 3, 31), 30) == (date(2026, 3, 1), date(2026, 3, 30))


def test_stats_and_position() -> None:
    history = [50.0, 52.0, 54.0, 56.0, 58.0, 50.0, 52.0, 54.0, 56.0, 58.0]
    b = compare(62, history)
    assert b.baseline.mean == 54 and b.baseline.median == 54
    assert b.baseline.std_dev == 2.98
    assert (b.baseline.usual_low, b.baseline.usual_high) == (51.02, 56.98)
    assert (b.baseline.min, b.baseline.max) == (50, 58)
    assert b.difference == 8 and b.difference_pct == 14.81 and b.z_score == 2.68
    assert b.percentile == 100.0 and b.position == "above"
    assert compare(54, history).position == "within"
    assert compare(54, history).percentile == 50.0  # 4 lower, 2 ties
    assert compare(49, history).position == "below"


def test_flat_history_has_no_z_score() -> None:
    b = compare(60, [55.0] * 10)
    assert b.baseline.std_dev == 0 and b.z_score is None and b.position == "above"


def test_too_few_days_for_a_baseline() -> None:
    b = compare(60, [55.0, 56.0])
    assert b.baseline.mean is None and b.baseline.max == 56
    assert b.position is None and b.difference is None
    assert "too few" in b.notes[0] and "garmin_sync" in b.notes[0]


def test_sparse_baseline_is_flagged() -> None:
    assert "rough" in compare(60, [55.0, 56.0, 57.0]).notes[0]


def test_missing_value_still_shows_the_baseline() -> None:
    b = compare(None, [55.0] * 10)
    assert b.value is None and b.baseline.mean == 55 and b.position is None
    assert any("No resting_hr_bpm" in n for n in b.notes)


def test_today_warns_about_running_totals() -> None:
    assert any("Today isn't over" in n for n in compare(1, [1.0] * 5, day=TODAY).notes)
    assert not any("Today" in n for n in compare(1, [1.0] * 5).notes)


def test_always_says_it_is_not_medical() -> None:
    assert "not a medical" in compare(60, [55.0] * 10).notes[-1]


# --- the tool


def seed_sleep(hours: dict[date, float]) -> None:
    conn = db.connect(config.data_paths().db)
    for day, value in hours.items():
        db.upsert_daily(conn, "sleep", day, {"sleepTimeSeconds": int(value * 3600)})
        db.record_sync(conn, "sleep", day, "ok")
    conn.commit()
    conn.close()


def call(args: dict[str, Any]) -> tuple[bool, Any, str]:
    async def go() -> tuple[bool, Any, str]:
        server = build_server(config.data_paths(), today=lambda: TODAY)
        async with Client(server) as client:
            result = await client.call_tool("garmin_get_baseline", args)
            text = result.content[0].text  # type: ignore[union-attr]
            return result.is_error, result.structured_content, text

    return anyio.run(go)


def test_tool_compares_a_day_with_the_days_before() -> None:
    history = {TODAY - timedelta(days=n): 7.0 + (n % 3) * 0.5 for n in range(1, 40)}
    seed_sleep({**history, TODAY: 5.0})
    error, result, text = call({"metric": "sleep_hours"})
    assert not error, text
    assert json.loads(text) == result
    assert result["date"] == "2026-03-31" and result["value"] == 5.0
    assert result["baseline_start"] == "2026-03-01" and result["baseline_end"] == "2026-03-30"
    assert result["baseline"]["days_with_data"] == 30  # day 31+ back is outside the window
    assert result["position"] == "below"
    assert result["unit"] == "hours"


def test_tool_days_and_date() -> None:
    seed_sleep({TODAY - timedelta(days=n): 7.0 for n in range(1, 40)})
    _, result, _ = call({"metric": "sleep_hours", "date": "yesterday", "days": 7})
    assert result["date"] == "2026-03-30" and result["baseline"]["days_with_data"] == 7


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"metric": "sleep_hrs"}, "Did you mean sleep_hours"),
        ({"metric": "sleep_hours", "days": 6}, "between 7 and 365"),
        ({"metric": "sleep_hours", "days": 366}, "between 7 and 365"),
        ({"metric": "sleep_hours", "date": "soon"}, "Invalid date"),
    ],
)
def test_tool_errors(args: dict[str, Any], message: str) -> None:
    seed_sleep({})
    error, _, text = call(args)
    assert error and message in text


def test_tool_without_archive_suggests_sync() -> None:
    error, _, text = call({"metric": "sleep_hours"})
    assert error and "sync" in text
