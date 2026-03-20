#!/usr/bin/env python3
"""gnss_stream.py — Unified GNSS device opener for peppar-fix.

Supports both:
  - /dev/gnss* (kernel GNSS char device, E810 onboard)
  - /dev/ttyACM*, /dev/gnss-top (serial ports, F9T EVK)

Returns a file-like object that pyubx2's UBXReader can wrap.
"""

import logging
import os

log = logging.getLogger(__name__)


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
        stream = open(device, 'r+b', buffering=0)
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
