"""peppar-mon Textual app — scaffold.

Starts as Textual's canonical clock example: a centered ``Digits``
widget showing the current local time, updating once per second.

Once this runs end-to-end (terminal via ``python -m peppar_mon``, web
via ``textual serve peppar_mon.app:PepparMonApp``), it gets fleshed
out into the real status display by:

  1. Adding a log-tailer input thread that reads the engine's logs
  2. Parsing structured ``[TAG] key=value`` lines into a state store
  3. Adding widgets that render the store — SV state histogram, NL
     fix count, nav2Δ, host state machine, etc.

This file stays ``< 150 lines`` for as long as the scaffold lives so
the structure stays grokable while we're iterating on it.
"""

from __future__ import annotations

from datetime import datetime

from textual.app import App, ComposeResult
from textual.widgets import Digits, Header, Footer


class PepparMonApp(App):
    """Status display for PePPAR-Fix.

    Right now: a clock.  The point is to prove the Textual scaffold +
    the `textual serve` web path work end-to-end before we wire in
    engine log parsing.
    """

    CSS = """
    Screen {
        align: center middle;
    }

    Digits {
        width: auto;
        color: $accent;
    }
    """

    TITLE = "peppar-mon"
    SUB_TITLE = "scaffold — clock only"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Digits("")
        yield Footer()

    def on_ready(self) -> None:
        self._tick()
        # Run once per second.  Textual schedules set_interval callbacks
        # on its own event loop, so the UI thread stays responsive.
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        # System local time — datetime.now() returns naive local time by
        # default.  .astimezone() attaches the system's local tz so we
        # get the abbreviation (e.g. "CDT") for the footer display.
        now = datetime.now().astimezone()
        digits = self.query_one(Digits)
        digits.update(now.strftime("%H:%M:%S"))
        # Put the date + tz abbreviation in the header's sub-title so
        # it's visible without adding a second widget.
        self.sub_title = now.strftime("%Y-%m-%d %Z")


if __name__ == "__main__":
    PepparMonApp().run()
