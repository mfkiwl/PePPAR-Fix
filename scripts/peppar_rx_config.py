#!/usr/bin/env python3
"""peppar-rx-config: Verify and configure a u-blox F9T for peppar-fix.

Passively listens first to check what the receiver is already producing.
Only reconfigures if requirements are not met.

Requirements for peppar-fix operation:
  - RXM-RAWX (dual-frequency raw observations)
  - RXM-SFRBX (broadcast navigation data)
  - NAV-PVT (position/velocity/time)
  - TIM-TP (PPS quantization error)
  - Dual-frequency signals: GPS L1+L5, GAL E1+E5a, BDS B1+B2a
  - At least 4 dual-frequency SVs per epoch

Exit codes:
    0 = requirements met (no changes, or changes applied successfully)
    1 = error (couldn't communicate with receiver)
    2 = requirements not met and --dry-run (report only, no changes)
"""

import argparse
import logging
import os
import sys
import time
import tomllib

from peppar_fix.receiver import (
    probe_baud, open_receiver, listen_for_messages,
    REQUIRED_MESSAGES, MESSAGE_TIMEOUTS, required_messages,
    full_configure, configure_signals, configure_gps_l5_health,
    configure_messages, configure_nmea_off, configure_tmode,
    configure_rate, configure_uart_baud,
    warm_restart, reopen_after_reset, factory_reset, get_driver,
)

log = logging.getLogger("peppar_rx_config")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DRY_RUN_FAIL = 2


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
    if not args.program_pin:
        args.program_pin = bool(profile.get("program_pin", False))


def check_pps(ptp_dev, extts_pin, extts_channel=0, program_pin=True, timeout_s=5):
    """Check if PPS is arriving on the specified SDP pin.

    Returns True if at least one PPS event is received.
    """
    try:
        from peppar_fix import PtpDevice
        from peppar_fix.ptp_device import PTP_PF_EXTTS
    except ImportError:
        log.warning("Cannot check PPS: peppar_fix.ptp_device not available")
        return False

    ptp = PtpDevice(ptp_dev)
    if program_pin:
        try:
            ptp.set_pin_function(extts_pin, PTP_PF_EXTTS, extts_channel)
        except OSError:
            pass
    ptp.enable_extts(extts_channel, rising_edge=True)

    log.info(f"  Checking PPS on {ptp_dev} pin={extts_pin} channel={extts_channel} ({timeout_s}s)...")
    event = ptp.read_extts(timeout_ms=timeout_s * 1000)
    ptp.disable_extts(extts_channel)
    ptp.close()

    if event is not None:
        phc_sec, phc_nsec, _, _recv_mono, _queue_remains, _parse_age_s = event
        log.info(f"  PPS detected: {phc_sec}.{phc_nsec:09d}")
        return True
    else:
        log.warning(f"  No PPS detected on pin={extts_pin} channel={extts_channel} within {timeout_s}s")
        return False


def _is_kernel_gnss(port):
    """Return True for kernel GNSS char devices like /dev/gnss0."""
    base = os.path.basename(port)
    return base.startswith("gnss") and base[4:].isdigit()


