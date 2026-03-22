"""Event envelopes for cross-stream correlation by local monotonic time."""

from dataclasses import dataclass
from typing import Optional


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
