"""Tests for FixSetIntegrityMonitor's fleet-consensus triggers
(Part 2 of docs/fleet-consensus-monitors.md).

Covers pos_consensus and ztd_consensus: trip on sustained delta,
silent skip on None, timer reset on delta drop, record_trip clears
both latches.  Kept standalone — doesn't import the engine, so
runs without pyserial.
"""

from __future__ import annotations

import os
import sys
import unittest
from dataclasses import dataclass, field

_SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from peppar_fix.fix_set_integrity_monitor import FixSetIntegrityMonitor  # noqa: E402


class _StubTracker:
    """Minimal SvStateTracker shim — the consensus paths don't
    touch tracker state."""

    def anchored_svs(self):
        return []

    def all_records(self):
        return []

    def state(self, sv):
        return None


@dataclass
class _StubApe:
    reached_anchored: bool = False
    _clear_count: int = 0

    def clear_latches(self, reason=""):
        self._clear_count += 1


def _monitor(**kwargs):
    """Build a monitor with sensible test defaults and the given overrides."""
    defaults = dict(
        rms_threshold_m=5.0,
        window_epochs=30,
        min_samples_in_window=10,
        eval_every=1,          # evaluate every epoch for deterministic tests
        cooldown_epochs=1,     # no cooldown between trips
        suppress_if_monitors_fired_within=0,
        anchor_collapse_epochs=60,
        pos_consensus_threshold_m=0.20,
        pos_consensus_sustained_epochs=30,
        ztd_consensus_threshold_m=0.10,
        ztd_consensus_sustained_epochs=60,
    )
    defaults.update(kwargs)
    return FixSetIntegrityMonitor(
        _StubTracker(), ape_state_machine=_StubApe(), **defaults,
    )


class PosConsensusTest(unittest.TestCase):

    def test_trip_after_sustained_delta(self):
        m = _monitor()
        # Feed delta = 0.5 m (well above 0.20 threshold) for the full
        # sustained window.  First epoch starts the timer; trip on the
        # epoch sustained_epochs later.
        ev = None
        for epoch in range(40):
            ev = m.evaluate(epoch, pos_consensus_delta_m=0.5)
            if ev is not None:
                break
        self.assertIsNotNone(ev)
        self.assertEqual(ev['reason'], 'pos_consensus')
        self.assertAlmostEqual(ev['delta_m'], 0.5)
        self.assertAlmostEqual(ev['threshold_m'], 0.20)
        self.assertGreaterEqual(ev['sustained_epochs'], 30)

    def test_no_trip_below_threshold(self):
        m = _monitor()
        for epoch in range(100):
            ev = m.evaluate(epoch, pos_consensus_delta_m=0.05)
            self.assertIsNone(ev)

    def test_timer_resets_on_dip(self):
        m = _monitor()
        # Above threshold for 20 epochs, dip below for one, above again.
        for epoch in range(20):
            self.assertIsNone(m.evaluate(epoch, pos_consensus_delta_m=0.5))
        # Dip: timer must reset.
        self.assertIsNone(m.evaluate(20, pos_consensus_delta_m=0.01))
        # Back above — must need 30 more epochs, not 10.
        for epoch in range(21, 51):
            ev = m.evaluate(epoch, pos_consensus_delta_m=0.5)
            self.assertIsNone(ev, f"premature trip at epoch {epoch}")
        # Exactly sustained_epochs=30 after re-arm → trip.
        ev = m.evaluate(51, pos_consensus_delta_m=0.5)
        self.assertIsNotNone(ev)

    def test_none_delta_silently_skipped(self):
        m = _monitor()
        for epoch in range(200):
            self.assertIsNone(m.evaluate(epoch, pos_consensus_delta_m=None))


class ZtdConsensusTest(unittest.TestCase):

    def test_trip_uses_absolute_value(self):
        m = _monitor()
        # Negative delta below −threshold must also trip.
        ev = None
        for epoch in range(70):
            ev = m.evaluate(epoch, ztd_consensus_delta_m=-0.20)
            if ev is not None:
                break
        self.assertIsNotNone(ev)
        self.assertEqual(ev['reason'], 'ztd_consensus')
        self.assertAlmostEqual(ev['delta_m'], -0.20)
        self.assertGreaterEqual(ev['sustained_epochs'], 60)

    def test_no_trip_below_threshold(self):
        m = _monitor()
        for epoch in range(200):
            ev = m.evaluate(epoch, ztd_consensus_delta_m=0.05)
            self.assertIsNone(ev)

    def test_none_delta_silently_skipped(self):
        m = _monitor()
        for epoch in range(200):
            self.assertIsNone(m.evaluate(epoch, ztd_consensus_delta_m=None))


class RecordTripClearsConsensusTest(unittest.TestCase):

    def test_both_since_timers_cleared(self):
        m = _monitor()
        # Partial buildup on both — not yet tripping.
        for epoch in range(10):
            m.evaluate(epoch,
                        pos_consensus_delta_m=0.5,
                        ztd_consensus_delta_m=0.3)
        self.assertIsNotNone(m._pos_consensus_above_since)
        self.assertIsNotNone(m._ztd_consensus_above_since)
        m.record_trip(10)
        self.assertIsNone(m._pos_consensus_above_since)
        self.assertIsNone(m._ztd_consensus_above_since)


class ConsensusOrderingTest(unittest.TestCase):
    """Consensus triggers fire ahead of ztd_impossible / window_rms.
    If both a consensus and a physical-envelope path would trip on the
    same epoch, consensus wins because its threshold is lower — so it
    cleared first by construction."""

    def test_pos_consensus_preempts_ztd_impossible(self):
        # Both conditions true at once: pos delta high AND ZTD past
        # 700 mm for sustained epochs.  Pos consensus trips first
        # (sustained_epochs=30 vs. ztd_sustained_epochs=60).
        m = _monitor()
        # Build both conditions simultaneously.
        ev = None
        for epoch in range(35):
            ev = m.evaluate(epoch,
                             pos_consensus_delta_m=0.5,
                             ztd_m=1.5)
            if ev is not None:
                break
        self.assertIsNotNone(ev)
        self.assertEqual(ev['reason'], 'pos_consensus')


if __name__ == '__main__':
    unittest.main()
