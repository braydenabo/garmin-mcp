import plistlib
import shlex
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
