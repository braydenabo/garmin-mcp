"""The MCP server: tools that answer questions from the local archive.

Tools only read the archive, which sync fills. Every tool has a flat input
schema, units in its output field names, accurate annotations, and a typed
output (structuredContent plus a text copy for clients that only read text).
ChatGPT chart calls expose a summary; full plot data goes only to the widget metadata.
Errors are returned as tool results that say what to do next.
"""

import difflib
import hashlib
import sqlite3
import sys
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from importlib.resources import files
from statistics import fmean
from typing import Annotated, Any, Literal

from mcp.server.apps import Apps, client_supports_apps
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resources import TextResource
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from garmin_connect_personal_mcp import (
    __version__,
    activities,
    analysis,
    charts,
    db,
    fit,
    metrics,
    query,
    streams,
)
from garmin_connect_personal_mcp.config import DataPaths

PROG = "garmin-connect-personal-mcp"

INSTRUCTIONS = (
    "Answers questions about the user's own Garmin health and activity data from a local "
    "archive. Call garmin_describe_data first to see which metrics exist, their units, and the "
    "dates covered. Dates are the user's local calendar dates (YYYY-MM-DD). Missing data is "
    "null, never zero: a null usually means the watch wasn't worn. This is consumer wearable "
    "data, not a medical device; don't diagnose. For chart requests, use garmin_show_chart: "
    "it already renders the chart. Do not recreate its chart with Python or another plotting "
    "tool. Show one chart unless the user explicitly requests multiple metrics. For a general "
    "sleep chart use sleep_hours; use sleep_stages only when stages are requested. After the "
    "chart, give a short text summary rather than another visualization."
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
    "daily": (
        "garmin_get_day",
        "garmin_get_trend",
        "garmin_get_baseline",
        "garmin_compare_periods",
    ),
    "activities": ("garmin_list_activities", "garmin_get_activity", "garmin_get_activity_stream"),
    "query": ("garmin_query",),
    "sync": ("garmin_sync",),
    "charts": ("garmin_show_chart", "garmin_get_chart_data"),
}
CHART_HTML = (
    files("garmin_connect_personal_mcp").joinpath("ui/chart.html").read_text(encoding="utf-8")
)
# Claude shows no widget for the content-addressed URI, so the standard ui.resourceUri
# stays stable. ChatGPT caches templates by URI, so its openai/outputTemplate is
# content-addressed: HTML/CSS fixes appear after refreshing its tool metadata.
CHART_URI = "ui://garmin/chart.html"
VERSIONED_CHART_URI = (
    f"ui://garmin/chart-{hashlib.sha256(CHART_HTML.encode()).hexdigest()[:16]}.html"
)
# ChatGPT redraws plot points it can see, so it gets a summary and the widget gets the
# points through result metadata. Other hosts (Claude) may not pass that metadata to the
# widget, so they keep the full data in structuredContent.
SUMMARY_ONLY_CLIENTS = ("openai", "chatgpt")
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


def _check_metric(metric: str) -> None:
    if metric not in metrics.NUMERIC_METRICS:
        close = difflib.get_close_matches(metric, metrics.NUMERIC_METRICS, n=3)
        hint = f" Did you mean {', '.join(close)}?" if close else ""
        raise ToolError(
            f"Unknown metric {metric!r}.{hint} garmin_describe_data lists every metric."
        )


def get_trend(
    paths: DataPaths, metric: str, start: date, end: date, bucket: Literal["day", "week"]
) -> Trend:
    _check_metric(metric)
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


def get_baseline(
    paths: DataPaths, metric: str, day: date, days: int, today: date
) -> analysis.Baseline:
    _check_metric(metric)
    if not 7 <= days <= analysis.MAX_BASELINE_DAYS:
        raise ToolError(f"days must be between 7 and {analysis.MAX_BASELINE_DAYS}.")
    start, _ = analysis.baseline_window(day, days)
    with open_archive(paths) as conn:
        if conn is None:
            raise ToolError(f"No data has been synced yet. The user can run `{PROG} sync`.")
        points = metrics.series(conn, metric, start, day)
    numbers = {d: float(v) for d, v in points if not isinstance(v, str)}
    value = numbers.pop(day, None)
    spec = metrics.METRICS[metric]
    return analysis.compare(
        metric,
        spec.unit,
        spec.description,
        day,
        days,
        value,
        [numbers[d] for d in sorted(numbers)],
        today=today,
    )


