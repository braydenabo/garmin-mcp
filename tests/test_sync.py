"""Sync against a fake Garmin that returns synthetic range responses (no network).

Response shapes follow Garmin's Personal API Swagger docs.
"""

import math
import os
import sqlite3
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from garmin_connect_personal_mcp import cli, db, sync
from garmin_connect_personal_mcp.config import DataPaths
from garmin_connect_personal_mcp.garmin_client import GarminError

TODAY = date(2026, 3, 10)


def span(start: date, end: date) -> list[str]:
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


class FakeClient:
    """Synthetic range responses.

    `fail` maps (dataset, start_date), an activity ID, or "search_activities" to a status code.
    `skip` holds (dataset, iso_date) pairs to leave out of a response, as Garmin does for
    days without data.
    """

    def __init__(self) -> None:
        self.fail: dict[Any, int] = {}
        self.skip: set[tuple[str, str]] = set()
        self.activities: list[Any] = []
        self.page_cap: int | None = None  # simulate a server that caps page size
        self.ignore_offset = False  # simulate a server that ignores `start`
        self.calls: list[tuple[str, Any, Any]] = []
        self.downloads: list[int] = []

    def _range(self, dataset: str, start: date, end: date) -> list[str]:
        self.calls.append((dataset, start, end))
        if (dataset, start) in self.fail:
            code = self.fail[(dataset, start)]
            raise GarminError(f"HTTP {code}", status_code=code)
        return [d for d in span(start, end) if (dataset, d) not in self.skip]

    def get_daily_summaries(self, start: date, end: date) -> Any:
        days = self._range("daily_summary", start, end)
        return {"data": [{"calendarDate": d, "heartRate": {"restingValue": 52}} for d in days]}

    def get_daily_sleep_summaries(self, start: date, end: date) -> Any:
        days = self._range("sleep", start, end)
        return [{"calendarDate": d, "sleepTimeSeconds": 27000} for d in days]

    def get_daily_hrv_summaries(self, start: date, end: date) -> Any:
        days = self._range("hrv", start, end)
        return {"hrvSummaries": [{"calendarDate": d, "lastNightAvg": 50} for d in days]}

    def get_daily_stress(self, start: date, end: date) -> Any:
        days = self._range("stress", start, end)
        return [{"calendarDate": d, "values": {"overallStressLevel": 30}} for d in days]

    def get_daily_steps(self, start: date, end: date) -> Any:
        days = self._range("steps", start, end)
        return [{"calendarDate": d, "totalSteps": 9000} for d in days]

    def get_daily_body_battery(self, start: date, end: date) -> Any:
        days = self._range("body_battery", start, end)
        return [{"date": d, "charged": 60} for d in days]

    def get_weight_measurements(self, start: date, end: date) -> Any:
        self._range("weight", start, end)
        return {"dailyWeightSummaries": [], "totalAverage": {}}

    def search_activities(self, *, start: int, limit: int, start_date: date, end_date: date) -> Any:
        self.calls.append(("activities", start, (start_date, end_date)))
        if "search_activities" in self.fail:
            code = self.fail["search_activities"]
            raise GarminError(f"HTTP {code}", status_code=code)
        size = min(limit, self.page_cap or limit)
        offset = 0 if self.ignore_offset else start
        return self.activities[offset : offset + size]

    def download_activity(self, activity_id: int) -> bytes:
        if activity_id in self.fail:
            raise GarminError("HTTP 404", status_code=self.fail[activity_id])
        self.downloads.append(activity_id)
        return b"PK fake zip " + str(activity_id).encode()

    def requests_for(self, dataset: str) -> list[tuple[Any, Any]]:
        return [(s, e) for name, s, e in self.calls if name == dataset]


class FakeApi:
    def __init__(self, client: FakeClient) -> None:
        self.client = client

    def call(self, request: Callable[[Any], Any]) -> Any:
        return request(self.client)

    def __enter__(self) -> "FakeApi":
        return self

    def __exit__(self, *exc: object) -> None:
        pass


def activity(n: int, day: date = TODAY) -> dict[str, Any]:
    return {
        "activityId": 1000 + n,
        "startTimeLocal": f"{day.isoformat()} 07:00:00",
        "startTimeGMT": f"{day.isoformat()} 12:00:00",
        "activityType": {"typeKey": "running"},
    }


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    return DataPaths(tmp_path / "data")


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


