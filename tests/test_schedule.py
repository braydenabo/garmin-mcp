import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from garmin_connect_personal_mcp import schedule
from garmin_connect_personal_mcp.config import DataPaths

PATHS = DataPaths(Path("/data dir/garmin"))


def job(env: dict[str, str] | None = None) -> schedule.Job:
    return schedule.make_job(PATHS, hour=6, minute=5, env=env or {})


# --- job definitions


@pytest.mark.parametrize(("text", "expected"), [("06:00", (6, 0)), ("23:59", (23, 59))])
def test_parse_time(text: str, expected: tuple[int, int]) -> None:
    assert schedule.parse_time(text) == expected


@pytest.mark.parametrize("text", ["6", "24:00", "12:60", "ab:cd", "1:2:3", ""])
def test_parse_time_rejects_bad_input(text: str) -> None:
    with pytest.raises(ValueError, match="HH:MM"):
        schedule.parse_time(text)


def test_job_runs_this_python_and_logs_to_data_folder() -> None:
    j = job()
    assert j.command == [sys.executable, "-m", "garmin_connect_personal_mcp", "sync"]
    assert j.log_file == PATHS.log_dir / "sync.log"
    assert j.time == "06:05"
    assert j.env == {}


def test_job_passes_data_dir_but_never_the_api_key() -> None:
    j = job({"GARMIN_MCP_DATA_DIR": "/custom", "GARMIN_PERSONAL_API_KEY": "fake-key-123"})
    assert j.env == {"GARMIN_MCP_DATA_DIR": "/custom"}


def test_launchd_plist() -> None:
    spec = plistlib.loads(schedule.launchd_plist(job({"GARMIN_MCP_DATA_DIR": "/custom"})))
    assert spec["Label"] == schedule.LAUNCHD_LABEL
    assert spec["ProgramArguments"][-2:] == ["garmin_connect_personal_mcp", "sync"]
    assert spec["StartCalendarInterval"] == {"Hour": 6, "Minute": 5}
    assert spec["StandardOutPath"] == str(PATHS.log_dir / "sync.log")
    assert spec["EnvironmentVariables"] == {"GARMIN_MCP_DATA_DIR": "/custom"}


def test_launchd_plist_without_env_has_no_env_key() -> None:
    assert "EnvironmentVariables" not in plistlib.loads(schedule.launchd_plist(job()))


def test_systemd_units_quote_and_escape() -> None:
    j = schedule.Job(
        command=["/opt/my python/bin/python", "-m", "garmin_connect_personal_mcp", "sync"],
        hour=6,
        minute=5,
        log_file=Path("/data/100%/sync.log"),
        env={"GARMIN_MCP_DATA_DIR": "/data/100%"},
    )
    service, timer = schedule.systemd_units(j)
    assert 'ExecStart="/opt/my python/bin/python" "-m"' in service
    assert 'Environment="GARMIN_MCP_DATA_DIR=/data/100%%"' in service
    assert f"StandardOutput=append:{str(j.log_file).replace('%', '%%')}" in service
    assert "OnCalendar=*-*-* 06:05:00" in timer
    assert "Persistent=true" in timer


def test_cron_line_is_valid_shell_with_escaped_percent() -> None:
    j = schedule.Job(
        command=["/opt/my python/python", "-m", "garmin_connect_personal_mcp", "sync"],
        hour=6,
        minute=5,
        log_file=Path("/data/50%/sync.log"),
        env={"GARMIN_MCP_DATA_DIR": "/data/50%"},
    )
    line = schedule.cron_line(j)
    assert line.startswith("5 6 * * * ")
    assert line.endswith(schedule.CRON_MARKER)
    command = line[len("5 6 * * * ") : -len(schedule.CRON_MARKER)].replace("\\%", "%")
    words = shlex.split(command)
    assert words[0] == "GARMIN_MCP_DATA_DIR=/data/50%"
    assert words[1] == "/opt/my python/python"
    assert words[-3:] == [">>", str(j.log_file), "2>&1"]
    assert "%" not in line.replace("\\%", "")


def test_windows_wrapper() -> None:
    j = schedule.Job(
        command=[r"C:\Users\Me\py\python.exe", "-m", "garmin_connect_personal_mcp", "sync"],
        hour=6,
        minute=5,
        log_file=Path("C:/Data/100%/sync.log"),
        env={"GARMIN_MCP_DATA_DIR": "C:/Data/100%"},
    )
    text = schedule.windows_wrapper(j)
    lines = text.split("\r\n")
    assert lines[0] == "@echo off"
    assert lines[1] == 'set "GARMIN_MCP_DATA_DIR=C:/Data/100%%"'
    assert lines[2].startswith(r'"C:\Users\Me\py\python.exe" "-m" "garmin_connect_personal_mcp"')
    log = str(j.log_file).replace("%", "%%")
    assert lines[2].endswith(f'>> "{log}" 2>&1')
    assert text.endswith("\r\n")


# --- install, remove and status (OS commands go to a fake runner)


