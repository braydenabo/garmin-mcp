"""Run `sync` once a day using the operating system's own scheduler.

macOS uses a launchd LaunchAgent, Linux a systemd user timer (or cron when
systemd isn't available), and Windows Task Scheduler. Each job runs the same
Python that installed it and appends its output to logs/sync.log in the data
folder. The API key is never written into a job definition.
"""

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
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


# --- installing, removing and checking

Runner = Callable[..., subprocess.CompletedProcess[str]]


def run_command(args: list[str], input: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, input=input, capture_output=True, text=True, check=False)  # noqa: S603


class ScheduleError(Exception):
    """The OS scheduler refused or isn't available; the message says what to do."""


@dataclass(frozen=True)
class Status:
    installed: bool
    backend: str
    detail: str


class Backend:
    name = ""

    def __init__(self, home: Path, run: Runner = run_command) -> None:
        self.home = home
        self.run = run

    def install(self, job: Job, paths: DataPaths) -> str:
        raise NotImplementedError

    def remove(self, paths: DataPaths) -> bool:
        raise NotImplementedError

    def status(self, paths: DataPaths) -> Status:
        raise NotImplementedError

    def _check(self, args: list[str], input: str | None = None) -> str:
        result = self.run(args, input=input)
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip()
            raise ScheduleError(f"`{' '.join(args[:3])}` failed: {message or result.returncode}")
        return result.stdout


class Launchd(Backend):
    name = "launchd"

    @property
    def plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

    def _domain(self) -> str:
        uid = getattr(os, "getuid", lambda: 0)()  # getuid only exists on POSIX
        return f"gui/{uid}"

    def install(self, job: Job, paths: DataPaths) -> str:
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        self.plist.write_bytes(launchd_plist(job))
        self.run(["launchctl", "bootout", f"{self._domain()}/{LAUNCHD_LABEL}"])  # may not exist
        bootstrap = ["launchctl", "bootstrap", self._domain(), str(self.plist)]
        if self.run(bootstrap).returncode != 0:
            time.sleep(1)  # launchd can briefly refuse a job it has just unloaded
            self._check(bootstrap)
        return f"launchd agent {self.plist}"

    def remove(self, paths: DataPaths) -> bool:
        if not self.plist.exists():
            return False
        self.run(["launchctl", "bootout", f"{self._domain()}/{LAUNCHD_LABEL}"])
        self.plist.unlink()
        return True

    def status(self, paths: DataPaths) -> Status:
        if not self.plist.exists():
            return Status(False, self.name, "not installed")
        spec = plistlib.loads(self.plist.read_bytes())
        when = spec.get("StartCalendarInterval", {})
        loaded = self.run(["launchctl", "print", f"{self._domain()}/{LAUNCHD_LABEL}"])
        state = "loaded" if loaded.returncode == 0 else "installed but not loaded; reinstall it"
        at = f"{when.get('Hour', 0):02d}:{when.get('Minute', 0):02d}"
        return Status(True, self.name, f"daily at {at} ({state})")


class Systemd(Backend):
    name = "systemd"

    @property
    def unit_dir(self) -> Path:
        return self.home / ".config" / "systemd" / "user"

    def install(self, job: Job, paths: DataPaths) -> str:
        service, timer = systemd_units(job)
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        (self.unit_dir / f"{NAME}.service").write_text(service)
        (self.unit_dir / f"{NAME}.timer").write_text(timer)
        self._check(["systemctl", "--user", "daemon-reload"])
        self._check(["systemctl", "--user", "enable", "--now", f"{NAME}.timer"])
        return f"systemd user timer {NAME}.timer"

    def remove(self, paths: DataPaths) -> bool:
        timer = self.unit_dir / f"{NAME}.timer"
        if not timer.exists():
            return False
        self.run(["systemctl", "--user", "disable", "--now", f"{NAME}.timer"])
        timer.unlink()
        (self.unit_dir / f"{NAME}.service").unlink(missing_ok=True)
        self.run(["systemctl", "--user", "daemon-reload"])
        return True

    def status(self, paths: DataPaths) -> Status:
        timer = self.unit_dir / f"{NAME}.timer"
        if not timer.exists():
            return Status(False, self.name, "not installed")
        at = next(
            (
                line.split()[-1][:5]
                for line in timer.read_text().splitlines()
                if line.startswith("OnCalendar=")
            ),
            "?",
        )
        enabled = self.run(["systemctl", "--user", "is-enabled", f"{NAME}.timer"])
        state = enabled.stdout.strip() or "unknown"
        return Status(True, self.name, f"daily at {at} ({state})")


