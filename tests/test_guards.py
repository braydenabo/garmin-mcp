import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def run(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True
    )


@pytest.mark.parametrize(
    "path",
    [
        "data/garmin.db",
        "garmin.sqlite3",
        "fit/12345.fit",
        "export.zip",
        "ride.gpx",
        ".env",
        ".env.local",
        "connect-personal-api-key",
        ".plans/build-plan.md",
        "Garmin MCP Server — Build Plan.md",
        "phase1.plan.md",
        "certs\\server.pem",
    ],
)
def test_forbidden_files_blocked(path: str) -> None:
    result = run("check_forbidden_files.py", path)
    assert result.returncode == 1
    assert "blocked" in result.stderr


@pytest.mark.parametrize("path", ["README.md", "src/x.py", ".env.example", "docs/plan-ahead.md"])
def test_normal_files_allowed(path: str) -> None:
    assert run("check_forbidden_files.py", path).returncode == 0


def test_large_file_blocked(tmp_path: Path) -> None:
    big = tmp_path / "big.json"
    big.write_bytes(b"0" * 1_000_001)
    assert run("check_forbidden_files.py", str(big)).returncode == 1
