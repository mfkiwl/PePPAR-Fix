#!/usr/bin/env python3
"""gnss_stream.py — Unified GNSS device opener for peppar-fix.

Supports both:
  - /dev/gnss* (kernel GNSS char device, E810 onboard)
  - /dev/ttyACM*, /dev/gnss-top (serial ports, F9T EVK)

Returns a file-like object that pyubx2's UBXReader can wrap.
"""

import logging
import os
import time

log = logging.getLogger(__name__)


class KernelGnssStream:
    """Serial-like wrapper for kernel GNSS char devices.

    The kernel device can return short reads. pyubx2 expects a stream whose
    ``read(n)`` behaves more like a serial port and blocks until enough bytes
    arrive, so coalesce reads here.
    """

    def __init__(self, path):
        self._fh = open(path, "r+b", buffering=0)
        self.name = path
        self._buf = bytearray()
        self._out = bytearray()
        self._read_chunk = 4096

    def _fill_raw(self, want=1):
        """Read a larger kernel chunk and retain leftovers for packet parsing.

        The kernel GNSS char device can return packetized data. pyubx2 often
        reads in 1-2 byte increments while scanning headers, so reading
        directly from the device for every tiny request can discard the rest
        of the current kernel packet. Buffer chunks here and serve pyubx2 out
        of the retained bytes instead.
        """
        while len(self._buf) < want:
            chunk = self._fh.read(max(self._read_chunk, want - len(self._buf)))
            if chunk:
                self._buf.extend(chunk)
                continue
            time.sleep(0.01)

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
            del self._buf[:packet_len]

    def read(self, size=-1):
        if size is None or size < 0:
            self._fill_ubx(1)
            data = bytes(self._out)
            self._out.clear()
            return data
        self._fill_ubx(size)
        data = bytes(self._out[:size])
        del self._out[:size]
        return data

    def write(self, data):
        return self._fh.write(data)

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
        return self._fh.flush()

    def close(self):
        return self._fh.close()


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
    import serial
    log.info(f"Opening {device} as serial port at {baud} baud")
    ser = serial.Serial(device, baud, timeout=2.0,
                        dsrdtr=False, rtscts=False)
    ser.reset_input_buffer()
    return ser, 'serial'


def close_gnss(stream, device_type):
    """Close a GNSS stream."""
    stream.close()
