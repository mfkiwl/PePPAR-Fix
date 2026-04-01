#!/usr/bin/env python3
"""Run a single igc TX timestamp test with clean driver state.

Reloads the igc driver before each test to clear all hardware state.

Usage:
    sudo python3 igc_single_test.py eth1 /dev/ptp0 --tx-rate 100 --adj-rate 1 --duration 30
"""

import argparse
import ctypes
import ctypes.util
import os
import re
import socket
import subprocess
import sys
import threading
import time


def reload_driver(iface):
    """Reload igc driver and wait for clean state."""
    subprocess.run(["ip", "link", "set", iface, "down"],
                   capture_output=True, timeout=5)
    subprocess.run(["rmmod", "igc"], capture_output=True, timeout=5)
    time.sleep(2)
    subprocess.run(["modprobe", "igc"], capture_output=True, timeout=5)
    time.sleep(3)
    subprocess.run(["ip", "link", "set", iface, "up"],
                   capture_output=True, timeout=5)
    time.sleep(2)
    subprocess.run(["dmesg", "-C"], capture_output=True, timeout=2)


def get_ethtool_stats(iface):
    result = subprocess.run(
        ["ethtool", "-S", iface],
        capture_output=True, text=True, timeout=5,
    )
    stats = {}
    for line in result.stdout.splitlines():
        m = re.match(r'\s+(tx_hwtstamp_\w+):\s+(\d+)', line)
        if m:
            stats[m.group(1)] = int(m.group(2))
    return stats


def get_phc_clockid(fd):
    return (~fd << 3) | 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("iface")
    ap.add_argument("ptp_dev")
    ap.add_argument("--tx-rate", type=int, default=100)
    ap.add_argument("--adj-rate", type=int, default=0)
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--no-reload", action="store_true",
                    help="Skip driver reload (use current state)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("ERROR: must run as root")
        sys.exit(1)

    if not args.no_reload:
        print("Reloading igc driver for clean state...")
        reload_driver(args.iface)

    stats_before = get_ethtool_stats(args.iface)
    phc_fd = os.open(args.ptp_dev, os.O_RDWR)

    stop = threading.Event()

    # TX thread
    def tx_loop():
        SO_TIMESTAMPING = 37
        flags = (1 << 0) | (1 << 6) | (1 << 11)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPING, flags)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                        args.iface.encode() + b'\0')
        sock.settimeout(0.01)
        n = 0
        while not stop.is_set():
            try:
                sock.sendto(b"test" * 10, ("224.0.0.1", 9999))
                n += 1
            except OSError:
                pass
            if args.tx_rate > 0 and args.tx_rate < 100000:
                time.sleep(1.0 / args.tx_rate)
            elif n % 100 == 0:
                time.sleep(0.0001)
        sock.close()
        return n

    # Adjfine thread
    adj_count = [0, 0]  # [ok, errors]
    def adj_loop():
        if args.adj_rate <= 0:
            return
        librt = ctypes.CDLL(ctypes.util.find_library("rt"), use_errno=True)

        class Timeval(ctypes.Structure):
            _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]
        class Timex(ctypes.Structure):
            _fields_ = [
                ("modes", ctypes.c_uint), ("offset", ctypes.c_long),
                ("freq", ctypes.c_long), ("maxerror", ctypes.c_long),
                ("esterror", ctypes.c_long), ("status", ctypes.c_int),
                ("constant", ctypes.c_long), ("precision", ctypes.c_long),
                ("tolerance", ctypes.c_long), ("time", Timeval),
                ("tick", ctypes.c_long), ("ppsfreq", ctypes.c_long),
                ("jitter", ctypes.c_long), ("shift", ctypes.c_int),
                ("stabil", ctypes.c_long), ("jitcnt", ctypes.c_long),
                ("calcnt", ctypes.c_long), ("errcnt", ctypes.c_long),
                ("stbcnt", ctypes.c_long), ("tai", ctypes.c_int),
            ]

        clockid = get_phc_clockid(phc_fd)
        toggle = False
        while not stop.is_set():
            tx = Timex()
            tx.modes = 0x0002
            tx.freq = 100 if toggle else -100
            toggle = not toggle
            ret = librt.clock_adjtime(ctypes.c_int32(clockid), ctypes.byref(tx))
            if ret < 0:
                adj_count[1] += 1
            else:
                adj_count[0] += 1
            if args.adj_rate < 100000:
                time.sleep(1.0 / args.adj_rate)

    desc = f"TX {args.tx_rate}/s"
    if args.adj_rate > 0:
        desc += f" + adjfine {args.adj_rate}/s"
    print(f"Test: {desc}, {args.duration}s")

    t_tx = threading.Thread(target=tx_loop)
    t_adj = threading.Thread(target=adj_loop)
    t_tx.start()
    t_adj.start()

    t0 = time.monotonic()
    timeout_time = None
    for sec in range(args.duration):
        time.sleep(1)
        n = subprocess.run(
            ["dmesg"], capture_output=True, text=True, timeout=2
        ).stdout.count("Tx timestamp timeout")
        if n > 0 and timeout_time is None:
            timeout_time = sec + 1

    stop.set()
    t_tx.join(timeout=2)
    t_adj.join(timeout=2)
    os.close(phc_fd)

    stats_after = get_ethtool_stats(args.iface)
    delta = {k: stats_after.get(k, 0) - stats_before.get(k, 0)
             for k in stats_after}

    to = delta.get("tx_hwtstamp_timeouts", 0)
    skip = delta.get("tx_hwtstamp_skipped", 0)

    print(f"  Timeout: {'YES at ' + str(timeout_time) + 's' if timeout_time else 'no'}")
    print(f"  HW timeouts: {to}")
    print(f"  HW skipped:  {skip}")
    print(f"  adjfine: {adj_count[0]} ok, {adj_count[1]} EBUSY/errors")

    if timeout_time:
        sys.exit(1)


if __name__ == "__main__":
    main()
