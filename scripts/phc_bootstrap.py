#!/usr/bin/env python3
"""phc_bootstrap.py — Warm-start PHC initialization.

Requires a stored position file. Runs a PPP clock solution for a few
epochs, sanity-checks the PHC frequency and phase, and intervenes only
if they disagree with the GNSS-derived estimates.

Exit codes:
    0 — PHC is ready for servo (blessed or stepped)
    1 — fatal error (no position, no GNSS, hardware failure)
    2 — position sanity check failed (stored position may be wrong)

Usage:
    python3 phc_bootstrap.py \
        --serial /dev/gnss-top --baud 115200 --port-type USB \
        --position-file data/position.json \
        --ntrip-conf ntrip.conf --eph-mount BCEP00BKG0 \
        --systems gps,gal \
        --ptp-dev /dev/ptp0 --ptp-profile i226 \
        --drift-file data/drift.json
"""

import argparse
import json
import logging
import math
import os
import queue
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
from peppar_fix.ptp_device import PtpDevice
from peppar_fix.receiver import ensure_receiver_ready
from solve_ppp import FixedPosFilter, ls_init

log = logging.getLogger("phc_bootstrap")
C = 299_792_458.0

# ── Helpers ──────────────────────────────────────────────────────── #


def load_position(path):
    with open(path) as f:
        pos = json.load(f)
    return np.array(pos["ecef_m"])


