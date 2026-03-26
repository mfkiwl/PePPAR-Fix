#!/usr/bin/env python3
"""Probe for stalls at the read() system call level on a GNSS device.

Opens the device with O_RDONLY (no DTR toggle for ttyACM), reads into a
large buffer, and logs a CSV row for every read() completion:
  monotonic timestamp, bytes returned, wall-clock delta since last read.

Usage:
    python3 read_stall_probe.py /dev/gnss0          # kernel GNSS char
    python3 read_stall_probe.py /dev/ttyACM0 115200  # USB-CDC ACM
"""

import csv
import os
import signal
import sys
import termios
import time

INITIAL_BUF = 16384   # 16 KB — should drain any kernel queue in one read
MAX_SECONDS = 120


def open_device(path, baud=None):
    """Open device with O_RDONLY + O_NONBLOCK to avoid DTR side-effects,
    then clear O_NONBLOCK so read() blocks."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)

    # If it's a tty, configure raw mode + baud
    if os.isatty(fd):
        attrs = termios.tcgetattr(fd)
        # cfmakeraw equivalent
        attrs[0] = 0        # iflag
        attrs[1] = 0        # oflag
        attrs[2] &= ~termios.CSIZE
        attrs[2] |= termios.CS8
        attrs[2] &= ~termios.PARENB
        attrs[2] |= termios.CREAD | termios.CLOCAL
        attrs[3] = 0        # lflag
        attrs[4] = termios.B115200  # default ispeed
        attrs[5] = termios.B115200  # default ospeed
        if baud:
            baud_map = {
                9600: termios.B9600, 38400: termios.B38400,
                115200: termios.B115200, 230400: termios.B230400,
                460800: termios.B460800,
            }
            bconst = baud_map.get(baud, termios.B115200)
            attrs[4] = bconst
            attrs[5] = bconst
        # VMIN=1, VTIME=0 → block until at least 1 byte
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

    # Clear O_NONBLOCK so reads block
    flags = os.get_blocking(fd) if hasattr(os, 'get_blocking') else None
    import fcntl
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl & ~os.O_NONBLOCK)

    return fd


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} /dev/gnssX [baud]", file=sys.stderr)
        sys.exit(1)

    dev = sys.argv[1]
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else None

    fd = open_device(dev, baud)
    print(f"Opened {dev} (fd={fd}, baud={baud})", file=sys.stderr)

    buf_size = INITIAL_BUF
    out_path = "/tmp/read_stall_probe.csv"
    out_f = open(out_path, "w", newline="")
    writer = csv.writer(out_f)
    writer.writerow(["mono_s", "bytes", "delta_ms", "buf_full"])

    stop = False
    def _stop(sig, frame):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, _stop)

    prev_mono = time.monotonic()
    n_reads = 0
    n_full = 0
    start = time.monotonic()
    max_bytes = 0
    max_delta_ms = 0.0

    print(f"Logging to {out_path}, buf_size={buf_size}, max {MAX_SECONDS}s", file=sys.stderr)

    while not stop and (time.monotonic() - start) < MAX_SECONDS:
        data = os.read(fd, buf_size)
        now = time.monotonic()
        nb = len(data)
        delta_ms = (now - prev_mono) * 1000.0
        full = (nb >= buf_size)

        writer.writerow([f"{now:.6f}", nb, f"{delta_ms:.3f}", int(full)])
        n_reads += 1
        if full:
            n_full += 1
        if nb > max_bytes:
            max_bytes = nb
        if delta_ms > max_delta_ms:
            max_delta_ms = delta_ms

        prev_mono = now

        if n_reads % 500 == 0:
            elapsed = now - start
            print(f"  [{elapsed:.0f}s] {n_reads} reads, max_bytes={max_bytes}, "
                  f"max_delta={max_delta_ms:.1f}ms, buf_full={n_full}",
                  file=sys.stderr)

    out_f.close()
    os.close(fd)

    elapsed = time.monotonic() - start
    print(f"\nDone: {n_reads} reads in {elapsed:.1f}s", file=sys.stderr)
    print(f"  max bytes in one read: {max_bytes} / {buf_size}", file=sys.stderr)
    print(f"  max delta between reads: {max_delta_ms:.1f} ms", file=sys.stderr)
    print(f"  buffer-full reads: {n_full}", file=sys.stderr)
    if n_full:
        print(f"  WARNING: {n_full} reads filled the buffer — re-run with larger buffer",
              file=sys.stderr)


if __name__ == "__main__":
    main()