def compare_periods(
    paths: DataPaths, metric: str, period: tuple[date, date], reference: tuple[date, date]
) -> analysis.PeriodComparison:
    _check_metric(metric)
    for name, (start, end) in (("start/end", period), ("compare_start/compare_end", reference)):
        if end < start:
            raise ToolError(f"{name}: the end is before the start.")
        if (end - start).days + 1 > analysis.MAX_PERIOD_DAYS:
            raise ToolError(f"{name}: each period can be at most {analysis.MAX_PERIOD_DAYS} days.")
    with open_archive(paths) as conn:
        if conn is None:
            raise ToolError(f"No data has been synced yet. The user can run `{PROG} sync`.")

        def values(start: date, end: date) -> list[float]:
            points = metrics.series(conn, metric, start, end)
            return [float(v) for _, v in points if not isinstance(v, str)]

        a = (*period, values(*period))
        b = (*reference, values(*reference))
    spec = metrics.METRICS[metric]
    return analysis.compare_periods(metric, spec.unit, spec.description, a, b)


SCHEMA_URI = "garmin://schema"


def run_query(paths: DataPaths, sql: str) -> query.QueryResult:
    with open_archive(paths) as conn:
        if conn is None:
            raise ToolError(f"No data has been synced yet. The user can run `{PROG} sync`.")
        try:
            return query.run(conn, paths.db, sql)
        except query.QueryError as exc:
            raise ToolError(str(exc)) from None


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


