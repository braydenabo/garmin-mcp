"""Command-line entry point: `garmin-connect-personal-mcp`."""

import argparse
import getpass
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from garmin_connect_personal_mcp import __version__, clients, config, db, doctor, schedule
from garmin_connect_personal_mcp.garmin_client import GarminApi, GarminError
from garmin_connect_personal_mcp.progress import ProgressDisplay
from garmin_connect_personal_mcp.sync import DAILY_DATASETS, SyncAlreadyRunning, run_sync

PROG = "garmin-connect-personal-mcp"
MAX_DAYS_BACK = 3650  # 10 years; a guard against typos, not a Garmin limit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Unofficial MCP server for your own Garmin Connect data.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    init = commands.add_parser(
        "init",
        help="save your Garmin Personal API key and create the data folder",
        description=(
            "Save your Garmin Personal API key (with owner-only permissions) and create the "
            f"local data folder. Create a key at {config.API_KEY_URL}"
        ),
    )
    init.add_argument(
        "--key-stdin",
        action="store_true",
        help="read the key from standard input instead of prompting",
    )
    init.add_argument("--force", action="store_true", help="replace an existing saved key")

    sync = commands.add_parser(
        "sync",
        help="download new data from Garmin into the local archive",
        description=(
            "Download new data from Garmin into the local archive. Each dataset resumes from "
            "its last good day, re-checks the last two days, and retries earlier failures."
        ),
    )
    sync.add_argument(
        "--days-back",
        type=int,
        metavar="N",
        help=f"sync the last N days (including today) instead of resuming; at most {MAX_DAYS_BACK}",
    )

    sched = commands.add_parser(
        "schedule",
        help="run sync automatically once a day",
        description=(
            "Run `sync` once a day with your operating system's scheduler (launchd on macOS, "
            "systemd or cron on Linux, Task Scheduler on Windows). Output goes to "
            "logs/sync.log in the data folder."
        ),
    )
    actions = sched.add_subparsers(dest="action", metavar="ACTION", required=True)
    install = actions.add_parser("install", help="install or update the daily sync")
    install.add_argument(
        "--time", default="06:00", metavar="HH:MM", help="local time to run (default 06:00)"
    )
    actions.add_parser("remove", help="remove the daily sync")
    actions.add_parser("status", help="show whether the daily sync is installed")

    check = commands.add_parser(
        "doctor",
        help="check your setup; the report is safe to paste into an issue",
        description=(
            "Check the API key, Garmin access, the archive, recent sync errors and the daily "
            "schedule. The report never includes your API key, health data or home folder path."
        ),
    )
    check.add_argument("--offline", action="store_true", help="skip the request to Garmin")
    check.add_argument("--json", action="store_true", help="print the report as JSON")

    install_client = commands.add_parser(
        "install-client",
        help="add this server to an MCP client (Claude, Cursor, VS Code, Gemini, Codex)",
        description=(
            "Show, or with --write apply, the configuration that adds this server to an MCP "
            "client. Without a client name, lists the supported clients."
        ),
    )
    install_client.add_argument(
        "client",
        nargs="?",
        choices=list(clients.CLIENTS),
        metavar="CLIENT",
        help="client to set up",
    )
    install_client.add_argument(
        "--write",
        action="store_true",
        help="apply the change (JSON configs are backed up first); without it, only show it",
    )

    serve = commands.add_parser(
        "serve",
        help="run the MCP server over stdio (what MCP clients launch)",
        description=(
            "Run the MCP server over stdio. MCP clients such as Claude Desktop, Claude Code, "
            "Cursor and Gemini CLI start this command themselves; you rarely run it by hand. "
            "Each option can also be set with the environment variable shown."
        ),
    )
    serve.add_argument(
        "--read-only",
        action="store_true",
        default=_env_flag("GARMIN_MCP_READ_ONLY"),
        help="never contact Garmin: hide garmin_sync and skip auto-sync (GARMIN_MCP_READ_ONLY=1)",
    )
    serve.add_argument(
        "--no-auto-sync",
        dest="auto_sync",
        action="store_false",
        default=os.environ.get("GARMIN_MCP_AUTO_SYNC", "1") != "0",
        help="don't sync on startup when the data is over a day old (GARMIN_MCP_AUTO_SYNC=0)",
    )
    serve.add_argument(
        "--toolsets",
        default=os.environ.get("GARMIN_MCP_TOOLSETS", "daily,activities,sync,charts"),
        metavar="LIST",
        help="comma-separated: daily, activities, sync, charts (GARMIN_MCP_TOOLSETS); "
        "garmin_describe_data is always on",
    )
    return parser


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def cmd_init(args: argparse.Namespace) -> int:
    paths = config.data_paths()
    config.ensure_data_dir(paths)
    print(f"Data folder: {paths.root}")

    key_file = config.api_key_file()
    if config.api_key_from_env():
        print(f"Using the API key from ${config.API_KEY_ENV_VAR}; nothing saved to disk.")
        return 0
    if key_file.exists() and not args.force:
        print(f"API key already saved at {key_file}. Use --force to replace it.")
        if config.readable_by_others(key_file):
            print(f"Warning: other users can read it. Run: chmod 600 {key_file}")
        return 0

    if args.key_stdin:
        key = sys.stdin.read()
    else:
        print(f"Create a Personal API key at {config.API_KEY_URL}")
        key = getpass.getpass("Paste your API key (input is hidden): ")
    try:
        saved = config.save_api_key(key, key_file)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"API key saved to {saved} (readable only by you).")
    print(f"Next, download your data: {PROG} sync")
    print(f"Then keep it up to date automatically: {PROG} schedule install")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    if args.days_back is not None and not 1 <= args.days_back <= MAX_DAYS_BACK:
        print(f"Error: --days-back must be between 1 and {MAX_DAYS_BACK}.", file=sys.stderr)
        return 2
    paths = config.data_paths()
    config.ensure_data_dir(paths)
    try:
        api = GarminApi()
    except GarminError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    display = ProgressDisplay(sys.stdout)
    rerun = PROG + " sync" + (f" --days-back {args.days_back}" if args.days_back else "")
    with api:
        try:
            report = run_sync(api, paths, days_back=args.days_back, progress=display)
        except SyncAlreadyRunning as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            display.close()
            print(f"Stopped. Everything saved so far is kept. To continue, run: {rerun}")
            return 130
        finally:
            display.close()

    print("Done.")
    for dataset in DAILY_DATASETS:
        saved, empty = report.days.get(dataset, 0), report.empty.get(dataset, 0)
        if saved or empty:
            print(f"  {dataset:<14} {saved} day(s) saved, {empty} with no data")
    print(f"  {'activities':<14} {report.activities} listed, {report.fit_files} FIT file(s) saved")
    for error in report.errors:
        print(f"Error: {error}", file=sys.stderr)
    if report.aborted:
        print(f"Stopped early: {report.aborted}", file=sys.stderr)
        print(f"Everything saved so far is kept. To continue later, run: {rerun}", file=sys.stderr)
    print(f"Archive: {paths.db}")
    return 0 if report.ok else 1


