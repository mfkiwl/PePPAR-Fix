#!/usr/bin/env python3
"""peppar-phc-servo: Discipline a PTP Hardware Clock using GNSS.

Thin CLI wrapper around the servo loop.  Assumes position is already known
(from --known-pos or --position-file) and receiver is already configured
(run peppar-rx-config first).

Uses competitive error source selection (M6) with adaptive discipline
interval (M7):
    PPS-only    (~20 ns)  -- always available
    PPS + qErr  (~3 ns)   -- when TIM-TP is available
    Carrier-phase (~0.1 ns) -- when PPP filter has converged

Exit codes:
    0 = clean shutdown (duration reached or SIGTERM)
    1 = error
    2 = position moved (watchdog alarm)
    3 = no PPS (no extts events received)
    4 = divergence (filter not converging)
"""

import argparse
import csv
import json
import logging
import math
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

import numpy as np

from solve_pseudorange import C, lla_to_ecef, ecef_to_lla
from solve_ppp import FixedPosFilter
from ntrip_client import NtripStream
from broadcast_eph import BroadcastEphemeris
from ssr_corrections import SSRState, RealtimeCorrections
from realtime_ppp import serial_reader, ntrip_reader, QErrStore
from peppar_fix import (
    PtpDevice, PIServo, ErrorSource, compute_error_sources,
    DisciplineScheduler, PositionWatchdog, save_position, load_position,
)
from peppar_fix.ptp_device import PTP_PF_EXTTS

log = logging.getLogger("peppar_phc_servo")

EXIT_CLEAN = 0
EXIT_ERROR = 1
EXIT_POSITION_MOVED = 2
EXIT_NO_PPS = 3
EXIT_DIVERGENCE = 4


# ── F9T timing mode switch ──────────────────────────────────────────────── #

def build_tmode_fixed_msg(ecef):
    """Build UBX CFG-VALSET to switch F9T to fixed-position timing mode."""
    try:
        from pyubx2 import UBXMessage, SET
    except ImportError:
        return None

    x_cm = int(ecef[0] * 100)
    y_cm = int(ecef[1] * 100)
    z_cm = int(ecef[2] * 100)
    x_hp = int(round((ecef[0] * 100 - x_cm) * 100))
    y_hp = int(round((ecef[1] * 100 - y_cm) * 100))
    z_hp = int(round((ecef[2] * 100 - z_cm) * 100))

    cfg_data = [
        ("CFG_TMODE_MODE", 2),
        ("CFG_TMODE_POS_TYPE", 0),
        ("CFG_TMODE_ECEF_X", x_cm),
        ("CFG_TMODE_ECEF_Y", y_cm),
        ("CFG_TMODE_ECEF_Z", z_cm),
        ("CFG_TMODE_ECEF_X_HP", x_hp),
        ("CFG_TMODE_ECEF_Y_HP", y_hp),
        ("CFG_TMODE_ECEF_Z_HP", z_hp),
        ("CFG_TMODE_FIXED_POS_ACC", 100),
    ]
    msg = UBXMessage.config_set(7, 0, cfg_data)
    return msg.serialize()


# ── PPS output configuration ────────────────────────────────────────────── #

def configure_pps_out(ptp, pin):
    """Configure a PTP SDP pin for 1PPS output (perout).

    Stub: The actual ioctl for PTP_PEROUT_REQUEST is hardware-specific.
    For Intel igc/E810, this enables disciplined PPS output on the given SDP.
    """
    log.info(f"PPS output on SDP{pin}: not yet implemented (stub)")
    # TODO: implement PTP_PEROUT_REQUEST ioctl


# ── PTP GM startup ──────────────────────────────────────────────────────── #

def start_ptp_gm(ptp_dev):
    """Start ptp4l as a PTP Grandmaster using the disciplined PHC.

    Stub: Would launch ptp4l with appropriate config.
    """
    log.info(f"PTP GM mode on {ptp_dev}: not yet implemented (stub)")
    # TODO: launch ptp4l -i <interface> -f gm.cfg


