#!/usr/bin/env python3
"""Unit tests for CycleSlipMonitor, MWTracker.detect_jump, and
flush_sv_phase.

Each detector is exercised in isolation with synthetic observations,
then cross-checked against the flush contract: after flush_sv_phase
runs, every per-SV phase-like field is gone and every shared /
frequency-like field is untouched.

Run: python3 -m unittest tests.test_cycle_slip
"""

import sys
import unittest
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from ppp_ar import MelbourneWubbenaTracker, NarrowLaneResolver, C
from peppar_fix.cycle_slip import (
    ARC_GAP_MAX_S,
    GF_JUMP_THRESHOLD_M,
    LOCKTIME_DROP_MS,
    MW_JUMP_N_SIGMA,
    CycleSlipMonitor,
    SlipEvent,
    flush_sv_phase,
)


# ── helpers ──────────────────────────────────────────────────────── #

# GPS L1 / L5 frequencies, wavelengths
_F_L1 = 1_575.42e6
_F_L5 = 1_176.45e6
_WL_L1 = C / _F_L1
_WL_L5 = C / _F_L5


def make_obs(sv='G01', lock_ms=5000.0, cno=42.0,
             phi1_cyc=100_000.0, phi2_cyc=95_000.0,
             pr1_m=22_000_000.0, pr2_m=22_000_000.0,
             wl_f1=_WL_L1, wl_f2=_WL_L5):
    """Build a minimal observation dict matching the realtime_ppp format."""
    return {
        'sv': sv,
        'sys': 'gps',
        'pr_if': pr1_m,                          # not used by monitor
        'phi_if_m': phi1_cyc * wl_f1,            # not used by monitor
        'cno': cno,
        'lock_duration_ms': lock_ms,
        'half_cyc_ok': True,
        'phi1_cyc': phi1_cyc,
        'phi2_cyc': phi2_cyc,
        'pr1_m': pr1_m,
        'pr2_m': pr2_m,
        'wl_f1': wl_f1,
        'wl_f2': wl_f2,
    }


# ── CycleSlipMonitor detectors ───────────────────────────────────── #

class TestUbxLocktimeDrop(unittest.TestCase):

    def test_clean_run_no_slip(self):
        mon = CycleSlipMonitor()
        t = 1000.0
        for i in range(5):
            mon.check([make_obs(lock_ms=1000 + i * 1000)], t, i)
            t += 1.0
        # Sixth epoch still rising — no slip.
        events = mon.check([make_obs(lock_ms=7000)], t, 5)
        self.assertEqual(events, [])

    def test_drop_triggers(self):
        mon = CycleSlipMonitor()
        mon.check([make_obs(lock_ms=30_000)], 1000.0, 0)
        events = mon.check(
            [make_obs(lock_ms=30_000 - (LOCKTIME_DROP_MS + 100))],
            1001.0, 1)
        self.assertEqual(len(events), 1)
        self.assertIn('ubx_locktime_drop', events[0].reasons)

    def test_drop_ignored_above_cap(self):
        """u-blox holds locktime at ~64s; below-cap check must skip."""
        mon = CycleSlipMonitor()
        mon.check([make_obs(lock_ms=70_000)], 1000.0, 0)
        # Apparent "drop" above the cap is artefact of capping, not slip.
        events = mon.check([make_obs(lock_ms=65_000)], 1001.0, 1)
        self.assertEqual(events, [])


class TestArcGap(unittest.TestCase):

    def test_single_epoch_no_gap_no_slip(self):
        mon = CycleSlipMonitor()
        mon.check([make_obs(lock_ms=10_000)], 1000.0, 0)
        events = mon.check([make_obs(lock_ms=11_000)], 1001.0, 1)
        self.assertEqual(events, [])

    def test_gap_with_fresh_lock_triggers(self):
        """SV reappears after a gap with a small locktime — the tracking
        arc restarted, ambiguity must be flushed."""
        mon = CycleSlipMonitor()
        mon.check([make_obs(lock_ms=10_000)], 1000.0, 0)
        events = mon.check([make_obs(lock_ms=500)],
                           1000.0 + ARC_GAP_MAX_S + 2.0, 1)
        self.assertEqual(len(events), 1)
        self.assertIn('arc_gap', events[0].reasons)

    def test_gap_with_sustained_lock_no_slip(self):
        """SV observation missing for several epochs but locktime_ms
        shows the carrier was locked through the gap — upstream filtering
        churn (half_cyc transient, missing SSR bias), not a real arc
        restart.  This is the pattern seen on day0418 where 273/273
        slips had lock_ms=64500 despite multi-second gaps."""
        mon = CycleSlipMonitor()
        mon.check([make_obs(lock_ms=60_000)], 1000.0, 0)
        events = mon.check([make_obs(lock_ms=64_000)],
                           1000.0 + ARC_GAP_MAX_S + 3.0, 1)
        if events:
            self.assertNotIn('arc_gap', events[0].reasons)


