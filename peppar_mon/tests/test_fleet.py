"""Unit tests for fleet aggregation.

Pure-function tests for compute_summary() — no bus, no threads.
PeerBus integration is covered separately in the peppar_bus test
suite; here we just feed dataclass snapshots and inspect the
summary.
"""

from __future__ import annotations

import unittest

from peppar_bus import PeerIdentity, schemas

from peppar_mon.fleet import PeerSnapshot, compute_summary


def _snap(host, lat, lon, alt, anchored_svs=(), ztd_m=None,
          antenna_ref="UFO1"):
    """Build a PeerSnapshot with the three display-relevant fields
    set.  Used by the tests below to keep cases concise."""
    sv_states = {sv: "ANCHORED" for sv in anchored_svs}
    return PeerSnapshot(
        host=host,
        identity=PeerIdentity(host=host, antenna_ref=antenna_ref),
        last_recv_mono_ns=1,
        position=schemas.PositionPayload(
            lat_deg=lat, lon_deg=lon, alt_m=alt,
            ant_pos_est_state="anchored",
        ),
        ztd=schemas.ZTDPayload(ztd_m=ztd_m) if ztd_m is not None else None,
        sv_state=schemas.SvStatePayload(sv_states=sv_states),
    )


class ComputeSummaryTest(unittest.TestCase):
    def test_fewer_than_two_peers_empty_summary(self):
        """With 0 or 1 peers, no pairwise Δ is meaningful — the
        widget should render a placeholder row instead."""
        self.assertEqual(compute_summary([]).max_delta_3d_m, None)
        self.assertEqual(
            compute_summary([_snap("h1", 40.0, -90.0, 200.0)]).max_delta_3d_m,
            None,
        )

    def test_shared_antenna_position_delta(self):
        """Two shared-antenna peers at identical coordinates → Δ=0.
        Offset them by a degree-sixth → Δ ≈ 1.1 m horizontal."""
        snaps = [
            _snap("a", 40.0, -90.0, 200.0),
            _snap("b", 40.0, -90.0, 200.0),
        ]
        s = compute_summary(snaps)
        self.assertAlmostEqual(s.max_delta_h_m, 0.0, places=5)
        self.assertAlmostEqual(s.max_delta_3d_m, 0.0, places=5)

        snaps[1] = _snap("b", 40.0 + 1e-5, -90.0, 200.0)
        s = compute_summary(snaps)
        # 1e-5° lat ≈ 1.113 m
        self.assertAlmostEqual(s.max_delta_h_m, 1.113, delta=0.01)

    def test_anchored_count_per_host_sorted(self):
        """Counts come out in hostname order (stable display)."""
        snaps = [
            _snap("clkpoc3", 40.0, -90.0, 200.0,
                  anchored_svs=("G05", "G10", "E11")),
            _snap("madhat", 40.0, -90.0, 200.0,
                  anchored_svs=("G05", "G10")),
            _snap("timehat", 40.0, -90.0, 200.0,
                  anchored_svs=("G05", "G10", "G17", "E11")),
        ]
        s = compute_summary(snaps)
        self.assertEqual(
            s.anchored_per_host,
            [("clkpoc3", 3), ("madhat", 2), ("timehat", 4)],
        )

    def test_ztd_spread(self):
        """Spread = max − min across the cohort, expressed in mm."""
        snaps = [
            _snap("a", 40.0, -90.0, 200.0, ztd_m=-0.100),
            _snap("b", 40.0, -90.0, 200.0, ztd_m=-0.095),
            _snap("c", 40.0, -90.0, 200.0, ztd_m=-0.112),
        ]
        s = compute_summary(snaps)
        # Spread is 0.017 m = 17 mm.
        self.assertAlmostEqual(s.ztd_spread_mm, 17.0, places=1)

    def test_heterogeneous_antenna_picks_largest_cohort(self):
        """When the multicast group contains two antennas, compare
        only within the largest cohort — avoids spurious cross-
        antenna Δ."""
        snaps = [
            _snap("a", 40.0, -90.0, 200.0, antenna_ref="UFO1"),
            _snap("b", 40.0, -90.0, 200.0, antenna_ref="UFO1"),
            _snap("c", 41.0, -91.0, 250.0, antenna_ref="PATCH3"),
        ]
        s = compute_summary(snaps)
        # Should only compare a↔b (both UFO1); PATCH3 cohort is
        # size 1 so produces no Δ.
        self.assertAlmostEqual(s.max_delta_h_m, 0.0, places=5)
        # But n_hosts reports the total (all three arrived on the bus).
        self.assertEqual(s.n_hosts, 3)


if __name__ == "__main__":
    unittest.main()
