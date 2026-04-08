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
import termios
import time as _time
import atexit

import serial

from peppar_fix.event_time import (
    TiccEvent,
    estimate_correlation_confidence,
    estimator_sample_weight,
)

from peppar_fix.timebase_estimator import TimebaseRelationEstimator

# Integer part DOT 11-or-12 fractional digits whitespace ch followed by A or B.
_LINE_RE = re.compile(r"^(\d+)\.(\d{11,12})\s+(ch[AB])$")

# Boot sentinel: the TICC prints this line just before starting timestamp output.
_BOOT_SENTINEL = "# timestamp"

_shared_ticc_ports: dict[tuple[str, int], "_SharedTiccPort"] = {}


class _SharedTiccPort:
    def __init__(self, port: str, baud: int) -> None:
        self.port = port
        self.baud = baud
        self.serial: serial.Serial | None = None
        self.refcount = 0
        self.booted = False

    def _open_serial(self) -> serial.Serial:
        # dsrdtr=False, rtscts=False: tell pyserial not to manage
        # DTR/RTS for hardware flow control.
        #
        # HUPCL clear: prevent the kernel from dropping DTR when the
        # last fd closes.  Without this, closing the port drops DTR,
        # and the next open raises it — the rising edge triggers the
        # Arduino Mega 2560's auto-reset via the DTR-RESET capacitor,
        # rebooting the TICC (~10s of lost data).  With HUPCL cleared,
        # DTR stays asserted across close/reopen, so subsequent opens
        # (same or different process) don't trigger a reboot.
        # See CLAUDE.md "TICC resets on serial open".
        #
        # exclusive=True: kernel-enforced exclusive open (TIOCEXCL).
        # Prevents other processes from opening the same tty.
        # Automatically released when the fd is closed, even on
        # crash or SIGKILL — no stale lock files to clean up.
        try:
            ser = serial.Serial(
                self.port,
                self.baud,
                timeout=2.0,
                dsrdtr=False,
                rtscts=False,
                exclusive=True,
            )
        except serial.SerialException:
            ser = serial.Serial(
                self.port,
                self.baud,
                timeout=2.0,
                dsrdtr=False,
                rtscts=False,
                exclusive=False,
            )
        # Clear HUPCL so DTR stays asserted after close.
        attrs = termios.tcgetattr(ser.fd)
        attrs[2] &= ~termios.HUPCL  # cflag
        termios.tcsetattr(ser.fd, termios.TCSANOW, attrs)
        return ser

    def acquire(self, wait_for_boot: bool) -> None:
        if self.serial is None:
            self.serial = self._open_serial()
            self.serial.reset_input_buffer()
            self.booted = False
        self.refcount += 1
        if wait_for_boot and not self.booted:
            self._wait_for_boot()
            self.booted = True

    def release(self) -> None:
        if self.refcount > 0:
            self.refcount -= 1

    def _force_dtr_reset(self) -> None:
        """Force the Arduino to reboot by pulsing DTR low-then-high.

        Needed when the TICC is in the cold state where DTR is already
        asserted (e.g. after a ModemManager probe at boot), so the
        normal open doesn't produce the rising edge that triggers the
        Arduino's auto-reset capacitor.
        """
        assert self.serial is not None
        try:
            self.serial.dtr = False
            _time.sleep(0.5)
            self.serial.dtr = True
        except (serial.SerialException, OSError):
            pass

    def _wait_for_boot(self) -> None:
        """Wait for TICC to be ready (booting or already running).

        The TICC (Arduino Mega) boot sequence after DTR reset:

          1. Prints the TAPR banner (# lines).
          2. Prints the loaded configuration (more # lines).
          3. Waits ~5 s for any keystroke.  *If a key arrives* the
             firmware drops into an interactive setup menu and waits
             forever for menu commands.
          4. If no key arrives within 5 s, the firmware exits the
             menu wait and begins normal timestamp output.

        Critically, **we do NOT send any keystrokes during the boot
        wait**.  Earlier versions of this code sent ``\\r`` "to skip
        the menu", but the TICC firmware interprets that as a key
        press and *enters* the menu, leaving the chip stuck waiting
        for a menu command instead of producing timestamps.  Empty
        silence is what makes the menu wait time out.

        Cold-start case (the reason this method ever needs to do
        anything): on a freshly-booted host where ModemManager (or
        any other prober) has already touched the ttyACM device,
        DTR is left asserted and our subsequent HUPCL-clearing open
        keeps it asserted, so no rising edge ever fires the
        Arduino's auto-reset capacitor.  The TICC sits in pre-boot
        state and never speaks.  We detect this — no timestamp line
        within ``cold_threshold_s`` seconds — and force a DTR
        low-then-high pulse, after which the boot sequence runs
        normally.

        Warm case: if the TICC is already running (typical on a
        long-uptime host), the first line read is usually a valid
        timestamp and we return immediately without resetting.

        Raises TimeoutError if no timestamp arrives even after a
        forced reset.
        """
        assert self.serial is not None
        cold_threshold_s = 3.0
        # One forced-reset attempt is allowed.  After a reset the
        # Arduino takes ~10 s of banner + 5 s of menu wait + ~1 s
        # of settle before timestamps start, so the post-reset
        # budget needs to be at least ~18 s.
        budget_s = 25.0
        deadline = _time.monotonic() + budget_s
        cold_kick_at = _time.monotonic() + cold_threshold_s
        seen_header = False
        seen_sentinel = False
        forced_reset = False
        while _time.monotonic() < deadline:
            # Cold-state detection: if no *timestamp line* has shown
            # up after cold_threshold_s and we haven't reset yet,
            # force one.  We trigger the reset on lack-of-timestamps
            # rather than lack-of-headers because the TICC can also
            # be stuck in the menu (header-only state) if any code
            # path has sent a stray byte to it.
            if (not forced_reset
                    and _time.monotonic() >= cold_kick_at):
                self._force_dtr_reset()
                forced_reset = True
                # Extend the deadline so we don't time out before the
                # banner finishes printing after the forced reset.
                deadline = max(deadline, _time.monotonic() + 20.0)
                # Drain anything stale that may have arrived during
                # the reset pulse.
                try:
                    self.serial.reset_input_buffer()
                except (serial.SerialException, OSError):
                    pass
                continue
            try:
                raw = self.serial.readline()
            except (serial.SerialException, OSError):
                self.serial.close()
                _time.sleep(1)
                try:
                    self.serial = self._open_serial()
                    self.serial.reset_input_buffer()
                except (serial.SerialException, OSError):
                    _time.sleep(1)
                continue
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            # Valid timestamp line — TICC is in steady-state output.
            if _LINE_RE.match(line):
                break
            if line.startswith("#"):
                seen_header = True
                if _BOOT_SENTINEL in line:
                    seen_sentinel = True
            # Note: we deliberately do NOT write anything to the
            # serial port here.  Any byte we send during the boot
            # menu wait would push the firmware into interactive
            # setup mode and break the boot.
        else:
            raise TimeoutError(
                f"TICC on {self.port} did not produce data within {budget_s:.0f}s "
                f"(seen_header={seen_header}, seen_sentinel={seen_sentinel}, "
                f"forced_reset={forced_reset})"
            )

    def close(self) -> None:
        if self.serial is not None:
            try:
                self.serial.close()
            finally:
                self.serial = None
                self.booted = False


def _get_shared_port(port: str, baud: int) -> _SharedTiccPort:
    key = (port, baud)
    if key not in _shared_ticc_ports:
        _shared_ticc_ports[key] = _SharedTiccPort(port, baud)
    return _shared_ticc_ports[key]


def _close_all_shared_ports() -> None:
    for port in list(_shared_ticc_ports.values()):
        port.close()


atexit.register(_close_all_shared_ports)


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
        self._recv_estimator = TimebaseRelationEstimator(
            min_sigma_s=0.05,
            sigma_scale=4.0,
        )
        self._shared_port = _get_shared_port(self.port, self.baud)

    def __enter__(self) -> "Ticc":
        # Exclusive access is enforced by the serial port's TIOCEXCL
        # (exclusive=True in _open_serial).  No flock needed — the
        # kernel releases the exclusion automatically when the fd
        # closes, even on crash or SIGKILL.
        self._shared_port.acquire(self.wait_for_boot)
        self._ser = self._shared_port.serial
        return self

    def __exit__(self, *_) -> None:
        self._shared_port.release()

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
