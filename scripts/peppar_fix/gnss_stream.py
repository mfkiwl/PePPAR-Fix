#!/usr/bin/env python3
"""gnss_stream.py — Unified GNSS device opener for peppar-fix.

Supports both:
  - /dev/gnss* (kernel GNSS char device, E810 onboard)
  - /dev/ttyACM*, /dev/gnss-top (serial ports, F9T EVK)

Returns a file-like object that pyubx2's UBXReader can wrap.
"""

import logging
import os
import select
import time
from collections import deque

from peppar_fix.exclusive_io import acquire_device_lock, release_device_lock

log = logging.getLogger(__name__)


class KernelGnssStream:
    """File-like wrapper around a kernel GNSS char device (/dev/gnssN).

    pyubx2's UBXReader expects a stream where ``read(n)`` returns up to *n*
    bytes of UBX data.  The kernel GNSS device delivers raw I2C chunks that
    may contain UBX, NMEA, and RTCM mixed together.

    ``read(n)`` behaves more like a serial port and blocks until enough bytes
    are available, reassembling complete UBX frames so pyubx2 never sees
    partial headers.
    """

    def __init__(self, path):
        self._lock_fd, self._lock_path = acquire_device_lock(path)
        try:
            self._fd = os.open(path, os.O_RDWR)
        except OSError:
            release_device_lock(self._lock_fd)
            raise
        self._buf = bytearray()      # raw bytes from device
        self._out = bytearray()      # reassembled UBX frames for pyubx2
        self._read_chunk = 4096
        self._pending_packet_sizes = deque()
        self._pending_packet_timestamps = deque()
        self._last_packet_queue_remains = None
        self._last_fill_mono = None

    def _fill_raw(self, want=1):
        """Read from kernel GNSS device and buffer for packet parsing.

        The fd is opened in blocking mode, so os.read() blocks until the
        driver delivers data.  With the patched ice driver, this is a
        15-byte I2C chunk every ~20 ms.  With the stock driver, it's a
        4 KB page every ~2 seconds.  Either way, os.read() does the
        blocking — no select() or sleep needed.
        """
        while len(self._buf) < want:
            chunk = os.read(self._fd, max(self._read_chunk, want - len(self._buf)))
            if chunk:
                self._last_fill_mono = time.monotonic()
                self._buf.extend(chunk)

    def _fill_ubx(self, want=1):
        """Populate the output buffer with complete UBX packets only.

        The F9T stream may contain a mix of UBX, NMEA, and RTCM bytes. pyubx2
        is happiest when handed a clean byte stream for the protocol we
        actually care about here: UBX. Scan the raw byte stream for UBX sync
        words, discard non-UBX bytes, and enqueue only complete UBX frames.
        """
        while len(self._out) < want:
            while True:
                sync_at = self._buf.find(b"\xb5\x62")
                if sync_at >= 0:
                    if sync_at > 0:
                        del self._buf[:sync_at]
                    break

                # No sync found. Keep a trailing 0xB5 so split sync words
                # across reads still match on the next chunk.
                if self._buf[-1:] == b"\xb5":
                    del self._buf[:-1]
                else:
                    self._buf.clear()
                # Need at least 2 bytes to find the sync word.
                self._fill_raw(2)

            self._fill_raw(6)
            payload_len = self._buf[4] | (self._buf[5] << 8)
            packet_len = 8 + payload_len
            self._fill_raw(packet_len)

            self._out.extend(self._buf[:packet_len])
            self._pending_packet_sizes.append(packet_len)
            self._pending_packet_timestamps.append(self._last_fill_mono)
            del self._buf[:packet_len]

    def read(self, size=-1):
        if size is None or size < 0:
            self._fill_ubx(1)
            data = bytes(self._out)
            self._out.clear()
            self._consume_packet_bytes(len(data))
            return data
        # pyubx2 calls read(N) expecting 1..N bytes back (like a socket).
        # Ensure at least one complete UBX frame is available, then return
        # up to *size* bytes from whatever is buffered.  Do NOT loop to
        # accumulate *size* bytes — that forces reading many small frames
        # to satisfy a large read (e.g. 1500-byte RAWX payload), stalling
        # the pipeline for seconds on 15-byte I2C streaming delivery.
        if not self._out:
            self._fill_ubx(1)
        data = bytes(self._out[:size])
        del self._out[:size]
        self._consume_packet_bytes(len(data))
        return data

    def read_raw(self, size):
        """Read raw bytes without UBX frame reassembly.

        Blocks until at least 1 byte is available, returns up to size bytes.
        Used by wait_ack to scan for ACK patterns without the cost of full
        UBX deserialization.
        """
        if not self._buf:
            chunk = os.read(self._fd, size)
            return bytes(chunk)
        # Drain from existing buffer first
        n = min(size, len(self._buf))
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def _consume_packet_bytes(self, nbytes):
        """Advance packet accounting for bytes handed to pyubx2."""
        while nbytes > 0 and self._pending_packet_sizes:
            head = self._pending_packet_sizes[0]
            if nbytes < head:
                self._pending_packet_sizes[0] = head - nbytes
                nbytes = 0
            else:
                nbytes -= head
                self._pending_packet_sizes.popleft()
                self._pending_packet_timestamps.popleft()

    def pop_packet_metadata(self):
        """Return (recv_mono, queue_remains) for the earliest unconsumed packet."""
        if self._pending_packet_timestamps:
            mono = self._pending_packet_timestamps[0]
            queue_remains = len(self._pending_packet_sizes) > 1
            return mono, queue_remains
        return None, None

    def pop_packet_timestamp(self):
        """Return recv_mono for the earliest unconsumed packet (compat)."""
        if self._pending_packet_timestamps:
            return self._pending_packet_timestamps[0]
        return None

    def write(self, data):
        return os.write(self._fd, data)

    @property
    def in_waiting(self):
        return len(self._out) + len(self._buf)

    def discard_input(self, idle_s=0.5, max_drain_s=5.0):
        """Discard queued input on a kernel GNSS device.

        Switches the fd to nonblocking mode, drains reads until the device
        stays idle for *idle_s* seconds, then switches back to blocking.

        Returns the number of bytes discarded, or 0 on error.
        """
        import fcntl
        fl = fcntl.fcntl(self._fd, fcntl.F_GETFL)
        fcntl.fcntl(self._fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        discarded = 0
        last_data = time.monotonic()
        deadline = time.monotonic() + max_drain_s
        try:
            while time.monotonic() < deadline:
                try:
                    data = os.read(self._fd, 4096)
                    if data:
                        discarded += len(data)
                        last_data = time.monotonic()
                except BlockingIOError:
                    pass
                if time.monotonic() - last_data > idle_s:
                    break
                time.sleep(0.01)
        finally:
            fcntl.fcntl(self._fd, fcntl.F_SETFL, fl)
            self._buf.clear()
            self._out.clear()
            self._pending_packet_sizes.clear()
            self._pending_packet_timestamps.clear()
        return discarded

    def close(self):
        try:
            os.close(self._fd)
        except OSError:
            pass
        release_device_lock(self._lock_fd)


def _open_serial_exclusive(device, baud):
    """Open a serial port with advisory lock and optional TIOCEXCL."""
    lock_fd, _lock_path = acquire_device_lock(device)
    import serial
    try:
        try:
            ser = serial.Serial(
                device, baudrate=baud, timeout=1,
                dsrdtr=False, rtscts=False,
                exclusive=True,
            )
        except (serial.SerialException, ValueError):
            # TIOCEXCL not supported on some cdc_acm drivers; fall back
            log.debug("TIOCEXCL failed on %s — opening non-exclusive", device)
            ser = serial.Serial(
                device, baudrate=baud, timeout=1,
                dsrdtr=False, rtscts=False,
                exclusive=False,
            )
    except Exception:
        release_device_lock(lock_fd)
        raise

    _lock_released = [False]  # mutable so closure can write it
    _original_close = ser.close
    def close_with_lock():
        _original_close()
        if not _lock_released[0]:
            _lock_released[0] = True
            release_device_lock(lock_fd)

    ser.close = close_with_lock
    ser._peppar_lock_fd = lock_fd
    return ser


def open_gnss(device, baud=9600):
    """Open a GNSS device, returning (stream, device_type).

    device_type is "gnss" for kernel GNSS char devices, "serial" otherwise.
    """
    base = os.path.basename(device)
    if base.startswith("gnss") and base[4:].isdigit():
        log.info("Opening %s as kernel GNSS char device", device)
        return KernelGnssStream(device), "gnss"
    else:
        ser = _open_serial_exclusive(device, baud)
        return ser, "serial"
