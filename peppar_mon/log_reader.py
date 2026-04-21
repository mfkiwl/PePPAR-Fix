"""Two-phase engine-log consumer: replay then follow.

Why "reader" not "tailer": the monitor needs the engine's start time
(for uptime) and any state the engine has accumulated since startup.
That requires reading the log from the beginning before we can do
anything useful.  Once caught up, we continue following the file for
live updates — the same way ``tail -n +1 -f`` would.

The reader runs in its own daemon thread and exposes a thread-safe
``LogState`` snapshot.  Consumers (the Textual app) poll the state on
a timer — no callbacks, no event queue plumbing for the first pass.
When we add real state (SV-state histogram, NL fix counts, etc.) the
same pattern scales: the reader writes into the state dataclass,
readers read it.

Scope today: extract the engine's start time from the first
timestamped line.  Everything else is a hook for the next commit.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from peppar_mon._util import parse_log_timestamp

log = logging.getLogger(__name__)


# Poll interval for the follow phase.  Fast enough to feel live on a
# 1 Hz engine, slow enough that a quiet log doesn't spin a core.
_FOLLOW_POLL_S = 0.2

# Retry delay when the log file doesn't exist yet.  The engine might
# not have started or might be writing somewhere else; don't busy-loop.
_WAIT_FOR_FILE_S = 1.0


@dataclass
class LogState:
    """Thread-safe-ish snapshot of what the reader has inferred.

    The writing thread (LogReader) sets fields via simple attribute
    writes.  The reading thread (Textual app) reads them.  Python's
    GIL makes single-attribute reads and writes atomic, so no lock is
    needed for the scalars currently exposed here.  When fields like
    dicts or lists land, a lock goes in.
    """

    #: Parsed timestamp from the first timestamped line we saw.  None
    #: until we've observed one.  Naive-local, same convention as
    #: ``datetime.now()`` — see ``parse_log_timestamp``.
    engine_start_time: Optional[datetime] = None

    #: Line count processed (for debugging — will become a heartbeat
    #: once we have real parsing).
    lines_read: int = 0

    #: Last-line timestamp (useful to detect a stalled engine — if the
    #: log hasn't advanced in a while, the engine likely crashed).
    last_line_time: Optional[datetime] = None

    #: Current state of the AntPosEst state machine (lowercase string
    #: matching the enum values in scripts/peppar_fix/states.py:
    #: "unsurveyed", "verifying", "verified", "converging", "resolved",
    #: "moved").  None until the first [STATE] line is observed.
    ant_pos_est_state: Optional[str] = None

    #: Current state of the DOFreqEst state machine (lowercase string:
    #: "uninitialized", "phase_setting", "freq_verifying", "tracking",
    #: "holdover").  None until the first [STATE] line is observed.
    do_freq_est_state: Optional[str] = None

    #: States each machine has visited so far this run (ordered set,
    #: preserving first-visit order).  Used by StateBar widgets to
    #: render visited-vs-unvisited distinction.  Reassigned (rather
    #: than mutated in place) so readers see a consistent snapshot.
    ant_pos_est_visited: tuple[str, ...] = field(default_factory=tuple)
    do_freq_est_visited: tuple[str, ...] = field(default_factory=tuple)

    #: Per-SV current state (SvAmbState as string), keyed by SV
    #: identifier like ``G05``, ``E21``, ``C32``.  Populated from
    #: ``[SV_STATE] <sv>: <from> → <to>`` transition lines.  SVs
    #: that haven't produced a transition yet aren't present —
    #: engine logs one at admission so every observed SV lands here
    #: within a few epochs.  Immutable from readers' perspective:
    #: the reader thread replaces the dict on each update rather
    #: than mutating in place, so an app tick sees a consistent
    #: snapshot.
    sv_states: dict[str, str] = field(default_factory=dict)

    #: Constellation prefixes that have NL integer-fix capability
    #: given the receiver + correction streams currently connected.
    #: Populated from ``Phase bias lookup`` log lines: if any SV of
    #: constellation X has been seen with both f1 and f2 bias HITs,
    #: X is in this set.  Used by SvStateTable to render ``-`` in
    #: NL cells for constellations that *architecturally* can't
    #: reach NL (ptpmon F9T-L2 tracking L2W + CNES publishing L2L
    #: → GPS never NL-capable).  Latched on first HIT-HIT; never
    #: downgraded, because a single confirmed SV proves the bias
    #: pair exists in the stream.
    nl_capable_constellations: frozenset[str] = field(
        default_factory=frozenset)


class LogReader:
    """Threaded engine-log consumer.

    Usage::

        reader = LogReader(Path("/var/log/peppar-fix.log"))
        reader.start()
        ...
        reader.state.engine_start_time   # readable from any thread
        reader.stop()

    The thread is a daemon so process exit doesn't wait on it.  Errors
    inside the thread are logged at WARNING and don't propagate — a
    dead reader just stops updating state; the UI keeps working.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.state = LogState()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── Lifecycle ──────────────────────────────────────────────── #

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="peppar-mon-log-reader", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    # ── Reader thread body ─────────────────────────────────────── #

    def _run(self) -> None:
        try:
            self._wait_for_file()
            if self._stop.is_set():
                return
            with self.path.open("r", encoding="utf-8", errors="replace") as f:
                # Replay: consume everything currently in the file.
                self._consume(f, follow=False)
                if self._stop.is_set():
                    return
                # Follow: block-poll for new content.
                self._consume(f, follow=True)
        except Exception:
            # Any exception kills the reader thread but not the app.
            # Log it and return — the state snapshot freezes at whatever
            # we'd inferred before the failure.
            log.warning("LogReader crashed", exc_info=True)

    def _wait_for_file(self) -> None:
        """Block until the log file exists or stop is signalled.

        The engine might not be running yet.  We don't want to error
        out — just wait politely.
        """
        while not self._stop.is_set():
            if self.path.exists():
                return
            if self._stop.wait(timeout=_WAIT_FOR_FILE_S):
                return  # stop signalled

    def _consume(self, f, *, follow: bool) -> None:
        """Read lines from ``f``.

        ``follow=False`` reads to EOF and returns.  ``follow=True``
        keeps polling for new content, returning only when stop is set.
        """
        while not self._stop.is_set():
            line = f.readline()
            if not line:
                if not follow:
                    return
                # EOF during follow — sleep briefly and try again.  The
                # Event.wait call makes stop() responsive without
                # blocking the full poll interval.
                if self._stop.wait(timeout=_FOLLOW_POLL_S):
                    return
                continue
            self._ingest(line)

    # ── Per-line processing ────────────────────────────────────── #

    def _ingest(self, line: str) -> None:
        """Update state from one raw log line.

        Extracts:
          * timestamps (first = engine_start_time, latest = last_line_time)
          * [STATE] transitions for AntPosEst and DOFreqEst
        """
        self.state.lines_read += 1
        ts = parse_log_timestamp(line)
        if ts is not None:
            if self.state.engine_start_time is None:
                self.state.engine_start_time = ts
            self.state.last_line_time = ts
        self._parse_state_line(line)
        self._parse_sv_state_line(line)
        self._parse_phase_bias_lookup(line)

    def _parse_phase_bias_lookup(self, line: str) -> None:
        """Look for ``Phase bias lookup: <sv> f1=...(HIT|MISS) f2=...(HIT|MISS)``.

        Engine emits one per SV as it's first processed with SSR
        biases active.  Both HITs → constellation of this SV can
        reach NL (the IF ambiguity has a matched phase-bias pair).

        We latch the constellation as NL-capable on the first HIT-
        HIT and never downgrade.  A single confirmed SV proves the
        bias pair exists in the stream for that system — other SVs
        of the same constellation may fall in and out of individual
        HIT status (newly-arrived SVs, stale biases) but the
        capability is a property of the correction stream, not of
        any one SV.

        The complement is the useful signal here: if no SV of
        constellation X ever shows HIT-HIT, X stays out of the set
        and the widget renders ``-`` for NL cells — matches the
        ptpmon+CNES reality where GPS's L2W tracking never lines
        up with CNES's L2L phase-bias publication.
        """
        m = _PHASE_BIAS_LOOKUP_RE.search(line)
        if m is None:
            return
        sv = m.group("sv")
        f1_ok = m.group("f1_status") == "HIT"
        f2_ok = m.group("f2_status") == "HIT"
        if not (f1_ok and f2_ok):
            return
        prefix = sv[:1]
        if prefix in self.state.nl_capable_constellations:
            return  # already latched
        self.state.nl_capable_constellations = (
            self.state.nl_capable_constellations | frozenset({prefix})
        )

    def _parse_sv_state_line(self, line: str) -> None:
        """Extract per-SV state from ``[SV_STATE] <sv>: <from> → <to>``.

        Engine emits one per transition (the peppar_fix.sv_state
        tracker logs every legal edge).  We capture only the
        post-transition state; the history isn't needed for the
        table view.

        Updates ``self.state.sv_states`` by copy-on-write so readers
        always see a consistent snapshot.  Python dict copies are
        cheap for the 20–40 SVs we typically track.
        """
        m = _SV_STATE_LINE_RE.search(line)
        if m is None:
            return
        sv = m.group("sv")
        new_state = m.group("to")
        # copy-on-write to keep readers race-free.
        new_dict = dict(self.state.sv_states)
        new_dict[sv] = new_state
        self.state.sv_states = new_dict

    def _parse_state_line(self, line: str) -> None:
        """Look for a [STATE] transition and update the relevant field.

        Engine emits two variants (see scripts/peppar_fix/states.py):
          * initial:    ``[STATE] AntPosEst: → unsurveyed (initial)``
          * transition: ``[STATE] AntPosEst: unsurveyed → verifying after 12s``

        Both end with ``→ <new_state>`` followed by either EOL or
        ``after …``.  The same regex catches both.
        """
        m = _STATE_LINE_RE.search(line)
        if m is None:
            return
        machine = m.group("machine")
        new_state = m.group("to")
        if machine == "AntPosEst":
            self.state.ant_pos_est_state = new_state
            if new_state not in self.state.ant_pos_est_visited:
                self.state.ant_pos_est_visited = (
                    self.state.ant_pos_est_visited + (new_state,)
                )
        elif machine == "DOFreqEst":
            self.state.do_freq_est_state = new_state
            if new_state not in self.state.do_freq_est_visited:
                self.state.do_freq_est_visited = (
                    self.state.do_freq_est_visited + (new_state,)
                )


