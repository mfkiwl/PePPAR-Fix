"""Event envelopes for cross-stream correlation by local monotonic time."""

from dataclasses import dataclass
import math
from typing import Optional


def estimate_correlation_confidence(
    *,
    queue_remains: Optional[bool],
    parse_age_s: Optional[float],
    queued_base: float = 0.65,
    clear_base: float = 1.0,
    age_half_life_s: float = 0.5,
    min_confidence: float = 0.05,
) -> float:
    """Estimate confidence of source-time to host-monotonic mapping.

    Confidence is highest when the reader consumed an event without any queued
    bytes/events remaining and the event was handed to user mode immediately.
    It falls when backlog is visible or when the event sits in user space long
    enough for that receive timestamp to become stale.
    """
    base = clear_base if not queue_remains else queued_base
    if parse_age_s is None or parse_age_s <= 0.0:
        return max(min_confidence, min(1.0, base))
    age_half_life_s = max(1e-3, age_half_life_s)
    decay = math.exp(-math.log(2.0) * (parse_age_s / age_half_life_s))
    return max(min_confidence, min(1.0, base * decay))


def merge_correlation_confidence(*values: Optional[float]) -> float:
    """Combine per-stream confidence values conservatively."""
    present = [float(v) for v in values if v is not None]
    if not present:
        return 1.0
    return max(0.0, min(present))


@dataclass(frozen=True)
class ObservationEvent:
    """One GNSS observation epoch with receive timestamps."""

    gps_time: object
    observations: list
    recv_mono: float
    recv_utc: object
    queue_remains: Optional[bool] = None
    parse_age_s: Optional[float] = None
    correlation_confidence: Optional[float] = None
    estimator_residual_s: Optional[float] = None

    def __iter__(self):
        yield self.gps_time
        yield self.observations


@dataclass(frozen=True)
class PpsEvent:
    """One PPS/EXTTS edge with receive timestamp."""

    phc_sec: int
    phc_nsec: int
    index: int
    recv_mono: float
    queue_remains: Optional[bool] = None
    parse_age_s: Optional[float] = None
    correlation_confidence: Optional[float] = None
    estimator_residual_s: Optional[float] = None

    def rounded_sec(self):
        return self.phc_sec if self.phc_nsec < 500_000_000 else self.phc_sec + 1

    def __iter__(self):
        yield self.phc_sec
        yield self.phc_nsec
        yield self.index


@dataclass(frozen=True)
class RtcmEvent:
    """One RTCM message with receive timestamps."""

    identity: str
    message: object
    recv_mono: float
    recv_utc: object
    queue_remains: Optional[bool] = None
    parse_age_s: Optional[float] = None
    correlation_confidence: Optional[float] = None
    estimator_residual_s: Optional[float] = None


@dataclass(frozen=True)
class TiccEvent:
    """One TICC edge with receive timestamps."""

    channel: str
    ref_sec: int
    ref_ps: int
    recv_mono: float
    recv_utc: object = None
    queue_remains: Optional[bool] = None
    parse_age_s: Optional[float] = None
    correlation_confidence: Optional[float] = None
    estimator_residual_s: Optional[float] = None