def run(args):
    """Main verify-and-configure flow."""
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    port_id = {"UART": 1, "UART2": 2, "USB": 3, "SPI": 4, "I2C": 0}[args.port_type]
    driver = get_driver(args.receiver)
    # Kernel GNSS char devices use I2C with a 15-byte AQ bandwidth limit.
    # Only require RAWX+TIM-TP to stay within ~1.5 kB/s throughput ceiling.
    minimal = _is_kernel_gnss(args.serial)
    if minimal:
        log.info("Kernel GNSS device — using minimal message set (RAWX+TIM-TP)")

    # ── Factory reset if requested ──────────────────────────────────────
    if args.factory_reset:
        if args.dry_run:
            log.info("--factory-reset ignored in --dry-run mode")
        else:
            log.info("Factory reset requested")
            baud = probe_baud(args.serial) or args.baud
            ser, ubr = open_receiver(args.serial, baud)
            factory_reset(ser, ubr)
            ser.close()
            ser, ubr = reopen_after_reset(args.serial, wait_s=5)
            # After factory reset, definitely need full configuration
            log.info("Configuring after factory reset...")
            _do_configure(ser, ubr, args, port_id, driver)
            ser.close()
            log.info("Factory reset + configure complete")
            return EXIT_OK

    # ── Probe baud rate ─────────────────────────────────────────────────
    log.info(f"Probing {args.serial}...")
    baud = probe_baud(args.serial)
    if baud is None:
        # Try the specified baud as fallback
        baud = args.baud
        log.info(f"  Probe failed, trying {baud} baud...")
        try:
            ser, ubr = open_receiver(args.serial, baud)
        except Exception as e:
            log.error(f"Cannot open {args.serial}: {e}")
            return EXIT_ERROR
    else:
        log.info(f"  Receiver found at {baud} baud")
        ser, ubr = open_receiver(args.serial, baud)

    # ── Passive listen phase ────────────────────────────────────────────
    log.info("Listening for UBX messages...")
    req = required_messages(minimal=minimal)
    seen, missing, signal_info = listen_for_messages(ser, ubr, required=req, driver=driver)

    # Report findings
    log.info(f"  Messages detected: {sorted(seen)}")
    if missing:
        log.warning(f"  Missing required messages: {sorted(missing)}")
    else:
        log.info(f"  All required messages present")

    if signal_info['systems']:
        log.info(f"  Constellations: {sorted(signal_info['systems'])}")
    if signal_info['dual_freq_svs'] is not None:
        log.info(f"  Dual-frequency SVs: {signal_info['dual_freq_svs']}")
    if signal_info['rate_hz'] is not None:
        log.info(f"  Measurement rate: ~{signal_info['rate_hz']} Hz")

    # ── Check PPS if requested ──────────────────────────────────────────
    pps_ok = True
    if args.check_pps:
        ser.close()
        pps_ok = check_pps(
            args.ptp_dev,
            args.extts_pin,
            extts_channel=args.extts_channel,
            program_pin=args.program_pin,
        )
        # Reopen serial
        ser, ubr = open_receiver(args.serial, baud)

    # ── Evaluate requirements ───────────────────────────────────────────
    needs_config = False
    reasons = []

    if missing:
        needs_config = True
        reasons.append(f"missing messages: {sorted(missing)}")

    if signal_info['dual_freq_svs'] is not None and signal_info['dual_freq_svs'] < 4:
        needs_config = True
        reasons.append(f"only {signal_info['dual_freq_svs']} dual-freq SVs (need >=4)")

    if not pps_ok and args.check_pps:
        reasons.append("no PPS on SDP")

    if not needs_config and not reasons:
        log.info("Requirements met -- no changes needed")
        ser.close()
        return EXIT_OK

    if not needs_config and reasons:
        # PPS issue only -- not a config problem
        for r in reasons:
            log.warning(f"  {r}")
        ser.close()
        return EXIT_OK

    # ── Dry run: report and exit ────────────────────────────────────────
    if args.dry_run:
        log.info("Changes needed but --dry-run specified:")
        for r in reasons:
            log.info(f"  - {r}")
        ser.close()
        return EXIT_DRY_RUN_FAIL

    # ── Apply configuration ─────────────────────────────────────────────
    log.info("Configuring receiver...")
    for r in reasons:
        log.info(f"  Reason: {r}")

    _do_configure(ser, ubr, args, port_id, driver, minimal=minimal)
    ser.close()

    # ── Verify after configuration ──────────────────────────────────────
    log.info("Verifying configuration...")
    baud = probe_baud(args.serial) or args.baud
    ser, ubr = open_receiver(args.serial, baud)
    if hasattr(ser, "discard_input"):
        try:
            drained = ser.discard_input(idle_s=0.5)
            if drained:
                log.info(f"  Drained {drained} queued kernel-GNSS bytes before verify")
        except Exception as e:
            log.debug(f"  Kernel-GNSS drain skipped: {e}")
    seen2, missing2, signal_info2 = listen_for_messages(ser, ubr, required=req, driver=driver)
    ser.close()

    if missing2:
        log.error(f"Still missing after configure: {sorted(missing2)}")
        return EXIT_ERROR

    log.info("Configuration applied and verified")
    return EXIT_OK


