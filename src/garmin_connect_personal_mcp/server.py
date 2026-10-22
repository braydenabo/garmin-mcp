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
from importlib.resources import files
from statistics import fmean
from typing import Annotated, Any, Literal

from mcp.server.apps import Apps
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from garmin_connect_personal_mcp import __version__, activities, charts, db, metrics
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
# Writes only to the local archive, safe to repeat, and talks to Garmin.
SYNC = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)

# Toolsets let a client load fewer tools (smaller models choose better from fewer).
# garmin_describe_data is always on.
TOOLSETS: dict[str, tuple[str, ...]] = {
    "daily": ("garmin_get_day", "garmin_get_trend"),
    "activities": ("garmin_list_activities", "garmin_get_activity"),
    "sync": ("garmin_sync",),
    "charts": ("garmin_show_chart",),
}
CHART_URI = "ui://garmin/chart.html"
MAX_TOOL_SYNC_DAYS = 90

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


class SyncResult(BaseModel):
    ok: bool
    already_running: bool = False
    days_saved: dict[str, int] = Field(default_factory=dict)
    days_without_data: dict[str, int] = Field(default_factory=dict)
    activities_listed: int = 0
    fit_files_saved: int = 0
    errors: list[str] = Field(default_factory=list)
    message: str


def sync_now(
    paths: DataPaths, days_back: int, api_factory: Callable[[], Any] | None = None
) -> SyncResult:
    from garmin_connect_personal_mcp.garmin_client import GarminApi, GarminError
    from garmin_connect_personal_mcp.sync import SyncAlreadyRunning, run_sync

    if not 0 <= days_back <= MAX_TOOL_SYNC_DAYS:
        raise ToolError(
            f"days_back must be 0-{MAX_TOOL_SYNC_DAYS}. For a longer backfill the user can run "
            f"`{PROG} sync --days-back N`."
        )
    try:
        api = (api_factory or GarminApi)()
    except GarminError as exc:
        raise ToolError(f"{exc} (The user has to do this; you can't.)") from None
    try:
        with api:
            report = run_sync(api, paths, days_back=days_back or None)
    except SyncAlreadyRunning:
        return SyncResult(
            ok=False,
            already_running=True,
            message="A sync is already running. Wait a minute, then read the data again.",
        )
    errors = report.errors[:10]
    if report.aborted:
        message = f"Stopped early: {report.aborted} What was saved is kept."
    elif errors:
        message = "Synced, with some errors; failed days are retried on the next sync."
    else:
        message = "Synced. Read the data again to see the update."
    return SyncResult(
        ok=report.ok,
        days_saved=report.days,
        days_without_data=report.empty,
        activities_listed=report.activities,
        fit_files_saved=report.fit_files,
        errors=errors,
        message=message,
    )


def show_chart(
    paths: DataPaths, metric: str, start: date, end: date, bucket: Literal["day", "week"]
) -> charts.ChartData:
    if metric not in charts.CHART_METRICS:
        close = difflib.get_close_matches(metric, charts.CHART_METRICS, n=3)
        hint = f" Did you mean {', '.join(close)}?" if close else ""
        raise ToolError(
            f"Unknown metric {metric!r}.{hint} garmin_describe_data lists every metric."
        )
    with open_archive(paths) as conn:
        if conn is None:
            raise ToolError(f"No data has been synced yet. The user can run `{PROG} sync`.")
        try:
            return charts.chart_data(conn, metric, start, end, bucket)
        except ValueError as exc:
            raise ToolError(str(exc)) from None


def _chart_apps(paths: DataPaths, today: Callable[[], date]) -> Apps:
    """garmin_show_chart, bound to the chart view that MCP Apps hosts display."""
    apps = Apps()
    html = (
        files("garmin_connect_personal_mcp").joinpath("ui/chart.html").read_text(encoding="utf-8")
    )
    apps.add_html_resource(
        CHART_URI, html, name="Garmin chart", description="Interactive chart for garmin_show_chart"
    )

    @apps.tool(
        resource_uri=CHART_URI,
        name="garmin_show_chart",
        title="Show a chart",
        description=(
            "Show the user a chart of one metric over time, or of sleep stages "
            "(metric='sleep_stages'). Use only when the user asks to see, plot, graph or "
            "visualize data. Apps that support it display an interactive chart; the result also "
            "has the data, so describe what it shows. Defaults to the last 30 days."
        ),
        annotations=READ_ONLY,
    )
    def garmin_show_chart(
        metric: Annotated[
            str,
            Field(
                description="sleep_stages, or a metric from garmin_describe_data",
                json_schema_extra={"enum": charts.CHART_METRICS},
            ),
        ],
        start: Annotated[
            str, Field(description="YYYY-MM-DD, 'today' or 'yesterday'. Default: 30 days ago")
        ] = "",
        end: Annotated[str, Field(description="YYYY-MM-DD, 'today' or 'yesterday'")] = "today",
        bucket: Annotated[
            Literal["day", "week"], Field(description="A bar or point per day, or weekly means")
        ] = "day",
    ) -> charts.ChartData:
        now = today()
        end_date = parse_date(end, now)
        start_date = parse_date(start, now) if start else end_date - timedelta(days=29)
        return show_chart(paths, metric, start_date, end_date, bucket)

    return apps


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


def build_server(
    paths: DataPaths,
    today: Callable[[], date] = local_today,
    *,
    toolsets: tuple[str, ...] = tuple(TOOLSETS),
    read_only: bool = False,
    api_factory: Callable[[], Any] | None = None,
) -> MCPServer:
    """The server with the chosen toolsets. read_only removes the only tool that calls Garmin."""
    unknown = set(toolsets) - set(TOOLSETS)
    if unknown:
        raise ValueError(
            f"Unknown toolset(s): {', '.join(sorted(unknown))}. Choose from: {', '.join(TOOLSETS)}."
        )
    server = MCPServer(
        "garmin",
        title="Garmin (personal data)",
        instructions=INSTRUCTIONS,
        version=__version__,
        extensions=[_chart_apps(paths, today)] if "charts" in toolsets else None,
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

    @server.tool(
        name="garmin_sync",
        title="Sync from Garmin",
        description=(
            "Download new data from Garmin into the archive. Use it when garmin_get_day says "
            "not_synced or the latest data is old. By default it resumes from the last sync (a "
            "few seconds). days_back re-checks up to 90 recent days."
        ),
        annotations=SYNC,
    )
    def garmin_sync(
        days_back: Annotated[
            int,
            Field(description=f"0 to resume (default), or 1-{MAX_TOOL_SYNC_DAYS} days to re-check"),
        ] = 0,
    ) -> SyncResult:
        return sync_now(paths, days_back, api_factory)

    enabled = {name for toolset in toolsets for name in TOOLSETS[toolset]}
    if read_only:
        enabled -= set(TOOLSETS["sync"])
    for toolset, toolset_tools in TOOLSETS.items():
        if toolset == "charts":
            continue  # only registered when enabled, above
        for name in toolset_tools:
            if name not in enabled:
                server.remove_tool(name)

    return server
