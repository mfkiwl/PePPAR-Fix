#!/usr/bin/env python3
"""
phc_servo.py — PePPAR Fix software GPSDO.

Disciplines a PTP Hardware Clock using progressively better
corrections to the GNSS PPS signal:
  PPS → PPS+qErr → PPS+PPP (→ PPS+PPP-AR, future)

Each correction layer improves absolute UTC phase accuracy.
The servo selects the best available correction at each epoch.

Requires phc_bootstrap.py to have run first — the bootstrap ensures
the PHC starts within ±10µs phase and ±10ppb frequency.  The servo
has no warmup or step logic: PI tracking begins from epoch 1.

Adaptive discipline interval: instead of calling adjfine every
second (which injects ~7.5 ppb of correction jitter), the servo
accumulates error samples over N epochs and applies one averaged
correction.  This reduces TDEV at short tau while preserving tracking
bandwidth.  Use --discipline-interval N for fixed interval or
--adaptive-interval to let the scheduler choose based on drift rate
vs measurement noise.

Architecture:
    F9T PPS → SDP1 → extts event (PHC timestamp of PPS edge)
    F9T TIM-TP → qErr (PPS quantization error, ~3 ns precision)
    PPP filter → dt_rx (receiver clock offset from GPS time)

    Error sources (compete by confidence):
      1. PPS-only:    error = pps_frac(phc)           ±20 ns
      2. PPS + qErr:  error = pps_frac(phc) + qErr    ±3 ns
      3. Carrier-phase: error = pps_frac(phc) + dt_rx  ±0.1 ns

    PI servo → adjfine() on /dev/ptp0, gains scaled by confidence
    DisciplineScheduler → accumulates samples, decides when to correct

    Output: SDP0 → disciplined PPS (SMA J4 → TICC chA for measurement)

The servo reads PPS timestamps via the PTP_EXTTS_EVENT ioctl and
correlates them with PPP clock estimates at the same GPS second.
A PI controller drives adjfine to minimize the PHC-GPS offset.

GNSS receiver requirements:
    The PPP filter uses ionosphere-free (IF) combination of dual-frequency
    observations. This REQUIRES two frequencies per satellite:
        GPS:     L1 C/A + L5Q
        Galileo: E1C + E5aQ
        BDS:     B1I + B2aI

    Single-frequency satellites are silently dropped. The filter needs
    at least 4 dual-frequency SVs per epoch from any combination of
    constellations.

    L1-only operation is NOT supported — the ionosphere-free combination
    is fundamental to the PPP approach. Without it, ionospheric delay
    (up to ~50 ns at zenith, worse at low elevation) would dominate the
    clock estimate.

    GPS L5 availability: only GPS Block IIF/III satellites transmit L5
    (~15 of 32 SVs). The signal is flagged "unhealthy" in the nav message;
    the receiver needs the L5 health override (u-blox App Note UBX-21038688,
    key 0x10320001). IMPORTANT: the override is saved to flash but does NOT
    take effect until the receiver is warm-restarted. configure_f9t.py
    handles this automatically. Without the restart, GPS delivers only L1
    (single-freq, dropped by filter) even though the config ACK'd correctly.

    With GPS L5 enabled: ~8 GPS + ~7 Galileo = ~15 dual-freq SVs per epoch.
    Without GPS L5: ~7 Galileo only — still sufficient for the filter.

    Run configure_f9t.py to set up the receiver:
        python scripts/configure_f9t.py /dev/gnss-top --port-type USB

    Correction stream requirements:
        - Broadcast ephemeris (RTCM 1019/1042/1045/1046) — required
        - SSR orbit + clock corrections — recommended for sub-meter accuracy
        - SSR code biases — applied when available (improves convergence)

Usage:
    python phc_servo.py --serial /dev/gnss-top --baud 9600 \\
        --known-pos '41.8430626,-88.1037190,201.671' \\
        --ntrip-conf ntrip.conf --eph-mount BCEP00BKG0 --ssr-mount SSRA00BKG0 \\
        --systems gps,gal --duration 3600 \\
        --ptp-dev /dev/ptp0 --extts-pin 1 \\
        --log servo_log.csv

    # Without NTRIP (broadcast ephemeris only, ~25m RMS floor):
    python phc_servo.py --serial /dev/gnss-top --baud 9600 \\
        --known-pos '41.8430626,-88.1037190,201.671' \\
        --caster products.igs-ip.net --port 2101 \\
        --eph-mount BCEP00BKG0 \\
        --systems gps,gal --duration 3600 \\
        --ptp-dev /dev/ptp0 --extts-pin 1
"""

