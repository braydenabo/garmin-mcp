"""The chart view (ui/chart.html): self-contained, and its drawing code works.

The drawing functions are plain JavaScript, so they're run with Node when it's
installed (it is on GitHub's Linux runners). No browser needed.
"""

import json
import re
import shutil
import subprocess
from importlib.resources import files
from typing import Any

import pytest

HTML = files("garmin_connect_personal_mcp").joinpath("ui/chart.html").read_text(encoding="utf-8")
SCRIPT = re.search(r"<script>(.*)</script>", HTML, re.S).group(1)  # type: ignore[union-attr]
NODE = shutil.which("node")
node = pytest.mark.skipif(NODE is None, reason="needs Node.js")


def test_view_is_small_and_self_contained() -> None:
    assert len(HTML.encode()) < 20_000
    # No external scripts, styles, fonts, images or network calls.
    assert not re.search(r"(src|href)\s*=", HTML)
    for word in ("http://", "https://", "fetch(", "XMLHttpRequest", "WebSocket", "@import", "url("):
        assert word not in HTML, word


def test_view_speaks_the_mcp_apps_protocol() -> None:
    for method in (
        "ui/initialize",
        "ui/notifications/initialized",
        "ui/notifications/tool-result",
        "ui/notifications/host-context-changed",
        "ui/notifications/size-changed",
    ):
        assert method in HTML
    assert '"2026-01-26"' in HTML
    assert "event.source !== window.parent" in HTML  # ignore messages from anyone else


def render(fn: str, data: Any) -> str:
    code = SCRIPT + f"\nprocess.stdout.write(module.exports.{fn}({json.dumps(data)}));"
    result = subprocess.run(
        [str(NODE), "-e", "var module = {exports: {}};" + code],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def chart(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "title": "Total sleep",
        "metric": "sleep_hours",
        "unit": "hours",
        "kind": "bar",
        "bucket": "day",
        "start": "2026-03-09",
        "end": "2026-03-15",
        "labels": [f"2026-03-{d:02d}" for d in range(9, 16)],
        "series": [
            {"name": "Total sleep", "role": "primary", "values": [7, 8, None, 6.5, 7.2, 8.1, 7]}
        ],
        "average": 7.3,
        "days_with_data": 6,
        "notes": [],
    }
    data.update(overrides)
    return data


@node
def test_bars_leave_gaps_for_missing_days() -> None:
    svg = render("renderChart", chart())
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert svg.count("<rect") == 6  # 7 days, one without data
    assert "NaN" not in svg and "undefined" not in svg
    assert "Mon 9" in svg and "Sun 15" in svg
    assert "2026-03-09: 7 hours" in svg  # hover label


@node
def test_stacked_sleep_stages() -> None:
    series = [
        {"name": "Deep", "role": "deep", "values": [1.5, None]},
        {"name": "Light", "role": "light", "values": [4, None]},
        {"name": "REM", "role": "rem", "values": [2, None]},
        {"name": "Awake", "role": "awake", "values": [0.5, None]},
    ]
    data = chart(kind="stacked", labels=["2026-03-14", "2026-03-15"], series=series, average=8)
    svg = render("renderChart", data)
    assert svg.count("<rect") == 4
    assert "var(--deep)" in svg and "var(--awake)" in svg
    view = render("renderView", data)
    assert 'class="legend"' in view and "<table" in view


@node
def test_line_breaks_at_gaps() -> None:
    values = [50, 51, None, 49, 48]
    data = chart(
        kind="line",
        unit="bpm",
        labels=chart()["labels"][:5],
        series=[{"name": "HR", "role": "primary", "values": values}],
    )
    svg = render("renderChart", data)
    path = re.search(r'<path d="([^"]+)"', svg).group(1)  # type: ignore[union-attr]
    assert path.count("M") == 2  # the line restarts after the missing day
    assert svg.count("<circle") == 4


@node
def test_text_is_escaped() -> None:
    view = render("renderView", chart(title='<img src=x onerror="alert(1)">', notes=["a < b"]))
    assert "<img" not in view and "&lt;img" in view and "a &lt; b" in view


@node
def test_empty_chart_renders() -> None:
    data = chart(series=[{"name": "x", "role": "primary", "values": [None] * 7}], average=None)
    svg = render("renderChart", data)
    assert "<rect" not in svg and "NaN" not in svg


@node
def test_long_ranges_thin_out_labels() -> None:
    labels = [f"2026-0{1 + d // 28}-{1 + d % 28:02d}" for d in range(90)]
    data = chart(labels=labels, series=[{"name": "x", "role": "primary", "values": [5] * 90}])
    svg = render("renderChart", data)
    assert svg.count('text-anchor="middle"') <= 11
