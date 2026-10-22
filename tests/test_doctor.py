import os
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from garmin_connect_personal_mcp import config, db, doctor, schedule, sync
from garmin_connect_personal_mcp.garmin_client import GarminError

posix_only = pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX-only")


@pytest.fixture
def paths() -> config.DataPaths:
    return config.data_paths()  # points at the test's temp folder (see conftest)


def by_name(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {c.name: c for c in checks}


# --- redaction


def test_redactor_hides_home_and_secrets(isolated_home: Path) -> None:
    redact = doctor.Redactor(isolated_home, ["fake-key-123456", "short"])
    text = f"{isolated_home}/.config/garmin key=fake-key-123456 short"
    assert redact(text) == "~/.config/garmin key=[redacted] short"


def test_redactor_applies_to_every_field(isolated_home: Path) -> None:
    redact = doctor.Redactor(isolated_home, ["fake-key-123456"])
    check = doctor.Check(
        "x", "fail", f"{isolated_home}/a", "fix fake-key-123456", [f"{isolated_home}"]
    )
    clean = redact.check(check)
    assert clean.summary == "~/a" and clean.fix == "fix [redacted]" and clean.details == ["~"]


# --- versions and key


def test_versions_are_listed() -> None:
    summary = doctor.check_versions().summary
    assert "garmin-connect-personal-mcp" in summary and "Python" in summary


def test_key_missing() -> None:
    check = doctor.check_api_key({})
    assert check.status == "fail" and "init" in (check.fix or "")


def test_key_saved() -> None:
    config.save_api_key("fake-key-123456")
    check = doctor.check_api_key({})
    assert check.status == "ok"
    assert "fake-key" not in check.summary


@posix_only
def test_key_readable_by_others() -> None:
    path = config.save_api_key("fake-key-123456")
    path.chmod(0o644)
    check = doctor.check_api_key({})
    assert check.status == "warn" and "chmod 600" in (check.fix or "")


def test_key_file_empty() -> None:
    path = config.api_key_file()
    path.parent.mkdir(parents=True)
    path.write_text("\n")
    assert doctor.check_api_key({}).status == "fail"


def test_key_from_env_mentions_schedule() -> None:
    check = doctor.check_api_key({"GARMIN_PERSONAL_API_KEY": "fake-key-123456"})
    assert check.status == "ok" and "scheduled" in check.details[0]


# --- archive


def test_no_data_folder(paths: config.DataPaths) -> None:
    checks = doctor.check_archive(paths)
    assert [(c.name, c.status) for c in checks] == [("Data folder", "fail")]


def test_no_archive_yet(paths: config.DataPaths) -> None:
    config.ensure_data_dir(paths)
    checks = by_name(doctor.check_archive(paths))
    assert checks["Archive"].status == "warn"


def test_archive_contents_and_errors(paths: config.DataPaths) -> None:
    conn = db.connect(paths.db)
    db.upsert_daily(conn, "sleep", date(2026, 3, 1), {"sleepTimeSeconds": 27000})
    db.record_sync(conn, "sleep", date(2026, 3, 1), "ok")
    db.record_sync(conn, "sleep", date(2026, 3, 2), "empty")
    db.record_sync(
        conn, "hrv", date(2026, 3, 1), "error", "Garmin's servers returned an error (HTTP 503)."
    )
    db.record_sync(
        conn, "hrv", date(2026, 3, 2), "error", "Garmin's rate limit was reached (HTTP 429)."
    )
    db.upsert_activity(conn, 1, {"x": 1})
    conn.commit()
    conn.close()

    checks = by_name(doctor.check_archive(paths))
    assert checks["Archive"].status == "ok"
    assert "schema version" in checks["Archive"].summary
    assert "sleep: 1 day(s) with data" in checks["Contents"].details
    assert "activities: 1, FIT files: 0" in checks["Contents"].details
    errors = checks["Sync errors"]
    assert errors.status == "warn" and "2 day(s)" in errors.summary
    assert errors.details == [
        "hrv: 2 day(s), latest 2026-03-02: Garmin's rate limit was reached (HTTP 429)."
    ]
    report = repr(checks)
    assert "27000" not in report  # no health values


def test_newer_archive_needs_upgrade(paths: config.DataPaths) -> None:
    conn = db.connect(paths.db)
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    conn.close()
    archive = by_name(doctor.check_archive(paths))["Archive"]
    assert archive.status == "fail" and "Upgrade" in (archive.fix or "")


def test_older_archive_upgrades_on_next_sync(paths: config.DataPaths) -> None:
    db.connect(paths.db).close()
    conn = db.connect(paths.db)
    conn.execute("PRAGMA user_version = 0")
    conn.close()
    archive = by_name(doctor.check_archive(paths))["Archive"]
    assert archive.status == "info" and "next sync" in archive.summary


def test_corrupt_archive(paths: config.DataPaths) -> None:
    config.ensure_data_dir(paths)
    paths.db.write_bytes(b"this is not a database" * 100)
    checks = by_name(doctor.check_archive(paths))
    assert checks["Archive"].status == "fail"


def test_empty_archive_suggests_sync(paths: config.DataPaths) -> None:
    db.connect(paths.db).close()
    contents = by_name(doctor.check_archive(paths))["Contents"]
    assert contents.status == "warn" and "sync" in (contents.fix or "")


# --- lock and log


def test_sync_running(paths: config.DataPaths) -> None:
    assert doctor.check_sync_running(paths).summary == "no"
    config.ensure_data_dir(paths)
    assert doctor.check_sync_running(paths).summary == "no"
    with sync.sync_lock(paths.sync_lock):
        assert "yes" in doctor.check_sync_running(paths).summary


def test_log(paths: config.DataPaths) -> None:
    assert "none yet" in doctor.check_log(paths).summary
    config.ensure_data_dir(paths)
    (paths.log_dir / "sync.log").write_text("x" * 3000)
    later = lambda: datetime.now(UTC) + timedelta(hours=5)  # noqa: E731
    summary = doctor.check_log(paths, now=later).summary
    assert "3 KB" in summary and "5 hour(s) ago" in summary


def test_local_checks_run_on_a_fresh_machine(paths: config.DataPaths) -> None:
    names = [c.name for c in doctor.local_checks(paths)]
    assert names[:3] == ["Versions", "API key", "Data folder"]
    assert doctor.as_dicts(doctor.local_checks(paths))[0]["name"] == "Versions"


# --- Garmin access and schedule


class FakeApi:
    closed = False

    def close(self) -> None:
        FakeApi.closed = True


def raises(status: int | None) -> Callable[[], Any]:
    def factory() -> Any:
        raise GarminError(f"HTTP {status}", status_code=status)

    return factory


def test_garmin_ok() -> None:
    check = doctor.check_garmin(lambda: FakeApi())  # type: ignore[arg-type,return-value]
    assert check.status == "ok" and FakeApi.closed


@pytest.mark.parametrize(
    ("status", "expected", "has_fix"),
    [(401, "fail", False), (429, "warn", True), (None, "fail", True)],
)
def test_garmin_failures(status: int | None, expected: str, has_fix: bool) -> None:
    check = doctor.check_garmin(raises(status))
    assert check.status == expected
    assert (check.fix is not None) == has_fix


def test_garmin_without_key_explains() -> None:
    check = doctor.check_garmin()
    assert check.status == "fail" and "init" in check.summary


class FakeBackend:
    def __init__(self, installed: bool) -> None:
        self.installed = installed

    def status(self, paths: config.DataPaths) -> schedule.Status:
        detail = "daily at 06:00 (loaded)" if self.installed else "not installed"
        return schedule.Status(self.installed, "launchd", detail)


def test_schedule_installed(paths: config.DataPaths) -> None:
    check = doctor.check_schedule(lambda: FakeBackend(True), paths)  # type: ignore[arg-type,return-value]
    assert check.status == "ok" and "06:00" in check.summary


def test_schedule_not_installed(paths: config.DataPaths) -> None:
    check = doctor.check_schedule(lambda: FakeBackend(False), paths)  # type: ignore[arg-type,return-value]
    assert check.status == "info" and "schedule install" in (check.fix or "")


def test_schedule_unsupported(paths: config.DataPaths) -> None:
    def none() -> schedule.Backend:
        raise schedule.ScheduleError("No supported scheduler found")

    assert "No supported" in doctor.check_schedule(none, paths).summary


def test_run_checks_order_and_skips(paths: config.DataPaths) -> None:
    backend = lambda: FakeBackend(False)  # noqa: E731
    names = [c.name for c in doctor.run_checks(paths, backend_factory=backend)]  # type: ignore[arg-type]
    assert names[:3] == ["Versions", "API key", "Garmin access"]
    assert names[-1] == "Daily sync"
    no_key = by_name(doctor.run_checks(paths, backend_factory=backend))  # type: ignore[arg-type]
    assert no_key["Garmin access"].summary == "skipped (no API key)"

    config.save_api_key("fake-key-123456")
    offline = by_name(doctor.run_checks(paths, offline=True, backend_factory=backend))  # type: ignore[arg-type]
    assert offline["Garmin access"].summary == "skipped (--offline)"
    called = by_name(
        doctor.run_checks(paths, api_factory=raises(401), backend_factory=backend)  # type: ignore[arg-type]
    )
    assert called["Garmin access"].status == "fail"
