#!/usr/bin/env python3
"""peppar-ensure-receiver: single entry point for receiver initialization.

Checks dual-frequency observations, reconfigures if needed (signals,
L5 health override, warm restart, measurement rate, message routing),
and verifies the result.  Replaces peppar_rx_config.py in the wrapper.

Exit codes:
    0 = receiver ready (prints driver name to stdout)
    1 = receiver cannot be brought to dual-frequency state
"""

import argparse
import logging
import os
import sys

log = logging.getLogger("peppar_ensure_receiver")


def main():
    ap = argparse.ArgumentParser(
        description="Initialize F9T receiver for peppar-fix",
    )
    ap.add_argument("serial", help="Serial port (e.g. /dev/gnss-top)")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--port-type", default="USB",
                    choices=["UART", "UART2", "USB", "SPI", "I2C"])
    ap.add_argument("--systems", default="gps,gal",
                    help="GNSS systems (comma-separated)")
    ap.add_argument("--measurement-rate-ms", type=int, default=1000,
                    help="Measurement rate in ms (default: 1000)")
    ap.add_argument("--sfrbx-rate", type=int, default=1,
                    help="SFRBX decimation (0=disabled, default: 1)")
    ap.add_argument("-v", "--verbose", action="store_true")

    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    systems = set(args.systems.split(","))

    # Auto-detect kernel GNSS → bandwidth-limited defaults
    base = os.path.basename(args.serial)
    is_kernel_gnss = base.startswith("gnss") and base[4:].isdigit()
    if is_kernel_gnss:
        if args.sfrbx_rate > 0:
            log.info("Kernel GNSS device — defaulting sfrbx_rate=0")
            args.sfrbx_rate = 0
        if args.measurement_rate_ms < 2000:
            log.info("Kernel GNSS device — defaulting measurement_rate_ms=2000")
            args.measurement_rate_ms = 2000

    from peppar_fix.receiver import ensure_receiver_ready

    driver = ensure_receiver_ready(
        args.serial,
        args.baud,
        port_type=args.port_type,
        systems=systems,
        sfrbx_rate=args.sfrbx_rate,
        measurement_rate_ms=args.measurement_rate_ms,
    )

    if driver is None:
        log.error("Receiver initialization failed")
        sys.exit(1)

    # Print driver name so the wrapper can capture it
    print(driver.name)
    sys.exit(0)


if __name__ == "__main__":
    main()
