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

import os
import time
from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static, Header, Footer

from peppar_mon._util import format_elapsed_short, format_uptime
from peppar_mon.log_reader import LogReader
from peppar_mon.widgets import (
    AntennaPositionLine, FilterStateLine, FleetStateLine,
    SecondOpinionLine, StateBar, SvStateTable,
)

# If no new timestamped line has landed in the log within this many
# seconds, the engine is assumed dead and the uptime row flips to a
# DOWN indicator.  30 s is generous — the engine writes [EPH] every
# few seconds and AntPosEst every 10 s even during a quiet period,
# so a 30 s silence is well outside normal operation.  Not so tight
# that a brief disk-flush hiccup trips it.
_STALE_LOG_THRESHOLD_S = 30.0

# State enums mirrored from scripts/peppar_fix/states.py.  Kept as
# plain tuples to preserve enum declaration order — that's the order
# the widget renders them in, left-to-right, and it should match the
# state-machine progression so users can see where the engine *is* and
# where it's *going* in one glance.  Keep in sync if the engine side
# grows a new state.
_ANT_POS_EST_STATES = (
    "surveying", "verifying",
    "converging", "anchoring", "anchored", "moved",
)
_DO_FREQ_EST_STATES = (
    "uninitialized", "phase_setting", "freq_verifying",
    "tracking", "holdover",
)

# Environment variable the --web launcher sets.  `textual serve` imports
# `peppar_mon.app:PepparMonApp` and instantiates it with no arguments,
# so there's no way to thread a log_path argument through the textual
# CLI.  We read it from the environment instead.  Direct TUI callers
# (via __main__) pass log_path explicitly through argparse and don't
# need to touch the env var.
_LOG_PATH_ENV = "PEPPAR_MON_LOG"


