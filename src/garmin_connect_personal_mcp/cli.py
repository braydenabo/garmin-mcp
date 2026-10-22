"""Command-line entry point: `garmin-connect-personal-mcp`."""

import argparse
import getpass
import sys
from collections.abc import Sequence

from garmin_connect_personal_mcp import __version__, config
from garmin_connect_personal_mcp.garmin_client import GarminApi, GarminError
from garmin_connect_personal_mcp.progress import ProgressDisplay
from garmin_connect_personal_mcp.sync import DAILY_DATASETS, run_sync

PROG = "garmin-connect-personal-mcp"


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
        help="sync the last N days (including today) instead of resuming",
    )
    return parser


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
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    if args.days_back is not None and args.days_back < 1:
        print("Error: --days-back must be at least 1.", file=sys.stderr)
        return 2
    paths = config.data_paths()
    config.ensure_data_dir(paths)
    try:
        api = GarminApi()
    except GarminError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    display = ProgressDisplay(sys.stdout)
    with api:
        try:
            report = run_sync(api, paths, days_back=args.days_back, progress=display)
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
        rerun = PROG + " sync" + (f" --days-back {args.days_back}" if args.days_back else "")
        print(f"Everything saved so far is kept. To continue later, run: {rerun}", file=sys.stderr)
    print(f"Archive: {paths.db}")
    return 0 if report.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    if args.command == "sync":
        return cmd_sync(args)
    parser.print_help()
    return 0
