#!/usr/bin/env python3
"""peppar-fix-engine: Unified GNSS clock engine.

Single process with two phases:

Phase 1 — Bootstrap (no known position):
  PPPFilter estimates position from scratch. Solution logged.
  When converged: save position, transition to phase 2.
  Skipped if --position-file points to an existing converged position.

Phase 2 — Steady state (position known):
  FixedPosFilter estimates clock. Solution logged.
  Optional consumers:
  - DO servo (--servo /dev/ptp0): disciplines oscillator via PHC adjfine
  - NTRIP caster (--caster :2102): streams RTCM to clients (future)

Usage:
    peppar-fix-engine --serial /dev/gnss-top --ntrip-conf ntrip.conf \\
        --position-file data/position.json \\
        --servo /dev/ptp0 --pps-pin 1 \\
        --out solution.csv --systems gps,gal,bds

    # Bootstrap only (no servo):
    peppar-fix-engine --serial /dev/gnss-top --ntrip-conf ntrip.conf \\
        --position-file data/position.json --out bootstrap.csv

    # With existing position (skip bootstrap):
    peppar-fix-engine --serial /dev/gnss-top --ntrip-conf ntrip.conf \\
        --position-file data/position.json --servo /dev/ptp0 --pps-pin 1
"""

import argparse
from collections import deque
import csv
from dataclasses import dataclass
import json
import logging
import math
import os
import queue
import signal
from statistics import pvariance
import sys
import threading
import time
import tomllib
from datetime import datetime, timezone, timedelta

import numpy as np

from solve_pseudorange import C, ecef_to_lla, lla_to_ecef
from solve_ppp import PPPFilter, FixedPosFilter, ls_init
from broadcast_eph import BroadcastEphemeris
from ssr_corrections import SSRState, RealtimeCorrections
from ntrip_client import NtripStream
from realtime_ppp import serial_reader, ntrip_reader, QErrStore, Nav2PositionStore
from ticc import Ticc
from peppar_fix import (
    CorrectionFreshnessGate,
    PositionWatchdog,
    StrictCorrelationGate,
    TimebaseRelationEstimator,
    PPPCalibration,
    CarrierPhaseTracker,
    estimator_sample_weight,
    estimate_correlation_confidence,
    match_pps_event_from_history,
    load_position,
    save_position,
)
from peppar_fix.event_time import PpsEvent
from peppar_fix.fault_injection import get_delay_injector, get_source_mute_controller
from peppar_fix.receiver import get_driver

log = logging.getLogger("peppar-fix")


