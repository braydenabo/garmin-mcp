"""The eval answer key: it runs on a demo archive and finds the patterns the questions ask about."""

import importlib.util
import re
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from garmin_connect_personal_mcp import config, demo

ROOT = Path(__file__).resolve().parent.parent
END = date(2026, 3, 31)


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("eval_key", ROOT / "scripts" / "eval_key.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def key(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    folder = tmp_path_factory.mktemp("eval") / "demo"
    demo.build(config.DataPaths(folder), end=END)
    return load_script().answer_key(folder)  # type: ignore[no-any-return]


def test_every_keyed_question_is_in_the_question_list(key: dict[str, Any]) -> None:
    questions = (ROOT / "evals" / "questions.md").read_text(encoding="utf-8")
    ids = set(re.findall(r"^### ([a-z0-9-]+)$", questions, re.MULTILINE))
    assert set(key) <= ids, set(key) - ids


def test_the_patterns_the_questions_ask_about_are_there(key: dict[str, Any]) -> None:
    sick = key["sick-spell"]
    assert sick["dates"] == [str(END - timedelta(26)), str(END - timedelta(22))]
    assert sick["resting_hr_bpm"]["during"] > sick["resting_hr_bpm"]["two_weeks_before"] + 5
    assert sick["hrv_last_night_ms"]["during"] < sick["hrv_last_night_ms"]["two_weeks_before"] - 5
    assert sick["workouts_during"] == 0
    assert key["training-block"]["hrv_difference_ms"] < 0
    assert key["hr-drift"]["last_quarter_hr_bpm"] > key["hr-drift"]["first_quarter_hr_bpm"]
    assert key["weekend-sleep"]["weekend"] > key["weekend-sleep"]["weekday"]
    assert key["not-worn"]["dates"] == sorted(
        str(END - timedelta(d)) for d in demo.NOT_WORN if d < 90
    )
    assert key["runs-last-30-days"]["runs"] > 0 and key["longest-run"]["distance_km"] > 10
    assert key["sleep-last-night"]["date"] == str(END)


def test_refuses_a_folder_that_is_not_a_demo(tmp_path: Path) -> None:
    (tmp_path / "DEMO.json").write_text('{"demo": false}')
    with pytest.raises(SystemExit, match="isn't a demo"):
        load_script().answer_key(tmp_path)
