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
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Static, Header, Footer

from peppar_mon._util import format_uptime
from peppar_mon.log_reader import LogReader


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

    def __init__(self, log_path: Optional[Path] = None) -> None:
        super().__init__()
        self.log_path = log_path
        self._reader: Optional[LogReader] = None
        # Fallback uptime reference — the monitor's own start time.
        # Used until / unless the LogReader recovers the engine's
        # start time from the log file.
        self._own_start_mono = time.monotonic()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            yield Static("", id="clock")
            yield Static("", id="uptime")
        yield Footer()

    def on_ready(self) -> None:
        if self.log_path is not None:
            self._reader = LogReader(self.log_path)
            self._reader.start()
            self.sub_title = f"reading {self.log_path}"
        else:
            self.sub_title = "no --log — showing peppar-mon uptime only"
        self._tick()
        # Textual schedules set_interval callbacks on its own event
        # loop so the UI thread stays responsive.
        self.set_interval(1.0, self._tick)

    def on_unmount(self) -> None:
        # Best-effort teardown when the app exits.
        if self._reader is not None:
            self._reader.stop()

    def _tick(self) -> None:
        # datetime.now() is naive-local-time; .astimezone() attaches the
        # system's tz so strftime("%Z") yields the abbreviation (e.g. CDT).
        now_tz = datetime.now().astimezone()
        self.query_one("#clock", Static).update(now_tz.strftime("%H:%M:%S %Z"))
        self.query_one("#uptime", Static).update(self._uptime_line())

    def _uptime_line(self) -> str:
        """Render the uptime row, preferring engine uptime when known.

        Once the LogReader has recovered the engine's start time,
        uptime is ``datetime.now() − engine_start_time``.  Until then
        we fall back to the monitor's own process uptime — labelled so
        the reader isn't misled about the source.
        """
        engine_start = (
            self._reader.state.engine_start_time
            if self._reader is not None else None
        )
        if engine_start is not None:
            # Both are naive-local (see parse_log_timestamp) — subtract
            # directly; no tz arithmetic required.
            elapsed_s = (datetime.now() - engine_start).total_seconds()
            up = format_uptime(max(0.0, elapsed_s))
            return f"PePPAR-Fix UpTime  {up}"
        # Fallback: peppar-mon's own uptime.  Label shows "(monitor)" so
        # the user knows they're looking at a different clock until the
        # engine log is parsed.
        up = format_uptime(time.monotonic() - self._own_start_mono)
        return f"PePPAR-Fix UpTime  (monitor) {up}"


if __name__ == "__main__":
    PepparMonApp().run()
