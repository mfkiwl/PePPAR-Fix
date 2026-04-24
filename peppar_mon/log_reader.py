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
    #: "surveying", "verifying", "converging", "anchoring", "anchored",
    #: "moved").  None until the first [STATE] line is observed.
    ant_pos_est_state: Optional[str] = None

    #: AntPosEst latches — have the filter's milestone states been
    #: reached at least once this run?  Clear only on integrity-
    #: monitor trip.  Populated from
    #: ``[STATE] AntPosEst: reached_anchoring + reached_anchored
    #: cleared (fix_set_integrity_trip)`` log lines (clear on trip)
    #: and implicitly True on first entry to ANCHORING / ANCHORED
    #: observed in a ``[STATE] AntPosEst: ... → anchoring / anchored``
    #: transition.  Drives the RECONVERGING / REANCHORING derived
    #: labels in AntennaPositionLine.
    reached_anchoring: bool = False
    reached_anchored: bool = False

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

    #: Latest AntPosEst filter position, extracted from the most
    #: recent ``[AntPosEst N] σ=X pos=(lat, lon, alt) ...`` log
    #: line.  None before the first such line lands.  Tuple is
    #: (lat_deg, lon_deg, alt_m).  Precision matches whatever the
    #: engine logged — today 6 decimals on lat/lon (~11 cm) and
    #: 1 decimal on altitude (~10 cm).  See
    #: ``project_to_main_position_log_precision_20260421.md`` for
    #: the engine-side precision ask.
    antenna_position: Optional[tuple[float, float, float]] = None

    #: Latest AntPosEst 3D σ (m) from the same log line.  Drives
    #: per-digit uncertainty shading in the AntennaPositionLine
    #: widget — digits below the σ quantum render dim.  None
    #: before first observation.
    antenna_sigma_m: Optional[float] = None

    #: Latest nav2Δ (m) — scalar 3D distance from AntPosEst pos to
    #: the F9T's secondary-engine position.  Engine only emits the
    #: delta, not the absolute NAV2 position, so that's what we
    #: display in the "2nd Opinion" row.  None until first
    #: observation or when the engine runs without a nav2_store.
    nav2_delta_m: Optional[float] = None

    #: Latest worstσ (m) — the largest per-SV position-sensitivity
    #: sigma produced by the engine's null-mode eigenvalue monitor
    #: (engine commit e5637d9).  Effectively the filter's
    #: smallest-eigenvalue reciprocal expressed as a per-SV σ; huge
    #: values (~10³ m) flag a rank-deficient mode, small values
    #: (<1 m) flag well-observed geometry.  Displayed alongside
    #: positionσ because they answer complementary "how
    #: trustworthy is the fix?" questions.
    worst_sigma_m: Optional[float] = None

    #: Latest ZTD residual (metres, signed).  Log emits as
    #: ``ZTD=<mm>±<sigma_mm>mm`` in millimetres; we store the
    #: central value in m to match the other sigma conventions in
    #: this struct.  None before the first AntPosEst line that
    #: carries ZTD (i.e., engine versions past the
    #: solid-tide/ZTD-log port).
    ztd_m: Optional[float] = None

    #: Latest ZTD uncertainty (mm, positive).  Optional — older
    #: engine versions emitted bare ``ZTD=<mm>mm`` without the
    #: ± field.
    ztd_sigma_mm: Optional[int] = None

    #: Total solid Earth tide magnitude (mm, positive) at the
    #: current epoch.  Parsed from the engine's ``tide=<mm>mm(U<±N>)``
    #: field on the [AntPosEst] line.  None before the first line
    #: that carries the field (older engine builds or --no-solid-tide
    #: runs).  See `docs/obs-model-completion-plan.md`.
    earth_tide_mm: Optional[int] = None

    #: Vertical (Up) component of the solid Earth tide (mm, signed).
    #: Dominates the total magnitude for most stations at most
    #: epochs (peak ±30 cm), so displayed alongside the total as an
    #: orientation cue.
    earth_tide_u_mm: Optional[int] = None

    #: NTRIP mount identifier for the broadcast-ephemeris stream
    #: the engine is connected to, e.g. ``"BCEP00BKG0"``.  Parsed
    #: once from the startup ``Ephemeris stream: HOST:PORT/MOUNT``
    #: line.  None if the engine is running without NTRIP (offline
    #: mode with `--nav` file).
    eph_mount: Optional[str] = None

    #: NTRIP mount identifier for the SSR-corrections stream, e.g.
    #: ``"SSRA00CNE0"``.  None if SSR isn't connected (engine
    #: running in broadcast-only NAV mode).
    ssr_mount: Optional[str] = None

    #: True after we've observed the engine announcing its own
    #: peer-bus publishing.  Latches once set (never flips back to
    #: False within a run) — if the engine was publishing at any
    #: point, peppar-mon retires its LogToBusBridge and lets the
    #: engine be the sole publisher.  Detection via the engine's
    #: ``peer-bus active:`` startup log line (see
    #: ``scripts/peer_publisher.py::initialize``).  Absent in logs
    #: produced by engines without ``--peer-bus``.
    engine_peer_bus_active: bool = False


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
        self._parse_antposest_line(line)
        self._parse_stream_lines(line)
        self._parse_peer_bus_active(line)

    def _parse_antposest_line(self, line: str) -> None:
        """Extract position + σ + nav2Δ from ``[AntPosEst N] ...``.

        The engine emits one of these every ~10 epochs with format:

            [AntPosEst 4200] σ=0.023m pos=(LAT, LON, ALT) ...
                             ... nav2Δ=2.8m ZTD=+274±3mm

        We extract three pieces:
          * σ (3D, metres) — drives uncertainty shading on the
            antenna-position display
          * (lat, lon, alt) — the filter's current position
          * nav2Δ — scalar delta to the F9T's secondary-engine
            position; rendered as the "2nd Opinion" row

        nav2Δ is optional (may not appear on early-bootstrap lines
        before a NAV2 fix arrives, or when no nav2_store is
        configured).  Position and σ are always present on
        ``[AntPosEst ...]`` lines, so missing them silently would
        mask a log-format change — treat as a noisy pattern
        mismatch and just skip the line.
        """
        m = _ANTPOSEST_LINE_RE.search(line)
        if m is None:
            return
        self.state.antenna_sigma_m = float(m.group("sigma"))
        self.state.antenna_position = (
            float(m.group("lat")),
            float(m.group("lon")),
            float(m.group("alt")),
        )
        nav2 = m.group("nav2d")
        if nav2 is not None:
            self.state.nav2_delta_m = float(nav2)
        worst = m.group("worst")
        if worst is not None:
            self.state.worst_sigma_m = float(worst)
        ztd_mm = m.group("ztd_mm")
        if ztd_mm is not None:
            self.state.ztd_m = int(ztd_mm) * 1e-3
            ztd_sigma = m.group("ztd_sigma_mm")
            if ztd_sigma is not None:
                self.state.ztd_sigma_mm = int(ztd_sigma)
        tide_mm = m.group("tide_mm")
        if tide_mm is not None:
            self.state.earth_tide_mm = int(tide_mm)
            self.state.earth_tide_u_mm = int(m.group("tide_u_mm"))

    def _parse_stream_lines(self, line: str) -> None:
        """Capture the NTRIP correction-stream identifiers.

        Engine logs one ``Ephemeris stream:`` and one ``SSR stream:``
        line at startup listing ``HOST:PORT/MOUNT``.  We store the
        mount names for display — the host+port is noise for the
        operator glancing at the monitor.
        """
        m = _EPH_STREAM_RE.search(line)
        if m is not None:
            self.state.eph_mount = m.group("mount")
            return
        m = _SSR_STREAM_RE.search(line)
        if m is not None:
            self.state.ssr_mount = m.group("mount")

    def _parse_peer_bus_active(self, line: str) -> None:
        """Latch ``engine_peer_bus_active`` when the engine's
        ``peer-bus active:`` startup line arrives.

        One-way latch: set once on first match, never cleared
        within a run.  If the engine crashes and restarts without
        ``--peer-bus``, we keep the bridge retired — acceptable
        because that's an unusual case and the staleness heuristic
        in the bus (5 s heartbeat timeout) already handles the
        'peer quietly left' story elsewhere.
        """
        if self.state.engine_peer_bus_active:
            return
        if _PEER_BUS_ACTIVE_RE.search(line) is not None:
            self.state.engine_peer_bus_active = True

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
        """Look for a [STATE] transition and update the relevant
        field, plus AntPosEst latch lines.

        Engine emits three relevant variants (see
        scripts/peppar_fix/states.py):
          * initial:    ``[STATE] AntPosEst: → surveying (initial)``
          * transition: ``[STATE] AntPosEst: surveying → verifying after 12s``
          * latch-clr:  ``[STATE] AntPosEst: reached_anchoring + reached_anchored cleared (fix_set_integrity_trip)``

        Transitions end with ``→ <new_state>`` — the regex catches
        both initial and transition variants.  Latch-clear lines
        are matched separately via substring check since their
        structure doesn't fit the arrow regex.
        """
        # Latch-clear: substring match — the engine emits this once
        # per FixSetIntegrityMonitor trip and covers whichever of
        # the two latches were set at the time.  Both fields drop
        # to False whenever we see this line; subsequent
        # transitions re-latch as appropriate.
        if "[STATE] AntPosEst:" in line and "cleared" in line:
            if "reached_anchored" in line:
                self.state.reached_anchored = False
            if "reached_anchoring" in line:
                self.state.reached_anchoring = False
            return

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
            # Latches follow the engine side (states.py transition):
            # first entry to ANCHORING sets reached_anchoring; first
            # entry to ANCHORED sets both.  We never un-latch on a
            # transition — that only happens on the explicit clear
            # line handled above.
            if new_state == "anchoring":
                self.state.reached_anchoring = True
            elif new_state == "anchored":
                self.state.reached_anchoring = True
                self.state.reached_anchored = True
        elif machine == "DOFreqEst":
            self.state.do_freq_est_state = new_state
            if new_state not in self.state.do_freq_est_visited:
                self.state.do_freq_est_visited = (
                    self.state.do_freq_est_visited + (new_state,)
                )


