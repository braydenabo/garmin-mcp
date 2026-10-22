"""Metrics from synthetic records shaped like real Garmin responses."""

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from garmin_connect_personal_mcp import db, metrics

DAY = date(2026, 3, 10)


def summary(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "calendarDate": DAY.isoformat(),
        "heartRate": {"restingValue": 52, "minValue": 48, "maxValue": 160},
        "movement": {
            "steps": {"value": 9000, "goal": 8000, "distanceInMeters": 7123.4},
            "floorsAscended": {"value": 12},
        },
        "stress": {"avgLevel": 31, "maxLevel": 90},
        "bodyBattery": {"maxValue": 88, "minValue": 20, "chargedValue": 60, "drainedValue": 70},
        "calories": {"burnedTotal": 2500, "burnedActive": 600},
        "intensityMinutes": {"moderate": 20, "vigorous": 15},
        "respiration": {"avgValue": 14},
    }
    raw.update(overrides)
    return raw


def sleep(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "calendarDate": DAY.isoformat(),
        "sleepTimeSeconds": 27000,
        "deepSleepSeconds": 5400,
        "lightSleepSeconds": 14400,
        "remSleepSeconds": 7200,
        "awakeSleepSeconds": 900,
        "sleepScores": {"overall": {"value": 82}},
        "avgHeartRate": 51.5,
        "averageRespirationValue": 13.9,
        # Local wall-clock epoch millis: 2026-03-09 23:15 and 2026-03-10 06:45.
        "sleepStartTimestampLocal": 1773098100000,
        "sleepEndTimestampLocal": 1773125100000,
    }
    raw.update(overrides)
    return raw


def hrv(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "calendarDate": DAY.isoformat(),
        "lastNightAvg": 48,
        "weeklyAvg": 50,
        "status": "BALANCED",
        "baseline": {"balancedLow": 42, "balancedUpper": 58},
    }
    raw.update(overrides)
    return raw


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return db.connect(tmp_path / "garmin.db")


def store(conn: sqlite3.Connection, dataset: str, when: date, raw: Any) -> None:
    db.upsert_daily(conn, dataset, when, raw)
    db.record_sync(conn, dataset, when, "ok")


# --- extraction and units


def test_day_extracts_every_metric_with_units(conn: sqlite3.Connection) -> None:
    store(conn, "daily_summary", DAY, summary())
    store(conn, "sleep", DAY, sleep())
    store(conn, "hrv", DAY, hrv())
    result = metrics.day(conn, DAY)
    assert result["sleep"] == {
        "sleep_hours": 7.5,
        "deep_sleep_minutes": 90,
        "light_sleep_minutes": 240,
        "rem_sleep_minutes": 120,
        "awake_minutes": 15,
        "sleep_score": 82,
        "sleep_avg_hr_bpm": 51.5,
        "sleep_avg_respiration_brpm": 13.9,
        "sleep_start_local": "2026-03-09T23:15",
        "sleep_end_local": "2026-03-10T06:45",
    }
    assert result["hrv"] == {
        "hrv_last_night_ms": 48,
        "hrv_weekly_avg_ms": 50,
        "hrv_baseline_low_ms": 42,
        "hrv_baseline_high_ms": 58,
        "hrv_status": "BALANCED",
    }
    daily = result["daily_summary"]
    assert daily is not None
    assert daily["resting_hr_bpm"] == 52
    assert daily["distance_km"] == 7.12
    assert daily["steps"] == 9000
    assert daily["body_battery_low"] == 20


def test_every_metric_is_covered_by_the_sample_records(conn: sqlite3.Connection) -> None:
    store(conn, "daily_summary", DAY, summary())
    store(conn, "sleep", DAY, sleep())
    store(conn, "hrv", DAY, hrv())
    result = metrics.day(conn, DAY)
    values = {k: v for group in result.values() if group for k, v in group.items()}
    assert set(values) == set(metrics.METRICS)
    assert None not in values.values()


