#!/usr/bin/env python3
"""Test whether extra F9T output flushes the kernel GNSS I2C buffer faster.

Temporarily enables high-rate NAV messages on the I2C port to create a
steady byte stream, then runs the read-stall probe to measure delivery
latency.  Restores the original config on exit.

Usage:
    python3 i2c_flush_probe.py /dev/gnss0
"""

import csv
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pyubx2 import UBXReader, UBXMessage

# Extra messages to enable at 1 Hz on I2C (port 0) to flood the bus.
# These are cheap NAV messages that the receiver produces anyway.
FLOOD_MESSAGES = {
    "CFG_MSGOUT_UBX_NAV_CLOCK_I2C": 1,
    "CFG_MSGOUT_UBX_NAV_DOP_I2C": 1,
    "CFG_MSGOUT_UBX_NAV_EOE_I2C": 1,
    "CFG_MSGOUT_UBX_NAV_POSECEF_I2C": 1,
    "CFG_MSGOUT_UBX_NAV_POSLLH_I2C": 1,
    "CFG_MSGOUT_UBX_NAV_STATUS_I2C": 1,
    "CFG_MSGOUT_UBX_NAV_TIMEGPS_I2C": 1,
    "CFG_MSGOUT_UBX_NAV_TIMEUTC_I2C": 1,
    "CFG_MSGOUT_UBX_NAV_VELECEF_I2C": 1,
    "CFG_MSGOUT_UBX_NAV_VELNED_I2C": 1,
    "CFG_MSGOUT_UBX_NAV_COV_I2C": 1,
    "CFG_MSGOUT_UBX_NAV_SIG_I2C": 1,
    "CFG_MSGOUT_UBX_MON_RF_I2C": 1,
    "CFG_MSGOUT_UBX_MON_HW_I2C": 1,
}

# Restore: set all flood messages to 0
RESTORE_MESSAGES = {k: 0 for k in FLOOD_MESSAGES}

MAX_SECONDS = 120
BUF_SIZE = 16384


def send_cfg_valset(fd, config, layer=1):
    """Send a UBX-CFG-VALSET via the GNSS char device."""
    msg = UBXMessage.config_set(layer, 0, list(config.items()))
    raw = msg.serialize()
    os.write(fd, raw)
    # Brief pause for receiver to process
    time.sleep(0.5)


def open_gnss(path):
    """Open kernel GNSS char device read-write."""
    return os.open(path, os.O_RDWR)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} /dev/gnssX", file=sys.stderr)
        sys.exit(1)

    dev = sys.argv[1]
    fd = open_gnss(dev)
    print(f"Opened {dev} (fd={fd})", file=sys.stderr)

    # Enable flood messages
    print(f"Enabling {len(FLOOD_MESSAGES)} extra messages on I2C...", file=sys.stderr)
    send_cfg_valset(fd, FLOOD_MESSAGES)
    print("  Done. Waiting 2s for receiver to start producing...", file=sys.stderr)
    time.sleep(2)
    # Drain any stale data
    try:
        while True:
            import select
            r, _, _ = select.select([fd], [], [], 0.0)
            if not r:
                break
            os.read(fd, BUF_SIZE)
    except Exception:
        pass

    # Run read probe
    out_path = "/tmp/read_stall_flood_probe.csv"
    out_f = open(out_path, "w", newline="")
    writer = csv.writer(out_f)
    writer.writerow(["mono_s", "bytes", "delta_ms"])

    stop = False
    def _stop(sig, frame):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, _stop)

    prev_mono = time.monotonic()
    n_reads = 0
    max_bytes = 0
    max_delta_ms = 0.0
    start = time.monotonic()

    print(f"Reading for {MAX_SECONDS}s, logging to {out_path}...", file=sys.stderr)

    while not stop and (time.monotonic() - start) < MAX_SECONDS:
        data = os.read(fd, BUF_SIZE)
        now = time.monotonic()
        nb = len(data)
        delta_ms = (now - prev_mono) * 1000.0

        writer.writerow([f"{now:.6f}", nb, f"{delta_ms:.3f}"])
        n_reads += 1
        if nb > max_bytes:
            max_bytes = nb
        if delta_ms > max_delta_ms:
            max_delta_ms = delta_ms

        prev_mono = now

        if n_reads % 500 == 0:
            elapsed = now - start
            print(f"  [{elapsed:.0f}s] {n_reads} reads, max_bytes={max_bytes}, "
                  f"max_delta={max_delta_ms:.1f}ms", file=sys.stderr)

    out_f.close()
    elapsed = time.monotonic() - start

    print(f"\nRead phase done: {n_reads} reads in {elapsed:.1f}s", file=sys.stderr)
    print(f"  max bytes: {max_bytes} / {BUF_SIZE}", file=sys.stderr)
    print(f"  max delta: {max_delta_ms:.1f} ms", file=sys.stderr)
    print(f"  reads/s: {n_reads / elapsed:.1f}", file=sys.stderr)

    # Restore original config
    print("\nRestoring original message config...", file=sys.stderr)
    send_cfg_valset(fd, RESTORE_MESSAGES)
    print("  Done.", file=sys.stderr)

    os.close(fd)


if __name__ == "__main__":
    main()
