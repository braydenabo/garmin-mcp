"""The MCP server: tools that answer questions from the local archive.

Tools only read the archive, which sync fills. Every tool has a flat input
schema, units in its output field names, accurate annotations, and a typed
output (structuredContent plus a text copy for clients that only read text).
Errors are returned as tool results that say what to do next.
"""

import difflib
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from statistics import fmean
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from garmin_connect_personal_mcp import __version__, activities, db, metrics
from garmin_connect_personal_mcp.config import DataPaths

PROG = "garmin-connect-personal-mcp"

INSTRUCTIONS = (
    "Answers questions about the user's own Garmin health and activity data from a local "
    "archive. Call garmin_describe_data first to see which metrics exist, their units, and the "
    "dates covered. Dates are the user's local calendar dates (YYYY-MM-DD). Missing data is "
    "null, never zero: a null usually means the watch wasn't worn. This is consumer wearable "
    "data, not a medical device; don't diagnose."
)

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

NOTES = [
    "Dates are the user's local calendar dates.",
    "Missing values are null, never zero. A day without data usually means the watch wasn't worn.",
    "Values come from a consumer wearable and are not medical measurements.",
]


class DatasetCoverage(BaseModel):
    dataset: str
    days_with_data: int
    first_date: str | None
    last_date: str | None
    last_synced_date: str | None = Field(description="Latest local date checked by sync")


class MetricInfo(BaseModel):
    name: str
    dataset: str
    unit: str
    description: str
    numeric: bool = Field(description="Whether garmin_get_trend can chart it")


class DataDescription(BaseModel):
    archive_exists: bool
    last_sync_utc: str | None
    datasets: list[DatasetCoverage]
    activities_count: int
    activities_first_date: str | None
    activities_last_date: str | None
    activity_types: dict[str, int] = Field(
        description="Activity count per type, for garmin_list_activities' activity_type filter"
    )
    metrics: list[MetricInfo]
    notes: list[str]


DayStatus = Literal["data", "no_data", "not_synced", "sync_failed"]
MAX_DAILY_POINTS = 400  # about 13 months; longer ranges should use weekly buckets


class ActivityBrief(BaseModel):
    activity_id: int
    activity_type: str | None
    start_time_utc: str | None


class DayResult(BaseModel):
    date: str
    status: dict[str, DayStatus] = Field(
        description="Per dataset: data, no_data (checked, nothing recorded), not_synced, or "
        "sync_failed"
    )
    sleep: dict[str, float | int | str | None] | None
    hrv: dict[str, float | int | str | None] | None
    daily_summary: dict[str, float | int | str | None] | None
    activities: list[ActivityBrief]
    notes: list[str]


class TrendPoint(BaseModel):
    period_start: str = Field(description="The day, or the Monday that starts the week")
    value: float
    days_with_data: int


class TrendStats(BaseModel):
    days_with_data: int
    mean: float | None
    min: float | None
    max: float | None
    first: float | None = Field(description="Earliest daily value in the range")
    last: float | None = Field(description="Latest daily value in the range")


class Trend(BaseModel):
    metric: str
    unit: str
    description: str
    start: str
    end: str
    bucket: Literal["day", "week"]
    points: list[TrendPoint]
    stats: TrendStats
    days_without_data: int
    notes: list[str]


def parse_date(value: str, today: date) -> date:
    """YYYY-MM-DD, 'today' or 'yesterday'."""
    text = value.strip().lower()
    if text == "today":
        return today
    if text == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ToolError(
            f"Invalid date {value!r}. Use YYYY-MM-DD, 'today' or 'yesterday'."
        ) from None


def _status(conn: sqlite3.Connection, dataset: str, when: date, has: bool) -> DayStatus:
    if has:
        return "data"
    state = metrics.sync_state(conn, dataset, when)
    if state == "error":
        return "sync_failed"
    return "not_synced" if state == "never" else "no_data"


def get_day(paths: DataPaths, when: date) -> DayResult:
    with open_archive(paths) as conn:
        if conn is None:
            raise ToolError(f"No data has been synced yet. The user can run `{PROG} sync`.")
        groups = metrics.day(conn, when)
        status = {name: _status(conn, name, when, groups[name] is not None) for name in groups}
        rows = conn.execute(
            "SELECT activity_id, activity_type, start_time_utc FROM activities"
            " WHERE calendar_date = ? ORDER BY start_time_utc",
            (when.isoformat(),),
        ).fetchall()
    notes = []
    if all(s == "not_synced" for s in status.values()):
        notes.append(f"{when} hasn't been synced yet. Call garmin_sync, then ask again.")
    elif all(s != "data" for s in status.values()) and not rows:
        notes.append("Nothing was recorded this day; the watch probably wasn't worn.")
    if "sync_failed" in status.values():
        notes.append("Some data failed to sync for this day. Calling garmin_sync retries it.")
    return DayResult(
        date=when.isoformat(),
        status=status,
        sleep=groups["sleep"],
        hrv=groups["hrv"],
        daily_summary=groups["daily_summary"],
        activities=[
            ActivityBrief(activity_id=r[0], activity_type=r[1], start_time_utc=r[2]) for r in rows
        ],
        notes=notes,
    )


