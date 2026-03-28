#!/usr/bin/env python3
"""Reproducer for igc driver Tx timestamp timeout bug.

The igc driver (Intel i225/i226) has a race between clock_adjtime
(ADJ_FREQUENCY) and hardware TX timestamping.  When adjfine() is
called while a TX timestamp is pending, the timestamp register can
wedge, producing "Tx timestamp timeout" errors in dmesg and eventually
breaking EXTTS (PPS capture).

This reproducer runs two threads:
  1. Hammers clock_adjtime(ADJ_FREQUENCY) on the PHC
  2. Sends UDP packets with SO_TIMESTAMPING (hardware TX timestamps)

On a vulnerable igc driver, "Tx timestamp timeout" appears in dmesg
within seconds.

Usage:
    sudo python3 tools/igc_tx_timeout_repro.py eth1 /dev/ptp0
    # Watch with: dmesg -w | grep "Tx timestamp"

Requires root for SO_TIMESTAMPING and clock_adjtime on PHC.
"""

import ctypes
import ctypes.util
import os
import socket
import struct
import sys
import threading
import time


def get_phc_clockid(fd):
    return (~fd << 3) | 3


def adjfine_loop(phc_fd, stop_event, stats):
    """Hammer adjfine() on the PHC."""
    librt = ctypes.CDLL(ctypes.util.find_library("rt"), use_errno=True)

    class Timeval(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]

    class Timex(ctypes.Structure):
        _fields_ = [
            ("modes", ctypes.c_uint),
            ("offset", ctypes.c_long),
            ("freq", ctypes.c_long),
            ("maxerror", ctypes.c_long),
            ("esterror", ctypes.c_long),
            ("status", ctypes.c_int),
            ("constant", ctypes.c_long),
            ("precision", ctypes.c_long),
            ("tolerance", ctypes.c_long),
            ("time", Timeval),
            ("tick", ctypes.c_long),
            ("ppsfreq", ctypes.c_long),
            ("jitter", ctypes.c_long),
            ("shift", ctypes.c_int),
            ("stabil", ctypes.c_long),
            ("jitcnt", ctypes.c_long),
            ("calcnt", ctypes.c_long),
            ("errcnt", ctypes.c_long),
            ("stbcnt", ctypes.c_long),
            ("tai", ctypes.c_int),
        ]

    ADJ_FREQUENCY = 0x0002
    clockid = get_phc_clockid(phc_fd)
    n = 0
    toggle = False

    while not stop_event.is_set():
        tx = Timex()
        tx.modes = ADJ_FREQUENCY
        # Alternate between two tiny frequency offsets
        tx.freq = 100 if toggle else -100  # ~0.0015 ppb
        toggle = not toggle
        ret = librt.clock_adjtime(ctypes.c_int32(clockid), ctypes.byref(tx))
        if ret < 0:
            print(f"adjfine error: {ctypes.get_errno()}")
            break
        n += 1

    stats["adjfine_count"] = n


def tx_timestamp_loop(iface, stop_event, stats):
    """Send UDP packets requesting hardware TX timestamps."""
    # SOL_SOCKET options for timestamping
    SO_TIMESTAMPING = 37
    SOF_TIMESTAMPING_TX_HARDWARE = (1 << 0)
    SOF_TIMESTAMPING_RAW_HARDWARE = (1 << 6)
    SOF_TIMESTAMPING_OPT_TSONLY = (1 << 11)

    flags = (SOF_TIMESTAMPING_TX_HARDWARE |
             SOF_TIMESTAMPING_RAW_HARDWARE |
             SOF_TIMESTAMPING_OPT_TSONLY)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPING, flags)
    # Bind to the specific interface
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                    iface.encode() + b'\0')
    sock.settimeout(0.01)

    # Send to a harmless destination (localhost or broadcast)
    dest = ("224.0.0.1", 9999)  # multicast, won't route
    payload = b"igc_repro" + b"\x00" * 32
    n = 0
    errors = 0

    while not stop_event.is_set():
        try:
            sock.sendto(payload, dest)
            n += 1
        except OSError:
            errors += 1
        # Don't sleep — maximize collision probability
        # But yield to avoid starving the adjfine thread
        if n % 100 == 0:
            time.sleep(0.0001)

    sock.close()
    stats["tx_count"] = n
    stats["tx_errors"] = errors


def check_dmesg_for_timeout():
    """Check if Tx timestamp timeout appeared in dmesg."""
    try:
        import subprocess
        result = subprocess.run(
            ["dmesg", "--since", "60 seconds ago"],
            capture_output=True, text=True, timeout=2
        )
        return "Tx timestamp timeout" in result.stdout
    except Exception:
        return False


def main():
    if len(sys.argv) < 3:
        print(f"Usage: sudo {sys.argv[0]} <interface> <ptp_device>")
        print(f"Example: sudo {sys.argv[0]} eth1 /dev/ptp0")
        sys.exit(1)

    iface = sys.argv[1]
    ptp_dev = sys.argv[2]
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0

    if os.geteuid() != 0:
        print("ERROR: must run as root (sudo)")
        sys.exit(1)

    phc_fd = os.open(ptp_dev, os.O_RDWR)
    print(f"Opened {ptp_dev} (fd={phc_fd})")
    print(f"Interface: {iface}")
    print(f"Duration: {duration}s")
    print(f"Watch for: dmesg -w | grep 'Tx timestamp timeout'")
    print()

    stop = threading.Event()
    stats = {}

    t_adj = threading.Thread(target=adjfine_loop, args=(phc_fd, stop, stats))
    t_tx = threading.Thread(target=tx_timestamp_loop, args=(iface, stop, stats))

    t_adj.start()
    t_tx.start()

    start = time.monotonic()
    triggered = False
    while time.monotonic() - start < duration:
        time.sleep(1)
        elapsed = time.monotonic() - start
        if check_dmesg_for_timeout():
            print(f"\n*** Tx timestamp timeout detected after {elapsed:.1f}s ***")
            triggered = True
            break
        print(f"  {elapsed:.0f}s: running...", end="\r")

    stop.set()
    t_adj.join(timeout=2)
    t_tx.join(timeout=2)
    os.close(phc_fd)

    adj_n = stats.get("adjfine_count", 0)
    tx_n = stats.get("tx_count", 0)
    tx_err = stats.get("tx_errors", 0)
    elapsed = time.monotonic() - start

    print(f"\nResults ({elapsed:.1f}s):")
    print(f"  adjfine calls: {adj_n} ({adj_n/elapsed:.0f}/s)")
    print(f"  TX packets:    {tx_n} ({tx_n/elapsed:.0f}/s), {tx_err} errors")

    if triggered:
        print(f"\n  BUG TRIGGERED: igc Tx timestamp timeout")
        print(f"  The igc driver's PTP timestamp register wedged due to")
        print(f"  concurrent adjfine() and hardware TX timestamping.")
        sys.exit(1)
    else:
        print(f"\n  No timeout detected in {duration}s")
        sys.exit(0)


if __name__ == "__main__":
    main()
