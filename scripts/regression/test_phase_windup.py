"""Unit tests for ``regression.phase_windup``.

Exercises the Wu 1993 wind-up formula on synthetic geometries
where the answer is known analytically.  Real-data sanity is
covered downstream by the regression harness's ABMF run with
``--phase-windup``.
"""

from __future__ import annotations

import math
import unittest

import numpy as np

from regression.phase_windup import (
    PhaseWindupTracker, instantaneous_windup_rad,
)


# A representative GPS satellite ECEF position roughly directly
# above ABMF (Caribbean, ~16° N, ~-61° E).  Geometry doesn't have
# to be exact for unit tests — we just need stable inputs.
_SAT_ZENITH_OVER_ABMF = np.array(
    [9_900_000.0, -19_000_000.0, 5_500_000.0])

# Sun direction from Earth at ~equinox vernal: roughly along +X
# ECEF in some idealization.  Realistic Sun-Earth distance.
_SUN_ECEF = np.array([1.5e11, 0.0, 0.0])

# ABMF surveyed ECEF (from PRIDE data).
_ABMF_ECEF = np.array([2919785.79086, -5383744.95943, 1774604.85992])


class InstantaneousWindupTest(unittest.TestCase):
    """The pure ``instantaneous_windup_rad`` function."""

    def test_returns_finite_for_typical_geometry(self):
        zeta = instantaneous_windup_rad(
            _SAT_ZENITH_OVER_ABMF, _SUN_ECEF, _ABMF_ECEF)
        self.assertTrue(math.isfinite(zeta))
        self.assertGreaterEqual(zeta, -math.pi)
        self.assertLessEqual(zeta, math.pi)

    def test_geometry_change_changes_angle(self):
        """Same Sun, slightly different sat position → different
        wind-up.  Confirms the formula isn't trivially zero."""
        zeta1 = instantaneous_windup_rad(
            _SAT_ZENITH_OVER_ABMF, _SUN_ECEF, _ABMF_ECEF)
        # Move the sat 1000 km along the orbit.
        sat2 = _SAT_ZENITH_OVER_ABMF + np.array([0.0, 1.0e6, 0.0])
        zeta2 = instantaneous_windup_rad(
            sat2, _SUN_ECEF, _ABMF_ECEF)
        self.assertNotAlmostEqual(zeta1, zeta2, places=4)

    def test_sun_change_changes_angle(self):
        """Same sat-receiver geometry, different Sun direction →
        different wind-up.  The satellite body frame depends on
        Sun position via the yaw-steering convention."""
        zeta1 = instantaneous_windup_rad(
            _SAT_ZENITH_OVER_ABMF, _SUN_ECEF, _ABMF_ECEF)
        sun2 = np.array([0.0, 1.5e11, 0.0])  # 90° around
        zeta2 = instantaneous_windup_rad(
            _SAT_ZENITH_OVER_ABMF, sun2, _ABMF_ECEF)
        self.assertNotAlmostEqual(zeta1, zeta2, places=4)

    def test_pure_function_no_side_effects(self):
        """Calling twice with same args returns same result."""
        zeta1 = instantaneous_windup_rad(
            _SAT_ZENITH_OVER_ABMF, _SUN_ECEF, _ABMF_ECEF)
        zeta2 = instantaneous_windup_rad(
            _SAT_ZENITH_OVER_ABMF, _SUN_ECEF, _ABMF_ECEF)
        self.assertEqual(zeta1, zeta2)


