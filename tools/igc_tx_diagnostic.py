#!/usr/bin/env python3
"""Diagnostic test for igc TX timestamp timeout root causes.

Tests at varying TX rates to find the threshold where timeouts begin,
both with and without adjfine.  Monitors tx_hwtstamp_timeouts and
tx_hwtstamp_skipped counters via ethtool to distinguish:

  1. Slot exhaustion: skipped count rises (all 4 slots occupied)
  2. Lost interrupt / corrupt capture: timeout count rises without skips
  3. Adjfine race: timeouts only when adjfine is active

Usage:
    sudo python3 igc_tx_diagnostic.py eth1 /dev/ptp0
"""

import ctypes
import ctypes.util
import os
import re
import socket
import subprocess
import sys
import threading
import time


def get_phc_clockid(fd):
    return (~fd << 3) | 3


def get_ethtool_stats(iface):
    """Read TX timestamp counters from ethtool."""
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


def check_dmesg_timeout():
    try:
        r = subprocess.run(
            ["dmesg", "--since", "30 seconds ago"],
            capture_output=True, text=True, timeout=2,
        )
        return r.stdout.count("Tx timestamp timeout")
    except Exception:
        return 0


class AdjfineThread:
    def __init__(self, phc_fd, rate_hz):
        self.clockid = get_phc_clockid(phc_fd)
        self.rate_hz = rate_hz
        self.stop = threading.Event()
        self.count = 0
        self.errors = 0
        self.librt = ctypes.CDLL(ctypes.util.find_library("rt"), use_errno=True)

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
        self.Timex = Timex

    def run(self):
        toggle = False
        while not self.stop.is_set():
            tx = self.Timex()
            tx.modes = 0x0002  # ADJ_FREQUENCY
            tx.freq = 100 if toggle else -100
            toggle = not toggle
            ret = self.librt.clock_adjtime(
                ctypes.c_int32(self.clockid), ctypes.byref(tx))
            if ret < 0:
                self.errors += 1
            else:
                self.count += 1
            if self.rate_hz > 0:
                time.sleep(1.0 / self.rate_hz)


class TxThread:
    def __init__(self, iface, rate_hz):
        self.iface = iface
        self.rate_hz = rate_hz
        self.stop = threading.Event()
        self.count = 0
        self.errors = 0

    def run(self):
        SO_TIMESTAMPING = 37
        flags = (1 << 0) | (1 << 6) | (1 << 11)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPING, flags)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                        self.iface.encode() + b'\0')
        sock.settimeout(0.01)
        dest = ("224.0.0.1", 9999)
        payload = b"diag" * 10

        while not self.stop.is_set():
            try:
                sock.sendto(payload, dest)
                self.count += 1
            except OSError:
                self.errors += 1
            if self.rate_hz > 0 and self.rate_hz < 100000:
                time.sleep(1.0 / self.rate_hz)
            elif self.count % 100 == 0:
                time.sleep(0.0001)  # yield at extreme rates

        sock.close()


def run_test(iface, phc_fd, tx_rate, adjfine_rate, duration):
    """Run one test configuration and return results."""
    subprocess.run(["dmesg", "-C"], timeout=2)

    stats_before = get_ethtool_stats(iface)

    tx = TxThread(iface, tx_rate)
    adj = None
    if adjfine_rate > 0:
        adj = AdjfineThread(phc_fd, adjfine_rate)

    t_tx = threading.Thread(target=tx.run)
    t_tx.start()
    if adj:
        t_adj = threading.Thread(target=adj.run)
        t_adj.start()

    # Monitor
    timeout_detected = False
    timeout_time = None
    for sec in range(duration):
        time.sleep(1)
        n = check_dmesg_timeout()
        if n > 0 and not timeout_detected:
            timeout_detected = True
            timeout_time = sec + 1

    tx.stop.set()
    if adj:
        adj.stop.set()
    t_tx.join(timeout=2)
    if adj:
        t_adj.join(timeout=2)

    stats_after = get_ethtool_stats(iface)

    # Compute deltas
    delta = {}
    for key in stats_after:
        delta[key] = stats_after.get(key, 0) - stats_before.get(key, 0)

    return {
        "tx_rate_target": tx_rate,
        "adjfine_rate": adjfine_rate,
        "duration": duration,
        "tx_actual": tx.count,
        "tx_actual_rate": tx.count / duration,
        "adjfine_actual": adj.count if adj else 0,
        "adjfine_errors": adj.errors if adj else 0,
        "timeout_detected": timeout_detected,
        "timeout_time": timeout_time,
        "timeouts_delta": delta.get("tx_hwtstamp_timeouts", 0),
        "skipped_delta": delta.get("tx_hwtstamp_skipped", 0),
        "stats_delta": delta,
    }


