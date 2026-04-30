"""Phase-1 bootstrap convergence-gate checks.

The default EKF self-consistency test — σ_3d below a threshold, position
stable between epochs — is necessary but not sufficient.  It tells us
the filter agrees with itself; it says nothing about whether the filter
is right.  The 2026-04-17 lab evidence (see docs/position-bootstrap-
reliability-plan.md) had three hosts exit Phase 1 with σ ≤ 0.1 m at
real position errors of 2 m, 17 m, and 40 m.

This module adds two independent gate checks the EKF cannot reach by
itself, plus a "harder reset" primitive for the case when a gate
aborts a convergence attempt.

W1  residuals_consistent(filt, resid, k=2.0)
    Split `resid` into PR and phi by the filter's `last_residual_labels`.
    Require PR RMS < k·SIGMA_P_IF and max |PR resid| < 5·SIGMA_P_IF.
    Catches gross outlier / wide-distribution failures that a pure σ
    gate does not.

W2  nav2_agrees(pos_ecef, nav2_opinion, horiz_m=5.0)
    Project ECEF displacement onto the tangent plane, require horizontal
    disagreement < horiz_m.  NAV2's known east-bias vs PPP-AR on shared
    antennas is ~4 m; 5 m is the smallest threshold that survives that
    bias as noise, with enough margin to catch the MadHat 40 m case.

W3  scrub_for_retry(filt)
    Inflate position and ambiguity covariances but preserve clock, ISB,
    and ZTD states (those don't carry position bias and re-learning them
    costs minutes).  Less destructive than filt.initialize(); decisive
    enough to break out of a locked-in local minimum.
"""

from __future__ import annotations

import logging
import math

import numpy as np

log = logging.getLogger(__name__)


# ── W1: residual consistency ───────────────────────────────────────── #

def residuals_consistent(
    filt,
    resid,
    sigma_p_if: float,
    pr_rms_k: float = 2.0,
    pr_max_k: float = 5.0,
) -> tuple[bool, dict]:
    """Is the residual distribution consistent with SIGMA_P_IF?

    Returns (ok, details).  `details` always populated for logging even
    when ok=True — callers should log whenever the gate is consulted.

    Gates:
      rms_pr   < pr_rms_k · sigma_p_if     # distribution width
      max |r|  < pr_max_k · sigma_p_if     # worst outlier

    Phi residuals not checked — they pick up carrier-phase ambiguity
    bias rather than measurement noise during cold-start float phase.
    """
    details = {
        'n_pr': 0, 'rms_pr': 0.0, 'max_pr': 0.0,
        'rms_threshold': pr_rms_k * sigma_p_if,
        'max_threshold': pr_max_k * sigma_p_if,
        'ok': True, 'reason': '',
    }

    labels = getattr(filt, 'last_residual_labels', None)
    if not labels or len(resid) == 0:
        # No labels → trust the EKF's own gate.  Logging a warning once
        # here would flood; silent pass is fine for this edge case.
        return True, details

    pr_residuals = [
        float(resid[i]) for i, (_, kind, _) in enumerate(labels)
        if kind == 'pr' and i < len(resid)
    ]
    if not pr_residuals:
        return True, details

    pr_arr = np.asarray(pr_residuals)
    n = len(pr_arr)
    rms = float(np.sqrt(np.mean(pr_arr ** 2)))
    max_abs = float(np.max(np.abs(pr_arr)))

    details['n_pr'] = n
    details['rms_pr'] = rms
    details['max_pr'] = max_abs

    if rms >= details['rms_threshold']:
        details['ok'] = False
        details['reason'] = f"rms_pr={rms:.2f}m ≥ {details['rms_threshold']:.2f}m"
    elif max_abs >= details['max_threshold']:
        details['ok'] = False
        details['reason'] = f"max|pr_resid|={max_abs:.2f}m ≥ {details['max_threshold']:.2f}m"

    return details['ok'], details


# ── W2: NAV2 horizontal cross-check ────────────────────────────────── #

