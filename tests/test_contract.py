"""The tool contract can't change by accident, and the tools doc can't drift."""

import difflib
from pathlib import Path

from garmin_connect_personal_mcp.config import DataPaths
from garmin_connect_personal_mcp.contract import contract_json, tools_contract, tools_markdown

ROOT = Path(__file__).resolve().parent.parent
FIX = (
    "If the change is deliberate, run `uv run python scripts/gen_tools_doc.py` and review the diff."
)


def _diff(expected: str, actual: str, name: str) -> str:
    return "".join(
        difflib.unified_diff(expected.splitlines(True), actual.splitlines(True), name, "now")
    )


def test_tool_contract_matches_snapshot(tmp_path: Path) -> None:
    snapshot = (ROOT / "tests" / "snapshots" / "tools.json").read_text(encoding="utf-8")
    current = contract_json(DataPaths(tmp_path))
    assert current == snapshot, (
        f"Tool contract changed. {FIX}\n{_diff(snapshot, current, 'snapshot')}"
    )


def test_tools_doc_is_current(tmp_path: Path) -> None:
    doc = (ROOT / "docs" / "tools.md").read_text(encoding="utf-8")
    current = tools_markdown(DataPaths(tmp_path))
    assert current == doc, f"docs/tools.md is out of date. {FIX}"


def test_input_schemas_are_portable(tmp_path: Path) -> None:
    """Flat inputs only: no anyOf/oneOf/allOf or nested objects, which some models handle badly."""
    for tool in tools_contract(DataPaths(tmp_path)):
        text = str(tool["input_schema"])
        for word in ("anyOf", "oneOf", "allOf", "$ref"):
            assert word not in text, f"{tool['name']} input schema uses {word}"
        for name, spec in tool["input_schema"].get("properties", {}).items():
            assert spec.get("type") in ("string", "integer", "number", "boolean"), (
                tool["name"],
                name,
            )
            assert spec.get("description"), f"{tool['name']}.{name} needs a description"


def test_every_tool_is_prefixed_and_described(tmp_path: Path) -> None:
    for tool in tools_contract(DataPaths(tmp_path)):
        assert tool["name"].startswith("garmin_")
        assert tool["title"] and 40 <= len(tool["description"]) <= 400, tool["name"]
        assert set(tool["annotations"]) >= {
            "read_only_hint",
            "destructive_hint",
            "idempotent_hint",
            "open_world_hint",
        }
        assert tool["output_schema"], f"{tool['name']} needs a typed output"
