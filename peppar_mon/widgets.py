"""Widgets for the peppar-mon display.

Started as a home for the horizontal state-bar widgets: one cell per
possible state of a state machine, current state highlighted, visited
states marked so the operator can see the trajectory at a glance.

The two machines we care about right now are AntPosEst (antenna-
position state: UNSURVEYED → VERIFYING → VERIFIED → CONVERGING →
RESOLVED → MOVED) and DOFreqEst (DO frequency estimator: UNINITIALIZED
→ PHASE_SETTING → FREQ_VERIFYING → TRACKING → HOLDOVER).  The values
come from ``scripts/peppar_fix/states.py`` — keep the enum lists here
in sync if the engine side changes.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Mapping, Optional

from textual.widget import Widget
from rich.table import Table
from rich.text import Text

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
# Column ORDER is operationally meaningful — it mirrors the usual
# SV trajectory: first seen (Tracked), wide-lane fixed (WL),
# temporary setback (SQUELCHED), then the promotion ladder
# (NL_SHORT → NL_LONG).  Placing SQUELCHED between WL and
# NL_SHORT matches the common recovery path: a squelched SV comes
# off cooldown, re-fixes WL, and typically heads into NL_SHORT
# next.  Reading left-to-right gives a coherent story.
#
# Third field flags columns whose values should render as ``-``
# (architecturally impossible) when the constellation lacks NL
# capability — i.e. when the receiver's tracked signals don't line
# up with the correction stream's published phase biases for NL
# integer fixing.  Only NL_SHORT and NL_LONG need the capability
# check; Tracked/WL/SQUELCHED are reachable on any dual-freq GNSS.
#
# TRACKING and FLOAT are pooled into "Tracked" because the distinction
# (receiver-sees vs admitted) isn't operationally meaningful at a
# glance — what matters is "we see it but haven't integer-fixed it."
_COLUMNS: tuple[tuple[str, frozenset[str], bool], ...] = (
    ("Tracked",   frozenset({"TRACKING", "FLOAT"}),    False),
    ("WL",        frozenset({"WL_FIXED"}),             False),
    ("SQUELCHED", frozenset({"SQUELCHED"}),            False),
    ("NL_SHORT",  frozenset({"NL_SHORT_FIXED"}),       True),
    ("NL_LONG",   frozenset({"NL_LONG_FIXED"}),        True),
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
            all_states=["unsurveyed", "verifying", ...],
        )
        ...
        bar.update_state(
            current="converging",
            visited=("unsurveyed", "verifying", "converging"),
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

        Layout: ``AntPosEst:  unsurveyed  verifying  [converging]  ...``

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

                    Tracked  WL  NL_SHORT  NL_LONG  SQUELCHED
            GPS         5     3         1        0          1
            GAL         6     4         2        1          0
            BDS         3     2         0        0          0

    Columns aggregate across ``SvAmbState`` values — see the
    ``_COLUMNS`` module constant for the exact grouping.  "Tracked"
    pools TRACKING+FLOAT because at-a-glance the distinction between
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
