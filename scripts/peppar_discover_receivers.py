#!/usr/bin/env python3
"""peppar-discover-receivers: scan serial ports for u-blox receivers.

Identifies each receiver by SEC-UNIQID + MON-VER and prints a summary.
Also shows stored state (position, last_seen) if available.

Usage:
    python3 peppar_discover_receivers.py
    python3 peppar_discover_receivers.py /dev/ttyACM0 /dev/ttyUSB0
    python3 peppar_discover_receivers.py --baud 115200
"""

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

log = logging.getLogger("peppar_discover_receivers")


def main():
    ap = argparse.ArgumentParser(
        description="Scan serial ports for u-blox receivers",
    )
    ap.add_argument("ports", nargs="*", default=None,
                    help="Serial ports to scan (default: auto-discover)")
    ap.add_argument("--baud", type=int, nargs="*", default=None,
                    help="Baud rates to try (default: 9600 115200 460800)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    from peppar_fix.receiver import discover_receivers
    from peppar_fix.receiver_state import load_receiver_state

    ports = args.ports if args.ports else None
    baud_rates = args.baud if args.baud else None

    print("Scanning for receivers...\n")
    results = discover_receivers(ports, baud_rates)

    if not results:
        print("No receivers found.")
        return 1

    for port, baud, identity in results:
        uid = identity.get("unique_id")
        uid_hex = identity.get("unique_id_hex", "?")
        module = identity.get("module", "unknown")
        firmware = identity.get("firmware", "unknown")
        protver = identity.get("protver", "?")

        print(f"  {port} @ {baud} baud")
        print(f"    Module:   {module}")
        print(f"    Firmware: {firmware} (PROTVER {protver})")
        print(f"    UniqueID: {uid} ({uid_hex})")

        if uid is not None:
            state = load_receiver_state(uid)
            if state is not None:
                pos = state.get("last_known_position")
                if pos:
                    ecef = pos.get("ecef_m", [])
                    sigma = pos.get("sigma_m", "?")
                    print(f"    Position: ECEF {ecef} (sigma={sigma}m)")
                else:
                    print(f"    Position: none")
                print(f"    Last seen: {state.get('last_seen', '?')}")
            else:
                print(f"    State:    new (no stored state)")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