def nav2_agrees(
    pos_ecef: np.ndarray,
    nav2_opinion: dict | None,
    horiz_m: float = 5.0,
) -> tuple[bool, dict]:
    """Does NAV2's independent fix agree horizontally?

    Returns (ok, details).  ok=True also when nav2_opinion is None (no
    opinion to compare against — don't block convergence on that).

    Projects the ECEF displacement onto the local tangent plane so
    "horizontal" is the component perpendicular to up at the filter's
    position.  Same method as the steady-state NAV2 watchdog.
    """
    details = {
        'available': False,
        'disp_h_m': 0.0, 'disp_v_m': 0.0,
        'threshold_m': horiz_m,
        'ok': True, 'reason': '',
    }
    if nav2_opinion is None:
        details['reason'] = 'no NAV2 opinion available'
        return True, details
    details['available'] = True

    nav2_ecef = nav2_opinion['ecef']
    diff = pos_ecef - nav2_ecef
    up_hat = pos_ecef / np.linalg.norm(pos_ecef)
    vertical = float(np.dot(diff, up_hat))
    horiz_vec = diff - vertical * up_hat
    disp_h = float(np.linalg.norm(horiz_vec))

    details['disp_h_m'] = disp_h
    details['disp_v_m'] = abs(vertical)

    if disp_h >= horiz_m:
        details['ok'] = False
        details['reason'] = (
            f"horiz disp {disp_h:.1f}m ≥ {horiz_m:.1f}m vs NAV2"
        )
    return details['ok'], details


# ── W3: harder reset on gate abort ────────────────────────────────── #

def scrub_for_retry(
    filt,
    n_base: int,
    pos_sigma_m: float = 100.0,
    amb_sigma_m: float = 50.0,
    reseed_ecef: np.ndarray | None = None,
) -> None:
    """Inflate the filter so a retry can escape a locked-in wrong state.

    Keeps clock, ISB, and ZTD states and their covariance rows/columns
    because those don't carry a position bias (clock is absolute, ISB
    is per-constellation offset, ZTD is atmospheric).  Re-learning them
    costs ~minutes we don't need to pay.

    - Position covariance → pos_sigma_m² on the diagonal, zero off-
      diagonal so position re-couples to observations cleanly.
    - Ambiguity covariances → amb_sigma_m² diagonal; off-diagonals
      zero so the ambiguities can re-converge from the current state
      without being dragged by the stale cross-correlations.
    - If reseed_ecef is provided, the position state is replaced;
      otherwise position stays where it was but with large covariance.

    After scrub the EKF predict/update should tighten things up in
    tens of epochs rather than minutes.

    NAV2-pull pattern (preferred, I-024532-charlie #4):
        scrub_for_retry(filt, n_base,
                        reseed_ecef=nav2_ecef,
                        pos_sigma_m=max(1.0, nav2_h_acc_m))

    The 1 m floor is empirical: NAV2 occasionally reports sub-metre
    hAcc on exceptionally clean fixes; over-trusting that claim
    against legitimate position reseed risk loses earned confidence
    that has to be re-paid in convergence time.

    Legacy P-blowup pattern (deprecated, kept for back-compat):
        scrub_for_retry(filt, n_base, pos_sigma_m=100.0)

    The 100 m default produces the failure mode documented on MadHat
    2026-04-29 (22 m altitude drop in 10 s after a SO_POS reset,
    single-freq iono bias re-walked into position via the loose σ).
    Callers with access to a NAV2 opinion should always prefer the
    NAV2-pull pattern.
    """
    if reseed_ecef is not None:
        filt.x[0:3] = np.asarray(reseed_ecef, dtype=float)

    # Blow out position covariance cleanly.
    filt.P[0:3, :] = 0.0
    filt.P[:, 0:3] = 0.0
    filt.P[0, 0] = pos_sigma_m ** 2
    filt.P[1, 1] = pos_sigma_m ** 2
    filt.P[2, 2] = pos_sigma_m ** 2

    # Ambiguity diagonals — keep indices intact so sv_to_idx still maps.
    n = filt.P.shape[0]
    if n > n_base:
        for i in range(n_base, n):
            filt.P[i, :] = 0.0
            filt.P[:, i] = 0.0
            filt.P[i, i] = amb_sigma_m ** 2

    # prev_obs carries phase/phi values from the rejected state —
    # next epoch will rebuild from fresh observations.
    if hasattr(filt, 'prev_obs') and filt.prev_obs is not None:
        filt.prev_obs.clear()
