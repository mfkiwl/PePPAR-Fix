"""Constant-offset estimators for source-time to host-monotonic relationships."""

from __future__ import annotations

import math


class TimebaseRelationEstimator:
    """Track a source-time to monotonic-time relationship as a weighted constant.

    The estimator models:

        recv_mono ~= source_time_s + offset_s

    where ``offset_s`` should be nearly constant. Sudden residual growth usually means
    queueing, batching, or a scheduling delay between the true event time and
    the point where user space handled it.
    """

    def __init__(
        self,
        *,
        min_sigma_s: float = 1e-3,
        sigma_scale: float = 4.0,
    ):
        self.min_sigma_s = min_sigma_s
        self.sigma_scale = sigma_scale

        self._offset_s = None
        self._sigma_s = min_sigma_s
        self._samples = 0
        self._total_weight = 0.0
        self._weighted_abs_residual_sum = 0.0

    def update(
        self,
        source_time_s: float,
        recv_mono_s: float,
        *,
        sample_weight: float = 1.0,
    ):
        """Update the estimate and return residual/confidence details."""
        observed_offset_s = recv_mono_s - source_time_s
        sample_weight = max(0.0, min(1.0, float(sample_weight)))

        if self._offset_s is None:
            self._offset_s = observed_offset_s
            self._samples = 1
            self._total_weight = max(sample_weight, 1.0)
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

        if sample_weight > 0.0:
            new_total_weight = self._total_weight + sample_weight
            self._offset_s = (
                (self._offset_s * self._total_weight)
                + (observed_offset_s * sample_weight)
            ) / new_total_weight
            self._weighted_abs_residual_sum += abs_residual_s * sample_weight
            self._total_weight = new_total_weight
            self._sigma_s = max(
                self.min_sigma_s,
                self._weighted_abs_residual_sum / self._total_weight,
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
            "sample_weight": sample_weight,
        }
