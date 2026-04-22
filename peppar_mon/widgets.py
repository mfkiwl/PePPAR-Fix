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

# Column definitions: (header, aggregated states, needs_nl_capability).
# Column ORDER is operationally meaningful — mirrors the usual SV
# trajectory per ``scripts/peppar_fix/sv_state.py``:
#
#   Tracked    — receiver sees SV (SvAmbState.TRACKING), not yet
#                admitted to the float PPP filter
#   Floating   — admitted, MW accumulating, no integer fix yet
#                (SvAmbState.FLOATING)
#   Converging — wide-lane integer fixed, narrow-lane pending
#                (SvAmbState.CONVERGING)
#   Waiting    — cooldown-bound after a slip / false fix
#                (SvAmbState.WAITING).  Placed between Converging
#                and Anchoring because a recovered SV typically
#                re-fixes WL off cooldown and then heads into
#                Anchoring next.
#   Anchoring  — NL integer landed, earning Δaz validation
#                (SvAmbState.ANCHORING).  Short-term member of the
#                fix set.
#   Anchored   — NL integer has survived ≥ 8° Δaz
#                (SvAmbState.ANCHORED).  Long-term member.
#
# Reading left-to-right gives a coherent progression story.  The
# Tracked/Floating split (added 2026-04-21) matters during
# bootstrap where Floating lags Tracked as the engine admits SVs
# to the filter at its own pace.  A persistent gap between the
# two indicates admission-path issues.
#
# Third field flags columns whose values should render as ``-``
# (architecturally impossible) when the constellation lacks NL
# capability — when the receiver's tracked signals don't line up
# with the correction stream's published phase biases for NL
# integer fixing.  Only Anchoring and Anchored need the capability
# check; the rest are reachable on any dual-freq GNSS.
#
# Column header strings are kept compact so the SvStateTable rows
# stay narrow (three-constellation fleet on an 80-col terminal is
# the target).  "Anchoring" / "Anchored" don't abbreviate cleanly,
# so we use them in full; "Converging" loses to the shorter "WL"
# as a compromise — the column's *state* is the phase between WL
# and NL (narrow-lane pending), which "WL" still communicates.
_COLUMNS: tuple[tuple[str, frozenset[str], bool], ...] = (
    ("Tracked",   frozenset({"TRACKING"}),   False),
    ("Floating",  frozenset({"FLOATING"}),   False),
    ("WL",        frozenset({"CONVERGING"}), False),
    ("Waiting",   frozenset({"WAITING"}),    False),
    ("Anchoring", frozenset({"ANCHORING"}),  True),
    ("Anchored",  frozenset({"ANCHORED"}),   True),
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
        for col_name, _members, _needs_nl in _COLUMNS:
            table.add_column(col_name, justify="right")
        for prefix, label in _CONSTELLATION_ROWS:
            row_counts = counts.get(prefix, {})
            cells = [label]
            constellation_observed = prefix in observed
            nl_capable = prefix in self._nl_capable
            for _col_name, members, needs_nl in _COLUMNS:
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
        reached_anchored: bool = False,
        id: Optional[str] = None,  # noqa: A002
        classes: Optional[str] = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._state = state
        self._position = position
        self._sigma_m = sigma_m
        self._reached_anchored = reached_anchored

    def update_position(
        self,
        *,
        state: Optional[str],
        position: Optional[tuple[float, float, float]],
        sigma_m: Optional[float],
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
            and reached_anchored == self._reached_anchored
        ):
            return
        self._state = state
        self._position = position
        self._sigma_m = sigma_m
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
        # Format matches engine's current precision (:.6f lat/lon,
        # :.1f alt).  Widens automatically when main lands the
        # precision ask — the regex captures whatever's logged,
        # so the string we get already has the full precision;
        # our format here re-stringifies with enough decimals to
        # preserve what the engine emitted.  Using :.8f / :.3f
        # gives a fixed width today (tail zeros) and becomes
        # exact when the engine widens.  Trailing-zero shading
        # from uncertainty handles the visual.
        lat_s = f"{lat:.8f}"
        lon_s = f"{lon:.8f}"
        alt_s = f"{alt:.3f}"
        self._append_shaded_deg(t, lat_s)
        t.append(" / ")
        self._append_shaded_deg(t, lon_s)
        t.append(" / ")
        self._append_shaded_m(t, alt_s)
        t.append(" ")
        t.append(format_uncertainty(self._sigma_m))
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
            t.append(f"{self._delta:.1f} m 3D")
        return t
