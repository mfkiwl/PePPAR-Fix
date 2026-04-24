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
        """`[STATE] AntPosEst: → surveying (initial)` seeds the state."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO [STATE] AntPosEst: → "
            "surveying (initial)\n"
            "2026-04-21 07:00:00,001 INFO [STATE] DOFreqEst: → "
            "uninitialized (initial)\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(
            lambda: r.state.ant_pos_est_state and r.state.do_freq_est_state
        )
        self.assertEqual(r.state.ant_pos_est_state, "surveying")
        self.assertEqual(r.state.do_freq_est_state, "uninitialized")
        self.assertEqual(r.state.ant_pos_est_visited, ("surveying",))
        self.assertEqual(r.state.do_freq_est_visited, ("uninitialized",))

    def test_transition_line_updates_current_and_visited(self):
        """A transition appends to visited, sets current to the new state."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO [STATE] AntPosEst: → "
            "surveying (initial)\n"
            "2026-04-21 07:05:00,000 INFO [STATE] AntPosEst: surveying → "
            "verifying after 300s\n"
            "2026-04-21 07:06:00,000 INFO [STATE] AntPosEst: verifying → "
            "converging after 60s\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.ant_pos_est_state == "converging")
        self.assertEqual(r.state.ant_pos_est_state, "converging")
        self.assertEqual(
            r.state.ant_pos_est_visited,
            ("surveying", "verifying", "converging"),
        )

    def test_revisiting_state_doesnt_dup_visited(self):
        """When the machine flaps back to a state it's been in before,
        visited must not contain duplicates — the set-like guarantee
        matters for the widget's rendering."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO [STATE] AntPosEst: → "
            "converging (initial)\n"
            "2026-04-21 07:05:00,000 INFO [STATE] AntPosEst: converging → "
            "anchored after 300s\n"
            "2026-04-21 07:06:00,000 INFO [STATE] AntPosEst: anchored → "
            "converging after 60s\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.ant_pos_est_state == "converging" and
                    "anchored" in r.state.ant_pos_est_visited)
        self.assertEqual(r.state.ant_pos_est_state, "converging")
        self.assertEqual(
            r.state.ant_pos_est_visited, ("converging", "anchored"),
        )

    def test_machines_are_independent(self):
        """Updating one machine's state leaves the other alone."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO [STATE] AntPosEst: → "
            "surveying (initial)\n"
            "2026-04-21 07:00:00,001 INFO [STATE] DOFreqEst: → "
            "uninitialized (initial)\n"
            "2026-04-21 07:05:00,000 INFO [STATE] DOFreqEst: "
            "uninitialized → phase_setting after 300s\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.do_freq_est_state == "phase_setting")
        self.assertEqual(r.state.ant_pos_est_state, "surveying")
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


