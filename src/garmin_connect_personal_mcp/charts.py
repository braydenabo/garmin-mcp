"""Chart data for garmin_show_chart.

The full structure hydrates the chart view. UI-capable clients receive plotted
points through component-only metadata and expose just a summary to the model.
Clients without MCP Apps retain the full structured and text data fallback.
Days without data are null, so a chart shows a gap rather than a zero.
"""

import sqlite3
from datetime import date, timedelta
from statistics import fmean
from typing import Literal

from pydantic import BaseModel, Field

from garmin_connect_personal_mcp import metrics

SLEEP_STAGES = "sleep_stages"
CHART_METRICS = [SLEEP_STAGES, *metrics.NUMERIC_METRICS]
MAX_DAILY_CHART_POINTS = 400

# Totals read best as bars; levels (rates, scores, variability) as lines.
LINE_UNITS = {"bpm", "ms", "breaths/min", "0-100"}
STAGES = [  # (series name, metric, colour role for the view)
    ("Deep", "deep_sleep_minutes", "deep"),
    ("Light", "light_sleep_minutes", "light"),
    ("REM", "rem_sleep_minutes", "rem"),
    ("Awake", "awake_minutes", "awake"),
]


class Series(BaseModel):
    name: str
    role: str = Field(description="Colour hint for the chart view")
    values: list[float | None] = Field(description="One per label; null means no data")


class ChartData(BaseModel):
    title: str
    metric: str
    unit: str
    kind: Literal["bar", "line", "stacked"]
    bucket: Literal["day", "week"]
    start: str
    end: str
    labels: list[str] = Field(description="Dates (or week-start dates), oldest first")
    series: list[Series]
    average: float | None = Field(description="Mean of the plotted totals over days with data")
    days_with_data: int
    notes: list[str]


class ChartResponse(ChartData):
    """Wire contract: plot arrays are present only for clients without MCP Apps."""

    labels: list[str] = Field(
        default_factory=list,
        description="Dates; omitted for UI-capable clients, whose widget receives the full plot",
    )
    series: list[Series] = Field(
        default_factory=list,
        description="Plot values; omitted for UI-capable clients to avoid duplicate rendering",
    )


def _periods(start: date, end: date, bucket: Literal["day", "week"]) -> list[date]:
    if bucket == "day":
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]
    first = start - timedelta(days=start.weekday())
    return [first + timedelta(weeks=i) for i in range((end - first).days // 7 + 1)]


def _bucketed(
    points: dict[date, float], periods: list[date], bucket: Literal["day", "week"]
) -> list[float | None]:
    if bucket == "day":
        return [points.get(p) for p in periods]
    out: list[float | None] = []
    for monday in periods:
        week = [v for d, v in points.items() if monday <= d < monday + timedelta(days=7)]
        out.append(round(fmean(week), 2) if week else None)
    return out


def chart_data(
    conn: sqlite3.Connection,
    metric: str,
    start: date,
    end: date,
    bucket: Literal["day", "week"],
) -> ChartData:
    """Raises ValueError (with a fix) for an unknown metric or an oversized range."""
    if metric not in CHART_METRICS:
        raise ValueError(f"Unknown chart metric {metric!r}.")
    if end < start:
        raise ValueError("end is before start.")
    days = (end - start).days + 1
    if bucket == "day" and days > MAX_DAILY_CHART_POINTS:
        raise ValueError(f"{days} daily bars is too many; use bucket='week' or a shorter range.")
    periods = _periods(start, end, bucket)
    labels = [p.isoformat() for p in periods]
    kind: Literal["bar", "line", "stacked"]

    if metric == SLEEP_STAGES:
        series = []
        totals: dict[date, float] = {}
        for name, source, role in STAGES:
            hours = {
                d: round(float(v) / 60, 2)
                for d, v in metrics.series(conn, source, start, end)
                if not isinstance(v, str)
            }
            for d, h in hours.items():
                totals[d] = totals.get(d, 0.0) + h
            series.append(Series(name=name, role=role, values=_bucketed(hours, periods, bucket)))
        title, unit, kind = "Sleep stages", "hours", "stacked"
        plotted = totals
    else:
        spec = metrics.METRICS[metric]
        plotted = {
            d: float(v)
            for d, v in metrics.series(conn, metric, start, end)
            if not isinstance(v, str)
        }
        series = [
            Series(
                name=spec.description, role="primary", values=_bucketed(plotted, periods, bucket)
            )
        ]
        title, unit = spec.description, spec.unit
        kind = "line" if spec.unit in LINE_UNITS else "bar"

    notes = []
    if not plotted:
        notes.append("No data in this range. garmin_describe_data shows which dates have data.")
    if bucket == "week":
        notes.append("Each point is the weekly mean of days with data.")
    return ChartData(
        title=title,
        metric=metric,
        unit=unit,
        kind=kind,
        bucket=bucket,
        start=start.isoformat(),
        end=end.isoformat(),
        labels=labels,
        series=series,
        average=round(fmean(plotted.values()), 2) if plotted else None,
        days_with_data=len(plotted),
        notes=notes,
    )
