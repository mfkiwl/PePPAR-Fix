"""Unit tests for ``peppar_mon.widgets``.

StateBar renders a one-line horizontal state indicator.  The tests
focus on behavior that matters for end users: current state is
visibly distinguished, visited states are dimmed, never-visited
states render plain, and update_state is a no-op when nothing
changed (so a 1 Hz tick that finds the machine idle doesn't churn
the framebuffer).

We exercise the widget without mounting it in a full Textual app.
``render()`` returns a Rich ``Text`` we can inspect directly.
"""

from __future__ import annotations

import unittest

from peppar_mon.widgets import StateBar, SvStateTable, _aggregate


_ANT_STATES = (
    "unsurveyed", "verifying", "verified",
    "converging", "resolved", "moved",
)


def _span_style(text, substring):
    """Return the Rich style string covering the given substring.

    The first span whose span range covers any character of the
    substring wins; adequate for these tests where each label is
    adjacent to the style that targets it.
    """
    plain = text.plain
    start = plain.index(substring)
    for span in text.spans:
        if span.start <= start < span.end:
            return str(span.style)
    return ""


class StateBarRenderTest(unittest.TestCase):
    def setUp(self):
        self.bar = StateBar(
            machine_name="AntPosEst",
            all_states=_ANT_STATES,
        )

    def test_machine_name_appears(self):
        t = self.bar.render()
        self.assertIn("AntPosEst:", t.plain)

    def test_all_states_appear_even_when_unvisited(self):
        t = self.bar.render()
        for s in _ANT_STATES:
            self.assertIn(s, t.plain)

    def test_current_state_wrapped_in_brackets(self):
        """Brackets provide a highlight cue that survives a
        monochrome terminal or a piped render.  Non-current states
        stay un-bracketed."""
        self.bar.update_state(current="converging", visited=("converging",))
        t = self.bar.render()
        self.assertIn("[converging]", t.plain)
        # Other states stay plain.
        self.assertNotIn("[unsurveyed]", t.plain)

    def test_current_state_has_bold_style(self):
        self.bar.update_state(current="converging", visited=("converging",))
        t = self.bar.render()
        self.assertIn("bold", _span_style(t, "[converging]"))

    def test_visited_states_are_dimmed(self):
        """Visited-but-not-current: dim style so the eye skips them
        but the operator can still see the trajectory."""
        self.bar.update_state(
            current="converging",
            visited=("unsurveyed", "verifying", "converging"),
        )
        t = self.bar.render()
        self.assertIn("dim", _span_style(t, "unsurveyed"))
        self.assertIn("dim", _span_style(t, "verifying"))

    def test_never_visited_states_plain(self):
        """States the machine hasn't touched render with no style
        (no dim, no reverse) — gives the operator a quick read of
        'where we haven't been yet.'"""
        self.bar.update_state(current="converging", visited=("converging",))
        t = self.bar.render()
        # `resolved` is a future state; must not be dim.
        style = _span_style(t, "resolved")
        self.assertNotIn("dim", style)
        self.assertNotIn("reverse", style)

    def test_update_state_no_op_when_unchanged(self):
        """A second update_state call with the same args shouldn't
        refresh() — saves repaints on the 1 Hz tick when the machine
        is idle.  We verify by counting refresh invocations."""
        calls = {"n": 0}

        def fake_refresh():
            calls["n"] += 1
        self.bar.refresh = fake_refresh  # type: ignore[method-assign]

        self.bar.update_state(current="converging", visited=("converging",))
        self.bar.update_state(current="converging", visited=("converging",))
        self.bar.update_state(current="converging", visited=("converging",))
        self.assertEqual(calls["n"], 1, "second and third calls must no-op")

    def test_update_state_refreshes_when_current_changes(self):
        calls = {"n": 0}

        def fake_refresh():
            calls["n"] += 1
        self.bar.refresh = fake_refresh  # type: ignore[method-assign]

        self.bar.update_state(current="unsurveyed", visited=("unsurveyed",))
        self.bar.update_state(
            current="verifying", visited=("unsurveyed", "verifying"),
        )
        self.assertEqual(calls["n"], 2)

    def test_handles_none_current_before_first_transition(self):
        """Before the first [STATE] line, current is None.  Bar should
        render all states plain and not crash."""
        t = self.bar.render()  # current=None, visited=() from ctor
        self.assertIn("AntPosEst:", t.plain)
        for s in _ANT_STATES:
            self.assertNotIn(f"[{s}]", t.plain)  # nothing is "current"


