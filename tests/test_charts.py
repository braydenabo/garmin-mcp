"""Chart data from synthetic records."""

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from garmin_connect_personal_mcp import charts, db

END = date(2026, 3, 15)  # a Sunday


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return db.connect(tmp_path / "garmin.db")


def store(conn: sqlite3.Connection, dataset: str, when: date, raw: dict[str, Any]) -> None:
    db.upsert_daily(conn, dataset, when, {"calendarDate": when.isoformat(), **raw})


def night(deep: int, light: int, rem: int, awake: int) -> dict[str, Any]:
    return {
        "sleepTimeSeconds": (deep + light + rem) * 60,
        "deepSleepSeconds": deep * 60,
        "lightSleepSeconds": light * 60,
        "remSleepSeconds": rem * 60,
        "awakeSleepSeconds": awake * 60,
    }


def test_sleep_stages_are_stacked_hours_with_gaps(conn: sqlite3.Connection) -> None:
    start = END - timedelta(days=6)
    for i in range(7):
        if i != 3:  # one night not worn
            store(conn, "sleep", start + timedelta(days=i), night(90, 240, 120, 30))
    data = charts.chart_data(conn, "sleep_stages", start, END, "day")
    assert data.kind == "stacked" and data.unit == "hours"
    assert data.labels[0] == start.isoformat() and len(data.labels) == 7
    assert [s.name for s in data.series] == ["Deep", "Light", "REM", "Awake"]
    deep = data.series[0].values
    assert deep[0] == 1.5 and deep[3] is None
    assert data.days_with_data == 6
    assert data.average == 8.0  # 1.5 + 4 + 2 + 0.5


def test_levels_are_lines_totals_are_bars(conn: sqlite3.Connection) -> None:
    store(
        conn,
        "daily_summary",
        END,
        {"heartRate": {"restingValue": 50, "minValue": 45}, "movement": {"steps": {"value": 9000}}},
    )
    hr = charts.chart_data(conn, "resting_hr_bpm", END, END, "day")
    steps = charts.chart_data(conn, "steps", END, END, "day")
    assert hr.kind == "line" and hr.unit == "bpm" and hr.series[0].values == [50.0]
    assert steps.kind == "bar" and steps.series[0].values == [9000.0]


def test_weekly_buckets_average_days_with_data(conn: sqlite3.Connection) -> None:
    for i, hours in enumerate([6, 8, 7]):
        store(
            conn, "sleep", date(2026, 3, 9) + timedelta(days=i), {"sleepTimeSeconds": hours * 3600}
        )
    data = charts.chart_data(conn, "sleep_hours", date(2026, 3, 1), END, "week")
    assert data.labels == ["2026-02-23", "2026-03-02", "2026-03-09"]
    assert data.series[0].values == [None, None, 7.0]
    assert any("weekly mean" in n for n in data.notes)


def test_empty_range_explains(conn: sqlite3.Connection) -> None:
    data = charts.chart_data(conn, "steps", END - timedelta(days=6), END, "day")
    assert data.series[0].values == [None] * 7 and data.average is None
    assert "garmin_describe_data" in data.notes[0]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("nope", END, END, "day"), "Unknown chart metric"),
        (("steps", END, END - timedelta(days=1), "day"), "before start"),
        (("steps", END - timedelta(days=500), END, "day"), "bucket='week'"),
    ],
)
def test_errors(conn: sqlite3.Connection, args: tuple[Any, ...], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        charts.chart_data(conn, *args)


def test_chart_metrics_cover_every_numeric_metric() -> None:
    from garmin_connect_personal_mcp import metrics

    assert charts.CHART_METRICS[0] == "sleep_stages"
    assert set(charts.CHART_METRICS[1:]) == set(metrics.NUMERIC_METRICS)
