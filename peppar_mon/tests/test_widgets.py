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

from peppar_mon.widgets import StateBar


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


if __name__ == "__main__":
    unittest.main()
