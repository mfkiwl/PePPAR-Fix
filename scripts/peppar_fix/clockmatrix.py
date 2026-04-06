"""Low-level I2C access to Renesas 8A34002 ClockMatrix.

Uses 2-byte register addressing via smbus2.i2c_rdwr. Thread-safe.
The 8A34002 does NOT use the 1B page register mode (0xFC) — that's
the 8A34002.

See docs/timebeat-otc-register-map.md for the full register map.
"""

import threading

try:
    import smbus2
except ImportError:
    smbus2 = None


class ClockMatrixI2C:
    """Thread-safe I2C access to the Renesas 8A34002 ClockMatrix."""

    def __init__(self, bus_num: int, addr: int = 0x58):
        if smbus2 is None:
            raise ImportError("smbus2 not installed: pip install smbus2")
        self._bus = smbus2.SMBus(bus_num)
        self._addr = addr
        self._lock = threading.Lock()

    def close(self):
        with self._lock:
            self._bus.close()

    def read(self, reg_addr: int, length: int) -> bytes:
        """Read `length` bytes from 16-bit register address."""
        hi = (reg_addr >> 8) & 0xFF
        lo = reg_addr & 0xFF
        with self._lock:
            msg_w = smbus2.i2c_msg.write(self._addr, [hi, lo])
            msg_r = smbus2.i2c_msg.read(self._addr, length)
            self._bus.i2c_rdwr(msg_w, msg_r)
            return bytes(msg_r)

    def write(self, reg_addr: int, data: bytes | list) -> None:
        """Write bytes to 16-bit register address."""
        hi = (reg_addr >> 8) & 0xFF
        lo = reg_addr & 0xFF
        with self._lock:
            msg = smbus2.i2c_msg.write(self._addr, [hi, lo] + list(data))
            self._bus.i2c_rdwr(msg)

    def read_modify_write(self, reg_addr: int, mask: int, value: int) -> int:
        """Atomic read-modify-write of a single byte register.

        Clears bits in `mask`, then ORs in `value`. Returns new value.
        """
        with self._lock:
            hi = (reg_addr >> 8) & 0xFF
            lo = reg_addr & 0xFF
            msg_w = smbus2.i2c_msg.write(self._addr, [hi, lo])
            msg_r = smbus2.i2c_msg.read(self._addr, 1)
            self._bus.i2c_rdwr(msg_w, msg_r)
            current = list(msg_r)[0]
            new_val = (current & ~mask) | (value & mask)
            msg = smbus2.i2c_msg.write(self._addr, [hi, lo, new_val])
            self._bus.i2c_rdwr(msg)
            return new_val

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
