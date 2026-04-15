#!/usr/bin/env python3
"""
ppp_ar.py — PPP-AR via Melbourne-Wubbena wide-lane + narrow-lane resolution.

Two-step ambiguity resolution that works with the existing IF PPPFilter:

  Step 1: MelbourneWubbenaTracker computes the geometry-free MW combination
          per satellite and fixes the wide-lane integer N_WL = N1 - N2.
          Converges in ~60 s from code-phase averaging.

  Step 2: NarrowLaneResolver extracts the narrow-lane integer N1 from
          the PPPFilter's float IF ambiguity using the known N_WL:
            N1 = (A_IF - alpha2 * lambda_WL * N_WL) / lambda_NL
          Fixes N1 when |frac| < threshold and sigma is small.

No changes to PPPFilter internals — AR sits alongside and constrains
the existing float ambiguity states.
"""

import logging
import math

import numpy as np

from solve_pseudorange import C
from solve_ppp import N_BASE
from lambda_ar import lambda_resolve, lambda_decorrelate, bootstrap_success_rate

log = logging.getLogger(__name__)


class MelbourneWubbenaTracker:
    """Per-satellite Melbourne-Wubbena wide-lane averaging and fixing.

    MW = (f1*phi1 - f2*phi2)/(f1 - f2) - (f1*P1 + f2*P2)/(f1 + f2)
       = lambda_WL * N_WL  +  code_noise

    After averaging, N_WL = round(MW_avg / lambda_WL).
    """

    def __init__(self, tau_s=60.0, fix_threshold=0.15, min_epochs=60):
        self.tau_s = tau_s              # exponential averaging time constant
        self.fix_threshold = fix_threshold  # |frac| < this to fix
        self.min_epochs = min_epochs    # minimum epochs before fixing
        self._state = {}   # sv -> {mw_avg, n_epochs, n_wl, fixed, f1, f2}

    def update(self, sv, phi1_cyc, phi2_cyc, pr1_m, pr2_m, f1, f2):
        """Update MW average for one satellite.

        Args:
            phi1_cyc, phi2_cyc: carrier phase in cycles (bias-corrected)
            pr1_m, pr2_m: pseudorange in meters
            f1, f2: frequencies in Hz
        """
        lambda_wl = C / (f1 - f2)

        # MW combination (meters)
        mw = (f1 * phi1_cyc * (C / f1) - f2 * phi2_cyc * (C / f2)) / (f1 - f2) \
           - (f1 * pr1_m + f2 * pr2_m) / (f1 + f2)

        if sv not in self._state:
            self._state[sv] = {
                'mw_avg': mw,
                'n_epochs': 1,
                'n_wl': None,
                'fixed': False,
                'f1': f1,
                'f2': f2,
            }
            return

        s = self._state[sv]
        # Exponential moving average
        alpha = 1.0 / max(1.0, min(s['n_epochs'] + 1, self.tau_s))
        s['mw_avg'] = (1.0 - alpha) * s['mw_avg'] + alpha * mw
        s['n_epochs'] += 1

        if s['fixed']:
            return

        if s['n_epochs'] >= self.min_epochs:
            n_wl_float = s['mw_avg'] / lambda_wl
            frac = abs(n_wl_float - round(n_wl_float))
            if frac < self.fix_threshold:
                s['n_wl'] = round(n_wl_float)
                s['fixed'] = True
                log.info("WL fixed: %s N_WL=%d (frac=%.3f, %d epochs)",
                         sv, s['n_wl'], frac, s['n_epochs'])

    def get_wl(self, sv):
        """Return fixed N_WL for satellite, or None if not yet fixed."""
        s = self._state.get(sv)
        if s is not None and s['fixed']:
            return s['n_wl']
        return None

    def get_freqs(self, sv):
        """Return (f1, f2) for satellite, or None."""
        s = self._state.get(sv)
        if s is not None:
            return s['f1'], s['f2']
        return None

    def reset(self, sv):
        """Reset state for a satellite (e.g. after cycle slip)."""
        self._state.pop(sv, None)

    @property
    def n_fixed(self):
        return sum(1 for s in self._state.values() if s['fixed'])

    def summary(self):
        n_total = len(self._state)
        return f"WL: {self.n_fixed}/{n_total} fixed"