def _round(value: float) -> float:
    return round(value, 2)


def get_trend(
    paths: DataPaths, metric: str, start: date, end: date, bucket: Literal["day", "week"]
) -> Trend:
    if metric not in metrics.NUMERIC_METRICS:
        close = difflib.get_close_matches(metric, metrics.NUMERIC_METRICS, n=3)
        hint = f" Did you mean {', '.join(close)}?" if close else ""
        raise ToolError(
            f"Unknown metric {metric!r}.{hint} garmin_describe_data lists every metric."
        )
    if end < start:
        raise ToolError("end is before start.")
    days = (end - start).days + 1
    if bucket == "day" and days > MAX_DAILY_POINTS:
        raise ToolError(
            f"{days} days is too many daily points; use bucket='week' or a shorter range."
        )
    with open_archive(paths) as conn:
        if conn is None:
            raise ToolError(f"No data has been synced yet. The user can run `{PROG} sync`.")
        raw_points = metrics.series(conn, metric, start, end)
    values = [(d, float(v)) for d, v in raw_points if not isinstance(v, str)]

    if bucket == "day":
        points = [
            TrendPoint(period_start=d.isoformat(), value=v, days_with_data=1) for d, v in values
        ]
    else:
        weeks: dict[date, list[float]] = {}
        for d, v in values:
            weeks.setdefault(d - timedelta(days=d.weekday()), []).append(v)
        points = [
            TrendPoint(period_start=w.isoformat(), value=_round(fmean(vs)), days_with_data=len(vs))
            for w, vs in sorted(weeks.items())
        ]

    nums = [v for _, v in values]
    spec = metrics.METRICS[metric]
    notes = []
    if not nums:
        notes.append("No data in this range. garmin_describe_data shows which dates have data.")
    return Trend(
        metric=metric,
        unit=spec.unit,
        description=spec.description,
        start=start.isoformat(),
        end=end.isoformat(),
        bucket=bucket,
        points=points,
        stats=TrendStats(
            days_with_data=len(nums),
            mean=_round(fmean(nums)) if nums else None,
            min=min(nums) if nums else None,
            max=max(nums) if nums else None,
            first=nums[0] if nums else None,
            last=nums[-1] if nums else None,
        ),
        days_without_data=days - len(nums),
        notes=notes,
    )


MAX_PAGE_SIZE = 100


class ActivityPage(BaseModel):
    activities: list[activities.ActivitySummary]
    total: int = Field(description="Activities matching the filters")
    next_cursor: str | None = Field(description="Pass as cursor for the next page; null at the end")
    notes: list[str]


def list_activities(
    paths: DataPaths, start: date, end: date, activity_type: str, limit: int, cursor: str
) -> ActivityPage:
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ToolError(f"limit must be between 1 and {MAX_PAGE_SIZE}.")
    if end < start:
        raise ToolError("end is before start.")
    try:
        offset = int(cursor) if cursor else 0
    except ValueError:
        raise ToolError("Invalid cursor; pass the next_cursor from the previous page.") from None
    with open_archive(paths) as conn:
        if conn is None:
            raise ToolError(f"No data has been synced yet. The user can run `{PROG} sync`.")
        page, total = activities.list_rows(conn, start, end, activity_type, limit, offset)
        types = activities.type_counts(conn) if total == 0 and activity_type else {}
    notes = []
    if total == 0 and types:
        notes.append(
            f"No {activity_type!r} activities in range. Types in the archive: {', '.join(types)}."
        )
    elif total == 0:
        notes.append("No activities in this range. Calling garmin_sync fetches recent ones.")
    more = offset + len(page) < total
    return ActivityPage(
        activities=page,
        total=total,
        next_cursor=str(offset + len(page)) if more else None,
        notes=notes,
    )


def get_activity(paths: DataPaths, activity_id: int) -> activities.ActivityDetail:
    with open_archive(paths) as conn:
        if conn is None:
            raise ToolError(f"No data has been synced yet. The user can run `{PROG} sync`.")
        found = activities.get_row(conn, activity_id)
    if found is None:
        raise ToolError(
            f"No activity {activity_id} in the archive. garmin_list_activities shows the IDs."
        )
    return found


def local_today() -> date:
    return datetime.now().astimezone().date()


@contextmanager
def open_archive(paths: DataPaths) -> Iterator[sqlite3.Connection | None]:
    """A read-only connection, or None if there is no archive yet."""
    if not paths.db.exists():
        yield None
        return
    conn = db.connect_readonly(paths.db)
    try:
        yield conn
    finally:
        conn.close()