class AggregateTest(unittest.TestCase):
    """`_aggregate` turns sv_states into constellation→state→count."""

    def test_empty_input_returns_empty_dict(self):
        self.assertEqual(_aggregate({}), {})

    def test_single_sv_lands_in_prefix(self):
        got = _aggregate({"G05": "FLOAT"})
        self.assertEqual(got, {"G": {"FLOAT": 1}})

    def test_multi_constellation_partitions_correctly(self):
        sv_states = {
            "G05": "FLOAT", "G10": "FLOAT", "G17": "WL_FIXED",
            "E21": "NL_LONG_FIXED", "E05": "NL_SHORT_FIXED",
            "C32": "SQUELCHED",
        }
        got = _aggregate(sv_states)
        self.assertEqual(got["G"], {"FLOAT": 2, "WL_FIXED": 1})
        self.assertEqual(
            got["E"], {"NL_LONG_FIXED": 1, "NL_SHORT_FIXED": 1},
        )
        self.assertEqual(got["C"], {"SQUELCHED": 1})

    def test_unknown_prefix_still_tallied(self):
        """An R-prefix (GLONASS) SV doesn't have a row in the table
        today, but _aggregate itself just groups by prefix — the
        widget is the one that filters to known constellations.
        Keeps the function decoupled from row definitions."""
        got = _aggregate({"R01": "FLOAT"})
        self.assertEqual(got, {"R": {"FLOAT": 1}})