# ── Main servo loop ──────────────────────────────────────────────────────── #

def run_servo(args):
    """Main PHC discipline loop with competitive error source selection."""
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Handle signals for clean shutdown
    stop_event = threading.Event()

    def on_signal(signum, frame):
        log.info(f"Signal {signum} received, shutting down")
        stop_event.set()
    signal.signal(signal.SIGTERM, on_signal)

    # ── Resolve position ────────────────────────────────────────────────
    position_source = None
    known_ecef = None

    if args.known_pos:
        parts = args.known_pos.split(',')
        lat, lon, alt = float(parts[0]), float(parts[1]), float(parts[2])
        known_ecef = lla_to_ecef(lat, lon, alt)
        position_source = "cli"
        log.info(f"Position (CLI): {lat:.6f}, {lon:.6f}, {alt:.1f}m")
    elif args.position_file:
        loaded = load_position(args.position_file)
        if loaded is not None:
            known_ecef = loaded
            position_source = "file"
            lat, lon, alt = ecef_to_lla(known_ecef[0], known_ecef[1], known_ecef[2])
            log.info(f"Position (file): {lat:.6f}, {lon:.6f}, {alt:.1f}m")

    if known_ecef is None:
        log.error("No position available. Provide --known-pos or --position-file.")
        return EXIT_ERROR

    # ── Open PTP device ────────────────────────────────────────────────
    ptp = PtpDevice(args.ptp_dev)
    caps = ptp.get_caps()
    log.info(f"PHC: {args.ptp_dev}, max_adj={caps['max_adj']} ppb, "
             f"n_extts={caps['n_ext_ts']}, n_pins={caps['n_pins']}")

    ptp.adjfine(0.0)
    log.info("PHC adjfine reset to 0")

    # Configure extts for PPS input
    extts_channel = 0
    try:
        ptp.set_pin_function(args.extts_pin, PTP_PF_EXTTS, extts_channel)
    except OSError:
        log.info("Pin config not supported by driver (igc uses implicit mapping)")
    ptp.enable_extts(extts_channel, rising_edge=True)
    log.info(f"EXTTS enabled: pin={args.extts_pin}, channel={extts_channel}")

    # Configure PPS output if requested
    if args.pps_out is not None:
        configure_pps_out(ptp, args.pps_out)

    # ── Set up PPP infrastructure ──────────────────────────────────────
    beph = BroadcastEphemeris()
    ssr = SSRState()
    corrections = RealtimeCorrections(beph, ssr)
    obs_queue = queue.Queue(maxsize=100)
    qerr_store = QErrStore()

    # Read NTRIP config
    ntrip_kwargs = {}
    if args.ntrip_conf:
        import configparser
        cfg = configparser.ConfigParser()
        cfg.read(args.ntrip_conf)
        ntrip_kwargs = {
            'caster': cfg.get('ntrip', 'caster', fallback='products.igs-ip.net'),
            'port': cfg.getint('ntrip', 'port', fallback=2101),
            'user': cfg.get('ntrip', 'user', fallback=''),
            'password': cfg.get('ntrip', 'password', fallback=''),
            'tls': cfg.getboolean('ntrip', 'tls', fallback=False),
        }
    else:
        ntrip_kwargs = {
            'caster': args.caster,
            'port': args.port,
            'user': args.user or '',
            'password': args.password or '',
            'tls': args.tls,
        }

    # Start NTRIP threads
    threads = []
    eph_stream = NtripStream(
        caster=ntrip_kwargs['caster'], port=ntrip_kwargs['port'],
        mountpoint=args.eph_mount,
        user=ntrip_kwargs['user'], password=ntrip_kwargs['password'],
        tls=ntrip_kwargs['tls'],
    )
    t_eph = threading.Thread(
        target=ntrip_reader,
        args=(eph_stream, beph, ssr, stop_event, "EPH"),
        daemon=True,
    )
    t_eph.start()
    threads.append(t_eph)
    log.info(f"NTRIP ephemeris: {args.eph_mount}")

    if args.ssr_mount:
        ssr_stream = NtripStream(
            caster=ntrip_kwargs['caster'], port=ntrip_kwargs['port'],
            mountpoint=args.ssr_mount,
            user=ntrip_kwargs['user'], password=ntrip_kwargs['password'],
            tls=ntrip_kwargs['tls'],
        )
        t_ssr = threading.Thread(
            target=ntrip_reader,
            args=(ssr_stream, beph, ssr, stop_event, "SSR"),
            daemon=True,
        )
        t_ssr.start()
        threads.append(t_ssr)
        log.info(f"NTRIP SSR: {args.ssr_mount}")

    # Wait for ephemeris warmup
    log.info("Waiting for broadcast ephemeris...")
    while beph.n_satellites < 8 and not stop_event.is_set():
        time.sleep(2)
        log.info(f"  Warmup: {beph.summary()}")
    log.info(f"Warmup complete: {beph.summary()}")

    # Parse systems filter
    systems = set(args.systems.split(',')) if args.systems else None

    # Config queue for sending UBX to receiver
    config_queue = queue.Queue(maxsize=10)

    # Start serial reader
    t_serial = threading.Thread(
        target=serial_reader,
        args=(args.serial, args.baud, obs_queue, stop_event, beph, systems, ssr),
        kwargs={'qerr_store': qerr_store, 'config_queue': config_queue},
        daemon=True,
    )
    t_serial.start()
    log.info(f"Serial: {args.serial} at {args.baud} baud")

    # ── Receiver signal diagnostic ──────────────────────────────────────
    log.info("Checking receiver signals (3 epochs)...")
    sys_counts = {}
    for _diag_i in range(3):
        try:
            _t, _obs = obs_queue.get(timeout=10)
        except queue.Empty:
            log.warning("  No observations -- check serial and receiver config")
            break
        for o in _obs:
            s = o.get('sys', '?')
            sys_counts[s] = sys_counts.get(s, 0) + 1
        obs_queue.put((_t, _obs))

    if sys_counts:
        parts = [f"{s.upper()}={n//3}" for s, n in sorted(sys_counts.items())]
        log.info(f"  Dual-freq SVs per epoch: {', '.join(parts)}")

    # Initialize PPP filter
    filt = FixedPosFilter(known_ecef)
    filt.prev_clock = 0.0

    # Servo parameters
    STEP_THRESHOLD_NS = 10_000
    BASE_KP = args.track_kp
    BASE_KI = args.track_ki
    GAIN_REF_SIGMA = args.gain_ref_sigma
    GAIN_MIN_SCALE = 0.1
    GAIN_MAX_SCALE = 3.0
    CONVERGE_ERROR_NS = 500
    CONVERGE_MIN_SCALE = 2.0

    servo = PIServo(BASE_KP, BASE_KI, max_ppb=caps['max_adj'])
    scheduler = DisciplineScheduler(
        base_interval=args.discipline_interval,
        adaptive=args.adaptive_interval,
        min_interval=args.min_interval,
        max_interval=args.max_interval,
    )
    watchdog = PositionWatchdog(threshold_m=args.watchdog_threshold)

    phase = 'warmup'
    prev_t = None
    n_epochs = 0
    warmup_epochs = args.warmup
    prev_source = None
    position_saved = False
    tmode_set = False
    no_pps_count = 0
    exit_code = EXIT_CLEAN

    # PPS event queue
    pps_queue = queue.Queue(maxsize=10)

    def extts_reader():
        while not stop_event.is_set():
            event = ptp.read_extts(timeout_ms=1500)
            if event is None:
                continue
            phc_sec, phc_nsec, _idx = event
            try:
                pps_queue.put_nowait((phc_sec, phc_nsec))
            except queue.Full:
                while not pps_queue.empty():
                    try:
                        pps_queue.get_nowait()
                    except queue.Empty:
                        break
                pps_queue.put_nowait((phc_sec, phc_nsec))

    t_extts = threading.Thread(target=extts_reader, daemon=True)
    t_extts.start()
    log.info("EXTTS reader started")

    def pps_fractional_error(phc_sec, phc_nsec):
        if phc_nsec <= 500_000_000:
            return float(phc_nsec)
        else:
            return float(phc_nsec) - 1_000_000_000

    def phc_gps_offset_s(phc_sec, phc_nsec, gps_unix_sec):
        phc_rounded = phc_sec if phc_nsec < 500_000_000 else phc_sec + 1
        return phc_rounded - gps_unix_sec

    # Open log file
    log_f = None
    log_w = None
    if args.log:
        log_f = open(args.log, 'w', newline='')
        log_w = csv.writer(log_f)
        log_w.writerow([
            'timestamp', 'gps_second', 'phc_sec', 'phc_nsec',
            'dt_rx_ns', 'dt_rx_sigma_ns', 'pps_error_ns', 'qerr_ns',
            'source', 'source_error_ns', 'source_confidence_ns',
            'adjfine_ppb', 'phase', 'n_meas', 'gain_scale',
            'discipline_interval', 'n_accumulated', 'watchdog_alarm',
            'isb_gal_ns', 'isb_bds_ns',
        ])

    start_time = time.time()
    adjfine_ppb = 0.0
    gain_scale = 1.0

    try:
        while not stop_event.is_set():
            if args.duration and (time.time() - start_time) > args.duration:
                log.info(f"Duration limit reached ({args.duration}s)")
                break

            try:
                gps_time, observations = obs_queue.get(timeout=5)
            except queue.Empty:
                continue

            # EKF predict + update
            if prev_t is not None:
                dt = (gps_time - prev_t).total_seconds()
                if dt <= 0 or dt > 30:
                    log.warning(f"Suspicious dt={dt:.1f}s, skipping")
                    prev_t = gps_time
                    continue
                filt.predict(dt)
            prev_t = gps_time

            n_used, resid, n_td = filt.update(
                observations, corrections, gps_time,
                clk_file=corrections,
            )

            if n_used < 4:
                continue

            resid_rms = float(np.sqrt(np.mean(resid ** 2))) if len(resid) > 0 else 0.0
            watchdog.update(resid_rms, n_used)
            if watchdog.alarmed:
                log.error("POSITION WATCHDOG ALARM: antenna position has changed! "
                          "Servo steering DISABLED.")
                exit_code = EXIT_POSITION_MOVED
                break

            dt_rx_ns = filt.x[filt.IDX_CLK] / C * 1e9
            p_clk = filt.P[filt.IDX_CLK, filt.IDX_CLK]
            dt_rx_sigma = math.sqrt(max(0, p_clk)) / C * 1e9
            n_epochs += 1

            # ISBs for logging
            isb_ns = {}
            if hasattr(filt, 'IDX_ISB_GAL') and filt.x.shape[0] > filt.IDX_ISB_GAL:
                isb_ns['gal'] = filt.x[filt.IDX_ISB_GAL] / C * 1e9
            if hasattr(filt, 'IDX_ISB_BDS') and filt.x.shape[0] > getattr(filt, 'IDX_ISB_BDS', 999):
                isb_ns['bds'] = filt.x[filt.IDX_ISB_BDS] / C * 1e9

            # Position save and F9T timing mode switch
            if n_epochs >= 300 and dt_rx_sigma < 100.0:
                sigma_m = dt_rx_sigma * 1e-9 * C

                if args.position_file and not position_saved and sigma_m < 0.1:
                    save_position(
                        args.position_file, known_ecef,
                        sigma_m=sigma_m,
                        source="ppp_bootstrap" if position_source == "file" else "known_pos",
                        note=f"saved after {n_epochs} epochs, dt_rx_sigma={dt_rx_sigma:.2f}ns",
                    )
                    position_saved = True
                    log.info(f"Position saved to {args.position_file}")

                if not tmode_set and sigma_m < 0.1:
                    tmode_msg = build_tmode_fixed_msg(known_ecef)
                    if tmode_msg is not None:
                        config_queue.put(tmode_msg)
                        tmode_set = True
                        lat, lon, alt = ecef_to_lla(
                            known_ecef[0], known_ecef[1], known_ecef[2])
                        log.info(f"F9T -> fixed-position timing mode "
                                 f"({lat:.6f}, {lon:.6f}, {alt:.1f}m)")

            # Get PPS event
            try:
                phc_sec, phc_nsec = pps_queue.get(timeout=0.5)
                no_pps_count = 0
            except queue.Empty:
                no_pps_count += 1
                if no_pps_count >= 60:
                    log.error("No PPS events for 60 consecutive epochs")
                    exit_code = EXIT_NO_PPS
                    break
                if n_epochs % 10 == 0:
                    log.info(f"  [{n_epochs}] No PPS event for this epoch")
                continue

            gps_unix_sec = int(round(gps_time.timestamp()))
            ts_str = gps_time.strftime('%Y-%m-%d %H:%M:%S')
            pps_error_ns = pps_fractional_error(phc_sec, phc_nsec)

            qerr_ns, _ = qerr_store.get()

            sources = compute_error_sources(
                pps_error_ns, qerr_ns, dt_rx_ns, dt_rx_sigma,
            )
            best = sources[0]

            # ── Warmup ──────────────────────────────────────────────────
            if phase == 'warmup':
                if n_epochs >= warmup_epochs:
                    epoch_offset = phc_gps_offset_s(phc_sec, phc_nsec, gps_unix_sec)
                    log.info(f"  Warmup complete ({n_epochs} epochs, "
                             f"best={best}, epoch_offset={epoch_offset}s)")
                    if epoch_offset != 0 or abs(best.error_ns) > STEP_THRESHOLD_NS:
                        phase = 'step'
                    else:
                        phase = 'tracking'
                        log.info(f"  -> tracking (no step needed)")
                elif n_epochs % 10 == 0:
                    log.info(f"  [{n_epochs}] warmup: best={best} "
                             f"dt_rx={dt_rx_ns:+.1f}+/-{dt_rx_sigma:.1f}ns")
                if log_w:
                    log_w.writerow([
                        ts_str, gps_unix_sec, phc_sec, phc_nsec,
                        f'{dt_rx_ns:.3f}', f'{dt_rx_sigma:.3f}',
                        f'{pps_error_ns:.1f}', f'{qerr_ns:.3f}' if qerr_ns is not None else '',
                        best.name, f'{best.error_ns:.3f}', f'{best.confidence_ns:.3f}',
                        f'{adjfine_ppb:.3f}', phase, n_used, f'{gain_scale:.3f}',
                        scheduler.interval, 0, int(watchdog.alarmed),
                        f'{isb_ns.get("gal", 0):.3f}', f'{isb_ns.get("bds", 0):.3f}',
                    ])
                continue

            # ── Step ────────────────────────────────────────────────────
            if phase == 'step':
                epoch_offset = phc_gps_offset_s(phc_sec, phc_nsec, gps_unix_sec)
                total_offset_ns = epoch_offset * 1_000_000_000 + best.error_ns
                log.info(f"  STEP: epoch_offset={epoch_offset}s, "
                         f"source={best}, total={total_offset_ns:+.0f}ns")

                adj_s = -total_offset_ns / 1_000_000_000
                result = subprocess.run(
                    ['/usr/sbin/phc_ctl', args.ptp_dev, '--',
                     'adj', f'{adj_s:.9f}'],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    log.info(f"  phc_ctl adj {adj_s:.6f}s: {result.stdout.strip()}")
                else:
                    log.error(f"  phc_ctl adj failed (rc={result.returncode}): "
                              f"{result.stderr.strip()} {result.stdout.strip()}")

                servo = PIServo(BASE_KP, BASE_KI, max_ppb=caps['max_adj'])
                scheduler = DisciplineScheduler(
                    base_interval=args.discipline_interval,
                    adaptive=args.adaptive_interval,
                    min_interval=args.min_interval,
                    max_interval=args.max_interval,
                )
                watchdog = PositionWatchdog(threshold_m=args.watchdog_threshold)
                phase = 'tracking'
                time.sleep(2)
                while not pps_queue.empty():
                    try:
                        pps_queue.get_nowait()
                    except queue.Empty:
                        break
                continue

            # ── Tracking ────────────────────────────────────────────────
            if abs(best.error_ns) > 5000 and not scheduler._converging:
                log.warning(f"  Outlier: {best}, skipping")
                continue

            scheduler.accumulate(best.error_ns, best.confidence_ns, best.name)

            if prev_source != best.name:
                if prev_source is not None:
                    log.info(f"  Source: {prev_source} -> {best.name} "
                             f"(confidence {best.confidence_ns:.1f}ns)")
                prev_source = best.name

            if scheduler.should_correct():
                avg_error, avg_confidence, n_samples = scheduler.flush()

                gain_scale = max(GAIN_MIN_SCALE, min(GAIN_MAX_SCALE,
                                 GAIN_REF_SIGMA / avg_confidence))
                if abs(avg_error) > CONVERGE_ERROR_NS:
                    gain_scale = max(gain_scale, CONVERGE_MIN_SCALE)

                servo.kp = BASE_KP * gain_scale
                servo.ki = BASE_KI * gain_scale
                adjfine_ppb = -servo.update(avg_error, dt=float(n_samples))
                ptp.adjfine(adjfine_ppb)

                scheduler.update_drift_rate(time.monotonic(), adjfine_ppb)
                scheduler.compute_adaptive_interval(avg_confidence)

                if n_epochs % 10 == 0:
                    log.info(f"  [{n_epochs}] {best.name}: "
                             f"err={avg_error:+.1f}ns (avg {n_samples}) "
                             f"adj={adjfine_ppb:+.1f}ppb "
                             f"gain={gain_scale:.2f}x "
                             f"interval={scheduler.interval}")
            else:
                n_samples = 0
                if n_epochs % 10 == 0:
                    log.info(f"  [{n_epochs}] {best.name}: "
                             f"err={best.error_ns:+.1f}ns "
                             f"coast ({scheduler.n_accumulated}/{scheduler.interval}) "
                             f"adj={adjfine_ppb:+.1f}ppb")

            if log_w:
                log_w.writerow([
                    ts_str, gps_unix_sec, phc_sec, phc_nsec,
                    f'{dt_rx_ns:.3f}', f'{dt_rx_sigma:.3f}',
                    f'{pps_error_ns:.1f}', f'{qerr_ns:.3f}' if qerr_ns is not None else '',
                    best.name, f'{best.error_ns:.3f}', f'{best.confidence_ns:.3f}',
                    f'{adjfine_ppb:.3f}', phase, n_used, f'{gain_scale:.3f}',
                    scheduler.interval, scheduler.n_accumulated,
                    int(watchdog.alarmed),
                    f'{isb_ns.get("gal", 0):.3f}', f'{isb_ns.get("bds", 0):.3f}',
                ])

    except KeyboardInterrupt:
        log.info("Interrupted")
    finally:
        stop_event.set()
        try:
            ptp.adjfine(0.0)
        except Exception:
            pass
        ptp.disable_extts(extts_channel)
        ptp.close()
        if log_f:
            log_f.close()

    # Start PTP GM if requested and servo ran successfully
    if args.ptp_gm and exit_code == EXIT_CLEAN:
        start_ptp_gm(args.ptp_dev)

    elapsed = time.time() - start_time
    log.info(f"{'='*60}")
    log.info(f"  peppar-phc-servo complete")
    log.info(f"  Duration: {elapsed:.0f}s, Epochs: {n_epochs}")
    log.info(f"  Last source: {prev_source}, adjfine: {adjfine_ppb:+.3f} ppb")
    log.info(f"  Exit code: {exit_code}")
    log.info(f"{'='*60}")

    return exit_code


# ── CLI ──────────────────────────────────────────────────────────────────── #

def main():
    ap = argparse.ArgumentParser(
        description="Discipline a PTP Hardware Clock using GNSS (peppar-phc-servo)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes:
  0  Clean shutdown (duration reached or SIGTERM)
  1  Error
  2  Position moved (watchdog alarm -- re-run peppar-find-position)
  3  No PPS (no extts events -- check PPS cable and receiver)
  4  Divergence (filter not converging)
""",
    )

    # Position
    pos = ap.add_argument_group("Position")
    pos.add_argument("--known-pos", default=None,
                     help="Known position as lat,lon,alt (overrides position file)")
    pos.add_argument("--position-file", default=None,
                     help="JSON file for position save/load")
    pos.add_argument("--watchdog-threshold", type=float, default=0.5,
                     help="Position watchdog threshold in meters (default: 0.5)")
    pos.add_argument("--systems", default="gps,gal,bds",
                     help="GNSS systems to use (default: gps,gal,bds)")

    # Serial
    serial = ap.add_argument_group("Serial")
    serial.add_argument("--serial", required=True,
                        help="F9T serial port (e.g. /dev/gnss-top)")
    serial.add_argument("--baud", type=int, default=9600,
                        help="Serial baud rate (default: 9600)")

    # NTRIP
    ntrip = ap.add_argument_group("NTRIP corrections")
    ntrip.add_argument("--ntrip-conf", help="NTRIP config file (INI format)")
    ntrip.add_argument("--caster", default="products.igs-ip.net")
    ntrip.add_argument("--port", type=int, default=2101)
    ntrip.add_argument("--user", default=None)
    ntrip.add_argument("--password", default=None)
    ntrip.add_argument("--tls", action="store_true")
    ntrip.add_argument("--eph-mount", required=True,
                        help="Broadcast ephemeris mountpoint")
    ntrip.add_argument("--ssr-mount", default=None,
                        help="SSR corrections mountpoint (optional)")

    # PTP hardware
    ptp = ap.add_argument_group("PTP hardware")
    ptp.add_argument("--ptp-dev", default="/dev/ptp0",
                     help="PTP device (default: /dev/ptp0)")
    ptp.add_argument("--extts-pin", type=int, default=1,
                     help="SDP pin for PPS input (default: 1 = SDP1)")
    ptp.add_argument("--pps-out", type=int, default=None,
                     help="SDP pin for disciplined PPS output (optional)")
    ptp.add_argument("--ptp-gm", action="store_true",
                     help="Start ptp4l as PTP Grandmaster after servo locks")

    # Servo tuning
    tune = ap.add_argument_group("Servo tuning")
    tune.add_argument("--warmup", type=int, default=20,
                      help="Warmup epochs before steering (default: 20)")
    tune.add_argument("--track-kp", type=float, default=0.3,
                      help="Tracking mode Kp gain (default: 0.3)")
    tune.add_argument("--track-ki", type=float, default=0.1,
                      help="Tracking mode Ki gain (default: 0.1)")
    tune.add_argument("--gain-ref-sigma", type=float, default=2.0,
                      help="Reference confidence for gain scale=1.0 (default: 2.0)")
    tune.add_argument("--discipline-interval", type=int, default=1,
                      help="Fixed discipline interval in epochs (default: 1)")
    tune.add_argument("--adaptive-interval", action="store_true",
                      help="Enable adaptive discipline interval")
    tune.add_argument("--max-interval", type=int, default=120,
                      help="Maximum discipline interval (default: 120)")
    tune.add_argument("--min-interval", type=int, default=1,
                      help="Minimum discipline interval (default: 1)")

    # Output
    out = ap.add_argument_group("Output")
    out.add_argument("--log", default=None, help="CSV log file")
    out.add_argument("--duration", type=int, default=None,
                     help="Run duration in seconds")
    out.add_argument("-v", "--verbose", action="store_true")

    args = ap.parse_args()
    sys.exit(run_servo(args))


if __name__ == "__main__":
    main()