# Matches both ``[STATE] AntPosEst: → unsurveyed (initial)`` and
# ``[STATE] AntPosEst: converging → resolved after 393s (details)``.
# Anchoring on ``[STATE]`` avoids false positives from other log lines
# that happen to contain an arrow.  The ``from`` group is optional to
# handle the initial-state log line which has no from-state.
_STATE_LINE_RE = re.compile(
    r"\[STATE\] (?P<machine>\w+): "
    r"(?:(?P<from>[\w_]+) )?→ (?P<to>[\w_]+)\b"
)

# Matches ``[SV_STATE] G05: TRACKING → FLOAT (epoch=…, elev=…, reason=…)``.
# SV is the PRN identifier: one alpha (G/E/C/R/J/I), two or three
# digits.  States are the SvAmbState enum values, all uppercase with
# underscores.  The parenthesised details are not captured — the
# table only needs the current state.
_SV_STATE_LINE_RE = re.compile(
    r"\[SV_STATE\] (?P<sv>[A-Z]\d{2,3}): "
    r"(?P<from>[A-Z_]+) → (?P<to>[A-Z_]+)\b"
)

# Matches ``Phase bias lookup: G24 f1=GPS-L1CA→('C1C', 'L1C')(HIT) ``
# ``f2=GPS-L2CL→('C2L', 'L2L')(MISS) avail=[...]``.  We only need
# the SV identifier and the two HIT/MISS statuses — the details
# after ``avail=`` aren't used.  The signal-mapping itself contains
# a tuple in parens (``('C1C', 'L1C')``), so the regex between
# ``f1=`` and ``(HIT|MISS)`` uses non-greedy ``.*?`` to skip past
# the tuple and lock onto the status parens.  Engine's format is
# stable because it's part of the log contract
# (scripts/realtime_ppp.py).
_PHASE_BIAS_LOOKUP_RE = re.compile(
    r"Phase bias lookup: (?P<sv>[A-Z]\d{2,3})\s+"
    r"f1=.*?\((?P<f1_status>HIT|MISS)\)\s+"
    r"f2=.*?\((?P<f2_status>HIT|MISS)\)"
)