def run(client: FakeClient, paths: DataPaths, **kwargs: Any) -> sync.SyncReport:
    kwargs.setdefault("today", TODAY)
    return sync.run_sync(FakeApi(client), paths, pause_s=0, **kwargs)


def open_db(paths: DataPaths) -> sqlite3.Connection:
    return db.connect(paths.db)


# --- helpers


def test_chunks_split_on_gaps_and_max_span() -> None:
    d = [TODAY + timedelta(days=i) for i in (0, 1, 2, 3, 4, 10, 11)]
    assert sync.chunks(d, 3) == [d[0:3], d[3:5], d[5:7]]
    assert sync.chunks([], 3) == []


def test_split_by_day_handles_odd_items() -> None:
    dataset = sync.DAILY_DATASETS["sleep"]
    raw = [
        {"calendarDate": "2026-03-01", "v": 1},
        {"calendarDate": "2026-03-02T00:00:00", "v": 2},
        {"calendarDate": "2026-03-02", "v": 3},
        {"calendarDate": "not a date"},
        {"other": 1},
        "junk",
    ]
    by_day = sync.split_by_day(dataset, raw)
    assert by_day[date(2026, 3, 1)] == {"calendarDate": "2026-03-01", "v": 1}
    assert [x["v"] for x in by_day[date(2026, 3, 2)]] == [2, 3]
    assert len(by_day) == 2
    assert sync.split_by_day(dataset, None) == {}
    assert sync.split_by_day(sync.DAILY_DATASETS["hrv"], {"unexpected": 1}) == {}


# --- daily data


def test_first_sync_is_one_request_per_dataset(client: FakeClient, paths: DataPaths) -> None:
    report = run(client, paths)
    assert report.ok
    first = TODAY - timedelta(days=6)
    for name in sync.DAILY_DATASETS:
        assert client.requests_for(name) == [(first, TODAY)]
    assert report.days["sleep"] == 7
    assert report.empty["weight"] == 7
    conn = open_db(paths)
    assert db.get_daily(conn, "hrv", TODAY) == {
        "calendarDate": TODAY.isoformat(),
        "lastNightAvg": 50,
    }
    assert db.get_daily(conn, "body_battery", TODAY)["charged"] == 60
    assert db.get_daily(conn, "weight", TODAY) is None
    assert db.last_synced_date(conn, "weight") == TODAY


def test_long_sync_respects_each_endpoints_max_span(client: FakeClient, paths: DataPaths) -> None:
    report = run(client, paths, days_back=90)
    assert report.ok
    for name, dataset in sync.DAILY_DATASETS.items():
        requests = client.requests_for(name)
        assert len(requests) == math.ceil(90 / dataset.max_days)
        assert all((end - start).days < dataset.max_days for start, end in requests)
        assert requests[0][0] == TODAY - timedelta(days=89)
        assert requests[-1][1] == TODAY
    assert report.days["daily_summary"] == 90
    assert len(client.requests_for("daily_summary")) == 1
    assert len(client.requests_for("stress")) == 4


def test_days_missing_from_response_count_as_no_data(client: FakeClient, paths: DataPaths) -> None:
    client.skip.add(("sleep", TODAY.isoformat()))
    report = run(client, paths)
    assert report.days["sleep"] == 6
    assert report.empty["sleep"] == 1
    assert db.get_daily(open_db(paths), "sleep", TODAY) is None


def test_rerun_resumes_and_rechecks_recent_days(client: FakeClient, paths: DataPaths) -> None:
    run(client, paths)
    client.calls.clear()
    tomorrow = TODAY + timedelta(days=1)
    run(client, paths, today=tomorrow)
    assert client.requests_for("sleep") == [(TODAY - timedelta(days=2), tomorrow)]


def test_404_counts_as_no_data(client: FakeClient, paths: DataPaths) -> None:
    client.fail[("hrv", TODAY - timedelta(days=6))] = 404
    report = run(client, paths)
    assert report.ok
    assert report.empty["hrv"] == 7


