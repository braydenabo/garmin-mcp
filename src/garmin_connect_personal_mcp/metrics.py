"""Readable daily metrics derived from the raw archive.

One registry, METRICS, defines every metric the MCP tools expose: its name
(with the unit in it), the dataset it comes from, how to extract it from
Garmin's raw JSON, and a description. Tools read from here, so they can't
disagree about names, units, or what counts as data.

"No data" is None, never 0. Garmin returns placeholder records on days the
watch wasn't worn and uses negative codes (e.g. stress -1/-2) for "not enough
data"; both become None here.
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

Value = float | int | str | None


def _get(raw: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(raw, dict):
            return None
        raw = raw.get(key)
    return raw


def _number(raw: Any, *path: str, scale: float = 1.0, digits: int = 1) -> float | int | None:
    """A non-negative number at path, scaled; None if missing or a negative 'no data' code."""
    value = _get(raw, *path)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    if scale == 1.0 and isinstance(value, int):
        return value
    scaled = round(value * scale, digits)
    return int(scaled) if digits == 0 else scaled


def _text(raw: Any, *path: str) -> str | None:
    """A label at path; Garmin's "NONE" means no label."""
    value = _get(raw, *path)
    return value if isinstance(value, str) and value and value != "NONE" else None


# Garmin occasionally reports a resting heart rate no person has at rest (seen: 148 bpm on a
# day whose minimum was 80). Values above this are dropped rather than shown as fact.
MAX_PLAUSIBLE_RESTING_HR_BPM = 120


def _resting_hr(raw: Any) -> float | int | None:
    value = _number(raw, "heartRate", "restingValue")
    return value if value is None or value <= MAX_PLAUSIBLE_RESTING_HR_BPM else None


def _local_time(raw: Any, *path: str) -> str | None:
    """Garmin's '...Local' epoch milliseconds are local wall-clock time encoded as UTC."""
    value = _get(raw, *path)
    if not isinstance(value, int | float) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, UTC).strftime("%Y-%m-%dT%H:%M")


@dataclass(frozen=True)
class Metric:
    dataset: str
    unit: str
    description: str
    extract: Callable[[Any], Value]
    numeric: bool = True


# The has-data rule for each dataset: a stored record only counts as data if this holds.
HAS_DATA: dict[str, Callable[[Any], bool]] = {
    "daily_summary": lambda r: (
        _number(r, "heartRate", "minValue") is not None
        or bool(_number(r, "movement", "steps", "value"))
    ),
    "sleep": lambda r: bool(_number(r, "sleepTimeSeconds")),
    "hrv": lambda r: _number(r, "lastNightAvg") is not None,
}

