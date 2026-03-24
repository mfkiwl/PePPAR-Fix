"""PTP hardware clock interface via Linux ioctls."""

import array
import ctypes
import ctypes.util
import fcntl
import os
import select
import struct
import time

from peppar_fix.exclusive_io import acquire_device_lock, release_device_lock

# ── PTP ioctl constants (from linux/ptp_clock.h) ─────────────────────── #

PTP_CLK_MAGIC = ord('=')

_IOC_WRITE = 1
_IOC_READ = 2


def _IOC(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (typ << 8) | nr


def _IOR(typ, nr, size):
    return _IOC(_IOC_READ, typ, nr, size)


def _IOW(typ, nr, size):
    return _IOC(_IOC_WRITE, typ, nr, size)


PTP_EXTTS_REQUEST = _IOW(PTP_CLK_MAGIC, 2, 16)
PTP_EXTTS_REQUEST2 = _IOW(PTP_CLK_MAGIC, 11, 16)
PTP_EXTTS_EVENT_SIZE = 32
PTP_CLOCK_GETCAPS = _IOR(PTP_CLK_MAGIC, 1, 80)
PTP_PIN_SETFUNC = _IOW(PTP_CLK_MAGIC, 7, 96)

PTP_ENABLE_FEATURE = (1 << 0)
PTP_RISING_EDGE = (1 << 1)

PTP_PF_NONE = 0
PTP_PF_EXTTS = 1
PTP_PF_PEROUT = 2

ADJ_FREQUENCY = 0x0002
ADJ_SETOFFSET = 0x0100
ADJ_NANO = 0x2000


def _clock_id_from_fd(fd):
    """Encode PTP device fd as clockid_t for clock_adjtime."""
    return (~fd << 3) | 3


class PtpDevice:
    """Low-level interface to a Linux PTP hardware clock."""

    def __init__(self, dev_path="/dev/ptp0"):
        self.path = dev_path
        self._lock_fd, self._lock_path = acquire_device_lock(dev_path)
        try:
            self.fd = os.open(dev_path, os.O_RDWR)
        except Exception:
            release_device_lock(self._lock_fd)
            raise
        self.clock_id = _clock_id_from_fd(self.fd)
        self._libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

    def close(self):
        try:
            os.close(self.fd)
        finally:
            release_device_lock(self._lock_fd)

    def get_caps(self):
        """Query PTP clock capabilities."""
        buf = array.array('b', b'\x00' * 80)
        fcntl.ioctl(self.fd, PTP_CLOCK_GETCAPS, buf, True)
        raw = buf.tobytes()
        max_adj = struct.unpack_from('<i', raw, 0)[0]
        n_alarm = struct.unpack_from('<i', raw, 4)[0]
        n_ext_ts = struct.unpack_from('<i', raw, 8)[0]
        n_per_out = struct.unpack_from('<i', raw, 12)[0]
        pps = struct.unpack_from('<i', raw, 16)[0]
        n_pins = struct.unpack_from('<i', raw, 20)[0]
        return {
            'max_adj': max_adj,
            'n_alarm': n_alarm,
            'n_ext_ts': n_ext_ts,
            'n_per_out': n_per_out,
            'pps': pps,
            'n_pins': n_pins,
        }

    def set_pin_function(self, pin_index, func, channel):
        """Configure an SDP pin (EXTTS, PEROUT, or NONE)."""
        buf = bytearray(96)
        struct.pack_into('<64sIII', buf, 0, b'', pin_index, func, channel)
        fcntl.ioctl(self.fd, PTP_PIN_SETFUNC, bytes(buf))

    def enable_extts(self, channel, rising_edge=True):
        """Enable external timestamp capture on a channel."""
        flags = PTP_ENABLE_FEATURE
        if rising_edge:
            flags |= PTP_RISING_EDGE
        buf = struct.pack('<IIII', channel, flags, 0, 0)
        try:
            fcntl.ioctl(self.fd, PTP_EXTTS_REQUEST2, buf)
        except OSError:
            fcntl.ioctl(self.fd, PTP_EXTTS_REQUEST, buf)

    def disable_extts(self, channel):
        """Disable external timestamp capture."""
        buf = struct.pack('<IIII', channel, 0, 0, 0)
        try:
            fcntl.ioctl(self.fd, PTP_EXTTS_REQUEST2, buf)
        except OSError:
            fcntl.ioctl(self.fd, PTP_EXTTS_REQUEST, buf)

    def read_extts(self, timeout_ms=1500):
        """Read one external timestamp event.

        Returns (sec, nsec, index, recv_mono, queue_remains, parse_age_s) or None.
        """
        r, _, _ = select.select([self.fd], [], [], timeout_ms / 1000.0)
        if not r:
            return None
        data = os.read(self.fd, PTP_EXTTS_EVENT_SIZE)
        recv_mono = time.monotonic()
        if len(data) < 20:
            return None
        sec, nsec, _reserved, index = struct.unpack_from('<qIII', data, 0)
        r_more, _, _ = select.select([self.fd], [], [], 0.0)
        return (sec, nsec, index, recv_mono, bool(r_more), 0.0)

    def adjfine(self, ppb):
        """Adjust PHC frequency by ppb (parts per billion)."""
        freq = int(ppb * 65.536)
        timex_size = 208
        buf = bytearray(timex_size)
        struct.pack_into('<I', buf, 0, ADJ_FREQUENCY)
        struct.pack_into('<q', buf, 16, freq)
        ret = self._libc.clock_adjtime(
            ctypes.c_int32(self.clock_id),
            ctypes.c_char_p(bytes(buf)),
        )
        if ret < 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"clock_adjtime failed: {os.strerror(errno)}")
        return ppb

    def step_time(self, offset_ns):
        """Step the PHC by offset_ns nanoseconds using ADJ_SETOFFSET."""
        sec = int(offset_ns // 1_000_000_000)
        nsec = int(offset_ns % 1_000_000_000)
        if nsec < 0:
            sec -= 1
            nsec += 1_000_000_000
        timex_size = 208
        buf = bytearray(timex_size)
        struct.pack_into('<I', buf, 0, ADJ_SETOFFSET | ADJ_NANO)
        struct.pack_into('<q', buf, 72, sec)
        struct.pack_into('<q', buf, 80, nsec)
        ret = self._libc.clock_adjtime(
            ctypes.c_int32(self.clock_id),
            ctypes.c_char_p(bytes(buf)),
        )
        if ret < 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"clock_adjtime ADJ_SETOFFSET failed: {os.strerror(errno)}")

    # ── PTP_SYS_OFFSET: cross-timestamp PHC vs system clock ───────── #

    _PTP_SYS_OFFSET_SIZE = 832  # 16-byte header + 51 * 16-byte timestamps
    _PTP_SYS_OFFSET = None  # resolved on first call

    def _resolve_sys_offset_ioctl(self):
        """Try PTP_SYS_OFFSET2 then PTP_SYS_OFFSET."""
        PTP_CLK = ord('=')
        _IOC_W = 1
        def iow(nr, sz):
            return (_IOC_W << 30) | (sz << 16) | (PTP_CLK << 8) | nr
        for nr in (14, 5):  # SYS_OFFSET2 then SYS_OFFSET
            ioctl_nr = iow(nr, self._PTP_SYS_OFFSET_SIZE)
            buf = bytearray(self._PTP_SYS_OFFSET_SIZE)
            struct.pack_into('<I', buf, 0, 3)  # n_samples=3
            try:
                fcntl.ioctl(self.fd, ioctl_nr, buf, True)
                self._PTP_SYS_OFFSET = ioctl_nr
                return
            except OSError:
                continue
        raise OSError("PTP_SYS_OFFSET not supported on this device")

    def read_phc_ns(self, n_samples=5):
        """Read PHC time cross-referenced to system clock.

        Uses PTP_SYS_OFFSET to get interleaved sys/PHC/sys timestamps,
        picks the tightest triplet. Returns (phc_ns, sys_ns).
        """
        if self._PTP_SYS_OFFSET is None:
            self._resolve_sys_offset_ioctl()
        buf = bytearray(self._PTP_SYS_OFFSET_SIZE)
        struct.pack_into('<I', buf, 0, n_samples)
        fcntl.ioctl(self.fd, self._PTP_SYS_OFFSET, buf, True)

        def _ts(offset):
            sec = struct.unpack_from('<q', buf, offset)[0]
            nsec = struct.unpack_from('<I', buf, offset + 8)[0]
            return sec * 1_000_000_000 + nsec

        best_span = float('inf')
        best_phc = 0
        best_sys = 0
        for i in range(n_samples):
            base = 16 + i * 32  # each pair is sys(16) + phc(16)
            sys_before = _ts(base)
            phc = _ts(base + 16)
            sys_after = _ts(base + 32) if i < n_samples - 1 else _ts(16 + (2 * n_samples) * 16)
            # Actually interleaved: ts[0]=sys, ts[1]=phc, ts[2]=sys, ts[3]=phc, ..., ts[2n]=sys
            pass

        # Re-parse correctly: timestamps are at 16-byte intervals after 16-byte header
        # Layout: sys0, phc0, sys1, phc1, ..., phcN-1, sysN  (2*n_samples + 1 entries)
        timestamps = []
        for i in range(2 * n_samples + 1):
            timestamps.append(_ts(16 + i * 16))

        for i in range(n_samples):
            sys_before = timestamps[2 * i]
            phc = timestamps[2 * i + 1]
            sys_after = timestamps[2 * i + 2]
            span = sys_after - sys_before
            if span < best_span:
                best_span = span
                best_phc = phc
                best_sys = (sys_before + sys_after) // 2

        return best_phc, best_sys

    def set_phc_ns(self, time_ns):
        """Set the PHC to an absolute time in nanoseconds."""
        sec = int(time_ns // 1_000_000_000)
        nsec = int(time_ns % 1_000_000_000)

        class Timespec(ctypes.Structure):
            _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

        ts = Timespec(sec, nsec)
        ret = self._libc.clock_settime(
            ctypes.c_int32(self.clock_id), ctypes.byref(ts))
        if ret != 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"clock_settime failed: {os.strerror(errno)}")

    def step_to(self, target_ns, target_error_ns=5000, max_time_ms=500,
                mean_compensation_ns=0):
        """Step the PHC to target_ns, retrying within a time budget.

        Each clock_settime overwrites the PHC — there is no keeping the
        best. We either meet the target and stop, or try again (the
        previous result is gone). On timeout, we're stuck with the last
        attempt.

        The residual is measured by reading back the PHC and subtracting
        the elapsed time since the set (measured via CLOCK_MONOTONIC),
        so PHC drift between set and readback doesn't inflate the error.

        Returns (residual_ns, attempts, met_target).
        """
        deadline = time.monotonic() + max_time_ms / 1000.0
        attempts = 0

        while True:
            attempts += 1
            aim_ns = target_ns - mean_compensation_ns
            mono_before = time.monotonic_ns()
            self.set_phc_ns(aim_ns)
            phc_after, _ = self.read_phc_ns()
            mono_after = time.monotonic_ns()
            elapsed_ns = mono_after - mono_before
            residual_ns = phc_after - (target_ns + elapsed_ns)

            if abs(residual_ns) < target_error_ns:
                return residual_ns, attempts, True

            if time.monotonic() >= deadline:
                return residual_ns, attempts, False
