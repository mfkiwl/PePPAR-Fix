"""4-state DOFreqEst — EKF fusing raw TICC with PPP carrier phase.

Models both oscillators (TCXO + PHC) and the 125 MHz tick quantization
that links them through PPS edges.  No external qErr correction needed.

State vector:
    x = [φ_tcxo, f_tcxo, φ_phc, f_phc]

    φ_tcxo: F9T TCXO phase offset from GPS (ns) — tracked from dt_rx
    f_tcxo: F9T TCXO frequency drift rate (ppb)
    φ_phc:  DO/PHC phase error from GPS (ns) — steered to zero
    f_phc:  DO crystal frequency drift rate (ppb)

Process model (linear):
    φ_tcxo += f_tcxo · dt
    f_tcxo += w_f_tcxo  (random walk)
    φ_phc  += (f_phc + adjfine) · dt
    f_phc  += w_f_phc  (random walk)

Measurements:
    z_ppp  = φ_tcxo + v_ppp                    (PPP dt_rx, ~0.1 ns, linear)
    z_ticc = −φ_phc − qerr(φ_tcxo) + v_ticc   (raw TICC, nonlinear via tick)

    qerr(φ) = φ − round(φ / tick) · tick       (sub-tick TCXO phase)

The nonlinearity is in qerr(): the 125 MHz tick quantization that
determines where the PPS edge fires.  PPP constrains φ_tcxo to ~0.1 ns,
resolving the tick ambiguity — analogous to integer ambiguity resolution
in PPP-AR.  The filter performs qErr correction internally at PPP
precision, which should be better than TIM-TP qErr.

Between tick boundaries (98.75% of epochs when PPP sigma = 0.1 ns):
    ∂qerr/∂φ_tcxo = 1, so H_ticc = [−1, 0, −1, 0]

This couples the TCXO and PHC states in the measurement — the key
insight that makes 4-state fusion work where 2-state failed.
"""

import math
import numpy as np


def _qerr(phi_tcxo_ns, tick_ns=8.0):
    """Compute qErr from TCXO phase: sub-tick residual."""
    return phi_tcxo_ns - round(phi_tcxo_ns / tick_ns) * tick_ns