METRICS: dict[str, Metric] = {
    # sleep
    "sleep_hours": Metric(
        "sleep",
        "hours",
        "Total sleep",
        lambda r: _number(r, "sleepTimeSeconds", scale=1 / 3600, digits=2),
    ),
    "deep_sleep_minutes": Metric(
        "sleep",
        "minutes",
        "Deep sleep",
        lambda r: _number(r, "deepSleepSeconds", scale=1 / 60, digits=0),
    ),
    "light_sleep_minutes": Metric(
        "sleep",
        "minutes",
        "Light sleep",
        lambda r: _number(r, "lightSleepSeconds", scale=1 / 60, digits=0),
    ),
    "rem_sleep_minutes": Metric(
        "sleep",
        "minutes",
        "REM sleep",
        lambda r: _number(r, "remSleepSeconds", scale=1 / 60, digits=0),
    ),
    "awake_minutes": Metric(
        "sleep",
        "minutes",
        "Time awake during the sleep window",
        lambda r: _number(r, "awakeSleepSeconds", scale=1 / 60, digits=0),
    ),
    "sleep_score": Metric(
        "sleep",
        "0-100",
        "Garmin sleep score",
        lambda r: _number(r, "sleepScores", "overall", "value"),
    ),
    "sleep_avg_hr_bpm": Metric(
        "sleep", "bpm", "Average heart rate while asleep", lambda r: _number(r, "avgHeartRate")
    ),
    "sleep_avg_respiration_brpm": Metric(
        "sleep",
        "breaths/min",
        "Average breathing rate while asleep",
        lambda r: _number(r, "averageRespirationValue"),
    ),
    "sleep_start_local": Metric(
        "sleep",
        "local time",
        "When sleep started",
        lambda r: _local_time(r, "sleepStartTimestampLocal"),
        numeric=False,
    ),
    "sleep_end_local": Metric(
        "sleep",
        "local time",
        "When sleep ended",
        lambda r: _local_time(r, "sleepEndTimestampLocal"),
        numeric=False,
    ),
    # heart rate variability
    "hrv_last_night_ms": Metric(
        "hrv", "ms", "Overnight average HRV", lambda r: _number(r, "lastNightAvg")
    ),
    "hrv_weekly_avg_ms": Metric(
        "hrv", "ms", "7-day average HRV", lambda r: _number(r, "weeklyAvg")
    ),
    "hrv_baseline_low_ms": Metric(
        "hrv",
        "ms",
        "Lower bound of the personal balanced HRV range",
        lambda r: _number(r, "baseline", "balancedLow"),
    ),
    "hrv_baseline_high_ms": Metric(
        "hrv",
        "ms",
        "Upper bound of the personal balanced HRV range",
        lambda r: _number(r, "baseline", "balancedUpper"),
    ),
    "hrv_status": Metric(
        "hrv",
        "label",
        "Garmin HRV status (e.g. BALANCED, UNBALANCED, LOW)",
        lambda r: _text(r, "status"),
        numeric=False,
    ),
    # daily summary
    "resting_hr_bpm": Metric(
        "daily_summary",
        "bpm",
        "Resting heart rate",
        _resting_hr,
    ),
    "min_hr_bpm": Metric(
        "daily_summary",
        "bpm",
        "Lowest heart rate of the day",
        lambda r: _number(r, "heartRate", "minValue"),
    ),
    "max_hr_bpm": Metric(
        "daily_summary",
        "bpm",
        "Highest heart rate of the day",
        lambda r: _number(r, "heartRate", "maxValue"),
    ),
    "steps": Metric(
        "daily_summary", "steps", "Steps", lambda r: _number(r, "movement", "steps", "value")
    ),
    "step_goal": Metric(
        "daily_summary",
        "steps",
        "Daily step goal",
        lambda r: _number(r, "movement", "steps", "goal"),
    ),
    "distance_km": Metric(
        "daily_summary",
        "km",
        "Distance walked or run during the day",
        lambda r: _number(r, "movement", "steps", "distanceInMeters", scale=1 / 1000, digits=2),
    ),
    "floors_climbed": Metric(
        "daily_summary",
        "floors",
        "Floors climbed",
        lambda r: _number(r, "movement", "floorsAscended", "value"),
    ),
    "stress_avg": Metric(
        "daily_summary", "0-100", "Average stress level", lambda r: _number(r, "stress", "avgLevel")
    ),
    "stress_max": Metric(
        "daily_summary", "0-100", "Highest stress level", lambda r: _number(r, "stress", "maxLevel")
    ),
    "body_battery_high": Metric(
        "daily_summary",
        "0-100",
        "Highest Body Battery",
        lambda r: _number(r, "bodyBattery", "maxValue"),
    ),
    "body_battery_low": Metric(
        "daily_summary",
        "0-100",
        "Lowest Body Battery",
        lambda r: _number(r, "bodyBattery", "minValue"),
    ),
    "body_battery_charged": Metric(
        "daily_summary",
        "points",
        "Body Battery gained",
        lambda r: _number(r, "bodyBattery", "chargedValue"),
    ),
    "body_battery_drained": Metric(
        "daily_summary",
        "points",
        "Body Battery used",
        lambda r: _number(r, "bodyBattery", "drainedValue"),
    ),
    "calories_total_kcal": Metric(
        "daily_summary",
        "kcal",
        "Total calories burned",
        lambda r: _number(r, "calories", "burnedTotal"),
    ),
    "calories_active_kcal": Metric(
        "daily_summary",
        "kcal",
        "Active calories burned",
        lambda r: _number(r, "calories", "burnedActive"),
    ),
    "intensity_minutes_moderate": Metric(
        "daily_summary",
        "minutes",
        "Moderate intensity minutes",
        lambda r: _number(r, "intensityMinutes", "moderate"),
    ),
    "intensity_minutes_vigorous": Metric(
        "daily_summary",
        "minutes",
        "Vigorous intensity minutes",
        lambda r: _number(r, "intensityMinutes", "vigorous"),
    ),
    "respiration_avg_brpm": Metric(
        "daily_summary",
        "breaths/min",
        "Average breathing rate",
        lambda r: _number(r, "respiration", "avgValue"),
    ),
}

