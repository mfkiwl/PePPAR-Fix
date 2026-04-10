"""4-state DOFreqEst — Disciplined Oscillator Frequency Estimator.

Fuses TICC+qErr measurements with PPP carrier-phase dt_rx in a single
Kalman filter that explicitly models both oscillators (F9T TCXO and
i226 DO/PHC).  This is the measurement fusion from architecture-vision.md.

State vector (4 states):
    x = [δ_tcxo, f_tcxo, δ_phc, f_phc]

    δ_tcxo: F9T TCXO phase offset from GPS (ns)
    f_tcxo: F9T TCXO frequency drift rate (ppb)
    δ_phc:  DO/PHC phase offset from GPS (ns) — what we steer to zero
    f_phc:  DO crystal frequency drift rate (ppb) — the unknown

Process model (discrete, per-epoch):
    δ_tcxo[n+1] = δ_tcxo[n] + f_tcxo[n] · dt + w_tcxo
    f_tcxo[n+1] = f_tcxo[n] + w_f_tcxo
    δ_phc[n+1]  = δ_phc[n] + (f_phc[n] + u[n]) · dt + w_phc
    f_phc[n+1]  = f_phc[n] + w_f_phc

    u[n] = adjfine correction we apply (ppb)

Measurement model:
    z_ppp[n]  = δ_tcxo[n] + v_ppp       (PPP dt_rx, ~0.1 ns)
    z_ticc[n] = δ_phc[n] - δ_tcxo[n] + v_ticc  (TICC+qErr, ~0.178 ns)

Why 4 states instead of 2:
    The 2-state [phase, freq] filter conflates f_phc and f_tcxo into
    a single frequency state.  This prevents using PPP carrier-phase
    information: dt_rx observes f_tcxo, but the 2-state freq tracks
    (f_phc - f_tcxo).  Injecting a measurement of one component when
    the state represents their difference breaks convergence.

    With explicit separation, dt_rx constrains δ_tcxo and f_tcxo,
    TICC constrains (δ_phc - δ_tcxo), and the filter estimates all
    four states optimally.  The inter-oscillator coupling falls out
    of the math.

    Satellite clocks couple to the DO *through* the TCXO: PPP carrier
    phase → δ_tcxo → (via PPS/TICC coupling) → δ_phc.

Why this is better than the 2-state Kalman + CarrierPhaseTracker:
    The CarrierPhaseTracker estimates the drift rate D = f_phc - f_tcxo
    separately, accumulating adjfine corrections in a running sum.
    This cascade introduces bias that the servo can't correct.  The
    4-state filter estimates all parameters jointly — no cascade, no
    accumulated bias, no separate D estimation.

PPS as ambiguity resolution:
    The PPS edges serve the same role as integer ambiguity resolution
    in PPP-AR.  The carrier phase gives continuous ~0.1 ns tracking of
    δ_tcxo, but with an unknown absolute offset.  Every second, the
    PPS edge says "the TCXO thinks it's the GPS second boundary" and
    the TICC says "and the PHC reads this."  That pins (δ_phc - δ_tcxo)
    absolutely, resolving the ambiguity.

Noise parameters (from lab measurements 2026-04-09/10):
    sigma_ticc_ns:    TICC+qErr measurement noise (0.178 ns)
    sigma_phc_phase:  DO phase noise per epoch (0.92 ns)
    sigma_phc_freq:   DO frequency random walk (~0.01 ppb)
    sigma_tcxo_phase: F9T TCXO phase noise per epoch (~2.0 ns)
    sigma_tcxo_freq:  F9T TCXO frequency random walk (~0.1 ppb)
"""

import math
import numpy as np


