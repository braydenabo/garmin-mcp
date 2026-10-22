"""A dependency-free progress display for the CLI.

In a terminal, each step is one line redrawn in place: a bar when the total is
known, a spinner when it isn't, and a check mark when the step finishes. When
output goes to a file or a scheduler's log, it prints plain lines instead.
Falls back to ASCII on consoles that can't show the Unicode symbols.
"""

import shutil
from typing import TextIO

BAR_WIDTH = 24
FANCY = {"full": "━", "empty": "─", "done": "✓", "spin": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"}
PLAIN = {"full": "#", "empty": "-", "done": "*", "spin": "|/-\\"}


def _can_encode(stream: TextIO, text: str) -> bool:
    try:
        text.encode(getattr(stream, "encoding", None) or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


class ProgressDisplay:
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.live = stream.isatty()
        self.symbols = FANCY if _can_encode(stream, "".join(FANCY.values())) else PLAIN
        self.step: str | None = None
        self.ticks = 0

    def __call__(self, step: str, done: int, total: int, detail: str) -> None:
        if step != self.step:
            self._end_line()
            self.step = step
        finished = total > 0 and done >= total
        line = self.render(step, done, total, detail, finished)
        if self.live:
            width = shutil.get_terminal_size((80, 24)).columns - 1
            self.stream.write("\r" + line[:width].ljust(width))
            if finished:
                self.stream.write("\n")
                self.step = None
        else:
            self.stream.write(line + "\n")
        self.stream.flush()
        self.ticks += 1

    def render(self, step: str, done: int, total: int, detail: str, finished: bool) -> str:
        s = self.symbols
        mark = s["done"] if finished else " "
        if total > 0:
            filled = round(BAR_WIDTH * min(done, total) / total)
            bar = s["full"] * filled + s["empty"] * (BAR_WIDTH - filled)
            body = f"{bar} {done}/{total}"
        else:
            body = s["spin"][self.ticks % len(s["spin"])]
        return f"{mark} {step:<11} {body}  {detail}".rstrip()

    def close(self) -> None:
        self._end_line()

    def _end_line(self) -> None:
        if self.live and self.step is not None:
            self.stream.write("\n")
            self.stream.flush()
        self.step = None
