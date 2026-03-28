"""
ticc.py — Serial reader for the TAPR TICC time interval counter.

The TICC outputs one line per PPS edge:
    <seconds_since_boot>  chA|chB
e.g.
    402.342588195696 chA
    402.342588174417 chB

Timestamps have 11–12 decimal places.  Older firmware used 12 (1 ps LSB);
newer firmware uses 11 (10 ps LSB).  The counter's single-shot noise is ~60 ps,
so the last displayed digit is noise in either case.
Lines starting with '#' are comments (boot-time header); they are skipped.

Output ordering: the TICC outputs whichever channel's timestamp is ready
first.  When both channels fire close together (e.g. same PPS source split
to both inputs), chB for second N may appear before chA for second N-1.
This is documented TICC behavior, not a parsing error.

Boot behavior (Arduino Mega auto-reset):
  Opening the serial port on an Arduino Mega toggles DTR, which resets
  the microcontroller via a hardware capacitor.  The TICC then:
    1. Prints a config header (# lines)
    2. Waits ~5 seconds for config menu input ("# ....")
    3. Prints "# timestamp (seconds)"  ← sentinel
    4. Begins outputting timestamp lines at ref_sec=1

  The OS serial buffer may contain stale timestamps from before the
  reboot.  These have large ref_sec values and different ref_ps ranges
  from the fresh post-boot data.

  Callers should either:
    (a) Use wait_for_boot=True (default) to automatically wait for the
        boot sentinel and discard stale data.  Takes ~10 seconds.
    (b) Use wait_for_boot=False when the port is already open and the
        TICC is known to be running (e.g. between calibration runs).

Precision notes:
  - Integer and fractional parts are parsed separately to avoid float64
    precision loss.  float64 has ~15-16 significant digits total; a
    6-digit integer part leaves only ~9 decimal digits, losing ps
    resolution after ~28 hours of TICC uptime.
  - ref_sec and ref_ps are returned as Python ints (arbitrary precision).
    Convert to float only at the final analysis stage.
"""

from __future__ import annotations

import re
import time as _time

import serial

from peppar_fix.event_time import (
    TiccEvent,
    estimate_correlation_confidence,
    estimator_sample_weight,
)
from peppar_fix.exclusive_io import acquire_device_lock, release_device_lock
from peppar_fix.timebase_estimator import TimebaseRelationEstimator

# Integer part DOT 11-or-12 fractional digits whitespace ch followed by A or B.
_LINE_RE = re.compile(r"^(\d+)\.(\d{11,12})\s+(ch[AB])$")

# Boot sentinel: the TICC prints this line just before starting timestamp output.
_BOOT_SENTINEL = "# timestamp"


