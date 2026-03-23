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