class PepparMonApp(App):
    """Status display for PePPAR-Fix — scaffold version."""

    CSS = """
    Screen {
        align: center top;
    }

    #status {
        width: 1fr;
        padding: 1 2;
    }

    /* Top two rows: left item auto-widths, right item stretches to
       fill and right-aligns its content so the position/delta read-
       outs hug the right edge without stepping on the clock/uptime
       to their left. */
    .top-row {
        height: 1;
        width: 1fr;
    }
    .top-row-left {
        width: auto;
        padding: 0 1;
    }
    .top-row-right {
        width: 1fr;
        content-align: right middle;
        padding: 0 1;
    }

    #clock {
        color: $accent;
        text-style: bold;
    }

    StateBar {
        padding: 0 1;
    }
    """

    TITLE = "peppar-mon"
    SUB_TITLE = "scaffold"

    def __init__(
        self,
        log_path: Path | None = None,
        *,
        fleet_mode: bool = False,
        fleet_host: str | None = None,
        fleet_antenna_ref: str = "",
    ) -> None:
        super().__init__()
        if log_path is None:
            env = os.environ.get(_LOG_PATH_ENV)
            if not env:
                raise RuntimeError(
                    f"log_path not provided and {_LOG_PATH_ENV} is unset. "
                    f"Direct invocation: python -m peppar_mon LOG_FILE.  "
                    f"Web invocation: scripts/peppar-mon --web PORT LOG_FILE "
                    f"(the launcher sets {_LOG_PATH_ENV} for you)."
                )
            log_path = env
        self.log_path = Path(log_path)
        self._reader: LogReader = LogReader(self.log_path)
        self._fleet_mode = fleet_mode
        self._fleet_host = fleet_host
        self._fleet_antenna_ref = fleet_antenna_ref
        self._bus = None
        self._aggregator = None
        self._bridge = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="status"):
            # Row 1: clock (left) + antenna-position (right).
            with Horizontal(classes="top-row"):
                yield Static("", id="clock", classes="top-row-left")
                yield AntennaPositionLine(
                    id="antenna-position", classes="top-row-right",
                )
            # Row 2: uptime / DOWN (left) + 2nd Opinion (right).
            with Horizontal(classes="top-row"):
                yield Static("", id="uptime", classes="top-row-left")
                yield SecondOpinionLine(
                    id="second-opinion", classes="top-row-right",
                )
            # Row 3: ZTD + correction-stream mounts (right-aligned so
            # it hugs the same edge as the position readouts above).
            with Horizontal(classes="top-row"):
                yield Static("", classes="top-row-left")
                yield FilterStateLine(
                    id="filter-state", classes="top-row-right",
                )
            # Row 4 (fleet mode only): cross-host summary.
            if self._fleet_mode:
                with Horizontal(classes="top-row"):
                    yield Static("", classes="top-row-left")
                    yield FleetStateLine(
                        id="fleet-state", classes="top-row-right",
                    )
            yield StateBar(
                machine_name="AntPosEst",
                all_states=_ANT_POS_EST_STATES,
                id="ant-pos-est",
            )
            yield StateBar(
                machine_name="DOFreqEst",
                all_states=_DO_FREQ_EST_STATES,
                id="do-freq-est",
            )
            yield SvStateTable(id="sv-state-table")
        yield Footer()

    def on_ready(self) -> None:
        self.sub_title = f"reading {self.log_path}"
        self._reader.start()
        if self._fleet_mode:
            self._start_fleet()
        self._tick()
        # Textual schedules set_interval callbacks on its own event
        # loop so the UI thread stays responsive.
        self.set_interval(1.0, self._tick)

    def on_unmount(self) -> None:
        # Best-effort teardown when the app exits.
        if self._bridge is not None:
            self._bridge.stop()
        if self._bus is not None:
            self._bus.close()
        self._reader.stop()

    def _start_fleet(self) -> None:
        """Boot the peer-bus + bridge + aggregator.  Called once on
        startup when --fleet was passed."""
        import socket as _s
        from peppar_bus import PeerIdentity, UDPMulticastBus
        from peppar_mon.bridge import LogToBusBridge
        from peppar_mon.fleet import FleetAggregator

        host = self._fleet_host or _s.gethostname()
        identity = PeerIdentity(
            host=host,
            version="peppar-mon",
            antenna_ref=self._fleet_antenna_ref,
        )
        self._bus = UDPMulticastBus(host=host, identity=identity)
        self._aggregator = FleetAggregator(self._bus)
        self._bridge = LogToBusBridge(
            reader=self._reader, bus=self._bus, host=host,
        )
        self._bridge.start()

    def _tick(self) -> None:
        # datetime.now() is naive-local-time; .astimezone() attaches the
        # system's tz so strftime("%Z") yields the abbreviation (e.g. CDT).
        now_tz = datetime.now().astimezone()
        self.query_one("#clock", Static).update(now_tz.strftime("%H:%M:%S %Z"))
        self.query_one("#uptime", Static).update(self._uptime_line())
        # State bars.  Read the snapshot once so the two bars see the
        # same LogState instant even if the reader thread updates mid-tick.
        s = self._reader.state
        self.query_one("#ant-pos-est", StateBar).update_state(
            current=s.ant_pos_est_state,
            visited=s.ant_pos_est_visited,
        )
        self.query_one("#do-freq-est", StateBar).update_state(
            current=s.do_freq_est_state,
            visited=s.do_freq_est_visited,
        )
        self.query_one("#sv-state-table", SvStateTable).update(
            sv_states=s.sv_states,
            nl_capable=s.nl_capable_constellations,
        )
        self.query_one("#antenna-position", AntennaPositionLine).update_position(
            state=s.ant_pos_est_state,
            position=s.antenna_position,
            sigma_m=s.antenna_sigma_m,
            worst_sigma_m=s.worst_sigma_m,
            reached_anchored=s.reached_anchored,
        )
        self.query_one("#second-opinion", SecondOpinionLine).update_delta(
            s.nav2_delta_m,
        )
        self.query_one("#filter-state", FilterStateLine).update_state(
            ztd_m=s.ztd_m,
            ztd_sigma_mm=s.ztd_sigma_mm,
            earth_tide_mm=s.earth_tide_mm,
            earth_tide_u_mm=s.earth_tide_u_mm,
            ssr_mount=s.ssr_mount,
            eph_mount=s.eph_mount,
        )
        if self._aggregator is not None:
            from peppar_mon.fleet import compute_summary
            summary = compute_summary(self._aggregator.snapshots())
            self.query_one("#fleet-state", FleetStateLine).update_summary(summary)

    def _uptime_line(self) -> str:
        """Delegates to the module-level pure function so it can
        be unit-tested without spinning up a Textual app."""
        return build_uptime_line(
            state=self._reader.state,
            now=datetime.now(),
            stale_threshold_s=_STALE_LOG_THRESHOLD_S,
        )


def build_uptime_line(
    *,
    state,
    now: datetime,
    stale_threshold_s: float,
) -> str:
    """Render the uptime / death-indicator row from a LogState.

    Three display cases:
      * No log data yet (engine hasn't started writing, or file
        still empty) — ``(waiting for first log line)``.
      * Log is fresh (last timestamp within ``stale_threshold_s``
        of ``now``) — engine uptime in ``Dd Hh Mm`` format.
      * Log is stale (last write past threshold) — red DOWN
        indicator with elapsed-since-last-activity.  Operator
        expects a dead engine to be visibly distinguished, not
        silently accumulate uptime based on a frozen start.

    Returns a string with Rich markup so Static.update()
    renders the DOWN case in red+bold without needing a separate
    Text renderable.  Pure function for unit-testability: no
    ``datetime.now()`` calls inside, caller injects the clock.
    """
    engine_start = state.engine_start_time
    if engine_start is None:
        return "PePPAR-Fix UpTime  (waiting for first log line)"
    last_line = state.last_line_time
    if last_line is not None:
        stale_s = (now - last_line).total_seconds()
        if stale_s > stale_threshold_s:
            return (
                f"[bold red]DOWN[/]  "
                f"no log activity for "
                f"{format_elapsed_short(stale_s)}"
            )
    elapsed_s = (now - engine_start).total_seconds()
    up = format_uptime(max(0.0, elapsed_s))
    return f"PePPAR-Fix UpTime  {up}"


if __name__ == "__main__":
    PepparMonApp().run()