def describe_data(paths: DataPaths) -> DataDescription:
    metric_list = [
        MetricInfo(
            name=name, dataset=m.dataset, unit=m.unit, description=m.description, numeric=m.numeric
        )
        for name, m in metrics.METRICS.items()
    ]
    with open_archive(paths) as conn:
        if conn is None:
            return DataDescription(
                archive_exists=False,
                last_sync_utc=None,
                datasets=[],
                activities_count=0,
                activities_first_date=None,
                activities_last_date=None,
                activity_types={},
                metrics=metric_list,
                notes=[f"No data has been synced yet. The user can run `{PROG} sync`.", *NOTES],
            )
        count, first, last = conn.execute(
            "SELECT COUNT(*), MIN(calendar_date), MAX(calendar_date) FROM activities"
        ).fetchone()
        return DataDescription(
            archive_exists=True,
            last_sync_utc=db.last_sync_time(conn),
            datasets=[
                DatasetCoverage(
                    dataset=c.dataset,
                    days_with_data=c.days_with_data,
                    first_date=c.first_date,
                    last_date=c.last_date,
                    last_synced_date=c.last_synced,
                )
                for c in metrics.coverage(conn)
            ],
            activities_count=count,
            activities_first_date=first,
            activities_last_date=last,
            activity_types=activities.type_counts(conn),
            metrics=metric_list,
            notes=NOTES,
        )


def build_server(paths: DataPaths, today: Callable[[], date] = local_today) -> MCPServer:
    server = MCPServer(
        "garmin",
        title="Garmin (personal data)",
        instructions=INSTRUCTIONS,
        version=__version__,
    )

    @server.tool(
        name="garmin_describe_data",
        title="Describe the Garmin data",
        description=(
            "What's in the archive: each dataset's date coverage and last sync, every metric "
            "with its unit, and activity counts. Call this first."
        ),
        annotations=READ_ONLY,
    )
    def garmin_describe_data() -> DataDescription:
        return describe_data(paths)

    @server.tool(
        name="garmin_get_day",
        title="Get one day",
        description=(
            "Sleep, HRV and daily summary (steps, resting heart rate, stress, Body Battery, "
            "calories) for one date, plus that day's activities. Each dataset has a status: "
            "data, no_data (checked, nothing recorded), not_synced or sync_failed. "
            "Missing values are null."
        ),
        annotations=READ_ONLY,
    )
    def garmin_get_day(
        date: Annotated[str, Field(description="YYYY-MM-DD, 'today' or 'yesterday'")] = "today",
    ) -> DayResult:
        return get_day(paths, parse_date(date, today()))

    @server.tool(
        name="garmin_get_trend",
        title="Get a metric over time",
        description=(
            "One metric over a date range, per day or per week (weekly values are means), with "
            "mean, min, max, first and last. Days without data are skipped and counted. "
            "Defaults to the last 30 days."
        ),
        annotations=READ_ONLY,
    )
    def garmin_get_trend(
        metric: Annotated[
            str,
            Field(
                description="Metric name; see garmin_describe_data",
                json_schema_extra={"enum": metrics.NUMERIC_METRICS},
            ),
        ],
        start: Annotated[
            str, Field(description="YYYY-MM-DD, 'today' or 'yesterday'. Default: 30 days ago")
        ] = "",
        end: Annotated[str, Field(description="YYYY-MM-DD, 'today' or 'yesterday'")] = "today",
        bucket: Annotated[
            Literal["day", "week"], Field(description="Points per day, or weekly means")
        ] = "day",
    ) -> Trend:
        now = today()
        end_date = parse_date(end, now)
        start_date = parse_date(start, now) if start else end_date - timedelta(days=29)
        return get_trend(paths, metric, start_date, end_date, bucket)

    @server.tool(
        name="garmin_list_activities",
        title="List activities",
        description=(
            "Workouts newest first, with headline stats (duration, distance, pace or speed, "
            "heart rate, calories, training load). Filter by date range and type; the type "
            "matches partially, so 'running' also finds trail runs. Pages via next_cursor. "
            "Defaults to the last 30 days."
        ),
        annotations=READ_ONLY,
    )
    def garmin_list_activities(
        start: Annotated[
            str, Field(description="YYYY-MM-DD, 'today' or 'yesterday'. Default: 30 days ago")
        ] = "",
        end: Annotated[str, Field(description="YYYY-MM-DD, 'today' or 'yesterday'")] = "today",
        activity_type: Annotated[
            str, Field(description="e.g. running, cycling, swimming; empty for all")
        ] = "",
        limit: Annotated[int, Field(description=f"Page size, 1-{MAX_PAGE_SIZE}")] = 20,
        cursor: Annotated[str, Field(description="next_cursor from the previous page")] = "",
    ) -> ActivityPage:
        now = today()
        end_date = parse_date(end, now)
        start_date = parse_date(start, now) if start else end_date - timedelta(days=29)
        return list_activities(paths, start_date, end_date, activity_type, limit, cursor)

    @server.tool(
        name="garmin_get_activity",
        title="Get one activity",
        description=(
            "Full summary of one activity: duration, distance, pace or speed, heart rate and "
            "minutes per heart-rate zone, power, cadence, elevation, training effect, and "
            "splits. Use an activity_id from garmin_list_activities."
        ),
        annotations=READ_ONLY,
    )
    def garmin_get_activity(
        activity_id: Annotated[int, Field(description="From garmin_list_activities")],
    ) -> activities.ActivityDetail:
        return get_activity(paths, activity_id)

    return server