def test_failed_range_is_recorded_and_retried(client: FakeClient, paths: DataPaths) -> None:
    run(client, paths, days_back=60)
    old_start = TODAY - timedelta(days=59)
    client.fail[("stress", old_start)] = 500
    report = run(client, paths, days_back=60)
    assert any(e.startswith(f"stress {old_start}") for e in report.errors)
    assert report.days["sleep"] == 60  # other datasets keep going

    del client.fail[("stress", old_start)]
    client.calls.clear()
    report = run(client, paths)
    assert report.ok
    requests = client.requests_for("stress")
    assert requests[0] == (old_start, old_start + timedelta(days=27))  # failed days retried
    assert requests[-1] == (TODAY - timedelta(days=2), TODAY)  # plus the usual recheck


@pytest.mark.parametrize("status", [401, 429])
def test_rate_limit_or_bad_key_stops_the_sync(
    client: FakeClient, paths: DataPaths, status: int
) -> None:
    client.fail[("daily_summary", TODAY - timedelta(days=6))] = status
    report = run(client, paths)
    assert report.aborted
    assert len(client.calls) == 1


def test_progress_events(client: FakeClient, paths: DataPaths) -> None:
    events: list[tuple[str, int, int, str]] = []
    client.activities = [activity(1)]
    run(client, paths, days_back=30, progress=lambda *e: events.append(e))
    daily = [e for e in events if e[0] == "Daily data"]
    total = sum(math.ceil(30 / d.max_days) for d in sync.DAILY_DATASETS.values())
    assert daily[0] == ("Daily data", 0, total, "daily_summary 2026-02-09 to 2026-03-10")
    assert ("Daily data", 4, total, "stress 2026-03-09 to 2026-03-10") in daily
    assert daily[-1] == ("Daily data", total, total, "")
    assert [d for _, d, _, _ in daily] == list(range(total + 1))
    assert ("Activities", 1, 1, "1 found") in events
    assert events[-1] == ("FIT files", 1, 1, "")


# --- activities and FIT files


def test_activities_are_paged_and_stored(client: FakeClient, paths: DataPaths) -> None:
    client.activities = [activity(n) for n in range(sync.ACTIVITY_PAGE_SIZE + 5)]
    client.activities.append({"activityId": "not-an-int"})
    report = run(client, paths)
    assert report.activities == sync.ACTIVITY_PAGE_SIZE + 5
    page = sync.ACTIVITY_PAGE_SIZE
    assert [s for s, _ in client.requests_for("activities")] == [0, page, page + 6]
    row = open_db(paths).execute("SELECT * FROM activities WHERE activity_id = 1000").fetchone()
    assert row["calendar_date"] == TODAY.isoformat()
    assert row["start_time_utc"] == f"{TODAY.isoformat()} 12:00:00"
    assert row["activity_type"] == "running"


def test_activities_complete_when_server_caps_page_size(
    client: FakeClient, paths: DataPaths
) -> None:
    client.activities = [activity(n) for n in range(120)]
    client.page_cap = 50
    report = run(client, paths)
    assert report.activities == 120
    assert [s for s, _ in client.requests_for("activities")] == [0, 50, 100, 120]


def test_activities_stop_if_server_ignores_offset(client: FakeClient, paths: DataPaths) -> None:
    client.activities = [activity(n) for n in range(10)]
    client.ignore_offset = True
    report = run(client, paths)
    assert report.activities == 10
    assert len(client.requests_for("activities")) == 2


def test_activity_with_odd_fields_is_still_archived(client: FakeClient, paths: DataPaths) -> None:
    client.activities = [{"activityId": 7, "startTimeLocal": "garbage", "activityType": "run"}]
    run(client, paths)
    row = open_db(paths).execute("SELECT * FROM activities WHERE activity_id = 7").fetchone()
    assert row["calendar_date"] is None
    assert row["activity_type"] is None
    assert '"startTimeLocal":"garbage"' in row["raw_json"]


def test_fit_files_downloaded_once(client: FakeClient, paths: DataPaths) -> None:
    client.activities = [activity(1), activity(2)]
    report = run(client, paths)
    assert report.fit_files == 2
    saved = paths.fit_dir / "1001.zip"
    assert saved.read_bytes() == b"PK fake zip 1001"
    if os.name == "posix":
        assert saved.stat().st_mode & 0o777 == 0o600
    run(client, paths)
    assert client.downloads == [1001, 1002]