DATASETS = sorted({m.dataset for m in METRICS.values()})
NUMERIC_METRICS = sorted(name for name, m in METRICS.items() if m.numeric)


def has_data(dataset: str, raw: Any) -> bool:
    rule = HAS_DATA.get(dataset)
    return raw is not None and (rule(raw) if rule else True)


def _raw_rows(
    conn: sqlite3.Connection, dataset: str, start: date, end: date
) -> list[tuple[date, Any]]:
    rows = conn.execute(
        "SELECT calendar_date, raw_json FROM daily_raw"
        " WHERE dataset = ? AND calendar_date BETWEEN ? AND ? ORDER BY calendar_date",
        (dataset, start.isoformat(), end.isoformat()),
    ).fetchall()
    return [(date.fromisoformat(d), json.loads(r)) for d, r in rows]


def day(conn: sqlite3.Connection, when: date) -> dict[str, dict[str, Value] | None]:
    """Every metric for one day, grouped by dataset. A dataset without real data is None."""
    out: dict[str, dict[str, Value] | None] = {}
    for dataset in DATASETS:
        rows = _raw_rows(conn, dataset, when, when)
        raw = rows[0][1] if rows else None
        if not has_data(dataset, raw):
            out[dataset] = None
            continue
        out[dataset] = {
            name: metric.extract(raw)
            for name, metric in METRICS.items()
            if metric.dataset == dataset
        }
    return out


def series(
    conn: sqlite3.Connection, metric: str, start: date, end: date
) -> list[tuple[date, float | int | str]]:
    """(date, value) for each day in range with real data for this metric, oldest first."""
    spec = METRICS[metric]
    points = []
    for when, raw in _raw_rows(conn, spec.dataset, start, end):
        if has_data(spec.dataset, raw):
            value = spec.extract(raw)
            if value is not None:
                points.append((when, value))
    return points


@dataclass(frozen=True)
class Coverage:
    dataset: str
    days_with_data: int
    first_date: str | None
    last_date: str | None
    last_synced: str | None


def coverage(conn: sqlite3.Connection) -> list[Coverage]:
    """Per dataset: how many days hold real data, the date range, and the last sync."""
    out = []
    for dataset in DATASETS:
        dates = [
            when
            for when, raw in _raw_rows(conn, dataset, date.min, date.max)
            if has_data(dataset, raw)
        ]
        synced = conn.execute(
            "SELECT MAX(calendar_date) FROM sync_log WHERE dataset = ? AND status != 'error'",
            (dataset,),
        ).fetchone()[0]
        out.append(
            Coverage(
                dataset,
                len(dates),
                dates[0].isoformat() if dates else None,
                dates[-1].isoformat() if dates else None,
                synced,
            )
        )
    return out


def sync_state(conn: sqlite3.Connection, dataset: str, when: date) -> str:
    """'ok', 'empty', 'error', or 'never' for one dataset and day."""
    row = conn.execute(
        "SELECT status FROM sync_log WHERE dataset = ? AND calendar_date = ?",
        (dataset, when.isoformat()),
    ).fetchone()
    return "never" if row is None else str(row[0])