class Ticc:
    """
    Context manager that opens the TICC serial port and yields
    (channel, ref_sec, ref_ps) tuples as edges arrive.

    channel : 'chA' or 'chB'
    ref_sec : int, integer seconds since TICC boot (arbitrary epoch)
    ref_ps  : int, picoseconds 0..999_999_999_999
              11-digit firmware → 10 ps resolution (last digit = 0)
              12-digit firmware →  1 ps resolution
    """

    def __init__(self, port: str, baud: int = 115200,
                 wait_for_boot: bool = True):
        self.port = port
        self.baud = baud
        self.wait_for_boot = wait_for_boot
        self._ser: serial.Serial | None = None
        self._lock_fd: int | None = None
        self._recv_estimator = TimebaseRelationEstimator(
            min_sigma_s=0.05,
            sigma_scale=4.0,
        )

    def _open_serial(self) -> serial.Serial:
        # dsrdtr=False, rtscts=False: prevent DTR toggle that would
        # reboot the Arduino Mega 2560 (see CLAUDE.md "TICC resets
        # on serial open").
        try:
            return serial.Serial(
                self.port,
                self.baud,
                timeout=2.0,
                dsrdtr=False,
                rtscts=False,
                exclusive=True,
            )
        except serial.SerialException:
            return serial.Serial(
                self.port,
                self.baud,
                timeout=2.0,
                dsrdtr=False,
                rtscts=False,
                exclusive=False,
            )

    def __enter__(self) -> "Ticc":
        self._lock_fd, _lock_path = acquire_device_lock(self.port)
        try:
            self._ser = self._open_serial()
            self._ser.reset_input_buffer()

            if self.wait_for_boot:
                # Opening the port triggers Arduino auto-reset via DTR
                # capacitor.  The TICC reboots, prints a config header,
                # waits ~5s for menu input, then prints the sentinel line
                # and starts data.  Total boot time: ~8-10 seconds.
                #
                # Read through boot output until we see the sentinel,
                # then the first valid timestamp.  If the port becomes
                # invalid during reboot (USB re-enumeration), close,
                # wait, and reopen.
                deadline = _time.monotonic() + 20
                seen_sentinel = False
                while _time.monotonic() < deadline:
                    try:
                        raw = self._ser.readline()
                    except (serial.SerialException, OSError):
                        try:
                            self._ser.close()
                        except Exception:
                            pass
                        _time.sleep(1)
                        try:
                            self._ser = self._open_serial()
                            self._ser.reset_input_buffer()
                        except (serial.SerialException, OSError):
                            _time.sleep(1)
                        continue
                    line = raw.decode(errors="replace").strip()
                    if _BOOT_SENTINEL in line:
                        seen_sentinel = True
                        continue
                    if seen_sentinel and _LINE_RE.match(line):
                        break  # first fresh timestamp — ready
        except Exception:
            # Release lock if ANYTHING in __enter__ fails — serial open,
            # boot wait, USB re-enumeration.  Without this, a failed
            # __enter__ leaks the flock and the reconnect loop blocks
            # itself on the next attempt.
            release_device_lock(self._lock_fd)
            self._lock_fd = None
            raise

        return self

    def __exit__(self, *_) -> None:
        try:
            if self._ser:
                self._ser.close()
        finally:
            release_device_lock(self._lock_fd)
            self._lock_fd = None

    def __iter__(self):
        """Yield (channel, ref_sec, ref_ps) for each valid edge line."""
        for raw in self._ser:
            line = raw.decode(errors="replace").strip()
            m = _LINE_RE.match(line)
            if not m:
                continue
            ref_sec = int(m.group(1))
            ref_ps  = int(m.group(2).ljust(12, '0'))   # normalise 11→12 digits
            yield m.group(3), ref_sec, ref_ps

    def iter_events(self):
        """Yield TiccEvent records with host receive timestamps."""
        for raw in self._ser:
            recv_mono = _time.monotonic()
            queue_remains = bool(getattr(self._ser, "in_waiting", 0))
            line = raw.decode(errors="replace").strip()
            m = _LINE_RE.match(line)
            if not m:
                continue
            ref_sec = int(m.group(1))
            ref_ps = int(m.group(2).ljust(12, '0'))
            base_confidence = estimate_correlation_confidence(
                queue_remains=queue_remains,
                parse_age_s=0.0,
            )
            source_time_s = ref_sec + (ref_ps * 1e-12)
            estimator_sample = self._recv_estimator.update(
                source_time_s,
                recv_mono,
                sample_weight=estimator_sample_weight(
                    queue_remains=queue_remains,
                    base_confidence=base_confidence,
                ),
            )
            yield TiccEvent(
                channel=m.group(3),
                ref_sec=ref_sec,
                ref_ps=ref_ps,
                recv_mono=recv_mono,
                queue_remains=queue_remains,
                parse_age_s=0.0,
                correlation_confidence=max(
                    0.05,
                    min(1.0, base_confidence * estimator_sample["confidence"]),
                ),
                estimator_residual_s=estimator_sample["residual_s"],
            )
