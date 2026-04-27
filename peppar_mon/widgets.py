"""Widgets for the peppar-mon display.

Started as a home for the horizontal state-bar widgets: one cell per
possible state of a state machine, current state highlighted, visited
states marked so the operator can see the trajectory at a glance.

The two machines we care about right now are AntPosEst (antenna-
position filter state: SURVEYING → VERIFYING → CONVERGING → ANCHORING
→ ANCHORED → MOVED) and DOFreqEst (DO frequency estimator:
UNINITIALIZED → PHASE_SETTING → FREQ_VERIFYING → TRACKING → HOLDOVER).
The values come from ``scripts/peppar_fix/states.py`` — keep the
enum lists here in sync if the engine side changes.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Mapping, Optional

from textual.widget import Widget
from rich.table import Table
from rich.text import Text

from peppar_mon._util import (
    format_elapsed_short,
    format_uncertainty,
    uncertain_decimals_deg,
    uncertain_decimals_m,
)

# AntPosEstState (engine side, lowercase string) → display label in
# the AntennaPositionLine row.  The raw label below is combined with
# the ``reached_anchored`` latch in ``_antenna_label()`` to emit the
# RECONVERGING / REANCHORING derived labels per the lifecycle-
# vocabulary rename memo's derived-label table.
#
# Raw state → label (no latch consulted):
#   SURVEYING / VERIFYING → "Surveying"  (pre-PPP / Phase 1)
#   CONVERGING            → "Converging"
#   ANCHORING             → "Anchoring"  (integer fixes landing)
#   ANCHORED              → "Anchored"   (≥ 4 geometry-validated)
#   MOVED                 → "Moved"      (position discontinuity)
#
# Anything else (e.g. None before first [STATE] line) falls back to
# "Waiting" so the row is never blank.
_ANTPOS_LABEL: dict[Optional[str], str] = {
    "surveying":  "Surveying",
    "verifying":  "Surveying",
    "converging": "Converging",
    "anchoring":  "Anchoring",
    "anchored":   "Anchored",
    "moved":      "Moved",
}

# Derived labels once the filter has ever been ANCHORED and then
# regressed: operator can distinguish "first anchoring attempt"
# (ANCHORING, low trust) from "regained anchor quorum after a
# slip storm" (REANCHORING, high trust — ZTD / clock / position
# state carried forward).  Composed at render time from
# ``raw_state × reached_anchored``.
_ANTPOS_LABEL_RECONV = "Reconverging"    # CONVERGING + reached_anchored=True
_ANTPOS_LABEL_REANCH = "Reanchoring"     # ANCHORING  + reached_anchored=True

# SV constellation prefix → human label, in display order.
# Keep GPS first (most satellites), then GAL, then BDS.  Add more
# rows here when we start running QZSS / NAVIC / SBAS and care
# about those too.
_CONSTELLATION_ROWS: tuple[tuple[str, str], ...] = (
    ("G", "GPS"),
    ("E", "GAL"),
    ("C", "BDS"),
)

# Column definitions: (header, per-SV position-res estimate,
# aggregated states, needs_nl_capability).  Column ORDER is
# operationally meaningful — left-to-right roughly tracks SV
# trust, starting from "observed but not contributing" on the
# left and ending with the anchored fix set on the right:
#
#   Tracked    — receiver sees SV (SvAmbState.TRACKING), not yet
#                admitted to the float PPP filter
#   Waiting    — cooldown-bound after a slip / false fix
#                (SvAmbState.WAITING).  Placed to the LEFT of
#                Floating (updated 2026-04-23) so the two
#                "not currently contributing" columns
#                (Tracked + Waiting) sit together; a returning
#                SV moves right through Floating → Converging →
#                Anchoring → Anchored.
#   Floating   — admitted, MW accumulating, no integer fix yet
#                (SvAmbState.FLOATING)
#   Converging — wide-lane integer fixed, narrow-lane pending
#                (SvAmbState.CONVERGING — was "WL" until
#                2026-04-23, renamed to match the state name)
#   Anchoring  — NL integer landed, earning Δaz validation
#                (SvAmbState.ANCHORING).  Short-term member of the
#                fix set.
#   Anchored   — NL integer has survived ≥ 8° Δaz
#                (SvAmbState.ANCHORED).  Long-term member.
#
# The resolution line below each header gives an approximate
# per-SV position-contribution σ at that stage.  Order-of-
# magnitude estimates, based on measurement noise + ambiguity
# state:
#
#   Tracked / Waiting  — SV not in fix set, no contribution (— )
#   Floating           — float IF ambiguity, PR-limited per-SV
#                        (σ_PR≈3 m before convergence, ~0.5 m
#                        after tens of epochs of MW averaging)
#   Converging         — WL integer fixed; NL float dominates.
#                        λ_NL ≈ 10.7 cm so per-SV contribution
#                        settles near ~15 cm once MW WL is stable
#   Anchoring          — NL integer just fixed but unvalidated
#                        over Δaz.  Per-SV phase residual at
#                        SIGMA_PHI_IF scale (~3 cm) with residual
#                        wrong-integer risk bounded by bootstrap
#                        success rate
#   Anchored           — NL validated over ≥ 8° Δaz.  Per-SV
#                        residual limited by phase noise +
#                        leftover obs-model gaps (~5 mm on clean
#                        geometry, ~1 cm with partial obs-model)
#
# These are displayed as a second header line.  Exact values are
# approximations — the real per-SV contribution depends on
# elevation, multipath, and how much of the obs-model (tides,
# PCVs, wind-up) has been applied.  The point is for the operator
# to see the scale progression, not to compute a precise number.
#
# Third field flags columns whose values should render as ``-``
# (architecturally impossible) when the constellation lacks NL
# capability — when the receiver's tracked signals don't line up
# with the correction stream's published phase biases for NL
# integer fixing.  Only Anchoring and Anchored need the capability
# check; the rest are reachable on any dual-freq GNSS.
_COLUMNS: tuple[tuple[str, str, frozenset[str], bool], ...] = (
    ("Tracked",    "—",      frozenset({"TRACKING"}),   False),
    ("Waiting",    "—",      frozenset({"WAITING"}),    False),
    ("Floating",   "~0.5 m", frozenset({"FLOATING"}),   False),
    ("Converging", "~15 cm", frozenset({"CONVERGING"}), False),
    ("Anchoring",  "~3 cm",  frozenset({"ANCHORING"}),  True),
    ("Anchored",   "~5 mm",  frozenset({"ANCHORED"}),   True),
)

# Rendered in cells that are architecturally impossible given the
# receiver+corrections combination.  Contrast with ``0``, which
# means "currently none but could rise."  The distinction matters
# on ptpmon where the GPS NL columns stay ``-`` the whole run
# because CNES's L2L phase biases don't match the F9T's L2W
# tracking — that failure mode is fundamentally different from
# "no NL fixes have landed yet but they could."
_NOT_POSSIBLE = "-"


class StateBar(Widget):
    """Horizontal state-machine indicator.

    Renders each possible state as a labeled cell in a row.  The
    current state is highlighted (bold + accent background); states
    the machine has visited earlier this run are marked as visited
    (dim); states not yet reached are rendered unmarked.

    One line tall.  Meant to sit in a Vertical container alongside
    other status rows — not a full panel.

    Usage::

        bar = StateBar(
            machine_name="AntPosEst",
            all_states=["surveying", "verifying", ...],
        )
        ...
        bar.update_state(
            current="converging",
            visited=("surveying", "verifying", "converging"),
        )

    Why Widget + render() instead of Static: Textual's Static is just
    a Widget with a string/markup renderable.  We want row layout of
    styled cells with both symbol and highlight states, which is
    cleaner to express as a single rich Text per frame than as a
    container of child widgets.  If we ever want each cell to be
    interactive, a container split is easy.
    """

    # One line tall — don't let the layout stretch it.
    DEFAULT_CSS = """
    StateBar {
        height: 1;
        width: 1fr;
    }
    """

    def __init__(
        self,
        *,
        machine_name: str,
        all_states: Iterable[str],
        current: Optional[str] = None,
        visited: Iterable[str] = (),
        id: Optional[str] = None,  # noqa: A002 — Textual's ctor kwarg
    ) -> None:
        super().__init__(id=id)
        self._machine_name = machine_name
        self._all_states = tuple(all_states)
        self._current = current
        self._visited = tuple(visited)

    def update_state(
        self,
        *,
        current: Optional[str],
        visited: Iterable[str] = (),
    ) -> None:
        """Record a new (current, visited) snapshot and refresh.

        No-op when nothing changed — avoids a Rich repaint on every
        app-tick when the machine is sitting in the same state.
        """
        new_visited = tuple(visited)
        if current == self._current and new_visited == self._visited:
            return
        self._current = current
        self._visited = new_visited
        self.refresh()

    # ── Rendering ───────────────────────────────────────────────── #

    def render(self) -> Text:  # noqa: D401 — Rich-style, not imperative
        """Build a one-line Text with the machine name + state cells.

        Layout: ``AntPosEst:  surveying  verifying  [converging]  ...``

        * The machine name gets a trailing colon and a fixed-width pad
          so the two bars line up vertically when stacked.
        * The active state is wrapped in brackets AND bold+accent for
          both monochrome and colour terminals; brackets alone read
          well on a non-color log view.
        * Visited-but-not-current states get a dim style.
        * Never-visited states render plain.
        """
        t = Text()
        t.append(f"{self._machine_name:>10}:  ", style="bold")
        for i, state in enumerate(self._all_states):
            if i > 0:
                t.append("  ")
            if state == self._current:
                # Brackets + bold accent — visible on both color and
                # monochrome terminals.
                t.append(f"[{state}]", style="bold reverse")
            elif state in self._visited:
                t.append(state, style="dim")
            else:
                t.append(state)
        return t


class SvStateTable(Widget):
    """Per-constellation × per-state count table.

    Reads the LogReader's ``sv_states`` dict (sv → state name) and
    renders a small grid:

        ::

                    Tracked  Floating  WL  Waiting  Anchoring  Anchored  WAITING
            GPS         5     3         1        0          1
            GAL         6     4         2        1          0
            BDS         3     2         0        0          0

    Columns aggregate across ``SvAmbState`` values — see the
    ``_COLUMNS`` module constant for the exact grouping.  "Tracked"
    pools TRACKING+FLOATING because at-a-glance the distinction between
    "receiver sees this SV" and "admitted but no integer" is less
    interesting than "integer-fixed yet?".  The remaining columns
    each correspond to exactly one state.

    Rows are fixed at GPS/GAL/BDS (see ``_CONSTELLATION_ROWS``).  A
    constellation row renders all zeros if the receiver hasn't seen
    any SVs from that system yet — predictable layout beats
    dynamic-row rearrangement on a live display.

    Why Widget+render() over DataTable: small fixed grid, no
    interactivity needed, and a Rich ``Table`` is simpler to update
    by replacement than cell-by-cell updates against a DataTable
    instance.  If we ever want per-cell clicking (e.g. to filter the
    log view to one SV), a DataTable swap is easy.
    """

    DEFAULT_CSS = """
    SvStateTable {
        height: auto;
        width: auto;
    }
    """

    def __init__(
        self,
        *,
        sv_states: Optional[Mapping[str, str]] = None,
        nl_capable: Optional[Iterable[str]] = None,
        id: Optional[str] = None,  # noqa: A002 — Textual ctor kwarg
    ) -> None:
        super().__init__(id=id)
        self._sv_states: Mapping[str, str] = dict(sv_states or {})
        self._nl_capable: frozenset[str] = frozenset(nl_capable or ())

    def update(
        self,
        *,
        sv_states: Mapping[str, str],
        nl_capable: Iterable[str] = (),
    ) -> None:
        """Record a new snapshot and refresh if anything visible changed.

        Change detection compares:
          * aggregated cell counts (what the widget actually shows)
          * nl_capable set (flips NL columns between "-" and counts)
        Updates that don't move either are silently dropped to avoid
        repaints on busy logs where per-SV churn doesn't move the
        displayed totals.
        """
        new_counts = _aggregate(sv_states)
        old_counts = _aggregate(self._sv_states)
        new_cap = frozenset(nl_capable)
        if new_counts == old_counts and new_cap == self._nl_capable:
            # Still replace the stored dict so future diffs compare
            # against the latest snapshot — but skip the refresh().
            self._sv_states = dict(sv_states)
            return
        self._sv_states = dict(sv_states)
        self._nl_capable = new_cap
        self.refresh()

    # Historical single-arg update.  Kept as a thin wrapper so
    # tests and callers that only care about sv_states don't break
    # when the NL-capability feature lands.
    def update_sv_states(
        self, sv_states: Mapping[str, str],
    ) -> None:
        self.update(sv_states=sv_states, nl_capable=self._nl_capable)

    # ── Rendering ───────────────────────────────────────────────── #

    def render(self) -> Table:  # noqa: D401 — Rich-style
        """Render the fixed-shape table with current counts.

        Cell rules:
          * Constellation never observed (no SV of that prefix has
            ever been in sv_states) → every cell in the row
            renders ``-``.  Protects the operator from reading a
            legitimate "0" when the constellation is actually
            disabled (engine's ``systems=`` arg) or simply not
            visible on this antenna.
          * Column requires NL capability and the constellation
            isn't in ``nl_capable`` → cell renders ``-``.  That's
            the architectural "can't get here" signal.
          * Otherwise → integer count (including plain ``0`` when
            the constellation is observed and capable but no SVs
            are currently in the column's state set).
        """
        counts = _aggregate(self._sv_states)
        observed = frozenset(counts.keys())
        table = Table(
            box=None,
            padding=(0, 1),
            show_header=True,
            show_edge=False,
        )
        table.add_column("", style="bold")
        # Two-line headers: column name on top, approximate per-SV
        # position-contribution σ underneath.  Rich renders the \n
        # as a soft break inside the header cell, preserving
        # right-justification on both lines.  The sub-header is
        # styled ``dim`` so it reads as annotation rather than data.
        for col_name, col_res, _members, _needs_nl in _COLUMNS:
            header = Text()
            header.append(col_name)
            header.append(f"\n{col_res}", style="dim")
            table.add_column(header, justify="right")
        for prefix, label in _CONSTELLATION_ROWS:
            row_counts = counts.get(prefix, {})
            cells = [label]
            constellation_observed = prefix in observed
            nl_capable = prefix in self._nl_capable
            for _col_name, _col_res, members, needs_nl in _COLUMNS:
                if not constellation_observed:
                    cells.append(_NOT_POSSIBLE)
                elif needs_nl and not nl_capable:
                    cells.append(_NOT_POSSIBLE)
                else:
                    n = sum(row_counts.get(m, 0) for m in members)
                    cells.append(str(n))
            table.add_row(*cells)
        return table


def _aggregate(
    sv_states: Mapping[str, str],
) -> dict[str, dict[str, int]]:
    """Aggregate SV→state mapping to constellation→state→count.

    Unknown-prefix SVs are ignored (e.g. R-prefix GLONASS isn't a
    row today).  Output keys are the first-character prefix; values
    are Counter-like dicts state_name → count.  Pure function, no
    side effects — easy to exercise in tests.
    """
    out: dict[str, Counter] = {}
    for sv, state in sv_states.items():
        if not sv:
            continue
        prefix = sv[0]
        out.setdefault(prefix, Counter())[state] += 1
    return {p: dict(c) for p, c in out.items()}


class AntennaPositionLine(Widget):
    """Single-line right-aligned antenna-position display.

    Layout::

        Antenna Position Anchoring LAT / LON / ALT ± 2.3 cm

    Four pieces separated by spaces:

      * ``Antenna Position`` — literal prefix
      * state label: Surveying / Converging / Anchoring /
        Anchored / Reconverging / Reanchoring / Moved / Waiting.
        See ``_ANTPOS_LABEL`` for the raw map and ``_label()``
        for the ``reached_anchored``-driven derived labels
        (Reconverging / Reanchoring).
      * ``lat / lon / alt`` — filter's current position.  Format
        matches whatever precision the engine emits in the
        ``[AntPosEst]`` line.  Digits below the σ quantum are
        rendered ``dim`` so the operator sees at a glance which
        digits are truth-bearing.
      * ``± X unit`` — 3D σ scaled to cm / m per
        ``format_uncertainty``.

    Design notes:
      * Single renderable.  No per-field subcomponents — this is
        a one-line status read-out, not an interactive form.
      * Right-alignment is the caller's job via CSS — the widget
        itself just produces a ``Text``.  Saves a layout argument
        here and matches how Rich-rendered widgets usually work
        in Textual.
      * If position is None (pre-bootstrap), renders ``Antenna
        Position Waiting`` with no numbers.  Rich-Text-safe.
    """

    DEFAULT_CSS = """
    AntennaPositionLine {
        height: 1;
        width: auto;
    }
    """

    def __init__(
        self,
        *,
        state: Optional[str] = None,
        position: Optional[tuple[float, float, float]] = None,
        sigma_m: Optional[float] = None,
        worst_sigma_m: Optional[float] = None,
        reached_anchored: bool = False,
        id: Optional[str] = None,  # noqa: A002
        classes: Optional[str] = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._state = state
        self._position = position
        self._sigma_m = sigma_m
        self._worst_sigma_m = worst_sigma_m
        self._reached_anchored = reached_anchored

    def update_position(
        self,
        *,
        state: Optional[str],
        position: Optional[tuple[float, float, float]],
        sigma_m: Optional[float],
        worst_sigma_m: Optional[float] = None,
        reached_anchored: bool = False,
    ) -> None:
        """Record a new snapshot; no-op if nothing display-relevant
        changed (saves repaints when the engine line arrives with
        the same state/σ the widget already shows — typical in
        steady state where position moves in sub-cm increments)."""
        if (
            state == self._state
            and position == self._position
            and sigma_m == self._sigma_m
            and worst_sigma_m == self._worst_sigma_m
            and reached_anchored == self._reached_anchored
        ):
            return
        self._state = state
        self._position = position
        self._sigma_m = sigma_m
        self._worst_sigma_m = worst_sigma_m
        self._reached_anchored = reached_anchored
        self.refresh()

    # ── Rendering ───────────────────────────────────────────────── #

    def _label(self) -> str:
        """Compose the displayed state label.  Consults the
        ``reached_anchored`` latch to distinguish first-time
        CONVERGING / ANCHORING (low trust) from regressed
        CONVERGING / ANCHORING after the filter has earned
        ANCHORED at least once (high trust — RECONVERGING /
        REANCHORING).  Table matches the memo's derived-label
        spec."""
        if self._reached_anchored:
            if self._state == "converging":
                return _ANTPOS_LABEL_RECONV
            if self._state == "anchoring":
                return _ANTPOS_LABEL_REANCH
        return _ANTPOS_LABEL.get(self._state, "Waiting")

    def render(self) -> Text:
        label = self._label()
        t = Text()
        t.append("Antenna Position ", style="bold")
        t.append(label + " ")
        if self._position is None:
            # No numeric block yet — state label alone.
            return t
        lat, lon, alt = self._position
        # Format to the widest precision the engine ever emits
        # (8 dp lat/lon ≈ 1.1 mm at the equator, 3 dp alt ≈ 1 mm).
        # Trailing-digit shading from uncertainty handles the
        # visual when the engine logs fewer digits — the regex
        # captures the value, we re-stringify to fixed width, and
        # uncertain_decimals_deg/m dim the digits below σ quantum.
        # A trailing ``°`` labels the angular units on lat/lon so
        # they aren't confused with signed-decimal altitude.
        lat_s = f"{lat:.8f}"
        lon_s = f"{lon:.8f}"
        alt_s = f"{alt:.3f}"
        self._append_shaded_deg(t, lat_s)
        t.append("°")
        t.append(" / ")
        self._append_shaded_deg(t, lon_s)
        t.append("°")
        t.append(" / ")
        self._append_shaded_m(t, alt_s)
        t.append(" m")
        t.append("  positionσ ")
        t.append(format_uncertainty(self._sigma_m).lstrip("± "))
        if self._worst_sigma_m is not None:
            t.append("  worstσ ")
            t.append(format_uncertainty(self._worst_sigma_m).lstrip("± "))
        return t

    def _append_shaded_deg(self, t: Text, s: str) -> None:
        """Append a degree-formatted number with digits below the
        σ quantum rendered ``dim``.  Point is preserved; integer
        part never shaded."""
        confident = uncertain_decimals_deg(self._sigma_m)
        self._append_with_decimal_shading(t, s, confident)

    def _append_shaded_m(self, t: Text, s: str) -> None:
        """Same for altitude (metres)."""
        confident = uncertain_decimals_m(self._sigma_m)
        self._append_with_decimal_shading(t, s, confident)

    @staticmethod
    def _append_with_decimal_shading(
        t: Text, s: str, confident_decimals: int,
    ) -> None:
        """Given a string like ``40.12345678`` and a count of
        confident decimal places, append it to ``t`` with the
        trailing (uncertain) decimals styled ``dim``.  Handles
        edge cases: no decimal point (integer), confident ≥ all
        decimals (no shading), confident = 0 (shade everything
        after the point)."""
        if "." not in s:
            t.append(s)
            return
        whole, frac = s.split(".", 1)
        t.append(whole + ".")
        if confident_decimals >= len(frac):
            t.append(frac)
            return
        cut = max(0, confident_decimals)
        t.append(frac[:cut])
        t.append(frac[cut:], style="dim")


class SecondOpinionLine(Widget):
    """Single-line right-aligned ``2nd Opinion X.X m 3D`` readout.

    Shows the scalar distance between the AntPosEst filter's
    position and the F9T's NAV2 secondary-engine position.  Bob's
    preference (2026-04-21): just show the delta magnitude, not
    the absolute NAV2 position — the delta is the useful signal.

    Renders ``2nd Opinion  —`` when no nav2Δ has been observed
    (engine without nav2_store, or pre-first-NAV2-fix bootstrap).
    """

    DEFAULT_CSS = """
    SecondOpinionLine {
        height: 1;
        width: auto;
    }
    """

    def __init__(
        self,
        *,
        nav2_delta_m: Optional[float] = None,
        id: Optional[str] = None,  # noqa: A002
        classes: Optional[str] = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._delta = nav2_delta_m

    def update_delta(self, nav2_delta_m: Optional[float]) -> None:
        if nav2_delta_m == self._delta:
            return
        self._delta = nav2_delta_m
        self.refresh()

    def render(self) -> Text:
        t = Text()
        t.append("2nd Opinion ", style="bold")
        if self._delta is None:
            t.append("—")
        else:
            # Δ annotates the numeric offset as a delta (matches
            # the log field ``nav2Δ`` and Bob's 2026-04-23 ask).
            # Layout: ``NN.N m Δ 3D``.
            t.append(f"{self._delta:.1f} m Δ 3D")
        return t


class FilterStateLine(Widget):
    """Single-line ``ZTD`` + Earth-tide + correction-stream indicator.

    Layout::

        ZTD -2.85 m ±293 mm   Earth tide 135 mm (U+131)   SSR SSRA00CNE0   EPH BCEP00BKG0

    Four pieces:

      * ``ZTD`` — current residual ZTD above Saastamoinen a priori.
        Displayed in m (signed), with ±σ in mm if the engine
        published it.  Renders ``—`` when no AntPosEst line with
        ZTD has arrived yet.
      * ``Earth tide`` — current IERS 2010 solid Earth tide
        magnitude in mm, with the vertical (U) component in
        parentheses as an orientation cue.  The U component
        dominates the total at most latitudes and epochs (peak
        ±300 mm).  Omitted when no tide has been logged yet —
        keeps the line short on engine builds without the tide
        correction applied.
      * ``SSR`` — NTRIP mount name for the SSR corrections stream
        (orbit/clock/bias).  ``—`` when not connected.
      * ``EPH`` — NTRIP mount name for the broadcast-ephemeris
        stream.  ``—`` when not connected.

    The correction-stream names are static-ish during a run
    (engine connects once at startup); they're shown here so a
    shared-lab monitor can distinguish sessions running on
    CNES vs WHU vs BCEP without having to grep the log.
    """

    DEFAULT_CSS = """
    FilterStateLine {
        height: 1;
        width: auto;
    }
    """

    def __init__(
        self,
        *,
        ztd_m: Optional[float] = None,
        ztd_sigma_mm: Optional[int] = None,
        earth_tide_mm: Optional[int] = None,
        earth_tide_u_mm: Optional[int] = None,
        ssr_mount: Optional[str] = None,
        eph_mount: Optional[str] = None,
        id: Optional[str] = None,  # noqa: A002
        classes: Optional[str] = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._ztd_m = ztd_m
        self._ztd_sigma_mm = ztd_sigma_mm
        self._earth_tide_mm = earth_tide_mm
        self._earth_tide_u_mm = earth_tide_u_mm
        self._ssr_mount = ssr_mount
        self._eph_mount = eph_mount

    def update_state(
        self,
        *,
        ztd_m: Optional[float],
        ztd_sigma_mm: Optional[int],
        earth_tide_mm: Optional[int] = None,
        earth_tide_u_mm: Optional[int] = None,
        ssr_mount: Optional[str],
        eph_mount: Optional[str],
    ) -> None:
        if (
            ztd_m == self._ztd_m
            and ztd_sigma_mm == self._ztd_sigma_mm
            and earth_tide_mm == self._earth_tide_mm
            and earth_tide_u_mm == self._earth_tide_u_mm
            and ssr_mount == self._ssr_mount
            and eph_mount == self._eph_mount
        ):
            return
        self._ztd_m = ztd_m
        self._ztd_sigma_mm = ztd_sigma_mm
        self._earth_tide_mm = earth_tide_mm
        self._earth_tide_u_mm = earth_tide_u_mm
        self._ssr_mount = ssr_mount
        self._eph_mount = eph_mount
        self.refresh()

    def render(self) -> Text:
        t = Text()
        t.append("ZTD ", style="bold")
        if self._ztd_m is None:
            t.append("—")
        else:
            t.append(f"{self._ztd_m:+.3f} m")
            if self._ztd_sigma_mm is not None:
                t.append(f" ±{self._ztd_sigma_mm} mm")
        if self._earth_tide_mm is not None:
            t.append("   Earth tide ", style="bold")
            t.append(f"{self._earth_tide_mm} mm")
            if self._earth_tide_u_mm is not None:
                t.append(f" (U{self._earth_tide_u_mm:+d})")
        t.append("   SSR ", style="bold")
        t.append(self._ssr_mount if self._ssr_mount else "—")
        t.append("   EPH ", style="bold")
        t.append(self._eph_mount if self._eph_mount else "—")
        return t


class CohortLine(Widget):
    """Single-line cohort-consensus indicator + last integrity trip.

    Layout when engine is participating in a cohort::

        Cohort pos=3 Δh=2mm Δ3d=4mm   ztd=4 Δztd=+12.3mm

    With a recent FixSetIntegrityMonitor trip appended::

        Cohort pos=3 Δh=2mm Δ3d=4mm   last trip: pos_consensus 4m 32s ago

    Two kinds of information on one row — they share the "how does
    this host compare to the cohort?" question, and both come from
    the fleet consensus Part 1+2 plumbing:

      * ``pos / ztd`` — this host's distance from the shared-ARP
        (pos) and shared-atmosphere (ztd) cohort medians as reported
        by the engine's ``[COHORT]`` log line.  Source: Bravo's Part
        1 (``7a7ad12``).  Updates every ~10 epochs.
      * ``last trip`` — most recent ``[FIX_SET_INTEGRITY] TRIPPED``
        reason + elapsed.  Fires at most once per trip (edge-
        triggered) so the elapsed-since-trip is the live signal:
        a trip 30 s ago is an active concern, a trip 6 h ago is
        historical.  Source: all integrity-monitor reasons
        (``pos_consensus``, ``ztd_consensus``, ``anchor_collapse``,
        ``window_rms``, ``ztd_impossible``, ``ztd_cycling``).
        Rendered in red so it draws the eye even when the cohort
        row is otherwise fine.

    When no cohort is available (no peer-bus, single-host run,
    cohort < 2) the line shows ``Cohort —``.  When no trip has
    ever been observed this session, the trip segment is absent.
    """

    DEFAULT_CSS = """
    CohortLine {
        height: 1;
        width: auto;
    }
    """

    def __init__(
        self,
        *,
        cohort_pos_n: Optional[int] = None,
        cohort_delta_h_mm: Optional[int] = None,
        cohort_delta_3d_mm: Optional[int] = None,
        cohort_ztd_n: Optional[int] = None,
        cohort_delta_ztd_mm: Optional[float] = None,
        last_trip: Optional[object] = None,
        elapsed_since_trip_s: Optional[float] = None,
        id: Optional[str] = None,  # noqa: A002
        classes: Optional[str] = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._pos_n = cohort_pos_n
        self._dh_mm = cohort_delta_h_mm
        self._d3_mm = cohort_delta_3d_mm
        self._ztd_n = cohort_ztd_n
        self._dztd_mm = cohort_delta_ztd_mm
        self._last_trip = last_trip
        self._elapsed_s = elapsed_since_trip_s

    def update_state(
        self,
        *,
        cohort_pos_n: Optional[int],
        cohort_delta_h_mm: Optional[int],
        cohort_delta_3d_mm: Optional[int],
        cohort_ztd_n: Optional[int],
        cohort_delta_ztd_mm: Optional[float],
        last_trip: Optional[object],
        elapsed_since_trip_s: Optional[float],
    ) -> None:
        if (
            cohort_pos_n == self._pos_n
            and cohort_delta_h_mm == self._dh_mm
            and cohort_delta_3d_mm == self._d3_mm
            and cohort_ztd_n == self._ztd_n
            and cohort_delta_ztd_mm == self._dztd_mm
            and last_trip == self._last_trip
            and elapsed_since_trip_s == self._elapsed_s
        ):
            return
        self._pos_n = cohort_pos_n
        self._dh_mm = cohort_delta_h_mm
        self._d3_mm = cohort_delta_3d_mm
        self._ztd_n = cohort_ztd_n
        self._dztd_mm = cohort_delta_ztd_mm
        self._last_trip = last_trip
        self._elapsed_s = elapsed_since_trip_s
        self.refresh()

    def render(self) -> Text:
        return build_cohort_line(
            cohort_pos_n=self._pos_n,
            cohort_delta_h_mm=self._dh_mm,
            cohort_delta_3d_mm=self._d3_mm,
            cohort_ztd_n=self._ztd_n,
            cohort_delta_ztd_mm=self._dztd_mm,
            last_trip=self._last_trip,
            elapsed_since_trip_s=self._elapsed_s,
        )


def build_cohort_line(
    *,
    cohort_pos_n: Optional[int],
    cohort_delta_h_mm: Optional[int],
    cohort_delta_3d_mm: Optional[int],
    cohort_ztd_n: Optional[int],
    cohort_delta_ztd_mm: Optional[float],
    last_trip: Optional[object],
    elapsed_since_trip_s: Optional[float],
) -> Text:
    """Pure renderer for ``CohortLine``.

    Split out so unit tests exercise the label logic without
    instantiating a Textual widget.  ``last_trip`` is a
    ``FixSetIntegrityTrip`` from ``peppar_mon.log_reader``
    but declared as ``object`` here to keep the widgets module
    free of a back-import on a dataclass defined in log_reader.
    """
    t = Text()
    t.append("Cohort ", style="bold")
    has_pos = cohort_pos_n is not None
    has_ztd = cohort_ztd_n is not None
    if not has_pos and not has_ztd:
        t.append("—")
    if has_pos:
        t.append(f"pos={cohort_pos_n} ")
        t.append(f"Δh={cohort_delta_h_mm}mm ")
        t.append(f"Δ3d={cohort_delta_3d_mm}mm")
    if has_ztd:
        if has_pos:
            t.append("   ")
        t.append(f"ztd={cohort_ztd_n} ")
        # Keep the signed 1-decimal format from the engine side so
        # the display reads the same as the log line it came from.
        t.append(f"Δztd={cohort_delta_ztd_mm:+.1f}mm")
    if last_trip is not None and elapsed_since_trip_s is not None:
        if has_pos or has_ztd:
            t.append("   ")
        t.append("last trip: ", style="bold red")
        reason = getattr(last_trip, "reason", "?")
        t.append(reason, style="red")
        t.append(f" {format_elapsed_short(elapsed_since_trip_s)} ago",
                 style="red")
    return t


class FleetStateLine(Widget):
    """One-line cross-host summary of the peer fleet.

    Layout when ≥ 2 peers share the same antenna::

        Fleet (3 hosts):  Δ 3 mm / 5 mm   |   Anchored 13/13 13/13 12/13   |   ZTD σ 2 mm

    - ``Δ horiz / 3D``: max pairwise position delta across the
      same-antenna cohort, horizontal then 3D.  Shared-antenna
      baseline is 0, so anything beyond mm points at wrong-
      integer or null-mode.
    - ``Anchored``: per-host Anchored-count list — one number per
      host, sorted by hostname for stability.
    - ``ZTD σ``: range (max - min) of ZTD residual in mm.  Same-
      atmosphere peers should match within a few mm.

    When fewer than 2 peers are present, renders ``Fleet: (waiting
    for peers)``.
    """

    DEFAULT_CSS = """
    FleetStateLine {
        height: 1;
        width: auto;
    }
    """

    def __init__(
        self,
        *,
        summary=None,
        id: Optional[str] = None,  # noqa: A002
        classes: Optional[str] = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._summary = summary

    def update_summary(self, summary) -> None:
        if summary == self._summary:
            return
        self._summary = summary
        self.refresh()

    def render(self) -> Text:
        t = Text()
        s = self._summary
        if s is None or s.n_hosts < 2:
            t.append("Fleet: ", style="bold")
            t.append("(waiting for peers)")
            return t
        t.append(f"Fleet ({s.n_hosts} hosts): ", style="bold")
        if s.max_delta_h_m is not None and s.max_delta_3d_m is not None:
            t.append("Δ ")
            t.append(_format_mm_cm(s.max_delta_h_m * 1000.0))
            t.append(" / ")
            t.append(_format_mm_cm(s.max_delta_3d_m * 1000.0))
        else:
            t.append("Δ —")
        if s.anchored_per_host:
            t.append("   Anchored ")
            t.append(" ".join(str(c) for _, c in s.anchored_per_host))
        if s.ztd_spread_mm is not None:
            t.append("   ZTD σ ")
            t.append(f"{s.ztd_spread_mm:.1f} mm")
        return t


def _format_mm_cm(mm: float) -> str:
    """Render a distance in the natural unit: mm below 100 mm,
    cm below 100 cm, m otherwise.  Mirrors format_uncertainty's
    scale choice but without the ± prefix."""
    if abs(mm) < 100:
        return f"{mm:.0f} mm"
    if abs(mm) < 1000:
        return f"{mm / 10:.0f} cm"
    return f"{mm / 1000:.2f} m"


# Decision thresholds from Geng et al. 2010 + Charlie's literature
# memo, also documented in the engine emission's parenthetical:
# < 0.99  = diagnose Q_â per-SV; AR push not advised (red)
# ≥ 0.99  = partial AR (PAR) green-light (yellow)
# ≥ 0.999 = full AR green-light (green)
_AR_READINESS_PAR = 0.99
_AR_READINESS_FULL = 0.999


class ArReadinessLine(Widget):
    """Single-line WL Integer Bootstrap success rate readout.

    Layout when the engine has emitted [WL_AR_READINESS]::

        WL P_IB: 0.9876 (n=5) ✓PAR-ready

    Color-coded against the Geng et al. 2010 thresholds:

      * P_IB ≥ 0.999 → green (full WL AR green-light)
      * P_IB ≥ 0.99  → yellow (partial AR / PAR-ready)
      * P_IB < 0.99  → red (diagnose Q_â per-SV before AR push)

    Before the engine emits the first [WL_AR_READINESS] line —
    older engine builds without commit 6e9cca6, or the first
    ~10 epochs of a new run — the line shows
    ``WL P_IB: (waiting)``.

    Stage 1 of B1 (per dayplan I-142015-main): WL only.  Stage 2
    will add NL P_IB once A1 (NL P_IB emission) ships.
    """

    DEFAULT_CSS = """
    ArReadinessLine {
        height: 1;
        width: auto;
    }
    """

    def __init__(
        self,
        *,
        wl_p_ib: Optional[float] = None,
        wl_p_ib_n: Optional[int] = None,
        id: Optional[str] = None,  # noqa: A002
        classes: Optional[str] = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._p = wl_p_ib
        self._n = wl_p_ib_n

    def update_state(
        self,
        *,
        wl_p_ib: Optional[float],
        wl_p_ib_n: Optional[int],
    ) -> None:
        if wl_p_ib == self._p and wl_p_ib_n == self._n:
            return
        self._p = wl_p_ib
        self._n = wl_p_ib_n
        self.refresh()

    def render(self) -> Text:
        return build_ar_readiness_line(
            wl_p_ib=self._p,
            wl_p_ib_n=self._n,
        )


def build_ar_readiness_line(
    *,
    wl_p_ib: Optional[float],
    wl_p_ib_n: Optional[int],
) -> Text:
    """Pure renderer for ``ArReadinessLine``.

    Split out so unit tests exercise the threshold + color logic
    without instantiating a Textual widget.  Returns a ``rich.Text``
    with style markup for the threshold tag.

    Threshold tag is whichever of three labels applies:
    ``✓full-AR`` (green), ``✓PAR-ready`` (yellow), or
    ``diagnose`` (red).  The check / no-check distinction marks
    whether the float is *ready for an AR push* — ``diagnose``
    has no check because it explicitly is not.
    """
    t = Text()
    t.append("WL P_IB: ", style="bold")
    if wl_p_ib is None:
        t.append("(waiting)")
        return t
    # Four decimals matches the engine's ``%.4f`` emission format,
    # so the monitor reads the same number the log line shows.
    t.append(f"{wl_p_ib:.4f}")
    if wl_p_ib_n is not None:
        t.append(f" (n={wl_p_ib_n})")
    t.append(" ")
    if wl_p_ib >= _AR_READINESS_FULL:
        t.append("✓full-AR", style="bold green")
    elif wl_p_ib >= _AR_READINESS_PAR:
        t.append("✓PAR-ready", style="bold yellow")
    else:
        t.append("diagnose", style="bold red")
    return t
