"""Timestamper abstraction — unified interface for PPS edge measurement.

A timestamper is anything that measures PPS edges: EXTTS channels on
PHCs, or standalone instruments like a TICC.  This module provides an
ABC and two concrete implementations so that bootstrap frequency
measurement, servo setup, and noise characterization can be written
once against the interface.

See docs/state-persistence-design.md § Timestamper for the data model.
"""

import abc
import logging
import math
import statistics
import time

log = logging.getLogger(__name__)


class Timestamper(abc.ABC):
    """Anything that measures PPS edges and can estimate DO frequency."""

    @abc.abstractmethod
    def measure_pps_frequency(self, n_samples=10, timeout_s=20):
        """Capture PPS edges and compute the DO's frequency offset.

        Returns (freq_ppb, sigma_ppb, n_intervals) on success, or
        (None, None, 0) if not enough edges arrive.

        freq_ppb: measured frequency offset of DO relative to GNSS PPS.
                  Positive = DO is fast.
        sigma_ppb: per-interval residual jitter (1-sigma).
        n_intervals: number of PPS intervals used.
        """


class ExttsTimestamper(Timestamper):
    """Measures PPS edges via a PHC's EXTTS hardware timestamping.

    Wraps PtpDevice.  The existing phc_bootstrap.measure_pps_frequency
    logic is moved here.
    """

    def __init__(self, ptp, channel):
        self.ptp = ptp
        self.channel = channel

    def measure_pps_frequency(self, n_samples=10, timeout_s=20):
        from peppar_fix.ptp_device import DualEdgeFilter

        ptp = self.ptp
        channel = self.channel

        ptp.enable_extts(channel, rising_edge=True)
        samples = []   # (elapsed_sec, nsec)
        first_sec = None
        deadline = time.monotonic() + timeout_s
        dedup = DualEdgeFilter()

        for _ in range(n_samples + 1):
            if time.monotonic() > deadline:
                break
            remaining_ms = max(50, int((deadline - time.monotonic()) * 1000))
            event = ptp.read_extts_dedup(dedup, timeout_ms=min(2000, remaining_ms))
            if event is None:
                continue
            sec, nsec, _idx, _mono, _qr, _pa = event
            if first_sec is None:
                first_sec = sec
            samples.append((sec - first_sec, nsec))

        ptp.disable_extts(channel)
        if dedup.dropped:
            log.info("EXTTS freq measurement: dual-edge filter dropped %d events",
                     dedup.dropped)

        if len(samples) < 2:
            return None, None, 0

        n_intervals = samples[-1][0]
        if n_intervals <= 0:
            return None, None, 0

        nsec_drift = samples[-1][1] - samples[0][1]
        if nsec_drift > 500_000_000:
            nsec_drift -= 1_000_000_000
            n_intervals += 1
        elif nsec_drift < -500_000_000:
            nsec_drift += 1_000_000_000
            n_intervals -= 1

        freq_ppb = nsec_drift / n_intervals

        if len(samples) >= 3:
            residuals = []
            for elapsed_s, nsec in samples[1:]:
                if elapsed_s <= 0:
                    continue
                predicted = samples[0][1] + freq_ppb * elapsed_s
                diff = nsec - predicted
                if diff > 500_000_000:
                    diff -= 1_000_000_000
                elif diff < -500_000_000:
                    diff += 1_000_000_000
                residuals.append(diff)
            sigma_ppb = statistics.stdev(residuals) if len(residuals) >= 2 else 0.0
        else:
            sigma_ppb = 0.0

        return freq_ppb, sigma_ppb, len(samples) - 1


class TiccTimestamper(Timestamper):
    """Measures PPS edges via a TICC time interval counter.

    Uses chA-chB differentials: the slope of diff_ns vs time is the
    DO frequency offset in ppb.  TICC resolution (~60 ps) is far
    better than EXTTS (~8 ns), so even 10 samples give an excellent
    frequency estimate.

    The TICC must have chA on the DO PPS and chB on the GNSS PPS
    (or vice versa — phc_channel / ref_channel configure this).
    """

    def __init__(self, ticc_port, ticc_baud=115200,
                 phc_channel='chA', ref_channel='chB'):
        self.ticc_port = ticc_port
        self.ticc_baud = ticc_baud
        self.phc_channel = phc_channel
        self.ref_channel = ref_channel

    def measure_pps_frequency(self, n_samples=10, timeout_s=30):
        """Measure DO frequency offset from TICC chA-chB differential slope.

        Collects paired (chA, chB) measurements at matching ref_sec,
        computes the differential in ns, and fits a linear slope.
        The slope (ns/s) is the frequency offset in ppb.

        timeout_s default is 30 to allow for TICC boot (~10s) plus
        measurement (~n_samples seconds).
        """
        from ticc import Ticc

        pairs = []  # (elapsed_sec, diff_ns)
        pending = {}  # ref_sec → {channel: (ref_sec, ref_ps)}
        first_ref_sec = None
        boot_discard = 2  # skip first 2 seconds (TICC boot artifacts)

        with Ticc(self.ticc_port, self.ticc_baud, wait_for_boot=True) as ticc:
            deadline = time.monotonic() + timeout_s
            for channel, ref_sec, ref_ps in ticc:
                if time.monotonic() > deadline:
                    break
                if channel not in (self.phc_channel, self.ref_channel):
                    continue
                if ref_sec <= boot_discard:
                    continue

                pending.setdefault(ref_sec, {})[channel] = (ref_sec, ref_ps)

                if (self.phc_channel in pending.get(ref_sec, {}) and
                        self.ref_channel in pending.get(ref_sec, {})):
                    phc = pending[ref_sec][self.phc_channel]
                    ref = pending[ref_sec][self.ref_channel]
                    del pending[ref_sec]

                    diff_ps = ((phc[0] - ref[0]) * 1_000_000_000_000
                               + phc[1] - ref[1])
                    diff_ns = diff_ps * 1e-3

                    if first_ref_sec is None:
                        first_ref_sec = ref_sec
                    elapsed = ref_sec - first_ref_sec
                    pairs.append((elapsed, diff_ns))

                    if len(pairs) >= n_samples:
                        break

                # Evict stale pending entries
                cutoff = ref_sec - 4
                stale = [k for k in pending if k < cutoff]
                for k in stale:
                    del pending[k]

        if len(pairs) < 3:
            log.warning("TICC freq measurement: only %d pairs (need ≥3)",
                        len(pairs))
            return None, None, 0

        # Linear regression: diff_ns = a + b * elapsed
        # b = frequency offset in ns/s = ppb
        n = len(pairs)
        sx = sum(t for t, _ in pairs)
        sy = sum(d for _, d in pairs)
        sxy = sum(t * d for t, d in pairs)
        sxx = sum(t * t for t, _ in pairs)
        denom = n * sxx - sx * sx
        if denom == 0:
            return None, None, 0

        slope = (n * sxy - sx * sy) / denom  # ppb
        intercept = (sy - slope * sx) / n

        # Residual jitter
        residuals = [d - (intercept + slope * t) for t, d in pairs]
        sigma = statistics.stdev(residuals) if len(residuals) >= 2 else 0.0

        n_intervals = pairs[-1][0] - pairs[0][0]
        if n_intervals <= 0:
            n_intervals = len(pairs) - 1

        log.info("TICC freq measurement: %.1f ±%.1f ppb (%d pairs, %ds baseline)",
                 slope, sigma / max(1, n_intervals), len(pairs), n_intervals)

        return slope, sigma, n_intervals
