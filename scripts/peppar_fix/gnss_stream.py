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
    """Serial-like wrapper for kernel GNSS char devices.

    The kernel device can return short reads. pyubx2 expects a stream whose
    ``read(n)`` behaves more like a serial port and blocks until enough bytes
    arrive, so coalesce reads here.
    """

    def __init__(self, path):
        self._lock_fd, self._lock_path = acquire_device_lock(path)
        try:
            self._fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except Exception:
            release_device_lock(self._lock_fd)
            raise
        self.name = path
        self._buf = bytearray()
        self._out = bytearray()
        self._read_chunk = 4096
        self._pending_packet_sizes = deque()
        self._pending_packet_timestamps = deque()
        self._last_packet_timestamp = None
        self._last_packet_queue_remains = None
        self._last_fill_mono = None

    def _fill_raw(self, want=1):
        """Read a larger kernel chunk and retain leftovers for packet parsing.

        The kernel GNSS char device can return packetized data. pyubx2 often
        reads in 1-2 byte increments while scanning headers, so reading
        directly from the device for every tiny request can discard the rest
        of the current kernel packet. Buffer chunks here and serve pyubx2 out
        of the retained bytes instead.
        """
        while len(self._buf) < want:
            r, _, _ = select.select([self._fd], [], [], 0.5)
            if not r:
                continue
            try:
                chunk = os.read(self._fd, max(self._read_chunk, want - len(self._buf)))
            except BlockingIOError:
                chunk = b""
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
                self._fill_raw(1)

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
        self._fill_ubx(size)
        data = bytes(self._out[:size])
        del self._out[:size]
        self._consume_packet_bytes(len(data))
        return data

    def _consume_packet_bytes(self, nbytes):
        """Advance packet accounting for bytes handed to pyubx2."""
        while nbytes > 0 and self._pending_packet_sizes:
            head = self._pending_packet_sizes[0]
            if nbytes < head:
                self._pending_packet_sizes[0] = head - nbytes
                return
            nbytes -= head
            self._pending_packet_sizes.popleft()
            self._last_packet_timestamp = self._pending_packet_timestamps.popleft()
            self._last_packet_queue_remains = bool(
                self._pending_packet_sizes or self._buf or self._out
            )

    def pop_packet_timestamp(self):
        """Return the receive-monotonic timestamp for the last complete packet."""
        ts = self._last_packet_timestamp
        self._last_packet_timestamp = None
        return ts

    def pop_packet_metadata(self):
        """Return metadata for the last complete packet handed to pyubx2."""
        ts = self._last_packet_timestamp
        queue_remains = self._last_packet_queue_remains
        self._last_packet_timestamp = None
        self._last_packet_queue_remains = None
        return ts, queue_remains

    def write(self, data):
        return os.write(self._fd, data)

    def readline(self, size=-1):
        if size is None or size < 0:
            size = 4096
        line = bytearray()
        while len(line) < size:
            ch = self.read(1)
            if not ch:
                break
            line.extend(ch)
            if ch == b"\n":
                break
        return bytes(line)

    def flush(self):
        return None

    def discard_input(self, idle_s=0.2, max_s=2.0):
        """Drop queued kernel GNSS bytes so subsequent reads start near live data.

        The kernel device can accumulate a large backlog while no userspace
        reader is attached. Drain any immediately available bytes in
        nonblocking mode until the device stays idle for a short interval.
        """
        self._buf.clear()
        self._out.clear()

        start = time.monotonic()
        idle_start = None
        drained = 0
        try:
            while time.monotonic() - start < max_s:
                try:
                    chunk = os.read(self._fd, self._read_chunk)
                except BlockingIOError:
                    chunk = b""

                if chunk:
                    drained += len(chunk)
                    idle_start = None
                    continue

                if idle_start is None:
                    idle_start = time.monotonic()
                elif time.monotonic() - idle_start >= idle_s:
                    break
                time.sleep(0.01)
        finally:
            pass
        return drained

    def close(self):
        try:
            return os.close(self._fd)
        finally:
            release_device_lock(self._lock_fd)

    def fileno(self):
        return self._fd


def _open_serial_exclusive(device, baud):
    import serial

    lock_fd, _lock_path = acquire_device_lock(device)
    try:
        ser = serial.Serial(
            device,
            baud,
            timeout=2.0,
            dsrdtr=False,
            rtscts=False,
            exclusive=True,
        )
        ser.reset_input_buffer()
    except Exception:
        release_device_lock(lock_fd)
        raise

    original_close = ser.close
    _lock_released = [False]  # mutable so closure can write it

    def close_with_lock():
        try:
            return original_close()
        finally:
            if not _lock_released[0]:
                _lock_released[0] = True
                release_device_lock(lock_fd)

    ser.close = close_with_lock
    ser._peppar_lock_fd = lock_fd
    return ser


def open_gnss(device, baud=115200):
    """Open a GNSS device, auto-detecting serial vs kernel char device.

    Args:
        device: path like /dev/gnss0 or /dev/gnss-top or /dev/ttyACM0
        baud: baud rate (only used for serial ports)

    Returns:
        (stream, device_type) where stream is readable/writable and
        device_type is 'gnss' or 'serial'
    """
    # Kernel GNSS devices: /dev/gnss0, /dev/gnss1, etc.
    basename = os.path.basename(device)
    if basename.startswith('gnss') and basename[4:].isdigit():
        log.info(f"Opening {device} as kernel GNSS char device")
        stream = KernelGnssStream(device)
        return stream, 'gnss'

    # Everything else: try pyserial
    log.info(f"Opening {device} as serial port at {baud} baud")
    ser = _open_serial_exclusive(device, baud)
    return ser, 'serial'


def close_gnss(stream, device_type):
    """Close a GNSS stream."""
    stream.close()