class QErrTimescaleTracker:
    """Track CLOCK_MONOTONIC offset between TIM-TP and TICC chB streams.

    TIM-TP for PPS epoch N arrives at mono_A.  TICC chB for the same
    epoch arrives at mono_B.  The offset = mono_B - mono_A is nearly
    constant (~0.95s on most hosts, depending on USB latency).

    Rather than dynamically searching for the best TIM-TP match per
    pulse (fragile — can grab the adjacent TIM-TP when the offset
    drifts), this tracker:
    1. Bootstraps the offset from the first few chB/TIM-TP pairs
    2. Maintains it with an exponential moving average
    3. Uses the tracked offset to index directly into the TIM-TP
       stream for each chB event — deterministic, no search ambiguity

    Since TIM-TP samples are ~1 second apart, the offset only needs
    to be accurate within ±0.3s to unambiguously pick the right one.
    """

    def __init__(self, initial_offset_s=0.95, alpha=0.005):
        self.offset_s = initial_offset_s
        self.calibrated = False
        self._alpha = alpha  # EMA weight (slow tracking)
        self._n = 0
        self._calibration_offsets = []
        self._logged = False
        self._last_consumed_mono = None

    def match_and_update(self, chb_recv_mono, qerr_store):
        """Find qerr for a chB event and update the offset estimate.

        Each TIM-TP sample is consumed at most once.  Two consecutive
        chB events cannot match the same TIM-TP — if the best match
        is the one we already consumed, return None for this epoch.

        Returns (qerr_ns, offset_s) or (None, None).
        """
        tol = 0.4 if not self.calibrated else 0.15
        qerr_ns, match_offset_s = qerr_store.match_pps_mono(
            chb_recv_mono,
            expected_offset_s=self.offset_s,
            tolerance_s=tol)

        if qerr_ns is None:
            return None, None

        # Deduplicate: the matched TIM-TP's host_time is approximately
        # chb_recv_mono - match_offset_s.  If it's within 0.1s of the
        # last consumed TIM-TP, it's the same sample — skip it.
        matched_tim_tp_mono = chb_recv_mono - match_offset_s
        if (self._last_consumed_mono is not None
                and abs(matched_tim_tp_mono - self._last_consumed_mono) < 0.5):
            return None, None
        self._last_consumed_mono = matched_tim_tp_mono

        actual_offset = match_offset_s
        if not self.calibrated:
            self._calibration_offsets.append(actual_offset)
            self._n += 1
            if self._n >= 10:
                median = sorted(self._calibration_offsets)[len(self._calibration_offsets) // 2]
                self.offset_s = median
                self.calibrated = True
        else:
            if abs(actual_offset - self.offset_s) < 0.2:
                self.offset_s += self._alpha * (actual_offset - self.offset_s)

        return qerr_ns, actual_offset


class QErrBeatNote:
    """Unwrap qErr sequence to extract the rx TCXO beat note against GPS.

    Each TIM-TP qErr sample reports where the PPS edge landed within the
    8 ns tick grid (~178 ps precision, range [-4, +4] ns).  As the rx TCXO
    drifts relative to GPS, qErr sweeps through this range and wraps at
    the ±4 ns boundary.

    This class unwraps the phase sequence to produce a continuous
    accumulated phase (the rx TCXO's phase relative to GPS), and
    estimates the rx TCXO frequency offset from the slope.

    The unwrapped phase should track PPP dt_rx (modulo a constant
    offset).  Divergence indicates a problem in one of the two
    measurement paths.
    """

    TICK_NS = 8.0  # 125 MHz = 8 ns period
    WRAP_THRESHOLD_NS = 4.0  # detect wrap when |Δ| > half tick
    # At 3 ns/s TCXO drift, normal deltas are ~3 ns and wrap deltas are
    # ~5 ns.  Threshold must be between these: drift < threshold < tick-drift.
    # Half-tick (4.0) works for drift rates up to ~3.5 ns/s.  Beyond that,
    # use frequency-aided unwrapping (not yet implemented).

    def __init__(self, freq_window=30):
        self._prev_qerr_ns = None
        self._accumulated_ns = 0.0  # unwrapped phase
        self._n = 0
        # Ring buffer for frequency estimation (slope of unwrapped phase)
        self._phase_buf = deque(maxlen=freq_window)
        # For dt_rx cross-validation (rate comparison, not absolute)
        self._prev_dt_rx = None
        self._dt_rx_calibrated = False
        # Synthesized phase: dt_rx resolves tick, qErr gives sub-tick
        self._synth_phase_ns = None

    def update(self, qerr_ns):
        """Feed one qErr sample (nanoseconds).  Returns unwrapped phase in ns."""
        if self._prev_qerr_ns is None:
            self._prev_qerr_ns = qerr_ns
            self._accumulated_ns = 0.0
            self._phase_buf.append(self._accumulated_ns)
            self._n = 1
            return self._accumulated_ns

        delta = qerr_ns - self._prev_qerr_ns
        # Detect wraps: if the TCXO drifts past a tick boundary,
        # qErr jumps by ~±8 ns.  Correct for it.
        if delta > self.WRAP_THRESHOLD_NS:
            delta -= self.TICK_NS
        elif delta < -self.WRAP_THRESHOLD_NS:
            delta += self.TICK_NS

        self._accumulated_ns += delta
        self._prev_qerr_ns = qerr_ns
        self._phase_buf.append(self._accumulated_ns)
        self._n += 1
        return self._accumulated_ns

    def frequency_offset_ns_per_s(self):
        """Estimate rx TCXO frequency offset in ns/s from recent window.

        Returns (freq_ns_per_s, n_samples) or (None, 0) if insufficient data.
        At 1 Hz sampling, each index step = 1 second.
        """
        n = len(self._phase_buf)
        if n < 5:
            return None, 0
        # Simple linear regression: phase = a + b*t
        phases = list(self._phase_buf)
        sx = n * (n - 1) / 2
        sxx = n * (n - 1) * (2 * n - 1) / 6
        sy = sum(phases)
        sxy = sum(i * v for i, v in enumerate(phases))
        denom = n * sxx - sx * sx
        if abs(denom) < 1e-10:
            return None, 0
        slope = (n * sxy - sx * sy) / denom
        return slope, n

    def accumulated_phase_ns(self):
        """Return current unwrapped accumulated phase in ns."""
        return self._accumulated_ns

    def cross_validate_dt_rx(self, dt_rx_ns):
        """Compare qErr frequency against PPP dt_rx rate (mod 8 ns).

        qErr unwrapped phase tracks dt_rx modulo the 8 ns tick period.
        The integer tick drift (N × 8 ns/s) is invisible to qErr.
        Cross-validation compares rates: dt_rx_rate mod 8 should match
        qErr frequency.  A discrepancy indicates a cycle slip, filter
        fault, or qErr unwrap error.

        Returns (rate_discrepancy_ns_s, calibrated) or (None, False).
        """
        if self._n < 5:
            self._prev_dt_rx = dt_rx_ns
            return None, False

        if self._prev_dt_rx is None:
            self._prev_dt_rx = dt_rx_ns
            return None, False

        # dt_rx rate over the frequency window
        dt_rx_rate = dt_rx_ns - self._prev_dt_rx  # ns/s at 1 Hz
        self._prev_dt_rx = dt_rx_ns

        qerr_freq, n = self.frequency_offset_ns_per_s()
        if qerr_freq is None:
            return None, False

        # Compare rates mod 8: both should agree on the sub-tick drift
        dt_rx_rate_mod8 = ((dt_rx_rate + 4.0) % self.TICK_NS) - 4.0
        qerr_freq_mod8 = ((qerr_freq + 4.0) % self.TICK_NS) - 4.0
        discrepancy = dt_rx_rate_mod8 - qerr_freq_mod8
        # Wrap discrepancy to [-4, +4] to handle boundary cases
        if discrepancy > self.TICK_NS / 2:
            discrepancy -= self.TICK_NS
        elif discrepancy < -self.TICK_NS / 2:
            discrepancy += self.TICK_NS

        self._dt_rx_calibrated = True
        return discrepancy, True

    def synthesize_phase(self, dt_rx_ns, qerr_ns):
        """Combine dt_rx (tick resolution) with qErr (sub-tick precision).

        dt_rx provides the full rx TCXO phase relative to GPS but with
        ~4 ns noise.  qErr provides the sub-tick position to ~178 ps but
        is ambiguous by 8 ns.  This method uses dt_rx to resolve the
        integer tick number and qErr for the fractional part.

        Returns synthesized phase in ns, or None if inputs are missing.
        The result has dt_rx-level ambiguity resolution with qErr-level
        precision — the best of both.

        Near tick boundaries (|qerr_ns| > 3 ns), the tick assignment is
        uncertain when dt_rx noise could push it to the adjacent tick.
        In those epochs, the method still returns a value but the caller
        should treat it with lower confidence.
        """
        if dt_rx_ns is None or qerr_ns is None:
            return self._synth_phase_ns

        # dt_rx tells us which tick; qerr tells us where within it.
        # tick_number = round((dt_rx - qerr) / 8)
        # synthesized = tick_number * 8 + qerr
        tick_number = round((dt_rx_ns - qerr_ns) / self.TICK_NS)
        self._synth_phase_ns = tick_number * self.TICK_NS + qerr_ns
        return self._synth_phase_ns

    @property
    def synth_phase_ns(self):
        return self._synth_phase_ns

    @property
    def n_samples(self):
        return self._n

    @property
    def calibrated(self):
        return self._dt_rx_calibrated


def _dt_rx_trend_predict(buf):
    """Linear regression on dt_rx buffer, predict at the latest epoch.

    dt_rx drifts at ~22 ppb (~22 ns/s), so a linear fit over 30s gives
    a smooth TCXO phase estimate that never hits tick boundaries.
    Returns the smoothed dt_rx at the last sample's time, or None.
    """
    n = len(buf)
    if n < 3:
        return None
    # Simple linear regression: y = a + b*t, t = 0..n-1, predict at t=n-1
    sx = n * (n - 1) / 2
    sxx = n * (n - 1) * (2 * n - 1) / 6
    sy = sum(buf)
    sxy = sum(i * v for i, v in enumerate(buf))
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-10:
        return sy / n
    b = (n * sxy - sx * sy) / denom
    a = (sy - b * sx) / n
    return a + b * (n - 1)


class RunningVarianceWindow:
    """Small rolling variance tracker for alignment qVIR metrics."""

    def __init__(self, maxlen=32):
        self._values = deque(maxlen=maxlen)

    def add(self, value):
        if value is not None:
            self._values.append(float(value))

    def variance(self):
        if len(self._values) < 2:
            return None
        return float(pvariance(self._values))

    def detrended_variance(self):
        """Return residual variance after removing a linear trend."""
        n = len(self._values)
        if n < 3:
            return None
        ts = list(range(n))
        values = list(self._values)
        mt = sum(ts) / n
        mv = sum(values) / n
        cov = sum((t - mt) * (v - mv) for t, v in zip(ts, values))
        var_t = sum((t - mt) ** 2 for t in ts)
        slope = cov / var_t if var_t else 0.0
        intercept = mv - slope * mt
        residuals = [v - (slope * t + intercept) for t, v in zip(ts, values)]
        return float(pvariance(residuals))

    def diff_variance(self):
        """Variance of first-differences: var(x[n] - x[n-1]).

        Immune to linear drift, quadratic glide, or any smooth trend —
        only measures the epoch-to-epoch jitter.
        """
        n = len(self._values)
        if n < 3:
            return None
        values = list(self._values)
        diffs = [values[i] - values[i - 1] for i in range(1, n)]
        return float(pvariance(diffs))

    def count(self):
        return len(self._values)


@dataclass
class TiccPairMeasurement:
    phc_channel: str
    ref_channel: str
    ref_sec: int
    diff_ns: float
    recv_mono: float
    confidence: float
    ref_qerr_ns: float = None  # qerr matched to the ref (gnss_pps) edge


class TiccPairTracker:
    """Pair TICC channel edges by integer ref_sec for realtime use."""

    def __init__(self, phc_channel: str, ref_channel: str):
        self.phc_channel = phc_channel
        self.ref_channel = ref_channel
        self._pending = {phc_channel: {}, ref_channel: {}}
        self._ref_qerr = {}  # ref_sec → qerr_ns, matched at chB arrival
        self._latest = None
        self._lock = threading.Lock()
        self._last_seen = {phc_channel: None, ref_channel: None}
        self._counts = {phc_channel: 0, ref_channel: 0}
        self._armed = False
        self._buffered_drops = 0
        self._boot_ref_sec_discard = 2

    def set_pending_ref_qerr(self, ref_sec, qerr_ns):
        """Store qerr matched to a ref (gnss_pps) edge before pairing."""
        with self._lock:
            self._ref_qerr[ref_sec] = qerr_ns

    def ingest(self, event):
        other = self.ref_channel if event.channel == self.phc_channel else self.phc_channel
        with self._lock:
            self._last_seen[event.channel] = event.recv_mono
            self._counts[event.channel] = self._counts.get(event.channel, 0) + 1
            self._pending[event.channel][event.ref_sec] = event
            other_event = self._pending[other].pop(event.ref_sec, None)
            if other_event is None:
                cutoff = event.ref_sec - 4
                for channel in self._pending.values():
                    stale_keys = [k for k in channel.keys() if k < cutoff]
                    for key in stale_keys:
                        channel.pop(key, None)
                stale_qerr = [k for k in self._ref_qerr if k < cutoff]
                for key in stale_qerr:
                    self._ref_qerr.pop(key, None)
                return

            this_event = event
            if this_event.channel == self.phc_channel:
                phc_event = this_event
                ref_event = other_event
            else:
                phc_event = other_event
                ref_event = this_event

            # Preserve raw logging, but do not let the first few post-open TICC
            # seconds into the live servo path. Those lines are commonly boot/
            # reopen artifacts and are not meaningful for control quality.
            #
            # Do not key this on queue_remains: for TICC, a valid matched pair
            # often arrives while its sibling line is still buffered, so
            # queue_remains can stay true even after the stream is healthy.
            if not self._armed:
                if event.ref_sec <= self._boot_ref_sec_discard:
                    self._buffered_drops += 1
                    return
                self._armed = True

            diff_ps = (
                (phc_event.ref_sec - ref_event.ref_sec) * 1_000_000_000_000
                + phc_event.ref_ps
                - ref_event.ref_ps
            )
            self._latest = TiccPairMeasurement(
                phc_channel=self.phc_channel,
                ref_channel=self.ref_channel,
                ref_sec=event.ref_sec,
                diff_ns=diff_ps * 1e-3,
                recv_mono=max(phc_event.recv_mono, ref_event.recv_mono),
                confidence=min(
                    getattr(phc_event, "correlation_confidence", 1.0) or 1.0,
                    getattr(ref_event, "correlation_confidence", 1.0) or 1.0,
                ),
                ref_qerr_ns=self._ref_qerr.pop(ref_event.ref_sec, None),
            )

    def latest(self, now_mono: float, max_age_s: float):
        with self._lock:
            if self._latest is None:
                return None
            if now_mono - self._latest.recv_mono > max_age_s:
                return None
            return self._latest

    def latest_diff_ns(self):
        """Return the most recent differential measurement in ns, or None."""
        with self._lock:
            if self._latest is None:
                return None
            return self._latest.diff_ns

    def health(self):
        with self._lock:
            return {
                "last_seen": dict(self._last_seen),
                "counts": dict(self._counts),
                "armed": self._armed,
                "buffered_drops": self._buffered_drops,
            }


def apply_ptp_profile(args):
    """Apply PTP defaults from config/receivers.toml when requested."""
    if not args.ptp_profile:
        return
    # Resolve config path relative to script directory if not found at CWD
    if not os.path.exists(args.device_config):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidate = os.path.join(script_dir, "..", "config", "receivers.toml")
        if os.path.exists(candidate):
            args.device_config = candidate
    try:
        with open(args.device_config, "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        log.warning(f"PTP profile config not found: {args.device_config}")
        return

    profile = cfg.get("ptp", {}).get(args.ptp_profile)
    if not profile:
        log.warning(f"PTP profile not found: {args.ptp_profile}")
        return

    if args.servo is None:
        args.servo = profile.get("device", args.servo)
    if args.pps_pin is None:
        args.pps_pin = profile.get("pps_pin", args.pps_pin)
    if args.extts_channel is None:
        args.extts_channel = profile.get("extts_channel", args.extts_channel)
    if args.phc_timescale is None:
        args.phc_timescale = profile.get("timescale", args.phc_timescale)
    if getattr(args, "track_kp", None) == 0.3:
        args.track_kp = profile.get("track_kp", args.track_kp)
    if getattr(args, "track_ki", None) == 0.1:
        args.track_ki = profile.get("track_ki", args.track_ki)
    if not args.program_pin:
        args.program_pin = bool(profile.get("program_pin", False))
    if args.max_broadcast_age_s is None:
        args.max_broadcast_age_s = profile.get(
            "max_broadcast_age_s", args.max_broadcast_age_s
        )
    if args.require_ssr is None:
        args.require_ssr = profile.get("require_ssr", args.require_ssr)
    if args.max_ssr_age_s is None:
        args.max_ssr_age_s = profile.get("max_ssr_age_s", args.max_ssr_age_s)
    if args.min_correlation_confidence is None:
        args.min_correlation_confidence = profile.get(
            "min_correlation_confidence", args.min_correlation_confidence
        )
    if getattr(args, 'max_correlation_window_s', None) is None:
        args.max_correlation_window_s = profile.get(
            "max_correlation_window_s", None
        )
    if args.min_broadcast_confidence is None:
        args.min_broadcast_confidence = profile.get(
            "min_broadcast_confidence", args.min_broadcast_confidence
        )
    if args.min_ssr_confidence is None:
        args.min_ssr_confidence = profile.get(
            "min_ssr_confidence", args.min_ssr_confidence
        )
    if args.track_max_ppb is None:
        args.track_max_ppb = profile.get("track_max_ppb", args.track_max_ppb)
    if args.track_restep_ns is None:
        args.track_restep_ns = profile.get("track_restep_ns", args.track_restep_ns)
    if args.phase_step_bias_ns is None:
        args.phase_step_bias_ns = profile.get("phase_step_bias_ns", args.phase_step_bias_ns)
    if args.obs_idle_timeout_s is None:
        args.obs_idle_timeout_s = profile.get("obs_idle_timeout_s", args.obs_idle_timeout_s)
    if args.carrier_max_sigma_ns is None:
        args.carrier_max_sigma_ns = profile.get(
            "carrier_max_sigma_ns", args.carrier_max_sigma_ns
        )
    if args.track_outlier_ns is None:
        args.track_outlier_ns = profile.get("track_outlier_ns", args.track_outlier_ns)
    if args.discipline_interval == 1:
        args.discipline_interval = profile.get("discipline_interval", args.discipline_interval)
    if not args.adaptive_interval:
        args.adaptive_interval = bool(profile.get("adaptive_interval", args.adaptive_interval))
    if args.min_interval == 1:
        args.min_interval = profile.get("min_interval", args.min_interval)
    if args.max_interval == 120:
        args.max_interval = profile.get("max_interval", args.max_interval)
    if args.gain_ref_sigma == 2.0:
        args.gain_ref_sigma = profile.get("gain_ref_sigma", args.gain_ref_sigma)
    if args.converge_error_ns == 500.0:
        args.converge_error_ns = profile.get("converge_error_ns", args.converge_error_ns)
    if args.converge_min_scale == 2.0:
        args.converge_min_scale = profile.get("converge_min_scale", args.converge_min_scale)
    if args.gain_min_scale == 0.1:
        args.gain_min_scale = profile.get("gain_min_scale", args.gain_min_scale)
    if args.gain_max_scale == 1.0:
        args.gain_max_scale = profile.get("gain_max_scale", args.gain_max_scale)
    if args.scheduler_converge_threshold_ns == 100.0:
        args.scheduler_converge_threshold_ns = profile.get(
            "scheduler_converge_threshold_ns", args.scheduler_converge_threshold_ns
        )
    if args.scheduler_settle_window == 10:
        args.scheduler_settle_window = profile.get(
            "scheduler_settle_window", args.scheduler_settle_window
        )
    if args.scheduler_unconverge_factor == 5.0:
        args.scheduler_unconverge_factor = profile.get(
            "scheduler_unconverge_factor", args.scheduler_unconverge_factor
        )
    if getattr(args, 'measurement_rate_ms', None) is None:
        args.measurement_rate_ms = profile.get("measurement_rate_ms", None)
    if getattr(args, 'sfrbx_rate', None) is None:
        args.sfrbx_rate = profile.get("sfrbx_rate", None)
    # ClockMatrix I2C actuator (optional — only on OTC hardware)
    if getattr(args, 'clockmatrix_bus', None) is None:
        cm_bus = profile.get("clockmatrix_bus")
        if cm_bus is not None:
            args.clockmatrix_bus = cm_bus
            args.clockmatrix_addr = profile.get("clockmatrix_addr", "0x58")
            args.clockmatrix_dpll_actuator = profile.get(
                "clockmatrix_dpll_actuator", 3)
            args.clockmatrix_dpll_phase = profile.get(
                "clockmatrix_dpll_phase", 2)
            args.clockmatrix_pps_clk = profile.get(
                "clockmatrix_pps_clk", 2)


def apply_ticc_drive_defaults(args):
    """Bias experimental TICC drive toward light-touch control when untouched."""
    if not args.ticc_drive:
        return
    if args.discipline_interval == 1:
        args.discipline_interval = 2
    if not args.adaptive_interval:
        args.adaptive_interval = True
    if args.max_interval == 120:
        args.max_interval = 10
    if args.gain_ref_sigma == 2.0:
        args.gain_ref_sigma = 4.0
    if args.gain_min_scale == 0.1:
        args.gain_min_scale = 0.05
    if args.gain_max_scale == 1.0:
        args.gain_max_scale = 0.5
    if args.converge_error_ns == 500.0:
        args.converge_error_ns = 5_000.0
    if args.converge_min_scale == 2.0:
        args.converge_min_scale = 1.0
    if args.scheduler_converge_threshold_ns == 100.0:
        args.scheduler_converge_threshold_ns = 1_000.0
    if args.scheduler_settle_window == 10:
        args.scheduler_settle_window = 20
    if args.scheduler_unconverge_factor == 5.0:
        args.scheduler_unconverge_factor = 10.0
    if args.track_outlier_ns is None:
        args.track_outlier_ns = 10_000.0
    if args.ticc_pullin_interval == 5:
        args.ticc_pullin_interval = 5
    if args.ticc_pullin_window_s == 8.0:
        args.ticc_pullin_window_s = 8.0
    if args.ticc_landing_threshold_ns == 1_500.0:
        args.ticc_landing_threshold_ns = 1_500.0
    if args.ticc_settled_threshold_ns == 100.0:
        args.ticc_settled_threshold_ns = 100.0
    if args.ticc_settled_deadband_ns == 75.0:
        args.ticc_settled_deadband_ns = 75.0
    if args.ticc_settled_interval == 3:
        args.ticc_settled_interval = 2
    if args.ticc_settled_count == 10:
        args.ticc_settled_count = 10


def _update_ticc_tracking_mode(ctx, args, best, now_mono):
    """Choose pull-in/landing/settled behavior for TICC-driven tracking."""
    if not args.ticc_drive or best.name != 'TICC':
        return None, None

    err = best.error_ns
    prev_err = ctx.get('ticc_prev_error_ns')
    prev_mono = ctx.get('ticc_prev_error_mono')
    mode = ctx.get('tracking_mode', 'pull_in')
    time_to_zero_s = None

    if prev_err is not None and prev_mono is not None:
        dt = max(1e-6, now_mono - prev_mono)
        slope = (err - prev_err) / dt
        if slope != 0.0 and ((err > 0.0 and slope < 0.0) or (err < 0.0 and slope > 0.0)):
            time_to_zero_s = abs(err / slope)

    crossed_zero = prev_err is not None and ((prev_err <= 0.0 <= err) or (prev_err >= 0.0 >= err))

    settled_limit = args.ticc_settled_threshold_ns
    deadband_limit = args.ticc_settled_deadband_ns

    if mode == 'pull_in':
        if crossed_zero or abs(err) <= args.ticc_landing_threshold_ns:
            mode = 'landing'
            ctx['ticc_settled_count'] = 0
        elif time_to_zero_s is not None and time_to_zero_s <= args.ticc_pullin_window_s:
            mode = 'landing'
            ctx['ticc_settled_count'] = 0
    elif mode == 'landing':
        if abs(err) <= deadband_limit:
            ctx['ticc_settled_count'] = args.ticc_settled_count
            mode = 'settled'
        elif abs(err) <= settled_limit:
            ctx['ticc_settled_count'] = ctx.get('ticc_settled_count', 0) + 1
            if ctx['ticc_settled_count'] >= args.ticc_settled_count:
                mode = 'settled'
        else:
            ctx['ticc_settled_count'] = 0
            if abs(err) > args.ticc_landing_threshold_ns * 2:
                mode = 'pull_in'
    else:  # settled
        if crossed_zero:
            ctx['ticc_settled_count'] = 0
            mode = 'landing'
        elif abs(err) > max(args.ticc_settled_threshold_ns * 4, 500.0):
            ctx['ticc_settled_count'] = 0
            mode = 'landing'

    ctx['ticc_prev_error_ns'] = err
    ctx['ticc_prev_error_mono'] = now_mono
    ctx['tracking_mode'] = mode
    return mode, time_to_zero_s


# ── Convergence detection (from peppar_find_position) ─────────────────── #

def position_sigma_3d(P):
    """Compute 3D position sigma from EKF covariance matrix."""
    P_pos = P[:3, :3]
    return math.sqrt(P_pos[0, 0] + P_pos[1, 1] + P_pos[2, 2])


# ── NTRIP config loading ─────────────────────────────────────────────── #

def load_ntrip_config(args):
    """Load NTRIP configuration from config file, merging with CLI args."""
    if args.ntrip_conf:
        import configparser
        conf = configparser.ConfigParser()
        conf.read(args.ntrip_conf)
        if 'ntrip' in conf:
            s = conf['ntrip']
            if not args.ntrip_caster:
                args.ntrip_caster = s.get('caster', args.ntrip_caster)
            if args.ntrip_port == 2101 and s.get('port'):
                args.ntrip_port = int(s.get('port'))
            if not args.ntrip_user:
                args.ntrip_user = s.get('user', args.ntrip_user)
            if not args.ntrip_password:
                args.ntrip_password = s.get('password', args.ntrip_password)
            if not args.ntrip_tls and s.getboolean('tls', False):
                args.ntrip_tls = True
            if not args.ssr_mount and s.get('mount'):
                args.ssr_mount = s.get('mount')


# ── Shared infrastructure setup ──────────────────────────────────────── #

def start_ntrip_threads(args, beph, ssr, stop_event):
    """Start NTRIP threads for ephemeris and SSR corrections."""
    threads = []
    use_tls = args.ntrip_tls or args.ntrip_port == 443

    if args.eph_mount:
        eph_stream = NtripStream(
            caster=args.ntrip_caster, port=args.ntrip_port,
            mountpoint=args.eph_mount,
            user=args.ntrip_user, password=args.ntrip_password,
            tls=use_tls,
        )
        t = threading.Thread(
            target=ntrip_reader,
            args=(eph_stream, beph, ssr, stop_event, "EPH"),
            daemon=True,
        )
        t.start()
        threads.append(t)
        log.info(f"Ephemeris stream: {args.ntrip_caster}:{args.ntrip_port}/{args.eph_mount}")

    if args.ssr_mount:
        ssr_stream = NtripStream(
            caster=args.ntrip_caster, port=args.ntrip_port,
            mountpoint=args.ssr_mount,
            user=args.ntrip_user, password=args.ntrip_password,
            tls=use_tls,
        )
        t = threading.Thread(
            target=ntrip_reader,
            args=(ssr_stream, beph, ssr, stop_event, "SSR"),
            daemon=True,
        )
        t.start()
        threads.append(t)
        log.info(f"SSR stream: {args.ntrip_caster}:{args.ntrip_port}/{args.ssr_mount}")

    return threads


def wait_for_ephemeris(beph, stop_event, systems=None, timeout_s=120):
    """Wait for broadcast ephemeris — each configured system must have >= 8 SVs."""
    SYS_TO_PREFIX = {'gps': 'G', 'gal': 'E', 'bds': 'C'}
    required = {SYS_TO_PREFIX[s] for s in (systems or {'gps', 'gal', 'bds'}) if s in SYS_TO_PREFIX}
    required.add('G')
    log.info(f"Waiting for broadcast ephemeris (need {required})...")
    warmup_start = time.time()
    while time.time() - warmup_start < timeout_s:
        if stop_event.is_set():
            return False
        by_sys = {}
        for prn in beph.satellites:
            s = prn[0]
            by_sys[s] = by_sys.get(s, 0) + 1
        if all(by_sys.get(p, 0) >= 8 for p in required):
            break
        time.sleep(1)
        if int(time.time() - warmup_start) % 10 == 0:
            log.info(f"  Warmup: {beph.summary()}")
    log.info(f"Broadcast ephemeris ready: {beph.summary()}")
    return True


# ── Phase 1: Bootstrap ─────────────────────────────────────────────────── #

def run_bootstrap(args, obs_queue, corrections, stop_event, out_w=None):
    """Run PPPFilter to estimate position from scratch.

    Returns:
        (ecef, sigma_m) on convergence, or None on timeout/error.
    """
    log.info("=== Phase 1: Position bootstrap (PPPFilter) ===")

    # Seed position
    seed_ecef = None
    if args.seed_pos:
        lat, lon, alt = [float(v) for v in args.seed_pos.split(',')]
        seed_ecef = lla_to_ecef(lat, lon, alt)
        log.info(f"Seed position: {lat:.6f}, {lon:.6f}, {alt:.1f}m")

    filt = PPPFilter()
    filt_initialized = False
    correction_gate = CorrectionFreshnessGate()
    run_bootstrap.last_correction_gate_stats = correction_gate.stats.as_dict()

    prev_t = None
    prev_pos_ecef = None
    n_epochs = 0
    n_empty = 0
    converged_at = None
    start_time = time.time()

    while not stop_event.is_set():
        elapsed = time.time() - start_time

        if args.timeout and elapsed > args.timeout:
            log.warning(f"Bootstrap timeout after {elapsed:.0f}s")
            return None

        try:
            gps_time, observations = obs_queue.get(timeout=5)
        except queue.Empty:
            n_empty += 1
            if n_empty > 12:
                log.error("No observations for 60s during bootstrap")
                return None
            continue
        n_empty = 0

        ok_corr, corr_reason, corr_snapshot = correction_gate.accept(
            corrections,
            max_broadcast_age_s=args.max_broadcast_age_s,
            require_ssr=args.require_ssr,
            max_ssr_age_s=args.max_ssr_age_s,
            min_broadcast_confidence=args.min_broadcast_confidence,
            min_ssr_confidence=args.min_ssr_confidence,
        )
        run_bootstrap.last_correction_gate_stats = correction_gate.stats.as_dict()
        if not ok_corr:
            if n_epochs % 10 == 0:
                log.info(
                    "Bootstrap waiting for fresh corrections: reason=%s "
                    "broadcast_age=%s",
                    corr_reason,
                    f"{corr_snapshot['broadcast_age_s']:.1f}s"
                    if corr_snapshot["broadcast_age_s"] is not None else "N/A",
                )
            continue

        # Initialize filter on first epoch with enough satellites
        if not filt_initialized:
            if seed_ecef is not None:
                init_pos = seed_ecef
                init_clk = 0.0
            else:
                # Use broadcast-only for LS init (same rationale as position
                # validation: SSR orbit corrections poison the absolute LS
                # solver).  Broadcast-only gives ~5m accuracy which is
                # plenty for PPPFilter seeding.
                _beph = corrections.beph
                x_ls, ok, n_sv = ls_init(observations, _beph, gps_time,
                                          clk_file=None)
                if not ok or n_sv < 4:
                    log.info(f"Waiting for enough satellites (got {n_sv})")
                    continue
                init_pos = x_ls[:3]
                init_clk = x_ls[3]
                log.info(f"LS init: {n_sv} SVs, pos error ~km-level")

            filt.initialize(init_pos, init_clk)
            filt_initialized = True
            prev_t = gps_time
            log.info("PPPFilter initialized, starting convergence")
            continue

        # EKF predict
        dt = (gps_time - prev_t).total_seconds()
        if dt <= 0 or dt > 30:
            log.warning(f"Suspicious dt={dt:.1f}s, skipping")
            prev_t = gps_time
            continue
        filt.predict(dt)
        prev_t = gps_time

        # Manage ambiguities
        current_svs = {o['sv'] for o in observations}
        if filt.prev_obs:
            slipped = filt.detect_cycle_slips(observations, filt.prev_obs)
            for sv in slipped:
                filt.remove_ambiguity(sv)

        for obs in observations:
            sv = obs['sv']
            if sv not in filt.sv_to_idx and obs.get('phi_if_m') is not None:
                sat_pos, sat_clk = corrections.sat_position(sv, gps_time)
                if sat_pos is not None:
                    N_init = obs['pr_if'] - obs['phi_if_m']
                    filt.add_ambiguity(sv, N_init)

        filt.prev_obs = {o['sv']: o for o in observations}

        for sv in list(filt.sv_to_idx.keys()):
            if sv not in current_svs:
                filt.remove_ambiguity(sv)

        # EKF update
        n_used, resid, sys_counts = filt.update(
            observations, corrections, gps_time, clk_file=corrections)

        if n_used < 4:
            continue

        n_epochs += 1

        pos_ecef = filt.x[:3]
        sigma_3d = position_sigma_3d(filt.P)
        lat, lon, alt = ecef_to_lla(pos_ecef[0], pos_ecef[1], pos_ecef[2])
        rms = np.sqrt(np.mean(resid ** 2)) if len(resid) > 0 else 0

        # CSV output
        if out_w:
            out_w.writerow([
                gps_time.strftime('%Y-%m-%d %H:%M:%S.%f')[:23],
                'bootstrap',
                f'{lat:.7f}', f'{lon:.7f}', f'{alt:.3f}',
                f'{sigma_3d:.4f}', '', '',
                n_used, f'{rms:.4f}',
                '', '', '', len(filt.sv_to_idx),
            ])

        if n_epochs % 5 == 0:
            log.info(
                f"  [{n_epochs}] σ={sigma_3d:.3f}m "
                f"pos=({lat:.6f}, {lon:.6f}, {alt:.1f}) "
                f"n={n_used} amb={len(filt.sv_to_idx)} "
                f"rms={rms:.3f}m [{elapsed:.0f}s]"
            )
            # Log ambiguity integrality (PPP-AR diagnostic)
            if len(filt.sv_to_idx) > 0 and n_epochs % 10 == 0:
                N_BASE = 6  # from solve_ppp.py
                fracs = []
                for sv, idx in filt.sv_to_idx.items():
                    si = N_BASE + idx
                    if si < len(filt.x):
                        N_float = filt.x[si]
                        frac = N_float - round(N_float)
                        sigma = np.sqrt(filt.P[si, si]) if si < filt.P.shape[0] else 999
                        fracs.append(abs(frac))
                        if n_epochs % 30 == 0:
                            log.debug(f"    {sv}: N={N_float:.3f} σ={sigma:.3f} frac={frac:+.3f}")
                if fracs:
                    mean_frac = np.mean(fracs)
                    log.info(f"    Ambiguity integrality: mean|frac|={mean_frac:.3f} "
                             f"(n={len(fracs)}, <0.15 = ready for AR)")

        # Convergence check
        pos_stable = True
        if prev_pos_ecef is not None:
            pos_delta = np.linalg.norm(pos_ecef - prev_pos_ecef)
            pos_stable = pos_delta < args.sigma
        prev_pos_ecef = pos_ecef.copy()

        if sigma_3d < args.sigma and pos_stable:
            if converged_at is None:
                converged_at = n_epochs
            if n_epochs - converged_at >= 30:
                log.info(f"CONVERGED at epoch {n_epochs} "
                         f"(σ={sigma_3d:.4f}m, rms={rms:.3f}m)")
                run_bootstrap.last_correction_gate_stats = correction_gate.stats.as_dict()
                return (pos_ecef, float(sigma_3d))
        else:
            converged_at = None

    run_bootstrap.last_correction_gate_stats = correction_gate.stats.as_dict()
    return None


# ── Phase 2: Steady state ────────────────────────────────────────────── #

def run_steady_state(args, known_ecef, obs_queue, corrections, beph, ssr,
                     stop_event, qerr_store=None, out_w=None, nav2_store=None):
    """Run FixedPosFilter for clock estimation with optional servo.

    This is the steady-state phase: position is known, we estimate clock
    offset and optionally discipline a PHC.
    """
    log.info("=== Phase 2: Steady state (FixedPosFilter) ===")
    lat, lon, alt = ecef_to_lla(known_ecef[0], known_ecef[1], known_ecef[2])
    log.info(f"Position: {lat:.6f}, {lon:.6f}, {alt:.1f}m")

    # Seed filter at dt_rx=0 — bootstrap guarantees PHC is within ±10µs
    # of truth, so the receiver clock residual at the PPS edge is near zero.
    # This makes sigma an honest convergence metric (starts large, shrinks
    # as filter converges) instead of instantly collapsing on the raw
    # receiver clock offset from pseudorange seeding.
    filt = FixedPosFilter(known_ecef)
    filt.x[filt.IDX_CLK] = 0.0
    filt.P[filt.IDX_CLK, filt.IDX_CLK] = 100.0 ** 2  # 100m ≈ 333ns 1σ
    filt.initialized = True  # skip pseudorange seeding
    filt.prev_clock = 0.0
    watchdog = PositionWatchdog(threshold_m=args.watchdog_threshold)
    correction_gate = CorrectionFreshnessGate()

    # Optional servo setup (PTP imports only loaded when needed)
    servo_ctx = None
    if args.servo:
        servo_result = _setup_servo(args, known_ecef, qerr_store)
        if isinstance(servo_result, int):
            log.error("Failed to set up PHC servo (exit code %d)", servo_result)
            return servo_result
        servo_ctx = servo_result
        servo_ctx["correlation_gate"] = StrictCorrelationGate()

    prev_t = None
    n_epochs = 0
    start_time = time.time()
    skip_stats = {
        "gate_wait_obs": 0,
        "corr_wait": 0,
        "dt_suspicious": 0,
        "too_few_meas": 0,
        "servo_no_pps": 0,
        "servo_outlier": 0,
        "obs_idle_holdover": 0,
        "obs_input_timeouts": 0,
        "obs_deferred_stalls": 0,
        "obs_dropped_expired": 0,
        "obs_dropped_queued": 0,
        "ticc_missing_pair": 0,
        "consumption_alarm": False,
    }
    last_skip_log = start_time
    last_obs_wall = time.monotonic()
    last_obs_input_wall = last_obs_wall
    last_usable_obs_wall = last_obs_wall
    obs_idle_alarm = False
    deferred_alarm = False
    # Queue monitoring: high-water marks (session max) + depth threshold alerts
    queue_hwm = {"obs_queue": 0, "obs_history": 0, "pps_history": 0}
    queue_alert_armed = {"obs_queue": True, "obs_history": True, "pps_history": True}
    queue_depth_threshold = getattr(args, "queue_depth_threshold", 5)
    queue_dump = getattr(args, "queue_depth_dump", False)
    last_hwm_log = start_time
    # Consumption rate monitor: detect when observation delivery persistently
    # falls behind PPS.  Growing recv_dt means the I2C/serial buffer is filling
    # faster than we drain it — we'll eventually lose observations and the
    # correlation window will fail.  This is a sanity check, not a retry:
    # the correct fix is to reduce message bandwidth (disable SFRBX, etc).
    recv_dt_history = deque(maxlen=30)
    consumption_alarm = False
    # Sink policy: steady-state + servo is a correlated-window consumer.
    # Preserve receive order here and let the correlator decide when an epoch
    # is too old to be useful, rather than draining the queue at phase entry.
    obs_history = deque()
    try:
        while not stop_event.is_set():
            if args.duration and (time.time() - start_time) > args.duration:
                log.info(f"Duration limit reached ({args.duration}s)")
                break

            try:
                added_obs = _append_queue_history(obs_history, obs_queue, timeout=5)
            except queue.Empty:
                skip_stats["obs_input_timeouts"] += 1
                idle_s = time.monotonic() - last_obs_wall
                if (
                    servo_ctx is not None and
                    args.obs_idle_timeout_s is not None and
                    idle_s >= args.obs_idle_timeout_s and
                    not obs_idle_alarm
                ):
                    skip_stats["obs_idle_holdover"] += 1
                    _enter_obs_holdover(
                        servo_ctx, args, "no_obs_input", f"no observation epochs for {idle_s:.1f}s"
                    )
                    obs_idle_alarm = True
                continue
            if added_obs:
                last_obs_input_wall = time.monotonic()

            # ── Queue depth monitoring ──
            _check_queue_depths(
                queue_hwm, queue_alert_armed, queue_depth_threshold, queue_dump,
                obs_queue=obs_queue,
                obs_history=obs_history,
                pps_history=servo_ctx.get("pps_history") if servo_ctx else None,
                pps_history_lock=servo_ctx.get("pps_history_lock") if servo_ctx else None,
                skip_stats=skip_stats,
                gate=servo_ctx.get("correlation_gate") if servo_ctx else None,
            )

            if servo_ctx is not None:
                gate = servo_ctx["correlation_gate"]
                dropped_before = gate.stats.dropped_unmatched
                obs_event, pps_match = gate.pop_observation_match(
                    obs_history,
                    target_sec_fn=lambda event: _target_timescale_sec(event.gps_time, args),
                    match_fn=lambda obs_event, target_sec, min_window_s=0.5, max_window_s=11.0:
                        _match_pps_event_from_history(
                            servo_ctx,
                            obs_event,
                            target_sec,
                            min_window_s=min_window_s,
                            max_window_s=max_window_s,
                        ),
                    min_confidence=args.min_correlation_confidence,
                )
                dropped_obs = gate.stats.dropped_unmatched - dropped_before
                queued_dropped = gate.stats.dropped_queued_behind
                if queued_dropped > skip_stats["obs_dropped_queued"]:
                    n_new = queued_dropped - skip_stats["obs_dropped_queued"]
                    log.info("  Skipped %d queued observations (unreliable recv_mono)", n_new)
                    skip_stats["obs_dropped_queued"] = queued_dropped
                if obs_event is None:
                    skip_stats["gate_wait_obs"] += 1
                    stall_s = time.monotonic() - last_usable_obs_wall
                    if (
                        args.obs_idle_timeout_s is not None and
                        stall_s >= args.obs_idle_timeout_s and
                        not deferred_alarm
                    ):
                        skip_stats["obs_deferred_stalls"] += 1
                        log.warning(
                            "Observation pipeline stalled without holdover: reason=obs_received_but_deferred "
                            "stalled_for=%.1fs queued=%d input_quiet_for=%.1fs",
                            stall_s,
                            len(obs_history),
                            time.monotonic() - last_obs_input_wall,
                        )
                        deferred_alarm = True
                    if added_obs and n_epochs % 10 == 0:
                        log.info(f"  [{n_epochs}] Awaiting correlatable observation "
                                 f"(queued={len(obs_history)})")
                    continue
            else:
                obs_event = obs_history.popleft()
                pps_match = None
                dropped_obs = 0

            last_obs_wall = time.monotonic()
            obs_idle_alarm = False
            deferred_alarm = False
            last_usable_obs_wall = last_obs_wall
            if servo_ctx is not None:
                _exit_holdover(servo_ctx, "fresh usable observation epoch received")

            # ── Consumption rate sanity check ──
            # Track recv_dt_s to detect persistent observation delivery lag.
            if pps_match is not None:
                _, _, match_recv_dt_s, _ = pps_match
                recv_dt_history.append(match_recv_dt_s)
                if len(recv_dt_history) >= 20 and not consumption_alarm:
                    # Linear trend of recv_dt over last 20 correlated epochs.
                    # Positive slope means observations arrive later each epoch.
                    dt_first = recv_dt_history[0]
                    dt_last = recv_dt_history[-1]
                    growth = dt_last - dt_first
                    if growth > 3.0:
                        consumption_alarm = True
                        skip_stats["consumption_alarm"] = True
                        log.error(
                            "CONSUMPTION RATE ALARM: observation delivery lag grew "
                            "%.1fs over %d epochs (recv_dt: %.1f → %.1fs). "
                            "The GNSS transport cannot sustain the configured "
                            "message rate. Observations will be lost. "
                            "Reduce I2C message bandwidth (disable SFRBX/PVT) "
                            "or check for transport bottlenecks.",
                            growth, len(recv_dt_history),
                            dt_first, dt_last,
                        )
                        _set_clock_class(servo_ctx, "freerun")

            ok_corr, corr_reason, corr_snapshot = correction_gate.accept(
                corrections,
                max_broadcast_age_s=args.max_broadcast_age_s,
                require_ssr=args.require_ssr,
                max_ssr_age_s=args.max_ssr_age_s,
                min_broadcast_confidence=args.min_broadcast_confidence,
                min_ssr_confidence=args.min_ssr_confidence,
            )
            if not ok_corr:
                skip_stats["corr_wait"] += 1
                if n_epochs % 10 == 0:
                    log.info(
                        "  [%s] Waiting for fresh corrections: reason=%s "
                        "broadcast_age=%s",
                        n_epochs,
                        corr_reason,
                        f"{corr_snapshot['broadcast_age_s']:.1f}s"
                        if corr_snapshot["broadcast_age_s"] is not None else "N/A",
                    )
                continue

            if dropped_obs and n_epochs % 10 == 0:
                log.info(f"  [{n_epochs}] Dropped {dropped_obs} expired observation epochs")
            skip_stats["obs_dropped_expired"] += dropped_obs
            gps_time, observations = obs_event

            # After a PHC step, the filter's clock state is stale.
            # Reset dt_rx to near-zero so the servo doesn't over-correct.
            if servo_ctx and servo_ctx.pop('filter_needs_clock_reset', False):
                filt.x[filt.IDX_CLK] = 0.0
                filt.P[filt.IDX_CLK, filt.IDX_CLK] = 2500.0 ** 2
                prev_t = None
                log.info("  EKF clock state reset after PHC step")

            # EKF predict
            if prev_t is not None:
                dt = (gps_time - prev_t).total_seconds()
                if dt <= 0:
                    skip_stats["dt_suspicious"] += 1
                    log.warning(f"Suspicious dt={dt:.1f}s, skipping")
                    prev_t = gps_time
                    continue
                if dt > 30:
                    # Gap recovery: reset filter time but don't skip the epoch.
                    # Clamp predict to 1s so the filter doesn't diverge, then
                    # let the update re-anchor from pseudoranges.
                    skip_stats["dt_suspicious"] += 1
                    log.warning(f"Gap dt={dt:.1f}s, resetting filter time (not skipping)")
                    filt.predict(1.0)
                else:
                    filt.predict(dt)
            prev_t = gps_time

            # EKF update
            n_used, resid, n_td = filt.update(
                observations, corrections, gps_time,
                clk_file=corrections,
            )

            if n_used < 4:
                skip_stats["too_few_meas"] += 1
                continue

            # Watchdog with NAV2 position consensus
            resid_rms = float(np.sqrt(np.mean(resid ** 2))) if len(resid) > 0 else 0.0
            watchdog.update(resid_rms, n_used)
            if watchdog.alarmed:
                # Before giving up: check NAV2 secondary engine position.
                # If NAV2 agrees with known_ecef, the antenna hasn't moved
                # and our FixedPosFilter just blew up — re-seed it instead
                # of exiting.  If NAV2 disagrees, the antenna actually
                # moved.  See docs/architecture-vision.md.
                reseed = False
                if nav2_store is not None:
                    nav2_ecef, nav2_acc, nav2_age = nav2_store.get_ecef(max_age_s=30.0)
                    if nav2_ecef is not None:
                        nav2_sep = float(np.linalg.norm(nav2_ecef - known_ecef))
                        log.warning(
                            "Watchdog: NAV2 position %.1fm from known_ecef "
                            "(hAcc=%.1fm, age=%.0fs) — %s",
                            nav2_sep, nav2_acc or -1, nav2_age,
                            "AGREES (re-seeding filter)" if nav2_sep < 50.0
                            else "DISAGREES (antenna may have moved)",
                        )
                        if nav2_sep < 50.0:
                            # NAV2 confirms antenna is fine.  Reset the
                            # filter state from known_ecef and continue.
                            log.info("Re-seeding FixedPosFilter from known_ecef "
                                     "(NAV2 consensus: antenna stable)")
                            filt = FixedPosFilter(known_ecef)
                            prev_t = None
                            watchdog.reset()
                            if servo_ctx is not None:
                                # Purge stale servo state so the servo
                                # doesn't act on the corrupted dt_rx
                                _purge_pps_state(servo_ctx)
                                servo_ctx['filter_needs_clock_reset'] = True
                            reseed = True
                    else:
                        log.warning("Watchdog: NAV2 position not available "
                                    "(%s) — cannot verify antenna position",
                                    nav2_store.summary())
                if not reseed:
                    if servo_ctx is not None:
                        _set_clock_class(servo_ctx, "freerun")
                    log.error("POSITION WATCHDOG ALARM: antenna may have moved! "
                              "Servo disabled. Restart with correct position or "
                              "delete position file to re-bootstrap.")
                    break

            dt_rx_ns = filt.x[filt.IDX_CLK] / C * 1e9
            p_clk = filt.P[filt.IDX_CLK, filt.IDX_CLK]
            dt_rx_sigma = math.sqrt(max(0, p_clk)) / C * 1e9
            n_epochs += 1

            # Extract ISBs for logging
            isb_gal_ns = 0.0
            isb_bds_ns = 0.0
            if hasattr(filt, 'IDX_ISB_GAL') and filt.x.shape[0] > filt.IDX_ISB_GAL:
                isb_gal_ns = filt.x[filt.IDX_ISB_GAL] / C * 1e9
            if hasattr(filt, 'IDX_ISB_BDS') and filt.x.shape[0] > getattr(filt, 'IDX_ISB_BDS', 999):
                isb_bds_ns = filt.x[filt.IDX_ISB_BDS] / C * 1e9

            # Correction source
            source = 'SSR' if ssr.n_clock > 0 else 'broadcast'
            ts_str = gps_time.strftime('%Y-%m-%d %H:%M:%S.%f')[:23]

            # CSV output
            if out_w:
                out_w.writerow([
                    ts_str, 'steady',
                    f'{lat:.7f}', f'{lon:.7f}', f'{alt:.3f}',
                    '', f'{dt_rx_ns:.3f}', f'{dt_rx_sigma:.4f}',
                    n_used, f'{resid_rms:.4f}',
                    source, f'{isb_gal_ns:.3f}', f'{isb_bds_ns:.3f}',
                    n_td,
                ])

            # Feed servo if active
            if servo_ctx is not None:
                servo_result = _servo_epoch(
                    servo_ctx, args, filt, obs_event, corr_snapshot, n_epochs,
                    dt_rx_ns, dt_rx_sigma, n_used, known_ecef,
                    resid_rms, isb_gal_ns, isb_bds_ns,
                    pps_match=pps_match,
                )
                if servo_result == "no_pps":
                    skip_stats["servo_no_pps"] += 1
                elif servo_result == "outlier":
                    skip_stats["servo_outlier"] += 1

            # PHC divergence: exit for re-bootstrap
            if servo_ctx is not None and servo_ctx.get('phc_diverged'):
                _set_clock_class(servo_ctx, "freerun")
                log.error("Shutting down — PHC needs re-bootstrap")
                return 5

            # Console status every 10 epochs
            if n_epochs % 10 == 0:
                elapsed = time.time() - start_time
                log.info(
                    f"  [{n_epochs}] {ts_str[:19]} "
                    f"clk={dt_rx_ns:+.1f}ns ±{dt_rx_sigma:.2f}ns "
                    f"n={n_used} rms={resid_rms:.3f}m "
                    f"[{source}]"
                )
            now = time.time()
            if now - last_skip_log >= 60.0:
                log.info(f"  Skip stats: {skip_stats}")
                last_skip_log = now
            if now - last_hwm_log >= 1200.0:  # every 20 minutes
                log.info(
                    "  Queue HWM (session max): obs_q=%d obs_hist=%d pps_hist=%d",
                    queue_hwm["obs_queue"], queue_hwm["obs_history"],
                    queue_hwm["pps_history"],
                )
                last_hwm_log = now

    except KeyboardInterrupt:
        log.info("Interrupted")
    finally:
        stop_event.set()
        if servo_ctx is not None and servo_ctx.get("correlation_gate") is not None:
            gate_stats = {
                "strict_correlation": servo_ctx["correlation_gate"].stats.as_dict(),
                "correction_freshness": correction_gate.stats.as_dict(),
                "steady_state_skips": skip_stats,
                "holdover": dict(servo_ctx["holdover"]),
                "queue_high_water_marks": dict(queue_hwm),
            }
        else:
            gate_stats = {
                "correction_freshness": correction_gate.stats.as_dict(),
                "steady_state_skips": skip_stats,
                "queue_high_water_marks": dict(queue_hwm),
            }
        if servo_ctx is not None:
            _cleanup_servo(servo_ctx)

    elapsed = time.time() - start_time
    log.info(f"Steady state complete: {elapsed:.0f}s, {n_epochs} epochs")
    return gate_stats


# ── Servo helpers (conditional PTP import) ────────────────────────────── #

def _open_pmc(args):
    """Open a PMC client if --pmc is configured.  Returns PmcClient or None."""
    pmc_path = getattr(args, 'pmc', None)
    if not pmc_path:
        return None
    domain = getattr(args, 'pmc_domain', 0)
    try:
        from peppar_fix.pmc import PmcClient
        client = PmcClient(pmc_path, domain=domain)
        client.open()
        log.info("pmc: connected to %s (domain %d)", pmc_path, domain)
        return client
    except Exception as e:
        log.warning("pmc: failed to open %s: %s (clockClass management disabled)", pmc_path, e)
        return None


def _set_clock_class(ctx, state):
    """Set ptp4l clockClass if PMC is configured and state has changed."""
    pmc = ctx.get('pmc')
    if pmc is None:
        return
    if ctx.get('pmc_announced') == state:
        return
    if pmc.set_grandmaster_class(state):
        ctx['pmc_announced'] = state


def _save_osc_freq_corr(ctx):
    """Save refined oscillator frequency corrections on clean shutdown.

    Skipped in --freerun mode: the live adjfine there is what the servo
    *would have* written if it were actuating, not a value the PHC has
    actually been steered to.  Persisting it would poison the next
    bootstrap with a fictitious frequency.
    """
    if ctx.get('freerun'):
        log.info("Skipping drift.json save (freerun mode)")
        return
    drift_path = ctx.get('drift_file', 'data/drift.json')
    adjfine = ctx.get('adjfine_ppb', 0.0)
    carrier_tracker = ctx.get('carrier_tracker')
    try:
        import json, os
        data = {}
        try:
            with open(drift_path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        data['adjfine_ppb'] = adjfine
        data['phc'] = ctx.get('phc_dev', '/dev/ptp0')
        data['timestamp'] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if (carrier_tracker is not None and carrier_tracker._n_d > 0
                and carrier_tracker.drift_rate_ppb != 0):
            data['tcxo_freq_corr_ppb'] = adjfine - carrier_tracker.drift_rate_ppb
        # Update dt_rx_ns for next DOFreqEst initialization
        if ctx.get('_prev_dt_rx_ns') is not None:
            data['dt_rx_ns'] = ctx['_prev_dt_rx_ns']
        os.makedirs(os.path.dirname(drift_path) or '.', exist_ok=True)
        with open(drift_path, 'w') as f:
            json.dump(data, f, indent=2)
        log.info("Saved osc freq corrections: adjfine=%.1f ppb, %s",
                 adjfine, drift_path)
    except Exception as e:
        log.warning("Failed to save osc freq corrections: %s", e)


def _log_do_characterization(args):
    """Read data/do_characterization.json and log inform-mode summary.

    Reports DO noise floor, dominant noise types, source crossovers,
    and a recommended loop bandwidth for the active servo input
    (informational only — does not auto-tune).
    """
    char_path = getattr(args, 'do_char_file', None) or 'data/do_characterization.json'
    try:
        import json
        with open(char_path) as f:
            char = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        log.info("DO characterization: no file at %s "
                 "(run with --freerun to create one)", char_path)
        return

    log.info("DO characterization: %s @ %s (captured %s)",
             char.get('do_label', 'unknown'),
             char.get('host', 'unknown'),
             char.get('captured', 'unknown'))
    for name, src in char.get('sources', {}).items():
        slope = src.get('slope')
        slope_str = f"{slope:+.2f}" if slope is not None else "n/a"
        units = src.get('units', '?')
        log.info("  %-22s ASD@0.1Hz=%.4f %s/√Hz  slope=%s (%s)",
                 name,
                 src.get('asd_at_0.1Hz', 0.0),
                 units,
                 slope_str,
                 src.get('noise_type', 'unknown'))
    crossovers = char.get('crossovers', {})
    if crossovers:
        log.info("DO characterization crossovers:")
        for pair, hz in crossovers.items():
            log.info("  %s: %.4f Hz (~%.0fs timescale)",
                     pair, hz, 1.0 / hz if hz > 0 else 0)


def _init_carrier_tracker(args):
    """Create CarrierPhaseTracker, seeding D from drift file if available.

    D = phc_freq_corr - tcxo_freq_corr, computed from two independently
    measured oscillator frequency corrections stored in the drift file.
    """
    tracker = CarrierPhaseTracker()
    drift_path = getattr(args, 'drift_file', None) or 'data/drift.json'
    try:
        import json
        with open(drift_path) as f:
            drift = json.load(f)
        phc_corr = drift.get('adjfine_ppb')
        tcxo_corr = drift.get('tcxo_freq_corr_ppb')
        if phc_corr is not None and tcxo_corr is not None:
            d = phc_corr - tcxo_corr
            tracker.drift_rate_ppb = d
            log.info("Carrier tracker: D=%.1f ppb "
                     "(phc_corr=%.1f - tcxo_corr=%.1f) from %s",
                     d, phc_corr, tcxo_corr, drift_path)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return tracker


def _setup_servo(args, known_ecef, qerr_store):
    """Set up PHC servo.

    Returns context dict on success, or an int exit code on failure:
    1 = fatal (device or library error), 3 = no PPS (retryable).
    """
    gate_stats = None
    try:
        from peppar_fix import PtpDevice, PIServo, DisciplineScheduler
        from peppar_fix import compute_error_sources, ticc_only_error_source
    except ImportError:
        log.error("peppar_fix library not available for servo")
        return 1

    try:
        ptp = PtpDevice(args.servo)
    except OSError as e:
        log.error(f"Cannot open PTP device {args.servo}: {e}")
        return 1

    caps = ptp.get_caps()
    log.info(f"PHC: {args.servo}, max_adj={caps['max_adj']} ppb, "
             f"n_extts={caps['n_ext_ts']}, n_pins={caps['n_pins']}")

    _log_do_characterization(args)

    # Preserve adjfine from bootstrap — read before switching actuator.
    bootstrap_adj = ptp.read_adjfine()
    log.info("PHC adjfine from bootstrap: %.1f ppb", bootstrap_adj)

    # Construct frequency actuator based on profile
    from peppar_fix.phc_actuator import PhcAdjfineActuator
    actuator = PhcAdjfineActuator(ptp)
    if getattr(args, 'clockmatrix_bus', None) is not None:
        try:
            from peppar_fix.clockmatrix import ClockMatrixI2C
            from peppar_fix.clockmatrix_actuator import ClockMatrixActuator
            cm_i2c = ClockMatrixI2C(
                bus_num=args.clockmatrix_bus,
                addr=int(getattr(args, 'clockmatrix_addr', '0x58'), 0))
            cm_dpll = getattr(args, 'clockmatrix_dpll_actuator', 3)
            actuator = ClockMatrixActuator(cm_i2c, dpll_id=cm_dpll)
            log.info("Using ClockMatrix actuator: bus=%d dpll=%d",
                     args.clockmatrix_bus, cm_dpll)
        except Exception as e:
            log.error("ClockMatrix actuator init failed: %s — falling back to PHC adjfine", e)
            actuator = PhcAdjfineActuator(ptp)
    actuator.setup()

    # For ClockMatrix: set up TDC phase source.
    # Bootstrap already set the FCW and zeroed adjfine — the actuator's
    # setup() inherits the FCW value. No transfer needed.
    cm_phase_source = None
    current_adj = actuator.read_frequency_ppb()
    if current_adj == 0 and abs(bootstrap_adj) > 1.0:
        # Actuator doesn't have a frequency set — use bootstrap's value.
        # This happens with PhcAdjfineActuator (normal PHC path).
        current_adj = bootstrap_adj
    if getattr(args, 'clockmatrix_bus', None) is not None:
        try:
            from peppar_fix.clockmatrix_phase import ClockMatrixPhaseSource
            cm_phase_dpll = getattr(args, 'clockmatrix_dpll_phase', 2)
            cm_pps_clk = getattr(args, 'clockmatrix_pps_clk', 2)
            cm_phase_source = ClockMatrixPhaseSource(
                cm_i2c, dpll_id=cm_phase_dpll, pps_clk=cm_pps_clk)
            cm_phase_source.setup()
            log.info("Using ClockMatrix TDC phase source: DPLL_%d, CLK%d",
                     cm_phase_dpll, cm_pps_clk)
        except Exception as e:
            log.error("ClockMatrix phase source failed: %s — using EXTTS", e)
            cm_phase_source = None
    log.info("Actuator freq at start: %.1f ppb (%s)", current_adj,
             type(actuator).__name__)
    if args.freerun:
        log.info("FREERUN MODE: PHC will not be steered. "
                 "Auto-stop at |pps_error| > %.0f ns",
                 args.freerun_max_error_ns or float('inf'))
        if not args.ticc_port:
            log.warning(
                "WARNING: freerun without --ticc-port. EXTTS TDEV measurements "
                "are unreliable — both i226 and E810 have ~8 ns effective "
                "resolution that masks real timing noise. Use --ticc-port for "
                "TDEV characterization, or pair with a separate TICC capture."
            )

    # Import PTP constants for pin setup
    from peppar_fix.ptp_device import PTP_PF_EXTTS, DualEdgeFilter

    extts_channel = args.extts_channel
    extts_ok = False
    # Try ioctl pin programming first (i226), fall back to sysfs
    # (E810 ice driver rejects PTP_PIN_SETFUNC ioctl but accepts
    # sysfs writes). This makes EXTTS Just Work on both platforms.
    pin_set = False
    if args.program_pin and caps['n_pins'] > 0:
        try:
            ptp.set_pin_function(args.pps_pin, PTP_PF_EXTTS, extts_channel)
            pin_set = True
            log.info("Pin %d programmed for EXTTS channel %d via ioctl",
                     args.pps_pin, extts_channel)
        except OSError:
            log.info("Pin config ioctl not supported; trying sysfs")
    if not pin_set and caps['n_pins'] > 0 and args.pps_pin is not None:
        from phc_bootstrap import _set_pin_function_sysfs, _E810_PIN_NAMES
        pin_name = _E810_PIN_NAMES.get(args.pps_pin, str(args.pps_pin))
        if _set_pin_function_sysfs(args.servo, pin_name,
                                   PTP_PF_EXTTS, extts_channel):
            pin_set = True
            log.info("Pin %s programmed for EXTTS channel %d via sysfs",
                     pin_name, extts_channel)
    if not pin_set:
        log.info("Skipping pin programming; using implicit EXTS mapping")
    try:
        ptp.enable_extts(extts_channel, rising_edge=True)
        log.info(f"EXTTS enabled: pin={args.pps_pin}, channel={extts_channel}")
        extts_ok = True
    except OSError as e:
        if args.ticc_drive:
            log.warning("EXTTS unavailable (%s) — TICC-driven servo does not require it", e)
        else:
            log.error("EXTTS failed: %s", e)
            ptp.close()
            return 1

    # Verify PPS is actually arriving before committing to the servo loop.
    # Use a DualEdgeFilter so the i226 dual-edge quirk doesn't fool the
    # one-shot test (it would otherwise sometimes return the falling edge,
    # which still proves PPS is arriving but is misleading for diagnostics).
    extts_dedup = DualEdgeFilter()
    if extts_ok:
        test_pps = ptp.read_extts_dedup(extts_dedup, timeout_ms=3000)
        if test_pps is None and not args.ticc_drive:
            log.error("No PPS event within 3s after enabling EXTTS — "
                      "check PPS wiring, pin config, and PTP device")
            ptp.disable_extts(extts_channel)
            ptp.close()
            return 3  # no PPS — wrapper should retry
        elif test_pps is None:
            log.warning("No PPS on EXTTS — TICC will provide servo feedback")
    else:
        test_pps = None
    if test_pps is not None:
        phc_sec, phc_nsec = test_pps[0], test_pps[1]
        pps_err = phc_nsec if phc_nsec < 500_000_000 else phc_nsec - 1_000_000_000
        log.info("PPS verified: phc_sec=%d error=%+d ns", phc_sec, pps_err)
    else:
        log.info("PPS verification skipped — TICC provides servo feedback")

    if getattr(args, 'do_freq_est', False):
        from peppar_fix.do_freq_est import DOFreqEst
        sigma_ticc = 0.178 if args.ticc_drive else 1.9
        # Seed TCXO phase from bootstrap dt_rx so the filter starts in
        # full 4-state mode from epoch 1 (no mid-run transition).
        drift_path = getattr(args, 'drift_file', None) or 'data/drift.json'
        bootstrap_dt_rx_ns = None
        bootstrap_base_freq = None
        try:
            with open(drift_path) as f:
                drift_data = json.load(f)
            bootstrap_dt_rx_ns = drift_data.get('dt_rx_ns')
            bootstrap_base_freq = drift_data.get('adjfine_ppb')
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        if bootstrap_dt_rx_ns is not None:
            log.info("DOFreqEst: seeding φ_tcxo from bootstrap dt_rx=%.1f ns",
                     bootstrap_dt_rx_ns)
        else:
            log.warning("DOFreqEst: no bootstrap dt_rx — running 2-state only")
        if bootstrap_base_freq is not None:
            log.info("DOFreqEst: crystal freq from drift file: %.1f ppb "
                     "(current_adj=%.1f, glide=%.1f)",
                     bootstrap_base_freq, current_adj,
                     current_adj - bootstrap_base_freq)
        servo = DOFreqEst(
            sigma_ticc_ns=sigma_ticc,
            sigma_do_phase_ns=0.92,
            sigma_do_freq_ppb=args.kalman_sigma_freq,
            sigma_tcxo_phase_ns=2.0,    # rx TCXO (F9T) PPS TDEV(1s)
            sigma_tcxo_freq_ppb=0.1,    # rx TCXO drift rate
            max_ppb=caps['max_adj'],
            initial_freq=current_adj,
            initial_dt_rx_ns=bootstrap_dt_rx_ns,
            base_freq=bootstrap_base_freq,
        )
        log.info("DOFreqEst 4-state: sigma_ticc=%.3f ns, "
                 "sigma_do=[0.92 ns, %.4f ppb], "
                 "sigma_tcxo=[2.0 ns, 0.1 ppb], "
                 "initial_freq=%.1f ppb, base_freq=%s, tcxo_init=%s",
                 sigma_ticc, args.kalman_sigma_freq, current_adj,
                 f"{bootstrap_base_freq:.1f}" if bootstrap_base_freq else "None",
                 bootstrap_dt_rx_ns is not None)
    elif getattr(args, 'kalman_servo', False):
        from peppar_fix.kalman_servo import KalmanServo
        # Measurement noise: TICC+qErr if TICC-drive, EXTTS+qErr otherwise
        sigma_meas = 0.178 if args.ticc_drive else 1.9
        servo = KalmanServo(
            sigma_meas_ns=sigma_meas,
            sigma_phase_ns=0.92,   # DO floor from adjfine noise test
            sigma_freq_ppb=args.kalman_sigma_freq,
            max_ppb=caps['max_adj'],
            initial_freq=current_adj,
            q_weight=args.kalman_q_weight,
            r_weight=args.kalman_r_weight,
            dead_zone_ppb=args.kalman_dead_zone,
        )
        carrier_fusion = (not getattr(args, 'no_carrier', False)
                          and args.ticc_drive)
        log.info("Kalman servo: sigma_meas=%.3f ns, sigma_phase=0.92 ns, "
                 "sigma_freq=%.4f ppb, q_weight=%.2f, r_weight=%.2f, "
                 "dead_zone=%.2f ppb, carrier_fusion=%s, "
                 "initial_freq=%.1f ppb",
                 sigma_meas, args.kalman_sigma_freq,
                 args.kalman_q_weight, args.kalman_r_weight,
                 args.kalman_dead_zone, carrier_fusion, current_adj)
    else:
        servo = PIServo(args.track_kp, args.track_ki, max_ppb=caps['max_adj'],
                        initial_freq=current_adj)
    scheduler = DisciplineScheduler(
        base_interval=args.discipline_interval,
        adaptive=args.adaptive_interval,
        min_interval=args.min_interval,
        max_interval=args.max_interval,
        converge_threshold_ns=args.scheduler_converge_threshold_ns,
        settle_window=args.scheduler_settle_window,
        unconverge_factor=args.scheduler_unconverge_factor,
    )

    qerr_alignment = {
        # Litmus 1: EXTTS PPS + qErr (matched to EXTTS epoch)
        "pps_var": RunningVarianceWindow(),
        "pps_qerr_plus_var": RunningVarianceWindow(),
        "pps_qerr_minus_var": RunningVarianceWindow(),
        # TICC qVIR is computed in the ticc_reader thread
        # (not here) using per-timestamp variance, not diff variance.
    }

    # PPS event queue
    pps_queue = queue.Queue(maxsize=10)
    pps_history = deque(maxlen=32)
    pps_history_lock = threading.Lock()
    stop_pps = threading.Event()
    stop_ticc = threading.Event()
    delay_injector = get_delay_injector()
    pps_recv_estimator = TimebaseRelationEstimator()
    ticc_tracker = None
    ticc_log_f = None
    ticc_log_w = None

    last_dedup_log_mono = [time.monotonic()]

    def extts_reader():
        while not stop_pps.is_set():
            event = ptp.read_extts(timeout_ms=1500)
            if event is None:
                continue
            phc_sec, phc_nsec, index, recv_mono, queue_remains, parse_age_s = event
            # Drop the falling edge of the i226 dual-edge timestamping quirk:
            # when the PPS pulse is wide (e.g. F9T default ~100 ms), the i226
            # reports both rising and falling edges and the engine would
            # otherwise see ~2 events/sec.  See ptp_device.DualEdgeFilter and
            # docs/madhat-bringup-2026-04-07.md stumble #10.
            if not extts_dedup.accept(phc_sec, phc_nsec):
                # Periodic summary so the user can see the filter working,
                # without spamming once-per-event log lines.
                now = time.monotonic()
                if now - last_dedup_log_mono[0] > 60.0:
                    log.info("EXTTS dual-edge filter: dropped %d, accepted %d "
                             "(min_spacing=%.3fs)",
                             extts_dedup.dropped, extts_dedup.accepted,
                             extts_dedup.min_spacing_s)
                    last_dedup_log_mono[0] = now
                continue
            delay_injector.maybe_inject_delay(f"ptp:{args.servo}")
            base_confidence = estimate_correlation_confidence(
                queue_remains=queue_remains,
                parse_age_s=parse_age_s,
            )
            estimator_sample = pps_recv_estimator.update(
                phc_sec + (phc_nsec / 1_000_000_000.0),
                recv_mono,
                sample_weight=estimator_sample_weight(
                    queue_remains=queue_remains,
                    base_confidence=base_confidence,
                ),
            )
            pps_event = PpsEvent(
                phc_sec=phc_sec,
                phc_nsec=phc_nsec,
                index=index,
                recv_mono=recv_mono,
                queue_remains=queue_remains,
                parse_age_s=parse_age_s,
                correlation_confidence=max(
                    0.05,
                    min(1.0, base_confidence * estimator_sample["confidence"]),
                ),
                estimator_residual_s=estimator_sample["residual_s"],
            )
            with pps_history_lock:
                pps_history.append(pps_event)
            dropped = _queue_put_drop_oldest(pps_queue, pps_event)
            if dropped:
                log.debug("Dropped one stale PPS notification due to full queue")

    if not args.ticc_drive:
        # EXTTS-driven: PPS events come from EXTTS hardware timestamps
        t_extts = threading.Thread(target=extts_reader, daemon=True)
        t_extts.start()
        log.info("EXTTS reader started (PPS source for correlation)")
    else:
        t_extts = None
        log.info("TICC-driven mode: TICC chB replaces EXTTS for PPS correlation")

    if args.ticc_port:
        ticc_tracker = TiccPairTracker(args.ticc_phc_channel, args.ticc_ref_channel)
        if args.ticc_log:
            ticc_log_f = open(args.ticc_log, 'w', newline='')
            ticc_log_w = csv.writer(ticc_log_f)
            ticc_log_w.writerow([
                'host_timestamp', 'host_monotonic', 'ref_sec', 'ref_ps', 'channel'
            ])
            ticc_log_f.flush()

        qerr_ticc_tracker = QErrTimescaleTracker()
        # TICC qVIR: pure correlation check, no DO in the picture.
        # Tracks chB interval deviations (PPS sawtooth) and checks
        # whether matched qerr removes that variance.
        _chb_raw_var = RunningVarianceWindow(maxlen=64)
        _chb_corr_var = RunningVarianceWindow(maxlen=64)
        _chb_qvir_count = [0]

        def ticc_reader():
            # When TICC-driven, the reference channel (chB) also generates
            # PpsEvent objects for the correlation gate — replacing EXTTS.
            # We derive the GPS second from host monotonic time + the
            # realtime-to-PHC offset known from bootstrap.
            ticc_pps_estimator = TimebaseRelationEstimator()

            while not stop_ticc.is_set():
                try:
                    with Ticc(args.ticc_port, args.ticc_baud, wait_for_boot=True) as ticc:
                        log.info("TICC reader started on %s", args.ticc_port)
                        for event in ticc.iter_events():
                            if stop_ticc.is_set():
                                return
                            if ticc_log_w is not None:
                                ticc_log_w.writerow([
                                    datetime.now(tz=timezone.utc).isoformat(),
                                    f"{event.recv_mono:.9f}",
                                    event.ref_sec,
                                    event.ref_ps,
                                    event.channel,
                                ])
                                ticc_log_f.flush()
                            # Ingest first so _armed is set after boot
                            # discard period completes.
                            was_armed = ticc_tracker._armed
                            ticc_tracker.ingest(event)

                            # Match qerr to chB by TIM-TP-initiated
                            # windowing.  See docs/qerr-correlation.md
                            # for the full design and rationale.
                            # See docs/stream-timescale-correlation.md.
                            #
                            # No queue_remains gate here — the timing
                            # window (0.8-1.1s) is the freshness check
                            # for qErr correlation.  queue_remains is
                            # only needed for timescale relationship
                            # estimation (case 1 in the design).  With
                            # correctly-aligned PEROUT, chA and chB
                            # arrive nearly simultaneously so the serial
                            # buffer always has data, making
                            # queue_remains=True on every chB event.
                            if event.channel == args.ticc_ref_channel:
                                _qerr = None
                                if was_armed:
                                    pending = qerr_store.get_pending_for_chb()
                                    if pending is not None:
                                        pend_mono, pend_qerr = pending
                                        delay = event.recv_mono - pend_mono
                                        if 0.8 <= delay <= 1.1:
                                            _qerr = pend_qerr
                                            qerr_store.clear_pending()
                                ticc_tracker.set_pending_ref_qerr(
                                    event.ref_sec, _qerr)
                                # TICC qVIR: apply qerr to each chB
                                # TIMESTAMP (not intervals).  Corrected
                                # = chB_phase + qerr.  Detrended variance
                                # of corrected should be much smaller than
                                # raw (sawtooth removed, leaving TICC noise).
                                # Pure F9T PPS + TICC, no DO.
                                phase_ns = event.ref_ps / 1000.0
                                _chb_raw_var.add(phase_ns)
                                if _qerr is not None:
                                    _chb_corr_var.add(phase_ns + _qerr)
                                    _chb_qvir_count[0] += 1
                                    if _chb_qvir_count[0] % 100 == 0:
                                        rv = _chb_raw_var.detrended_variance()
                                        cv = _chb_corr_var.detrended_variance()
                                        if rv and cv and cv > 0:
                                            qvir = rv / cv
                                            log.info("TICC qVIR: %.1f "
                                                     "(raw=%.2f corr=%.2f ns²)",
                                                     qvir, rv, cv)

                            # In TICC-drive mode, chB (reference PPS) events
                            # replace EXTTS as the PPS source for correlation.
                            if args.ticc_drive and event.channel == args.ticc_ref_channel:
                                # Derive approximate PHC second from realtime
                                # (PPS fires at the GPS second boundary; recv_mono
                                # is within ~100ms of true PPS time)
                                rt_ns = time.clock_gettime_ns(time.CLOCK_REALTIME)
                                rt_sec = rt_ns // 1_000_000_000
                                # Apply TAI offset if PHC is in TAI timescale
                                offset_s = 0
                                if hasattr(args, 'phc_timescale'):
                                    if args.phc_timescale == 'tai':
                                        offset_s = getattr(args, 'leap', 18) + getattr(args, 'tai_minus_gps', 19)
                                    elif args.phc_timescale == 'gps':
                                        offset_s = getattr(args, 'tai_minus_gps', 19)
                                phc_sec_approx = rt_sec + offset_s

                                pps_event = PpsEvent(
                                    phc_sec=phc_sec_approx,
                                    phc_nsec=0,  # PPS is at the second boundary
                                    index=-1,    # not from EXTTS
                                    recv_mono=event.recv_mono,
                                    queue_remains=event.queue_remains,
                                    parse_age_s=event.parse_age_s,
                                    correlation_confidence=event.correlation_confidence,
                                    estimator_residual_s=event.estimator_residual_s,
                                )
                                with pps_history_lock:
                                    pps_history.append(pps_event)
                                _queue_put_drop_oldest(pps_queue, pps_event)

                except Exception as exc:
                    if stop_ticc.is_set():
                        return
                    log.warning("TICC reader reconnect after error: %s", exc)
                    time.sleep(1.0)

        t_ticc = threading.Thread(target=ticc_reader, daemon=True)
        t_ticc.start()
    else:
        t_ticc = None

    # Servo log file
    log_f = None
    log_w = None
    if args.servo_log:
        log_f = open(args.servo_log, 'w', newline='')
        log_w = csv.writer(log_f)
        log_w.writerow([
            'timestamp', 'gps_second', 'phc_sec', 'phc_nsec',
            'phc_rounded_sec', 'epoch_offset_s', 'timescale_error_ns',
            'extts_index', 'pps_match_delta_s', 'pps_match_recv_dt_s', 'pps_queue_depth',
            'obs_confidence', 'obs_estimator_residual_s',
            'pps_confidence', 'pps_estimator_residual_s', 'match_confidence',
            'broadcast_confidence', 'ssr_confidence',
            'dt_rx_ns', 'dt_rx_sigma_ns', 'pps_error_ns', 'qerr_ns',
            'qerr_offset_s', 'pps_err_ticc_ns', 'ticc_age_s',
            'ticc_confidence', 'pps_var_ns2',
            'pps_qerr_plus_var_ns2', 'pps_qerr_plus_ratio',
            'pps_qerr_minus_var_ns2', 'pps_qerr_minus_ratio',
            'carrier_error_ns',
            'source', 'source_error_ns', 'source_confidence_ns',
            'adjfine_ppb', 'phase', 'n_meas', 'gain_scale',
            'discipline_interval', 'n_accumulated', 'watchdog_alarm',
            'tracking_mode', 'time_to_zero_s',
            'isb_gal_ns', 'isb_bds_ns',
            'phc_gettime_ns',
            'qerr_unwrapped_ns', 'qerr_freq_ns_s', 'qerr_dt_rx_rate_discrep_ns_s',
            'synth_phase_ns',
        ])

    return {
        'ptp': ptp,
        'phc_dev': args.servo,
        'drift_file': getattr(args, 'drift_file', None) or 'data/drift.json',
        'actuator': actuator,
        'cm_phase_source': cm_phase_source,
        'servo': servo,
        'scheduler': scheduler,
        'qerr_store': qerr_store,
        'qerr_alignment': qerr_alignment,
        'pps_queue': pps_queue,
        'pps_history': pps_history,
        'pps_history_lock': pps_history_lock,
        'stop_pps': stop_pps,
        'stop_ticc': stop_ticc,
        'ticc_tracker': ticc_tracker,
        'qerr_ticc_tracker': qerr_ticc_tracker if args.ticc_port else None,
        'ticc_log_f': ticc_log_f,
        'extts_channel': extts_channel,
        'caps': caps,
        'log_f': log_f,
        'log_w': log_w,
        'freerun': args.freerun,
        'phase': 'freerun' if args.freerun else 'tracking',
        'adjfine_ppb': current_adj,
        'gain_scale': 1.0,
        'prev_source': None,
        'tmode_set': False,
        'position_saved': False,
        'compute_error_sources': compute_error_sources,
        'ticc_only_error_source': ticc_only_error_source,
        'ppp_cal': PPPCalibration(tick_ns=8.0, min_samples=10),
        'qerr_beat_note': QErrBeatNote(freq_window=30),
        'dt_rx_buffer': deque(maxlen=30),
        'carrier_tracker': _init_carrier_tracker(args),
        'tracking_large_error_count': 0,
        'tracking_mode': 'pull_in',
        'pmc': _open_pmc(args),
        'pmc_announced': None,
        'ticc_prev_error_ns': None,
        'ticc_prev_error_mono': None,
        'consecutive_outliers': 0,
        'ticc_settled_count': 0,
        'holdover': {
            'active': False,
            'reason': '',
            'entered': 0,
            'exited': 0,
            'reasons': {},
        },
    }

    # ── TICC sanity check ─────────────────────────────────────────── #
    # When TICC-driven, wait for the first differential measurement
    # and verify it's within a sane range.  A large offset (e.g., ±500 ms)
    # indicates PEROUT misalignment or PHC not bootstrapped properly.
    if args.ticc_drive and ticc_tracker is not None:
        TICC_SANITY_TIMEOUT = 10  # seconds
        TICC_SANITY_MAX_NS = 100_000_000  # 100 ms — beyond this, exit for re-bootstrap
        log.info("Waiting for TICC sanity check (first differential measurement)...")
        t0 = _time.monotonic()
        first_diff = None
        while _time.monotonic() - t0 < TICC_SANITY_TIMEOUT:
            diff = ticc_tracker.latest_diff_ns()
            if diff is not None:
                first_diff = diff
                break
            _time.sleep(0.5)

        if first_diff is None:
            log.error("TICC sanity check: no differential measurement in %ds — "
                      "check TICC wiring (chA=PEROUT, chB=PPS)", TICC_SANITY_TIMEOUT)
            _cleanup_servo(ctx)
            return 3  # retry
        elif abs(first_diff) > TICC_SANITY_MAX_NS:
            log.error("TICC sanity check FAILED: diff=%+.0f ns (limit ±%.0f ns). "
                      "PEROUT may be misaligned or PHC not bootstrapped. "
                      "Exiting for re-bootstrap (exit code 5).",
                      first_diff, TICC_SANITY_MAX_NS)
            ctx = ctx  # keep reference for cleanup
            _cleanup_servo(ctx)
            return 5  # PHC diverged — wrapper will re-bootstrap
        else:
            log.info("TICC sanity check passed: diff=%+.1f ns", first_diff)


def _pps_fractional_error(phc_nsec):
    """Compute PHC error from PPS fractional second."""
    if phc_nsec <= 500_000_000:
        return float(phc_nsec)
    else:
        return float(phc_nsec) - 1_000_000_000


def _enter_obs_holdover(ctx, args, reason_code, detail):
    """Return servo to a safe state after an observation outage."""
    holdover = ctx['holdover']
    if holdover['active']:
        return
    holdover['active'] = True
    holdover['reason'] = reason_code
    holdover['entered'] += 1
    holdover['reasons'][reason_code] = holdover['reasons'].get(reason_code, 0) + 1
    last_freq = ctx['adjfine_ppb']
    log.warning(
        "Entering holdover: reason=%s detail=%s; "
        "holding PHC at last adjfine=%.1f ppb (temperature-stable assumption)",
        reason_code,
        detail,
        last_freq,
    )
    _set_clock_class(ctx, "holdover")
    # Do NOT zero adjfine — the last known frequency is almost certainly
    # better than zero.  TCXO/OCXO drift is dominated by temperature;
    # if temperature hasn't changed, the old frequency is correct.
    _purge_pps_state(ctx)
    from peppar_fix import PIServo, DisciplineScheduler
    ctx['servo'] = PIServo(args.track_kp, args.track_ki, max_ppb=ctx['caps']['max_adj'],
                           initial_freq=last_freq)
    ctx['scheduler'] = DisciplineScheduler(
        base_interval=args.discipline_interval,
        adaptive=args.adaptive_interval,
        min_interval=args.min_interval,
        max_interval=args.max_interval,
        converge_threshold_ns=args.scheduler_converge_threshold_ns,
        settle_window=args.scheduler_settle_window,
        unconverge_factor=args.scheduler_unconverge_factor,
    )
    ctx['phase'] = 'tracking'
    ctx['tracking_large_error_count'] = 0
    ctx['tracking_mode'] = 'pull_in'
    ctx['ticc_prev_error_ns'] = None
    ctx['ticc_prev_error_mono'] = None
    ctx['ticc_settled_count'] = 0


def _exit_holdover(ctx, detail):
    """Leave holdover after fresh usable observations return."""
    holdover = ctx['holdover']
    if not holdover['active']:
        return
    log.info(
        "Leaving holdover: reason=%s detail=%s",
        holdover['reason'],
        detail,
    )
    holdover['active'] = False
    holdover['reason'] = ''
    holdover['exited'] += 1


def _phc_gps_offset_s(phc_sec, phc_nsec, gps_unix_sec):
    """Whole-second offset: PHC_time - GPS_time."""
    phc_rounded = phc_sec if phc_nsec < 500_000_000 else phc_sec + 1
    return phc_rounded - gps_unix_sec


def _target_timescale_sec(gps_time, args):
    """Map a RAWX GPS epoch to the PPS second it aligns with.

    RAWX rcvTow is typically ~N.997 — just before the integer second.
    The PPS edge that aligns with this epoch is second N (floor), not
    N+1 (round).  Using round() here introduces a systematic +1s error
    in the epoch_offset calculation.
    """
    gps_sec = int(gps_time.timestamp())  # floor, not round
    if args.phc_timescale == "gps":
        return gps_sec
    if args.phc_timescale == "utc":
        return gps_sec - args.leap
    if args.phc_timescale == "tai":
        return gps_sec + args.tai_minus_gps
    raise ValueError(f"Unsupported PHC timescale: {args.phc_timescale}")


def _find_pps_event_for_obs(ctx, obs_event, target_sec, timeout=0.5,
                            min_window_s=0.5, max_window_s=11.0):
    """Correlate one observation epoch against PPS history.

    Prefer PPS events whose receive-monotonic timestamp is within an acceptable
    correlation window of the observation event. Among those, choose the event
    whose rounded PHC second best matches the target timescale second.
    """
    deadline = time.monotonic() + timeout

    while True:
        result = _match_pps_event_from_history(
            ctx, obs_event, target_sec,
            min_window_s=min_window_s,
            max_window_s=max_window_s,
        )
        event, delta, recv_dt = result[0], result[1], result[2]
        if event is not None:
            return event, delta, recv_dt

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise queue.Empty
        ctx['pps_queue'].get(timeout=remaining)


def _match_pps_event_from_history(ctx, obs_event, target_sec,
                                  min_window_s=0.5, max_window_s=11.0):
    """Return the best PPS history match for an observation, if any."""
    with ctx['pps_history_lock']:
        return match_pps_event_from_history(
            ctx['pps_history'],
            obs_event,
            target_sec,
            min_window_s=min_window_s,
            max_window_s=max_window_s,
        )


def _cm_servo_epoch(ctx, args, n_epochs, dt_rx_ns, dt_rx_sigma):
    """ClockMatrix-only servo epoch: TDC phase → PI → FCW.

    Reads phase error from the ClockMatrix TDC (DPLL PFD measuring
    PPS vs clock tree) and steers frequency via FCW. No EXTTS, no PHC,
    no PPS correlation — everything is I2C.
    """
    cm_phase = ctx['cm_phase_source']
    servo = ctx['servo']
    scheduler = ctx['scheduler']
    log_w = ctx.get('log_w')

    phase_ns = cm_phase.read_phase_ns()
    if phase_ns is None:
        if n_epochs % 10 == 0:
            log.info("  [%d] No ClockMatrix phase reading", n_epochs)
        return "no_phase"

    # The TDC PFD measures PPS vs DPLL output.
    # Positive = output behind PPS = need to speed up.
    # Use TDC as the sole error source with 50 ps confidence.
    from peppar_fix.error_sources import ErrorSource
    source = ErrorSource('CM_TDC', phase_ns, 0.050)

    TRACK_OUTLIER_NS = args.track_outlier_ns
    if TRACK_OUTLIER_NS is not None and abs(phase_ns) > TRACK_OUTLIER_NS:
        ctx['consecutive_outliers'] = ctx.get('consecutive_outliers', 0) + 1
        if ctx['consecutive_outliers'] >= 30:
            log.error("  %d consecutive outliers — exiting (code 5)",
                      ctx['consecutive_outliers'])
            ctx['phc_diverged'] = True
            return "outlier"
        return "outlier"
    else:
        ctx['consecutive_outliers'] = 0

    scheduler.accumulate(source.error_ns, source.confidence_ns, source.name)

    if scheduler.should_correct():
        avg_error, avg_confidence, n_samples = scheduler.flush()

        BASE_KP = args.track_kp
        BASE_KI = args.track_ki
        GAIN_REF_SIGMA = args.gain_ref_sigma
        GAIN_MIN_SCALE = args.gain_min_scale
        GAIN_MAX_SCALE = args.gain_max_scale
        gain_scale = max(GAIN_MIN_SCALE, min(GAIN_MAX_SCALE,
                         GAIN_REF_SIGMA / avg_confidence))
        servo.kp = BASE_KP * gain_scale
        servo.ki = BASE_KI * gain_scale

        # ClockMatrix FCW: positive error → positive freq (opposite of adjfine)
        freq_ppb = servo.update(avg_error, dt=float(n_samples))

        max_ppb = args.track_max_ppb or 244_000.0
        if abs(freq_ppb) > max_ppb:
            freq_ppb = math.copysign(max_ppb, freq_ppb)
        if abs(freq_ppb) >= max_ppb * 0.95:
            servo.integral = freq_ppb / servo.ki if servo.ki != 0 else 0
            log.warning('  Anti-windup: freq=%+.0fppb at rail', freq_ppb)

        if not args.freerun:
            ctx['actuator'].adjust_frequency_ppb(freq_ppb)
        ctx['adjfine_ppb'] = freq_ppb
        ctx['gain_scale'] = gain_scale

        scheduler.update_drift_rate(time.monotonic(), freq_ppb)
        scheduler.compute_adaptive_interval(avg_confidence)

        if n_epochs % 10 == 0:
            log.info("  [%d] CM_TDC: err=%+.1fns (avg %d) freq=%+.1fppb "
                     "gain=%.2fx interval=%d",
                     n_epochs, avg_error, n_samples, freq_ppb,
                     gain_scale, scheduler.interval)

    if log_w is not None:
        log_w.writerow({
            'epoch': n_epochs,
            'source': 'CM_TDC',
            'source_error_ns': phase_ns,
            'source_confidence_ns': 0.050,
            'adjfine_ppb': ctx.get('adjfine_ppb', 0),
            'gain_scale': ctx.get('gain_scale', 1.0),
            'discipline_interval': scheduler.interval,
        })

    return "ok"


def _servo_epoch(ctx, args, filt, obs_event, corr_snapshot, n_epochs,
                 dt_rx_ns, dt_rx_sigma, n_used, known_ecef,
                 resid_rms, isb_gal_ns, isb_bds_ns, pps_match=None):
    """Process one servo epoch: read PPS, compute error, steer PHC."""

    # ClockMatrix TDC fast path: read phase directly via I2C, skip EXTTS/PPS
    cm_phase = ctx.get('cm_phase_source')
    if cm_phase is not None:
        return _cm_servo_epoch(ctx, args, n_epochs, dt_rx_ns, dt_rx_sigma)

    ptp = ctx['ptp']
    servo = ctx['servo']
    scheduler = ctx['scheduler']
    qerr_store = ctx['qerr_store']
    qerr_alignment = ctx['qerr_alignment']
    pps_queue = ctx['pps_queue']
    ticc_tracker = ctx.get('ticc_tracker')
    log_w = ctx['log_w']
    compute_error_sources = ctx['compute_error_sources']
    ticc_only_error_source = ctx.get('ticc_only_error_source')
    skip_stats = ctx.get('skip_stats')

    BASE_KP = args.track_kp
    BASE_KI = args.track_ki
    GAIN_REF_SIGMA = args.gain_ref_sigma
    GAIN_MIN_SCALE = args.gain_min_scale
    GAIN_MAX_SCALE = args.gain_max_scale
    # Convergence boost disabled — bootstrap handles convergence.
    # The boost caused oscillation with gentle gains (overnight run 2026-03-25).
    CONVERGE_ERROR_NS = 1_000_000
    CONVERGE_MIN_SCALE = args.converge_min_scale
    TRACK_RESTEP_NS = args.track_restep_ns
    TRACK_OUTLIER_NS = args.track_outlier_ns

    # Once filter converges: switch F9T to timing mode
    if n_epochs >= 300 and dt_rx_sigma < 100.0:
        sigma_m = dt_rx_sigma * 1e-9 * C

        if args.position_file and not ctx['position_saved'] and sigma_m < 0.1:
            save_position(
                args.position_file, known_ecef,
                sigma_m=sigma_m, source="peppar_fix",
                note=f"saved after {n_epochs} epochs, dt_rx_sigma={dt_rx_sigma:.2f}ns",
            )
            ctx['position_saved'] = True
            log.info(f"Position saved to {args.position_file} "
                     f"(sigma={sigma_m:.4f}m)")

        if not ctx['tmode_set'] and sigma_m < 0.1:
            try:
                from peppar_fix.receiver import get_driver as _get_driver
                _drv = _get_driver(args.receiver)
                tmode_msg = _drv.build_tmode_fixed_msg(known_ecef)
                if tmode_msg is not None:
                    # Would need config_queue to serial_reader — skip for now
                    # The F9T timing mode can be set separately via configure_f9t.py
                    ctx['tmode_set'] = True
                    lat, lon, alt = ecef_to_lla(
                        known_ecef[0], known_ecef[1], known_ecef[2])
                    log.info(f"F9T timing mode ready "
                             f"({lat:.6f}, {lon:.6f}, {alt:.1f}m)")
            except ImportError:
                ctx['tmode_set'] = True

    gps_time = obs_event.gps_time
    target_sec = _target_timescale_sec(gps_time, args)
    if pps_match is not None:
        pps_event, pps_match_delta_s, pps_match_recv_dt_s, _match_confidence = pps_match
    else:
        try:
            pps_event, pps_match_delta_s, pps_match_recv_dt_s = _find_pps_event_for_obs(
                ctx, obs_event, target_sec, timeout=0.5
            )
        except queue.Empty:
            if n_epochs % 10 == 0:
                log.info(f"  [{n_epochs}] No PPS event for this epoch")
            return "no_pps"

    phc_sec, phc_nsec, extts_index = pps_event
    phc_rounded_sec = phc_sec if phc_nsec < 500_000_000 else phc_sec + 1
    epoch_offset = phc_rounded_sec - target_sec
    ts_str = gps_time.strftime('%Y-%m-%d %H:%M:%S')
    pps_err_extts_ns = _pps_fractional_error(phc_nsec)
    timescale_error_ns = epoch_offset * 1_000_000_000 + pps_err_extts_ns
    pps_queue_depth = pps_queue.qsize()

    # Match qErr to this PPS edge by host monotonic time.  TIM-TP
    # arrives ~900 ms before the PPS it describes; correlating by
    # monotonic clock avoids all GPS TOW / receiver clock bias issues.
    # See docs/stream-timescale-correlation.md "TICC–qErr epoch matching"
    # for why epoch alignment is critical and how it's verified.
    # Match qErr to the EXTTS PPS edge (for EXTTS qVIR and non-TICC paths).
    # See docs/stream-timescale-correlation.md for the full correlation model.
    # qerr values are OFFSETS (corrections), not timestamps.  Each is
    # matched to a specific PPS edge via CLOCK_MONOTONIC.  After adding
    # qerr to a timestamp, the result is still on the timestamp's
    # timescale (EXTTS/PHC or TICC) — the qerr just removes the F9T
    # PPS quantization from the reference edge.
    #
    # qerr_for_extts_pps_ns: matched to the EXTTS PPS edge (pps_event.recv_mono)
    # qerr_for_ticc_pps_ns:  matched to the TICC PPS edge (ticc_measurement.recv_mono)
    qerr_for_extts_pps_ns, qerr_offset_s = qerr_store.match_pps_mono(pps_event.recv_mono)
    qerr_for_ticc_pps_ns = None
    pps_err_ticc_ns = None
    ticc_age_s = None
    ticc_confidence = None
    if ticc_tracker is not None:
        ticc_measurement = ticc_tracker.latest(time.monotonic(), args.ticc_max_age_s)
        if ticc_measurement is not None:
            # PPS error = gnss_pps - do_pps, measured on TICC timescale.
            # Positive = DO is late (needs to advance).
            # ticc_measurement.diff_ns is chA-chB (do_pps-gnss_pps),
            # so we negate to get gnss_pps-do_pps.
            pps_err_ticc_ns = -(ticc_measurement.diff_ns - args.ticc_target_ns)
            # Sanity: if the TICC diff is larger than 100 ms, PEROUT is
            # grossly misaligned (e.g., 500 ms offset).  Log loudly and
            # do NOT silently use this as a servo input.
            if abs(pps_err_ticc_ns) > 100_000_000:
                log.error("TICC diff = %+.0f ns — PEROUT is misaligned "
                          "(raw diff_ns=%+.0f). NOT using as servo input.",
                          pps_err_ticc_ns, ticc_measurement.diff_ns)
                pps_err_ticc_ns = None
            ticc_age_s = max(0.0, time.monotonic() - ticc_measurement.recv_mono)
            ticc_confidence = ticc_measurement.confidence
            # qerr for this TICC measurement was matched at chB arrival
            # time in the ticc_reader thread — deterministic, no race
            # with latest().  See docs/stream-timescale-correlation.md.
            qerr_for_ticc_pps_ns = ticc_measurement.ref_qerr_ns
    if qerr_for_extts_pps_ns is None and n_epochs % 10 == 0:
        log.info("  [%s] qErr match miss (mono)", n_epochs)
    elif qerr_for_extts_pps_ns is not None and n_epochs % 10 == 0:
        log.info(
            "  [%s] qErr match ok: extts_offset=%.3fs extts_qerr=%+.1fns"
            "  ticc_qerr=%s",
            n_epochs,
            qerr_offset_s if qerr_offset_s is not None else -1.0,
            qerr_for_extts_pps_ns,
            f"{qerr_for_ticc_pps_ns:+.1f}ns" if qerr_for_ticc_pps_ns is not None else "None",
        )
    # ── qErr beat note: unwrap phase and cross-validate against dt_rx ──
    qerr_beat = ctx['qerr_beat_note']
    _qerr_dt_rx_discrep = None
    _qerr_freq_ns_s = None
    _synth_phase_ns = None
    # Prefer TICC-matched qErr (more precise epoch), fall back to EXTTS
    _beat_qerr = qerr_for_ticc_pps_ns if qerr_for_ticc_pps_ns is not None else qerr_for_extts_pps_ns
    if _beat_qerr is not None:
        unwrapped_ns = qerr_beat.update(_beat_qerr)
        _qerr_freq_ns_s, freq_n = qerr_beat.frequency_offset_ns_per_s()
        # Synthesize phase: dt_rx resolves tick, qErr gives sub-tick
        if dt_rx_ns is not None and dt_rx_sigma is not None and dt_rx_sigma < 2.0:
            _synth_phase_ns = qerr_beat.synthesize_phase(dt_rx_ns, _beat_qerr)
            discrep, cal = qerr_beat.cross_validate_dt_rx(dt_rx_ns)
            if cal:
                _qerr_dt_rx_discrep = discrep
            if n_epochs % 30 == 0:
                synth_str = f"synth={_synth_phase_ns:.1f}ns " if _synth_phase_ns is not None else ""
                if cal and discrep is not None:
                    log.info("  [%d] qErr beat note: unwrapped=%.1f ns, "
                             "%sfreq=%.3f ns/s (%d samples), "
                             "dt_rx rate discrepancy=%+.2f ns/s",
                             n_epochs, unwrapped_ns, synth_str,
                             _qerr_freq_ns_s if _qerr_freq_ns_s is not None else 0.0,
                             freq_n, discrep)
                elif not cal:
                    log.info("  [%d] qErr beat note: unwrapped=%.1f ns, "
                             "%sfreq=%.3f ns/s (%d samples), calibrating",
                             n_epochs, unwrapped_ns, synth_str,
                             _qerr_freq_ns_s if _qerr_freq_ns_s is not None else 0.0,
                             freq_n)

    # ── Litmus test 1: EXTTS PPS + qErr ──
    # Uses qerr_for_extts_pps_ns matched to the EXTTS PPS epoch.
    cum_adj = qerr_alignment.get("cumulative_adjfine_ns", 0.0)
    rate_compensated = pps_err_extts_ns - cum_adj
    qerr_alignment["cumulative_adjfine_ns"] = cum_adj + ctx['adjfine_ppb']
    qerr_alignment["pps_var"].add(rate_compensated)
    if qerr_for_extts_pps_ns is not None:
        qerr_alignment["pps_qerr_plus_var"].add(rate_compensated + qerr_for_extts_pps_ns)
        qerr_alignment["pps_qerr_minus_var"].add(rate_compensated - qerr_for_extts_pps_ns)
    # TICC qVIR (the definitive correlation check) runs in the
    # ticc_reader thread, not here.  See TICC qVIR log messages.
    # Carrier phase tracker: auto-init and accumulate adjfine
    carrier_tracker = ctx.get('carrier_tracker')
    if carrier_tracker is not None and not getattr(args, 'no_carrier', False):
        if dt_rx_ns is not None and dt_rx_sigma is not None:
            just_initialized = (not carrier_tracker.initialized
                                and carrier_tracker.try_auto_init(dt_rx_ns))
            if just_initialized:
                # Anchor the Carrier zero-point to PPS truth.  Without
                # this, the servo drives carrier_error to zero but
                # carries a hidden bias relative to pps_error.
                carrier_tracker.anchor_to_pps(pps_err_extts_ns, dt_rx_ns)
                if carrier_tracker._anchored:
                    log.info("Carrier tracker: anchored to PPS "
                             "(offset=%+.1f ns)",
                             carrier_tracker.phase_anchor_ns)
        if carrier_tracker.initialized:
            carrier_tracker.accumulate_adjfine(ctx['adjfine_ppb'])
            carrier_tracker.update_drift_estimate(
                dt_rx_ns, pps_err_extts_ns, ctx['adjfine_ppb'])

    # Time-differenced dt_rx tracking (for future 4-state filter).
    # The 2-state Kalman's frequency state conflates f_phc_drift and
    # f_tcxo, so Δdt_rx (which observes f_tcxo alone) can't be injected
    # as a frequency measurement without breaking the state semantics.
    # The 4-state DOFreqEst filter (see architecture-vision.md) will
    # properly separate these.  For now, just track Δdt_rx for logging.
    _delta_dt_rx_ns = None
    if dt_rx_ns is not None:
        prev_dt_rx = ctx.get('_prev_dt_rx_ns')
        if prev_dt_rx is not None:
            delta = dt_rx_ns - prev_dt_rx
            if abs(delta) < 5000.0:
                _delta_dt_rx_ns = delta
        ctx['_prev_dt_rx_ns'] = dt_rx_ns

    pps_var_ns2 = qerr_alignment["pps_var"].diff_variance()
    pps_qerr_plus_var_ns2 = qerr_alignment["pps_qerr_plus_var"].diff_variance()
    pps_qerr_minus_var_ns2 = qerr_alignment["pps_qerr_minus_var"].diff_variance()
    qerr_plus_ratio = None
    qerr_minus_ratio = None
    if (
        pps_var_ns2 is not None and
        pps_qerr_plus_var_ns2 is not None and
        pps_qerr_plus_var_ns2 > 0.0
    ):
        qerr_plus_ratio = pps_var_ns2 / pps_qerr_plus_var_ns2
    if (
        pps_var_ns2 is not None and
        pps_qerr_minus_var_ns2 is not None and
        pps_qerr_minus_var_ns2 > 0.0
    ):
        qerr_minus_ratio = pps_var_ns2 / pps_qerr_minus_var_ns2
    if n_epochs % 10 == 0:
        if qerr_plus_ratio is not None and qerr_alignment["pps_qerr_plus_var"].count() >= 8:
            label = ("good" if qerr_plus_ratio >= 1.5
                     else "ok" if qerr_plus_ratio >= 1.0
                     else "BAD")
            lvl = log.info if qerr_plus_ratio >= 1.0 else log.warning
            lvl("  [%s] EXTTS qVIR: Δvar(pps)/Δvar(pps+qErr) = %.2f (%s)",
                n_epochs, qerr_plus_ratio, label)

    # Litmus 2: TICC + qErr (separate epoch matching)
    if args.ticc_drive:
        if pps_err_ticc_ns is None:
            if skip_stats is not None:
                skip_stats["ticc_missing_pair"] += 1
            if n_epochs % 10 == 0:
                health = ticc_tracker.health() if ticc_tracker is not None else {}
                last_seen = health.get("last_seen", {})
                counts = health.get("counts", {})
                armed = health.get("armed", False)
                buffered_drops = health.get("buffered_drops", 0)
                now = time.monotonic()
                phc_last = last_seen.get(args.ticc_phc_channel)
                ref_last = last_seen.get(args.ticc_ref_channel)
                log.info(
                    "  [%s] Awaiting fresh paired TICC measurement: "
                    "armed=%s buffered_drops=%s "
                    "%s_count=%s last=%.3fs %s_count=%s last=%.3fs",
                    n_epochs,
                    armed,
                    buffered_drops,
                    args.ticc_phc_channel,
                    counts.get(args.ticc_phc_channel, 0),
                    (now - phc_last) if phc_last is not None else -1.0,
                    args.ticc_ref_channel,
                    counts.get(args.ticc_ref_channel, 0),
                    (now - ref_last) if ref_last is not None else -1.0,
                )
            return "no_ticc"
        # Apply qErr correction to the TICC measurement before using it
        # as the servo error.  The raw TICC chA-chB diff includes the
        # F9T PPS quantization noise (~2.3 ns) on the chB (reference)
        # side.  Without correction, the servo faithfully tracks that
        # noise and injects it into the DO — making the output WORSE
        # than the DO's free-running 1.17 ns.  With correction, the
        # effective reference noise drops to ~178 ps (the TICC+qErr
        # floor), well below the DO, so the servo can actually improve it.
        # Apply qErr to produce pps_err_ticc_qerr_ns.
        # pps_err_ticc_ns = raw PPS error on the TICC timescale (never mutated).
        # pps_err_ticc_qerr_ns = same with PPS quantization removed.
        # These are different values with different noise properties;
        # never conflate them in a single variable.
        pps_err_ticc_qerr_ns = pps_err_ticc_ns  # start with raw
        pps_corr_applied = False
        if getattr(args, 'pps_corr', None) == 'ppp':
            ppp_cal = ctx['ppp_cal']
            dt_rx_buf = ctx['dt_rx_buffer']
            # Calibrate against TIM-TP during first ~10 converged epochs.
            # Use qerr_for_ticc_pps_ns (matched to TICC epoch) when available.
            cal_qerr = qerr_for_ticc_pps_ns if qerr_for_ticc_pps_ns is not None else qerr_for_extts_pps_ns
            if (not ppp_cal.calibrated and cal_qerr is not None
                    and dt_rx_sigma is not None and dt_rx_sigma < 1.0):
                done = ppp_cal.add_sample(dt_rx_ns, cal_qerr)
                if done:
                    log.info("  PPP qErr calibration done: offset=%.3f ns "
                             "(%d samples)", ppp_cal.offset_ns, ppp_cal._n)
            # Feed dt_rx into the sliding window
            if dt_rx_ns is not None and dt_rx_sigma is not None and dt_rx_sigma < 1.0:
                dt_rx_buf.append(dt_rx_ns)
            # Once calibrated and buffer has enough samples, use smoothed qerr
            if ppp_cal.calibrated and len(dt_rx_buf) >= 5:
                from peppar_fix.error_sources import ppp_qerr as _ppp_qerr
                smoothed = _dt_rx_trend_predict(dt_rx_buf)
                if smoothed is not None:
                    pps_corr_ppp_ns = _ppp_qerr(smoothed, cal_offset_ns=ppp_cal.offset_ns)
                    pps_err_ticc_qerr_ns = pps_err_ticc_ns + pps_corr_ppp_ns
                    pps_corr_applied = True
                    if n_epochs % 10 == 0 and qerr_for_extts_pps_ns is not None:
                        raw_ppp = _ppp_qerr(dt_rx_ns, cal_offset_ns=ppp_cal.offset_ns)
                        log.info("  [%d] smooth qErr=%.3f raw=%.3f TIM-TP=%.3f "
                                 "(smooth-TIM=%.3f, raw-TIM=%.3f, buf=%d)",
                                 n_epochs, pps_corr_ppp_ns, raw_ppp, qerr_for_extts_pps_ns,
                                 pps_corr_ppp_ns - qerr_for_extts_pps_ns,
                                 raw_ppp - qerr_for_extts_pps_ns,
                                 len(dt_rx_buf))
        if not pps_corr_applied:
            if qerr_for_ticc_pps_ns is not None:
                pps_err_ticc_qerr_ns = pps_err_ticc_ns + qerr_for_ticc_pps_ns
            elif qerr_for_extts_pps_ns is not None:
                pps_err_ticc_qerr_ns = pps_err_ticc_ns + qerr_for_extts_pps_ns
        # ── EXTTS → TICC servo drive transition ──
        # During pull-in (large TICC error), drive from gnss_pps_extts
        # (on the PHC timescale we discipline).  EXTTS has 8 ns
        # resolution but that's fine when the error is hundreds of ns.
        # Once the TICC error converges below the threshold, switch to
        # gnss_pps_ticc (60 ps, timescale-independent diff).
        # The threshold is based on the raw TICC error (pps_err_ticc_ns)
        # — NOT pps_err_extts_ns, which reads near 0 after bootstrap
        # step even when PEROUT hasn't converged.
        TICC_DRIVE_THRESHOLD_NS = getattr(args, 'ticc_drive_threshold_ns', 100.0)
        use_extts = (abs(pps_err_ticc_ns) > TICC_DRIVE_THRESHOLD_NS
                     and qerr_for_extts_pps_ns is not None)
        if use_extts:
            pps_err_extts_qerr_ns = pps_err_extts_ns + qerr_for_extts_pps_ns
            sources = ticc_only_error_source(pps_err_extts_qerr_ns,
                                             args.ticc_confidence_ns)
            if ctx.get('ticc_drive_source') != 'extts':
                log.info("  Servo drive: EXTTS (|ticc_err|=%.0f ns > %.0f threshold)",
                         abs(pps_err_ticc_ns), TICC_DRIVE_THRESHOLD_NS)
                ctx['ticc_drive_source'] = 'extts'
        else:
            sources = ticc_only_error_source(pps_err_ticc_qerr_ns,
                                             args.ticc_confidence_ns)
            if ctx.get('ticc_drive_source') != 'ticc':
                log.info("  Servo drive: TICC (|ticc_err|=%.0f ns ≤ %.0f threshold)",
                         abs(pps_err_ticc_ns), TICC_DRIVE_THRESHOLD_NS)
                ctx['ticc_drive_source'] = 'ticc'
        corr_age_for_inflation = None  # not applicable in TICC-drive mode
    else:
        # Feed PPP calibration: compare PPP-derived qerr against TIM-TP
        # qErr for the first ~10 epochs to determine the constant offset.
        ppp_cal = ctx['ppp_cal']
        if (not ppp_cal.calibrated and qerr_for_extts_pps_ns is not None
                and dt_rx_sigma is not None and dt_rx_sigma < args.carrier_max_sigma_ns):
            done = ppp_cal.add_sample(dt_rx_ns, qerr_for_extts_pps_ns)
            if done:
                log.info(f"  PPP calibration done: offset={ppp_cal.offset_ns:+.3f}ns "
                         f"({ppp_cal._n} samples)")
        # Correction age drives sigma inflation on Carrier and PPS+PPP.
        # When NTRIP goes stale, those sources lose competition gracefully
        # to PPS+qErr / PPS instead of dying.
        corr_age_for_inflation = None
        if corr_snapshot is not None:
            ages = [a for a in (corr_snapshot.get("broadcast_age_s"),
                                corr_snapshot.get("ssr_age_s"))
                    if a is not None]
            if ages:
                corr_age_for_inflation = max(ages)
        sources = compute_error_sources(
            pps_err_extts_ns,
            None if args.no_qerr else qerr_for_extts_pps_ns,
            dt_rx_ns,
            dt_rx_sigma,
            carrier_max_sigma=args.carrier_max_sigma_ns,
            ppp_cal=None if args.no_ppp else ppp_cal,
            carrier_tracker=(None if getattr(args, 'no_carrier', False)
                             else ctx.get('carrier_tracker')),
            corr_age_s=corr_age_for_inflation,
            corr_staleness_ns_per_s=getattr(
                args, 'corr_staleness_ns_per_s', 0.1),
        )
    best = sources[0]

    # Source-change logging: when the winner of the source competition
    # changes, emit a one-liner so postmortem can spot graceful degradation
    # cascades (Carrier → PPS+qErr → PPS → holdover).
    last_source_name = ctx.get('last_source_name')
    if last_source_name != best.name:
        log.info(
            "Source change: %s → %s (err=%+.1fns σ=%.1fns, corr_age=%s)",
            last_source_name or "(none)", best.name,
            best.error_ns, best.confidence_ns,
            f"{corr_age_for_inflation:.1f}s"
            if corr_age_for_inflation is not None else "n/a",
        )
        ctx['last_source_name'] = best.name

    # No warmup or step phases — PHC bootstrap handles phase and frequency.
    # PI tracking from epoch 1.

    # Tracking phase
    mode_time_to_zero_s = None
    mode_gain_floor = None
    if args.ticc_drive and best.name == 'TICC':
        mode_name, mode_time_to_zero_s = _update_ticc_tracking_mode(
            ctx, args, best, time.monotonic()
        )
        if mode_name == 'pull_in':
            scheduler._converging = False
            scheduler.interval = max(args.min_interval, min(args.max_interval, args.ticc_pullin_interval))
        elif mode_name == 'landing':
            scheduler._converging = True
            scheduler.interval = 1
            mode_gain_floor = args.ticc_landing_gain_floor
        else:
            scheduler._converging = False
            scheduler.interval = max(args.min_interval, min(args.max_interval, args.ticc_settled_interval))

    if (
        TRACK_OUTLIER_NS is not None and
        abs(best.error_ns) > TRACK_OUTLIER_NS and
        not scheduler._converging
    ):
        ctx['consecutive_outliers'] += 1
        if ctx['consecutive_outliers'] >= 30:
            log.error("  %d consecutive outliers — servo has lost control. "
                      "Exiting for re-bootstrap (exit code 5).",
                      ctx['consecutive_outliers'])
            ctx['phc_diverged'] = True
            return "outlier"
        log.warning(f"  Outlier: {best}, skipping ({ctx['consecutive_outliers']}/30)")
        return "outlier"
    else:
        ctx['consecutive_outliers'] = 0

    scheduler.accumulate(best.error_ns, best.confidence_ns, best.name)
    # Three-stage clockClass promotion: 248 (boot) → 52 (PHC bootstrapped,
    # set by wrapper after bootstrap) → 6 (servo settled).  Demote back
    # to 52 if the scheduler leaves settled state.
    # In freerun mode, clockClass stays at 248 — PHC is not disciplined.
    if not args.freerun:
        if not scheduler._converging:
            _set_clock_class(ctx, "locked")
        else:
            _set_clock_class(ctx, "initialized")

    if TRACK_RESTEP_NS is not None and not args.freerun:
        # Use pps_err_extts_ns (raw PHC fractional offset) for the restep
        # check, not best.error_ns which includes the filter's dt_rx.
        # After a step, dt_rx is stale and large while the filter
        # reconverges — checking it would cause spurious resteps.
        if abs(pps_err_extts_ns) >= TRACK_RESTEP_NS:
            ctx['tracking_large_error_count'] += 1
        else:
            ctx['tracking_large_error_count'] = 0
        if ctx['tracking_large_error_count'] >= 3:
            log.error(
                "PHC error above %.0fns for %d consecutive epochs — "
                "exiting for PHC re-bootstrap (exit code 5)",
                TRACK_RESTEP_NS,
                ctx['tracking_large_error_count'],
            )
            ctx['phc_diverged'] = True

    # Freerun auto-stop: exit when PPS error grows too large for
    # the correlation gate to work reliably.
    if args.freerun and args.freerun_max_error_ns is not None:
        if abs(pps_err_extts_ns) >= args.freerun_max_error_ns:
            log.info(
                "Freerun auto-stop: |pps_error|=%.0fns exceeds %.0fns threshold",
                abs(pps_err_extts_ns), args.freerun_max_error_ns,
            )
            ctx['phc_diverged'] = True

    # TODO(ta-e744, ta-7j06): Re-enable timescale restep once the step
    # source is GNSS-derived (not system clock).  The PI servo tracks
    # frequency well from any starting phase; absolute phase alignment
    # requires a reliable step source.

    if ctx['prev_source'] != best.name:
        if ctx['prev_source'] is not None:
            log.info(f"  Source: {ctx['prev_source']} → {best.name} "
                     f"(confidence {best.confidence_ns:.1f}ns)")
        ctx['prev_source'] = best.name

    # Post-step cooldown: skip frequency corrections while the filter
    # reconverges.  Without this, stale dt_rx drives the servo to
    # over-correct, undoing the step.
    cooldown = ctx.get('post_step_cooldown', 0)
    if cooldown > 0:
        ctx['post_step_cooldown'] = cooldown - 1
        scheduler.flush()  # drain accumulated samples
        return "cooldown"

    if scheduler.should_correct():
        avg_error, avg_confidence, n_samples = scheduler.flush()

        gain_scale = max(GAIN_MIN_SCALE, min(GAIN_MAX_SCALE,
                         GAIN_REF_SIGMA / avg_confidence))
        if abs(avg_error) > CONVERGE_ERROR_NS:
            gain_scale = max(gain_scale, CONVERGE_MIN_SCALE)
        if mode_gain_floor is not None:
            gain_scale = max(gain_scale, mode_gain_floor)

        # In TICC+qErr-drive mode, adapt gain based on error magnitude.
        # The TICC+qErr reference noise (178 ps) is far below the DO floor
        # (0.92 ns), so TINY corrections (< 1 ppb) are invisible.  But the
        # bootstrap leaves a ~5 µs phase offset that needs aggressive pull-in.
        # Solution: interpolate kp between pull-in gain and settled gain
        # based on error magnitude.  Settled gain keeps corrections < 1 ppb
        # when the error is < 5 ns (the adjfine noise test showed < 1 ppb
        # corrections add unmeasurable noise to the DO).
        if args.ticc_drive and abs(avg_error) < 500:
            # Error is small enough for gentle gains.  Scale kp down
            # proportionally to error magnitude: at 500 ns use full kp,
            # at 5 ns use kp/100.  This keeps corrections below 1 ppb
            # when settled while still pulling in from moderate errors.
            error_ratio = max(abs(avg_error), 5.0) / 500.0
            adaptive_scale = max(0.01, min(1.0, error_ratio))
            gain_scale *= adaptive_scale

        is_ekf = getattr(args, 'do_freq_est', False)

        if is_ekf and pps_err_ticc_ns is not None:
            # DOFreqEst EKF: pass raw TICC (no qErr) + PPP dt_rx.
            # The EKF has its own LQR gain — skip PI gain adaptation,
            # landing mode, and anti-windup (those are PI-specific).
            adjfine_ppb = -servo.update(
                pps_err_ticc_ns, dt=float(n_samples),
                dt_rx_ns=dt_rx_ns, dt_rx_sigma_ns=dt_rx_sigma)
        else:
            servo.kp = BASE_KP * gain_scale
            servo.ki = BASE_KI * gain_scale
            adjfine_ppb = -servo.update(avg_error, dt=float(n_samples))
        max_track_ppb = min(
            ctx['caps']['max_adj'],
            args.track_max_ppb if args.track_max_ppb is not None else ctx['caps']['max_adj'],
        )
        if abs(adjfine_ppb) > max_track_ppb:
            log.warning(
                "  Tracking clamp: adj=%+.1fppb limited to %+.1fppb",
                adjfine_ppb,
                math.copysign(max_track_ppb, adjfine_ppb),
            )
            adjfine_ppb = math.copysign(max_track_ppb, adjfine_ppb)
        if not is_ekf and args.ticc_drive and best.name == 'TICC' and ctx.get('tracking_mode') == 'landing':
            landing_floor_ppb = abs(avg_error) / max(1e-6, args.ticc_landing_horizon_s)
            landing_floor_ppb = min(max_track_ppb, landing_floor_ppb)
            desired_sign = math.copysign(1.0, -avg_error) if avg_error != 0 else 1.0
            if abs(adjfine_ppb) < landing_floor_ppb:
                adjfine_ppb = math.copysign(landing_floor_ppb, desired_sign)
        # Anti-windup: if adjfine is at the rail, reset integral
        # to prevent windup-driven oscillation (PI servo only)
        if not is_ekf and abs(adjfine_ppb) >= max_track_ppb * 0.95:
            servo.integral = -adjfine_ppb / servo.ki if servo.ki != 0 else 0
            log.warning(f'  Anti-windup: adj={adjfine_ppb:+.0f}ppb at rail, integral reset')
        if not args.freerun:
            ctx['actuator'].adjust_frequency_ppb(adjfine_ppb)
        ctx['adjfine_ppb'] = adjfine_ppb
        ctx['gain_scale'] = gain_scale

        scheduler.update_drift_rate(time.monotonic(), adjfine_ppb)
        scheduler.compute_adaptive_interval(avg_confidence)
        if not is_ekf and args.ticc_drive and best.name == 'TICC' and ctx.get('tracking_mode') == 'landing':
            scheduler.interval = 1

        if n_epochs % 10 == 0:
            mode_suffix = ''
            if args.ticc_drive and best.name == 'TICC':
                ttz = f"{mode_time_to_zero_s:.1f}s" if mode_time_to_zero_s is not None else 'na'
                mode_suffix = f" mode={ctx.get('tracking_mode')} t0={ttz}"
            ct = ctx.get('carrier_tracker')
            if (ct is not None and ct.initialized
                    and best.name == 'Carrier' and n_epochs % 60 == 0):
                mode_suffix += (f" anchor_resid={ct.anchor_residual_ns:+.1f}ns")
            log.info(f"  [{n_epochs}] {best.name}: "
                     f"err={avg_error:+.1f}ns (avg {n_samples}) "
                     f"adj={adjfine_ppb:+.1f}ppb "
                     f"gain={gain_scale:.2f}x "
                     f"interval={scheduler.interval}{mode_suffix}")
    else:
        if n_epochs % 10 == 0:
            mode_suffix = ''
            if args.ticc_drive and best.name == 'TICC':
                ttz = f"{mode_time_to_zero_s:.1f}s" if mode_time_to_zero_s is not None else 'na'
                mode_suffix = f" mode={ctx.get('tracking_mode')} t0={ttz}"
            log.info(f"  [{n_epochs}] {best.name}: "
                     f"err={best.error_ns:+.1f}ns "
                     f"coast ({scheduler.n_accumulated}/{scheduler.interval}) "
                     f"adj={ctx['adjfine_ppb']:+.1f}ppb{mode_suffix}")

    # PHC time at PPS edge (from EXTTS hardware timestamp).
    phc_gettime_ns = phc_sec * 1_000_000_000 + phc_nsec
    carrier_error_ns = None
    if carrier_tracker is not None and carrier_tracker.initialized:
        carrier_error_ns = carrier_tracker.compute_error(dt_rx_ns)
    _log_servo(log_w, ctx['log_f'], ts_str, target_sec, phc_sec, phc_nsec,
               phc_rounded_sec, epoch_offset, timescale_error_ns,
               extts_index, pps_match_delta_s, pps_match_recv_dt_s, pps_queue_depth,
               obs_event, pps_event, _match_confidence, corr_snapshot,
               dt_rx_ns, dt_rx_sigma, pps_err_extts_ns, qerr_for_extts_pps_ns, qerr_offset_s,
               pps_err_ticc_ns, ticc_age_s, ticc_confidence,
               pps_var_ns2, pps_qerr_plus_var_ns2, qerr_plus_ratio,
               pps_qerr_minus_var_ns2, qerr_minus_ratio,
               carrier_error_ns, best,
               ctx['adjfine_ppb'], ctx['phase'], n_used,
               ctx['gain_scale'], scheduler, isb_gal_ns, isb_bds_ns,
               ctx.get('tracking_mode'), mode_time_to_zero_s,
               phc_gettime_ns,
               qerr_unwrapped_ns=qerr_beat.accumulated_phase_ns() if qerr_beat.n_samples > 0 else None,
               qerr_freq_ns_s=_qerr_freq_ns_s,
               qerr_dt_rx_discrep_ns=_qerr_dt_rx_discrep,
               synth_phase_ns=_synth_phase_ns)
    return "logged"


def _log_servo(log_w, log_f, ts_str, gps_unix_sec, phc_sec, phc_nsec,
               phc_rounded_sec, epoch_offset_s, timescale_error_ns,
               extts_index, pps_match_delta_s, pps_match_recv_dt_s, pps_queue_depth,
               obs_event, pps_event, match_confidence, corr_snapshot,
               dt_rx_ns, dt_rx_sigma, pps_err_extts_ns, qerr_for_extts_pps_ns, qerr_offset_s,
               pps_err_ticc_ns, ticc_age_s, ticc_confidence,
               pps_var_ns2, pps_qerr_plus_var_ns2, qerr_plus_ratio,
               pps_qerr_minus_var_ns2, qerr_minus_ratio,
               carrier_error_ns, best,
               adjfine_ppb, phase, n_used, gain_scale, scheduler,
               isb_gal_ns, isb_bds_ns, tracking_mode, time_to_zero_s,
               phc_gettime_ns=None,
               qerr_unwrapped_ns=None, qerr_freq_ns_s=None,
               qerr_dt_rx_discrep_ns=None, synth_phase_ns=None):
    """Write one servo log row."""
    if log_w is None:
        return
    obs_confidence = getattr(obs_event, 'correlation_confidence', None)
    obs_residual_s = getattr(obs_event, 'estimator_residual_s', None)
    pps_confidence = getattr(pps_event, 'correlation_confidence', None)
    pps_residual_s = getattr(pps_event, 'estimator_residual_s', None)
    broadcast_confidence = None
    ssr_confidence = None
    if corr_snapshot is not None:
        broadcast_confidence = corr_snapshot.get('broadcast_confidence')
        ssr_confidence = corr_snapshot.get('ssr_confidence')
    log_w.writerow([
        ts_str, gps_unix_sec, phc_sec, phc_nsec,
        phc_rounded_sec, epoch_offset_s, f'{timescale_error_ns:.1f}',
        extts_index, pps_match_delta_s,
        f'{pps_match_recv_dt_s:.3f}', pps_queue_depth,
        f'{obs_confidence:.3f}' if obs_confidence is not None else '',
        f'{obs_residual_s:.6f}' if obs_residual_s is not None else '',
        f'{pps_confidence:.3f}' if pps_confidence is not None else '',
        f'{pps_residual_s:.6f}' if pps_residual_s is not None else '',
        f'{match_confidence:.3f}' if match_confidence is not None else '',
        f'{broadcast_confidence:.3f}' if broadcast_confidence is not None else '',
        f'{ssr_confidence:.3f}' if ssr_confidence is not None else '',
        f'{dt_rx_ns:.3f}', f'{dt_rx_sigma:.3f}',
        f'{pps_err_extts_ns:.1f}', f'{qerr_for_extts_pps_ns:.3f}' if qerr_for_extts_pps_ns is not None else '',
        f'{qerr_offset_s:.3f}' if qerr_offset_s is not None else '',
        f'{pps_err_ticc_ns:.3f}' if pps_err_ticc_ns is not None else '',
        f'{ticc_age_s:.3f}' if ticc_age_s is not None else '',
        f'{ticc_confidence:.3f}' if ticc_confidence is not None else '',
        f'{pps_var_ns2:.3f}' if pps_var_ns2 is not None else '',
        f'{pps_qerr_plus_var_ns2:.3f}' if pps_qerr_plus_var_ns2 is not None else '',
        f'{qerr_plus_ratio:.3f}' if qerr_plus_ratio is not None else '',
        f'{pps_qerr_minus_var_ns2:.3f}' if pps_qerr_minus_var_ns2 is not None else '',
        f'{qerr_minus_ratio:.3f}' if qerr_minus_ratio is not None else '',
        f'{carrier_error_ns:.3f}' if carrier_error_ns is not None else '',
        best.name, f'{best.error_ns:.3f}', f'{best.confidence_ns:.3f}',
        f'{adjfine_ppb:.3f}', phase, n_used, f'{gain_scale:.3f}',
        scheduler.interval, scheduler.n_accumulated, 0,
        tracking_mode or '',
        f'{time_to_zero_s:.3f}' if time_to_zero_s is not None else '',
        f'{isb_gal_ns:.3f}', f'{isb_bds_ns:.3f}',
        str(phc_gettime_ns) if phc_gettime_ns is not None else '',
        f'{qerr_unwrapped_ns:.3f}' if qerr_unwrapped_ns is not None else '',
        f'{qerr_freq_ns_s:.3f}' if qerr_freq_ns_s is not None else '',
        f'{qerr_dt_rx_discrep_ns:.1f}' if qerr_dt_rx_discrep_ns is not None else '',
        f'{synth_phase_ns:.3f}' if synth_phase_ns is not None else '',
    ])
    if log_f is not None:
        log_f.flush()


def _cleanup_servo(ctx):
    """Clean up servo resources."""
    ctx['stop_pps'].set()
    if 'stop_ticc' in ctx:
        ctx['stop_ticc'].set()
    ptp = ctx['ptp']
    if not ctx.get('freerun'):
        try:
            ctx['actuator'].adjust_frequency_ppb(0.0)
        except Exception:
            pass
    try:
        ctx['actuator'].teardown()
    except Exception:
        pass
    cm_phase = ctx.get('cm_phase_source')
    if cm_phase is not None:
        try:
            cm_phase.teardown()
        except Exception:
            pass
    try:
        ptp.disable_extts(ctx['extts_channel'])
    except OSError:
        pass  # EXTTS may not have been enabled (TICC-only mode)
    ptp.close()
    if ctx['log_f']:
        ctx['log_f'].close()
    if ctx.get('ticc_log_f'):
        ctx['ticc_log_f'].close()
    pmc = ctx.get('pmc')
    if pmc is not None:
        pmc.close()
    # Save refined oscillator corrections for next bootstrap
    _save_osc_freq_corr(ctx)
    log.info("PHC servo cleaned up")


def _purge_pps_state(ctx):
    """Drop PPS events captured before a PHC step.

    Historical EXTS events are invalid after stepping the PHC because they were
    timestamped on the old PHC timescale.
    """
    with ctx['pps_history_lock']:
        ctx['pps_history'].clear()
    while True:
        try:
            ctx['pps_queue'].get_nowait()
        except queue.Empty:
            break


def _locked_len(collection, lock):
    """Return len(collection) while holding lock, or 0 if either is None."""
    if collection is None or lock is None:
        return 0
    with lock:
        return len(collection)


def _check_queue_depths(queue_hwm, alert_armed, threshold, dump,
                        obs_queue, obs_history,
                        pps_history=None, pps_history_lock=None,
                        skip_stats=None, gate=None):
    """Update queue high-water marks and fire threshold alerts.

    High-water marks track the session maximum depth for each queue.
    When any queue's current depth reaches *threshold*, a warning is
    logged (with optional diagnostic dump via *dump*).  The alert
    re-arms once the queue drains below half the threshold.
    """
    now_mono = time.monotonic()
    depths = {
        "obs_queue": obs_queue.qsize(),
        "obs_history": len(obs_history),
        "pps_history": _locked_len(pps_history, pps_history_lock),
    }
    for name, depth in depths.items():
        if depth > queue_hwm[name]:
            queue_hwm[name] = depth
        if depth >= threshold and alert_armed[name]:
            alert_armed[name] = False
            # Age of oldest item (seconds behind real-time)
            oldest_age_s = None
            if name == "obs_history" and obs_history:
                oldest_age_s = now_mono - obs_history[0].recv_mono
            elif name == "pps_history" and pps_history and pps_history_lock:
                with pps_history_lock:
                    if pps_history:
                        oldest_age_s = now_mono - pps_history[0].recv_mono
            age_str = f"{oldest_age_s:.1f}s" if oldest_age_s is not None else "N/A"
            log.warning(
                "Queue depth threshold: %s depth=%d (threshold=%d) "
                "oldest_age=%s  all_depths: obs_q=%d obs_hist=%d pps_hist=%d",
                name, depth, threshold, age_str,
                depths["obs_queue"], depths["obs_history"], depths["pps_history"],
            )
            if dump:
                _dump_queue_diagnostics(
                    name, depths, queue_hwm, obs_history,
                    pps_history, pps_history_lock,
                    skip_stats, gate, now_mono,
                )
        # Re-arm alert when queue drains below half threshold
        if depth < max(1, threshold // 2) and not alert_armed[name]:
            alert_armed[name] = True


def _dump_queue_diagnostics(trigger_queue, depths, queue_hwm, obs_history,
                            pps_history, pps_history_lock,
                            skip_stats, gate, now_mono):
    """Dump detailed diagnostic state when a queue depth threshold is breached."""
    lines = [f"--- Queue diagnostic dump (triggered by {trigger_queue}) ---"]
    lines.append(f"  Current depths: {depths}")
    lines.append(f"  Session HWMs:   {queue_hwm}")

    # obs_history age spread
    if obs_history:
        oldest = now_mono - obs_history[0].recv_mono
        newest = now_mono - obs_history[-1].recv_mono
        lines.append(f"  obs_history age range: oldest={oldest:.2f}s newest={newest:.2f}s")
        # Show first few items' GPS times and confidence
        for i, ev in enumerate(obs_history):
            if i >= 5:
                lines.append(f"    ... ({len(obs_history) - 5} more)")
                break
            age = now_mono - ev.recv_mono
            conf = getattr(ev, "correlation_confidence", None)
            qr = getattr(ev, "queue_remains", None)
            lines.append(
                f"    [{i}] gps={ev.gps_time} age={age:.2f}s conf={conf} queue_remains={qr}"
            )

    # pps_history age spread
    if pps_history and pps_history_lock:
        with pps_history_lock:
            pps_snap = list(pps_history)
        if pps_snap:
            oldest = now_mono - pps_snap[0].recv_mono
            newest = now_mono - pps_snap[-1].recv_mono
            lines.append(f"  pps_history age range: oldest={oldest:.2f}s newest={newest:.2f}s "
                         f"(n={len(pps_snap)})")

    if skip_stats:
        lines.append(f"  skip_stats: {skip_stats}")

    if gate:
        lines.append(f"  gate_stats: {gate.stats.as_dict()}")

    lines.append("--- end diagnostic dump ---")
    log.warning("\n".join(lines))


def _queue_put_drop_oldest(qobj, item):
    """Enqueue one item, dropping at most one oldest entry if full.

    This queue is only a wakeup/notification path for sinks that keep their own
    history. When full, preserve continuity by dropping one oldest wakeup
    rather than draining the queue and erasing recent timing context.
    """
    try:
        qobj.put_nowait(item)
        return 0
    except queue.Full:
        try:
            qobj.get_nowait()
        except queue.Empty:
            pass
        qobj.put_nowait(item)
        return 1


def _append_queue_history(history, qobj, timeout=0.5):
    """Append one or more queued items into a history deque."""
    history.append(qobj.get(timeout=timeout))
    added = 1
    while True:
        try:
            history.append(qobj.get_nowait())
            added += 1
        except queue.Empty:
            return added


# ── Main ──────────────────────────────────────────────────────────────── #

def run(args):
    """Main entry point: bootstrap → steady state."""
    stop_event = threading.Event()
    gate_stats = None
    exit_code = 0
    # Verify receiver config on open (defensive: re-applies if needed).
    # This opens/closes the serial port to check for dual-freq observations,
    # reconfigures if single-freq, then releases the port for serial_reader.
    from peppar_fix.receiver import ensure_receiver_ready
    port_type = getattr(args, 'port_type', 'USB') or 'USB'
    systems_for_check = set(args.systems.split(',')) if args.systems else {'gps', 'gal'}
    driver = ensure_receiver_ready(args.serial, args.baud, port_type=port_type,
                                   systems=systems_for_check,
                                   sfrbx_rate=args.sfrbx_rate,
                                   measurement_rate_ms=args.measurement_rate_ms)
    if driver is None:
        driver = get_driver(args.receiver)
        log.warning("Receiver check failed — falling back to %s (may lack dual-freq)",
                    driver.name)
    mute_controller = get_source_mute_controller()
    mute_controller.install_signal_handlers()

    def on_signal(signum, frame):
        log.info("Signal received, shutting down")
        stop_event.set()
    signal.signal(signal.SIGTERM, on_signal)
    if args.pid_file:
        with open(args.pid_file, "w") as f:
            f.write(f"{os.getpid()}\n")
        log.info("Wrote PID file: %s", args.pid_file)

    # Shared state
    beph = BroadcastEphemeris()
    ssr = SSRState()
    corrections = RealtimeCorrections(beph, ssr)
    obs_queue = queue.Queue(maxsize=100)

    # QErr store (shared with serial reader if servo is active).
    # If --qerr-log was specified, open a CSV that captures every
    # TIM-TP message with its CLOCK_MONOTONIC arrival time, for
    # post-hoc index-matching against TICC chB events.
    qerr_store = None
    qerr_log_f = None
    if args.servo:
        qerr_log_writer = None
        if getattr(args, 'qerr_log', None):
            try:
                qerr_log_f = open(args.qerr_log, 'w', newline='')
                qerr_log_writer = csv.writer(qerr_log_f)
                qerr_log_writer.writerow([
                    'host_timestamp', 'host_monotonic', 'qerr_ns',
                    'tow_ms', 'qerr_invalid',
                ])
                qerr_log_f.flush()
                log.info("qErr CSV log: %s", args.qerr_log)
            except OSError as e:
                log.error("Failed to open qerr_log %s: %s", args.qerr_log, e)
                qerr_log_writer = None
                qerr_log_f = None
        qerr_store = QErrStore(log_writer=qerr_log_writer,
                               log_file=qerr_log_f)

    # Load NTRIP config
    load_ntrip_config(args)

    if not args.ntrip_caster and not args.eph_mount:
        log.warning("No NTRIP source — using broadcast ephemeris from receiver only")

    # Start NTRIP threads
    start_ntrip_threads(args, beph, ssr, stop_event)

    # Parse systems filter (needed before warmup)
    systems = set(args.systems.split(',')) if args.systems else None
    log.info(f"Systems: {systems}")

    # Wait for ephemeris
    if args.eph_mount:
        if not wait_for_ephemeris(beph, stop_event, systems=systems):
            return 1

    # NAV2 position store — captures the F9T secondary engine's fresh
    # position fix for the position-consensus watchdog.
    nav2_store = Nav2PositionStore()

    # Enable NAV2 secondary engine on the F9T if not already on.
    # This is done here (before serial_reader starts) because the
    # ensure_receiver step only runs the full config when dual-freq
    # observations are missing — if the F9T was already configured
    # for L5 from a previous run, the NAV2 keys would never be sent.
    # This quick config burst is harmless if NAV2 is already enabled.
    try:
        from peppar_fix.receiver import send_cfg, PORT_SUFFIX
        from peppar_fix.gnss_stream import open_gnss
        from pyubx2 import UBXReader as _UBR
        _nav2_ser, _ = open_gnss(args.serial, args.baud)
        _nav2_ubr = _UBR(_nav2_ser, protfilter=2)
        _pname = PORT_SUFFIX.get(args.port_type, "USB")
        _nav2_ok = send_cfg(_nav2_ser, _nav2_ubr, {
            "CFG_NAV2_OUT_ENABLED": 1,
            f"CFG_MSGOUT_UBX_NAV2_PVT_{_pname}": 5,
        }, "NAV2 secondary engine enable")
        _nav2_ser.close()
        if _nav2_ok:
            log.info("NAV2 secondary engine enabled (position consensus)")
        else:
            log.warning("NAV2 config failed (position consensus unavailable)")
    except Exception as e:
        log.warning("NAV2 config attempt failed: %s (continuing without)", e)

    # Start serial reader
    serial_kwargs = {}
    if qerr_store:
        serial_kwargs['qerr_store'] = qerr_store
    t_serial = threading.Thread(
        target=serial_reader,
        args=(args.serial, args.baud, obs_queue, stop_event, beph, systems, ssr),
        kwargs={**serial_kwargs, 'driver': driver, 'nav2_store': nav2_store},
        daemon=True,
    )
    t_serial.start()
    log.info(f"Serial: {args.serial} at {args.baud} baud")

    # Open CSV output
    out_f = None
    out_w = None
    if args.out:
        out_f = open(args.out, 'w', newline='')
        out_w = csv.writer(out_f)
        out_w.writerow([
            'timestamp', 'phase',
            'lat', 'lon', 'alt_m',
            'sigma_3d_m', 'clock_ns', 'clock_sigma_ns',
            'n_meas', 'rms_m',
            'correction_source', 'isb_gal_ns', 'isb_bds_ns',
            'n_ambiguities',
        ])

    # Determine starting phase
    known_ecef = None

    if args.known_pos:
        lat, lon, alt = [float(v) for v in args.known_pos.split(',')]
        known_ecef = lla_to_ecef(lat, lon, alt)
        log.info(f"Position (CLI): {lat:.6f}, {lon:.6f}, {alt:.1f}m")
    elif args.position_file:
        known_ecef = load_position(args.position_file)
        if known_ecef is not None:
            lat, lon, alt = ecef_to_lla(known_ecef[0], known_ecef[1], known_ecef[2])
            log.info(f"Position (file): {lat:.6f}, {lon:.6f}, {alt:.1f}m")

    try:
        if known_ecef is None:
            # Phase 1: Bootstrap
            result = run_bootstrap(args, obs_queue, corrections, stop_event,
                                   out_w=out_w)
            if result is None:
                log.error("Bootstrap failed — no converged position")
                return 1

            pos_ecef, sigma_m = result
            known_ecef = pos_ecef

            # Save position
            if args.position_file:
                save_position(args.position_file, pos_ecef, sigma_m,
                              "ppp_bootstrap",
                              note="converged during unified run")
                log.info(f"Position saved to {args.position_file}")

        if stop_event.is_set():
            return 0

        # Validate loaded position against live pseudorange fix.
        # A tampered or stale position file would send the FixedPosFilter
        # into 100+ km residuals without any warning.
        if args.position_file or args.known_pos:
            log.info('Validating loaded position against live LS fix...')
            for _attempt in range(30):
                if stop_event.is_set():
                    return 0
                try:
                    gps_time, observations = obs_queue.get(timeout=5)
                except Exception:
                    continue
                if len(observations) < 6:
                    continue
                # Use broadcast-only ephemeris for the LS validation check,
                # NOT the full SSR-corrected RealtimeCorrections object.
                # CAS single-AC SSR orbit+clock corrections cause the LS
                # solver to produce wildly wrong positions (altitude -2000m)
                # when the SSR correction reference frame doesn't match the
                # broadcast ephemeris's reference.  FixedPosFilter is immune
                # (time differencing cancels the bias) but the LS solver's
                # absolute pseudorange model is not.  Using broadcast-only
                # for validation gives ~5-10m accuracy which is plenty for
                # the 100m threshold check.
                x_ls, ok, n_sv = ls_init(observations, beph, gps_time,
                                          clk_file=None)
                if not ok or n_sv < 6:
                    continue
                ls_ecef = x_ls[:3]
                import numpy as _np
                separation_m = _np.linalg.norm(ls_ecef - known_ecef)
                ls_lat, ls_lon, ls_alt = ecef_to_lla(ls_ecef[0], ls_ecef[1], ls_ecef[2])
                log.info(f'  LS check: ({ls_lat:.4f}, {ls_lon:.4f}, {ls_alt:.0f}m) '
                         f'separation={separation_m:.0f}m from loaded position')
                if separation_m > 100:
                    log.error(f'Position file disagrees with live LS fix by {separation_m:.0f}m '
                              f'(threshold 100m). File may be stale or corrupted. '
                              f'Falling back to bootstrap.')
                    known_ecef = None
                    result = run_bootstrap(args, obs_queue, corrections, stop_event,
                                           out_w=out_w)
                    if result is None:
                        log.error('Bootstrap failed')
                        return 1
                    pos_ecef, sigma_m = result
                    known_ecef = pos_ecef
                    if args.position_file:
                        save_position(args.position_file, pos_ecef, sigma_m,
                                      'ppp_bootstrap', note='re-bootstrapped after position validation failure')
                else:
                    log.info(f'  Position validated (within {separation_m:.0f}m of LS fix)')
                break

        if stop_event.is_set():
            return 0

        # Phase 2: Steady state
        steady_result = run_steady_state(
            args,
            known_ecef,
            obs_queue,
            corrections,
            beph,
            ssr,
            stop_event,
            qerr_store=qerr_store,
            out_w=out_w,
            nav2_store=nav2_store,
        )
        # run_steady_state returns an int exit code on error,
        # or a gate_stats dict on normal completion.
        if isinstance(steady_result, int):
            exit_code = steady_result
        else:
            gate_stats = steady_result

    except KeyboardInterrupt:
        log.info("Interrupted")
    finally:
        stop_event.set()
        if out_f:
            out_f.close()
        if args.gate_stats and gate_stats is not None:
            with open(args.gate_stats, "w") as f:
                json.dump(gate_stats, f, indent=2, sort_keys=True)

    if args.pid_file:
        try:
            os.unlink(args.pid_file)
        except FileNotFoundError:
            pass
    return exit_code


# ── CLI ──────────────────────────────────────────────────────────────── #

def main():
    ap = argparse.ArgumentParser(
        description="Unified peppar-fix: GNSS position bootstrap + clock discipline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Two-phase operation:
  Phase 1 (Bootstrap): PPPFilter estimates position from scratch.
          Skipped if --known-pos or --position-file provides a position.
  Phase 2 (Steady state): FixedPosFilter estimates clock.
          Optional: --servo for PHC discipline, --out for CSV logging.
""",
    )

    # Position
    pos = ap.add_argument_group("Position")
    pos.add_argument("--known-pos",
                     help="Known position as lat,lon,alt (skips bootstrap)")
    pos.add_argument("--seed-pos",
                     help="Seed position for bootstrap (speeds convergence)")
    pos.add_argument("--position-file", default="data/position.json",
                     help="Position file for save/load (default: data/position.json)")
    pos.add_argument("--sigma", type=float, default=0.1,
                     help="Bootstrap convergence threshold in meters (default: 0.1)")
    pos.add_argument("--timeout", type=int, default=3600,
                     help="Bootstrap timeout in seconds (default: 3600)")
    pos.add_argument("--watchdog-threshold", type=float, default=0.5,
                     help="Position watchdog threshold in meters (default: 0.5)")

    # Serial
    serial = ap.add_argument_group("Serial")
    serial.add_argument("--serial", required=True,
                        help="Serial port for F9T (e.g. /dev/gnss-top)")
    serial.add_argument("--baud", type=int, default=115200)
    serial.add_argument("--receiver", default="f9t",
                        help="Receiver model/profile: f9t, f9t-l5, f10t (default: f9t)")
    serial.add_argument("--port-type", default="USB",
                        choices=["UART", "UART2", "USB", "SPI", "I2C"],
                        help="Receiver port type for UBX message routing (default: USB)")
    serial.add_argument("--measurement-rate-ms", type=int, default=None,
                        help="F9T measurement rate in ms (profile default: 1000 for i226, 2000 for E810)")
    serial.add_argument("--sfrbx-rate", type=int, default=None,
                        help="SFRBX output rate (0=disabled, 1=every epoch; profile default: 1 for serial, 0 for E810 I2C)")

    # GNSS
    gnss = ap.add_argument_group("GNSS")
    gnss.add_argument("--systems", default="gps,gal,bds",
                      help="GNSS systems (default: gps,gal,bds)")
    gnss.add_argument("--leap", type=int, default=18,
                      help="GPS-UTC leap seconds (default: 18)")
    gnss.add_argument("--tai-minus-gps", type=int, default=19,
                      help="TAI-GPS offset in seconds (default: 19)")

    # NTRIP (corrections input)
    ntrip = ap.add_argument_group("NTRIP corrections")
    ntrip.add_argument("--ntrip-conf", help="NTRIP config file (INI format)")
    ntrip.add_argument("--ntrip-caster", help="NTRIP caster hostname")
    ntrip.add_argument("--ntrip-port", type=int, default=2101)
    ntrip.add_argument("--ntrip-tls", action="store_true")
    ntrip.add_argument("--eph-mount", help="Broadcast ephemeris mountpoint")
    ntrip.add_argument("--ssr-mount", help="SSR corrections mountpoint")
    ntrip.add_argument("--ntrip-user", help="NTRIP username")
    ntrip.add_argument("--ntrip-password", help="NTRIP password")
    ntrip.add_argument("--max-broadcast-age-s", type=float, default=None,
                       help="Maximum host-monotonic age for broadcast correction state "
                            "(default: 600). Past this, the freshness gate skips EKF "
                            "updates entirely. Sigma inflation (see "
                            "--corr-staleness-ns-per-s) handles graceful degradation "
                            "well before reaching this hard limit.")
    ntrip.add_argument("--corr-staleness-ns-per-s", type=float, default=0.1,
                       help="Linear inflation rate (ns of σ per second of NTRIP "
                            "correction age) applied to Carrier and PPS+PPP error "
                            "sources. Default 0.1 puts those sources past the "
                            "PPS+qErr 3 ns floor at ~30 s of NTRIP staleness, so "
                            "the source competition hands off automatically.")
    ntrip.add_argument("--require-ssr", action="store_true", default=None,
                       help="Require fresh SSR state before EKF updates")
    ntrip.add_argument("--max-ssr-age-s", type=float, default=None,
                       help="Maximum host-monotonic age for SSR state when --require-ssr is set (default: 30)")
    ntrip.add_argument("--min-broadcast-confidence", type=float, default=None,
                       help="Minimum acceptable confidence for broadcast correction timing")
    ntrip.add_argument("--min-ssr-confidence", type=float, default=None,
                       help="Minimum acceptable confidence for SSR correction timing")

    # PHC servo (optional)
    servo = ap.add_argument_group("PHC servo (optional)")
    servo.add_argument("--ptp-profile",
                       help="PTP NIC profile for default PHC/pin/channel settings")
    servo.add_argument("--device-config", default="config/receivers.toml",
                       help="Device/profile config TOML (default: config/receivers.toml)")
    servo.add_argument("--servo", default=None,
                       help="PTP device for PHC servo (e.g. /dev/ptp0)")
    servo.add_argument("--pps-pin", type=int, default=None,
                       help="PTP pin index for PPS input (profile/default if omitted)")
    servo.add_argument("--extts-channel", type=int, default=None,
                       help="PTP EXTS channel for PPS input (profile/default if omitted)")
    servo.add_argument("--program-pin", action="store_true",
                       help="Explicitly program PTP pin function before enabling EXTS")
    servo.add_argument("--phc-timescale", choices=["gps", "utc", "tai"], default=None,
                       help="Target PHC timescale for PPS alignment (profile/default if omitted)")
    servo.add_argument("--min-correlation-confidence", type=float, default=None,
                       help="Minimum acceptable confidence for observation/PPS correlation")
    servo.add_argument("--max-correlation-window-s", type=float, default=None,
                       help="Max recv_mono delta for obs/PPS correlation (default: 11s, increase for high-latency transports like E810 I2C)")
    servo.add_argument("--kalman-servo", action="store_true",
                       help="Use 2-state Kalman filter + LQR servo instead of PI. "
                            "Optimal pull-in (no overshoot) and noise-matched "
                            "steady-state tracking.  Noise parameters from "
                            "DO characterization + TICC+qErr measurement.")
    servo.add_argument("--do-freq-est", action="store_true",
                       help="Use 4-state DOFreqEst (architecture vision). "
                            "Fuses TICC+qErr with PPP dt_rx in a single filter "
                            "that models both oscillators (TCXO + DO).  Supersedes "
                            "--kalman-servo when both are given.")
    servo.add_argument("--kalman-q-weight", type=float, default=1.0,
                       help="Kalman process noise Q scale (>1 = more aggressive "
                            "tracking, <1 = smoother output)")
    servo.add_argument("--kalman-r-weight", type=float, default=1.0,
                       help="Kalman measurement noise R scale (>1 = trust "
                            "measurements less, <1 = trust them more)")
    servo.add_argument("--kalman-dead-zone", type=float, default=0.0,
                       help="Minimum adjfine change (ppb) to actually apply. "
                            "Below this, hold previous value to reduce noise. "
                            "Suggested: 0.5 (below DO floor)")
    servo.add_argument("--kalman-sigma-freq", type=float, default=0.01,
                       help="DO frequency random walk (ppb/epoch). Lower = "
                            "more stable frequency estimate, less wander. "
                            "Default 0.01 from ADEV characterization.")
    servo.add_argument("--track-kp", type=float, default=0.3,
                       help="PI servo Kp gain (default: 0.3)")
    servo.add_argument("--track-ki", type=float, default=0.1,
                       help="PI servo Ki gain (default: 0.1)")
    servo.add_argument("--gain-ref-sigma", type=float, default=2.0,
                       help="Reference confidence for gain scale=1.0 (default: 2.0)")
    servo.add_argument("--gain-min-scale", type=float, default=0.1,
                       help="Minimum gain scale in tracking (default: 0.1)")
    servo.add_argument("--gain-max-scale", type=float, default=1.0,
                       help="Maximum gain scale in tracking before convergence boost (default: 1.0)")
    servo.add_argument("--converge-error-ns", type=float, default=500.0,
                       help="Boost gains above this tracking error magnitude (default: 500)")
    servo.add_argument("--converge-min-scale", type=float, default=2.0,
                       help="Minimum gain scale while converging (default: 2.0)")
    servo.add_argument("--discipline-interval", type=int, default=1,
                       help="Fixed discipline interval (default: 1)")
    servo.add_argument("--adaptive-interval", action="store_true",
                       help="Enable adaptive discipline interval")
    servo.add_argument("--max-interval", type=int, default=120,
                       help="Maximum discipline interval (default: 120)")
    servo.add_argument("--min-interval", type=int, default=1,
                       help="Minimum discipline interval (default: 1)")
    servo.add_argument("--scheduler-converge-threshold-ns", type=float, default=100.0,
                       help="Scheduler settled threshold in ns (default: 100)")
    servo.add_argument("--scheduler-settle-window", type=int, default=10,
                       help="Consecutive corrections required to declare settled (default: 10)")
    servo.add_argument("--scheduler-unconverge-factor", type=float, default=5.0,
                       help="Re-enter convergence when error exceeds threshold*f (default: 5.0)")
    servo.add_argument("--servo-log", default=None,
                       help="CSV log file for servo data")
    servo.add_argument("--track-max-ppb", type=float, default=None,
                       help="Clamp tracking corrections to this ppb magnitude")
    servo.add_argument("--track-outlier-ns", type=float, default=None,
                       help="Skip tracking updates above this error magnitude when settled")
    servo.add_argument("--track-restep-ns", type=float, default=None,
                       help="Re-enter step if |tracking error| exceeds this for 3 epochs")
    servo.add_argument("--phase-step-bias-ns", type=float, default=None,
                       help="Per-host bias compensation applied to PHC phase steps")
    servo.add_argument("--ticc-pullin-interval", type=int, default=5,
                       help="TICC pull-in correction interval when zero crossing is far away")
    servo.add_argument("--ticc-pullin-window-s", type=float, default=8.0,
                       help="Switch from pull-in to landing when predicted intercept is within this window")
    servo.add_argument("--ticc-landing-threshold-ns", type=float, default=1500.0,
                       help="Enter landing mode when |TICC error| falls below this")
    servo.add_argument("--ticc-settled-threshold-ns", type=float, default=100.0,
                       help="Declare settled when |TICC error| stays below this")
    servo.add_argument("--ticc-settled-deadband-ns", type=float, default=75.0,
                       help="Stop aggressive landing corrections once errors fall inside this band")
    servo.add_argument("--ticc-settled-interval", type=int, default=2,
                       help="TICC settled-mode correction interval")
    servo.add_argument("--ticc-settled-count", type=int, default=10,
                       help="Consecutive low-error TICC corrections required before settled mode")
    servo.add_argument("--ticc-landing-gain-floor", type=float, default=2.0,
                       help="Minimum gain scale while TICC tracking mode is landing")
    servo.add_argument("--ticc-landing-horizon-s", type=float, default=10.0,
                       help="In landing mode, enforce enough frequency to clear the current TICC error over this horizon")
    servo.add_argument("--obs-idle-timeout-s", type=float, default=None,
                       help="Log and enter safe holdover if no observation epochs arrive for this long")
    servo.add_argument("--queue-depth-threshold", type=int, default=5,
                       help="Warn when any pipeline queue exceeds this depth (epochs, default: 5)")
    servo.add_argument("--queue-depth-dump", action="store_true",
                       help="Dump full diagnostic state when queue depth threshold is breached")
    servo.add_argument("--carrier-max-sigma-ns", type=float, default=None,
                       help="Maximum PPP sigma allowed to compete as a servo source")
    servo.add_argument("--pmc", default=None, metavar="UDS_PATH",
                       help="ptp4l UDS path for clockClass management (e.g. /var/run/ptp4l)")
    servo.add_argument("--pmc-domain", type=int, default=0,
                       help="PTP domain number for pmc messages (must match ptp4l config, default: 0)")
    servo.add_argument("--pid-file", default=None,
                       help="Write engine PID here for external test control")
    servo.add_argument("--freerun", action="store_true",
                       help="Run full pipeline without steering PHC. "
                            "Logs what the servo would do but never calls adjfine. "
                            "For characterizing EXTTS precision and oscillator stability.")
    servo.add_argument("--freerun-max-error-ns", type=float, default=None,
                       help="Auto-stop freerun when |pps_err_extts_ns| exceeds this "
                            "(default: 100000 for OCXO, 500000 for TCXO)")
    servo.add_argument("--no-qerr", action="store_true",
                       help="Disable qErr correction (PPS-only discipline)")
    servo.add_argument("--pps-corr", choices=["timtp", "ppp"], default=None,
                       help="PPS correction source: 'timtp' (default, TIM-TP qErr) "
                            "or 'ppp' (smoothed dt_rx drift model). "
                            "qErr corrects PPS quantization (discrete, ±4 ns). "
                            "PPP correction models the rx TCXO drift from dt_rx "
                            "(continuous, ~0.1 ns) — different physics, not qErr.")
    servo.add_argument("--no-ppp", action="store_true",
                       help="Disable PPP carrier-phase correction "
                            "(PPS+qErr only, no PPS+PPP source)")
    servo.add_argument("--no-carrier", action="store_true",
                       help="Disable PPP Carrier Phase servo drive "
                            "(Carrier source disabled, PPS+PPP still available)")
    servo.add_argument("--do-char-file", default="data/do_characterization.json",
                       help="Path to DO characterization JSON (read at startup)")

    ticc = ap.add_argument_group("TICC experimental input (optional)")
    ticc.add_argument("--ticc-port", default=None,
                      help="TICC serial port for experimental measurement/servo input")
    ticc.add_argument("--ticc-log", default=None,
                      help="Optional raw TICC CSV log path for lab analysis. "
                           "Each row records host_monotonic when the line "
                           "arrived from the TICC over USB, plus ref_sec/"
                           "ref_ps/channel.  Pair with --qerr-log to do "
                           "post-hoc qErr correction by index-matching on "
                           "CLOCK_MONOTONIC.")
    ticc.add_argument("--qerr-log", default=None,
                      help="Optional raw qErr CSV log path.  Each row "
                           "captures one TIM-TP message from the F9T with "
                           "(host_timestamp, host_monotonic, qerr_for_extts_pps_ns, "
                           "tow_ms, qerr_invalid) — host_monotonic is "
                           "CLOCK_MONOTONIC at the moment the message was "
                           "parsed, the same clock the engine's "
                           "match_pps_mono uses internally.  Independent "
                           "of servo state; lets post-processing redo the "
                           "qErr ↔ TICC chB matching the engine does in "
                           "real time, without sawtooth dewrap heuristics.")
    ticc.add_argument("--ticc-baud", type=int, default=115200,
                      help="TICC baud rate (default: 115200)")
    ticc.add_argument("--ticc-phc-channel", choices=["chA", "chB"], default="chA",
                      help="TICC channel carrying disciplined PHC PPS OUT (default: chA)")
    ticc.add_argument("--ticc-ref-channel", choices=["chA", "chB"], default="chB",
                      help="TICC channel carrying raw reference PPS (default: chB)")
    ticc.add_argument("--ticc-max-age-s", type=float, default=2.0,
                      help="Maximum age for a paired TICC measurement to be used")
    ticc.add_argument("--ticc-target-ns", type=float, default=0.0,
                      help="Target chPHC-chREF offset in ns for TICC-driven servo mode")
    ticc.add_argument("--ticc-confidence-ns", type=float, default=3.0,
                      help="Assumed confidence of TICC differential error when driving servo")
    ticc.add_argument("--ticc-drive", action="store_true",
                      help="Use paired TICC differential measurement as a servo source")

    # NTRIP caster output (optional, future)
    caster = ap.add_argument_group("NTRIP caster output (optional)")
    caster.add_argument("--caster", default=None,
                        help="NTRIP caster listen address (e.g. :2102) [not yet implemented]")

    # Output
    out = ap.add_argument_group("Output")
    out.add_argument("--out", help="Solution CSV output file")
    out.add_argument("--duration", type=int, default=None,
                     help="Run duration in seconds (0 = unlimited)")
    out.add_argument("--gate-stats", help="Optional JSON output for strict sink gate statistics")
    out.add_argument("-v", "--verbose", action="store_true")

    args = ap.parse_args()
    apply_ptp_profile(args)
    # --ticc-port enables passive TICC measurement/logging.  --ticc-drive
    # additionally promotes TICC to the servo input.  Keep them separate
    # so a TICC can monitor a servo driven by other sources (PPS, PPP
    # Carrier Phase, etc.) for independent stability analysis.
    apply_ticc_drive_defaults(args)
    if args.pps_pin is None:
        args.pps_pin = 1
    if args.extts_channel is None:
        args.extts_channel = 0
    if args.phc_timescale is None:
        args.phc_timescale = "tai"
    if args.max_broadcast_age_s is None:
        # Generous so the sigma-inflation cascade has room to run.
        # Broadcast ephemeris is good for hours; the gate's job is a
        # last-resort safety, not graceful degradation.
        args.max_broadcast_age_s = 600.0
    if args.require_ssr is None:
        args.require_ssr = False
    if args.max_ssr_age_s is None:
        args.max_ssr_age_s = 30.0
    if args.min_correlation_confidence is None:
        args.min_correlation_confidence = 0.5
    if getattr(args, 'measurement_rate_ms', None) is None:
        _base = os.path.basename(args.serial)
        if _base.startswith("gnss") and _base[4:].isdigit():
            args.measurement_rate_ms = 2000  # kernel GNSS I2C: 0.5 Hz for lossless
        else:
            args.measurement_rate_ms = 1000
    if getattr(args, 'sfrbx_rate', None) is None:
        _base = os.path.basename(args.serial)
        if _base.startswith("gnss") and _base[4:].isdigit():
            args.sfrbx_rate = 0  # kernel GNSS I2C: disable SFRBX
        else:
            args.sfrbx_rate = 1
    if args.track_restep_ns is None:
        args.track_restep_ns = 100_000.0
    if args.phase_step_bias_ns is None:
        args.phase_step_bias_ns = 0.0
    if args.track_outlier_ns is None:
        args.track_outlier_ns = 500.0
    if args.obs_idle_timeout_s is None:
        args.obs_idle_timeout_s = 15.0
    if args.carrier_max_sigma_ns is None:
        args.carrier_max_sigma_ns = 50.0
    if args.min_broadcast_confidence is None:
        args.min_broadcast_confidence = 0.0
    if args.min_ssr_confidence is None:
        args.min_ssr_confidence = 0.0
    if args.freerun and args.freerun_max_error_ns is None:
        # Default auto-stop threshold: 100 µs for OCXO, 500 µs for TCXO.
        # Heuristic: if bootstrap adjfine is > 1000 ppb, likely a TCXO.
        args.freerun_max_error_ns = 500_000.0  # conservative default

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    if args.caster:
        log.warning(f"NTRIP caster output ({args.caster}) not yet implemented")

    sys.exit(run(args))


if __name__ == "__main__":
    main()