class FakeRunner:
    """Records commands; `results` maps a command prefix to (returncode, stdout)."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.results: dict[tuple[str, ...], list[tuple[int, str]]] = {}
        self.crontab = ""

    def __call__(
        self, args: list[str], input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, input))
        if args[:2] == ["crontab", "-"]:
            self.crontab = input or ""
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["crontab", "-l"]:
            code = 0 if self.crontab else 1
            return subprocess.CompletedProcess(args, code, self.crontab, "")
        for prefix, queue in self.results.items():
            if tuple(args[: len(prefix)]) == prefix and queue:
                code, out = queue.pop(0)
                return subprocess.CompletedProcess(args, code, out, "boom" if code else "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def commands(self) -> list[list[str]]:
        return [args for args, _ in self.calls]


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    monkeypatch.setattr(schedule.time, "sleep", lambda s: None)
    return FakeRunner()


@pytest.fixture
def data(tmp_path: Path) -> DataPaths:
    return DataPaths(tmp_path / "data")


def test_launchd_install_status_remove(tmp_path: Path, runner: FakeRunner, data: DataPaths) -> None:
    backend = schedule.Launchd(tmp_path, runner)
    assert backend.status(data) == schedule.Status(False, "launchd", "not installed")
    backend.install(schedule.make_job(data, 7, 30, env={}), data)
    assert backend.plist.exists()
    assert [c[:2] for c in runner.commands()] == [
        ["launchctl", "bootout"],
        ["launchctl", "bootstrap"],
    ]
    assert backend.status(data).detail == "daily at 07:30 (loaded)"
    assert backend.remove(data)
    assert not backend.plist.exists()
    assert not backend.remove(data)


def test_launchd_retries_bootstrap_once(
    tmp_path: Path, runner: FakeRunner, data: DataPaths
) -> None:
    runner.results[("launchctl", "bootstrap")] = [(5, ""), (0, "")]
    schedule.Launchd(tmp_path, runner).install(schedule.make_job(data, env={}), data)
    assert sum(c[1] == "bootstrap" for c in runner.commands()) == 2


def test_launchd_reports_failure(tmp_path: Path, runner: FakeRunner, data: DataPaths) -> None:
    runner.results[("launchctl", "bootstrap")] = [(5, ""), (5, "")]
    with pytest.raises(schedule.ScheduleError, match="launchctl bootstrap"):
        schedule.Launchd(tmp_path, runner).install(schedule.make_job(data, env={}), data)


def test_launchd_status_when_not_loaded(
    tmp_path: Path, runner: FakeRunner, data: DataPaths
) -> None:
    backend = schedule.Launchd(tmp_path, runner)
    backend.install(schedule.make_job(data, env={}), data)
    runner.results[("launchctl", "print")] = [(113, "")]
    assert "not loaded" in backend.status(data).detail


def test_systemd_install_status_remove(tmp_path: Path, runner: FakeRunner, data: DataPaths) -> None:
    backend = schedule.Systemd(tmp_path, runner)
    assert not backend.status(data).installed
    backend.install(schedule.make_job(data, 6, 0, env={}), data)
    assert (backend.unit_dir / f"{schedule.NAME}.service").exists()
    assert ["systemctl", "--user", "enable", "--now", f"{schedule.NAME}.timer"] in (
        runner.commands()
    )
    runner.results[("systemctl", "--user", "is-enabled")] = [(0, "enabled\n")]
    assert backend.status(data).detail == "daily at 06:00 (enabled)"
    assert backend.remove(data)
    assert not (backend.unit_dir / f"{schedule.NAME}.service").exists()
    assert not backend.remove(data)


def test_cron_keeps_other_entries(tmp_path: Path, runner: FakeRunner, data: DataPaths) -> None:
    runner.crontab = "0 1 * * * /usr/bin/backup\n"
    backend = schedule.Cron(tmp_path, runner)
    backend.install(schedule.make_job(data, 6, 15, env={}), data)
    backend.install(schedule.make_job(data, 6, 45, env={}), data)  # replaces, not duplicates
    lines = runner.crontab.splitlines()
    assert lines[0] == "0 1 * * * /usr/bin/backup"
    assert sum(schedule.CRON_MARKER in line for line in lines) == 1
    assert backend.status(data).detail == "daily at 06:45"
    assert backend.remove(data)
    assert runner.crontab == "0 1 * * * /usr/bin/backup\n"
    assert not backend.remove(data)
    assert not backend.status(data).installed


def test_task_scheduler_install_status_remove(
    tmp_path: Path, runner: FakeRunner, data: DataPaths
) -> None:
    backend = schedule.TaskScheduler(tmp_path, runner)
    backend.install(schedule.make_job(data, 6, 0, env={}), data)
    create = runner.commands()[0]
    assert create[:2] == ["schtasks", "/Create"]
    assert create[create.index("/ST") + 1] == "06:00"
    assert create[create.index("/TR") + 1] == f'"{backend.wrapper(data)}"'
    assert backend.wrapper(data).read_bytes().count(b"\r\n") == 2
    runner.results[("schtasks", "/Query")] = [(0, "TaskName: x\nNext Run Time: 23/10/2026 06:00\n")]
    assert backend.status(data).detail == "next run: 23/10/2026 06:00"
    assert backend.remove(data)
    assert not backend.wrapper(data).exists()
    runner.results[("schtasks", "/Query")] = [(1, "")]
    assert not backend.status(data).installed


def test_choose_backend(
    tmp_path: Path, runner: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert isinstance(schedule.choose_backend("darwin", tmp_path, runner), schedule.Launchd)
    assert isinstance(schedule.choose_backend("win32", tmp_path, runner), schedule.TaskScheduler)

    monkeypatch.setattr(schedule.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner.results[("systemctl", "--user", "is-system-running")] = [(0, "running\n")]
    assert isinstance(schedule.choose_backend("linux", tmp_path, runner), schedule.Systemd)
    # No user systemd session (e.g. a container): fall back to cron.
    assert isinstance(schedule.choose_backend("linux", tmp_path, runner), schedule.Cron)

    monkeypatch.setattr(schedule.shutil, "which", lambda name: None)
    with pytest.raises(schedule.ScheduleError, match="own scheduler"):
        schedule.choose_backend("linux", tmp_path, runner)