import argparse
import csv
import json
import logging
import math
import os
import queue
import sys
import threading
import time
import tomllib
from collections import deque
from datetime import datetime, timezone, timedelta

import numpy as np

# Local imports (same scripts/ directory)
from solve_pseudorange import C, lla_to_ecef, ecef_to_lla
from solve_ppp import FixedPosFilter
from ntrip_client import NtripStream
from broadcast_eph import BroadcastEphemeris
from ssr_corrections import SSRState, RealtimeCorrections
from realtime_ppp import serial_reader, ntrip_reader, QErrStore
from peppar_fix import (
    CorrectionFreshnessGate, PtpDevice, PIServo, ErrorSource, compute_error_sources,
    DisciplineScheduler, PositionWatchdog, StrictCorrelationGate,
    estimate_correlation_confidence, match_pps_event_from_history,
    save_position, load_position,
)
from peppar_fix.event_time import PpsEvent
from ntrip_caster import NtripCasterServer, rawx_to_caster_obs

log = logging.getLogger("phc_servo")


def apply_ptp_profile(args):
    """Apply PTP defaults from config/receivers.toml when requested."""
    if not args.ptp_profile:
        return
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

    if args.ptp_dev is None:
        args.ptp_dev = profile.get("device", args.ptp_dev)
    if args.extts_pin is None:
        args.extts_pin = profile.get("pps_pin", args.extts_pin)
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
    if args.min_broadcast_confidence is None:
        args.min_broadcast_confidence = profile.get(
            "min_broadcast_confidence", args.min_broadcast_confidence
        )
    if args.min_ssr_confidence is None:
        args.min_ssr_confidence = profile.get(
            "min_ssr_confidence", args.min_ssr_confidence
        )



# ── Timing mode switch (delegates to receiver driver) ──────────────────── #

def build_tmode_fixed_msg(ecef, driver=None):
    """Build UBX CFG-VALSET bytes to switch to fixed-position timing mode.

    Delegates to the receiver driver's implementation. Only timing-grade
    receivers (e.g. ZED-F9T) support this; navigation receivers (e.g.
    NEO-F10T) return None.

    Args:
        ecef: numpy array [x, y, z] in meters (ECEF)
        driver: ReceiverDriver instance. Defaults to F9TDriver.

    Returns:
        bytes ready to write to the serial port, or None if unsupported.
    """
    if driver is None:
        from peppar_fix.receiver import F9TDriver
        driver = F9TDriver()
    return driver.build_tmode_fixed_msg(ecef)


def get_latest_pps_event(pps_queue, timeout=0.5):
    """Return the newest queued PPS event, discarding older stale ones."""
    event = pps_queue.get(timeout=timeout)
    dropped = 0
    while True:
        try:
            event = pps_queue.get_nowait()
            dropped += 1
        except queue.Empty:
            return event, dropped


def queue_put_drop_oldest(qobj, item):
    """Enqueue one item, dropping at most one oldest entry if full."""
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


def append_queue_history(history, qobj, timeout=5):
    """Append one blocking item plus any immediately queued items."""
    item = qobj.get(timeout=timeout)
    history.append(item)
    while True:
        try:
            history.append(qobj.get_nowait())
        except queue.Empty:
            return len(history)


# ── Main servo loop ──────────────────────────────────────────────────────── #