class DOFreqEst:
    """4-state Kalman filter + LQR for DO frequency steering.

    Drop-in interface with KalmanServo/PIServo: update() takes offset_ns,
    returns ppb.  Additionally accepts dt_rx_ns and dt_rx_sigma_ns for
    the PPP carrier-phase measurement.
    """

    def __init__(self, sigma_ticc_ns=0.178,
                 sigma_phc_phase_ns=0.92, sigma_phc_freq_ppb=0.01,
                 sigma_tcxo_phase_ns=2.0, sigma_tcxo_freq_ppb=0.1,
                 max_ppb=62_500_000.0, initial_freq=0.0):
        """
        Args:
            sigma_ticc_ns: TICC+qErr measurement noise (ns).
            sigma_phc_phase_ns: DO phase noise per epoch (ns).
            sigma_phc_freq_ppb: DO frequency random walk per epoch (ppb).
            sigma_tcxo_phase_ns: F9T TCXO phase noise per epoch (ns).
            sigma_tcxo_freq_ppb: F9T TCXO frequency random walk (ppb).
            max_ppb: maximum adjfine magnitude.
            initial_freq: bootstrap adjfine to seed the PHC frequency.
        """
        self.max_ppb = max_ppb
        self.dt = 1.0

        # State: [δ_tcxo, f_tcxo, δ_phc, f_phc]
        # Initialize: TCXO at PPP's dt_rx (unknown until first measurement),
        # PHC at TICC offset, frequencies at zero (filter will learn).
        self.x = np.array([0.0, 0.0, 0.0, 0.0])

        # State transition matrix
        self.F = np.array([
            [1.0, self.dt, 0.0, 0.0],   # δ_tcxo += f_tcxo * dt
            [0.0, 1.0,     0.0, 0.0],   # f_tcxo (random walk)
            [0.0, 0.0,     1.0, self.dt],  # δ_phc += (f_phc + u) * dt
            [0.0, 0.0,     0.0, 1.0],   # f_phc (random walk)
        ])

        # Control input matrix: adjfine affects only δ_phc
        self.B = np.array([0.0, 0.0, self.dt, 0.0])

        # Measurement matrices
        # PPP dt_rx observes δ_tcxo: H_ppp = [1, 0, 0, 0]
        self.H_ppp = np.array([[1.0, 0.0, 0.0, 0.0]])
        # TICC observes (δ_phc - δ_tcxo): H_ticc = [-1, 0, 1, 0]
        self.H_ticc = np.array([[-1.0, 0.0, 1.0, 0.0]])

        # Process noise
        self.Q = np.diag([
            sigma_tcxo_phase_ns ** 2,
            sigma_tcxo_freq_ppb ** 2,
            sigma_phc_phase_ns ** 2,
            sigma_phc_freq_ppb ** 2,
        ])

        # Measurement noise
        self.R_ticc = np.array([[sigma_ticc_ns ** 2]])
        # R_ppp is set dynamically from dt_rx_sigma_ns each epoch

        # Initial covariance — start uncertain
        self.P = np.diag([1000.0**2, 100.0**2, 1000.0**2, 100.0**2])

        # LQR gain: we want to minimize δ_phc (state index 2) and
        # cancel f_phc (state index 3).  The control u only enters
        # through B = [0, 0, dt, 0], so only the PHC states are
        # controllable.  Use a simple analytical gain:
        #   L_phase (ppb per ns of δ_phc error) = 0.05
        #   L_freq (full cancellation of f_phc) = 1.0
        # Same as the 2-state Kalman's proven gains.
        self.L = np.array([0.0, 0.0, 0.05, 1.0])

        self.freq = initial_freq
        self._last_u = 0.0
        self._initialized_tcxo = False

    def update(self, offset_ns, dt=1.0,
               dt_rx_ns=None, dt_rx_sigma_ns=None):
        """Process one epoch. Returns frequency adjustment in ppb.

        Args:
            offset_ns: TICC+qErr measurement of (δ_phc - δ_tcxo) in ns.
            dt: seconds since last correction.
            dt_rx_ns: PPP carrier-phase dt_rx (ns).  Observes δ_tcxo.
                When None, only the TICC measurement is used (graceful
                degradation — the filter runs as a 2-state equivalent).
            dt_rx_sigma_ns: 1-sigma uncertainty of dt_rx (~0.1 ns).

        Returns:
            Frequency in ppb to apply via adjfine.
        """
        # Update dt-dependent matrices if interval changed
        if dt != self.dt:
            self.dt = dt
            self.F[0, 1] = dt  # δ_tcxo += f_tcxo * dt
            self.F[2, 3] = dt  # δ_phc += f_phc * dt
            self.B[2] = dt     # adjfine affects δ_phc

        # ── Initialize TCXO state from first dt_rx ──
        # On the first dt_rx, set δ_tcxo to dt_rx so the filter starts
        # with a reasonable absolute offset.  Without this, the TICC
        # measurement (δ_phc - δ_tcxo) can't separate the two phases.
        if not self._initialized_tcxo and dt_rx_ns is not None:
            self.x[0] = dt_rx_ns
            # δ_phc = TICC + δ_tcxo (from the first measurement)
            self.x[2] = offset_ns + dt_rx_ns
            self._initialized_tcxo = True

        # ── Adaptive Q: boost during pull-in ──
        phc_phase_abs = abs(self.x[2])
        if phc_phase_abs > 50.0:
            q_scale = 10.0
        elif phc_phase_abs < 10.0:
            q_scale = 1.0
        else:
            q_scale = 1.0 + 9.0 * (phc_phase_abs - 10.0) / 40.0
        Q_scaled = self.Q * q_scale

        # ── Kalman predict ──
        x_pred = self.F @ self.x + self.B * self._last_u
        P_pred = self.F @ self.P @ self.F.T + Q_scaled * dt

        # ── Kalman update: TICC measurement ──
        # z_ticc = (δ_phc - δ_tcxo) + noise
        z_ticc = offset_ns
        innov_ticc = z_ticc - (self.H_ticc @ x_pred).item()
        S_ticc = (self.H_ticc @ P_pred @ self.H_ticc.T + self.R_ticc).item()
        K_ticc = (P_pred @ self.H_ticc.T) / S_ticc

        self.x = x_pred + K_ticc.flatten() * innov_ticc
        self.P = P_pred - np.outer(K_ticc.flatten(), K_ticc.flatten()) * S_ticc

        # ── Kalman update: PPP dt_rx (sequential, when available) ──
        # z_ppp = δ_tcxo + noise
        if dt_rx_ns is not None and dt_rx_sigma_ns is not None:
            R_ppp = np.array([[dt_rx_sigma_ns ** 2]])
            innov_ppp = dt_rx_ns - (self.H_ppp @ self.x).item()
            S_ppp = (self.H_ppp @ self.P @ self.H_ppp.T + R_ppp).item()
            K_ppp = (self.P @ self.H_ppp.T) / S_ppp

            self.x = self.x + K_ppp.flatten() * innov_ppp
            self.P = self.P - np.outer(K_ppp.flatten(), K_ppp.flatten()) * S_ppp

        # ── LQR control ──
        # u = -L @ x: only L[2] and L[3] are nonzero (PHC states).
        # L[2] * δ_phc: proportional correction for PHC phase error.
        # L[3] * f_phc: full cancellation of estimated crystal drift.
        #
        # Sign convention (matching KalmanServo):
        # u is the raw control.  We return -u as adjfine because the
        # engine calls adjfine = -servo.update().
        u = -(self.L @ self.x)
        adjfine = -u  # engine will negate this back

        # Clamp
        adjfine = max(-self.max_ppb, min(self.max_ppb, adjfine))

        self._last_u = u
        self.freq = adjfine
        return self.freq

    def reset(self, current_freq):
        """Reset for bumpless transfer (e.g., after PHC re-bootstrap)."""
        self.x = np.array([0.0, 0.0, 0.0, 0.0])
        self.P = np.diag([1000.0**2, 100.0**2, 1000.0**2, 100.0**2])
        self._last_u = 0.0
        self._initialized_tcxo = False
        self.freq = current_freq

    @property
    def estimated_phase_ns(self):
        """Current best estimate of PHC phase error (ns)."""
        return self.x[2]

    @property
    def estimated_freq_ppb(self):
        """Current best estimate of PHC frequency drift (ppb)."""
        return self.x[3]

    @property
    def estimated_tcxo_phase_ns(self):
        """Current best estimate of TCXO phase offset (ns)."""
        return self.x[0]

    @property
    def estimated_tcxo_freq_ppb(self):
        """Current best estimate of TCXO frequency drift (ppb)."""
        return self.x[1]

    @property
    def phase_uncertainty_ns(self):
        """1-sigma uncertainty of δ_phc (ns)."""
        return math.sqrt(max(0, self.P[2, 2]))

    @property
    def freq_uncertainty_ppb(self):
        """1-sigma uncertainty of f_phc (ppb)."""
        return math.sqrt(max(0, self.P[3, 3]))
