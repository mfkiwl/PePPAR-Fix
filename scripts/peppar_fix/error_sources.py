"""M6 competitive error source selection."""

import math


class ErrorSource:
    """One candidate error estimate with its confidence."""
    __slots__ = ('name', 'error_ns', 'confidence_ns')

    def __init__(self, name, error_ns, confidence_ns):
        self.name = name
        self.error_ns = error_ns
        self.confidence_ns = confidence_ns

    def __repr__(self):
        return f"{self.name}({self.error_ns:+.1f}ns ±{self.confidence_ns:.1f})"


def ppp_qerr(dt_rx_ns, tick_ns=8.0, cal_offset_ns=0.0):
    """Compute PPP-derived PPS quantization error from the 125 MHz tick model.

    The receiver fires PPS at the nearest clock tick to the GNSS integer
    second.  Given the PPP filter's precise dt_rx (receiver clock offset
    from GNSS time), the PPS timing error is the signed distance from
    dt_rx to the nearest tick boundary.

    Args:
        dt_rx_ns: receiver clock offset from GNSS time (ns), from PPP filter.
        tick_ns: receiver clock tick period (8.0 ns for 125 MHz F9T).
        cal_offset_ns: calibration offset (ns) to align PPP's dt_rx
            with the receiver's internal clock estimate.  Determined at
            startup by comparing against TIM-TP qErr.

    Returns:
        PPS quantization error in nanoseconds (same sign convention as
        TIM-TP qErr: positive = PPS fires late).
    """
    D_ns = (dt_rx_ns - cal_offset_ns) % 1_000_000_000
    nearest_tick = round(D_ns / tick_ns) * tick_ns
    return nearest_tick - D_ns


class PPPCalibration:
    """Calibrate PPP dt_rx offset against TIM-TP qErr at startup.

    Accumulates (qerr_ppp - qerr_timtp) samples and computes a circular
    mean (period = tick_ns) to determine the constant offset between PPP's
    dt_rx and the receiver's internal clock estimate.

    Requires dt_rx to be stable (consecutive values within a few µs)
    before accepting samples, to avoid calibrating during filter convergence.
    """

    def __init__(self, tick_ns=8.0, min_samples=10, max_dt_rx_jump_ns=1000.0):
        self.tick_ns = tick_ns
        self.min_samples = min_samples
        self.max_dt_rx_jump_ns = max_dt_rx_jump_ns
        self._sin_sum = 0.0
        self._cos_sum = 0.0
        self._n = 0
        self._prev_dt_rx = None
        self._stable_count = 0
        self._stable_threshold = 3  # need 3 consecutive stable epochs
        self.offset_ns = 0.0
        self.calibrated = False

    def add_sample(self, dt_rx_ns, qerr_timtp_ns):
        """Add one comparison sample.  Returns True when calibration is done."""
        if self.calibrated:
            return True

        # Require dt_rx stability before accepting calibration samples.
        if self._prev_dt_rx is not None:
            jump = abs(dt_rx_ns - self._prev_dt_rx)
            if jump < self.max_dt_rx_jump_ns:
                self._stable_count += 1
            else:
                self._stable_count = 0
        self._prev_dt_rx = dt_rx_ns

        if self._stable_count < self._stable_threshold:
            return False

        raw_qerr = ppp_qerr(dt_rx_ns, self.tick_ns, cal_offset_ns=0.0)
        delta = raw_qerr - qerr_timtp_ns
        angle = 2.0 * math.pi * delta / self.tick_ns
        self._sin_sum += math.sin(angle)
        self._cos_sum += math.cos(angle)
        self._n += 1
        if self._n >= self.min_samples:
            mean_angle = math.atan2(self._sin_sum / self._n,
                                    self._cos_sum / self._n)
            self.offset_ns = mean_angle * self.tick_ns / (2.0 * math.pi)
            self.calibrated = True
            return True
        return False


