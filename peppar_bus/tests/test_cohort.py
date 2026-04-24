"""Pure-function tests for peppar_bus.cohort.

No bus, no threads.  Snapshots are lightweight namedtuple-like
stand-ins — cohort helpers duck-type against ``.identity``,
``.position``, ``.ztd``.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Optional

from peppar_bus import PeerIdentity, cohort, schemas


@dataclass
class _Snap:
    """Test-side PeerSnapshot stand-in.  Matches the duck-typed
    shape cohort helpers expect without importing peppar_mon."""
    host: str
    identity: PeerIdentity
    position: Optional[schemas.PositionPayload] = None
    ztd: Optional[schemas.ZTDPayload] = None


def _snap(host, antenna_ref="UFO1", site_ref="DuPage",
          lat=None, lon=None, alt=None, ztd_m=None):
    pos = None
    if lat is not None:
        pos = schemas.PositionPayload(lat_deg=lat, lon_deg=lon, alt_m=alt)
    ztd = None
    if ztd_m is not None:
        ztd = schemas.ZTDPayload(ztd_m=ztd_m)
    return _Snap(
        host=host,
        identity=PeerIdentity(
            host=host, antenna_ref=antenna_ref, site_ref=site_ref,
        ),
        position=pos, ztd=ztd,
    )


class GroupingTest(unittest.TestCase):
    def test_group_by_antenna_ref(self):
        snaps = [
            _snap("h1", antenna_ref="UFO1"),
            _snap("h2", antenna_ref="UFO1"),
            _snap("h3", antenna_ref="PATCH3"),
        ]
        groups = cohort.group_by_antenna_ref(snaps)
        self.assertEqual(set(groups.keys()), {"UFO1", "PATCH3"})
        self.assertEqual(len(groups["UFO1"]), 2)
        self.assertEqual(len(groups["PATCH3"]), 1)

    def test_empty_refs_excluded(self):
        """Hosts with empty antenna_ref don't join any cohort."""
        snaps = [
            _snap("h1", antenna_ref="UFO1"),
            _snap("h2", antenna_ref=""),
        ]
        groups = cohort.group_by_antenna_ref(snaps)
        self.assertEqual(list(groups.keys()), ["UFO1"])
        self.assertEqual(len(groups["UFO1"]), 1)

    def test_missing_identity_skipped(self):
        """A snapshot whose identity is None is silently skipped."""
        snaps = [
            _Snap(host="x", identity=None),
            _snap("h2", antenna_ref="UFO1"),
        ]
        self.assertEqual(
            list(cohort.group_by_antenna_ref(snaps).keys()), ["UFO1"],
        )

    def test_group_by_site_ref_differs_from_antenna(self):
        """L5-fleet-ish scenario: 3 hosts on UFO1 + 1 on PATCH3,
        all declaring site_ref='DuPage'.  Site grouping collapses
        them all; antenna grouping keeps them separate."""
        snaps = [
            _snap("timehat", antenna_ref="UFO1", site_ref="DuPage"),
            _snap("clkpoc3", antenna_ref="UFO1", site_ref="DuPage"),
            _snap("madhat", antenna_ref="UFO1", site_ref="DuPage"),
            _snap("ptpmon", antenna_ref="PATCH3", site_ref="DuPage"),
        ]
        ant = cohort.group_by_antenna_ref(snaps)
        site = cohort.group_by_site_ref(snaps)
        self.assertEqual(len(ant), 2)
        self.assertEqual(len(ant["UFO1"]), 3)
        self.assertEqual(len(ant["PATCH3"]), 1)
        self.assertEqual(list(site.keys()), ["DuPage"])
        self.assertEqual(len(site["DuPage"]), 4)