class Cron(Backend):
    name = "cron"

    def _lines(self) -> list[str]:
        result = self.run(["crontab", "-l"])
        return result.stdout.splitlines() if result.returncode == 0 else []

    def install(self, job: Job, paths: DataPaths) -> str:
        lines = [line for line in self._lines() if CRON_MARKER not in line]
        self._check(["crontab", "-"], input="\n".join([*lines, cron_line(job)]) + "\n")
        return "crontab entry"

    def remove(self, paths: DataPaths) -> bool:
        lines = self._lines()
        kept = [line for line in lines if CRON_MARKER not in line]
        if len(kept) == len(lines):
            return False
        self._check(["crontab", "-"], input="\n".join(kept) + "\n" if kept else "")
        return True

    def status(self, paths: DataPaths) -> Status:
        line = next((line for line in self._lines() if CRON_MARKER in line), None)
        if line is None:
            return Status(False, self.name, "not installed")
        minute, hour = line.split()[:2]
        return Status(True, self.name, f"daily at {int(hour):02d}:{int(minute):02d}")


class TaskScheduler(Backend):
    name = "Task Scheduler"
    task = "garmin-connect-personal-mcp sync"

    def wrapper(self, paths: DataPaths) -> Path:
        return paths.root / "scheduled-sync.cmd"

    def install(self, job: Job, paths: DataPaths) -> str:
        wrapper = self.wrapper(paths)
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text(windows_wrapper(job), newline="")
        self._check(
            [
                "schtasks",
                "/Create",
                "/F",
                "/SC",
                "DAILY",
                "/ST",
                job.time,
                "/TN",
                self.task,
                "/TR",
                f'"{wrapper}"',
            ]
        )
        return f"scheduled task '{self.task}'"

    def remove(self, paths: DataPaths) -> bool:
        result = self.run(["schtasks", "/Delete", "/F", "/TN", self.task])
        self.wrapper(paths).unlink(missing_ok=True)
        return result.returncode == 0

    def status(self, paths: DataPaths) -> Status:
        result = self.run(["schtasks", "/Query", "/TN", self.task, "/FO", "LIST"])
        if result.returncode != 0:
            return Status(False, self.name, "not installed")
        at = next(
            (
                line.split(":", 1)[1].strip()
                for line in result.stdout.splitlines()
                if line.lower().startswith("next run time")
            ),
            "scheduled",
        )
        return Status(True, self.name, f"next run: {at}")


def choose_backend(
    platform: str = sys.platform, home: Path | None = None, run: Runner = run_command
) -> Backend:
    """The scheduler for this OS: launchd, Task Scheduler, or systemd with cron as a fallback."""
    home = home or Path.home()
    if platform == "darwin":
        return Launchd(home, run)
    if platform == "win32":
        return TaskScheduler(home, run)
    if shutil.which("systemctl") and run(
        ["systemctl", "--user", "is-system-running"]
    ).stdout.strip() not in ("", "offline"):
        return Systemd(home, run)
    if shutil.which("crontab"):
        return Cron(home, run)
    raise ScheduleError(
        "No supported scheduler found (systemd user services or cron). Run "
        "`garmin-connect-personal-mcp sync` from your own scheduler instead."
    )
