"""Unit tests for ``peppar_mon.app`` — specifically the pure
``build_uptime_line`` renderer.

The rest of the app is a Textual App that pumps widgets through
the event loop — exercised end-to-end by the ``--web`` smoke test
(which we don't include in unit tests).  This file targets the
piece with enough branching logic to need per-case coverage: the
uptime / DOWN indicator.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from peppar_mon.app import build_uptime_line


def _state(
    *,
    engine_start: datetime | None = None,
    last_line: datetime | None = None,
) -> SimpleNamespace:
    """Minimal LogState stand-in carrying only the two fields
    ``build_uptime_line`` reads.  Avoids the full dataclass import
    so the test doubles as documentation of which fields the
    function actually touches."""
    return SimpleNamespace(
        engine_start_time=engine_start,
        last_line_time=last_line,
    )


class BuildUptimeLineTest(unittest.TestCase):
    NOW = datetime(2026, 4, 21, 15, 0, 0)
    THRESHOLD = 30.0

    def test_no_engine_start_shows_waiting(self):
        """Before the log reader has seen a timestamped line, the
        row shows the waiting state — distinct from DOWN because
        the engine may simply not have started yet."""
        line = build_uptime_line(
            state=_state(engine_start=None),
            now=self.NOW,
            stale_threshold_s=self.THRESHOLD,
        )
        self.assertEqual(
            line, "PePPAR-Fix UpTime  (waiting for first log line)",
        )
        self.assertNotIn("DOWN", line)

    def test_fresh_log_shows_uptime(self):
        """Last line within threshold → show uptime, not DOWN."""
        engine_start = self.NOW - timedelta(hours=2, minutes=15)
        last_line = self.NOW - timedelta(seconds=5)
        line = build_uptime_line(
            state=_state(engine_start=engine_start, last_line=last_line),
            now=self.NOW,
            stale_threshold_s=self.THRESHOLD,
        )
        self.assertIn("UpTime", line)
        self.assertIn("0d 2h 15m", line)
        self.assertNotIn("DOWN", line)

    def test_stale_log_shows_down(self):
        """Last line older than threshold → DOWN indicator with
        elapsed-since-last-activity."""
        engine_start = self.NOW - timedelta(hours=3)
        last_line = self.NOW - timedelta(seconds=45)
        line = build_uptime_line(
            state=_state(engine_start=engine_start, last_line=last_line),
            now=self.NOW,
            stale_threshold_s=self.THRESHOLD,
        )
        self.assertIn("DOWN", line)
        self.assertIn("45s", line)
        # Uptime text must be *replaced*, not appended — operator
        # should never see both a UpTime and a DOWN on the same line.
        self.assertNotIn("UpTime", line)

    def test_stale_log_uses_rich_red_markup(self):
        """DOWN renders with red+bold Rich markup so it's visually
        distinct on a color terminal / web-terminal render."""
        line = build_uptime_line(
            state=_state(
                engine_start=self.NOW - timedelta(hours=1),
                last_line=self.NOW - timedelta(minutes=5),
            ),
            now=self.NOW,
            stale_threshold_s=self.THRESHOLD,
        )
        self.assertIn("[bold red]DOWN[/]", line)

    def test_just_below_threshold_still_shows_uptime(self):
        """29 s stale with a 30 s threshold → still uptime.
        Boundary case matters because the stall might be a
        transient write hiccup, not a dead engine."""
        last_line = self.NOW - timedelta(seconds=29)
        line = build_uptime_line(
            state=_state(
                engine_start=self.NOW - timedelta(minutes=5),
                last_line=last_line,
            ),
            now=self.NOW,
            stale_threshold_s=self.THRESHOLD,
        )
        self.assertIn("UpTime", line)
        self.assertNotIn("DOWN", line)

    def test_just_above_threshold_shows_down(self):
        """31 s stale with a 30 s threshold → DOWN."""
        last_line = self.NOW - timedelta(seconds=31)
        line = build_uptime_line(
            state=_state(
                engine_start=self.NOW - timedelta(minutes=5),
                last_line=last_line,
            ),
            now=self.NOW,
            stale_threshold_s=self.THRESHOLD,
        )
        self.assertIn("DOWN", line)
        self.assertIn("31s", line)

    def test_hour_scale_stale_drops_to_hm(self):
        """Long-dead engine — show hours/minutes, not seconds
        precision that doesn't help."""
        last_line = self.NOW - timedelta(hours=2, minutes=5)
        line = build_uptime_line(
            state=_state(
                engine_start=self.NOW - timedelta(hours=5),
                last_line=last_line,
            ),
            now=self.NOW,
            stale_threshold_s=self.THRESHOLD,
        )
        self.assertIn("DOWN", line)
        self.assertIn("2h 5m", line)

    def test_stale_log_from_cold_monitor_start(self):
        """Monitor starts up on a log whose engine died hours ago.
        First tick shows DOWN immediately — we shouldn't wait for
        the threshold to elapse on the *monitor's* wall clock,
        because the log data already proves the engine is dead."""
        engine_start = self.NOW - timedelta(hours=5)
        last_line = self.NOW - timedelta(hours=3)
        line = build_uptime_line(
            state=_state(engine_start=engine_start, last_line=last_line),
            now=self.NOW,
            stale_threshold_s=self.THRESHOLD,
        )
        self.assertIn("DOWN", line)
        self.assertIn("3h 0m", line)


if __name__ == "__main__":
    unittest.main()
