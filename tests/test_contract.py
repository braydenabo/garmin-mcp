"""The tool contract can't change by accident, and the tools doc can't drift."""

import difflib
from pathlib import Path

from garmin_connect_personal_mcp import metrics
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


def test_data_doc_lists_every_metric() -> None:
    text = (ROOT / "docs" / "data.md").read_text(encoding="utf-8")
    for name, metric in metrics.METRICS.items():
        assert f"| `{name}` | {metric.unit} | {metric.description} |" in text, name


# --- stability: within a major version the public API can only grow


def test_public_api_is_backward_compatible(tmp_path: Path) -> None:
    import json

    from garmin_connect_personal_mcp import __version__
    from garmin_connect_personal_mcp.contract import breaking_changes, public_api

    major = max(1, int(__version__.split(".")[0]))
    frozen_file = ROOT / "tests" / "snapshots" / f"public-api-v{major}.json"
    assert frozen_file.exists(), f"Run `uv run python scripts/freeze_api.py` for version {major}."
    frozen = json.loads(frozen_file.read_text(encoding="utf-8"))
    problems = breaking_changes(frozen, public_api(tools_contract(DataPaths(tmp_path))))
    assert not problems, (
        "These would break clients and prompts that depend on the current major version. "
        "Keep the old name working (add, don't rename), or save it for the next major "
        "version; see Stability in CONTRIBUTING.md:\n" + "\n".join(problems)
    )


def _api(**tool: object) -> dict[str, object]:
    base = {
        "arguments": {"metric": {"type": "string", "required": True, "enum": ["a", "b"]}},
        "output": {"value": "number|null", "notes": "array"},
    }
    return {
        "tools": {"garmin_x": {**base, **tool}},
        "metrics": {"steps": "steps"},
        "query_columns": {"daily": ["date", "steps"]},
        "resources": ["garmin://schema"],
    }


def test_additions_are_allowed() -> None:
    from garmin_connect_personal_mcp.contract import breaking_changes

    grown = _api(
        arguments={
            "metric": {"type": "string", "required": True, "enum": ["a", "b", "c"]},
            "days": {"type": "integer", "required": False},
        },
        output={"value": "number|null", "notes": "array", "extra": "string"},
    )
    grown["tools"]["garmin_new"] = grown["tools"]["garmin_x"]  # type: ignore[index]
    grown["metrics"] = {"steps": "steps", "floors": "floors"}
    assert breaking_changes(_api(), grown) == []


def test_breaking_changes_are_caught() -> None:
    from garmin_connect_personal_mcp.contract import breaking_changes

    old = _api()
    cases = {
        "removed": ({"tools": {}}, "tool garmin_x was removed"),
        "arg renamed": (
            _api(arguments={"metric_name": {"type": "string", "required": True}}),
            "argument metric was removed",
        ),
        "arg retyped": (
            _api(arguments={"metric": {"type": "integer", "required": True}}),
            "changed type",
        ),
        "enum shrank": (
            _api(arguments={"metric": {"type": "string", "required": True, "enum": ["a"]}}),
            "lost allowed values",
        ),
        "new required": (
            _api(
                arguments={
                    "metric": {"type": "string", "required": True, "enum": ["a", "b"]},
                    "days": {"type": "integer", "required": True},
                }
            ),
            "new argument days is required",
        ),
        "output removed": (_api(output={"notes": "array"}), "output field value was removed"),
        "output retyped": (
            _api(output={"value": "string", "notes": "array"}),
            "output field value changed type",
        ),
    }
    for label, (change, message) in cases.items():
        new = {**old, **change}
        problems = breaking_changes(old, new)
        assert any(message in p for p in problems), (label, problems)
    unit = {**old, "metrics": {"steps": "thousands"}}
    assert any("changed unit" in p for p in breaking_changes(old, unit))
    gone = {**old, "query_columns": {"daily": ["date"]}, "resources": []}
    assert len(breaking_changes(old, gone)) == 2