class CarrierPhaseTracker:
    """PHC phase error from PPP + measured inter-oscillator drift rate.

    On most hardware, the PPP receiver (F9T) and the PHC use different
    oscillators.  dt_rx from PPP measures the F9T TCXO drift from GPS;
    the PHC drifts at a different rate.  We measure both rates directly
    and use their difference (D) to correct the Carrier formula:

        carrier_error = (dt_rx - dt_rx_ref) + cumulative_adjfine + D * t

    Where:
    - dt_rx - dt_rx_ref: F9T TCXO phase change from GPS (PPP, ~0.1 ns)
    - cumulative_adjfine: integral of servo corrections applied to PHC
    - D: inter-oscillator differential drift rate (PHC_rate - TCXO_rate)
    - t: elapsed epochs since initialization

    D is estimated directly from two independent measurements:
    - R_tcxo = d(dt_rx)/dt  (from PPP, ~0.1 ppb/epoch precision)
    - R_phc = d(pps_error)/dt - adjfine  (from PPS, ~3.3 ppb/epoch,
      averages to ~0.1 ppb after ~1000 epochs)
    - D = R_phc - R_tcxo

    D improves with runtime: after N epochs, σ_D ≈ 3.3/sqrt(N) ppb.
    Phase error from D uncertainty is bounded by σ_D per epoch (not
    accumulated) because D is re-estimated every epoch from all history.

    No filter lag.  No steady-state phase offset.  The Carrier error
    tracks PPS truth with zero bias while using PPP precision for
    short-term stability.

    Sign convention: positive = PHC ahead of GPS (matches pps_error_ns).
    """

    def __init__(self, stable_threshold=5, max_jump_ns=5000.0):
        self.dt_rx_ref_ns = None
        self.cumulative_adjfine_ns = 0.0
        self.initialized = False
        self._prev_dt_rx_ns = None
        self._stable_count = 0
        self._stable_threshold = stable_threshold
        self._max_jump_ns = max_jump_ns
        # Inter-oscillator drift estimation
        self._epoch_count = 0
        self._sum_d = 0.0       # running sum of per-epoch D samples
        self._sum_d_sq = 0.0    # for variance estimation
        self._n_d = 0           # number of D samples
        self._prev_dt_rx_for_rate = None
        self._prev_pps_error = None
        self._prev_adjfine = None
        self.drift_rate_ppb = 0.0  # current best estimate of D

    def initialize(self, dt_rx_ns):
        """Set the reference dt_rx (called when PHC is aligned to GPS)."""
        self.dt_rx_ref_ns = dt_rx_ns
        self.cumulative_adjfine_ns = 0.0
        self._epoch_count = 0
        self._sum_d = 0.0
        self._sum_d_sq = 0.0
        self._n_d = 0
        self._prev_dt_rx_for_rate = None
        self._prev_pps_error = None
        self._prev_adjfine = None
        self.drift_rate_ppb = 0.0
        self.initialized = True
        self._stable_count = 0

    def try_auto_init(self, dt_rx_ns):
        """Auto-initialize after dt_rx stabilizes (PPP filter convergence).

        Returns True when initialization is complete.
        """
        if self.initialized:
            return True
        if self._prev_dt_rx_ns is not None:
            if abs(dt_rx_ns - self._prev_dt_rx_ns) < self._max_jump_ns:
                self._stable_count += 1
            else:
                self._stable_count = 0
        self._prev_dt_rx_ns = dt_rx_ns
        if self._stable_count >= self._stable_threshold:
            self.initialize(dt_rx_ns)
            return True
        return False

    def accumulate_adjfine(self, adjfine_ppb, dt_s=1.0):
        """Accumulate one epoch of adjfine. Call every discipline epoch."""
        self.cumulative_adjfine_ns += adjfine_ppb * dt_s
        self._epoch_count += 1

    def update_drift_estimate(self, dt_rx_ns, pps_error_ns, adjfine_ppb):
        """Update the inter-oscillator drift rate estimate.

        Call every epoch with current dt_rx, pps_error, and adjfine.
        Computes per-epoch rates for both oscillators and accumulates
        their difference for a running mean estimate of D.
        """
        if not self.initialized:
            return
        if (self._prev_dt_rx_for_rate is not None
                and self._prev_pps_error is not None):
            # R_tcxo: F9T TCXO drift rate (ppb) from PPP
            r_tcxo = dt_rx_ns - self._prev_dt_rx_for_rate

            # R_phc: PHC oscillator drift rate (ppb) from PPS,
            # removing the adjfine contribution we applied
            r_phc = (pps_error_ns - self._prev_pps_error) - self._prev_adjfine

            # D = R_phc - R_tcxo (inter-oscillator differential)
            d_sample = r_phc - r_tcxo
            self._sum_d += d_sample
            self._sum_d_sq += d_sample * d_sample
            self._n_d += 1
            self.drift_rate_ppb = self._sum_d / self._n_d

        self._prev_dt_rx_for_rate = dt_rx_ns
        self._prev_pps_error = pps_error_ns
        self._prev_adjfine = adjfine_ppb

    def drift_rate_sigma(self):
        """Standard error of the drift rate estimate (ppb)."""
        if self._n_d < 2:
            return float('inf')
        mean = self._sum_d / self._n_d
        var = (self._sum_d_sq / self._n_d) - mean * mean
        if var < 0:
            var = 0.0
        import math
        return math.sqrt(var / self._n_d)

    def compute_error(self, dt_rx_ns):
        """Compute PHC phase error with inter-oscillator correction.

        Returns None if not initialized.
        """
        if not self.initialized:
            return None
        raw = (dt_rx_ns - self.dt_rx_ref_ns) + self.cumulative_adjfine_ns
        correction = self.drift_rate_ppb * self._epoch_count
        return raw + correction

    def reset(self, dt_rx_ns):
        """Reset after a PHC restep (phase was re-aligned to GPS)."""
        self.initialize(dt_rx_ns)


