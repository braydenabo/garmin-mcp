"""An activity's samples over time, averaged into evenly spaced points.

The FIT file has a sample about every second; a model rarely needs that, so
samples are grouped into fixed-length intervals of elapsed time and averaged.
Intervals with no samples (the timer was paused) are left out, so gaps show
as jumps in elapsed_minutes. Pace is derived from the averaged speed, never
averaged itself. Units are in the field names; no GPS is ever included.
"""

import math
from collections.abc import Callable, Sequence
from statistics import fmean

from pydantic import BaseModel, Field

from garmin_connect_personal_mcp.activities import FOOT_SPORTS
from garmin_connect_personal_mcp.fit import Sample

AUTO_INTERVALS = (1, 2, 5, 10, 15, 20, 30, 60, 120, 300, 600)
TARGET_POINTS = 300  # auto interval picks the finest one with at most this many points
MAX_POINTS = 600
MAX_INTERVAL_SECONDS = 600
MIN_PACE_SPEED_MPS = 0.5  # slower than this is standing still: pace would be meaningless


class Stream(BaseModel):
    activity_id: int
    activity_type: str | None
    interval_seconds: int = Field(description="Each point averages this many seconds")
    start_minute: float = Field(description="Elapsed minute where the window starts")
    end_minute: float = Field(description="Elapsed minute where the window ends")
    total_minutes: float = Field(description="Elapsed minutes from first to last sample")
    point_count: int
    elapsed_minutes: list[float] = Field(description="Start of each point, from the first sample")
    heart_rate_bpm: list[float | None] | None = Field(description="null if never recorded")
    speed_kmh: list[float | None] | None
    pace_min_per_km: list[float | None] | None = Field(description="On-foot sports only")
    cadence_spm: list[float | None] | None = Field(description="Steps per minute, on-foot sports")
    cadence_rpm: list[float | None] | None = Field(
        description="Other sports: pedal revolutions or swim strokes per minute"
    )
    power_w: list[float | None] | None
    altitude_m: list[float | None] | None
    distance_km: list[float | None] | None = Field(description="Cumulative, at each point's end")
    temperature_c: list[float | None] | None = Field(description="Device sensor, not air")
    notes: list[str]


def is_foot_sport(activity_type: str | None) -> bool:
    return bool(activity_type) and any(s in str(activity_type) for s in FOOT_SPORTS)


def auto_interval(duration_seconds: float) -> int:
    for interval in AUTO_INTERVALS:
        if duration_seconds / interval <= TARGET_POINTS:
            return interval
    return AUTO_INTERVALS[-1]


def _mean(values: list[float | None], digits: int) -> float | None:
    present = [v for v in values if v is not None]
    return round(fmean(present), digits) if present else None


def _last(values: list[float | None], digits: int) -> float | None:
    present = [v for v in values if v is not None]
    return round(present[-1], digits) if present else None


def _column(
    buckets: Sequence[list[Sample]],
    get: Callable[[Sample], float | None],
    combine: Callable[[list[float | None], int], float | None],
    digits: int,
) -> list[float | None] | None:
    column = [combine([get(s) for s in bucket], digits) for bucket in buckets]
    return column if any(v is not None for v in column) else None


def build(
    activity_id: int,
    activity_type: str | None,
    samples: Sequence[Sample],
    *,
    interval_seconds: int = 0,
    start_minute: float = 0,
    end_minute: float = 0,
) -> Stream:
    """Average samples into points. interval 0 picks one; end_minute 0 means the end."""
    if interval_seconds < 0 or interval_seconds > MAX_INTERVAL_SECONDS:
        raise ValueError(f"interval_seconds must be 0 (automatic) or 1-{MAX_INTERVAL_SECONDS}.")
    if start_minute < 0 or end_minute < 0:
        raise ValueError("start_minute and end_minute can't be negative.")
    if end_minute and end_minute <= start_minute:
        raise ValueError("end_minute must be after start_minute (or 0 for the end).")

    ordered = sorted(samples, key=lambda s: s.timestamp)
    first = ordered[0].timestamp if ordered else 0
    total = (ordered[-1].timestamp - first) if ordered else 0
    window_start = start_minute * 60
    window_end = end_minute * 60 if end_minute else total
    window = [s for s in ordered if window_start <= s.timestamp - first <= window_end]

    notes: list[str] = []
    if not ordered:
        notes.append("This activity's FIT file has no samples (for example, a manual entry).")
    elif not window:
        notes.append(
            f"No samples in that window; the activity lasts {total / 60:.1f} elapsed minutes."
        )

    interval = interval_seconds or auto_interval(max(window_end - window_start, 1))
    span = max(window_end - window_start, 0)
    if math.floor(span / interval) + 1 > MAX_POINTS:
        raise ValueError(
            f"That would be over {MAX_POINTS} points. Use a larger interval_seconds, or a "
            "shorter window with start_minute and end_minute."
        )

    grouped: dict[int, list[Sample]] = {}
    for s in window:
        grouped.setdefault(int((s.timestamp - first - window_start) // interval), []).append(s)
    keys = sorted(grouped)
    buckets = [grouped[k] for k in keys]
    elapsed = [round((window_start + k * interval) / 60, 2) for k in keys]

    foot = is_foot_sport(activity_type)
    speed = _column(buckets, lambda s: s.speed_mps, _mean, 3)
    cadence = _column(buckets, lambda s: s.cadence, _mean, 1)
    pace = None
    if foot and speed is not None:
        pace = [
            round(1000 / v / 60, 2) if v is not None and v >= MIN_PACE_SPEED_MPS else None
            for v in speed
        ]
    if foot and cadence is not None:
        cadence = [None if v is None else round(v * 2) for v in cadence]  # strides -> steps

    distance = _column(buckets, lambda s: s.distance_m, _last, 0)
    stream = Stream(
        activity_id=activity_id,
        activity_type=activity_type,
        interval_seconds=interval,
        start_minute=round(window_start / 60, 2),
        end_minute=round(window_end / 60, 2),
        total_minutes=round(total / 60, 2),
        point_count=len(buckets),
        elapsed_minutes=elapsed,
        heart_rate_bpm=_column(buckets, lambda s: s.heart_rate_bpm, _mean, 0),
        speed_kmh=None
        if speed is None
        else [None if v is None else round(v * 3.6, 1) for v in speed],
        pace_min_per_km=pace,
        cadence_spm=cadence if foot else None,
        cadence_rpm=None if foot else cadence,
        power_w=_column(buckets, lambda s: s.power_w, _mean, 0),
        altitude_m=_column(buckets, lambda s: s.altitude_m, _mean, 1),
        distance_km=None
        if distance is None
        else [None if v is None else round(v / 1000, 2) for v in distance],
        temperature_c=_column(buckets, lambda s: s.temperature_c, _mean, 0),
        notes=notes,
    )
    if buckets:
        recorded = {
            "heart rate": stream.heart_rate_bpm,
            "speed": speed,
            "cadence": cadence,
            "power": stream.power_w,
            "altitude": stream.altitude_m,
        }
        missing = [name for name, column in recorded.items() if column is None]
        if missing:
            stream.notes.append(f"Not recorded for this activity: {', '.join(missing)}.")
        if interval > 1:
            stream.notes.append(f"Each point is the average of {interval} seconds of samples.")
    return stream