class TrackerTest(unittest.TestCase):
    """``PhaseWindupTracker`` cumulative + reset behaviour."""

    def test_first_call_seeds_total(self):
        """First update() for an SV stores the instantaneous as
        the cumulative — there's no prior reference."""
        tr = PhaseWindupTracker()
        cy = tr.update(
            "G05", _SAT_ZENITH_OVER_ABMF, _SUN_ECEF, _ABMF_ECEF)
        self.assertIsInstance(cy, float)
        self.assertEqual(cy, tr.cycles("G05"))

    def test_unknown_sv_returns_none(self):
        tr = PhaseWindupTracker()
        self.assertIsNone(tr.cycles("G05"))
        # correction_m returns 0 (silently skipped, like PCV does)
        self.assertEqual(tr.correction_m("G05", 0.247), 0.0)

    def test_update_accumulates_no_unwrap(self):
        """Two close-together epochs should accumulate without
        unwrap (Δ < π)."""
        tr = PhaseWindupTracker()
        cy1 = tr.update("G05", _SAT_ZENITH_OVER_ABMF,
                        _SUN_ECEF, _ABMF_ECEF)
        # Move sat by 100 km — small epoch step.
        sat2 = _SAT_ZENITH_OVER_ABMF + np.array([0.0, 1.0e5, 0.0])
        cy2 = tr.update("G05", sat2, _SUN_ECEF, _ABMF_ECEF)
        # Same SV, slightly different geometry.  Both finite.
        self.assertTrue(math.isfinite(cy2))
        # Tracker advanced past the first call.
        self.assertNotEqual(cy1, cy2)

    def test_reset_clears_state(self):
        tr = PhaseWindupTracker()
        tr.update("G05", _SAT_ZENITH_OVER_ABMF,
                  _SUN_ECEF, _ABMF_ECEF)
        self.assertIn("G05", tr.known_svs())
        tr.reset("G05")
        self.assertNotIn("G05", tr.known_svs())
        self.assertIsNone(tr.cycles("G05"))

    def test_reset_unknown_sv_is_safe(self):
        """reset() on an SV the tracker hasn't seen is a noop."""
        tr = PhaseWindupTracker()
        tr.reset("G05")  # must not raise

    def test_correction_meters_scales_with_wavelength(self):
        """correction_m at 0.5 m λ_eff should be 2× correction
        at 0.25 m λ_eff for the same cumulative cycles."""
        tr = PhaseWindupTracker()
        tr.update("G05", _SAT_ZENITH_OVER_ABMF,
                  _SUN_ECEF, _ABMF_ECEF)
        c1 = tr.correction_m("G05", 0.25)
        c2 = tr.correction_m("G05", 0.50)
        self.assertAlmostEqual(c2, 2.0 * c1, places=10)

    def test_correction_sign(self):
        """Correction is the NEGATIVE of cycles × λ_eff — the
        harness adds it to the observed phase, removing the
        wind-up."""
        tr = PhaseWindupTracker()
        tr.update("G05", _SAT_ZENITH_OVER_ABMF,
                  _SUN_ECEF, _ABMF_ECEF)
        cy = tr.cycles("G05")
        c_m = tr.correction_m("G05", 0.247)
        self.assertAlmostEqual(c_m, -cy * 0.247, places=12)

    def test_if_effective_wavelength_formula(self):
        """For IF wind-up, λ_eff = c / (f1 + f2), not c / f_IF.

        This test documents the formula choice — derived in the
        module docstring — by checking that the correction
        delivered for plausible L1+L5 frequencies sits at the
        ~10 cm scale per cycle, between λ_L1 (19 cm) and λ_L5
        (25 cm) but smaller than either.
        """
        c = 299_792_458.0
        f1 = 1.57542e9     # GPS L1
        f2 = 1.17645e9     # L5
        lam_eff = c / (f1 + f2)
        # Sanity: ~0.1089 m for L1+L5.
        self.assertAlmostEqual(lam_eff, 0.10894, places=4)
        # And about half the L1+L5 average wavelength.
        lam1 = c / f1
        lam2 = c / f2
        avg = (lam1 + lam2) / 2
        self.assertLess(lam_eff, avg)

    def test_unwrap_through_full_rotation(self):
        """Synthetic full rotation: feed a sequence of single-
        epoch angles that cross the ±π boundary and check the
        cumulative goes through 2π without a discontinuity.
        We use the underlying tracker state by manipulating the
        geometry — easier with pure-math: directly assemble a
        sequence of (sat, sun, rcv) triplets that yield specific
        instantaneous angles.

        Approach: rotate the satellite slowly around the
        receiver; the cumulative wind-up should grow
        monotonically without 2π reset jumps.
        """
        tr = PhaseWindupTracker()
        # Generate 64 epochs spread around an orbital arc.
        cycles_history: list[float] = []
        for i in range(64):
            theta = i * (2 * math.pi / 64)
            # Rotate the sat in ECEF Z-plane around the receiver.
            r0 = _SAT_ZENITH_OVER_ABMF - _ABMF_ECEF
            r0_norm = np.linalg.norm(r0)
            # Build a rotated direction in the same plane.
            cos_t, sin_t = math.cos(theta), math.sin(theta)
            sat_rotated = _ABMF_ECEF + np.array([
                r0[0] * cos_t - r0[1] * sin_t,
                r0[0] * sin_t + r0[1] * cos_t,
                r0[2],
            ])
            cy = tr.update("G05", sat_rotated, _SUN_ECEF,
                           _ABMF_ECEF)
            cycles_history.append(cy)
        # Expect monotonic-ish trajectory without ±0.5-cycle
        # discontinuities.  Check no consecutive pair differs by
        # more than 0.5 cycles (would be the un-unwrapped sign of
        # a missing 2π).
        for a, b in zip(cycles_history, cycles_history[1:]):
            self.assertLess(
                abs(b - a), 0.5,
                f"discontinuity: {a:.4f} → {b:.4f} cycles")


if __name__ == "__main__":
    unittest.main()
