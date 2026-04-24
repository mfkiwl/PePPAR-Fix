"""Phase wind-up correction (Wu et al. 1993).

Carrier-phase wind-up is a deterministic geometric correction that
arises because GPS/GAL/BDS antennas transmit and receive
right-hand circularly polarized (RHCP) signals.  As the
satellite-to-receiver geometry rotates over an arc — through
satellite yaw-steering and receiver Earth-rotation — the apparent
carrier phase of an RHCP signal shifts by an amount proportional
to the rotation angle.  Over a 24 h arc the cumulative wind-up
can reach multiple cycles and contributes a few mm to phase
residuals if uncorrected; combined over many SVs through the
filter's null-mode coupling, those mm-scale unmodeled signals
amplify into meter-scale trajectory error (see
``project_to_main_qpos_sweep_20260424.md`` — the residual after
σ_phi + Q_pos tuning is dominated by phase-model gaps).

References:
- Wu et al. (1993), "Effects of Antenna Orientation on GPS Carrier
  Phase," Manuscripta Geodaetica.
- Kouba (2009), "A Guide to Using International GNSS Service (IGS)
  Products," sections on phase wind-up.

Reference frames used here (Kouba 2009 conventions):

* Satellite body frame: nominal yaw-steering.  ``e_z`` along nadir
  (toward Earth center), ``e_y`` along the solar-panel axis (perp
  to the Sun-Earth-satellite plane), ``e_x`` completes the
  right-handed frame and points roughly toward the Sun's
  meridian.  Computed by ``regression.antex.sat_body_frame``.
* Receiver body frame: local geodetic at the antenna position.
  ``x_r`` = North (ECEF), ``y_r`` = East (ECEF).  No ``z_r``
  needed — the wind-up dipole formula only uses the in-plane axes.

Single-epoch wind-up angle, in radians:

    D_s = e_x_sat - k(k · e_x_sat) - k × e_y_sat
    D_r = x_r    - k(k · x_r)    + k × y_r
    ζ   = sign((D_s × D_r) · k) · arccos((D_s · D_r) / (|D_s| · |D_r|))

where ``k`` is the unit vector from satellite to receiver.

The integer-cycle ambiguity in ``arccos`` is resolved by
unwrapping against the previous epoch's value (per SV).  After
unwrap, the cumulative wind-up in radians is converted to cycles,
then to a per-SV phase correction in metres at the IF wavelength:

    correction_m = -(ζ_total / 2π) · λ_IF

Subtract this correction from the observed IF carrier-phase
observation to remove the wind-up.  The harness convention is
``o['phi_if_m'] += delta`` with delta = -windup_m, equivalent.

Slip handling: a cycle slip on the carrier resets the ambiguity
state; the absolute wind-up tracker is reset along with it via
``PhaseWindupTracker.reset(sv)``.  After reset the next epoch's
``update()`` call seeds ``ζ_total`` with the current single-epoch
``ζ`` — the absolute integer-cycle reference is lost, but that
loss is folded into the ambiguity (which the filter re-floats
after a slip anyway).

This module deliberately approximates yaw-steering as nominal at
all times — eclipse handling per Phase 5 of
``docs/obs-model-completion-plan.md`` is out of scope here.  Wind-up
inaccuracy during eclipse periods is bounded by the yaw rate's
saturation time (typically < 30 min per orbit) times the wind-up
slope at zenith, which is < 1 mm cumulative over the affected
arc — below the ~5 mm noise floor the harness sees.

The IF correction is wavelength-dependent in metres but
**combination-invariant in cycles**: a one-cycle wind-up shifts
L1 by λ_L1, L5 by λ_L5, and the IF combination's metres by
exactly ``c / (f1 + f2)``.  Derivation: φ_IF_m = (f1²·φ1_m -
f2²·φ2_m) / (f1² - f2²); substituting φ_i_m = ζ·c/f_i for a
common cycle shift gives the result.

The harness path passes ``c / (f1 + f2)`` as the effective
wavelength to ``PhaseWindupTracker.correction_m``.  Per-band
callers (if any future use) pass ``c / f_band`` for that
individual band.  PRIDE-PPPAR's ``gpsmod.f90`` applies
``dphwp/freq(i)*VLIGHT`` per band before forming IF; same
result, different sequencing.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

from regression.antex import sat_body_frame, ecef_to_enu_matrix


def instantaneous_windup_rad(
    sat_pos_ecef: np.ndarray,
    sun_pos_ecef: np.ndarray,
    rcv_pos_ecef: np.ndarray,
) -> float:
    """Single-epoch wind-up angle in radians (Wu 1993).

    Returns the wind-up angle without any unwrapping — caller is
    responsible for tracking cumulative across epochs (see
    ``PhaseWindupTracker``).  Pure function: no state, side-
    effect-free.

    Sign convention: positive angle when the satellite-receiver
    geometry rotates such that the RHCP signal phase advances at
    the receiver (Wu 1993).
    """
    e_x_sat, e_y_sat, _e_z_sat = sat_body_frame(sat_pos_ecef, sun_pos_ecef)

    # Receiver body frame: x = local North, y = local East (ECEF
    # coordinates).  ecef_to_enu_matrix returns rows in (E, N, U)
    # order; we pull out N and E.
    enu = ecef_to_enu_matrix(rcv_pos_ecef)
    x_rcv = enu[1]   # North row
    y_rcv = enu[0]   # East row

    # Unit LOS from satellite to receiver.
    los = rcv_pos_ecef - sat_pos_ecef
    k = los / float(np.linalg.norm(los))

    # Effective dipoles (Kouba 2009 eqs).
    d_s = e_x_sat - k * float(np.dot(k, e_x_sat)) - np.cross(k, e_y_sat)
    d_r = x_rcv - k * float(np.dot(k, x_rcv)) + np.cross(k, y_rcv)

    cos_arg = float(np.dot(d_s, d_r)
                    / (np.linalg.norm(d_s) * np.linalg.norm(d_r)))
    # Numerical clamp: |cos| > 1 by a few ulps would NaN the arccos.
    cos_arg = max(-1.0, min(1.0, cos_arg))

    sign_arg = float(np.dot(np.cross(d_s, d_r), k))
    sign = 1.0 if sign_arg >= 0.0 else -1.0
    return sign * math.acos(cos_arg)


class PhaseWindupTracker:
    """Per-SV cumulative wind-up tracker with epoch-to-epoch unwrap.

    State is one ``(zeta_total_rad, zeta_prev_rad)`` pair per SV.
    ``update(sv, ...)`` computes the new instantaneous angle,
    unwraps against the previous, and returns the cumulative
    cycles.  ``correction_m(sv, lambda_if_m)`` returns the
    correction to ADD to the observed IF carrier-phase (it is
    already negated, so callers can write
    ``o['phi_if_m'] += tracker.correction_m(sv, lam_if)``).

    ``reset(sv)`` clears state on a cycle slip.
    """

    def __init__(self) -> None:
        self._state: dict[str, tuple[float, float]] = {}

    def update(
        self,
        sv: str,
        sat_pos_ecef: np.ndarray,
        sun_pos_ecef: np.ndarray,
        rcv_pos_ecef: np.ndarray,
    ) -> float:
        """Advance ``sv``'s wind-up tracker; return cumulative cycles.

        First call for a given SV seeds ``zeta_total`` from the
        current instantaneous angle — i.e., the absolute
        zero-reference is whatever the wind-up was at first
        sighting.  That zero gets absorbed into the float
        ambiguity downstream, identical to how a slip-reset
        seeds a fresh ambiguity float estimate.
        """
        zeta_inst = instantaneous_windup_rad(
            sat_pos_ecef, sun_pos_ecef, rcv_pos_ecef)
        prev = self._state.get(sv)
        if prev is None:
            zeta_total = zeta_inst
        else:
            zeta_total_prev, zeta_prev = prev
            delta = zeta_inst - zeta_prev
            # Unwrap to nearest cycle.  Acos returns in [-π, π], so
            # consecutive samples can never differ by more than 2π
            # in the underlying continuous angle as long as the
            # geometry rotates by less than π between epochs (true
            # for any reasonable epoch interval on GPS-like orbits).
            if delta > math.pi:
                delta -= 2.0 * math.pi
            elif delta < -math.pi:
                delta += 2.0 * math.pi
            zeta_total = zeta_total_prev + delta
        self._state[sv] = (zeta_total, zeta_inst)
        return zeta_total / (2.0 * math.pi)

    def cycles(self, sv: str) -> Optional[float]:
        """Return current cumulative wind-up in cycles, or None if
        the SV has never been observed (or was reset)."""
        prev = self._state.get(sv)
        if prev is None:
            return None
        return prev[0] / (2.0 * math.pi)

    def correction_m(self, sv: str, lambda_eff_m: float) -> float:
        """Return the metre-domain correction to ADD to the
        observed phase to remove wind-up.  Returns 0.0 when
        the SV has no tracker state — caller should call
        ``update()`` before requesting the correction.

        ``lambda_eff_m`` is the band-effective wavelength for
        wind-up:
          - For a single-band carrier observation: ``c / f_band``
            (i.e., the standard wavelength for that band).
          - For an IF combination: ``c / (f1 + f2)``.  This is
            NOT the same as ``c / f_IF``; see the module
            docstring for the derivation.

        Sign: Wu 1993 says observed phase = geometric phase +
        wind-up.  We return a NEGATIVE-of-(cycles × wavelength)
        so the harness's ``o['phi_if_m'] += correction`` removes
        the wind-up from the observation.
        """
        cy = self.cycles(sv)
        if cy is None:
            return 0.0
        return -cy * lambda_eff_m

    def reset(self, sv: str) -> None:
        """Drop any tracker state for ``sv`` — used after a cycle
        slip when the ambiguity is being re-floated."""
        self._state.pop(sv, None)

    def known_svs(self) -> list[str]:
        """Diagnostic: list of SVs the tracker has seen."""
        return list(self._state.keys())