# Matches both ``[STATE] AntPosEst: → surveying (initial)`` and
# ``[STATE] AntPosEst: converging → anchoring after 393s (details)``.
# Anchoring on ``[STATE]`` avoids false positives from other log lines
# that happen to contain an arrow.  The ``from`` group is optional to
# handle the initial-state log line which has no from-state.
_STATE_LINE_RE = re.compile(
    r"\[STATE\] (?P<machine>\w+): "
    r"(?:(?P<from>[\w_]+) )?→ (?P<to>[\w_]+)\b"
)

# Matches ``[SV_STATE] G05: TRACKING → FLOATING (epoch=…, elev=…, reason=…)``.
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

# Matches ``[AntPosEst 4200] positionσ=0.023m pos=(LAT, LON, ALT) ...``.
# positionσ, lat, lon, alt are always present; nav2Δ, ZTD, and
# worstσ are optional — nav2Δ is absent on pre-NAV2 bootstrap
# lines or runs without nav2_store; ZTD + worstσ came in with
# engine ports f7da44e / e5637d9 and appear alongside obs-model
# corrections.  Altitude is signed (bootstrap-glitch negatives
# seen) and can have any number of decimals.  ZTD is signed in
# mm with optional ±sigma (e.g. ``ZTD=-2850±293mm``); worstσ is
# the filter's null-mode smallest-eigenvalue proxy in metres
# (huge = uncostrained; small = well-observed).
#
# Field name change history: the σ field was renamed to
# ``positionσ`` by engine commit f17fc05 to disambiguate from
# the new ``worstσ`` null-mode metric on the same line.  We
# only match the new name; older pre-rename logs won't parse
# (acceptable — peppar-mon's support window is the current
# engine).
_ANTPOSEST_LINE_RE = re.compile(
    r"\[AntPosEst \d+\]\s+"
    r"positionσ=(?P<sigma>[\d.]+)m\s+"
    r"pos=\((?P<lat>-?[\d.]+),\s*(?P<lon>-?[\d.]+),\s*(?P<alt>-?[\d.]+)\)"
    r"(?:.*?nav2Δ=(?P<nav2d>[\d.]+)m)?"
    r"(?:.*?ZTD=(?P<ztd_mm>[-+]?\d+)(?:±(?P<ztd_sigma_mm>\d+))?mm)?"
    r"(?:.*?tide=(?P<tide_mm>\d+)mm\(U(?P<tide_u_mm>[-+]?\d+)\))?"
    r"(?:.*?worstσ=(?P<worst>[\d.]+)m)?"
)

# Startup lines identifying the correction streams the engine
# connected to.  Emitted once at engine boot, replayed by the
# log reader.
_EPH_STREAM_RE = re.compile(
    r"Ephemeris stream:\s*(?P<host>[\w.-]+):(?P<port>\d+)/(?P<mount>[\w_]+)"
)
_SSR_STREAM_RE = re.compile(
    r"SSR stream:\s*(?P<host>[\w.-]+):(?P<port>\d+)/(?P<mount>[\w_]+)"
)

# Engine's ``peer-bus active: ...`` startup line, emitted once by
# ``scripts/peer_publisher.py::initialize`` when the engine has
# actually opened a peer bus (via ``--peer-bus``).  Peppar-mon's
# fleet mode uses this to decide whether to keep its own
# LogToBusBridge running or retire it in favour of the engine's
# native publishing.
_PEER_BUS_ACTIVE_RE = re.compile(r"peer-bus active:")