class TestGeometryFreeJump(unittest.TestCase):

    def test_clean_phases_no_jump(self):
        mon = CycleSlipMonitor()
        mon.check([make_obs(phi1_cyc=100_000.0, phi2_cyc=95_000.0,
                            lock_ms=10_000)], 1000.0, 0)
        # Small consistent increment on both — GF delta near zero.
        events = mon.check(
            [make_obs(phi1_cyc=100_010.0, phi2_cyc=95_007.47,
                      lock_ms=11_000)], 1001.0, 1)
        self.assertEqual(events, [])

    def test_half_cycle_jump_on_L5_triggers(self):
        mon = CycleSlipMonitor()
        mon.check([make_obs(phi1_cyc=100_000.0, phi2_cyc=95_000.0,
                            lock_ms=10_000)], 1000.0, 0)
        # Inject a 1-cycle L5 jump without touching L1: GF jumps by L5 λ.
        events = mon.check(
            [make_obs(phi1_cyc=100_000.0, phi2_cyc=95_001.0,
                      lock_ms=11_000)], 1001.0, 1)
        self.assertEqual(len(events), 1)
        self.assertIn('gf_jump', events[0].reasons)
        self.assertAlmostEqual(events[0].gf_jump_m, -_WL_L5, places=4)


class TestMwJump(unittest.TestCase):
    """MW detector requires a warmed-up MelbourneWubbenaTracker.

    MW = λ_WL·(Φ1 − Φ2) − (f1·PR1 + f2·PR2)/(f1+f2) is geometry-free:
    advancing phase and pseudorange consistently with range drift leaves
    MW constant.  For a clean synthetic warm-up, the cheapest trick is to
    hold observations static — same phase, same PR — which makes MW
    exactly constant and the residual std floor to _SIGMA_FLOOR_CYC.
    """

    PHI1 = 100_000.0
    PHI2 = 95_000.0
    PR1 = 22_000_000.0
    PR2 = 22_000_000.0

    def _warm_mw(self, mw, sv='G01', n_epochs=30):
        for _ in range(n_epochs):
            mw.update(sv, self.PHI1, self.PHI2, self.PR1, self.PR2,
                      _F_L1, _F_L5)

    def test_clean_mw_no_jump(self):
        mw = MelbourneWubbenaTracker()
        self._warm_mw(mw)
        obs = make_obs(phi1_cyc=self.PHI1, phi2_cyc=self.PHI2,
                       pr1_m=self.PR1, pr2_m=self.PR2)
        info = mw.detect_jump(obs)
        self.assertIsNotNone(info)
        self.assertFalse(info['is_slip'])

    def test_l1_cycle_jump_triggers_mw(self):
        mw = MelbourneWubbenaTracker()
        self._warm_mw(mw)
        # +1 cycle on L1 only — MW jumps by ~λ_WL/λ_NL cycles, well past
        # the sigma floor.
        obs = make_obs(phi1_cyc=self.PHI1 + 1.0, phi2_cyc=self.PHI2,
                       pr1_m=self.PR1, pr2_m=self.PR2)
        info = mw.detect_jump(obs)
        self.assertIsNotNone(info)
        self.assertTrue(info['is_slip'])
        self.assertGreater(abs(info['delta_cyc']), MW_JUMP_N_SIGMA * 0.2)

    def test_monitor_reports_mw_jump(self):
        mw = MelbourneWubbenaTracker()
        self._warm_mw(mw)
        mon = CycleSlipMonitor(mw_tracker=mw)
        # First epoch is clean (matches warming observations).
        events = mon.check(
            [make_obs(lock_ms=30_000,
                      phi1_cyc=self.PHI1, phi2_cyc=self.PHI2,
                      pr1_m=self.PR1, pr2_m=self.PR2)],
            1000.0, 0)
        self.assertEqual(events, [])
        # Second epoch — inject L1 slip.
        events = mon.check(
            [make_obs(lock_ms=30_500,
                      phi1_cyc=self.PHI1 + 1.0, phi2_cyc=self.PHI2,
                      pr1_m=self.PR1, pr2_m=self.PR2)],
            1001.0, 1)
        self.assertEqual(len(events), 1)
        self.assertIn('mw_jump', events[0].reasons)


