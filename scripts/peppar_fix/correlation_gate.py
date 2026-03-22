"""Strict sink-side correlation gate for time-sensitive multi-stream consumers."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, asdict

from peppar_fix.event_time import merge_correlation_confidence


@dataclass
class GateStats:
    consumed_correlated: int = 0
    deferred_waiting: int = 0
    dropped_unmatched: int = 0
    dropped_outside_window: int = 0
    dropped_low_confidence: int = 0

    def as_dict(self):
        return asdict(self)


@dataclass
class CorrectionGateStats:
    consumed_fresh: int = 0
    deferred_waiting: int = 0
    dropped_stale: int = 0

    def as_dict(self):
        return asdict(self)


class StrictCorrelationGate:
    """Consume only events that can be matched inside an explicit window.

    This gate sits in front of strict sinks where a wrong match is worse than
    silence. It does not deliver an observation until a valid companion event
    is available, and it drops observations only once the correlation window
    proves they can no longer match.
    """

    def __init__(self, min_confidence=0.5):
        self.stats = GateStats()
        self.min_confidence = min_confidence

    def pop_observation_match(
        self,
        obs_history: deque,
        target_sec_fn,
        match_fn,
        min_window_s=0.5,
        max_window_s=11.0,
        min_confidence=None,
    ):
        if min_confidence is None:
            min_confidence = self.min_confidence
        while obs_history:
            obs_event = obs_history[0]
            target_sec = target_sec_fn(obs_event)
            pps_event, delta_s, recv_dt_s, latest_pps_mono, combined_confidence = match_fn(
                obs_event,
                target_sec,
                min_window_s=min_window_s,
                max_window_s=max_window_s,
            )
            if pps_event is not None and combined_confidence >= min_confidence:
                obs_history.popleft()
                self.stats.consumed_correlated += 1
                return obs_event, (pps_event, delta_s, recv_dt_s, combined_confidence)
            if pps_event is not None:
                if latest_pps_mono is not None and latest_pps_mono - obs_event.recv_mono > max_window_s:
                    obs_history.popleft()
                    self.stats.dropped_low_confidence += 1
                    continue
                self.stats.deferred_waiting += 1
                return None, None
            if latest_pps_mono is None:
                self.stats.deferred_waiting += 1
                return None, None
            if latest_pps_mono - obs_event.recv_mono > max_window_s:
                obs_history.popleft()
                self.stats.dropped_unmatched += 1
                self.stats.dropped_outside_window += 1
                continue
            self.stats.deferred_waiting += 1
            return None, None
        return None, None


class CorrectionFreshnessGate:
    """Gate EKF updates on correction freshness rather than queue order."""

    def __init__(self):
        self.stats = CorrectionGateStats()

    def accept(
        self,
        corrections,
        *,
        now_mono=None,
        max_broadcast_age_s=30.0,
        require_ssr=False,
        max_ssr_age_s=30.0,
        min_broadcast_confidence=0.0,
        min_ssr_confidence=0.0,
    ):
        snapshot = corrections.freshness(now_mono=now_mono)

        if not snapshot["broadcast_ready"]:
            self.stats.deferred_waiting += 1
            return False, "waiting_broadcast", snapshot

        broadcast_age = snapshot["broadcast_age_s"]
        if broadcast_age is not None and broadcast_age > max_broadcast_age_s:
            self.stats.dropped_stale += 1
            return False, "stale_broadcast", snapshot
        broadcast_confidence = snapshot.get("broadcast_confidence")
        if (
            broadcast_confidence is not None
            and broadcast_confidence < min_broadcast_confidence
        ):
            self.stats.deferred_waiting += 1
            return False, "low_broadcast_confidence", snapshot

        if require_ssr:
            if not snapshot["ssr_ready"]:
                self.stats.deferred_waiting += 1
                return False, "waiting_ssr", snapshot
            ssr_age = snapshot["ssr_age_s"]
            if ssr_age is not None and ssr_age > max_ssr_age_s:
                self.stats.dropped_stale += 1
                return False, "stale_ssr", snapshot
            ssr_confidence = snapshot.get("ssr_confidence")
            if ssr_confidence is not None and ssr_confidence < min_ssr_confidence:
                self.stats.deferred_waiting += 1
                return False, "low_ssr_confidence", snapshot

        self.stats.consumed_fresh += 1
        return True, "fresh", snapshot


def match_pps_event_from_history(
    pps_history: deque,
    obs_event,
    target_sec,
    min_window_s=0.5,
    max_window_s=11.0,
):
    """Match one observation event against PPS history by receive time and second.

    Returns `(pps_event, second_delta, recv_dt_s, latest_pps_mono, combined_confidence)` where
    `pps_event` is `None` if no acceptable match is currently available.
    """
    latest_pps_mono = pps_history[-1].recv_mono if pps_history else None
    best = None

    while len(pps_history) > 1 and obs_event.recv_mono - pps_history[0].recv_mono > max_window_s:
        pps_history.popleft()

    for idx, pps_event in enumerate(pps_history):
        recv_dt_s = obs_event.recv_mono - pps_event.recv_mono
        if recv_dt_s < min_window_s or recv_dt_s > max_window_s:
            continue
        delta_s = pps_event.rounded_sec() - target_sec
        combined_confidence = merge_correlation_confidence(
            getattr(obs_event, "correlation_confidence", None),
            getattr(pps_event, "correlation_confidence", None),
        )
        candidate = (
            abs(delta_s),
            -combined_confidence,
            abs(recv_dt_s - 1.0),
            idx,
            pps_event,
            delta_s,
            recv_dt_s,
            combined_confidence,
        )
        if best is None or candidate < best:
            best = candidate

    if best is None:
        return None, None, None, latest_pps_mono, None

    _, _, _, matched_idx, pps_event, delta_s, recv_dt_s, combined_confidence = best
    for _ in range(matched_idx + 1):
        pps_history.popleft()
    return pps_event, delta_s, recv_dt_s, latest_pps_mono, combined_confidence
