"""Pull data from Garmin into the local archive.

Incremental and safe to re-run: each dataset resumes from its last good day,
re-checks the most recent days (watches often sync late), and retries days
that failed before.

Daily data uses Garmin's date-range endpoints, chunked to each endpoint's
documented maximum span, so a 90-day sync is a few dozen requests rather than
hundreds (Garmin rate-limits Personal API keys). Each range response is split
back into per-day items, which are stored unchanged; see db.py.
"""

import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection
from typing import Any, Protocol, TypeVar

from garmin_personal_api import GarminPersonalApiClient

from garmin_connect_personal_mcp import db
from garmin_connect_personal_mcp.config import DataPaths
from garmin_connect_personal_mcp.garmin_client import GarminError

T = TypeVar("T")

# progress(step, done, total, detail): total is 0 when it isn't known in advance.
Progress = Callable[[str, int, int, str], None]


def _no_progress(step: str, done: int, total: int, detail: str) -> None:
    pass


@dataclass(frozen=True)
class RangeDataset:
    """A Garmin endpoint that returns one item per day for a date range."""

    fetch: Callable[[GarminPersonalApiClient, date, date], Any]
    max_days: int  # Garmin's documented maximum span per request
    day_key: str  # the field in each item holding its local date
    items_key: str | None = None  # where the item list sits, if not the top level

    def items(self, raw: Any) -> list[Any]:
        found = raw.get(self.items_key) if isinstance(raw, dict) and self.items_key else raw
        return found if isinstance(found, list) else []


# Spans from Garmin's Personal API Swagger docs; 31 where none is documented.
DAILY_DATASETS: dict[str, RangeDataset] = {
    "daily_summary": RangeDataset(
        lambda c, s, e: c.get_daily_summaries(s, e), 365, "calendarDate", "data"
    ),
    "sleep": RangeDataset(lambda c, s, e: c.get_daily_sleep_summaries(s, e), 31, "calendarDate"),
    "hrv": RangeDataset(
        lambda c, s, e: c.get_daily_hrv_summaries(s, e), 31, "calendarDate", "hrvSummaries"
    ),
    "stress": RangeDataset(lambda c, s, e: c.get_daily_stress(s, e), 28, "calendarDate"),
    "steps": RangeDataset(lambda c, s, e: c.get_daily_steps(s, e), 28, "calendarDate"),
    "body_battery": RangeDataset(lambda c, s, e: c.get_daily_body_battery(s, e), 31, "date"),
    "weight": RangeDataset(
        lambda c, s, e: c.get_weight_measurements(s, e),
        31,
        "summaryDate",
        "dailyWeightSummaries",
    ),
}
ACTIVITIES = "activities"

FIRST_SYNC_DAYS = 7
RECHECK_DAYS = 2
RETRY_ERRORS_WITHIN_DAYS = 365
ACTIVITY_PAGE_SIZE = 99  # Garmin's documented default page size
MAX_ACTIVITY_PAGES = 200  # safety stop if the server ever ignores the offset


class Api(Protocol):
    def call(self, request: Callable[[GarminPersonalApiClient], T]) -> T: ...


class SyncAlreadyRunning(Exception):
    """Another sync holds the lock (a scheduled run, another terminal, or the MCP server)."""