class TestMultiDetectorConfidence(unittest.TestCase):

    def test_high_confidence_on_two_detectors(self):
        mon = CycleSlipMonitor()
        # Clean prior epoch with a normal locktime.
        mon.check([make_obs(lock_ms=30_000,
                            phi1_cyc=100_000.0, phi2_cyc=95_000.0)],
                  1000.0, 0)
        # Next epoch within ARC_GAP_MAX_S but with BOTH a big locktime
        # drop AND an L5 phase jump → detectors 1 and 3 fire.
        events = mon.check([make_obs(lock_ms=500,
                                     phi1_cyc=100_010.0,
                                     phi2_cyc=95_001.0)],
                           1001.0, 1)
        self.assertEqual(len(events), 1)
        self.assertGreaterEqual(len(events[0].reasons), 2)
        self.assertEqual(events[0].confidence, 'HIGH')

    def test_low_confidence_on_one_detector(self):
        mon = CycleSlipMonitor()
        mon.check([make_obs(lock_ms=30_000)], 1000.0, 0)
        events = mon.check([make_obs(lock_ms=500)], 1001.0, 1)
        self.assertEqual(events[0].confidence, 'LOW')


# ── flush_sv_phase contract ──────────────────────────────────────── #

class _FakePPPFilter:
    """Just enough of PPPFilter for flush_sv_phase to exercise."""

    def __init__(self, sv_list):
        self.sv_to_idx = {sv: i for i, sv in enumerate(sv_list)}
        # Shared / frequency-like state that MUST survive.
        self.clock_state = 0.123       # receiver clock (shared)
        self.isb_gal = 0.456           # ISB (shared)
        self.ztd = 2.3                 # ZTD (shared)
        self.prev_obs = {sv: {'phi_if_m': 1.0} for sv in sv_list}
        self._removed = []

    def remove_ambiguity(self, sv):
        self._removed.append(sv)
        self.sv_to_idx.pop(sv, None)


class _FakePfr:
    def __init__(self, sv_list):
        self._per_sv = {sv: deque([1.0, 2.0]) for sv in sv_list}
        self._per_sv_phi = {sv: deque([0.1]) for sv in sv_list}
        self._per_sv_last_elev = {sv: 45.0 for sv in sv_list}


class TestFlushContract(unittest.TestCase):

    def test_flush_removes_only_phase_state(self):
        filt = _FakePPPFilter(['G01', 'G02', 'G03'])
        mw = MelbourneWubbenaTracker()
        mw.update('G01', 100_000.0, 95_000.0, 22e6, 22e6, _F_L1, _F_L5)
        mw.update('G02', 110_000.0, 105_000.0, 23e6, 23e6, _F_L1, _F_L5)
        nl = NarrowLaneResolver()
        nl._fixed['G01'] = {'n1': 42, 'a_if_fixed': 5.0}
        nl._fixed['G02'] = {'n1': 17, 'a_if_fixed': 3.0}
        pfr = _FakePfr(['G01', 'G02', 'G03'])
        mon = CycleSlipMonitor(mw_tracker=mw)
        # Seed monitor memory for G01 then flush.
        mon.check([make_obs(sv='G01')], 1000.0, 0)

        flush_sv_phase('G01', filt=filt, mw_tracker=mw, nl_resolver=nl,
                       pfr_monitor=pfr, slip_monitor=mon,
                       reason='test', epoch=42)

        # PHASE-LIKE state for G01: gone.
        self.assertNotIn('G01', filt.sv_to_idx)
        self.assertNotIn('G01', filt.prev_obs)
        self.assertNotIn('G01', mw._state)
        self.assertNotIn('G01', nl._fixed)
        self.assertNotIn('G01', pfr._per_sv)
        self.assertNotIn('G01', pfr._per_sv_phi)
        self.assertNotIn('G01', pfr._per_sv_last_elev)
        self.assertNotIn('G01', mon._prev)

        # PHASE-LIKE state for G02/G03: untouched.
        self.assertIn('G02', filt.sv_to_idx)
        self.assertIn('G03', filt.sv_to_idx)
        self.assertIn('G02', mw._state)
        self.assertIn('G02', nl._fixed)
        self.assertIn('G02', pfr._per_sv)

        # Shared / frequency-like receiver state: untouched.
        self.assertEqual(filt.clock_state, 0.123)
        self.assertEqual(filt.isb_gal, 0.456)
        self.assertEqual(filt.ztd, 2.3)


class TestStalePrune(unittest.TestCase):

    def test_sv_memory_pruned_after_stale_window(self):
        mon = CycleSlipMonitor(stale_after_s=10.0)
        mon.check([make_obs(sv='G01')], 1000.0, 0)
        # Next observation from a different SV, far enough in the future
        # that G01 is stale.
        mon.check([make_obs(sv='G02')], 1100.0, 1)
        self.assertNotIn('G01', mon._prev)
        self.assertIn('G02', mon._prev)


if __name__ == '__main__':
    unittest.main()