class DOFreqEst:
    """4-state EKF + LQR for DO frequency steering.

    Drop-in interface: update() takes offset_ns (raw TICC, no qErr),
    returns ppb.  Also takes dt_rx_ns for PPP carrier-phase fusion.
    """

    def __init__(self, sigma_ticc_ns=0.178,
                 sigma_phc_phase_ns=0.92, sigma_phc_freq_ppb=0.01,
                 sigma_tcxo_phase_ns=2.0, sigma_tcxo_freq_ppb=0.1,
                 tick_ns=8.0,
                 max_ppb=62_500_000.0, initial_freq=0.0,
                 initial_dt_rx_ns=None):
        self.max_ppb = max_ppb
        self.tick_ns = tick_ns
        self.dt = 1.0

        # State: [φ_tcxo, f_tcxo, φ_phc, f_phc]
        # f_phc = crystal drift = negative of bootstrap adjfine.
        # Steady state: φ_phc += (f_phc + adjfine) * dt = 0 when
        # adjfine = -f_phc = initial_freq.
        phi_tcxo_init = initial_dt_rx_ns if initial_dt_rx_ns is not None else 0.0
        self.x = np.array([phi_tcxo_init, 0.0, 0.0, -initial_freq])

        # F matrix
        self.F = np.array([
            [1.0, self.dt, 0.0, 0.0],
            [0.0, 1.0,     0.0, 0.0],
            [0.0, 0.0,     1.0, self.dt],
            [0.0, 0.0,     0.0, 1.0],
        ])

        # B: adjfine only affects φ_phc
        self.B = np.array([0.0, 0.0, self.dt, 0.0])

        # Measurement: PPP (linear)
        self.H_ppp = np.array([[1.0, 0.0, 0.0, 0.0]])

        # Measurement: TICC (EKF — Jacobian computed at each step)
        # Between tick boundaries: H_ticc ≈ [-1, 0, -1, 0]
        # This is the linearization of h(x) = -x[2] - qerr(x[0])

        # Process noise
        self.Q = np.diag([
            sigma_tcxo_phase_ns ** 2,
            sigma_tcxo_freq_ppb ** 2,
            sigma_phc_phase_ns ** 2,
            sigma_phc_freq_ppb ** 2,
        ])

        # Measurement noise
        self.R_ticc = np.array([[sigma_ticc_ns ** 2]])

        # Initial covariance
        self.P = np.diag([1e6, 100.0**2, 1000.0**2, 100.0**2])

        # LQR: only PHC states are controllable
        # L[2] = phase gain, L[3] = freq cancellation
        self.L = np.array([0.0, 0.0, 0.05, 1.0])

        self.freq = initial_freq
        # _last_u is the LQR u value.  Engine applies u as adjfine
        # (after double negation: servo returns -u, engine negates).
        # At startup, bootstrap set adjfine = initial_freq, so
        # the last applied u = initial_freq.
        self._last_u = initial_freq
        # TCXO state must be initialized at construction from bootstrap
        # dt_rx to avoid a mid-run measurement model transition that
        # causes divergence.  If dt_rx wasn't available at construction,
        # stay in 2-state mode permanently (no mid-run switch).
        self._tcxo_initialized = initial_dt_rx_ns is not None
        # PHC phase (x[2]) must be seeded from the first TICC measurement
        # before any Kalman update.  Without this, x[2]=0 creates a huge
        # innovation that the coupled H=[-1,0,-1,0] splits between x[0]
        # and x[2], corrupting the TCXO state.
        self._need_phc_seed = self._tcxo_initialized

    def _h_ticc(self, x):
        """Nonlinear TICC measurement function.

        z_ticc = -φ_phc - qerr(φ_tcxo)

        When TCXO not yet initialized, treat as z_ticc = -φ_phc
        (no qerr correction — degrades to 2-state equivalent).
        """
        if self._tcxo_initialized:
            return -x[2] - _qerr(x[0], self.tick_ns)
        else:
            return -x[2]

    def _H_ticc(self, x):
        """Jacobian of h_ticc at x.

        When TCXO is initialized (PPP has provided dt_rx):
            H = [-1, 0, -1, 0] — full coupling, EKF resolves tick.
        When TCXO is NOT initialized:
            H = [0, 0, -1, 0] — degrade to 2-state (TICC observes
            φ_phc only, ignoring unknown TCXO contribution).
        """
        if self._tcxo_initialized:
            return np.array([[-1.0, 0.0, -1.0, 0.0]])
        else:
            return np.array([[0.0, 0.0, -1.0, 0.0]])

    def update(self, offset_ns, dt=1.0,
               dt_rx_ns=None, dt_rx_sigma_ns=None):
        """Process one epoch.

        Args:
            offset_ns: RAW ticc_diff_ns (before qErr correction).
                = -(PEROUT - PPS) = -φ_phc - qerr(φ_tcxo) + noise.
            dt: seconds since last correction.
            dt_rx_ns: PPP carrier-phase dt_rx (ns).
            dt_rx_sigma_ns: 1-sigma uncertainty of dt_rx (~0.1 ns).

        Returns:
            Frequency in ppb to apply via adjfine.
        """
        if dt != self.dt:
            self.dt = dt
            self.F[0, 1] = dt
            self.F[2, 3] = dt
            self.B[2] = dt

        # ── Seed φ_phc from first TICC measurement ──
        # z_ticc = -φ_phc - qerr(φ_tcxo) → φ_phc = -z - qerr(φ_tcxo)
        # Must happen before any Kalman update so the first innovation
        # is near zero and doesn't corrupt the TCXO state.
        if self._need_phc_seed:
            self.x[2] = -offset_ns - _qerr(self.x[0], self.tick_ns)
            self._need_phc_seed = False

        # ── Adaptive Q: boost during pull-in ──
        phc_abs = abs(self.x[2])
        if phc_abs > 50.0:
            q_scale = 10.0
        elif phc_abs < 10.0:
            q_scale = 1.0
        else:
            q_scale = 1.0 + 9.0 * (phc_abs - 10.0) / 40.0
        Q_scaled = self.Q.copy()
        # Only boost PHC states during pull-in (TCXO states converge from PPP)
        Q_scaled[2, 2] *= q_scale
        Q_scaled[3, 3] *= q_scale

        # ── EKF predict ──
        x_pred = self.F @ self.x + self.B * self._last_u
        P_pred = self.F @ self.P @ self.F.T + Q_scaled * dt

        # ── Sequential updates: PPP first, then TICC ──
        # Order matters!  TICC H=[-1,0,-1,0] creates P[0,2] correlation.
        # If PPP H=[1,0,0,0] runs after TICC, K_ppp[2] = P[2,0]/S ≈ -1,
        # which applies the PPP innovation to x[2] and destroys the PHC
        # state.  PPP first keeps P[0,2]=0 so K_ppp[2]=0.  Then TICC
        # correctly directs its innovation to x[2] (since PPP pinned x[0]).

        # ── Kalman update 1: PPP dt_rx (linear) ──
        if dt_rx_ns is not None and dt_rx_sigma_ns is not None:
            R_ppp = np.array([[dt_rx_sigma_ns ** 2]])
            innov_ppp = dt_rx_ns - (self.H_ppp @ x_pred).item()
            S_ppp = (self.H_ppp @ P_pred @ self.H_ppp.T + R_ppp).item()
            K_ppp = (P_pred @ self.H_ppp.T) / S_ppp

            x_pred = x_pred + K_ppp.flatten() * innov_ppp
            P_pred = P_pred - np.outer(K_ppp.flatten(), K_ppp.flatten()) * S_ppp

        # ── Kalman update 2: raw TICC (nonlinear) ──
        z_ticc = offset_ns
        h_pred = self._h_ticc(x_pred)
        innov_ticc = z_ticc - h_pred
        H_ticc = self._H_ticc(x_pred)
        S_ticc = (H_ticc @ P_pred @ H_ticc.T + self.R_ticc).item()
        K_ticc = (P_pred @ H_ticc.T) / S_ticc

        self.x = x_pred + K_ticc.flatten() * innov_ticc
        self.P = P_pred - np.outer(K_ticc.flatten(), K_ticc.flatten()) * S_ticc

        # ── LQR control ──
        # Only L[2] (φ_phc) and L[3] (f_phc) are nonzero.
        # Sign convention: return -u, engine applies -(-u) = u.
        u = -(self.L @ self.x)
        adjfine = -u

        adjfine = max(-self.max_ppb, min(self.max_ppb, adjfine))

        import logging
        _log = logging.getLogger("DOFreqEst")
        _log.info("  EKF: x=[%.1f, %.3f, %.1f, %.3f] "
                  "innov_ticc=%.3f u=%.3f adjfine=%.3f "
                  "last_u=%.3f dt=%.1f z_ticc=%.3f h=%.3f "
                  "ppp=%s",
                  self.x[0], self.x[1], self.x[2], self.x[3],
                  innov_ticc, u, adjfine,
                  self._last_u, dt, z_ticc, h_pred,
                  f"{dt_rx_ns:.1f}" if dt_rx_ns is not None else "None")

        self._last_u = u
        self.freq = adjfine
        return self.freq

    def reset(self, current_freq):
        self.x = np.array([0.0, 0.0, 0.0, 0.0])
        self.P = np.diag([1e6, 100.0**2, 1000.0**2, 100.0**2])
        self._last_u = 0.0
        self._tcxo_initialized = False
        self.freq = current_freq

    @property
    def estimated_phase_ns(self):
        return self.x[2]

    @property
    def estimated_freq_ppb(self):
        return self.x[3]

    @property
    def estimated_tcxo_phase_ns(self):
        return self.x[0]

    @property
    def estimated_tcxo_freq_ppb(self):
        return self.x[1]

    @property
    def phase_uncertainty_ns(self):
        return math.sqrt(max(0, self.P[2, 2]))

    @property
    def freq_uncertainty_ppb(self):
        return math.sqrt(max(0, self.P[3, 3]))