def _temporary_python() -> bool:
    """True when running from uvx's cache, which can be cleared at any time."""
    return "archive-v" in sys.executable


def cmd_schedule(args: argparse.Namespace) -> int:
    paths = config.data_paths()
    try:
        backend = schedule.choose_backend()
        if args.action == "install":
            return _schedule_install(args, backend, paths)
        if args.action == "remove":
            removed = backend.remove(paths)
            print("Daily sync removed." if removed else "No daily sync was installed.")
            return 0
    except (schedule.ScheduleError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    status = backend.status(paths)
    print(f"Daily sync: {status.detail} [{status.backend}]")
    if not status.installed:
        print(f"Turn it on with: {PROG} schedule install")
    last = None
    if paths.db.exists():
        conn = db.connect_readonly(paths.db)
        try:
            last = db.last_sync_time(conn)
        finally:
            conn.close()
    print(f"Last successful sync: {last or 'never'} (UTC)")
    print(f"Log: {paths.log_dir / 'sync.log'}")
    return 0


def _schedule_install(
    args: argparse.Namespace, backend: schedule.Backend, paths: config.DataPaths
) -> int:
    hour, minute = schedule.parse_time(args.time)
    if not config.api_key_file().exists():
        if config.api_key_from_env():
            print(
                f"Error: your API key is only in ${config.API_KEY_ENV_VAR}, which a scheduled "
                f"job can't see. Save it to a file first: {PROG} init",
                file=sys.stderr,
            )
        else:
            print(f"Error: no API key saved yet. Run: {PROG} init", file=sys.stderr)
        return 1
    if _temporary_python():
        print(
            "Warning: this is running from uvx's temporary cache, which can be cleared at any "
            "time, and the scheduled job would stop working. For a lasting install run: "
            "uv tool install garmin-connect-personal-mcp, then "
            f"{PROG} schedule install",
            file=sys.stderr,
        )
    config.ensure_data_dir(paths)
    job = schedule.make_job(paths, hour, minute)
    where = backend.install(job, paths)
    print(f"Daily sync installed: every day at {job.time} ({where}).")
    print(f"Output is appended to {job.log_file}")
    print(f"Check it with: {PROG} schedule status")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = doctor.run_checks(config.data_paths(), offline=args.offline)
    redact = doctor.Redactor(Path.home(), doctor.known_secrets())
    checks = [redact.check(c) for c in checks]
    if args.json:
        text = doctor.render_json(checks)
    else:
        marks = doctor.MARKS if _can_print("".join(doctor.MARKS.values())) else doctor.ASCII_MARKS
        text = doctor.render_text(checks, marks)
    sys.stdout.write(redact(text))
    return 1 if any(c.status == "fail" for c in checks) else 0


def _can_print(text: str) -> bool:
    try:
        text.encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def cmd_install_client(args: argparse.Namespace) -> int:
    if args.client is None:
        print("Supported clients:")
        for key, cls in clients.CLIENTS.items():
            print(f"  {key:<15} {cls.title}")
        print(f"\nPreview: {PROG} install-client <client>")
        print(f"Apply:   {PROG} install-client <client> --write")
        print("ChatGPT needs the remote (HTTP) server, which isn't available yet.")
        return 0
    client = clients.get_client(args.client)
    entry = clients.server_entry()
    if _temporary_python():
        print(
            "Warning: this is running from uvx's temporary cache, which can be cleared at any "
            "time and would break the client's configuration. For a lasting install run: "
            f"uv tool install garmin-connect-personal-mcp, then {PROG} install-client "
            f"{args.client}",
            file=sys.stderr,
        )
    if not (config.api_key_file().exists() or config.api_key_from_env()):
        print(
            f"Note: no API key saved yet. The server will start, but it has no data until you "
            f"run `{PROG} init` and `{PROG} sync`.",
            file=sys.stderr,
        )
    if not args.write:
        print(f"{client.title}:\n")
        print(client.preview(entry))
        print(f"To apply this automatically: {PROG} install-client {args.client} --write")
        return 0
    try:
        result = client.write(entry)
    except clients.ClientError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(result.message)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    # Imported here so the rest of the CLI starts fast and doesn't need the MCP SDK loaded.
    from garmin_connect_personal_mcp import autosync
    from garmin_connect_personal_mcp.server import build_server

    toolsets = tuple(t.strip() for t in args.toolsets.split(",") if t.strip())
    paths = config.data_paths()
    try:
        server = build_server(paths, toolsets=toolsets, read_only=args.read_only)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if args.auto_sync and not args.read_only:
        autosync.start_if_stale(paths)
    server.run("stdio")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    if args.command == "sync":
        return cmd_sync(args)
    if args.command == "schedule":
        return cmd_schedule(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "install-client":
        return cmd_install_client(args)
    parser.print_help()
    return 0
