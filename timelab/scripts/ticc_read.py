#!/usr/bin/env python3
"""ticc_read.py — Read TICC without resetting it.

Opens serial port with dsrdtr=False to prevent DTR toggle (Arduino reset).
Outputs TICC measurement lines to stdout as CSV.

Usage:
    python3 ticc_read.py /dev/ticc1 --duration 300
    python3 ticc_read.py /dev/ticc1 --duration 300 --csv  # add header + host timestamp
"""

import argparse
import re
import sys
import time
from datetime import datetime, timezone

import serial

LINE_RE = re.compile(r"^(\d+)\.(\d{11,12})\s+(ch[AB])$")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", help="TICC serial device (e.g. /dev/ticc1)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--duration", type=int, default=300, help="Seconds to capture")
    ap.add_argument("--csv", action="store_true", help="Output with CSV header + host timestamp")
    args = ap.parse_args()

    ser = serial.Serial(
        args.port, args.baud, timeout=2.0,
        dsrdtr=False, rtscts=False,
    )
    ser.reset_input_buffer()

    if args.csv:
        print("host_time,channel,ref_sec,ref_ps")

    start = time.time()
    try:
        for raw in ser:
            if time.time() - start > args.duration:
                break
            line = raw.decode(errors="replace").strip()
            m = LINE_RE.match(line)
            if not m:
                continue
            ref_sec = int(m.group(1))
            ref_ps = int(m.group(2).ljust(12, '0'))
            ch = m.group(3)
            if args.csv:
                ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')
                print(f"{ts},{ch},{ref_sec},{ref_ps}")
            else:
                print(line)
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()


if __name__ == "__main__":
    main()
