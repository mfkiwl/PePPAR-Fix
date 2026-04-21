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

from typing import Iterable, Optional

from textual.widget import Widget
from rich.text import Text


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

    def render(self) -> Text:
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
