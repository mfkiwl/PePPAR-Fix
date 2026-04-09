#!/usr/bin/env python3
"""Tests for the TICC boot-recovery state machine.

The state machine in scripts/ticc.py::_SharedTiccPort._wait_for_boot
distinguishes between four conditions and applies up to N DTR resets:

  - "ok"               — TICC is producing valid timestamp lines
  - "cold"             — no bytes at all on the wire (DTR never pulsed)
  - "stuck after sentinel"   — boot sentinel printed but timestamps
                                 never followed
  - "stuck in headers" — endless config dump without ever reaching the
                          boot sentinel (the classic menu-stuck symptom)

These tests use a fake serial port that scripts the bytes returned
from successive readline() calls, so the state machine runs against
known sequences without any real hardware.  The boot-recovery
constants are scaled down to keep test runs fast.

Run: python3 tests/test_ticc_boot_recovery.py
"""

import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))


# Stub the serial module so the import works on hosts that don't have
# pyserial installed (test runners, CI, gt-dev).  We only need the names
# that ticc.py touches at import time and inside the recovery state
# machine.  The actual serial port is replaced wholesale by the
# FakeSerial class below.
def _install_serial_stub():
    if "serial" in sys.modules:
        return
    serial_mod = types.ModuleType("serial")

    class _FakeSerialClass:
        pass

    class SerialException(Exception):
        pass

    serial_mod.Serial = _FakeSerialClass
    serial_mod.SerialException = SerialException
    sys.modules["serial"] = serial_mod


_install_serial_stub()

# Import the class via the module path so we can patch its constants
# without affecting other test runs.
import ticc as ticc_module  # noqa: E402


class FakeClock:
    """Monotonic clock that advances only when explicitly told to.

    The TICC boot-recovery state machine reads ``time.monotonic()``
    on every loop iteration and on every byte arrival.  Tests need
    to model that wall-clock advancement so the time-based detectors
    actually fire.

    ``advance_per_byte_s`` controls how much fake time elapses for
    each readline() call that returned bytes (mimicking the time the
    bytes took to arrive on the wire).
    ``advance_per_silence_s`` is how much fake time elapses for each
    empty (timeout) readline() — must be larger so we can quickly
    cross the silence threshold without thousands of fake calls.
    """

    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeSerial:
    """Scriptable replacement for pyserial.Serial.

    ``script`` is a list of bytes-or-None entries.  Each readline()
    consumes one entry: bytes -> returned as-is; None -> empty bytes
    (a timeout/silence pause).  Each call also advances the fake
    clock so the recovery state machine's timers actually progress.
    """

    def __init__(self, script, clock,
                 advance_per_byte_s=0.05,
                 advance_per_silence_s=0.05):
        self._script = list(script)
        self._cursor = 0
        self._clock = clock
        self._advance_per_byte_s = advance_per_byte_s
        self._advance_per_silence_s = advance_per_silence_s
        self.dtr = True
        self.dtr_low_count = 0
        self.dtr_high_count = 0
        self._closed = False

    def readline(self):
        if self._closed:
            raise OSError("closed")
        if self._cursor >= len(self._script):
            self._clock.sleep(self._advance_per_silence_s)
            return b""
        item = self._script[self._cursor]
        self._cursor += 1
        if item is None:
            self._clock.sleep(self._advance_per_silence_s)
            return b""
        self._clock.sleep(self._advance_per_byte_s)
        return item

    def reset_input_buffer(self):
        pass

    def close(self):
        self._closed = True

    @property
    def fd(self):
        return -1  # not used in tests; only readline()/dtr matter

    def __setattr__(self, name, value):
        # Track DTR pulses so tests can assert that the recovery code
        # actually invoked the reset.
        if name == "dtr":
            object.__setattr__(self, name, value)
            if value:
                object.__setattr__(self, "dtr_high_count",
                                   getattr(self, "dtr_high_count", 0) + 1)
            else:
                object.__setattr__(self, "dtr_low_count",
                                   getattr(self, "dtr_low_count", 0) + 1)
        else:
            object.__setattr__(self, name, value)


def _make_port(script, max_resets=2):
    """Build a _SharedTiccPort whose serial is a FakeSerial scripted
    with the given byte sequence.  Patches the ``_time.monotonic``
    binding inside the ticc module so the recovery state machine
    sees the FakeClock's notion of "now"."""
    clock = FakeClock(start=1000.0)
    fake_serial = FakeSerial(script, clock)

    # Patch the module-level time reference inside ticc.py.  The
    # source uses ``import time as _time`` and then ``_time.monotonic()``
    # everywhere, so we replace _time on the module with our FakeClock.
    ticc_module._time = clock  # type: ignore[attr-defined]

    port = ticc_module._SharedTiccPort.__new__(ticc_module._SharedTiccPort)
    port.port = "/dev/fake-ticc"
    port.baud = 115200
    port.serial = fake_serial
    port.refcount = 0
    port.booted = False

    # Scale all the per-attempt budgets down to a few hundred ms so
    # tests run fast.  The state machine logic is independent of the
    # absolute values — what matters is that the relative ordering
    # (silence < sentinel < header < budget) is preserved.
    port._BOOT_MAX_RESET_ATTEMPTS = max_resets
    port._BOOT_PER_ATTEMPT_BUDGET_S = 5.0
    port._BOOT_SILENCE_THRESHOLD_S = 0.15
    port._BOOT_POST_SENTINEL_THRESHOLD_S = 0.15
    port._BOOT_POST_HEADER_THRESHOLD_S = 0.50

    return port