def test_metric_names_carry_units() -> None:
    unitless_ok = {"steps", "step_goal", "floors_climbed", "sleep_score", "hrv_status"}
    for name, metric in metrics.METRICS.items():
        if name in unitless_ok or name.startswith(("stress_", "body_battery_", "intensity_")):
            continue
        assert name.endswith(
            ("_hours", "_minutes", "_bpm", "_brpm", "_ms", "_km", "_kcal", "_local")
        ), name
        assert metric.unit and metric.description


# --- "no data" is None, never zero


def test_no_wear_placeholders_are_not_data(conn: sqlite3.Connection) -> None:
    store(conn, "daily_summary", DAY, {"calendarDate": DAY.isoformat(), "heartRate": {}})
    store(conn, "sleep", DAY, sleep(sleepTimeSeconds=None, deepSleepSeconds=None))
    store(conn, "hrv", DAY, hrv(lastNightAvg=None))
    assert metrics.day(conn, DAY) == {"daily_summary": None, "hrv": None, "sleep": None}
    assert metrics.series(conn, "sleep_hours", DAY, DAY) == []


def test_unsynced_day_is_none(conn: sqlite3.Connection) -> None:
    assert metrics.day(conn, DAY) == {"daily_summary": None, "hrv": None, "sleep": None}


@pytest.mark.parametrize("code", [-1, -2])
def test_negative_codes_mean_no_data(conn: sqlite3.Connection, code: int) -> None:
    store(conn, "daily_summary", DAY, summary(stress={"avgLevel": code, "maxLevel": code}))
    daily = metrics.day(conn, DAY)["daily_summary"]
    assert daily is not None
    assert daily["stress_avg"] is None and daily["stress_max"] is None


def test_implausible_resting_hr_is_dropped(conn: sqlite3.Connection) -> None:
    store(conn, "daily_summary", DAY, summary(heartRate={"restingValue": 148, "minValue": 80}))
    daily = metrics.day(conn, DAY)["daily_summary"]
    assert daily is not None
    assert daily["resting_hr_bpm"] is None
    assert daily["min_hr_bpm"] == 80  # the rest of the day is still reported


def test_none_label_is_none(conn: sqlite3.Connection) -> None:
    store(conn, "hrv", DAY, hrv(status="NONE"))
    group = metrics.day(conn, DAY)["hrv"]
    assert group is not None and group["hrv_status"] is None


def test_booleans_and_strings_are_not_numbers(conn: sqlite3.Connection) -> None:
    store(conn, "sleep", DAY, sleep(sleepScores={"overall": {"value": True}}, avgHeartRate="52"))
    group = metrics.day(conn, DAY)["sleep"]
    assert group is not None
    assert group["sleep_score"] is None and group["sleep_avg_hr_bpm"] is None


# --- series and coverage


def test_series_skips_days_without_data(conn: sqlite3.Connection) -> None:
    for i in range(5):
        when = DAY + timedelta(days=i)
        raw = sleep(sleepTimeSeconds=None) if i == 2 else sleep(sleepTimeSeconds=3600 * (6 + i))
        store(conn, "sleep", when, raw)
    points = metrics.series(conn, "sleep_hours", DAY, DAY + timedelta(days=4))
    assert [v for _, v in points] == [6.0, 7.0, 9.0, 10.0]
    assert metrics.series(conn, "sleep_hours", DAY, DAY + timedelta(days=1))[-1][1] == 7.0


def test_coverage(conn: sqlite3.Connection) -> None:
    store(conn, "sleep", DAY, sleep())
    store(conn, "sleep", DAY + timedelta(days=1), sleep(sleepTimeSeconds=None))
    store(conn, "sleep", DAY + timedelta(days=2), sleep())
    cover = {c.dataset: c for c in metrics.coverage(conn)}
    assert cover["sleep"] == metrics.Coverage(
        "sleep",
        2,
        DAY.isoformat(),
        (DAY + timedelta(days=2)).isoformat(),
        (DAY + timedelta(days=2)).isoformat(),
    )
    assert cover["hrv"] == metrics.Coverage("hrv", 0, None, None, None)


def test_sync_state(conn: sqlite3.Connection) -> None:
    assert metrics.sync_state(conn, "sleep", DAY) == "never"
    db.record_sync(conn, "sleep", DAY, "empty")
    assert metrics.sync_state(conn, "sleep", DAY) == "empty"
