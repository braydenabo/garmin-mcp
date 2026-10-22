"""Command-line entry point: `garmin-connect-personal-mcp`."""

import argparse
import getpass
import sys
from collections.abc import Sequence

from garmin_connect_personal_mcp import __version__, config

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
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    parser.print_help()
    return 0