def compute_error_sources(pps_error_ns, qerr_ns, dt_rx_ns, dt_rx_sigma_ns,
                          pps_confidence=20.0, qerr_confidence=3.0,
                          carrier_max_sigma=50.0,
                          ticc_error_ns=None, ticc_confidence=None,
                          ppp_cal=None, tick_ns=8.0,
                          carrier_tracker=None):
    """Compute all available error sources and return sorted by confidence.

    Args:
        pps_error_ns: fractional-second PHC error from PPS timestamp
        qerr_ns: quantization error from TIM-TP (None if unavailable)
        dt_rx_ns: receiver clock offset from carrier-phase filter
        dt_rx_sigma_ns: filter's confidence in dt_rx (None if unavailable)
        pps_confidence: assumed PPS-only confidence (ns)
        qerr_confidence: assumed PPS+qErr confidence (ns)
        carrier_max_sigma: max sigma to accept carrier-phase (ns)
        ppp_cal: PPPCalibration instance (None disables PPS+PPP)
        tick_ns: receiver clock tick period (ns)
        carrier_tracker: CarrierPhaseTracker instance (None disables Carrier)

    Returns:
        List of ErrorSource, sorted by confidence (best first).
    """
    sources = []

    sources.append(ErrorSource('PPS', pps_error_ns, pps_confidence))

    if qerr_ns is not None:
        sources.append(ErrorSource('PPS+qErr',
                                   pps_error_ns + qerr_ns,
                                   qerr_confidence))

    if (carrier_tracker is not None and carrier_tracker.initialized
            and dt_rx_sigma_ns is not None
            and dt_rx_sigma_ns < carrier_max_sigma):
        carrier_error = carrier_tracker.compute_error(dt_rx_ns)
        if carrier_error is not None:
            sources.append(ErrorSource('Carrier', carrier_error,
                                       dt_rx_sigma_ns))

    if (dt_rx_sigma_ns is not None and dt_rx_sigma_ns < carrier_max_sigma
            and ppp_cal is not None and ppp_cal.calibrated):
        qerr_ppp_ns = ppp_qerr(dt_rx_ns, tick_ns, ppp_cal.offset_ns)
        # Sanity: qerr_ppp must be within ±tick/2 (by construction it is,
        # but guard against numerical edge cases).
        if abs(qerr_ppp_ns) <= tick_ns / 2 + 0.1:
            sources.append(ErrorSource('PPS+PPP',
                                       pps_error_ns + qerr_ppp_ns,
                                       dt_rx_sigma_ns))

    if ticc_error_ns is not None and ticc_confidence is not None:
        sources.append(ErrorSource('TICC',
                                   ticc_error_ns,
                                   ticc_confidence))

    sources.sort(key=lambda s: s.confidence_ns)
    return sources


def ticc_only_error_source(ticc_error_ns, ticc_confidence):
    """Return a single-source list for experimental TICC-driven servo mode."""
    return [ErrorSource('TICC', ticc_error_ns, ticc_confidence)]