class MedianPositionTest(unittest.TestCase):
    def test_three_peers_identical_position(self):
        snaps = [
            _snap("a", lat=40.0, lon=-90.0, alt=200.0),
            _snap("b", lat=40.0, lon=-90.0, alt=200.0),
            _snap("c", lat=40.0, lon=-90.0, alt=200.0),
        ]
        got = cohort.cohort_median_position(snaps, "UFO1")
        self.assertIsNotNone(got)
        lat_m, lon_m, alt_m, n = got
        self.assertEqual((lat_m, lon_m, alt_m, n),
                         (40.0, -90.0, 200.0, 3))

    def test_outlier_doesnt_drag_median(self):
        """Three peers where one is 1 m offset.  Median ignores
        the outlier."""
        snaps = [
            _snap("a", lat=40.000, lon=-90.000, alt=200.0),
            _snap("b", lat=40.000, lon=-90.000, alt=200.0),
            _snap("c", lat=40.00001, lon=-90.00001, alt=201.0),
        ]
        lat_m, lon_m, alt_m, n = cohort.cohort_median_position(
            snaps, "UFO1",
        )
        self.assertEqual((lat_m, lon_m, alt_m), (40.0, -90.0, 200.0))
        self.assertEqual(n, 3)

    def test_single_peer_returns_none(self):
        """Median of one isn't a consensus — reject cohorts of 1."""
        snaps = [_snap("a", lat=40.0, lon=-90.0, alt=200.0)]
        self.assertIsNone(cohort.cohort_median_position(snaps, "UFO1"))

    def test_cross_cohort_isolated(self):
        """A peer with a different antenna_ref is ignored by the
        UFO1 cohort computation."""
        snaps = [
            _snap("a", antenna_ref="UFO1", lat=40.0, lon=-90.0, alt=200.0),
            _snap("b", antenna_ref="UFO1", lat=40.0, lon=-90.0, alt=200.0),
            _snap("c", antenna_ref="PATCH3", lat=41.0, lon=-91.0, alt=250.0),
        ]
        lat_m, lon_m, alt_m, n = cohort.cohort_median_position(
            snaps, "UFO1",
        )
        self.assertEqual(n, 2)
        self.assertEqual((lat_m, lon_m, alt_m), (40.0, -90.0, 200.0))


class MedianZtdTest(unittest.TestCase):
    def test_even_count_averages_middle_two(self):
        snaps = [
            _snap("a", ztd_m=-0.100),
            _snap("b", ztd_m=-0.050),
            _snap("c", ztd_m=+0.050),
            _snap("d", ztd_m=+0.100),
        ]
        got = cohort.cohort_median_ztd(snaps, "DuPage")
        self.assertIsNotNone(got)
        ztd_m, n = got
        # Median of [-0.1, -0.05, 0.05, 0.1] = mean of middle two = 0
        self.assertAlmostEqual(ztd_m, 0.0, places=6)
        self.assertEqual(n, 4)

    def test_site_cohort_spans_antennas(self):
        """Hosts on different antennas but same site contribute to
        ZTD median (the shared-atmosphere cohort semantics)."""
        snaps = [
            _snap("a", antenna_ref="UFO1", site_ref="DuPage", ztd_m=0.01),
            _snap("b", antenna_ref="UFO1", site_ref="DuPage", ztd_m=0.02),
            _snap("c", antenna_ref="PATCH3", site_ref="DuPage", ztd_m=0.03),
        ]
        ztd_m, n = cohort.cohort_median_ztd(snaps, "DuPage")
        self.assertEqual(n, 3)
        self.assertAlmostEqual(ztd_m, 0.02, places=6)


class DistanceTest(unittest.TestCase):
    def test_same_point_is_zero(self):
        h, d3 = cohort.ecef_distance_m(40.0, -90.0, 200.0,
                                        40.0, -90.0, 200.0)
        self.assertAlmostEqual(h, 0.0, places=6)
        self.assertAlmostEqual(d3, 0.0, places=6)

    def test_one_degree_lat_is_111km(self):
        """Flat-earth approximation: 1° lat ≈ 111.3 km."""
        h, d3 = cohort.ecef_distance_m(40.0, -90.0, 200.0,
                                        41.0, -90.0, 200.0)
        self.assertAlmostEqual(h / 1000, 111.32, places=1)
        self.assertAlmostEqual(d3 / 1000, 111.32, places=1)

    def test_vertical_only(self):
        """Same horizontal, 1 m altitude difference."""
        h, d3 = cohort.ecef_distance_m(40.0, -90.0, 200.0,
                                        40.0, -90.0, 201.0)
        self.assertAlmostEqual(h, 0.0, places=4)
        self.assertAlmostEqual(d3, 1.0, places=4)


if __name__ == "__main__":
    unittest.main()