def test_failed_fit_download_is_retried_later(client: FakeClient, paths: DataPaths) -> None:
    client.activities = [activity(1), activity(2)]
    client.fail[1001] = 404
    report = run(client, paths)
    assert report.fit_files == 1
    assert any("1001" in e for e in report.errors)
    del client.fail[1001]
    report = run(client, paths)
    assert report.fit_files == 1
    assert client.downloads == [1002, 1001]


def test_activity_search_error_is_recorded(client: FakeClient, paths: DataPaths) -> None:
    client.fail["search_activities"] = 500
    report = run(client, paths)
    assert any("activities" in e for e in report.errors)
    statuses = open_db(paths).execute(
        "SELECT DISTINCT status FROM sync_log WHERE dataset = 'activities'"
    )
    assert [r["status"] for r in statuses] == ["error"]


# --- one sync at a time


def test_second_sync_is_refused_while_one_runs(client: FakeClient, paths: DataPaths) -> None:
    with sync.sync_lock(paths.sync_lock), pytest.raises(sync.SyncAlreadyRunning):
        run(client, paths)
    assert client.calls == []
    assert run(client, paths).ok  # lock released afterwards


def test_lock_released_when_sync_fails(client: FakeClient, paths: DataPaths) -> None:
    class Boom(FakeApi):
        def call(self, request: Callable[[Any], Any]) -> Any:
            raise RuntimeError("unexpected")

    with pytest.raises(RuntimeError):
        sync.run_sync(Boom(client), paths, today=TODAY, pause_s=0)
    assert run(client, paths).ok


# --- CLI


def test_cli_sync_prints_progress_and_summary(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client.activities = [activity(1, date.today())]
    monkeypatch.setattr(cli, "GarminApi", lambda: FakeApi(client))
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    assert cli.main(["sync", "--days-back", "2"]) == 0
    out = capsys.readouterr().out
    assert "Daily data" in out and "FIT files" in out
    assert "2 day(s) saved" in out
    assert "1 listed, 1 FIT file(s) saved" in out
    assert "27000" not in out  # no health data printed


def test_cli_sync_exit_code_on_errors(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client.fail[("sleep", date.today())] = 500
    monkeypatch.setattr(cli, "GarminApi", lambda: FakeApi(client))
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    assert cli.main(["sync", "--days-back", "1"]) == 1
    assert "sleep" in capsys.readouterr().err


def test_cli_sync_after_rate_limit_says_how_to_continue(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client.fail[("daily_summary", date.today() - timedelta(days=89))] = 429
    monkeypatch.setattr(cli, "GarminApi", lambda: FakeApi(client))
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    assert cli.main(["sync", "--days-back", "90"]) == 1
    assert "sync --days-back 90" in capsys.readouterr().err


def test_cli_sync_when_another_is_running(
    client: FakeClient,
    paths: DataPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "GarminApi", lambda: FakeApi(client))
    with sync.sync_lock(cli.config.data_paths().sync_lock):
        assert cli.main(["sync"]) == 1
    assert "already running" in capsys.readouterr().err


def test_cli_sync_ctrl_c_stops_cleanly(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = 0

    class Interrupted(FakeApi):
        def call(self, request: Callable[[Any], Any]) -> Any:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise KeyboardInterrupt
            return super().call(request)

    monkeypatch.setattr(cli, "GarminApi", lambda: Interrupted(client))
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    assert cli.main(["sync", "--days-back", "5"]) == 130
    out = capsys.readouterr().out
    assert "Stopped. Everything saved so far is kept" in out
    assert "sync --days-back 5" in out
    conn = db.connect(cli.config.data_paths().db)
    assert db.get_daily(conn, "sleep", date.today()) is not None  # finished work kept
    assert db.last_synced_date(conn, "stress") is None  # interrupted dataset untouched
    with sync.sync_lock(cli.config.data_paths().sync_lock):
        pass  # lock was released


def test_cli_sync_without_key_explains(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["sync"]) == 1
    assert "init" in capsys.readouterr().err


def test_cli_sync_rejects_bad_days_back(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["sync", "--days-back", "0"]) == 2
