"""M7 adaptive discipline interval scheduler."""

import logging

log = logging.getLogger("peppar_fix.discipline")


class DisciplineScheduler:
    """Accumulates error samples and decides when to apply a correction.

    M7: instead of correcting every epoch, buffer N samples and apply one
    averaged correction. This reduces correction jitter while preserving
    tracking bandwidth.
    """

    def __init__(self, base_interval=1, adaptive=False,
                 min_interval=1, max_interval=120):
        self.base_interval = base_interval
        self.adaptive = adaptive
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.interval = base_interval

        self._errors = []
        self._confidences = []
        self._sources = []

        self._drift_rate = 0.1
        self._drift_alpha = 0.05
        self._prev_adjfine = None
        self._prev_adjfine_t = None
        self._adjfine_history_s = 0.0

        self._converge_threshold = 100.0
        self._settled_count = 0
        self._settle_window = 10
        self._converging = True

    @property
    def n_accumulated(self):
        return len(self._errors)

    def accumulate(self, error_ns, confidence_ns, source_name):
        """Buffer one error sample."""
        self._errors.append(error_ns)
        self._confidences.append(confidence_ns)
        self._sources.append(source_name)

    def should_correct(self):
        """True when it's time to flush the buffer and correct."""
        n = len(self._errors)
        if n == 0:
            return False

        effective_interval = 1 if self._converging else self.interval

        if n >= effective_interval:
            return True

        if n > 1 and self._sources[-1] != self._sources[0]:
            return True

        return False

    def flush(self):
        """Return averaged error, confidence, and sample count; reset buffer.

        Returns:
            (avg_error_ns, avg_confidence_ns, n_samples)
        """
        if not self._errors:
            return (0.0, 0.0, 0)

        n = len(self._errors)
        avg_error = sum(self._errors) / n
        avg_confidence = sum(self._confidences) / n
        self._errors.clear()
        self._confidences.clear()
        self._sources.clear()

        if self._converging:
            if abs(avg_error) < self._converge_threshold:
                self._settled_count += 1
                if self._settled_count >= self._settle_window:
                    self._converging = False
                    log.info(f"  M7: settled after {self._settled_count} corrections, "
                             f"interval -> {self.base_interval}")
            else:
                self._settled_count = 0
        else:
            if abs(avg_error) > self._converge_threshold * 5:
                self._converging = True
                self._settled_count = 0
                log.info(f"  M7: error {avg_error:+.0f}ns, back to convergence mode")

        return (avg_error, avg_confidence, n)

    def update_drift_rate(self, timestamp, adjfine_ppb):
        """Update EMA of |delta_adjfine / delta_t| for adaptive scheduling."""
        if self._prev_adjfine is not None and self._prev_adjfine_t is not None:
            dt = timestamp - self._prev_adjfine_t
            if dt > 0:
                rate = abs(adjfine_ppb - self._prev_adjfine) / dt
                self._drift_rate = (self._drift_alpha * rate +
                                    (1.0 - self._drift_alpha) * self._drift_rate)
                self._adjfine_history_s += dt

        self._prev_adjfine = adjfine_ppb
        self._prev_adjfine_t = timestamp

    def compute_adaptive_interval(self, measurement_sigma_ns):
        """Compute optimal discipline interval from drift rate and noise."""
        if not self.adaptive:
            return self.base_interval

        if self._adjfine_history_s < 60.0:
            return self.base_interval

        if self._drift_rate < 1e-6:
            return self.max_interval

        tau = (2.0 * measurement_sigma_ns / self._drift_rate) ** 0.4
        tau = max(self.min_interval, min(self.max_interval, int(round(tau))))
        self.interval = tau
        return tau
