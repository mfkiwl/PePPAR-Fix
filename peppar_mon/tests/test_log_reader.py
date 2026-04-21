"""Unit tests for peppar_mon.log_reader.

Uses a temp file written synchronously and polls the reader's
``state`` to assert it catches up.  No Textual, no sockets, no
stdlib-only.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from peppar_mon.log_reader import LogReader


def _wait_until(predicate, *, timeout_s: float = 2.0, poll_s: float = 0.02):
    """Poll ``predicate`` until it returns truthy or the timeout hits.

    Returns the last predicate value.  Keeps tests deterministic without
    sleeping for fixed durations — tests fail fast when something stalls.
    """
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(poll_s)
    return last


class LogReaderTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = Path(self._tmpdir.name) / "engine.log"

    # ── Replay phase ─────────────────────────────────────────── #

    def test_replay_extracts_first_timestamp(self):
        self.path.write_text(
            "2026-04-19 21:09:12,007 INFO Host config: /home/bob/...\n"
            "2026-04-19 21:09:12,019 INFO Opening /dev/gnss0 ...\n"
            "2026-04-19 21:09:13,041 INFO Receiver identity: ZED-F9T\n"
        )
        reader = LogReader(self.path)
        reader.start()
        self.addCleanup(reader.stop)
        got = _wait_until(lambda: reader.state.engine_start_time)
        self.assertEqual(got, datetime(2026, 4, 19, 21, 9, 12, 7_000))
        # Last observed timestamp is the third line's.
        _wait_until(lambda: reader.state.last_line_time and
                    reader.state.last_line_time.second == 13)
        self.assertEqual(
            reader.state.last_line_time,
            datetime(2026, 4, 19, 21, 9, 13, 41_000),
        )

    def test_replay_skips_blank_and_non_timestamp_lines(self):
        self.path.write_text(
            "\n"
            "Traceback (most recent call last):\n"
            "  File 'foo.py', line 42, in bar\n"
            "2026-04-19 21:09:15,500 INFO first real line\n"
        )
        reader = LogReader(self.path)
        reader.start()
        self.addCleanup(reader.stop)
        got = _wait_until(lambda: reader.state.engine_start_time)
        self.assertEqual(got, datetime(2026, 4, 19, 21, 9, 15, 500_000))

    # ── Follow phase ─────────────────────────────────────────── #

    def test_follow_picks_up_appended_lines(self):
        self.path.write_text(
            "2026-04-19 21:09:12,007 INFO initial\n"
        )
        reader = LogReader(self.path)
        reader.start()
        self.addCleanup(reader.stop)
        _wait_until(lambda: reader.state.engine_start_time)
        lines_before = reader.state.lines_read
        # Append a new line; reader should pick it up within the
        # follow poll interval.
        with self.path.open("a") as f:
            f.write("2026-04-19 21:10:00,123 INFO later line\n")
        new_last = _wait_until(
            lambda: (reader.state.last_line_time
                     and reader.state.last_line_time.minute == 10),
        )
        self.assertIsNotNone(new_last)
        self.assertGreater(reader.state.lines_read, lines_before)
        # Start time must NOT move — only last_line_time changes.
        self.assertEqual(
            reader.state.engine_start_time,
            datetime(2026, 4, 19, 21, 9, 12, 7_000),
        )

    # ── File-not-found tolerance ─────────────────────────────── #

    def test_waits_for_file_that_appears_later(self):
        later_path = Path(self._tmpdir.name) / "appears-later.log"
        reader = LogReader(later_path)
        reader.start()
        self.addCleanup(reader.stop)
        # Give the reader a moment to notice the file is missing, then
        # create it.
        time.sleep(0.1)
        self.assertIsNone(reader.state.engine_start_time)
        later_path.write_text(
            "2026-04-19 22:00:00,000 INFO here now\n"
        )
        got = _wait_until(lambda: reader.state.engine_start_time,
                          timeout_s=3.0)
        self.assertEqual(got, datetime(2026, 4, 19, 22, 0, 0, 0))

    # ── Stop is prompt ───────────────────────────────────────── #

    def test_stop_joins_thread_promptly(self):
        self.path.write_text(
            "2026-04-19 21:09:12,007 INFO initial\n"
        )
        reader = LogReader(self.path)
        reader.start()
        _wait_until(lambda: reader.state.engine_start_time)
        t0 = time.monotonic()
        reader.stop(timeout=2.0)
        elapsed = time.monotonic() - t0
        # Reader is in the follow loop with a 0.2 s poll; stop should
        # return well within a second.
        self.assertLess(elapsed, 1.0)
        self.assertFalse(reader._thread.is_alive())


class StateLineParsingTest(unittest.TestCase):
    """[STATE] transitions feed into ant_pos_est_state / do_freq_est_state."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = Path(self._tmpdir.name) / "engine.log"

    def test_initial_state_line_sets_field(self):
        """`[STATE] AntPosEst: → unsurveyed (initial)` seeds the state."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO [STATE] AntPosEst: → "
            "unsurveyed (initial)\n"
            "2026-04-21 07:00:00,001 INFO [STATE] DOFreqEst: → "
            "uninitialized (initial)\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(
            lambda: r.state.ant_pos_est_state and r.state.do_freq_est_state
        )
        self.assertEqual(r.state.ant_pos_est_state, "unsurveyed")
        self.assertEqual(r.state.do_freq_est_state, "uninitialized")
        self.assertEqual(r.state.ant_pos_est_visited, ("unsurveyed",))
        self.assertEqual(r.state.do_freq_est_visited, ("uninitialized",))

    def test_transition_line_updates_current_and_visited(self):
        """A transition appends to visited, sets current to the new state."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO [STATE] AntPosEst: → "
            "unsurveyed (initial)\n"
            "2026-04-21 07:05:00,000 INFO [STATE] AntPosEst: unsurveyed → "
            "verifying after 300s\n"
            "2026-04-21 07:06:00,000 INFO [STATE] AntPosEst: verifying → "
            "converging after 60s\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.ant_pos_est_state == "converging")
        self.assertEqual(r.state.ant_pos_est_state, "converging")
        self.assertEqual(
            r.state.ant_pos_est_visited,
            ("unsurveyed", "verifying", "converging"),
        )

    def test_revisiting_state_doesnt_dup_visited(self):
        """When the machine flaps back to a state it's been in before,
        visited must not contain duplicates — the set-like guarantee
        matters for the widget's rendering."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO [STATE] AntPosEst: → "
            "converging (initial)\n"
            "2026-04-21 07:05:00,000 INFO [STATE] AntPosEst: converging → "
            "resolved after 300s\n"
            "2026-04-21 07:06:00,000 INFO [STATE] AntPosEst: resolved → "
            "converging after 60s\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.ant_pos_est_state == "converging" and
                    "resolved" in r.state.ant_pos_est_visited)
        self.assertEqual(r.state.ant_pos_est_state, "converging")
        self.assertEqual(
            r.state.ant_pos_est_visited, ("converging", "resolved"),
        )

    def test_machines_are_independent(self):
        """Updating one machine's state leaves the other alone."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO [STATE] AntPosEst: → "
            "unsurveyed (initial)\n"
            "2026-04-21 07:00:00,001 INFO [STATE] DOFreqEst: → "
            "uninitialized (initial)\n"
            "2026-04-21 07:05:00,000 INFO [STATE] DOFreqEst: "
            "uninitialized → phase_setting after 300s\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.do_freq_est_state == "phase_setting")
        self.assertEqual(r.state.ant_pos_est_state, "unsurveyed")
        self.assertEqual(r.state.do_freq_est_state, "phase_setting")

    def test_non_state_lines_are_ignored(self):
        """Lines without ``[STATE] <machine>: → <state>`` must not touch
        the state fields — false matches here would corrupt the display."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO AntPosEst position improved\n"
            "2026-04-21 07:00:00,001 INFO Moving from one place to another\n"
            "2026-04-21 07:00:01,000 INFO [NL_DIAG] epoch=5 result=CAND\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.lines_read >= 3)
        self.assertIsNone(r.state.ant_pos_est_state)
        self.assertIsNone(r.state.do_freq_est_state)


class SvStateParsingTest(unittest.TestCase):
    """[SV_STATE] transitions feed the per-SV state dict."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = Path(self._tmpdir.name) / "engine.log"

    def test_transition_updates_sv_state(self):
        """Basic case: one SV transitions TRACKING → FLOAT → WL_FIXED,
        final value in sv_states matches the last transition."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO [SV_STATE] G05: TRACKING → "
            "FLOAT (epoch=10)\n"
            "2026-04-21 07:00:30,000 INFO [SV_STATE] G05: FLOAT → "
            "WL_FIXED (epoch=40)\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.sv_states.get("G05") == "WL_FIXED")
        self.assertEqual(r.state.sv_states.get("G05"), "WL_FIXED")

    def test_multi_sv_independent_tracking(self):
        """Concurrent SVs: each tracked independently, no cross-talk."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO [SV_STATE] G05: TRACKING → "
            "FLOAT (epoch=10)\n"
            "2026-04-21 07:00:01,000 INFO [SV_STATE] E21: TRACKING → "
            "FLOAT (epoch=10)\n"
            "2026-04-21 07:00:02,000 INFO [SV_STATE] C32: TRACKING → "
            "FLOAT (epoch=10)\n"
            "2026-04-21 07:00:03,000 INFO [SV_STATE] E21: FLOAT → "
            "WL_FIXED (epoch=15)\n"
            "2026-04-21 07:00:04,000 INFO [SV_STATE] E21: WL_FIXED → "
            "NL_SHORT_FIXED (epoch=20)\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(
            lambda: r.state.sv_states.get("E21") == "NL_SHORT_FIXED",
        )
        self.assertEqual(r.state.sv_states.get("G05"), "FLOAT")
        self.assertEqual(r.state.sv_states.get("E21"), "NL_SHORT_FIXED")
        self.assertEqual(r.state.sv_states.get("C32"), "FLOAT")

    def test_squelched_is_captured(self):
        """SVs squelched after false-fix should land with their
        SQUELCHED state reflected — needed for the table's
        SQUELCHED column."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO [SV_STATE] E21: NL_SHORT_FIXED → "
            "SQUELCHED (epoch=100, elev=74°, squelch=120s, reason=...)\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.sv_states.get("E21") == "SQUELCHED")
        self.assertEqual(r.state.sv_states.get("E21"), "SQUELCHED")

    def test_non_sv_state_lines_ignored(self):
        """Lines without the [SV_STATE] tag must not leak into
        sv_states — false matches corrupt the table."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO [STATE] AntPosEst: → "
            "unsurveyed (initial)\n"
            "2026-04-21 07:00:00,001 INFO slip: sv=E21 reasons=mw_jump\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.lines_read >= 2)
        self.assertEqual(r.state.sv_states, {})

    def test_snapshot_is_safe_to_copy(self):
        """Readers will snapshot sv_states on each tick.  The
        replacement-on-write protocol must mean a copy taken *before*
        the next update remains valid after it.  If the reader mutated
        the dict in place, that contract would break."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO [SV_STATE] G05: TRACKING → "
            "FLOAT (epoch=10)\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: "G05" in r.state.sv_states)
        snap1 = r.state.sv_states
        # Append another transition.
        with self.path.open("a") as f:
            f.write(
                "2026-04-21 07:00:01,000 INFO [SV_STATE] G05: FLOAT → "
                "WL_FIXED (epoch=15)\n"
            )
        _wait_until(lambda: r.state.sv_states.get("G05") == "WL_FIXED")
        snap2 = r.state.sv_states
        # snap1 is frozen in time.  If the reader had mutated in
        # place, snap1 would now show WL_FIXED too.
        self.assertEqual(snap1.get("G05"), "FLOAT")
        self.assertEqual(snap2.get("G05"), "WL_FIXED")


if __name__ == "__main__":
    unittest.main()
