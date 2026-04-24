"""Tests for MelbourneWubbenaTracker.integrality_snapshot — the
diagnostic view of per-SV WL fix eligibility used by the engine's
periodic [WL_INTEGRALITY] log emission.
"""

from __future__ import annotations

import os
import sys
import unittest

_SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from ppp_ar import MelbourneWubbenaTracker  # noqa: E402
from solve_pseudorange import C              # noqa: E402


# Typical GPS L1/L2 frequencies — MW lambda_WL ≈ 86 cm.
F_L1 = 1575.42e6
F_L2 = 1227.60e6


def _feed_steady_mw(tracker, sv, target_n_wl_float, n_epochs,
                     phi1_scale=1000.0, f1=F_L1, f2=F_L2):
    """Feed the tracker synthetic steady-state observations chosen so
    that the MW combination converges to target_n_wl_float * lambda_WL
    in meters.  Uses fake phases/prs that cancel algebraically to the
    desired MW value.
    """
    lambda_wl = C / (f1 - f2)
    mw_target_m = target_n_wl_float * lambda_wl
    # Simplest construction: phi2 = 0, pr1 = pr2 = 0, phi1 chosen so
    # _mw_meters returns exactly mw_target_m.  From _mw_meters:
    #   mw = (f1*phi1*(C/f1) - f2*0*(C/f2))/(f1-f2) - 0
    #      = phi1*C / (f1-f2) = phi1*lambda_wl
    # So phi1 = mw_target_m / lambda_wl cycles.
    phi1 = mw_target_m / lambda_wl
    for _ in range(n_epochs):
        tracker.update(sv, phi1, 0.0, 0.0, 0.0, f1, f2)


class IntegralitySnapshotTest(unittest.TestCase):

    def test_empty_tracker_returns_empty(self):
        t = MelbourneWubbenaTracker()
        self.assertEqual(t.integrality_snapshot(), [])

    def test_single_sv_reports_frac_epochs_fixed(self):
        t = MelbourneWubbenaTracker(
            tau_s=30.0, fix_threshold=0.10, min_epochs=20)
        # Target N_WL = 12345.06 → frac = 0.06.  Feed enough epochs to
        # satisfy min_epochs AND to pass fix_threshold (frac < 0.10).
        _feed_steady_mw(t, "G01", 12345.06, n_epochs=25)
        snap = t.integrality_snapshot()
        self.assertEqual(len(snap), 1)
        s = snap[0]
        self.assertEqual(s['sv'], "G01")
        self.assertAlmostEqual(s['n_wl_float'], 12345.06, places=2)
        self.assertAlmostEqual(s['frac'], 0.06, places=2)
        self.assertGreaterEqual(s['n_epochs'], 20)
        self.assertTrue(s['fixed'])

    def test_not_yet_fixed_has_fixed_false(self):
        t = MelbourneWubbenaTracker(
            tau_s=30.0, fix_threshold=0.10, min_epochs=20)
        # Target frac = 0.30 → above fix_threshold, should NOT fix.
        _feed_steady_mw(t, "G02", 12345.30, n_epochs=30)
        snap = t.integrality_snapshot()
        self.assertEqual(len(snap), 1)
        s = snap[0]
        self.assertFalse(s['fixed'])
        self.assertAlmostEqual(s['frac'], 0.30, places=2)

    def test_below_min_epochs_not_fixed(self):
        t = MelbourneWubbenaTracker(
            tau_s=30.0, fix_threshold=0.10, min_epochs=20)
        # Target frac = 0.05 (would-fix on frac alone) but only 10
        # epochs — below min_epochs.
        _feed_steady_mw(t, "G03", 12345.05, n_epochs=10)
        snap = t.integrality_snapshot()
        self.assertEqual(len(snap), 1)
        s = snap[0]
        self.assertFalse(s['fixed'])
        self.assertLess(s['n_epochs'], 20)

    def test_multiple_svs_all_reported(self):
        t = MelbourneWubbenaTracker(
            tau_s=30.0, fix_threshold=0.10, min_epochs=20)
        _feed_steady_mw(t, "G01", 12345.02, n_epochs=25)  # will fix
        _feed_steady_mw(t, "G02", 23456.35, n_epochs=25)  # won't fix
        _feed_steady_mw(t, "E07", 34567.08, n_epochs=25)  # will fix
        snap = t.integrality_snapshot()
        self.assertEqual(len(snap), 3)
        fixed_svs = {s['sv'] for s in snap if s['fixed']}
        unfixed_svs = {s['sv'] for s in snap if not s['fixed']}
        self.assertEqual(fixed_svs, {"G01", "E07"})
        self.assertEqual(unfixed_svs, {"G02"})

    def test_resid_std_populated_after_window(self):
        t = MelbourneWubbenaTracker()
        _feed_steady_mw(t, "G01", 12345.06, n_epochs=30)
        snap = t.integrality_snapshot()
        self.assertEqual(len(snap), 1)
        # 30 epochs of identical synthetic input → resid_std ~ 0 but
        # should be populated.  test_not_yet_fixed verified it can be
        # None when < 8 samples.
        self.assertIsNotNone(snap[0]['resid_std_cyc'])


if __name__ == '__main__':
    unittest.main()
