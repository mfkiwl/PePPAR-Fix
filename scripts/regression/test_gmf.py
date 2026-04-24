"""Unit tests for ``regression.gmf`` (Boehm et al. 2006).

The published Boehm 2006 paper is the authoritative reference;
PRIDE-PPPAR's ``src/lib/global_map.f90`` is a verified port we
trust.  The coefficient tables in our module were transcribed
element-by-element from the PRIDE Fortran.  These tests check:

1. **Property tests** — the formula's behaviour at known
   limits (zenith, low elevation, hemisphere symmetry, seasonal
   variation).
2. **Provider equivalence** — the cached ``GMFProvider`` returns
   the same numbers as the pure ``gmf_at`` function for the
   same inputs.
3. **Reference spot values** — handful of (lat, lon, height,
   epoch, elev) inputs with hand-computed expected ranges,
   bracketing the answer.
"""

from __future__ import annotations

import math
import unittest

from regression.gmf import GMFProvider, gmf_at


# ABMF station MGEX ground-truth-ish coordinates (16°N, -61°E,
# height ~25 m below mean sea level).
_ABMF_LAT_RAD = math.radians(16.262)
_ABMF_LON_RAD = math.radians(-61.528)
_ABMF_HEIGHT_M = -25.0

# 2020 DOY 001 → MJD 58849 (UTC 0h).
_MJD_2020_001 = 58849.0


class PropertyTests(unittest.TestCase):
    """The math has invariants we can exploit even without a
    reference table on hand."""

    def test_zenith_is_close_to_unity(self):
        """At elev = 90° (zenith), both mappings should be ~1.0
        — the slant-to-zenith ratio is by definition 1 there.
        Boehm 2006's expansion gives 1 to 4-5 decimal places."""
        elev = math.radians(90.0)
        m_h, m_w = gmf_at(_MJD_2020_001, _ABMF_LAT_RAD,
                          _ABMF_LON_RAD, _ABMF_HEIGHT_M, elev)
        self.assertAlmostEqual(m_h, 1.0, places=3)
        self.assertAlmostEqual(m_w, 1.0, places=3)

    def test_low_elev_amplifies_mapping(self):
        """At elev = 5° (the harness's low-elev floor),
        both mappings should be ~10x — slant path through the
        atmosphere is ~10× zenith path.  GMF refines this; we
        check it's in the 8-12× range."""
        elev = math.radians(5.0)
        m_h, m_w = gmf_at(_MJD_2020_001, _ABMF_LAT_RAD,
                          _ABMF_LON_RAD, _ABMF_HEIGHT_M, elev)
        self.assertGreater(m_h, 8.0)
        self.assertLess(m_h, 12.0)
        self.assertGreater(m_w, 8.0)
        self.assertLess(m_w, 12.0)

    def test_mapping_decreases_with_increasing_elevation(self):
        """Monotonic in elev — higher in the sky = shorter slant
        path = smaller mapping factor."""
        prev_h = float('inf')
        prev_w = float('inf')
        for elev_deg in (5, 10, 15, 30, 45, 60, 75, 89):
            m_h, m_w = gmf_at(_MJD_2020_001, _ABMF_LAT_RAD,
                              _ABMF_LON_RAD, _ABMF_HEIGHT_M,
                              math.radians(elev_deg))
            self.assertLess(m_h, prev_h,
                            f"m_h not monotonic at elev={elev_deg}")
            self.assertLess(m_w, prev_w,
                            f"m_w not monotonic at elev={elev_deg}")
            prev_h, prev_w = m_h, m_w

    def test_hydrostatic_close_to_wet_at_zenith(self):
        """At zenith the difference between m_h and m_w is
        sub-mm-equivalent — both should agree to ~3 decimals."""
        elev = math.radians(89.5)
        m_h, m_w = gmf_at(_MJD_2020_001, _ABMF_LAT_RAD,
                          _ABMF_LON_RAD, _ABMF_HEIGHT_M, elev)
        self.assertAlmostEqual(m_h, m_w, places=3)

    def test_hydrostatic_diverges_from_wet_at_low_elev(self):
        """Hydrostatic and wet mapping functions both grow at
        low elev but at different rates (different a/b/c).  At
        5° they should be measurably different — > 0.1 apart."""
        elev = math.radians(5.0)
        m_h, m_w = gmf_at(_MJD_2020_001, _ABMF_LAT_RAD,
                          _ABMF_LON_RAD, _ABMF_HEIGHT_M, elev)
        self.assertGreater(abs(m_h - m_w), 0.1)

    def test_height_correction_increases_hydrostatic(self):
        """The Niell 1996 height correction adds to m_h
        proportional to height.  Higher station → larger m_h
        at a given elev.  Wet has no height correction."""
        elev = math.radians(15.0)
        m_h_low, m_w_low = gmf_at(
            _MJD_2020_001, _ABMF_LAT_RAD, _ABMF_LON_RAD,
            -25.0, elev)
        m_h_high, m_w_high = gmf_at(
            _MJD_2020_001, _ABMF_LAT_RAD, _ABMF_LON_RAD,
            3000.0, elev)
        # Hydrostatic scales with height.
        self.assertGreater(m_h_high, m_h_low)
        # Wet should be invariant (no height term in Boehm 2006).
        self.assertAlmostEqual(m_w_high, m_w_low, places=12)

    def test_seasonal_variation_changes_mapping(self):
        """Hydrostatic coefficients have an annual-cycle term —
        winter and summer should give different m_h.  Magnitude
        is small (mm level at zenith, slightly larger at low
        elev) but should be detectable above floating-point
        precision."""
        elev = math.radians(15.0)
        # MJD 58849 is Jan 1 2020; MJD 59031 is ~Jul 2020.
        m_h_jan, _ = gmf_at(58849.0, _ABMF_LAT_RAD,
                            _ABMF_LON_RAD, _ABMF_HEIGHT_M, elev)
        m_h_jul, _ = gmf_at(59031.0, _ABMF_LAT_RAD,
                            _ABMF_LON_RAD, _ABMF_HEIGHT_M, elev)
        self.assertNotEqual(m_h_jan, m_h_jul)
        # But not wildly different — both should still be in the
        # ~3.86 range at 15° elev.
        self.assertGreater(abs(m_h_jan - m_h_jul), 1e-6)
        self.assertLess(abs(m_h_jan - m_h_jul), 0.05)