class SvStateTableRenderTest(unittest.TestCase):
    """`SvStateTable.render()` produces a correct count grid."""

    def setUp(self):
        self.table = SvStateTable()

    def test_empty_sv_states_renders_all_zeros(self):
        """No SVs observed yet — table still shows all three
        constellation rows with zero counts.  Predictable shape,
        no "invisible until data arrives" flicker."""
        rich_table = self.table.render()
        # Render the table to a plain string to introspect cells.
        from rich.console import Console
        console = Console(width=100, record=True, legacy_windows=False)
        console.print(rich_table)
        out = console.export_text()
        self.assertIn("GPS", out)
        self.assertIn("GAL", out)
        self.assertIn("BDS", out)

    def test_counts_partition_correctly(self):
        """Cells match `_aggregate` output for a known input.
        All three constellations are NL-capable here so the NL
        cells render counts (not ``-``) — isolates partitioning
        from the capability signal tested separately below."""
        self.table.update(
            sv_states={
                "G05": "FLOAT", "G10": "FLOAT", "G17": "WL_FIXED",
                "E21": "NL_LONG_FIXED", "E05": "NL_SHORT_FIXED",
                "C32": "SQUELCHED",
            },
            nl_capable=frozenset("GEC"),
        )
        from rich.console import Console
        console = Console(width=100, record=True, legacy_windows=False)
        console.print(self.table.render())
        out = console.export_text()
        # GPS row should have 2 in Tracked (2× FLOAT) + 1 in WL.
        gps_line = next(line for line in out.splitlines() if "GPS" in line)
        # Header is `Tracked WL NL_SHORT NL_LONG SQUELCHED`; our row
        # values in the same order are 2 1 0 0 0.  Rich-render
        # inserts whitespace; check each value appears in the row.
        self.assertEqual(
            gps_line.split(), ["GPS", "2", "1", "0", "0", "0"],
        )
        gal_line = next(line for line in out.splitlines() if "GAL" in line)
        self.assertEqual(
            gal_line.split(), ["GAL", "0", "0", "0", "1", "1"],
        )
        bds_line = next(line for line in out.splitlines() if "BDS" in line)
        self.assertEqual(
            bds_line.split(), ["BDS", "0", "0", "1", "0", "0"],
        )

    def test_squelched_is_its_own_column(self):
        """SQUELCHED SVs don't fall into Tracked — they're their own
        column.  Catches a common source of mis-aggregation where
        "SV not in fix set" gets conflated with "tracked"."""
        self.table.update(
            sv_states={
                "G05": "SQUELCHED", "G10": "SQUELCHED", "G17": "FLOAT",
            },
            nl_capable=frozenset("G"),
        )
        from rich.console import Console
        console = Console(width=100, record=True, legacy_windows=False)
        console.print(self.table.render())
        out = console.export_text()
        gps_line = next(line for line in out.splitlines() if "GPS" in line)
        # Tracked=1 (the one FLOAT), SQUELCHED=2
        self.assertEqual(
            gps_line.split(), ["GPS", "1", "0", "2", "0", "0"],
        )

    def test_tracking_pools_into_tracked(self):
        """TRACKING and FLOAT both land in the "Tracked" column —
        the distinction is less useful than "admitted-but-not-fixed"
        grouping."""
        self.table.update(
            sv_states={"G05": "TRACKING", "G10": "FLOAT"},
            nl_capable=frozenset("G"),
        )
        from rich.console import Console
        console = Console(width=100, record=True, legacy_windows=False)
        console.print(self.table.render())
        out = console.export_text()
        gps_line = next(line for line in out.splitlines() if "GPS" in line)
        self.assertEqual(
            gps_line.split(), ["GPS", "2", "0", "0", "0", "0"],
        )

    def test_update_no_op_when_counts_unchanged(self):
        """A new snapshot that aggregates to identical counts
        shouldn't trigger refresh() — matters for busy logs where
        per-SV churn doesn't change the totals."""
        calls = {"n": 0}

        def fake_refresh():
            calls["n"] += 1
        self.table.refresh = fake_refresh  # type: ignore[method-assign]

        # First update — state changes from {} to {G05: FLOAT}.
        self.table.update_sv_states({"G05": "FLOAT"})
        self.assertEqual(calls["n"], 1)
        # Second update — same counts per constellation column
        # (G05 FLOAT → G10 FLOAT is a different dict but same cell
        # counts).  Must no-op on refresh.
        self.table.update_sv_states({"G10": "FLOAT"})
        self.assertEqual(calls["n"], 1)
        # Third update — G05 transitions to WL_FIXED: the counts
        # change, so refresh must fire.
        self.table.update_sv_states({"G05": "WL_FIXED"})
        self.assertEqual(calls["n"], 2)