def _do_configure(ser, ubr, args, port_id, driver, minimal=False):
    """Apply receiver configuration (signals, messages, rate, tmode, L5)."""
    configure_signals(ser, ubr, driver=driver)
    l5_ok = configure_gps_l5_health(ser, ubr)

    if l5_ok:
        log.info("  Warm restart for L5 health override...")
        warm_restart(ser)
        ser.close()
        ser, ubr = reopen_after_reset(args.serial, wait_s=10)

    configure_rate(ser, ubr, args.rate)
    configure_messages(ser, ubr, port_id, minimal=minimal)
    configure_nmea_off(ser, ubr, port_id)
    configure_tmode(ser, ubr, args.survey_dur, args.survey_acc)

    basename = os.path.basename(args.serial)
    is_kernel_gnss = basename.startswith("gnss") and basename[4:].isdigit()
    if args.port_type == "UART" and args.target_baud != args.baud and not is_kernel_gnss:
        configure_uart_baud(ser, ubr, args.target_baud)

    log.info("  Configuration saved to RAM + BBR + Flash")
    return ser, ubr


# ── CLI ──────────────────────────────────────────────────────────────────── #

def main():
    ap = argparse.ArgumentParser(
        description="Verify and configure F9T receiver for peppar-fix",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes:
  0  Requirements met (no changes, or changes applied successfully)
  1  Error (couldn't communicate with receiver)
  2  Requirements not met (--dry-run mode, no changes applied)

Examples:
  # Check what receiver is doing (no changes):
  peppar-rx-config /dev/gnss-top --port-type USB --dry-run

  # Verify and configure if needed:
  peppar-rx-config /dev/gnss-top --port-type USB

  # Full factory reset + configure:
  peppar-rx-config /dev/gnss-top --port-type USB --factory-reset

  # Also check PPS on SDP1:
  peppar-rx-config /dev/gnss-top --port-type USB --check-pps
""",
    )

    ap.add_argument("serial", help="Serial port (e.g. /dev/gnss-top)")
    ap.add_argument("--baud", type=int, default=9600,
                    help="Initial baud rate (default: 9600)")
    ap.add_argument(
        "--receiver",
        default="f9t",
        help="Receiver model/profile: f9t, f9t-l5, f10t (default: f9t)",
    )
    ap.add_argument("--target-baud", type=int, default=460800,
                    help="Target UART baud rate (default: 460800, ignored for non-UART ports)")
    ap.add_argument("--port-type", default="USB", choices=["UART", "UART2", "USB", "SPI"],
                    help="u-blox logical port to configure (default: USB)")
    ap.add_argument("--rate", type=int, default=1,
                    help="Measurement rate in Hz (default: 1)")
    ap.add_argument("--survey-dur", type=int, default=300,
                    help="Survey-in duration in seconds (default: 300)")
    ap.add_argument("--survey-acc", type=float, default=5.0,
                    help="Survey-in accuracy in meters (default: 5.0)")

    # Mode flags
    ap.add_argument("--dry-run", action="store_true",
                    help="Report status only, don't change receiver config")
    ap.add_argument("--factory-reset", action="store_true",
                    help="Factory reset before configuring")
    ap.add_argument("--check-pps", action="store_true",
                    help="Check PPS arriving on SDP pin")
    ap.add_argument("--ptp-profile", choices=["i226", "e810"],
                    help="PTP NIC profile for default PHC/pin/channel settings")
    ap.add_argument("--device-config", default="config/receivers.toml",
                    help="Device/profile config TOML (default: config/receivers.toml)")
    ap.add_argument("--ptp-dev", default=None,
                    help="PTP device for PPS check (profile/default if omitted)")
    ap.add_argument("--extts-pin", type=int, default=None,
                    help="PTP pin index for PPS check (profile/default if omitted)")
    ap.add_argument("--extts-channel", type=int, default=None,
                    help="PTP EXTS channel for PPS check (profile/default if omitted)")
    ap.add_argument("--program-pin", action="store_true",
                    help="Explicitly program PTP pin function before enabling EXTS")
    ap.add_argument("-v", "--verbose", action="store_true")

    args = ap.parse_args()
    apply_ptp_profile(args)
    if args.ptp_dev is None:
        args.ptp_dev = "/dev/ptp0"
    if args.extts_pin is None:
        args.extts_pin = 1
    if args.extts_channel is None:
        args.extts_channel = 0
    sys.exit(run(args))


if __name__ == "__main__":
    main()
