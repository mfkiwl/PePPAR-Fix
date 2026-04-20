"""peppar-mon Textual app — scaffold.

Two lines of text, updated once per second:

  - local time HH:MM:SS with the system's tz suffix (e.g. "21:57:15 CDT")
  - "PePPAR-Fix UpTime  Dd Hh Mm"

Plain ``Static`` widgets, no segmented digits.  Once the log reader
lands, the uptime source switches from the monitor's own start time
to the engine's (recovered from the first [STATE] line the reader
replays); the label stays the same so the display doesn't visibly
flip.

Fleshed out in subsequent commits:

  1. LogReader — two-phase: replays the log from the start to
     reconstruct state (including the engine's start time for the
     uptime line), then follows the file for live updates.  Named
     "reader" not "tailer" because it does both.
  2. Parser for [TAG] key=value lines into a state store.
  3. Widgets for SV-state histogram, NL fix count, nav2Δ, host state.
"""

from __future__ import annotations

import time
from datetime import datetime

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Static, Header, Footer

from peppar_mon._util import format_uptime


class PepparMonApp(App):
    """Status display for PePPAR-Fix — scaffold version."""

    CSS = """
    Screen {
        align: center middle;
    }

    #clock, #uptime {
        width: auto;
        padding: 0 1;
    }

    #clock {
        color: $accent;
        text-style: bold;
    }
    """

    TITLE = "peppar-mon"
    SUB_TITLE = "scaffold"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            yield Static("", id="clock")
            yield Static("", id="uptime")
        yield Footer()

    def on_ready(self) -> None:
        # Monotonic reference captured at startup.  When the LogReader
        # lands, `_start_mono` is replaced with (or shadowed by) the
        # engine's reported start time — the label text doesn't change.
        self._start_mono = time.monotonic()
        self._tick()
        # Textual schedules set_interval callbacks on its own event
        # loop so the UI thread stays responsive.
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        # datetime.now() is naive-local-time; .astimezone() attaches the
        # system's tz so strftime("%Z") yields the abbreviation (e.g. CDT).
        now = datetime.now().astimezone()
        self.query_one("#clock", Static).update(now.strftime("%H:%M:%S %Z"))
        up = format_uptime(time.monotonic() - self._start_mono)
        self.query_one("#uptime", Static).update(f"PePPAR-Fix UpTime  {up}")


if __name__ == "__main__":
    PepparMonApp().run()