class SvStateTableCapabilityTest(unittest.TestCase):
    """``-`` vs ``0`` distinction driven by nl_capable set and
    observed-constellation set."""

    def _render(self, table):
        from rich.console import Console
        console = Console(width=100, record=True, legacy_windows=False)
        console.print(table.render())
        return console.export_text()

    def test_unobserved_constellation_renders_all_dashes(self):
        """Constellation never observed in sv_states (e.g. BDS on a
        run with systems=gps,gal) → entire row renders ``-``.
        Protects operator from reading a "0" as "currently none"
        when it really means "not configured"."""
        table = SvStateTable()
        table.update(sv_states={"G05": "FLOAT"}, nl_capable=frozenset("G"))
        out = self._render(table)
        bds_line = next(line for line in out.splitlines() if "BDS" in line)
        self.assertEqual(
            bds_line.split(), ["BDS", "-", "-", "-", "-", "-"],
        )

    def test_nl_cells_dash_when_not_capable(self):
        """Observed constellation without NL capability (ptpmon GPS
        case: tracked, WL reachable, but L2L phase biases missing)
        → Tracked/WL/SQUELCHED render counts, NL cells render ``-``."""
        table = SvStateTable()
        table.update(
            sv_states={"G05": "FLOAT", "G10": "WL_FIXED"},
            nl_capable=frozenset(),  # GPS not NL-capable
        )
        out = self._render(table)
        gps_line = next(line for line in out.splitlines() if "GPS" in line)
        self.assertEqual(
            gps_line.split(), ["GPS", "1", "1", "0", "-", "-"],
        )

    def test_nl_cells_zero_when_capable_but_empty(self):
        """Observed + NL-capable constellation with no NL fixes yet
        → NL cells render ``0`` (not ``-``).  This is the
        distinction that lets an operator see the filter is in a
        position to promote SVs even when none have promoted yet."""
        table = SvStateTable()
        table.update(
            sv_states={"E05": "FLOAT", "E10": "WL_FIXED"},
            nl_capable=frozenset("E"),
        )
        out = self._render(table)
        gal_line = next(line for line in out.splitlines() if "GAL" in line)
        self.assertEqual(
            gal_line.split(), ["GAL", "1", "1", "0", "0", "0"],
        )

    def test_ptpmon_scenario(self):
        """End-to-end ptpmon day0421c picture: GPS SVs tracked + WL
        but zero NL capability; GAL has full capability with some
        counts in each state; BDS not present.  Expected rendering
        (columns: Tracked, WL, SQUELCHED, NL_SHORT, NL_LONG):

            GPS  3  6  0  -  -
            GAL  3  4  2  1  0
            BDS  -  -  -  -  -
        """
        sv_states = {
            # GPS: 3 in tracked, 6 in WL, 0 in NL states, 0 squelched.
            **{f"G0{i}": "FLOAT" for i in range(1, 4)},
            **{f"G1{i}": "WL_FIXED" for i in range(0, 6)},
            # GAL: 3 tracked, 4 WL, 1 NL_SHORT, 0 NL_LONG, 2 squelched.
            **{f"E0{i}": "FLOAT" for i in range(1, 4)},
            **{f"E1{i}": "WL_FIXED" for i in range(0, 4)},
            "E21": "NL_SHORT_FIXED",
            "E22": "SQUELCHED",
            "E23": "SQUELCHED",
            # BDS: none.
        }
        table = SvStateTable()
        table.update(sv_states=sv_states, nl_capable=frozenset("E"))
        out = self._render(table)
        gps_line = next(line for line in out.splitlines() if "GPS" in line)
        gal_line = next(line for line in out.splitlines() if "GAL" in line)
        bds_line = next(line for line in out.splitlines() if "BDS" in line)
        self.assertEqual(
            gps_line.split(), ["GPS", "3", "6", "0", "-", "-"],
        )
        self.assertEqual(
            gal_line.split(), ["GAL", "3", "4", "2", "1", "0"],
        )
        self.assertEqual(
            bds_line.split(), ["BDS", "-", "-", "-", "-", "-"],
        )

    def test_capability_flip_triggers_refresh(self):
        """When nl_capable gains a new constellation, the table must
        refresh — cells flip from ``-`` to counts, which is a visible
        change even if sv_states didn't move."""
        table = SvStateTable()
        table.update(
            sv_states={"E05": "WL_FIXED"}, nl_capable=frozenset(),
        )
        calls = {"n": 0}

        def fake_refresh():
            calls["n"] += 1
        table.refresh = fake_refresh  # type: ignore[method-assign]

        # Same sv_states, different nl_capable — must refresh.
        table.update(
            sv_states={"E05": "WL_FIXED"}, nl_capable=frozenset("E"),
        )
        self.assertEqual(calls["n"], 1)
        # Same sv_states AND same nl_capable — no-op.
        table.update(
            sv_states={"E05": "WL_FIXED"}, nl_capable=frozenset("E"),
        )
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
