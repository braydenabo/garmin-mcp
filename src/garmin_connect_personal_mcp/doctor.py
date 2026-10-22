"""`doctor`: check a setup and produce a report that is safe to paste publicly.

Each check returns a status and, when something is wrong, the fix. The report
never contains the API key, health values, or the user's home folder path:
paths are shown relative to `~`, and the finished text is scanned for the key
as a last line of defence.
"""

import os
import platform
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from garmin_personal_api import __version__ as garmin_client_version

from garmin_connect_personal_mcp import __version__, config, db
from garmin_connect_personal_mcp.sync import SyncAlreadyRunning, sync_lock

PROG = "garmin-connect-personal-mcp"
Status = Literal["ok", "info", "warn", "fail"]


@dataclass
class Check:
    name: str
    status: Status
    summary: str
    fix: str | None = None
    details: list[str] = field(default_factory=list)


class Redactor:
    """Hides the home folder and any known secret in report text."""

    def __init__(self, home: Path, secrets: list[str]) -> None:
        self.home = str(home)
        self.secrets = [s for s in secrets if len(s) >= 8]

    def __call__(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]")
        if self.home and self.home not in ("/", "\\"):
            text = text.replace(self.home, "~")
        return text

    def check(self, check: Check) -> Check:
        return Check(
            name=check.name,
            status=check.status,
            summary=self(check.summary),
            fix=None if check.fix is None else self(check.fix),
            details=[self(d) for d in check.details],
        )


def check_versions() -> Check:
    return Check(
        "Versions",
        "info",
        f"{PROG} {__version__}, Python {platform.python_version()}, "
        f"{platform.system()} {platform.release()}, Garmin client {garmin_client_version}",
    )


def check_api_key(env: dict[str, str] | None = None) -> Check:
    environment = dict(os.environ) if env is None else env
    key_file = config.api_key_file()
    if config.api_key_from_env(environment):
        detail = "the key file also exists" if key_file.exists() else "no key file"
        return Check(
            "API key",
            "ok",
            f"from ${config.API_KEY_ENV_VAR} ({detail})",
            details=["A scheduled sync can't see environment variables; it needs the key file."],
        )
    if not key_file.exists():
        return Check(
            "API key",
            "fail",
            "no key saved",
            fix=f"Create a key at {config.API_KEY_URL}, then run `{PROG} init`.",
        )
    if not key_file.read_text(encoding="utf-8").strip():
        return Check("API key", "fail", f"{key_file} is empty", fix=f"Run `{PROG} init --force`.")
    if config.readable_by_others(key_file):
        return Check(
            "API key",
            "warn",
            f"saved in {key_file}, but other users can read it",
            fix=f"Run `chmod 600 {key_file}`.",
        )
    return Check("API key", "ok", f"saved in {key_file} (readable only by you)")


def check_archive(paths: config.DataPaths) -> list[Check]:
    """The data folder, the archive's health, what's in it, and recent sync errors."""
    if not paths.root.exists():
        return [
            Check(
                "Data folder",
                "fail",
                f"{paths.root} doesn't exist",
                fix=f"Run `{PROG} init`, then `{PROG} sync`.",
            )
        ]
    checks = [Check("Data folder", "ok", str(paths.root))]
    if not paths.db.exists():
        checks.append(
            Check("Archive", "warn", "no archive yet", fix=f"Run `{PROG} sync` to create it.")
        )
        return checks
    try:
        conn = db.connect_readonly(paths.db)
    except sqlite3.Error as exc:
        checks.append(
            Check(
                "Archive",
                "fail",
                f"can't open {paths.db}: {exc}",
                fix="Restore it from a backup, or move it aside and run sync to rebuild it.",
            )
        )
        return checks
    try:
        checks += _archive_contents(conn, paths)
    except sqlite3.Error as exc:
        checks.append(Check("Archive", "fail", f"can't read {paths.db}: {exc}"))
    finally:
        conn.close()
    return checks


def _archive_contents(conn: sqlite3.Connection, paths: config.DataPaths) -> list[Check]:
    version = db.schema_version(conn)
    size_mb = paths.db.stat().st_size / 1_000_000
    if version > db.SCHEMA_VERSION:
        archive = Check(
            "Archive",
            "fail",
            f"schema version {version} is newer than this package supports ({db.SCHEMA_VERSION})",
            fix=f"Upgrade {PROG}.",
        )
    elif version < db.SCHEMA_VERSION:
        archive = Check(
            "Archive",
            "info",
            f"schema version {version}; it upgrades to {db.SCHEMA_VERSION} on the next sync",
        )
    else:
        archive = Check("Archive", "ok", f"schema version {version}, {size_mb:.1f} MB")

    ok_days = dict(
        conn.execute(
            "SELECT dataset, COUNT(*) FROM sync_log WHERE status = 'ok' GROUP BY dataset"
        ).fetchall()
    )
    activities = conn.execute("SELECT COUNT(*), COUNT(fit_path) FROM activities").fetchone()
    last = db.last_sync_time(conn)
    contents = Check(
        "Contents",
        "ok" if ok_days or activities[0] else "warn",
        f"last successful sync {last or 'never'} (UTC)",
        fix=None if ok_days or activities[0] else f"Run `{PROG} sync`.",
        details=[f"{name}: {count} day(s) with data" for name, count in sorted(ok_days.items())]
        + [f"activities: {activities[0]}, FIT files: {activities[1]}"],
    )

    errors = conn.execute(
        """
        SELECT dataset, COUNT(*) AS days, MAX(calendar_date) AS latest,
               (SELECT error FROM sync_log e WHERE e.dataset = s.dataset AND e.status = 'error'
                ORDER BY calendar_date DESC LIMIT 1) AS message
        FROM sync_log s WHERE status = 'error' GROUP BY dataset ORDER BY dataset
        """
    ).fetchall()
    if errors:
        problems = Check(
            "Sync errors",
            "warn",
            f"{sum(row['days'] for row in errors)} day(s) failed and will be retried",
            fix=f"Run `{PROG} sync`. If it keeps failing, include this report in an issue.",
            details=[
                f"{row['dataset']}: {row['days']} day(s), latest {row['latest']}: {row['message']}"
                for row in errors
            ],
        )
    else:
        problems = Check("Sync errors", "ok", "none")
    return [archive, contents, problems]


def check_sync_running(paths: config.DataPaths) -> Check:
    if not paths.root.exists():
        return Check("Sync running", "info", "no")
    try:
        with sync_lock(paths.sync_lock):
            pass
    except SyncAlreadyRunning:
        return Check("Sync running", "info", "yes, a sync is running right now")
    return Check("Sync running", "info", "no")


def check_log(
    paths: config.DataPaths, now: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> Check:
    log = paths.log_dir / "sync.log"
    if not log.exists():
        return Check("Sync log", "info", "none yet (created by the daily schedule)")
    stat = log.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, UTC)
    age_h = (now() - modified).total_seconds() / 3600
    summary = f"{log}, {stat.st_size / 1000:.0f} KB, last written {age_h:.0f} hour(s) ago"
    return Check("Sync log", "ok", summary)


def local_checks(paths: config.DataPaths) -> list[Check]:
    return [
        check_versions(),
        check_api_key(),
        *check_archive(paths),
        check_sync_running(paths),
        check_log(paths),
    ]


def as_dicts(checks: list[Check]) -> list[dict[str, object]]:
    return [asdict(check) for check in checks]