@contextmanager
def sync_lock(path: Path) -> Iterator[None]:
    """Hold an OS-level lock for the duration of a sync.

    The OS releases it if the process dies, so a crash never leaves a stale lock.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SyncAlreadyRunning(
                "Another sync is already running. Wait for it to finish and try again."
            ) from None
        try:
            yield
        finally:
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class SyncAborted(Exception):
    """Garmin keeps refusing requests (rate limit or bad key); stop instead of hammering it."""


@dataclass
class SyncReport:
    days: dict[str, int] = field(default_factory=dict)
    empty: dict[str, int] = field(default_factory=dict)
    activities: int = 0
    fit_files: int = 0
    errors: list[str] = field(default_factory=list)
    aborted: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors and self.aborted is None


def is_empty(raw: Any) -> bool:
    return raw is None or raw == [] or raw == {}


def days_to_sync(
    conn: Connection, dataset: str, today: date, days_back: int | None = None
) -> list[date]:
    """The days this dataset needs, oldest first."""
    if days_back is not None:
        start = today - timedelta(days=days_back - 1)
    else:
        last = db.last_synced_date(conn, dataset)
        if last is None:
            start = today - timedelta(days=FIRST_SYNC_DAYS - 1)
        else:
            start = min(last, today) - timedelta(days=RECHECK_DAYS)
    wanted = {start + timedelta(days=i) for i in range((today - start).days + 1)}
    retry_since = (today - timedelta(days=RETRY_ERRORS_WITHIN_DAYS)).isoformat()
    for row in conn.execute(
        "SELECT calendar_date FROM sync_log"
        " WHERE dataset = ? AND status = 'error' AND calendar_date >= ?",
        (dataset, retry_since),
    ):
        wanted.add(date.fromisoformat(row["calendar_date"]))
    return sorted(wanted)


def chunks(days: list[date], max_days: int) -> list[list[date]]:
    """Split sorted days into runs of consecutive days, each at most max_days long."""
    runs: list[list[date]] = []
    for day in days:
        run = runs[-1] if runs else None
        if run and day - run[-1] == timedelta(days=1) and len(run) < max_days:
            run.append(day)
        else:
            runs.append([day])
    return runs


def split_by_day(dataset: RangeDataset, raw: Any) -> dict[date, Any]:
    """Map each item in a range response to its local date. Items without one are dropped."""
    by_day: dict[date, list[Any]] = {}
    for item in dataset.items(raw):
        value = item.get(dataset.day_key) if isinstance(item, dict) else None
        if not isinstance(value, str):
            continue
        try:
            day = date.fromisoformat(value[:10])
        except ValueError:
            continue
        by_day.setdefault(day, []).append(item)
    return {day: found[0] if len(found) == 1 else found for day, found in by_day.items()}


def _for_range(
    dataset: RangeDataset, start: date, end: date
) -> Callable[[GarminPersonalApiClient], Any]:
    return lambda client: dataset.fetch(client, start, end)


def _activity_page(offset: int, start: date, end: date) -> Callable[[GarminPersonalApiClient], Any]:
    return lambda client: client.search_activities(
        start=offset, limit=ACTIVITY_PAGE_SIZE, start_date=start, end_date=end
    )


def _download(activity_id: int) -> Callable[[GarminPersonalApiClient], bytes]:
    return lambda client: client.download_activity(activity_id)


def _check_abort(exc: GarminError) -> None:
    if exc.status_code in (401, 429):
        raise SyncAborted(str(exc)) from None


def sync_daily(
    conn: Connection,
    api: Api,
    report: SyncReport,
    today: date,
    days_back: int | None = None,
    pause: Callable[[], None] = lambda: None,
    progress: Progress = _no_progress,
) -> None:
    plan = [
        (name, dataset, chunks(days_to_sync(conn, name, today, days_back), dataset.max_days))
        for name, dataset in DAILY_DATASETS.items()
    ]
    total = sum(len(runs) for _, _, runs in plan)
    done = 0
    for name, dataset, runs in plan:
        for run in runs:
            progress("Daily data", done, total, f"{name} {run[0]} to {run[-1]}")
            done += 1
            try:
                raw = api.call(_for_range(dataset, run[0], run[-1]))
            except GarminError as exc:
                if exc.status_code == 404:
                    raw = None
                else:
                    with conn:
                        for day in run:
                            db.record_sync(conn, name, day, "error", str(exc))
                    report.errors.append(f"{name} {run[0]} to {run[-1]}: {exc}")
                    _check_abort(exc)
                    continue
            finally:
                pause()
            by_day = split_by_day(dataset, raw)
            with conn:
                for day in run:
                    item = by_day.get(day)
                    if is_empty(item):
                        db.record_sync(conn, name, day, "empty")
                        report.empty[name] = report.empty.get(name, 0) + 1
                    else:
                        db.upsert_daily(conn, name, day, item)
                        db.record_sync(conn, name, day, "ok")
                        report.days[name] = report.days.get(name, 0) + 1
    progress("Daily data", total, total, "")


def _activity_fields(item: dict[str, Any]) -> tuple[str | None, date | None, str | None]:
    """Best-effort start time, local date and type; the raw JSON is kept regardless."""
    start_gmt = item.get("startTimeGMT")
    local = item.get("startTimeLocal")
    local_date = None
    if isinstance(local, str) and len(local) >= 10:
        try:
            local_date = date.fromisoformat(local[:10])
        except ValueError:
            local_date = None
    kind = item.get("activityType")
    type_key = kind.get("typeKey") if isinstance(kind, dict) else None
    return (
        start_gmt if isinstance(start_gmt, str) else None,
        local_date,
        type_key if isinstance(type_key, str) else None,
    )


def sync_activities(
    conn: Connection,
    api: Api,
    report: SyncReport,
    today: date,
    days_back: int | None = None,
    pause: Callable[[], None] = lambda: None,
    progress: Progress = _no_progress,
) -> None:
    days = days_to_sync(conn, ACTIVITIES, today, days_back)
    start, end = days[0], days[-1]
    offset = 0
    seen: set[int] = set()
    try:
        # Page until Garmin returns nothing new. A short page isn't a reliable end
        # marker, since the server may cap the page size below what was asked for.
        for page_number in range(MAX_ACTIVITY_PAGES):
            progress("Activities", page_number, 0, f"{start} to {end}")
            page = api.call(_activity_page(offset, start, end))
            pause()
            items = page if isinstance(page, list) else []
            new = 0
            with conn:
                for item in items:
                    activity_id = item.get("activityId") if isinstance(item, dict) else None
                    if not isinstance(activity_id, int) or activity_id in seen:
                        continue
                    seen.add(activity_id)
                    new += 1
                    start_gmt, local_date, kind = _activity_fields(item)
                    db.upsert_activity(
                        conn,
                        activity_id,
                        item,
                        start_time_utc=start_gmt,
                        calendar_date=local_date,
                        activity_type=kind,
                    )
                    report.activities += 1
            if not items or not new:
                break
            offset += len(items)
    except GarminError as exc:
        with conn:
            for day in days:
                db.record_sync(conn, ACTIVITIES, day, "error", str(exc))
        report.errors.append(f"activities {start}..{end}: {exc}")
        _check_abort(exc)
        return
    with conn:
        for day in days:
            db.record_sync(conn, ACTIVITIES, day, "ok")
    progress("Activities", 1, 1, f"{report.activities} found")


def _write_private(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(content)


def download_fit_files(
    conn: Connection,
    api: Api,
    paths: DataPaths,
    report: SyncReport,
    pause: Callable[[], None] = lambda: None,
    progress: Progress = _no_progress,
) -> None:
    """Save each activity's original file (a ZIP holding the FIT file) once."""
    paths.fit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    missing = db.activities_missing_fit(conn)
    for i, activity_id in enumerate(missing):
        progress("FIT files", i, len(missing), f"activity {activity_id}")
        try:
            content = api.call(_download(activity_id))
        except GarminError as exc:
            report.errors.append(f"FIT file for activity {activity_id}: {exc}")
            _check_abort(exc)
            continue
        finally:
            pause()
        name = f"{activity_id}.zip"
        _write_private(paths.fit_dir / name, content)
        with conn:
            db.set_fit_path(conn, activity_id, name)
        report.fit_files += 1
    if missing:
        progress("FIT files", len(missing), len(missing), "")


def run_sync(
    api: Api,
    paths: DataPaths,
    *,
    days_back: int | None = None,
    today: date | None = None,
    pause_s: float = 0.5,
    progress: Progress = _no_progress,
) -> SyncReport:
    today = today or datetime.now().astimezone().date()

    def pause() -> None:
        if pause_s:
            time.sleep(pause_s)

    report = SyncReport()
    with sync_lock(paths.sync_lock):
        conn = db.connect(paths.db)
        try:
            sync_daily(conn, api, report, today, days_back, pause, progress)
            sync_activities(conn, api, report, today, days_back, pause, progress)
            download_fit_files(conn, api, paths, report, pause, progress)
        except SyncAborted as exc:
            report.aborted = str(exc)
        finally:
            conn.close()
    return report