def get_activity_stream(
    paths: DataPaths,
    activity_id: int,
    interval_seconds: int = 0,
    start_minute: float = 0,
    end_minute: float = 0,
) -> streams.Stream:
    with open_archive(paths) as conn:
        if conn is None:
            raise ToolError(f"No data has been synced yet. The user can run `{PROG} sync`.")
        row = conn.execute(
            "SELECT activity_type, fit_path FROM activities WHERE activity_id = ?", (activity_id,)
        ).fetchone()
    if row is None:
        raise ToolError(
            f"No activity {activity_id} in the archive. garmin_list_activities shows the IDs."
        )
    activity_type, fit_path = row
    if fit_path is None:
        raise ToolError(
            f"The FIT file for activity {activity_id} isn't downloaded yet. Call garmin_sync to "
            "fetch it; garmin_get_activity has the summary meanwhile."
        )
    path = paths.fit_dir / str(fit_path)
    if path.name != fit_path or not path.is_file():
        raise ToolError(
            f"The FIT file for activity {activity_id} is missing from the archive folder, so its "
            "samples can't be read. garmin_get_activity still has the summary."
        )
    try:
        samples = fit.read_fit_file(path)
    except (fit.FitError, OSError, zipfile.BadZipFile) as exc:
        raise ToolError(
            f"Couldn't read the FIT file for activity {activity_id}: {exc} "
            "garmin_get_activity still has the summary."
        ) from None
    try:
        return streams.build(
            activity_id,
            activity_type,
            samples,
            interval_seconds=interval_seconds,
            start_minute=start_minute,
            end_minute=end_minute,
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from None


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
    for uri in (CHART_URI, VERSIONED_CHART_URI):
        apps.add_resource(
            TextResource(
                uri=uri,
                name="Garmin chart" if uri == CHART_URI else "Garmin chart (versioned)",
                description="Displays the requested Garmin metric as an interactive chart.",
                mime_type="text/html;profile=mcp-app",
                text=CHART_HTML,
                meta={
                    "openai/widgetDescription": (
                        "The requested Garmin chart is already displayed here. Summarize it "
                        "briefly in text; do not draw another chart or add unrequested metrics."
                    )
                },
            )
        )

    @apps.tool(
        resource_uri=CHART_URI,
        meta={"openai/outputTemplate": VERSIONED_CHART_URI},
        name="garmin_show_chart",
        title="Show a chart",
        description=(
            "Render one interactive chart when requested. For a general sleep chart use "
            "sleep_hours; use sleep_stages when stages are requested. Do not add other metrics "
            "unless asked. This tool displays the final chart: summarize in text without "
            "recreating it in Python or another chart tool. Defaults to the last 30 days."
        ),
        annotations=READ_ONLY,
    )
    def garmin_show_chart(
        ctx: Context[Any, Any],
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
    ) -> Annotated[CallToolResult, charts.ChartResponse]:
        now = today()
        end_date = parse_date(end, now)
        start_date = parse_date(start, now) if start else end_date - timedelta(days=29)
        data = show_chart(paths, metric, start_date, end_date, bucket)
        if not summary_only_chart(ctx):
            return CallToolResult(
                structured_content=data.model_dump(),
                content=[TextContent(type="text", text=data.model_dump_json())],
            )
        return CallToolResult(
            structured_content=data.model_dump(exclude={"labels", "series"}),
            content=[
                TextContent(
                    type="text",
                    text="The interactive Garmin chart is displayed. Briefly summarize its "
                    "average and coverage in text. Do not create a second chart or fetch the same "
                    "points again just to redraw them.",
                )
            ],
            _meta={"garmin/chart": data.model_dump()},
        )

    return apps


def client_name(ctx: Context[Any, Any]) -> str:
    params = ctx.session.client_params
    info = params.client_info if params else None
    return info.name if info else ""


def summary_only_chart(ctx: Context[Any, Any]) -> bool:
    """Whether to keep plot points out of the model-visible chart result."""
    if not client_supports_apps(ctx):
        return False
    name = client_name(ctx)
    print(f"garmin_show_chart: client {name or 'unnamed'}", file=sys.stderr)
    return any(part in name.lower() for part in SUMMARY_ONLY_CLIENTS)


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

    if "charts" in toolsets:
        _register_chart_data_tool(server, paths, today)

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
        name="garmin_get_baseline",
        title="Compare a day with the usual",
        description=(
            "One day's value of a metric against the user's usual level over the days before "
            "it: baseline mean, median and usual range (mean +/- 1 SD), and the day's "
            "difference, z-score, percentile and below/within/above. Default: today vs the "
            "previous 30 days."
        ),
        annotations=READ_ONLY,
    )
    def garmin_get_baseline(
        metric: Annotated[
            str,
            Field(
                description="Metric name; see garmin_describe_data",
                json_schema_extra={"enum": metrics.NUMERIC_METRICS},
            ),
        ],
        date: Annotated[str, Field(description="YYYY-MM-DD, 'today' or 'yesterday'")] = "today",
        days: Annotated[
            int,
            Field(
                description=f"Baseline length in days before date, 7-{analysis.MAX_BASELINE_DAYS}"
            ),
        ] = analysis.DEFAULT_BASELINE_DAYS,
    ) -> analysis.Baseline:
        now = today()
        return get_baseline(paths, metric, parse_date(date, now), days, now)

    @server.tool(
        name="garmin_compare_periods",
        title="Compare two periods",
        description=(
            "One metric in two date ranges side by side: days with data, mean, median, spread, "
            "min and max for each, and the difference in means (absolute and %). For questions "
            "like 'October vs September'. Default: last 30 days vs the 30 before."
        ),
        annotations=READ_ONLY,
    )
    def garmin_compare_periods(
        metric: Annotated[
            str,
            Field(
                description="Metric name; see garmin_describe_data",
                json_schema_extra={"enum": metrics.NUMERIC_METRICS},
            ),
        ],
        start: Annotated[
            str, Field(description="Period start: YYYY-MM-DD. Default: 29 days before end")
        ] = "",
        end: Annotated[
            str, Field(description="Period end: YYYY-MM-DD, 'today' or 'yesterday'")
        ] = "today",
        compare_start: Annotated[
            str,
            Field(description="Comparison start: YYYY-MM-DD. Default: same length as the period"),
        ] = "",
        compare_end: Annotated[
            str, Field(description="Comparison end. Default: the day before start")
        ] = "",
    ) -> analysis.PeriodComparison:
        now = today()
        end_date = parse_date(end, now)
        start_date = parse_date(start, now) if start else end_date - timedelta(days=29)
        length = (end_date - start_date).days + 1
        if compare_end:
            ref_end = parse_date(compare_end, now)
        elif compare_start:
            ref_end = parse_date(compare_start, now) + timedelta(days=length - 1)
        else:
            ref_end = start_date - timedelta(days=1)
        ref_start = (
            parse_date(compare_start, now)
            if compare_start
            else ref_end - timedelta(days=length - 1)
        )
        return compare_periods(paths, metric, (start_date, end_date), (ref_start, ref_end))

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
        name="garmin_get_activity_stream",
        title="Get an activity over time",
        description=(
            "One activity's heart rate, pace or speed, cadence, power, altitude and distance "
            "over elapsed time, averaged into even intervals (about 300 points by default). "
            "For pacing, drift, intervals and climbs. Zoom in with start_minute/end_minute. "
            "No GPS."
        ),
        annotations=READ_ONLY,
    )
    def garmin_get_activity_stream(
        activity_id: Annotated[int, Field(description="From garmin_list_activities")],
        interval_seconds: Annotated[
            int,
            Field(description=f"Seconds per point, 1-{streams.MAX_INTERVAL_SECONDS}; 0 picks one"),
        ] = 0,
        start_minute: Annotated[
            float, Field(description="Elapsed minute to start from; 0 for the start")
        ] = 0,
        end_minute: Annotated[
            float, Field(description="Elapsed minute to stop at; 0 for the end")
        ] = 0,
    ) -> streams.Stream:
        return get_activity_stream(paths, activity_id, interval_seconds, start_minute, end_minute)

    @server.tool(
        name="garmin_query",
        title="Query the data with SQL",
        description=(
            "One read-only SQLite SELECT over two clean tables: daily (a row per date, a column "
            "per metric named as in garmin_describe_data) and activities (a row per workout). "
            "For questions other tools can't answer. Max 500 rows. Full schema: garmin://schema."
        ),
        annotations=READ_ONLY,
    )
    def garmin_query(
        sql: Annotated[
            str,
            Field(
                description=(
                    "e.g. SELECT strftime('%Y-%m', date) AS month, avg(steps) FROM daily "
                    "GROUP BY month"
                )
            ),
        ],
    ) -> query.QueryResult:
        return run_query(paths, sql)

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

    if "query" in toolsets:
        server.add_resource(
            TextResource(
                uri=SCHEMA_URI,
                name="Garmin query schema",
                description="Tables and columns that garmin_query can read, with units.",
                mime_type="text/plain",
                text=query.schema_text(),
            )
        )

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


def _register_chart_data_tool(
    server: MCPServer, paths: DataPaths, today: Callable[[], date]
) -> None:
    @server.tool(
        name="garmin_get_chart_data",
        title="Load chart data for the widget",
        description=(
            "Widget-only: load plot points from the local archive for an already requested "
            "chart. Used when a host does not deliver component-only result metadata. "
            "Does not create another chart, sync data, or contact Garmin."
        ),
        annotations=READ_ONLY,
        meta={"ui": {"visibility": ["app"]}},
    )
    def garmin_get_chart_data(
        metric: Annotated[
            str, Field(description="Chart metric", json_schema_extra={"enum": charts.CHART_METRICS})
        ],
        start: Annotated[str, Field(description="YYYY-MM-DD, 'today' or 'yesterday'")] = "",
        end: Annotated[str, Field(description="YYYY-MM-DD, 'today' or 'yesterday'")] = "today",
        bucket: Annotated[
            Literal["day", "week"], Field(description="Daily or weekly points")
        ] = "day",
    ) -> charts.ChartData:
        now = today()
        end_date = parse_date(end, now)
        start_date = parse_date(start, now) if start else end_date - timedelta(days=29)
        return show_chart(paths, metric, start_date, end_date, bucket)
