"""Unit tests for WlReAdmissionGate."""

from __future__ import annotations

import unittest

from peppar_fix.wl_readmission_gate import WlReAdmissionGate


class WlReAdmissionGateTest(unittest.TestCase):

    def test_no_flush_no_hold(self):
        """SV never flushed: gate never blocks."""
        g = WlReAdmissionGate(min_elev_delta_deg=2.0)
        self.assertFalse(g.is_blocked("G01", current_elev_deg=45.0))
        self.assertFalse(g.is_blocked("G01", current_elev_deg=None))

    def test_flush_records_elev_and_blocks(self):
        g = WlReAdmissionGate(min_elev_delta_deg=2.0)
        g.note_flush("G01", elev_deg=45.0)
        # Same elevation: blocked.
        self.assertTrue(g.is_blocked("G01", current_elev_deg=45.0))
        # Slightly moved: still blocked.
        self.assertTrue(g.is_blocked("G01", current_elev_deg=46.5))

    def test_releases_after_elev_delta(self):
        g = WlReAdmissionGate(min_elev_delta_deg=2.0)
        g.note_flush("G01", elev_deg=45.0)
        # Moved exactly 2°: released (threshold is ≥).
        self.assertFalse(g.is_blocked("G01", current_elev_deg=47.0))
        # Moved more: released.
        self.assertFalse(g.is_blocked("G01", current_elev_deg=60.0))

    def test_release_on_setting(self):
        """SV that is dropping (setting) should also release when
        it has dropped enough from flush-time elevation."""
        g = WlReAdmissionGate(min_elev_delta_deg=2.0)
        g.note_flush("G01", elev_deg=45.0)
        self.assertFalse(g.is_blocked("G01", current_elev_deg=42.0))
        self.assertFalse(g.is_blocked("G01", current_elev_deg=30.0))

    def test_none_elev_at_flush_does_not_hold(self):
        """Flush with None elev: fail safe, no hold taken."""
        g = WlReAdmissionGate(min_elev_delta_deg=2.0)
        g.note_flush("G01", elev_deg=None)
        self.assertFalse(g.is_blocked("G01", current_elev_deg=45.0))
        self.assertFalse(g.is_blocked("G01", current_elev_deg=None))
        self.assertEqual(g.n_held(), 0)

    def test_none_current_elev_does_not_block(self):
        """Held SV with no current elev data: fail safe, not blocked."""
        g = WlReAdmissionGate(min_elev_delta_deg=2.0)
        g.note_flush("G01", elev_deg=45.0)
        self.assertFalse(g.is_blocked("G01", current_elev_deg=None))

    def test_note_admitted_clears_hold(self):
        g = WlReAdmissionGate(min_elev_delta_deg=2.0)
        g.note_flush("G01", elev_deg=45.0)
        self.assertTrue(g.is_blocked("G01", current_elev_deg=45.0))
        g.note_admitted("G01")
        # No longer held.
        self.assertFalse(g.is_blocked("G01", current_elev_deg=45.0))
        self.assertEqual(g.n_held(), 0)

    def test_note_admitted_is_idempotent(self):
        """Admitting an SV that wasn't held is a no-op."""
        g = WlReAdmissionGate(min_elev_delta_deg=2.0)
        g.note_admitted("G01")  # no flush, no hold — silent
        self.assertEqual(g.n_held(), 0)

    def test_re_flush_after_admit_takes_new_hold(self):
        """Full drift → flush → release → fix → drift → flush cycle:
        the second flush takes a hold at the new elevation."""
        g = WlReAdmissionGate(min_elev_delta_deg=2.0)
        g.note_flush("G01", elev_deg=45.0)
        # Release at 50°
        self.assertFalse(g.is_blocked("G01", current_elev_deg=50.0))
        g.note_admitted("G01")
        # Drift happens again at 52° — new flush takes hold there.
        g.note_flush("G01", elev_deg=52.0)
        self.assertTrue(g.is_blocked("G01", current_elev_deg=52.0))
        # Release this time at 54.5°
        self.assertFalse(g.is_blocked("G01", current_elev_deg=54.5))

    def test_multiple_svs_independent(self):
        g = WlReAdmissionGate(min_elev_delta_deg=2.0)
        g.note_flush("G01", elev_deg=45.0)
        g.note_flush("E05", elev_deg=20.0)
        self.assertTrue(g.is_blocked("G01", current_elev_deg=45.5))
        self.assertTrue(g.is_blocked("E05", current_elev_deg=20.5))
        # G01 moves, E05 doesn't:
        self.assertFalse(g.is_blocked("G01", current_elev_deg=48.0))
        self.assertTrue(g.is_blocked("E05", current_elev_deg=20.5))
        self.assertEqual(g.n_held(), 2)

    def test_configurable_threshold(self):
        """Stricter threshold (wider wait) still works."""
        g = WlReAdmissionGate(min_elev_delta_deg=5.0)
        g.note_flush("G01", elev_deg=45.0)
        self.assertTrue(g.is_blocked("G01", current_elev_deg=48.0))
        self.assertFalse(g.is_blocked("G01", current_elev_deg=50.0))

    def test_summary(self):
        g = WlReAdmissionGate(min_elev_delta_deg=2.0)
        self.assertIn("holding 0", g.summary())
        g.note_flush("G01", elev_deg=45.0)
        g.note_flush("E05", elev_deg=20.0)
        self.assertIn("holding 2", g.summary())

    def test_held_svs_list(self):
        g = WlReAdmissionGate(min_elev_delta_deg=2.0)
        g.note_flush("G01", elev_deg=45.0)
        g.note_flush("E05", elev_deg=20.0)
        held = set(g.held_svs())
        self.assertEqual(held, {"G01", "E05"})

    def test_flush_elev_lookup(self):
        g = WlReAdmissionGate(min_elev_delta_deg=2.0)
        self.assertIsNone(g.flush_elev("G01"))
        g.note_flush("G01", elev_deg=45.0)
        self.assertAlmostEqual(g.flush_elev("G01"), 45.0)


if __name__ == "__main__":
    unittest.main()