class NarrowLaneResolver:
    """Resolve narrow-lane integer N1 from float IF ambiguity + fixed N_WL.

    N1 = (A_IF - alpha2 * lambda_WL * N_WL) / lambda_NL

    Fixes the PPPFilter ambiguity state when N1 is close to integer.
    """

    def __init__(self, frac_threshold=0.10, sigma_threshold=0.12):
        self.frac_threshold = frac_threshold    # |N1_frac| < this to fix
        self.sigma_threshold = sigma_threshold  # sigma_N1 < this to fix
        self._fixed = {}  # sv -> {'n1': int, 'a_if_fixed': float}
        self.last_ratio = 0.0   # LAMBDA ratio test value (0 = not attempted)
        self.last_method = ""   # "lambda" or "rounding"
        self.last_success_rate = 0.0  # bootstrap success rate (0 = not computed)

    def attempt(self, filt, mw_tracker):
        """Try to fix ambiguities in the PPPFilter.

        Also re-constrains already-fixed ambiguities every epoch to prevent
        drift from process noise.

        Args:
            filt: PPPFilter instance with .x, .P, .sv_to_idx
            mw_tracker: MelbourneWubbenaTracker with fixed N_WL values

        Returns:
            dict of newly fixed satellites: {sv: n1_int}
        """
        newly_fixed = {}

        # Re-constrain already-fixed ambiguities every epoch
        for sv, fix_info in list(self._fixed.items()):
            amb_idx = filt.sv_to_idx.get(sv)
            if amb_idx is None:
                continue
            si = N_BASE + amb_idx
            if si < len(filt.x):
                self._apply_fix(filt, si, fix_info['a_if_fixed'])

        # Collect WL-fixed, not-yet-NL-fixed candidates with their params
        cands = []  # list of (sv, amb_idx, si, f1, f2, n_wl, lambda_wl, lambda_nl, alpha2)
        for sv, amb_idx in list(filt.sv_to_idx.items()):
            if sv in self._fixed:
                continue
            n_wl = mw_tracker.get_wl(sv)
            if n_wl is None:
                continue
            freqs = mw_tracker.get_freqs(sv)
            if freqs is None:
                continue
            f1, f2 = freqs
            lambda_wl = C / (f1 - f2)
            lambda_nl = C / (f1 + f2)
            alpha2 = f2**2 / (f1**2 - f2**2)
            si = N_BASE + amb_idx
            if si >= len(filt.x):
                continue
            cands.append((sv, amb_idx, si, f1, f2, n_wl,
                          lambda_wl, lambda_nl, alpha2))

        # Pre-screen: loose reject for obviously unconverged ambiguities
        screened = []
        for c in cands:
            sv, amb_idx, si, f1, f2, n_wl, lambda_wl, lambda_nl, alpha2 = c
            a_if_float = filt.x[si]
            sigma_a = math.sqrt(filt.P[si, si]) if si < filt.P.shape[0] else 999.0
            n1_float = (a_if_float - alpha2 * lambda_wl * n_wl) / lambda_nl
            n1_frac = abs(n1_float - round(n1_float))
            sigma_n1 = sigma_a / lambda_nl
            if n1_frac < 0.25 and sigma_n1 < 1.0:
                screened.append(c)

        # Try LAMBDA when >= 4 candidates pass pre-screen
        if len(screened) >= 4:
            newly_fixed = self._attempt_lambda(filt, screened)
            if newly_fixed:
                return newly_fixed

        # Fallback: per-satellite rounding for < 4 or if LAMBDA failed
        self.last_method = "rounding"
        for c in screened:
            sv, amb_idx, si, f1, f2, n_wl, lambda_wl, lambda_nl, alpha2 = c
            a_if_float = filt.x[si]
            sigma_a = math.sqrt(filt.P[si, si]) if si < filt.P.shape[0] else 999.0
            n1_float = (a_if_float - alpha2 * lambda_wl * n_wl) / lambda_nl
            n1_frac = abs(n1_float - round(n1_float))
            sigma_n1 = sigma_a / lambda_nl

            if n1_frac < self.frac_threshold and sigma_n1 < self.sigma_threshold:
                n1_int = round(n1_float)
                a_if_fixed = lambda_nl * n1_int + alpha2 * lambda_wl * n_wl
                self._apply_fix(filt, si, a_if_fixed)
                self._fixed[sv] = {'n1': n1_int, 'a_if_fixed': a_if_fixed}
                newly_fixed[sv] = n1_int
                log.info("NL fixed (rounding): %s N1=%d (frac=%.3f, "
                         "sigma_N1=%.3f, A_IF: %.4f → %.4f m)",
                         sv, n1_int, n1_frac, sigma_n1,
                         a_if_float, a_if_fixed)

        return newly_fixed

    def _attempt_lambda(self, filt, screened):
        """Try LAMBDA resolution on screened candidates.

        Args:
            filt: PPPFilter instance
            screened: list of (sv, amb_idx, si, f1, f2, n_wl,
                      lambda_wl, lambda_nl, alpha2) tuples

        Returns:
            dict of newly fixed satellites, or empty dict if failed
        """
        # Build NL float vector and covariance from filter state
        svs = []
        n1_floats = []
        state_indices = []
        params = []  # (lambda_wl, lambda_nl, alpha2, n_wl) per sat

        for c in screened:
            sv, amb_idx, si, f1, f2, n_wl, lambda_wl, lambda_nl, alpha2 = c
            a_if_float = filt.x[si]
            n1_float = (a_if_float - alpha2 * lambda_wl * n_wl) / lambda_nl
            svs.append(sv)
            n1_floats.append(n1_float)
            state_indices.append(si)
            params.append((lambda_wl, lambda_nl, alpha2, n_wl))

        n1_vec = np.array(n1_floats)
        si_arr = np.array(state_indices)

        # Extract NL covariance from IF ambiguity covariance
        # Qa_IF = P[si, si] submatrix; Qa_NL = Qa_IF / (lambda_nl^2)
        # When all sats have the same lambda_nl (same constellation+freq),
        # this is exact.  For mixed constellations, scale per element.
        n_amb = len(svs)
        Qa_nl = np.zeros((n_amb, n_amb))
        for i in range(n_amb):
            for j in range(n_amb):
                Qa_nl[i, j] = filt.P[si_arr[i], si_arr[j]] / (
                    params[i][1] * params[j][1])  # lambda_nl_i * lambda_nl_j

        # Compute bootstrap success rate for diagnostics before resolving
        eigvals = np.linalg.eigvalsh(Qa_nl)
        Qa_reg = Qa_nl.copy()
        if eigvals.min() <= 0:
            Qa_reg += np.eye(n_amb) * max(1e-10, -eigvals.min() * 2)
        _, _, D_diag = lambda_decorrelate(Qa_reg)
        self.last_success_rate = bootstrap_success_rate(D_diag)

        fixed_vec, n_fixed, ratio, mask = lambda_resolve(
            n1_vec, Qa_nl, ratio_threshold=2.0, min_fixed=4,
            min_success_rate=0.999)

        if fixed_vec is None:
            self.last_ratio = ratio
            if self.last_success_rate < 0.999:
                log.debug("LAMBDA skipped: bootstrap P=%.4f (need 0.999), "
                          "%d candidates", self.last_success_rate, n_amb)
            return {}

        self.last_ratio = ratio
        self.last_method = "lambda"
        newly_fixed = {}
        for i in range(n_amb):
            if not mask[i]:
                continue
            sv = svs[i]
            n1_int = int(fixed_vec[i])
            lambda_wl, lambda_nl, alpha2, n_wl = params[i]
            a_if_fixed = lambda_nl * n1_int + alpha2 * lambda_wl * n_wl
            si = state_indices[i]

            self._apply_fix(filt, si, a_if_fixed)
            self._fixed[sv] = {'n1': n1_int, 'a_if_fixed': a_if_fixed}
            newly_fixed[sv] = n1_int
            log.info("NL fixed (LAMBDA): %s N1=%d (ratio=%.1f, P=%.4f, "
                     "%d/%d fixed, A_IF: %.4f → %.4f m)",
                     sv, n1_int, ratio, self.last_success_rate,
                     n_fixed, n_amb, filt.x[si], a_if_fixed)

        return newly_fixed

    def _apply_fix(self, filt, state_idx, fixed_value):
        """Constrain a PPPFilter ambiguity state to a fixed value.

        Uses a tight pseudo-measurement update: H = [0...1...0],
        z = fixed_value, R = (0.001 m)^2.  This pulls the state to
        the fixed value and collapses its covariance without altering
        the filter's structure (the state remains, just tightly constrained).
        """
        n = len(filt.x)
        H = np.zeros((1, n))
        H[0, state_idx] = 1.0
        z = np.array([fixed_value - filt.x[state_idx]])
        R = np.array([[0.001**2]])  # 1 mm sigma — effectively fixed

        S = H @ filt.P @ H.T + R
        K = filt.P @ H.T / S[0, 0]
        filt.x = filt.x + K[:, 0] * z[0]
        I_KH = np.eye(n) - K @ H
        filt.P = I_KH @ filt.P @ I_KH.T + K @ R @ K.T
        filt.P = 0.5 * (filt.P + filt.P.T)

    def is_fixed(self, sv):
        return sv in self._fixed

    def unfix(self, sv):
        """Remove a fix (e.g. after cycle slip detected)."""
        self._fixed.pop(sv, None)

    def integrality(self, filt, mw_tracker):
        """Compute corrected integrality metric for all satellites.

        Returns list of (sv, n1_frac, sigma_n1, fixed) for diagnostics.
        """
        results = []
        for sv, amb_idx in filt.sv_to_idx.items():
            n_wl = mw_tracker.get_wl(sv)
            if n_wl is None:
                continue

            freqs = mw_tracker.get_freqs(sv)
            if freqs is None:
                continue
            f1, f2 = freqs

            lambda_wl = C / (f1 - f2)
            lambda_nl = C / (f1 + f2)
            alpha2 = f2**2 / (f1**2 - f2**2)

            si = N_BASE + amb_idx
            if si >= len(filt.x):
                continue

            a_if_float = filt.x[si]
            sigma_a = math.sqrt(filt.P[si, si]) if si < filt.P.shape[0] else 999.0

            n1_float = (a_if_float - alpha2 * lambda_wl * n_wl) / lambda_nl
            n1_frac = n1_float - round(n1_float)
            sigma_n1 = sigma_a / lambda_nl

            results.append((sv, n1_frac, sigma_n1, sv in self._fixed))

        return results

    @property
    def n_fixed(self):
        return len(self._fixed)

    def summary(self):
        s = f"NL: {self.n_fixed} fixed"
        if self.last_ratio > 0:
            s += f" R={self.last_ratio:.1f}"
        if self.last_success_rate > 0:
            s += f" P={self.last_success_rate:.3f}"
        return s
