"""Readable activity summaries from the raw archive.

Garmin's activity records use seconds, meters and meters per second; these
helpers convert to minutes, km, km/h and min/km, with the unit in each field
name. They also leave out personal details the raw record carries (the
owner's name, profile photo URLs, and start/end GPS coordinates), so a remote
assistant isn't sent where the user lives. The archive keeps everything.
"""

import json
import sqlite3
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

FOOT_SPORTS = ("running", "walking", "hiking")


def _num(raw: dict[str, Any], key: str, scale: float = 1.0, digits: int = 1) -> float | None:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return round(value * scale, digits)


def _int(raw: dict[str, Any], key: str) -> int | None:
    value = raw.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) and value else None


def _type(raw: dict[str, Any]) -> str | None:
    kind = raw.get("activityType")
    key = kind.get("typeKey") if isinstance(kind, dict) else None
    return key if isinstance(key, str) else None


def _start_local(raw: dict[str, Any]) -> str | None:
    text = _str(raw, "startTimeLocal")  # "YYYY-MM-DD HH:MM:SS"
    return text[:16].replace(" ", "T") if text and len(text) >= 16 else None


def _pace(raw: dict[str, Any], kind: str | None) -> float | None:
    speed = raw.get("averageSpeed")
    if not kind or not any(s in kind for s in FOOT_SPORTS):
        return None
    if not isinstance(speed, int | float) or speed <= 0:
        return None
    return round(1000 / speed / 60, 2)


class ActivitySummary(BaseModel):
    activity_id: int
    name: str | None
    activity_type: str | None
    start_local: str | None = Field(description="Local start time, YYYY-MM-DDTHH:MM")
    duration_minutes: float | None
    distance_km: float | None
    avg_pace_min_per_km: float | None = Field(description="On-foot sports only")
    avg_speed_kmh: float | None
    avg_hr_bpm: float | None
    max_hr_bpm: float | None
    calories_kcal: float | None
    elevation_gain_m: float | None
    training_load: float | None
    aerobic_training_effect: float | None = Field(description="0-5")


class Split(BaseModel):
    split_type: str | None
    distance_km: float | None
    duration_minutes: float | None
    avg_speed_kmh: float | None


class ActivityDetail(ActivitySummary):
    moving_minutes: float | None
    elapsed_minutes: float | None
    max_speed_kmh: float | None
    avg_power_w: float | None
    max_power_w: float | None
    normalized_power_w: float | None
    avg_run_cadence_spm: float | None
    elevation_loss_m: float | None
    steps: int | None
    lap_count: int | None
    anaerobic_training_effect: float | None = Field(description="0-5")
    training_effect_label: str | None
    hr_zone_minutes: list[float | None] = Field(description="Minutes in heart-rate zones 1-5")
    location_name: str | None = Field(description="Approximate place name, if Garmin has one")
    splits: list[Split]
    fit_file_saved: bool = Field(description="Whether the original FIT file is in the archive")


def summarize(activity_id: int, raw: dict[str, Any]) -> ActivitySummary:
    kind = _type(raw)
    return ActivitySummary(
        activity_id=activity_id,
        name=_str(raw, "activityName"),
        activity_type=kind,
        start_local=_start_local(raw),
        duration_minutes=_num(raw, "duration", 1 / 60),
        distance_km=_num(raw, "distance", 1 / 1000, 2),
        avg_pace_min_per_km=_pace(raw, kind),
        avg_speed_kmh=_num(raw, "averageSpeed", 3.6),
        avg_hr_bpm=_num(raw, "averageHR", digits=0),
        max_hr_bpm=_num(raw, "maxHR", digits=0),
        calories_kcal=_num(raw, "calories", digits=0),
        elevation_gain_m=_num(raw, "elevationGain", digits=0),
        training_load=_num(raw, "activityTrainingLoad"),
        aerobic_training_effect=_num(raw, "aerobicTrainingEffect"),
    )


def detail(activity_id: int, raw: dict[str, Any], fit_saved: bool) -> ActivityDetail:
    splits = [
        Split(
            split_type=_str(s, "splitType"),
            distance_km=_num(s, "distance", 1 / 1000, 2),
            duration_minutes=_num(s, "duration", 1 / 60),
            avg_speed_kmh=_num(s, "averageSpeed", 3.6),
        )
        for s in raw.get("splitSummaries") or []
        if isinstance(s, dict)
    ]
    return ActivityDetail(
        **summarize(activity_id, raw).model_dump(),
        moving_minutes=_num(raw, "movingDuration", 1 / 60),
        elapsed_minutes=_num(raw, "elapsedDuration", 1 / 60),
        max_speed_kmh=_num(raw, "maxSpeed", 3.6),
        avg_power_w=_num(raw, "avgPower", digits=0),
        max_power_w=_num(raw, "maxPower", digits=0),
        normalized_power_w=_num(raw, "normPower", digits=0),
        avg_run_cadence_spm=_num(raw, "averageRunningCadenceInStepsPerMinute", digits=0),
        elevation_loss_m=_num(raw, "elevationLoss", digits=0),
        steps=_int(raw, "steps"),
        lap_count=_int(raw, "lapCount"),
        anaerobic_training_effect=_num(raw, "anaerobicTrainingEffect"),
        training_effect_label=_str(raw, "trainingEffectLabel"),
        hr_zone_minutes=[_num(raw, f"hrTimeInZone_{z}", 1 / 60) for z in range(1, 6)],
        location_name=_str(raw, "locationName"),
        splits=splits,
        fit_file_saved=fit_saved,
    )


def list_rows(
    conn: sqlite3.Connection,
    start: date,
    end: date,
    activity_type: str,
    limit: int,
    offset: int,
) -> tuple[list[ActivitySummary], int]:
    """Newest first. Returns (page, total matching)."""
    where = "calendar_date BETWEEN ? AND ?"
    params: list[Any] = [start.isoformat(), end.isoformat()]
    if activity_type:
        where += " AND activity_type LIKE ?"
        params.append(f"%{activity_type.strip().lower()}%")
    total = conn.execute(f"SELECT COUNT(*) FROM activities WHERE {where}", params).fetchone()[0]  # noqa: S608
    rows = conn.execute(
        f"SELECT activity_id, raw_json FROM activities WHERE {where}"  # noqa: S608
        " ORDER BY start_time_utc DESC, activity_id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [summarize(r[0], json.loads(r[1])) for r in rows], int(total)


def get_row(conn: sqlite3.Connection, activity_id: int) -> ActivityDetail | None:
    row = conn.execute(
        "SELECT raw_json, fit_path FROM activities WHERE activity_id = ?", (activity_id,)
    ).fetchone()
    if row is None:
        return None
    return detail(activity_id, json.loads(row[0]), fit_saved=row[1] is not None)


def type_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT activity_type, COUNT(*) FROM activities WHERE activity_type IS NOT NULL"
        " GROUP BY activity_type ORDER BY COUNT(*) DESC, activity_type"
    ).fetchall()
    return {r[0]: r[1] for r in rows}