def run_servo(args):
    """Main PHC discipline loop with competitive error source selection (M6).

    Three error sources compete at every epoch:
      1. PPS-only    (~20 ns confidence, always available)
      2. PPS + qErr  (~3 ns, when TIM-TP is available)
      3. Carrier-phase (~0.1 ns, when PPP filter has converged)

    The source with the lowest confidence interval drives the servo.
    PI gains scale with selected confidence: better measurement → more
    aggressive correction.  No warmup or step phases — phc_bootstrap.py
    ensures the PHC starts within ±10µs and ±10ppb, so PI tracking
    begins from epoch 1.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # ── Receiver driver: verify config on open ─────────────────────────
    # The F9T stores config in three layers (RAM, BBR, Flash).
    # configure_f9t.py writes all three, so config survives power cycles.
    # But DTR toggles on serial open can reset RAM, so we verify here
    # and reconfigure if needed.  This is our defensive stance:
    # never assume the receiver is configured; always verify.
    from peppar_fix.receiver import ensure_receiver_ready
    systems = set(args.systems.split(',')) if args.systems else None
    driver = ensure_receiver_ready(
        args.serial, args.baud, port_type=args.port_type, systems=systems)
    if driver is None:
        log.error("Receiver not producing dual-frequency observations — "
                  "cannot proceed. Run configure_f9t.py first.")
        return
    log.info(f"Receiver: {driver.name}")

    # ── Resolve position: --known-pos > position file > error ────────────
    position_source = None  # tracks where the position came from
    known_ecef = None

    if args.known_pos:
        # Explicit CLI override — highest priority
        parts = args.known_pos.split(',')
        lat, lon, alt = float(parts[0]), float(parts[1]), float(parts[2])
        known_ecef = lla_to_ecef(lat, lon, alt)
        position_source = "cli"
        log.info(f"Position (CLI): {lat:.6f}, {lon:.6f}, {alt:.1f}m")
    elif args.position_file:
        # Try loading from saved position file
        loaded = load_position(args.position_file)
        if loaded is not None:
            known_ecef = loaded
            position_source = "file"
            lat, lon, alt = ecef_to_lla(known_ecef[0], known_ecef[1], known_ecef[2])
            log.info(f"Position (file): {lat:.6f}, {lon:.6f}, {alt:.1f}m")
            log.info(f"  Loaded from: {args.position_file}")
            # TODO: sanity check saved position against NAV-PVT on startup.
            # The runtime PositionWatchdog will catch moved antennas.
        else:
            log.warning(f"Position file not found or invalid: {args.position_file}")

    if known_ecef is None:
        log.error("No position available. Provide --known-pos or --position-file.")
        sys.exit(1)

    # Open PTP device
    ptp = PtpDevice(args.ptp_dev)
    caps = ptp.get_caps()
    log.info(f"PHC: {args.ptp_dev}, max_adj={caps['max_adj']} ppb, "
             f"n_extts={caps['n_ext_ts']}, n_pins={caps['n_pins']}")

    # Read current adjfine — bootstrap should have set a good value.
    # Do NOT reset to 0; that destroys the bootstrap's frequency calibration.
    current_adj = ptp.read_adjfine()
    log.info("PHC adjfine at start: %.1f ppb", current_adj)

    # Configure SDP pin for extts
    extts_channel = args.extts_channel
    if args.program_pin and caps['n_pins'] > 0:
        try:
            ptp.set_pin_function(args.extts_pin, PTP_PF_EXTTS, extts_channel)
        except OSError:
            log.info("Pin config not supported by driver")
    else:
        log.info("Skipping pin programming; using implicit EXTS mapping")
    ptp.enable_extts(extts_channel, rising_edge=True)
    log.info(f"EXTTS enabled: pin={args.extts_pin}, channel={extts_channel}")

    # Set up PPP infrastructure
    beph = BroadcastEphemeris()
    ssr = SSRState()
    corrections = RealtimeCorrections(beph, ssr)
    obs_queue = queue.Queue(maxsize=100)
    stop_event = threading.Event()

    # QErrStore for TIM-TP quantization error (M6)
    qerr_store = QErrStore()

    # Read NTRIP config if provided
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

    # Config queue: main thread can send UBX config to receiver via serial_reader
    config_queue = queue.Queue(maxsize=10)

    # ── NTRIP caster (serve corrections to peers) ─────────────────────
    caster_server = None
    raw_callback = None
    if args.serve_caster:
        bind_parts = args.serve_caster.rsplit(':', 1)
        if len(bind_parts) == 2:
            caster_bind_addr = bind_parts[0]
            caster_bind_port = int(bind_parts[1])
        else:
            caster_bind_addr = ""
            caster_bind_port = int(bind_parts[0])
        caster_server = NtripCasterServer(
            bind_addr=caster_bind_addr, bind_port=caster_bind_port,
            station_id=args.caster_station_id)
        caster_server.start()

        # Callback: feed raw RAWX observations to the caster
        _ref_ecef = known_ecef  # reference position for RTCM 1005
        def _caster_raw_callback(parsed_msg):
            gps_time, obs = rawx_to_caster_obs(parsed_msg)
            if gps_time is not None and len(obs) >= 4:
                caster_server.broadcast_epoch(obs, gps_time,
                                              ref_ecef=_ref_ecef)
        raw_callback = _caster_raw_callback

    # Start serial reader (with qerr_store for TIM-TP extraction)
    t_serial = threading.Thread(
        target=serial_reader,
        args=(args.serial, args.baud, obs_queue, stop_event, beph, systems, ssr),
        kwargs={'qerr_store': qerr_store, 'config_queue': config_queue,
                'driver': driver, 'raw_callback': raw_callback},
        daemon=True,
    )
    t_serial.start()
    log.info(f"Serial: {args.serial} at {args.baud} baud")

    # Receiver signal check is done by ensure_receiver_ready() above.

    # Initialize PPP filter.
    # Bootstrap guarantees PHC is within ±10µs of truth, so the receiver
    # clock residual at the PPS edge is also near zero.  Seed clock at 0
    # with a moderate P — this makes sigma an honest convergence metric.
    # Without this, the LS pseudorange seed puts dt_rx at ~ms (the raw
    # receiver clock offset), and sigma drops to 0.1ns in one epoch
    # while the estimate is still ms off.
    filt = FixedPosFilter(known_ecef)
    filt.x[filt.IDX_CLK] = 0.0
    filt.P[filt.IDX_CLK, filt.IDX_CLK] = 100.0 ** 2  # 100m ≈ 333ns 1σ
    filt.initialized = True  # skip pseudorange seeding
    filt.prev_clock = 0.0

    # PI gains — base values, scaled by error source confidence at runtime
    BASE_KP = args.track_kp        # default 0.3
    BASE_KI = args.track_ki        # default 0.1

    # Gain scaling: gain_factor = clamp(REF_SIGMA / source_confidence)
    # REF_SIGMA chosen so gains = 1× at PPS+qErr quality (~2 ns)
    GAIN_REF_SIGMA = args.gain_ref_sigma
    GAIN_MIN_SCALE = 0.1           # floor (PPS-only: gentle)
    GAIN_MAX_SCALE = 3.0           # ceiling (excellent carrier: aggressive)

    # During convergence (large error), ensure minimum gain aggressiveness
    # so pull-in doesn't stall at PPS-only quality
    CONVERGE_ERROR_NS = 500        # above this, boost gains
    CONVERGE_MIN_SCALE = 2.0       # minimum gain scale during convergence

    servo = PIServo(BASE_KP, BASE_KI, max_ppb=caps['max_adj'])
    scheduler = DisciplineScheduler(
        base_interval=args.discipline_interval,
        adaptive=args.adaptive_interval,
        min_interval=args.min_interval,
        max_interval=args.max_interval,
    )
    watchdog = PositionWatchdog(
        threshold_m=args.watchdog_threshold,
    )
    phase = 'tracking'
    prev_t = None
    n_epochs = 0
    prev_source = None
    position_saved = False  # track whether we've saved position to file
    tmode_set = False       # track whether F9T has been switched to timing mode

    # PPS event queue: extts reader publishes edge events, strict gate decides
    # when an observation has a valid companion event.
    pps_queue = queue.Queue(maxsize=10)
    pps_history = deque()
    obs_history = deque()
    correlation_gate = StrictCorrelationGate()
    correction_gate = CorrectionFreshnessGate()

    def extts_reader():
        """Background thread reading PPS timestamps from PHC."""
        while not stop_event.is_set():
            event = ptp.read_extts(timeout_ms=1500)
            if event is None:
                continue
            phc_sec, phc_nsec, index, recv_mono, queue_remains, parse_age_s = event
            pps_event = PpsEvent(
                phc_sec=phc_sec,
                phc_nsec=phc_nsec,
                index=index,
                recv_mono=recv_mono,
                queue_remains=queue_remains,
                parse_age_s=parse_age_s,
                correlation_confidence=estimate_correlation_confidence(
                    queue_remains=queue_remains,
                    parse_age_s=parse_age_s,
                ),
            )
            dropped = queue_put_drop_oldest(
                pps_queue, pps_event
            )
            if dropped:
                log.debug("Dropped one stale PPS notification due to full queue")

    t_extts = threading.Thread(target=extts_reader, daemon=True)
    t_extts.start()
    log.info("EXTTS reader started")

    def pps_fractional_error(phc_sec, phc_nsec):
        """Compute PHC error from PPS fractional second.

        phc_nsec near 0 → PHC slightly ahead (positive error)
        phc_nsec near 1e9 → PHC slightly behind (negative error)
        """
        if phc_nsec <= 500_000_000:
            return float(phc_nsec)
        else:
            return float(phc_nsec) - 1_000_000_000

    def phc_gps_offset_s(phc_sec, phc_nsec, gps_unix_sec):
        """Whole-second offset: PHC_time - GPS_time."""
        phc_rounded = phc_sec if phc_nsec < 500_000_000 else phc_sec + 1
        return phc_rounded - gps_unix_sec

    def target_timescale_sec(gps_time):
        """Map a RAWX GPS epoch to the requested PHC timescale."""
        gps_sec = int(round(gps_time.timestamp()))
        if args.phc_timescale == 'gps':
            return gps_sec
        if args.phc_timescale == 'utc':
            return gps_sec - args.leap
        if args.phc_timescale == 'tai':
            return gps_sec + args.tai_minus_gps
        raise ValueError(f"Unsupported PHC timescale: {args.phc_timescale}")

    # Open log file
    log_f = None
    log_w = None
    if args.log:
        log_f = open(args.log, 'w', newline='')
        log_w = csv.writer(log_f)
        log_w.writerow([
            'timestamp', 'gps_second', 'phc_sec', 'phc_nsec',
            'phc_rounded_sec', 'epoch_offset_s', 'timescale_error_ns',
            'extts_index', 'stale_pps_dropped', 'pps_queue_depth',
            'dt_rx_ns', 'dt_rx_sigma_ns', 'pps_error_ns', 'qerr_ns',
            'source', 'source_error_ns', 'source_confidence_ns',
            'adjfine_ppb', 'phase', 'n_meas', 'gain_scale',
            'discipline_interval', 'n_accumulated', 'watchdog_alarm',
            'isb_gal_ns', 'isb_bds_ns',
        ])

    start_time = time.time()
    adjfine_ppb = current_adj
    gain_scale = 1.0

    try:
        while not stop_event.is_set():
            if args.duration and (time.time() - start_time) > args.duration:
                log.info(f"Duration limit reached ({args.duration}s)")
                break

            try:
                append_queue_history(obs_history, obs_queue, timeout=5)
            except queue.Empty:
                continue
            while True:
                try:
                    pps_history.append(pps_queue.get_nowait())
                except queue.Empty:
                    break

            obs_event, pps_match = correlation_gate.pop_observation_match(
                obs_history,
                target_sec_fn=lambda event: target_timescale_sec(event.gps_time),
                match_fn=lambda obs_event, target_sec, min_window_s=0.5, max_window_s=11.0:
                    match_pps_event_from_history(
                        pps_history,
                        obs_event,
                        target_sec,
                        min_window_s=min_window_s,
                        max_window_s=max_window_s,
                    ),
                min_confidence=args.min_correlation_confidence,
            )
            if obs_event is None:
                if n_epochs % 10 == 0 and obs_history:
                    log.info(f"  [{n_epochs}] Awaiting correlatable observation "
                             f"(queued={len(obs_history)})")
                continue
            gps_time, observations = obs_event

            ok_corr, corr_reason, corr_snapshot = correction_gate.accept(
                corrections,
                max_broadcast_age_s=args.max_broadcast_age_s,
                require_ssr=args.require_ssr,
                max_ssr_age_s=args.max_ssr_age_s,
                min_broadcast_confidence=args.min_broadcast_confidence,
                min_ssr_confidence=args.min_ssr_confidence,
            )
            if not ok_corr:
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

            # Feed watchdog with residual RMS
            resid_rms = float(np.sqrt(np.mean(resid ** 2))) if len(resid) > 0 else 0.0
            watchdog.update(resid_rms, n_used)
            if watchdog.alarmed:
                log.error("POSITION WATCHDOG ALARM: residuals indicate antenna "
                          "position has changed! Servo steering DISABLED. "
                          "Investigate and restart with correct position.")
                # Stop steering — don't call adjfine, let PHC free-run
                # The PPS OUT (if configured externally) will drift, which is
                # better than being wrong by a large constant offset.
                break

            dt_rx_ns = filt.x[filt.IDX_CLK] / C * 1e9
            p_clk = filt.P[filt.IDX_CLK, filt.IDX_CLK]
            dt_rx_sigma = math.sqrt(max(0, p_clk)) / C * 1e9
            n_epochs += 1

            # Extract ISBs (inter-system biases) for logging
            # FixedPosFilter has IDX_ISB_GAL; future: IDX_ISB_BDS etc.
            isb_ns = {}
            if hasattr(filt, 'IDX_ISB_GAL') and filt.x.shape[0] > filt.IDX_ISB_GAL:
                isb_ns['gal'] = filt.x[filt.IDX_ISB_GAL] / C * 1e9
            if hasattr(filt, 'IDX_ISB_BDS') and filt.x.shape[0] > getattr(filt, 'IDX_ISB_BDS', 999):
                isb_ns['bds'] = filt.x[filt.IDX_ISB_BDS] / C * 1e9

            # Once filter converges: save position and switch F9T to timing mode
            if n_epochs >= 300 and dt_rx_sigma < 100.0:
                sigma_m = dt_rx_sigma * 1e-9 * C  # convert ns to meters

                # Save position to file
                if args.position_file and not position_saved and sigma_m < 0.1:
                    save_position(
                        args.position_file, known_ecef,
                        sigma_m=sigma_m,
                        source="ppp_bootstrap" if position_source == "file" else "known_pos",
                        note=f"saved after {n_epochs} epochs, dt_rx_sigma={dt_rx_sigma:.2f}ns",
                    )
                    position_saved = True
                    log.info(f"Position saved to {args.position_file} "
                             f"(sigma={sigma_m:.4f}m after {n_epochs} epochs)")

                # Switch receiver to fixed-position timing mode for better
                # PPS+qErr fallback.  Only done once, and only for timing
                # receivers (F9T).  Navigation receivers (F10T) return None.
                if not tmode_set and sigma_m < 0.1:
                    tmode_msg = build_tmode_fixed_msg(known_ecef, driver=driver)
                    if tmode_msg is not None:
                        config_queue.put(tmode_msg)
                        tmode_set = True
                        lat, lon, alt = ecef_to_lla(
                            known_ecef[0], known_ecef[1], known_ecef[2])
                        log.info(f"{driver.name} → fixed-position timing mode "
                                 f"({lat:.6f}, {lon:.6f}, {alt:.1f}m)")

            pps_event, _epoch_delta_s, pps_match_recv_dt_s, _pps_match_confidence = pps_match
            phc_sec, phc_nsec, extts_index = pps_event
            target_sec = target_timescale_sec(gps_time)
            phc_rounded_sec = pps_event.rounded_sec()
            epoch_offset = phc_rounded_sec - target_sec
            ts_str = gps_time.strftime('%Y-%m-%d %H:%M:%S')
            pps_error_ns = pps_fractional_error(phc_sec, phc_nsec)
            timescale_error_ns = epoch_offset * 1_000_000_000 + pps_error_ns
            pps_queue_depth = pps_queue.qsize()
            stale_pps_dropped = 0

            # Get qErr from TIM-TP (None if stale or unavailable)
            qerr_ns, _ = qerr_store.get()

            # Compute competitive error sources (M6).
            # dt_rx tracks the RECEIVER clock offset from GPS time, not
            # the PHC-to-GPS offset.  The formula PPS+PPP = pps_error +
            # dt_rx is only valid once the filter has transitioned from
            # absolute pseudorange mode to time-differenced carrier phase,
            # where dt_rx changes track PHC changes.  Sigma is honest
            # (formal EKF uncertainty given the measurements) but doesn't
            # distinguish between "converged on receiver clock" and
            # "converged on PHC-relative correction."  Gate on abs(dt_rx)
            # to ensure the filter state is PHC-compatible.
            use_dt_rx = dt_rx_ns if abs(dt_rx_ns) < 100_000 else None
            use_sigma = dt_rx_sigma if use_dt_rx is not None else None
            sources = compute_error_sources(
                pps_error_ns, qerr_ns, use_dt_rx, use_sigma,
            )
            best = sources[0]

            # ── Continuous tracking with competitive error sources ──────
            # Outlier rejection
            # Outlier rejection: skip extreme errors in steady state.
            # During convergence (scheduler._converging), allow large errors
            # through — the convergence gains need them to pull in.
            if abs(best.error_ns) > 5000 and not scheduler._converging:
                log.warning(f"  Outlier: {best}, skipping")
                continue

            # Accumulate sample into discipline scheduler (M7)
            scheduler.accumulate(best.error_ns, best.confidence_ns, best.name)

            # Log source transitions
            if prev_source != best.name:
                if prev_source is not None:
                    log.info(f"  Source: {prev_source} → {best.name} "
                             f"(confidence {best.confidence_ns:.1f}ns)")
                prev_source = best.name

            if scheduler.should_correct():
                # Flush buffer: get averaged error and confidence
                avg_error, avg_confidence, n_samples = scheduler.flush()

                # Gain scaling by averaged confidence
                gain_scale = max(GAIN_MIN_SCALE, min(GAIN_MAX_SCALE,
                                 GAIN_REF_SIGMA / avg_confidence))

                # Boost gains during convergence (large error) to ensure
                # pull-in doesn't stall when using low-confidence sources
                if abs(avg_error) > CONVERGE_ERROR_NS:
                    gain_scale = max(gain_scale, CONVERGE_MIN_SCALE)

                servo.kp = BASE_KP * gain_scale
                servo.ki = BASE_KI * gain_scale

                # Negate: positive error (PHC ahead) → negative adjfine (slow down)
                # dt = n_samples seconds since last correction
                adjfine_ppb = -servo.update(avg_error, dt=float(n_samples))
                ptp.adjfine(adjfine_ppb)

                # Update drift rate tracker for adaptive mode
                scheduler.update_drift_rate(time.monotonic(), adjfine_ppb)

                # Adapt interval for next cycle
                scheduler.compute_adaptive_interval(avg_confidence)

                if n_epochs % 10 == 0:
                    log.info(f"  [{n_epochs}] {best.name}: "
                             f"err={avg_error:+.1f}ns (avg {n_samples}) "
                             f"adj={adjfine_ppb:+.1f}ppb "
                             f"gain={gain_scale:.2f}x "
                             f"interval={scheduler.interval}")
            else:
                # Coast epoch: don't call adjfine, just log
                n_samples = 0
                if n_epochs % 10 == 0:
                    log.info(f"  [{n_epochs}] {best.name}: "
                             f"err={best.error_ns:+.1f}ns "
                             f"coast ({scheduler.n_accumulated}/{scheduler.interval}) "
                             f"adj={adjfine_ppb:+.1f}ppb")

            # CSV log (every epoch, including coast)
            if log_w:
                log_w.writerow([
                    ts_str, target_sec, phc_sec, phc_nsec,
                    phc_rounded_sec, epoch_offset, f'{timescale_error_ns:.1f}',
                    extts_index, stale_pps_dropped, pps_queue_depth,
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
        if caster_server is not None:
            caster_server.stop()
        try:
            ptp.adjfine(0.0)
        except Exception:
            pass
        ptp.disable_extts(extts_channel)
        ptp.close()
        if log_f:
            log_f.close()

    elapsed = time.time() - start_time
    log.info(f"\n{'='*60}")
    log.info(f"  PHC servo complete (M7 adaptive discipline interval)")
    log.info(f"  Duration: {elapsed:.0f}s, Epochs: {n_epochs}")
    log.info(f"  Last source: {prev_source}, adjfine: {adjfine_ppb:+.3f} ppb")
    log.info(f"{'='*60}")


# ── CLI ──────────────────────────────────────────────────────────────────── #

def main():
    ap = argparse.ArgumentParser(
        description="PHC discipline loop with competitive error sources and adaptive discipline interval (M7)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Position
    ap.add_argument("--known-pos", default=None,
                    help="Known position as lat,lon,alt (overrides position file)")
    ap.add_argument("--position-file", default=None,
                    help="JSON file for position save/load (default: None)")
    ap.add_argument("--watchdog-threshold", type=float, default=0.5,
                    help="Position watchdog threshold in meters (default: 0.5)")
    ap.add_argument("--leap", type=int, default=18,
                    help="GPS-UTC leap seconds (default: 18)")
    ap.add_argument("--tai-minus-gps", type=int, default=19,
                    help="TAI-GPS offset in seconds (default: 19)")
    ap.add_argument("--systems", default="gps,gal,bds",
                    help="GNSS systems to use (default: gps,gal)")

    # Serial / Receiver
    ap.add_argument("--serial", required=True,
                    help="Receiver serial port (e.g. /dev/gnss-top)")
    ap.add_argument("--receiver", default="f9t",
                    help="Receiver model: f9t, f10t (default: f9t)")
    ap.add_argument("--baud", type=int, default=9600,
                    help="Serial baud rate (default: 9600)")
    ap.add_argument("--port-type", default="USB",
                    choices=["UART", "UART2", "USB", "SPI", "I2C"],
                    help="Receiver port type for UBX message routing (default: USB)")

    # NTRIP (direct args or config file)
    ap.add_argument("--ntrip-conf", help="NTRIP config file (INI format)")
    ap.add_argument("--caster", default="products.igs-ip.net")
    ap.add_argument("--port", type=int, default=2101)
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--tls", action="store_true")
    ap.add_argument("--eph-mount", required=True,
                    help="Broadcast ephemeris mountpoint")
    ap.add_argument("--ssr-mount", default=None,
                    help="SSR corrections mountpoint (optional)")
    ap.add_argument("--max-broadcast-age-s", type=float, default=None,
                    help="Maximum host-monotonic age for broadcast correction state (default: 30)")
    ap.add_argument("--require-ssr", action="store_true", default=None,
                    help="Require fresh SSR state before EKF updates")
    ap.add_argument("--max-ssr-age-s", type=float, default=None,
                    help="Maximum host-monotonic age for SSR state when --require-ssr is set (default: 30)")
    ap.add_argument("--min-broadcast-confidence", type=float, default=None,
                    help="Minimum acceptable confidence for broadcast correction timing")
    ap.add_argument("--min-ssr-confidence", type=float, default=None,
                    help="Minimum acceptable confidence for SSR correction timing")

    # PTP
    ap.add_argument("--ptp-profile", choices=["i226", "e810"],
                    help="PTP NIC profile for default PHC/pin/channel settings")
    ap.add_argument("--device-config", default="config/receivers.toml",
                    help="Device/profile config TOML (default: config/receivers.toml)")
    ap.add_argument("--ptp-dev", default=None,
                    help="PTP device (profile/default if omitted)")
    ap.add_argument("--extts-pin", type=int, default=None,
                    help="PTP pin index for PPS input (profile/default if omitted)")
    ap.add_argument("--extts-channel", type=int, default=None,
                    help="PTP EXTS channel for PPS input (profile/default if omitted)")
    ap.add_argument("--program-pin", action="store_true",
                    help="Explicitly program PTP pin function before enabling EXTS")
    ap.add_argument("--phc-timescale", choices=["gps", "utc", "tai"], default=None,
                    help="Target PHC timescale for PPS alignment (profile/default if omitted)")
    ap.add_argument("--min-correlation-confidence", type=float, default=None,
                    help="Minimum acceptable confidence for observation/PPS correlation")

    # Servo tuning
    ap.add_argument("--track-kp", type=float, default=0.3,
                    help="Tracking mode Kp gain (default: 0.3)")
    ap.add_argument("--track-ki", type=float, default=0.1,
                    help="Tracking mode Ki gain (default: 0.1)")
    ap.add_argument("--gain-ref-sigma", type=float, default=2.0,
                    help="Reference confidence (ns) for gain scale=1.0 (default: 2.0)")

    # Discipline interval (M7)
    ap.add_argument("--discipline-interval", type=int, default=1,
                    help="Fixed discipline interval in epochs (default: 1 = M6 behavior)")
    ap.add_argument("--adaptive-interval", action="store_true",
                    help="Enable adaptive discipline interval based on drift rate")
    ap.add_argument("--max-interval", type=int, default=120,
                    help="Maximum discipline interval in epochs (default: 120)")
    ap.add_argument("--min-interval", type=int, default=1,
                    help="Minimum discipline interval in epochs (default: 1)")

    # NTRIP caster (serve corrections to peers)
    ap.add_argument("--serve-caster", default=None, metavar="[ADDR]:PORT",
                    help="Start NTRIP caster on this bind address "
                    "(e.g. :2102 or 0.0.0.0:2102)")
    ap.add_argument("--caster-station-id", type=int, default=0,
                    help="RTCM station ID for caster (0-4095, default: 0)")

    # Output
    ap.add_argument("--log", default=None,
                    help="CSV log file for servo data")
    ap.add_argument("--duration", type=int, default=None,
                    help="Run duration in seconds")

    args = ap.parse_args()
    apply_ptp_profile(args)
    if args.ptp_dev is None:
        args.ptp_dev = "/dev/ptp0"
    if args.extts_pin is None:
        args.extts_pin = 1
    if args.extts_channel is None:
        args.extts_channel = 0
    if args.phc_timescale is None:
        args.phc_timescale = "utc"
    if args.max_broadcast_age_s is None:
        args.max_broadcast_age_s = 30.0
    if args.require_ssr is None:
        args.require_ssr = False
    if args.max_ssr_age_s is None:
        args.max_ssr_age_s = 30.0
    if args.min_correlation_confidence is None:
        args.min_correlation_confidence = 0.5
    if args.min_broadcast_confidence is None:
        args.min_broadcast_confidence = 0.0
    if args.min_ssr_confidence is None:
        args.min_ssr_confidence = 0.0
    run_servo(args)


if __name__ == "__main__":
    main()
