"""Run `sync` once a day using the operating system's own scheduler.

macOS uses a launchd LaunchAgent, Linux a systemd user timer (or cron when
systemd isn't available), and Windows Task Scheduler. Each job runs the same
Python that installed it and appends its output to logs/sync.log in the data
folder. The API key is never written into a job definition.
"""

import os
import plistlib
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from garmin_connect_personal_mcp.config import DATA_DIR_ENV_VAR, DataPaths

NAME = "garmin-connect-personal-mcp-sync"
LAUNCHD_LABEL = "garmin-connect-personal-mcp.sync"
CRON_MARKER = "# garmin-connect-personal-mcp sync"


@dataclass(frozen=True)
class Job:
    command: list[str]
    hour: int
    minute: int
    log_file: Path
    env: dict[str, str] = field(default_factory=dict)

    @property
    def time(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"


def parse_time(value: str) -> tuple[int, int]:
    """Parse HH:MM (24-hour) into (hour, minute)."""
    try:
        hour_text, minute_text = value.split(":")
        hour, minute = int(hour_text), int(minute_text)
    except ValueError:
        raise ValueError(f"Invalid time {value!r}; use 24-hour HH:MM, e.g. 06:00.") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time {value!r}; use 24-hour HH:MM, e.g. 06:00.")
    return hour, minute


def make_job(
    paths: DataPaths, hour: int = 6, minute: int = 0, env: dict[str, str] | None = None
) -> Job:
    """A job that runs this installation's sync. Only non-secret settings are passed on."""
    environment = os.environ if env is None else env
    passed = (
        {DATA_DIR_ENV_VAR: environment[DATA_DIR_ENV_VAR]}
        if environment.get(DATA_DIR_ENV_VAR)
        else {}
    )
    return Job(
        command=[sys.executable, "-m", "garmin_connect_personal_mcp", "sync"],
        hour=hour,
        minute=minute,
        log_file=paths.log_dir / "sync.log",
        env=passed,
    )


def launchd_plist(job: Job) -> bytes:
    spec: dict[str, object] = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": job.command,
        "StartCalendarInterval": {"Hour": job.hour, "Minute": job.minute},
        "StandardOutPath": str(job.log_file),
        "StandardErrorPath": str(job.log_file),
        "ProcessType": "Background",
    }
    if job.env:
        spec["EnvironmentVariables"] = job.env
    return plistlib.dumps(spec)


def _systemd_quote(arg: str) -> str:
    escaped = arg.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def systemd_units(job: Job) -> tuple[str, str]:
    """The (service, timer) unit files."""
    command = " ".join(_systemd_quote(arg) for arg in job.command)
    log = str(job.log_file).replace("%", "%%")
    env_lines = "".join(
        f"Environment={_systemd_quote(f'{key}={value}')}\n" for key, value in job.env.items()
    )
    service = (
        "[Unit]\n"
        "Description=Sync Garmin data into the local archive\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={command}\n"
        f"{env_lines}"
        f"StandardOutput=append:{log}\n"
        f"StandardError=append:{log}\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Daily Garmin sync\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar=*-*-* {job.time}:00\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def cron_line(job: Job) -> str:
    """One crontab line. `%` is special in crontab, so it's escaped."""
    env = " ".join(f"{key}={shlex.quote(value)}" for key, value in job.env.items())
    command = " ".join(shlex.quote(arg) for arg in job.command)
    log = shlex.quote(str(job.log_file))
    line = f"{env + ' ' if env else ''}{command} >> {log} 2>&1".replace("%", "\\%")
    return f"{job.minute} {job.hour} * * * {line} {CRON_MARKER}"


def windows_wrapper(job: Job) -> str:
    """A .cmd script for Task Scheduler, which can't redirect output itself.

    `%` expands variables in .cmd files, so it's doubled.
    """

    def cmd(text: str) -> str:
        return text.replace("%", "%%")

    lines = ["@echo off"]
    lines += [f'set "{key}={cmd(value)}"' for key, value in job.env.items()]
    command = " ".join(f'"{cmd(arg)}"' for arg in job.command)
    lines.append(f'{command} >> "{cmd(str(job.log_file))}" 2>&1')
    return "\r\n".join(lines) + "\r\n"