class AntPosEstLineTest(unittest.TestCase):
    """Parsing position + σ + nav2Δ from ``[AntPosEst N]``
    lines.  The widget relies on these fields landing in LogState
    on every epoch — a silent parser failure would freeze the
    position display at its last value."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = Path(self._tmpdir.name) / "engine.log"

    def test_typical_line_extracts_all_fields(self):
        self.path.write_text(
            "2026-04-21 17:48:07,703 INFO   [AntPosEst 4210] "
            "positionσ=0.023m pos=(40.123456, -90.123456, 198.2) n=12 amb=12 "
            "WL: 12/17 fixed NL: 3 fixed R=5.4 P=0.997 nav2Δ=2.7m "
            "ZTD=+274±3mm worstσ=1.5m\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.antenna_position is not None)
        self.assertEqual(r.state.antenna_sigma_m, 0.023)
        self.assertEqual(
            r.state.antenna_position, (40.123456, -90.123456, 198.2),
        )
        self.assertEqual(r.state.nav2_delta_m, 2.7)
        self.assertEqual(r.state.worst_sigma_m, 1.5)
        self.assertAlmostEqual(r.state.ztd_m, 0.274, places=6)
        self.assertEqual(r.state.ztd_sigma_mm, 3)

    def test_earth_tide_fields(self):
        """``tide=<mm>mm(U±<mm>)`` captures total magnitude and the
        vertical component.  Signed U — can be negative at certain
        latitudes/epochs."""
        self.path.write_text(
            "2026-04-21 17:48:07,703 INFO   [AntPosEst 10] "
            "positionσ=1.5m pos=(40.0, -90.0, 200.0) "
            "ZTD=+0±500mm tide=135mm(U+131) worstσ=1000.0m\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.earth_tide_mm is not None)
        self.assertEqual(r.state.earth_tide_mm, 135)
        self.assertEqual(r.state.earth_tide_u_mm, 131)

    def test_earth_tide_negative_u(self):
        """U can be negative (outbound tide pulling antenna down)."""
        self.path.write_text(
            "2026-04-21 17:48:07,703 INFO   [AntPosEst 10] "
            "positionσ=1.5m pos=(40.0, -90.0, 200.0) "
            "tide=87mm(U-42)\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.earth_tide_mm is not None)
        self.assertEqual(r.state.earth_tide_mm, 87)
        self.assertEqual(r.state.earth_tide_u_mm, -42)

    def test_signed_ztd_and_missing_sigma(self):
        """ZTD can be negative and the ±sigma is optional — older
        engine versions emitted bare ``ZTD=<mm>mm``."""
        self.path.write_text(
            "2026-04-21 17:48:07,703 INFO   [AntPosEst 10] "
            "positionσ=2.0m pos=(40.0, -90.0, 200.0) "
            "ZTD=-2850mm worstσ=1000.0m\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.antenna_position is not None)
        self.assertAlmostEqual(r.state.ztd_m, -2.850, places=6)
        self.assertIsNone(r.state.ztd_sigma_mm)
        self.assertEqual(r.state.worst_sigma_m, 1000.0)

    def test_stream_identifiers_captured(self):
        """NTRIP mount names from the engine startup banner feed
        LogState.eph_mount / ssr_mount."""
        self.path.write_text(
            "2026-04-23 20:11:18,257 INFO Ephemeris stream: "
            "ntrip.data.gnss.ga.gov.au:443/BCEP00BKG0\n"
            "2026-04-23 20:11:18,257 INFO SSR stream: "
            "products.igs-ip.net:443/SSRA00CNE0\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.eph_mount is not None
                    and r.state.ssr_mount is not None)
        self.assertEqual(r.state.eph_mount, "BCEP00BKG0")
        self.assertEqual(r.state.ssr_mount, "SSRA00CNE0")

    def test_negative_altitude_parses(self):
        """Engine has been briefly seen emitting negative altitudes
        during a filter glitch.  Regex must accept them."""
        self.path.write_text(
            "2026-04-21 17:48:07,703 INFO   [AntPosEst 4210] "
            "positionσ=1.500m pos=(40.123456, -90.123456, -5.8) n=12 amb=12\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.antenna_position is not None)
        self.assertEqual(r.state.antenna_position[2], -5.8)

    def test_line_without_nav2_still_parses(self):
        """Early bootstrap lines may not carry nav2Δ (no NAV2
        fix yet).  Position + σ must still land; nav2_delta_m
        stays None."""
        self.path.write_text(
            "2026-04-21 17:48:07,703 INFO   [AntPosEst 10] "
            "positionσ=2.356m pos=(40.123400, -90.123500, 201.3) n=11 amb=14 "
            "WL: 0/15 fixed NL: 0 fixed\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.antenna_position is not None)
        self.assertEqual(r.state.antenna_sigma_m, 2.356)
        self.assertIsNone(r.state.nav2_delta_m)

    def test_latest_line_wins(self):
        """When multiple [AntPosEst] lines appear, the latest
        fields win — the widget always shows the most recent
        position, never a stale frame."""
        self.path.write_text(
            "2026-04-21 17:48:00,000 INFO   [AntPosEst 10] "
            "positionσ=0.500m pos=(40.12, -90.12, 195.0)\n"
            "2026-04-21 17:48:10,000 INFO   [AntPosEst 20] "
            "positionσ=0.023m pos=(40.123456, -90.123456, 198.2)\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(
            lambda: r.state.antenna_position
            and r.state.antenna_position[2] > 196,
        )
        self.assertEqual(r.state.antenna_sigma_m, 0.023)
        self.assertEqual(r.state.antenna_position[2], 198.2)

    def test_widened_precision_is_preserved(self):
        """When main lands the precision ask (8 decimals on
        lat/lon, 3 on altitude), the parser must preserve all of
        those digits — a regex that accidentally truncated would
        defeat the point of asking for more precision."""
        self.path.write_text(
            "2026-04-21 17:48:07,703 INFO   [AntPosEst 4210] "
            "positionσ=0.023m pos=(40.12345678, -90.12345678, 198.247)\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.antenna_position is not None)
        self.assertAlmostEqual(
            r.state.antenna_position[0], 40.12345678, places=8,
        )
        self.assertAlmostEqual(
            r.state.antenna_position[1], -90.12345678, places=8,
        )
        self.assertAlmostEqual(
            r.state.antenna_position[2], 198.247, places=3,
        )


class PhaseBiasCapabilityTest(unittest.TestCase):
    """``Phase bias lookup`` lines feed nl_capable_constellations.

    Matches the engine-side signal that distinguishes architecturally-
    reachable NL states from merely-empty ones: a HIT-HIT lookup for
    any SV of a constellation proves the correction stream has the
    phase-bias pair needed to reach NL.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = Path(self._tmpdir.name) / "engine.log"

    def test_both_hit_latches_constellation(self):
        """HIT on f1 AND f2 for any GAL SV → GAL NL-capable."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO Phase bias lookup: E34 "
            "f1=GAL-E1C→('C1C', 'L1C')(HIT) f2=GAL-E5bQ→('C7Q', 'L7Q')"
            "(HIT) avail=['L1C', 'L5Q', 'L7Q']\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: "E" in r.state.nl_capable_constellations)
        self.assertIn("E", r.state.nl_capable_constellations)

    def test_miss_on_f2_keeps_constellation_out(self):
        """GPS f1=HIT, f2=MISS (ptpmon + CNES reality) → GPS stays
        out of nl_capable_constellations across the whole run."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO Phase bias lookup: G24 "
            "f1=GPS-L1CA→('C1C', 'L1C')(HIT) f2=GPS-L2CL→('C2L', 'L2L')"
            "(MISS) avail=['L1C', 'L2W', 'L5I']\n"
            "2026-04-21 07:00:01,000 INFO Phase bias lookup: G12 "
            "f1=GPS-L1CA→('C1C', 'L1C')(HIT) f2=GPS-L2CL→('C2L', 'L2L')"
            "(MISS) avail=['L1C', 'L2W']\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.lines_read >= 2)
        self.assertNotIn("G", r.state.nl_capable_constellations)

    def test_one_hit_hit_sv_is_enough(self):
        """Capability latches on the first HIT-HIT for any SV of a
        constellation; subsequent MISS-containing lookups for other
        SVs of the same constellation don't downgrade."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO Phase bias lookup: E34 "
            "f1=GAL-E1C→('C1C', 'L1C')(HIT) f2=GAL-E5bQ→('C7Q', 'L7Q')"
            "(HIT) avail=['L1C', 'L7Q']\n"
            # Later SV with a MISS — shouldn't un-latch E.
            "2026-04-21 07:00:01,000 INFO Phase bias lookup: E99 "
            "f1=GAL-E1C→('C1C', 'L1C')(HIT) f2=GAL-E5bQ→('C7Q', 'L7Q')"
            "(MISS) avail=[]\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: "E" in r.state.nl_capable_constellations)
        self.assertIn("E", r.state.nl_capable_constellations)

    def test_multiple_constellations_independent(self):
        """GAL NL-capable doesn't make GPS NL-capable."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO Phase bias lookup: E34 "
            "f1=GAL-E1C→('C1C', 'L1C')(HIT) f2=GAL-E5bQ→('C7Q', 'L7Q')"
            "(HIT) avail=[]\n"
            "2026-04-21 07:00:01,000 INFO Phase bias lookup: G24 "
            "f1=GPS-L1CA→('C1C', 'L1C')(HIT) f2=GPS-L2CL→('C2L', 'L2L')"
            "(MISS) avail=[]\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: "E" in r.state.nl_capable_constellations)
        self.assertEqual(r.state.nl_capable_constellations, frozenset({"E"}))


