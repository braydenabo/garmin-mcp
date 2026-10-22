"""Command-line entry point: `garmin-connect-personal-mcp`."""

import argparse
from collections.abc import Sequence

from garmin_connect_personal_mcp import __version__

PROG = "garmin-connect-personal-mcp"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Unofficial MCP server for your own Garmin Connect data.",
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
