import io
import os

import pytest

from garmin_connect_personal_mcp.progress import BAR_WIDTH, ProgressDisplay


class Terminal(io.StringIO):
    def __init__(self, encoding: str = "utf-8") -> None:
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._encoding

    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def fixed_width(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.get_terminal_size", lambda fallback: os.terminal_size((100, 24)))


def test_bar_fills_with_progress() -> None:
    display = ProgressDisplay(Terminal())
    assert display.render("Daily data", 0, 4, "sleep", False).startswith(
        "  Daily data  " + "─" * BAR_WIDTH + " 0/4  sleep"
    )
    half = display.render("Daily data", 2, 4, "", False)
    assert "━" * (BAR_WIDTH // 2) + "─" * (BAR_WIDTH // 2) + " 2/4" in half
    assert display.render("Daily data", 4, 4, "", True).startswith("✓ Daily data  " + "━" * 24)


def test_live_terminal_redraws_one_line_per_step() -> None:
    term = Terminal()
    display = ProgressDisplay(term)
    display("Daily data", 0, 2, "sleep")
    display("Daily data", 1, 2, "hrv")
    display("Daily data", 2, 2, "")
    display("Activities", 0, 0, "2026-01-01 to 2026-01-07")
    display.close()
    out = term.getvalue()
    assert out.count("\n") == 2  # one finished step, one closed step
    assert out.count("\r") == 4
    assert "✓ Daily data" in out
    assert "⠸" in out or "⠹" in out  # spinner frame for the unknown-total step


def test_lines_are_truncated_to_terminal_width() -> None:
    term = Terminal()
    ProgressDisplay(term)("Daily data", 0, 1, "x" * 500)
    assert all(len(line) <= 100 for line in term.getvalue().split("\r"))


def test_log_file_gets_plain_lines() -> None:
    log = io.StringIO()
    display = ProgressDisplay(log)
    display("FIT files", 0, 2, "activity 1")
    display("FIT files", 2, 2, "")
    display.close()
    lines = log.getvalue().splitlines()
    assert len(lines) == 2
    assert "\r" not in log.getvalue()
    assert lines[1].startswith("✓ FIT files") or lines[1].startswith("* FIT files")


def test_ascii_fallback_for_limited_consoles() -> None:
    term = Terminal(encoding="cp1252")
    display = ProgressDisplay(term)
    display("Daily data", 1, 2, "")
    display("Daily data", 2, 2, "")
    out = term.getvalue()
    assert "#" * BAR_WIDTH in out and "* Daily data" in out
    out.encode("cp1252")  # nothing the console can't show