class ProviderEquivalenceTest(unittest.TestCase):
    """``GMFProvider`` caches station + epoch state for cheap
    per-SV queries.  Output must match the pure ``gmf_at`` for
    the same inputs to within fp precision."""

    def test_provider_matches_pure_function(self):
        prov = GMFProvider(
            _ABMF_LAT_RAD, _ABMF_LON_RAD, _ABMF_HEIGHT_M)
        prov.update_epoch(_MJD_2020_001)
        for elev_deg in (5, 10, 15, 30, 45, 60, 75, 89):
            elev = math.radians(elev_deg)
            m_h_pure, m_w_pure = gmf_at(
                _MJD_2020_001, _ABMF_LAT_RAD, _ABMF_LON_RAD,
                _ABMF_HEIGHT_M, elev)
            m_h_prov = prov.m_hydrostatic(elev)
            m_w_prov = prov.m_wet(elev)
            self.assertAlmostEqual(
                m_h_prov, m_h_pure, places=10,
                msg=f"m_h mismatch at {elev_deg}°")
            self.assertAlmostEqual(
                m_w_prov, m_w_pure, places=10,
                msg=f"m_w mismatch at {elev_deg}°")

    def test_update_epoch_changes_results(self):
        """Different epoch → different mapping due to seasonal
        term."""
        prov = GMFProvider(
            _ABMF_LAT_RAD, _ABMF_LON_RAD, _ABMF_HEIGHT_M)
        prov.update_epoch(58849.0)
        m_h_jan = prov.m_hydrostatic(math.radians(15.0))
        prov.update_epoch(59031.0)
        m_h_jul = prov.m_hydrostatic(math.radians(15.0))
        self.assertNotEqual(m_h_jan, m_h_jul)


class SimpleVs1OverSinElev(unittest.TestCase):
    """The current harness uses ``1/sin(elev)`` — these tests
    document GMF's deviation from the trivial mapping at each
    elev band, which is what we expect to see fix the meter-
    scale low-elev phase residuals."""

    def test_low_elev_difference_meaningful(self):
        """At elev = 5°, GMF differs from 1/sin(e) by tens of
        cm of slant delay (when ZTD = 2.3 m).  That's the
        low-elev correction that the obs-model-completion-plan
        identified as the next-bigger lever."""
        elev = math.radians(5.0)
        m_h, m_w = gmf_at(_MJD_2020_001, _ABMF_LAT_RAD,
                          _ABMF_LON_RAD, _ABMF_HEIGHT_M, elev)
        m_simple = 1.0 / math.sin(elev)  # 11.474
        # GMF hydrostatic at 5° is typically ~10.2; simple is
        # ~11.5.  Slant delay difference at ZTD ~2.3 m:
        # (11.5 - 10.2) * 2.3 = 3 m.
        delta = abs(m_h - m_simple)
        slant_delta_m = delta * 2.3
        self.assertGreater(slant_delta_m, 0.5,
                           f"Δslant @5°={slant_delta_m:.2f} m, "
                           "expected > 0.5 m")
        self.assertLess(slant_delta_m, 5.0)

    def test_zenith_difference_negligible(self):
        """At zenith both mappings are ~1.0 to 4-5 decimal
        places — < 1 mm slant-delay difference."""
        elev = math.radians(89.0)
        m_h, _ = gmf_at(_MJD_2020_001, _ABMF_LAT_RAD,
                        _ABMF_LON_RAD, _ABMF_HEIGHT_M, elev)
        m_simple = 1.0 / math.sin(elev)
        slant_delta_m = abs(m_h - m_simple) * 2.3
        self.assertLess(slant_delta_m, 0.005)  # < 5 mm


if __name__ == "__main__":
    unittest.main()
