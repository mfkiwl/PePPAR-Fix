"""Slow-moving estimators for source-time to host-monotonic relationships."""

from __future__ import annotations

import math


class TimebaseRelationEstimator:
    """Track a source-time to monotonic-time relationship with a simple EMA.

    The estimator models:

        recv_mono ~= source_time_s + offset_s

    where ``offset_s`` changes slowly. Sudden residual growth usually means
    queueing, batching, or a scheduling delay between the true event time and
    the point where user space handled it.
    """

    def __init__(
        self,
        *,
        alpha: float = 0.02,
        sigma_alpha: float = 0.05,
        min_sigma_s: float = 1e-3,
        sigma_scale: float = 4.0,
    ):
        self.alpha = alpha
        self.sigma_alpha = sigma_alpha
        self.min_sigma_s = min_sigma_s
        self.sigma_scale = sigma_scale

        self._offset_s = None
        self._sigma_s = min_sigma_s
        self._samples = 0

    def update(self, source_time_s: float, recv_mono_s: float):
        """Update the estimate and return residual/confidence details."""
        observed_offset_s = recv_mono_s - source_time_s

        if self._offset_s is None:
            self._offset_s = observed_offset_s
            self._samples = 1
            return {
                "predicted_recv_mono_s": recv_mono_s,
                "residual_s": 0.0,
                "sigma_s": self._sigma_s,
                "confidence": 1.0,
                "samples": self._samples,
            }

        predicted_recv_mono_s = source_time_s + self._offset_s
        residual_s = recv_mono_s - predicted_recv_mono_s
        abs_residual_s = abs(residual_s)

        self._offset_s = (
            (1.0 - self.alpha) * self._offset_s
            + self.alpha * observed_offset_s
        )
        self._sigma_s = max(
            self.min_sigma_s,
            (1.0 - self.sigma_alpha) * self._sigma_s
            + self.sigma_alpha * abs_residual_s,
        )
        self._samples += 1

        scale_s = max(self.min_sigma_s, self._sigma_s * self.sigma_scale)
        confidence = math.exp(-(abs_residual_s / scale_s))
        confidence = max(0.05, min(1.0, confidence))

        return {
            "predicted_recv_mono_s": predicted_recv_mono_s,
            "residual_s": residual_s,
            "sigma_s": self._sigma_s,
            "confidence": confidence,
            "samples": self._samples,
        }
