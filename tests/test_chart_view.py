"""The chart view (ui/chart.html): self-contained, and its drawing code works.

The drawing functions are plain JavaScript, so they're run with Node when it's
installed (it is on GitHub's Linux runners). No browser needed.
"""

import json
import re
import shutil
import subprocess
from importlib.resources import files
from itertools import pairwise
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
    for word in ("http://", "https://", "fetch(", "XMLHttpRequest", "WebSocket", "@import"):
        assert word not in HTML, word
    assert not re.search(r"url\((?!#)", HTML), "only in-document url(#id) references are allowed"


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


def render_with(fn: str, *args: Any) -> str:
    call = ", ".join(json.dumps(a) for a in args)
    code = SCRIPT + f"\nprocess.stdout.write(String(module.exports.{fn}({call})));"
    result = subprocess.run(
        [str(NODE), "-e", "var module = {exports: {}};" + code],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def stacked_week() -> dict[str, Any]:
    series = [
        {"name": "Deep", "role": "deep", "values": [1.5, None]},
        {"name": "Light", "role": "light", "values": [4, None]},
        {"name": "REM", "role": "rem", "values": [2, None]},
        {"name": "Awake", "role": "awake", "values": [0.5, None]},
    ]
    return chart(
        title="Sleep stages",
        kind="stacked",
        labels=["2026-03-14", "2026-03-15"],
        series=series,
        average=8,
        start="2026-03-14",
    )


@node
def test_bars_leave_gaps_for_missing_days() -> None:
    svg = render("renderChart", chart())
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert svg.count('class="col"') == 6  # 7 days, one without data
    assert "NaN" not in svg and "undefined" not in svg
    assert ">Mon<" in svg and ">Sun<" in svg
    assert "Mon, Mar 9: 7h 0m" in svg  # hover label as hours and minutes


@node
def test_stacked_sleep_stages() -> None:
    data = stacked_week()
    svg = render("renderChart", data)
    assert svg.count('class="col"') == 1
    assert svg.count('clip-path="url(#c0)"') == 1  # rounded column
    assert "var(--deep)" in svg and "var(--awake)" in svg
    view = render("renderView", data)
    assert 'class="legend"' in view and "<table" in view
    assert "1h 30m" in view  # Deep average in the legend


@node
def test_headline_shows_average_or_the_selected_day() -> None:
    data = stacked_week()
    assert "8h 0m" in render_with("renderHeader", data, None)
    assert "Average" in render_with("renderHeader", data, None)
    picked = render_with("renderHeader", data, 0)
    assert "Sat, Mar 14" in picked and "8h 0m" in picked
    assert "No data this day" in render_with("renderHeader", data, 1)


@node
def test_number_formatting() -> None:
    def parts(value: Any, unit: str) -> str:
        code = (
            "var module = {exports: {}};"
            + SCRIPT
            + f"\nvar p = module.exports.parts({json.dumps(value)}, {json.dumps(unit)});"
            + "process.stdout.write(p.n + '|' + p.u);"
        )
        return subprocess.run(
            [str(NODE), "-e", code], capture_output=True, text=True, check=True
        ).stdout

    assert parts(8.3, "hours") == "8h 18m|"
    assert parts(45, "minutes") == "45m|"
    assert parts(9214, "steps") == "9,214|steps"
    assert parts(50.94, "bpm") == "50.9|bpm"
    assert parts(None, "bpm") == "—|"


@node
def test_line_breaks_at_gaps() -> None:
    values = [50, 51, None, 49, 48]
    data = chart(
        kind="line",
        unit="bpm",
        metric="resting_hr_bpm",
        labels=chart()["labels"][:5],
        series=[{"name": "HR", "role": "primary", "values": values}],
    )
    svg = render("renderChart", data)
    lines = re.findall(r'<path d="(M[^"]+)" fill="none"', svg)
    assert len(lines) == 2  # the line restarts after the missing day
    assert svg.count("<circle") == 4
    assert "var(--heart)" in svg  # heart-rate metrics use the heart colour


@node
def test_text_is_escaped() -> None:
    view = render("renderView", chart(title='<img src=x onerror="alert(1)">', notes=["a < b"]))
    assert "<img" not in view and "&lt;img" in view and "a &lt; b" in view


@node
def test_empty_chart_renders() -> None:
    data = chart(series=[{"name": "x", "role": "primary", "values": [None] * 7}], average=None)
    svg = render("renderChart", data)
    assert 'class="col"' not in svg and "NaN" not in svg
    assert "—" in render("renderView", data)


@node
def test_long_ranges_thin_out_labels() -> None:
    labels = [f"2026-0{1 + d // 28}-{1 + d % 28:02d}" for d in range(90)]
    data = chart(labels=labels, series=[{"name": "x", "role": "primary", "values": [5] * 90}])
    svg = render("renderChart", data)
    assert svg.count('text-anchor="middle"') <= 9


@node
def test_axis_uses_round_steps() -> None:
    svg = render(
        "renderChart",
        chart(
            metric="steps",
            unit="steps",
            series=[
                {
                    "name": "s",
                    "role": "primary",
                    "values": [8412, 12030, None, 6544, 10221, 14876, 3201],
                }
            ],
        ),
    )
    assert ">5k<" in svg and ">10k<" in svg and ">15k<" in svg
    hours = render("renderChart", stacked_week())
    assert ">0h<" in hours and ">3h<" in hours and ">9h<" in hours  # whole-hour steps


@node
def test_month_end_date_labels_do_not_overlap() -> None:
    labels = [f"2026-08-{day:02d}" for day in range(1, 32)]
    svg = render(
        "renderChart",
        chart(
            start=labels[0],
            end=labels[-1],
            labels=labels,
            series=[{"name": "Synthetic", "role": "primary", "values": [70] * 31}],
        ),
    )
    ticks = re.findall(
        r'<text class="axis" x="([\d.]+)"[^>]*text-anchor="middle">([^<]+)</text>', svg
    )
    assert ticks
    assert not (">Aug 29<" in svg and ">Aug 31<" in svg)
    for (left_x, left_text), (right_x, right_text) in pairwise(ticks):
        assert float(right_x) - float(left_x) >= (len(left_text) + len(right_text)) * 3 + 8


@node
def test_accessibility_table_is_clipped_inside_an_anchored_wrapper() -> None:
    view = render("renderView", chart())
    assert '<div class="sr"><table>' in view
    assert "</table></div>" in view
    assert '<table class="sr">' not in view  # table rows cannot reliably collapse to 1px
    assert view.count("<tr>") == len(chart()["labels"]) + 1
    styles = re.search(r"\.sr\s*\{([^}]+)\}", HTML).group(1)  # type: ignore[union-attr]
    assert "top: 0" in styles and "left: 0" in styles and "overflow: hidden" in styles
    assert "display: none" not in styles and "visibility: hidden" not in styles


@node
@pytest.mark.parametrize("native_bridge", [False, True])
def test_size_reports_wait_for_host_and_allow_shrinking(native_bridge: bool) -> None:
    harness = r"""
const vm = require("node:vm"), assert = require("node:assert/strict");
const sent = [], listeners = {}, intrinsic = [];
let height = 320, resized;
const parent = {postMessage: message => sent.push(message)};
const app = {getBoundingClientRect: () => ({height, width: 640}), innerHTML: ""};
const context = {
  window: {parent, addEventListener: (name, fn) => {listeners[name] = fn;}},
  document: {
    getElementById: () => app,
    documentElement: {setAttribute() {}, style: {setProperty() {}}}
  },
  ResizeObserver: class {constructor(fn) {resized = fn;} observe() {}},
  console
};
context.window.ResizeObserver = context.ResizeObserver;
if (NATIVE_BRIDGE) context.window.openai = {notifyIntrinsicHeight: h => intrinsic.push(h)};
vm.createContext(context);
vm.runInContext(SOURCE, context);
const sizes = () => sent.filter(m => m.method === "ui/notifications/size-changed");
async function check() {
  // A tool result or first resize can happen before the initialize reply.
  context.reportSize();
  resized();
  assert.equal(sizes().length, 0);
  const initialize = sent.find(m => m.method === "ui/initialize");
  listeners.message({source: parent, data: {id: initialize.id, result: {hostContext: {}}}});
  await Promise.resolve();
  assert.equal(sizes().length, 1);
  assert.equal(sizes()[0].params.height, 320);
  const readyIndex = sent.findIndex(m => m.method === "ui/notifications/initialized");
  assert.ok(readyIndex < sent.indexOf(sizes()[0]));
  resized();
  assert.equal(sizes().length, 1); // stable sizing does not create a resize loop
  height = 180;
  resized();
  assert.equal(sizes()[1].params.height, 180); // switching to shorter content shrinks
  listeners.message({source: parent, data: {
    method: "ui/notifications/host-context-changed", params: {theme: "dark"}
  }});
  assert.equal(sizes().length, 3); // resend even if host context leaves content height unchanged
  height = 50;
  listeners.message({source: parent, data: {method: "ui/notifications/tool-result", params: {}}});
  assert.equal(sizes()[3].params.height, 50); // no-data/error message also shrinks
  if (NATIVE_BRIDGE) {
    assert.deepEqual(intrinsic, [320, 180, 50]);
    listeners["openai:set_globals"]({});
    assert.deepEqual(intrinsic, [320, 180, 50]); // host echoes must not trigger a loop
    context.window.openai.notifyIntrinsicHeight = () => {throw new Error("unavailable")};
    height = 60;
    resized();
    // Standard resizing survives optional bridge failure.
    assert.equal(sizes()[4].params.height, 60);
  } else {
    context.window.openai = {notifyIntrinsicHeight: h => intrinsic.push(h)};
    listeners["openai:set_globals"]({});
    assert.deepEqual(intrinsic, [50]); // bridge can arrive after initialization
  }
}
check().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run(
        [
            str(NODE),
            "-e",
            "const SOURCE = "
            + json.dumps(SCRIPT)
            + "; const NATIVE_BRIDGE = "
            + json.dumps(native_bridge)
            + ";\n"
            + harness,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
