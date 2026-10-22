"""Sync in the background when the MCP server starts and the archive is stale.

A client launching the server is a good moment to catch up, so answers aren't
based on yesterday's data. It never delays startup, never writes to stdout
(which carries the MCP protocol over stdio), and only logs problems to stderr.
"""

import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from garmin_connect_personal_mcp import config, db

STALE_AFTER = timedelta(hours=24)


def is_stale(paths: config.DataPaths, now: datetime | None = None) -> bool:
    """True if there's no successful sync in the last 24 hours."""
    if not paths.db.exists():
        return True
    conn = db.connect_readonly(paths.db)
    try:
        last = db.last_sync_time(conn)
    finally:
        conn.close()
    if last is None:
        return True
    return (now or datetime.now(UTC)) - datetime.fromisoformat(last) > STALE_AFTER


def _sync_quietly(paths: config.DataPaths) -> None:
    from garmin_connect_personal_mcp.garmin_client import GarminApi, GarminError
    from garmin_connect_personal_mcp.sync import SyncAlreadyRunning, run_sync

    try:
        with GarminApi() as api:
            report = run_sync(api, paths)
        if not report.ok:
            print(
                f"garmin-mcp: background sync finished with problems: {report.errors[:3]}",
                file=sys.stderr,
            )
    except SyncAlreadyRunning:
        pass
    except GarminError as exc:
        print(f"garmin-mcp: background sync skipped: {exc}", file=sys.stderr)
    except Exception as exc:  # never let a background sync take the server down
        print(f"garmin-mcp: background sync failed: {exc!r}", file=sys.stderr)


def start_if_stale(
    paths: config.DataPaths,
    *,
    has_key: Callable[[], bool] | None = None,
    start: Callable[[Callable[[], None]], None] | None = None,
) -> bool:
    """Start a background sync if the archive is stale and a key is saved; True if started."""
    key_present = has_key or (lambda: config.api_key_file().exists() or config.api_key_from_env())
    if not key_present() or not is_stale(paths):
        return False
    launch = start or (
        lambda fn: threading.Thread(target=fn, name="garmin-sync", daemon=True).start()
    )
    launch(lambda: _sync_quietly(paths))
    return True