class TestWaitForBoot(unittest.TestCase):

    def test_warm_path_first_line_is_timestamp(self):
        """The fast path: TICC was already running when we opened the
        port.  First readline returns a timestamp line and we're done.
        DTR must NOT be pulsed."""
        port = _make_port([
            b"123.456789012345 chA\n",
        ])
        port._wait_for_boot()
        self.assertEqual(port.serial.dtr_low_count, 0,
                         "Warm path must not pulse DTR")

    def test_normal_boot_with_sentinel_then_timestamps(self):
        """Cold boot with the full banner: # lines, then # timestamp
        sentinel, then real timestamp lines.  No reset needed."""
        port = _make_port([
            b"# TAPR TICC Timestamping Counter\n",
            b"# Software Version: 20170108.1\n",
            b"# Coarse tick (ps): 100000000\n",
            b"# timestamp (seconds)\n",
            b"1.234567890123 chA\n",
        ])
        port._wait_for_boot()
        self.assertEqual(port.serial.dtr_low_count, 0,
                         "A clean boot must not require a forced reset")

    def test_cold_state_one_reset_then_recovers(self):
        """No bytes at all in the first attempt → cold detection
        fires → DTR reset → second attempt produces timestamps."""
        # First attempt: silence (None entries trigger silence
        # detection by returning empty bytes from readline).
        # After enough silence, the loop returns "cold" and the
        # outer loop forces a DTR reset.  Second attempt: timestamps.
        script = [None] * 10 + [
            b"1.234567890123 chA\n",  # second attempt success
        ]
        port = _make_port(script, max_resets=2)
        port._wait_for_boot()
        self.assertGreaterEqual(
            port.serial.dtr_low_count, 1,
            "Cold detection must have triggered a DTR reset",
        )

    def test_stuck_in_headers_triggers_reset(self):
        """Endless header lines without ever reaching the sentinel —
        the classic ModemManager-corrupted menu-stuck case.  Should
        detect after POST_HEADER_THRESHOLD seconds and reset.

        With advance_per_byte=0.05s and POST_HEADER_THRESHOLD=0.50s,
        ~12 headers are enough to trip the detector.  After the reset,
        attempt 2 reads one more header to seed first_header_at and
        then the recovery timestamp."""
        # Attempt 1: ~13 headers trip the detector and reset.
        attempt1 = [b"# TICC Configuration:\n"] * 13
        # Attempt 2: a header (so cold detection is gated off) followed
        # by the recovery timestamp.
        attempt2 = [b"# Boot recovered\n", b"1.234567890123 chA\n"]
        port = _make_port(attempt1 + attempt2, max_resets=2)
        port._wait_for_boot()
        self.assertGreaterEqual(
            port.serial.dtr_low_count, 1,
            "Stuck-in-headers detection must have triggered a reset",
        )

    def test_stuck_after_sentinel_triggers_reset(self):
        """Sentinel printed but timestamps never followed — pathological
        case where something interfered after the menu wait.  Should
        detect after POST_SENTINEL_THRESHOLD and reset."""
        # Attempt 1: header + sentinel + 4 None entries (silence).
        # Each None advances 0.05 s, so 4 Nones = 0.20 s post-sentinel,
        # which is > the 0.15 s POST_SENTINEL_THRESHOLD → trigger.
        attempt1 = [
            b"# TAPR TICC\n",
            b"# timestamp (seconds)\n",
            None, None, None, None,
        ]
        # Attempt 2: header (gates off cold detection) + recovery
        # timestamp.
        attempt2 = [b"# Boot recovered\n", b"1.234567890123 chA\n"]
        port = _make_port(attempt1 + attempt2, max_resets=2)
        port._wait_for_boot()
        self.assertGreaterEqual(
            port.serial.dtr_low_count, 1,
            "Stuck-after-sentinel detection must have triggered a reset",
        )

    def test_total_failure_raises_after_max_resets(self):
        """If every attempt fails, raise TimeoutError after exhausting
        the configured reset budget.  Each reset must have been
        attempted before the raise."""
        # Always silent — cold detection on every attempt.
        script = [None] * 200
        port = _make_port(script, max_resets=2)
        with self.assertRaises(TimeoutError) as cm:
            port._wait_for_boot()
        self.assertIn("after 2 reset attempts", str(cm.exception))
        self.assertEqual(
            port.serial.dtr_low_count, 2,
            f"Expected exactly 2 DTR resets, got {port.serial.dtr_low_count}",
        )

    def test_timestamps_eventually_after_two_resets(self):
        """Two failed attempts then success on the third — exercises
        the multi-attempt loop fully.  This is the case the *current*
        ocxo TICC #2 falls into: fully wedged in menu mode, requires
        more than one DTR pulse to escape (sometimes the chip wedges
        again on the very next reset until ModemManager goes away)."""
        # 13 headers per stuck attempt is enough to trip POST_HEADER
        # detection (13 × 0.05 s = 0.65 s > 0.50 s threshold).  Then a
        # recovery timestamp at the end.
        attempt1 = [b"# stuck dump line\n"] * 13
        attempt2 = [b"# stuck dump line\n"] * 13
        attempt3 = [b"# Boot recovered\n", b"1.234567890123 chA\n"]
        port = _make_port(attempt1 + attempt2 + attempt3, max_resets=3)
        port._wait_for_boot()
        self.assertEqual(
            port.serial.dtr_low_count, 2,
            f"Expected exactly 2 DTR resets before success, got {port.serial.dtr_low_count}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