def main():
    if len(sys.argv) < 3:
        print(f"Usage: sudo {sys.argv[0]} <interface> <ptp_device>")
        sys.exit(1)

    iface = sys.argv[1]
    ptp_dev = sys.argv[2]

    if os.geteuid() != 0:
        print("ERROR: must run as root")
        sys.exit(1)

    phc_fd = os.open(ptp_dev, os.O_RDWR)

    # Test matrix: varying TX rates, with and without adjfine
    tests = [
        # (tx_rate_hz, adjfine_rate_hz, duration_s, description)
        (100,    0,      30, "TX 100/s, no adjfine"),
        (1000,   0,      30, "TX 1k/s, no adjfine"),
        (10000,  0,      30, "TX 10k/s, no adjfine"),
        (50000,  0,      30, "TX 50k/s, no adjfine"),
        (100000, 0,      30, "TX 100k/s, no adjfine"),
        (0,      0,      30, "TX max rate, no adjfine"),  # 0 = no sleep
        (100,    1,      30, "TX 100/s + adjfine 1/s"),
        (1000,   1,      30, "TX 1k/s + adjfine 1/s"),
        (10000,  1,      30, "TX 10k/s + adjfine 1/s"),
        (1000,   1000,   30, "TX 1k/s + adjfine 1k/s"),
        (10000,  10000,  30, "TX 10k/s + adjfine 10k/s"),
        (0,      0,      30, "TX max + adjfine max"),  # both max
    ]
    # Fix last test - need special handling for max rates
    tests[-1] = (0, 0, 30, "TX max + adjfine max (both unsleeping)")

    print(f"igc TX Timestamp Diagnostic")
    print(f"Interface: {iface}, PHC: {ptp_dev}")
    print(f"{'='*90}")
    print(f"{'Description':40s} {'TX/s':>8} {'Adj/s':>8} {'Timeout?':>9} "
          f"{'T(s)':>5} {'HW-TO':>6} {'Skip':>6}")
    print(f"{'-'*90}")

    for tx_rate, adj_rate, duration, desc in tests:
        # Clean driver state between tests
        subprocess.run(["dmesg", "-C"], timeout=2)
        time.sleep(2)

        r = run_test(iface, phc_fd, tx_rate, adj_rate, duration)

        to_str = f"{r['timeout_time']}s" if r['timeout_detected'] else "no"
        tt = str(r['timeout_time']) if r['timeout_time'] is not None else ""
        print(f"{desc:40s} {r['tx_actual_rate']:>8.0f} "
              f"{r['adjfine_actual']/duration:>8.0f} "
              f"{to_str:>9} {tt:>5} "
              f"{r['timeouts_delta']:>6} {r['skipped_delta']:>6}")

    os.close(phc_fd)
    print(f"\n{'='*90}")
    print("HW-TO = tx_hwtstamp_timeouts delta (from ethtool -S)")
    print("Skip  = tx_hwtstamp_skipped delta (all 4 slots busy)")
    print("\nIf Skip rises without TO: slot exhaustion at that TX rate")
    print("If TO rises without Skip: lost interrupt or corrupt capture")
    print("If TO only with adjfine: confirms the TIMINCA race")


if __name__ == "__main__":
    main()
