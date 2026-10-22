"""Regenerate docs/tools.md and the tool contract snapshot from the server.

Usage: uv run python scripts/gen_tools_doc.py
Run it after a deliberate change to a tool, and review the diff.
"""

import tempfile
from pathlib import Path

from garmin_connect_personal_mcp.config import DataPaths
from garmin_connect_personal_mcp.contract import contract_json, tools_markdown

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        paths = DataPaths(Path(tmp))
        (ROOT / "docs" / "tools.md").write_text(tools_markdown(paths), encoding="utf-8")
        (ROOT / "tests" / "snapshots" / "tools.json").write_text(
            contract_json(paths), encoding="utf-8"
        )
    print("Updated docs/tools.md and tests/snapshots/tools.json")


if __name__ == "__main__":
    main()
