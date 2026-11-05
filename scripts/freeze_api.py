"""Record the stable public API for a major version.

Usage: uv run python scripts/freeze_api.py

Writes tests/snapshots/public-api-v<major>.json from the current server. Within a
major version the test suite fails on anything that would break it (see
"Stability" in CONTRIBUTING.md). Run this only when starting a new major version,
or to record additions you want to promise: a frozen API can only grow.
"""

import json
import tempfile
from pathlib import Path

from garmin_connect_personal_mcp import __version__
from garmin_connect_personal_mcp.config import DataPaths
from garmin_connect_personal_mcp.contract import breaking_changes, public_api, tools_contract

ROOT = Path(__file__).resolve().parent.parent


def frozen_path() -> Path:
    major = max(1, int(__version__.split(".")[0]))
    return ROOT / "tests" / "snapshots" / f"public-api-v{major}.json"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        current = public_api(tools_contract(DataPaths(Path(tmp))))
    path = frozen_path()
    if path.exists():
        problems = breaking_changes(json.loads(path.read_text(encoding="utf-8")), current)
        if problems:
            print("Refusing: these break the frozen API for this major version:")
            print("\n".join(f"  - {p}" for p in problems))
            return 1
    path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
