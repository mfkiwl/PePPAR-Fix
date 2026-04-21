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

# Column definitions: (header label, SvAmbState values aggregated).
# TRACKING and FLOAT are pooled into "Tracked" because the distinction
# (receiver-sees vs admitted) isn't operationally meaningful at a
# glance — what matters is "we see it but haven't integer-fixed it."
# WL/NL_SHORT/NL_LONG are each their own column since they're distinct
# steps in the promotion ladder.  SQUELCHED is its own column so a
# cooldown-dominated constellation is visible.
_COLUMNS: tuple[tuple[str, frozenset[str]], ...] = (
    ("Tracked",   frozenset({"TRACKING", "FLOAT"})),
    ("WL",        frozenset({"WL_FIXED"})),
    ("NL_SHORT",  frozenset({"NL_SHORT_FIXED"})),
    ("NL_LONG",   frozenset({"NL_LONG_FIXED"})),
    ("SQUELCHED", frozenset({"SQUELCHED"})),
)


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
        id: Optional[str] = None,  # noqa: A002 — Textual ctor kwarg
    ) -> None:
        super().__init__(id=id)
        self._sv_states: Mapping[str, str] = dict(sv_states or {})

    def update_sv_states(
        self, sv_states: Mapping[str, str],
    ) -> None:
        """Record a new sv_states snapshot and refresh.

        We compare by counts-per-cell, not by the raw dict, because
        the dict changes on every SV transition but most of those
        transitions won't move any cell count (e.g. a WL_FIXED SV
        cycling into NL_SHORT_FIXED and back doesn't change the
        aggregated totals when both happen between ticks).  Saves
        repaints on busy logs.
        """
        new_counts = _aggregate(sv_states)
        old_counts = _aggregate(self._sv_states)
        if new_counts == old_counts:
            # Still replace the stored dict so future diffs compare
            # against the latest snapshot — but skip the refresh().
            self._sv_states = dict(sv_states)
            return
        self._sv_states = dict(sv_states)
        self.refresh()

    # ── Rendering ───────────────────────────────────────────────── #

    def render(self) -> Table:  # noqa: D401 — Rich-style
        """Render the fixed-shape table with current counts."""
        counts = _aggregate(self._sv_states)
        table = Table(
            box=None,      # the app container has its own padding
            padding=(0, 1),
            show_header=True,
            show_edge=False,
        )
        # Left column is the constellation label; the rest are state
        # aggregates from _COLUMNS.
        table.add_column("", style="bold")
        for col_name, _members in _COLUMNS:
            # Right-justify numeric columns so varying widths line up.
            table.add_column(col_name, justify="right")
        for prefix, label in _CONSTELLATION_ROWS:
            row_counts = counts.get(prefix, {})
            cells = [label]
            for _col_name, members in _COLUMNS:
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
