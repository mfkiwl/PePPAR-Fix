"""In-band DO noise estimation from discipline gaps.

When the adaptive discipline scheduler extends its interval, epochs
between corrections are genuinely free-running: the DO drifts with no
adjfine change applied.  After removing the expected linear drift
(constant adjfine × dt), the residual is pure DO phase noise.

Two channels:
- Gap channel: epochs where no correction was applied.  Pure DO noise.
- Correction channel: residuals after removing expected frequency step.
  If corrections are truly noise-free (as measured), both channels
  should agree.  Divergence indicates DO stress or write latency.

Computes running overlapping Allan deviation (ADEV) at tau = 1, 2, 4, ...
seconds from the residual phase samples.
"""

import logging
import math
import time

log = logging.getLogger("peppar_fix.noise_estimator")


class InBandNoiseEstimator:
    """Estimates DO noise floor from discipline gap measurements.

    Feed every epoch's phase error and whether a correction was applied.
    The estimator detrends phase using the current adjfine and computes
    ADEV from the residuals.

    Args:
        min_gap_epochs: minimum gap epochs before computing statistics
        max_history: maximum phase samples to retain per channel
    """

    def __init__(self, min_gap_epochs=10, max_history=7200):
        self._min_gap_epochs = min_gap_epochs
        self._max_history = max_history

        # Gap channel: phase residuals during no-correction epochs
        self._gap_phases = []       # detrended phase (ns)
        self._gap_times = []        # monotonic timestamps

        # Correction channel: phase residuals including correction epochs
        self._corr_phases = []
        self._corr_times = []

        # Running state
        self._last_correction_mono = None
        self._last_adjfine_ppb = None
        self._gap_tdev = {}         # tau -> tdev_ns
        self._corr_tdev = {}
        self._gap_count = 0
        self._corr_count = 0

        # Detrending: accumulated phase from known frequency
        self._phase_acc_ns = 0.0
        self._prev_mono = None

    def feed(self, phase_error_ns, adjfine_ppb, corrected_this_epoch,
             mono=None):
        """Feed one epoch's measurement.

        Args:
            phase_error_ns: measured phase error (from best source)
            adjfine_ppb: current adjfine setting
            corrected_this_epoch: True if a frequency correction was
                applied this epoch
            mono: CLOCK_MONOTONIC timestamp (default: now)
        """
        if mono is None:
            mono = time.monotonic()

        # Detrend: remove expected phase accumulation from adjfine drift.
        # If adjfine is A ppb, phase drifts A ns/s.  Between epochs,
        # expected_phase_change = adjfine_ppb * dt_s.
        if self._prev_mono is not None and self._last_adjfine_ppb is not None:
            dt_s = mono - self._prev_mono
            if 0 < dt_s < 30:
                expected_drift_ns = self._last_adjfine_ppb * dt_s
                self._phase_acc_ns += expected_drift_ns

        residual_ns = phase_error_ns - self._phase_acc_ns
        self._prev_mono = mono
        self._last_adjfine_ppb = adjfine_ppb

        # Correction channel: all epochs
        self._corr_phases.append(residual_ns)
        self._corr_times.append(mono)
        self._corr_count += 1
        if len(self._corr_phases) > self._max_history:
            self._corr_phases.pop(0)
            self._corr_times.pop(0)

        # Gap channel: only non-correction epochs
        if not corrected_this_epoch:
            self._gap_phases.append(residual_ns)
            self._gap_times.append(mono)
            self._gap_count += 1
            if len(self._gap_phases) > self._max_history:
                self._gap_phases.pop(0)
                self._gap_times.pop(0)
        else:
            # Reset detrending accumulator on correction — the correction
            # changes the frequency, so phase accumulation restarts.
            self._phase_acc_ns = 0.0
            self._last_correction_mono = mono

        # Periodically recompute ADEV (every 60 samples)
        if self._corr_count % 60 == 0:
            self._recompute_tdev()

    def _recompute_tdev(self):
        """Recompute overlapping ADEV for both channels."""
        self._gap_tdev = _compute_tdev(self._gap_phases)
        self._corr_tdev = _compute_tdev(self._corr_phases)

    @property
    def gap_tdev(self):
        """Gap channel TDEV: {tau_s: tdev_ns}."""
        return dict(self._gap_tdev)

    @property
    def correction_tdev(self):
        """Correction channel TDEV: {tau_s: tdev_ns}."""
        return dict(self._corr_tdev)

    @property
    def gap_samples(self):
        return len(self._gap_phases)

    @property
    def total_samples(self):
        return len(self._corr_phases)

    def summary(self):
        """One-line summary for logging."""
        parts = [f"gap={self.gap_samples} corr={self.total_samples}"]
        if self._gap_tdev:
            tau1 = self._gap_tdev.get(1)
            if tau1 is not None:
                parts.append(f"gap_TDEV(1s)={tau1:.2f}ns")
        if self._corr_tdev:
            tau1 = self._corr_tdev.get(1)
            if tau1 is not None:
                parts.append(f"corr_TDEV(1s)={tau1:.2f}ns")
        return " ".join(parts)


def _compute_tdev(phases, taus=None):
    """Compute time deviation (TDEV) from phase samples.

    TDEV has units of time (ns when phase is in ns), unlike ADEV which
    is dimensionless.  Uses the overlapping TDEV estimator:

        TDEV²(nτ₀) = τ₀²/(6n²(N-3n+1)) Σ_{j=0}^{N-3n} [Σ_{i=j}^{j+n-1} (x[i+2n] - 2x[i+n] + x[i])]²

    This is equivalent to TDEV(τ) = τ/√3 × MDEV(τ), where MDEV is the
    modified Allan deviation computed with nested averaging.

    Args:
        phases: list of phase values (ns), equally spaced at tau0=1s
        taus: list of averaging factors n to compute (default: 1, 2, 4, ..., N/4)

    Returns:
        dict {tau_s: tdev_ns}
    """
    N = len(phases)
    if N < 4:
        return {}

    if taus is None:
        taus = []
        n = 1
        while 3 * n < N:
            taus.append(n)
            n *= 2

    result = {}
    for n in taus:
        if 3 * n >= N:
            break
        # Outer sum: j = 0 .. N-3n
        outer_count = N - 3 * n + 1
        if outer_count < 1:
            break
        total = 0.0
        for j in range(outer_count):
            # Inner sum: average n consecutive second-differences
            inner = 0.0
            for i in range(j, j + n):
                inner += phases[i + 2 * n] - 2 * phases[i + n] + phases[i]
            total += inner * inner
        # τ₀ = 1s, so τ₀² = 1
        tdev_sq = total / (6.0 * n * n * outer_count)
        result[n] = math.sqrt(tdev_sq)

    return result

    return result