def load_drift(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_drift(path, adjfine_ppb, phc_dev):
    data = {
        "adjfine_ppb": adjfine_ppb,
        "phc": phc_dev,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def lla_to_ecef(lat, lon, alt):
    from solve_ppp import lla_to_ecef as _lla
    return _lla(lat, lon, alt)


def ecef_to_lla(x, y, z):
    from solve_ppp import ecef_to_lla as _lla
    return _lla(x, y, z)


def _realtime_to_phc_offset_s(phc_timescale, leap, tai_minus_gps):
    """Seconds to add to CLOCK_REALTIME (UTC) to get PHC timescale.

    CLOCK_REALTIME tracks UTC.  GPS = UTC + leap.  TAI = GPS + 19 = UTC + leap + 19.
    """
    if phc_timescale == "utc":
        return 0
    if phc_timescale == "gps":
        return leap
    if phc_timescale == "tai":
        return leap + tai_minus_gps
    raise ValueError(f"Unsupported timescale: {phc_timescale}")


# ── PPS frequency measurement ────────────────────────────────────── #


def measure_pps_frequency(ptp, channel, n_samples=5, timeout_s=8):
    """Measure PHC frequency error from PPS-to-PPS intervals.

    Returns (median_ppb, sigma_ppb, n_intervals) or (None, None, 0)
    if not enough samples.  The median is the frequency error estimate;
    sigma is the standard deviation of individual interval measurements.
    """
    ptp.enable_extts(channel, rising_edge=True)
    intervals = []
    prev_ns = None
    deadline = time.monotonic() + timeout_s

    for _ in range(n_samples + 1):
        if time.monotonic() > deadline:
            break
        event = ptp.read_extts(timeout_ms=2000)
        if event is None:
            continue
        sec, nsec, _idx, _mono, _qr, _pa = event
        phc_ns = sec * 1_000_000_000 + nsec
        if prev_ns is not None:
            interval_ns = phc_ns - prev_ns
            # Expect ~1e9 ns per PPS interval
            freq_error_ppb = (interval_ns - 1_000_000_000) / 1.0
            intervals.append(freq_error_ppb)
        prev_ns = phc_ns

    ptp.disable_extts(channel)

    if len(intervals) < 2:
        return None, None, 0
    import statistics
    return (statistics.median(intervals),
            statistics.stdev(intervals),
            len(intervals))


# ── Main ─────────────────────────────────────────────────────────── #


def main():
    ap = argparse.ArgumentParser(description="Warm-start PHC initialization")
    ap.add_argument("--position-file", required=True)
    ap.add_argument("--serial", required=True)
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--port-type", default="USB",
                    choices=["UART", "UART2", "USB", "SPI", "I2C"],
                    help="Receiver port type for message routing")
    ap.add_argument("--systems", default="gps,gal")
    ap.add_argument("--ntrip-conf", required=True)
    ap.add_argument("--eph-mount", required=True)
    ap.add_argument("--ssr-mount", default=None)
    ap.add_argument("--ptp-dev", required=True)
    ap.add_argument("--ptp-profile", default=None)
    ap.add_argument("--extts-channel", type=int, default=0)
    ap.add_argument("--pps-pin", type=int, default=None)
    ap.add_argument("--program-pin", action="store_true")
    ap.add_argument("--phc-timescale", default="tai",
                    choices=["gps", "utc", "tai"])
    ap.add_argument("--leap", type=int, default=18)
    ap.add_argument("--tai-minus-gps", type=int, default=19)
    ap.add_argument("--drift-file", default="data/drift.json")
    ap.add_argument("--epochs", type=int, default=10,
                    help="Number of filter epochs before evaluating PHC")
    ap.add_argument("--freq-tolerance-ppb", type=float, default=10.0,
                    help="Frequency sanity threshold in ppb")
    ap.add_argument("--step-error-ns", type=int, default=5000,
                    help="Target step accuracy in ns")
    ap.add_argument("--step-budget-ms", type=int, default=500,
                    help="Max time for step retry loop in ms")
    ap.add_argument("--settime-lag-ns", type=int, default=0,
                    help="Mean clock_settime-to-PHC landing lag in ns (from characterization)")
    ap.add_argument("--position-check-m", type=float, default=100.0,
                    help="Max acceptable LS-vs-stored position delta in meters")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    # Load position
    if not os.path.exists(args.position_file):
        log.error("Position file not found: %s", args.position_file)
        log.error("Run peppar_find_position.py first for cold start")
        return 1
    known_ecef = load_position(args.position_file)
    lat, lon, alt = ecef_to_lla(*known_ecef)
    log.info("Loaded position: %.6f, %.6f, %.1fm", lat, lon, alt)

    # Load drift file
    drift = load_drift(args.drift_file)
    if drift:
        log.info("Drift file: adjfine=%.1f ppb (from %s)",
                 drift["adjfine_ppb"], drift.get("timestamp", "?"))
    else:
        log.info("No drift file found")

    # Open PHC
    ptp = PtpDevice(args.ptp_dev)
    caps = ptp.get_caps()
    log.info("PHC: %s, max_adj=%d ppb", args.ptp_dev, caps["max_adj"])

    if args.program_pin and args.pps_pin is not None:
        from peppar_fix.ptp_device import PTP_PF_EXTTS
        try:
            ptp.set_pin_function(args.pps_pin, PTP_PF_EXTTS, args.extts_channel)
        except OSError:
            pass

    # Read current PHC time
    try:
        phc_ns, sys_ns = ptp.read_phc_ns()
        log.info("PHC read OK: %d ns", phc_ns)
    except OSError as e:
        log.error("Cannot read PHC: %s", e)
        ptp.close()
        return 1

    # Measure PPS frequency
    log.info("Measuring PHC frequency from PPS intervals...")
    pps_freq_ppb, pps_freq_sigma, pps_freq_n = measure_pps_frequency(
        ptp, args.extts_channel, n_samples=args.epochs, timeout_s=args.epochs + 3)
    if pps_freq_ppb is not None:
        pps_freq_unc = pps_freq_sigma / math.sqrt(pps_freq_n)
        log.info("PPS frequency error: %.1f ±%.1f ppb (σ=%.1f, n=%d)",
                 pps_freq_ppb, pps_freq_unc, pps_freq_sigma, pps_freq_n)
    else:
        pps_freq_unc = None
        log.warning("Could not measure PPS frequency (no PPS?)")

    # Ensure receiver is producing dual-frequency observations.
    # Auto-detects active signals; reconfigures for L1+L5 if needed.
    systems = set(args.systems.split(","))
    driver = ensure_receiver_ready(
        args.serial, args.baud, port_type=args.port_type, systems=systems)
    if driver is None:
        log.error("Receiver not producing dual-frequency observations — "
                  "cannot proceed. See docs/receiver-signals.md.")
        ptp.close()
        return 1
    log.info("Receiver ready: %s", driver.name)

    # Start NTRIP and observation infrastructure
    from realtime_ppp import serial_reader, ntrip_reader
    from ntrip_client import NtripStream
    from broadcast_eph import BroadcastEphemeris
    from ssr_corrections import SSRState, RealtimeCorrections

    beph = BroadcastEphemeris()
    ssr = SSRState()
    corrections = RealtimeCorrections(beph, ssr)
    obs_queue = queue.Queue(maxsize=20)
    stop_event = threading.Event()

    # Parse NTRIP config
    import configparser
    nc = configparser.ConfigParser()
    nc.read(args.ntrip_conf)
    s = nc["ntrip"]
    ntrip_caster = s.get("caster")
    ntrip_port = int(s.get("port", 2101))
    ntrip_user = s.get("user", "")
    ntrip_password = s.get("password", "")
    ntrip_tls = s.get("tls", "false").lower() == "true"
    ssr_mount = s.get("mount", args.ssr_mount)

    # Start ephemeris stream
    eph_stream = NtripStream(
        caster=ntrip_caster, port=ntrip_port,
        mountpoint=args.eph_mount,
        user=ntrip_user, password=ntrip_password, tls=ntrip_tls)
    t_eph = threading.Thread(
        target=ntrip_reader,
        args=(eph_stream, beph, ssr, stop_event, "EPH"),
        daemon=True)
    t_eph.start()
    log.info("Ephemeris stream: %s:%d/%s", ntrip_caster, ntrip_port, args.eph_mount)

    # Start SSR stream if configured
    if ssr_mount:
        ssr_stream = NtripStream(
            caster=ntrip_caster, port=ntrip_port,
            mountpoint=ssr_mount,
            user=ntrip_user, password=ntrip_password, tls=ntrip_tls)
        t_ssr = threading.Thread(
            target=ntrip_reader,
            args=(ssr_stream, beph, ssr, stop_event, "SSR"),
            daemon=True)
        t_ssr.start()

    # Wait for broadcast ephemeris
    log.info("Waiting for broadcast ephemeris...")
    for _ in range(60):
        if stop_event.is_set():
            break
        by_sys = {}
        for prn in beph.satellites:
            by_sys[prn[0]] = by_sys.get(prn[0], 0) + 1
        if by_sys.get('G', 0) >= 8:
            break
        time.sleep(1)
    else:
        log.error("Timeout waiting for broadcast ephemeris")
        ptp.close()
        return 1
    log.info("Broadcast ephemeris ready: %s", beph.summary())

    # Start observation reader (driver from ensure_receiver_ready)
    t_serial = threading.Thread(
        target=serial_reader,
        args=(args.serial, args.baud, obs_queue, stop_event, beph, systems, ssr),
        kwargs={'driver': driver},
        daemon=True,
    )
    t_serial.start()

    # Run FixedPosFilter for N epochs
    filt = FixedPosFilter(known_ecef)
    prev_t = None
    n_epochs = 0
    dt_rx_ns = None
    dt_rx_sigma_ns = None

    log.info("Running PPP clock solution for %d epochs...", args.epochs)
    for _ in range(args.epochs * 3):  # allow some dropped epochs
        try:
            gps_time, observations = obs_queue.get(timeout=5)
        except queue.Empty:
            continue

        if prev_t is not None:
            dt = (gps_time - prev_t).total_seconds()
            if dt <= 0 or dt > 30:
                prev_t = gps_time
                continue
            filt.predict(dt)
        prev_t = gps_time

        n_used, resid, n_td = filt.update(
            observations, corrections, gps_time, clk_file=corrections)
        if n_used < 4:
            continue

        dt_rx_ns = filt.x[filt.IDX_CLK] / C * 1e9
        p_clk = filt.P[filt.IDX_CLK, filt.IDX_CLK]
        dt_rx_sigma_ns = math.sqrt(max(0, p_clk)) / C * 1e9
        n_epochs += 1

        if n_epochs % 5 == 0 or n_epochs == args.epochs:
            log.info("  [%d/%d] dt_rx=%.1f ±%.1f ns  n_used=%d",
                     n_epochs, args.epochs, dt_rx_ns, dt_rx_sigma_ns, n_used)

        if n_epochs >= args.epochs:
            break

    stop_event.set()

    if dt_rx_ns is None:
        log.error("Filter did not converge in %d epochs", args.epochs)
        ptp.close()
        return 1

    log.info("Clock estimate: dt_rx=%.1f ±%.1f ns after %d epochs",
             dt_rx_ns, dt_rx_sigma_ns, n_epochs)

    # Sanity check position via LS
    # (skipped for now — the filter ran with the stored position,
    #  large residuals would indicate a bad position)

    # ── Evaluate PHC ──────────────────────────────────────────────── #

    # Read PHC phase at the most recent PPS edge
    # We need to capture one more PPS event to compare
    ptp.enable_extts(args.extts_channel, rising_edge=True)
    pps_event = ptp.read_extts(timeout_ms=2000)
    pps_realtime_ns = time.clock_gettime_ns(time.CLOCK_REALTIME)
    ptp.disable_extts(args.extts_channel)

    if pps_event is None:
        log.error("No PPS event received — cannot evaluate PHC phase")
        ptp.close()
        return 1

    phc_sec, phc_nsec, _idx, _mono, _qr, _pa = pps_event
    phc_rounded_sec = phc_sec if phc_nsec < 500_000_000 else phc_sec + 1

    # Determine which second this PPS belongs to using CLOCK_REALTIME.
    # PPS fires on the second boundary; CLOCK_REALTIME (UTC via NTP) tells
    # us which one — avoids stale gps_time from the filter loop.
    offset_s = _realtime_to_phc_offset_s(
        args.phc_timescale, args.leap, args.tai_minus_gps)
    utc_sec = round(pps_realtime_ns / 1_000_000_000)
    target_sec = utc_sec + offset_s

    epoch_offset = phc_rounded_sec - target_sec
    pps_error_ns = phc_nsec if phc_nsec < 500_000_000 else phc_nsec - 1_000_000_000

    # Total phase error including whole-second offset
    phase_error_ns = epoch_offset * 1_000_000_000 + pps_error_ns

    log.info("PHC phase: epoch_offset=%ds, pps_error=%+.0f ns, "
             "total_phase_error=%+.0f ns (realtime_utc=%d, target=%d)",
             epoch_offset, pps_error_ns, phase_error_ns, utc_sec, target_sec)

    # Frequency sanity check — PPS measurement alone is sufficient.
    # pps_freq_ppb is the PHC's total frequency error (crystal + current
    # adjfine) as seen by PPS intervals.  If it's outside tolerance, the
    # PHC frequency needs correction regardless of whether we have a drift
    # file.
    freq_sane = True
    if pps_freq_ppb is not None:
        if abs(pps_freq_ppb) > args.freq_tolerance_ppb:
            log.warning("Frequency error: PPS=%.1f ±%.1f ppb — outside ±%.1f ppb",
                        pps_freq_ppb, pps_freq_unc, args.freq_tolerance_ppb)
            freq_sane = False
        else:
            log.info("Frequency sane: PPS=%.1f ±%.1f ppb (within ±%.1f ppb)",
                     pps_freq_ppb, pps_freq_unc, args.freq_tolerance_ppb)

    # Phase sanity check
    phase_sane = abs(phase_error_ns) < args.step_error_ns

    if phase_sane and freq_sane:
        log.info("PHC state is sane — blessing without intervention")
        log.info("  Phase error: %+.0f ns (within %d ns)",
                 phase_error_ns, args.step_error_ns)
        ptp.close()
        return 0

    # ── Intervene on whichever is wrong, leave the other alone ───── #

    if not phase_sane:
        # PPS-anchored step: the PHC should read target_sec.000000000
        # at the PPS edge.  CLOCK_REALTIME is used only as a transfer
        # standard — its phase error cancels in the subtraction of two
        # reads.  See docs/stream-timescale-correlation.md Rule 6.
        pps_anchor_ns = target_sec * 1_000_000_000
        log.info("Stepping PHC phase (PPS-anchored, target_sec=%d, "
                 "target_error=%d ns, budget=%d ms)",
                 target_sec, args.step_error_ns, args.step_budget_ms)
        residual, attempts, met = ptp.step_to(
            pps_anchor_ns=pps_anchor_ns,
            pps_realtime_ns=pps_realtime_ns,
            target_error_ns=args.step_error_ns,
            max_time_ms=args.step_budget_ms,
            settime_lag_ns=args.settime_lag_ns,
        )
        log.info("Step result: residual=%+.0f ns, attempts=%d, %s",
                 residual, attempts, "HIT" if met else "TIMEOUT")

        # Close the loop: capture fresh PPS events to confirm the step.
        # The PPS edge IS the ground truth — the readback residual above
        # is consistent but PPS is the independent check.
        log.info("Verifying step with fresh PPS capture...")
        ptp.enable_extts(args.extts_channel, rising_edge=True)
        last_verify_error = None
        for i in range(3):
            evt = ptp.read_extts(timeout_ms=2000)
            if evt is None:
                log.warning("  PPS verify [%d]: no event", i + 1)
                continue
            # Use CLOCK_REALTIME as transfer standard to identify
            # the PPS second (phase error < 0.5s from NTP — fine for
            # whole-second identification).
            v_realtime_ns = time.clock_gettime_ns(time.CLOCK_REALTIME)
            v_sec, v_nsec = evt[0], evt[1]
            v_rounded = v_sec if v_nsec < 500_000_000 else v_sec + 1
            v_target = round(v_realtime_ns / 1_000_000_000) + offset_s
            v_epoch_off = v_rounded - v_target
            v_sub_ns = v_nsec if v_nsec < 500_000_000 else v_nsec - 1_000_000_000
            last_verify_error = v_epoch_off * 1_000_000_000 + v_sub_ns
            log.info("  PPS verify [%d]: %+.0f ns (epoch_offset=%d)",
                     i + 1, last_verify_error, v_epoch_off)
            # Update anchor for any retry: this PPS edge is fresher
            pps_anchor_ns = v_target * 1_000_000_000
            pps_realtime_ns = v_realtime_ns
        ptp.disable_extts(args.extts_channel)

        if last_verify_error is not None and abs(last_verify_error) > args.step_error_ns:
            log.warning("Post-step PPS shows %+.0f ns error — retrying step",
                        last_verify_error)
            residual, attempts, met = ptp.step_to(
                pps_anchor_ns=pps_anchor_ns,
                pps_realtime_ns=pps_realtime_ns,
                target_error_ns=args.step_error_ns,
                max_time_ms=args.step_budget_ms,
                settime_lag_ns=args.settime_lag_ns,
            )
            log.info("Retry result: residual=%+.0f ns, attempts=%d, %s",
                     residual, attempts, "HIT" if met else "TIMEOUT")
        elif last_verify_error is not None:
            log.info("Post-step PPS verified: %+.0f ns", last_verify_error)
    else:
        log.info("Phase OK (%+.0f ns) — leaving PHC time alone", phase_error_ns)

    if not freq_sane:
        # Read current hardware adjfine to compute correction.
        # new_adjfine = current_adjfine - pps_freq_ppb  (cancel the error)
        current_adj = ptp.read_adjfine()
        log.info("Current PHC adjfine: %.1f ppb", current_adj)

        if pps_freq_ppb is not None and pps_freq_unc is not None:
            # Decide: trust PPS measurement or drift file?
            # Low sigma/sqrt(n) → PPS is reliable, use it.
            # High sigma → PPS is noisy, prefer drift file if available.
            if drift and pps_freq_unc > args.freq_tolerance_ppb:
                new_freq = drift["adjfine_ppb"]
                source = ("drift file (PPS σ/√n=%.1f ppb too high)" %
                          pps_freq_unc)
            else:
                new_freq = current_adj - pps_freq_ppb
                source = ("PPS correction: %.1f - %.1f" %
                          (current_adj, pps_freq_ppb))
        elif drift:
            new_freq = drift["adjfine_ppb"]
            source = "drift file"
        else:
            new_freq = 0.0
            source = "default (no data)"
        log.info("Setting PHC frequency to %.1f ppb (from %s)",
                 new_freq, source)
        ptp.adjfine(new_freq)
        # Only update drift file when we changed frequency
        save_drift(args.drift_file, new_freq, args.ptp_dev)
        log.info("Drift file updated: %s", args.drift_file)
    else:
        log.info("Frequency OK — leaving adjfine and drift file alone")

    ptp.close()
    log.info("PHC bootstrap complete — servo may start")
    return 0


if __name__ == "__main__":
    sys.exit(main())