class SvStateParsingTest(unittest.TestCase):
    """[SV_STATE] transitions feed the per-SV state dict."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = Path(self._tmpdir.name) / "engine.log"

    def test_transition_updates_sv_state(self):
        """Basic case: one SV transitions TRACKING → FLOATING → CONVERGING,
        final value in sv_states matches the last transition."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO [SV_STATE] G05: TRACKING → "
            "FLOATING (epoch=10)\n"
            "2026-04-21 07:00:30,000 INFO [SV_STATE] G05: FLOATING → "
            "CONVERGING (epoch=40)\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.sv_states.get("G05") == "CONVERGING")
        self.assertEqual(r.state.sv_states.get("G05"), "CONVERGING")

    def test_multi_sv_independent_tracking(self):
        """Concurrent SVs: each tracked independently, no cross-talk."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO [SV_STATE] G05: TRACKING → "
            "FLOATING (epoch=10)\n"
            "2026-04-21 07:00:01,000 INFO [SV_STATE] E21: TRACKING → "
            "FLOATING (epoch=10)\n"
            "2026-04-21 07:00:02,000 INFO [SV_STATE] C32: TRACKING → "
            "FLOATING (epoch=10)\n"
            "2026-04-21 07:00:03,000 INFO [SV_STATE] E21: FLOATING → "
            "CONVERGING (epoch=15)\n"
            "2026-04-21 07:00:04,000 INFO [SV_STATE] E21: CONVERGING → "
            "ANCHORING (epoch=20)\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(
            lambda: r.state.sv_states.get("E21") == "ANCHORING",
        )
        self.assertEqual(r.state.sv_states.get("G05"), "FLOATING")
        self.assertEqual(r.state.sv_states.get("E21"), "ANCHORING")
        self.assertEqual(r.state.sv_states.get("C32"), "FLOATING")

    def test_squelched_is_captured(self):
        """SVs squelched after false-fix should land with their
        WAITING state reflected — needed for the table's
        WAITING column."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO [SV_STATE] E21: ANCHORING → "
            "WAITING (epoch=100, elev=74°, squelch=120s, reason=...)\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: r.state.sv_states.get("E21") == "WAITING")
        self.assertEqual(r.state.sv_states.get("E21"), "WAITING")

    def test_non_sv_state_lines_ignored(self):
        """Lines without the [SV_STATE] tag must not leak into
        sv_states — false matches corrupt the table."""
        self.path.write_text(
            "2026-04-21 07:00:00,000 INFO [STATE] AntPosEst: → "
            "surveying (initial)\n"
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
            "FLOATING (epoch=10)\n"
        )
        r = LogReader(self.path); r.start(); self.addCleanup(r.stop)
        _wait_until(lambda: "G05" in r.state.sv_states)
        snap1 = r.state.sv_states
        # Append another transition.
        with self.path.open("a") as f:
            f.write(
                "2026-04-21 07:00:01,000 INFO [SV_STATE] G05: FLOATING → "
                "CONVERGING (epoch=15)\n"
            )
        _wait_until(lambda: r.state.sv_states.get("G05") == "CONVERGING")
        snap2 = r.state.sv_states
        # snap1 is frozen in time.  If the reader had mutated in
        # place, snap1 would now show CONVERGING too.
        self.assertEqual(snap1.get("G05"), "FLOATING")
        self.assertEqual(snap2.get("G05"), "CONVERGING")


if __name__ == "__main__":
    unittest.main()
