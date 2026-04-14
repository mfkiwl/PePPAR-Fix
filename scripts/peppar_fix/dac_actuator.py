"""DAC-based frequency actuator for voltage-controlled oscillators.

Steers a VCOCXO by writing a voltage to a DAC over I2C.  The DAC
output voltage controls the varactor, which tunes the crystal frequency.

The mapping from ppb to DAC code depends on the oscillator's tuning
sensitivity (Hz/V) and the DAC's voltage range.  These must be
characterized per-oscillator and provided at construction.

Supported DACs:
- MCP4725 (12-bit, I2C, 0-Vcc)
- AD5693R (16-bit, I2C, 0-Vcc or 0-2×Vref)
- Generic: any DAC addressable via smbus2 write_word_data

See docs/state-persistence-design.md Phase 4.
"""

import logging
import struct

from peppar_fix.interfaces import FrequencyActuator

log = logging.getLogger(__name__)


class DacActuator(FrequencyActuator):
    """Frequency actuator via DAC → VCOCXO varactor.

    Args:
        bus_num: I2C bus number (e.g. 1 for /dev/i2c-1)
        addr: I2C device address (e.g. 0x60 for MCP4725)
        bits: DAC resolution in bits (12 for MCP4725, 16 for AD5693R)
        center_code: DAC code for nominal frequency (default: midscale)
        ppb_per_code: tuning sensitivity in ppb per DAC LSB.
            Positive = higher code → higher frequency.
            Must be characterized per-oscillator.
        max_ppb: maximum frequency adjustment range (default: computed
            from ppb_per_code × available codes from center)
        dac_type: "mcp4725" or "ad5693r" or "generic"
    """

    def __init__(self, bus_num, addr, bits=12, center_code=None,
                 ppb_per_code=1.0, max_ppb=None, dac_type="mcp4725"):
        self._bus_num = bus_num
        self._addr = addr
        self._bits = bits
        self._max_code = (1 << bits) - 1
        self._center_code = center_code if center_code is not None else self._max_code // 2
        self._ppb_per_code = ppb_per_code
        self._dac_type = dac_type
        self._bus = None
        self._current_code = self._center_code
        self._current_ppb = 0.0

        # Compute range from center to rails
        codes_up = self._max_code - self._center_code
        codes_down = self._center_code
        max_from_center = min(codes_up, codes_down) * abs(ppb_per_code)
        self._max_ppb = max_ppb if max_ppb is not None else max_from_center
        self._resolution_ppb = abs(ppb_per_code)

    def setup(self):
        """Open I2C bus and set DAC to center code."""
        try:
            import smbus2
            self._bus = smbus2.SMBus(self._bus_num)
        except ImportError:
            raise ImportError("smbus2 required for DAC actuator: pip install smbus2")
        # Read current DAC state if possible, otherwise set to center
        self._write_code(self._center_code)
        self._current_ppb = 0.0
        log.info("DAC actuator: bus=%d addr=0x%02x bits=%d center=%d ppb/code=%.4f",
                 self._bus_num, self._addr, self._bits, self._center_code,
                 self._ppb_per_code)

    def teardown(self):
        """Return DAC to center (nominal frequency) and close bus."""
        if self._bus is not None:
            try:
                self._write_code(self._center_code)
                log.info("DAC returned to center code %d", self._center_code)
            except Exception as e:
                log.warning("DAC teardown failed: %s", e)
            self._bus.close()
            self._bus = None

    def adjust_frequency_ppb(self, ppb):
        """Set absolute frequency offset. Returns actual ppb applied."""
        ppb = max(-self._max_ppb, min(self._max_ppb, ppb))
        code_offset = round(ppb / self._ppb_per_code)
        code = self._center_code + code_offset
        code = max(0, min(self._max_code, code))
        self._write_code(code)
        self._current_code = code
        actual_ppb = (code - self._center_code) * self._ppb_per_code
        self._current_ppb = actual_ppb
        return actual_ppb

    def read_frequency_ppb(self):
        """Return last-written frequency offset."""
        return self._current_ppb

    @property
    def max_adj_ppb(self):
        return self._max_ppb

    @property
    def resolution_ppb(self):
        return self._resolution_ppb

    def _write_code(self, code):
        """Write a DAC code to the device."""
        if self._bus is None:
            raise RuntimeError("DAC bus not open — call setup() first")

        if self._dac_type == "mcp4725":
            # MCP4725: fast mode write (2 bytes, upper 4 bits = 0b0000)
            # Byte 0: [0 0 PD1 PD0 D11 D10 D9 D8]
            # Byte 1: [D7 D6 D5 D4 D3 D2 D1 D0]
            high = (code >> 8) & 0x0F
            low = code & 0xFF
            self._bus.write_i2c_block_data(self._addr, high, [low])

        elif self._dac_type == "ad5693r":
            # AD5693R: write DAC register (command 0x30)
            # 3 bytes: cmd, MSB, LSB (16-bit left-aligned)
            msb = (code >> 8) & 0xFF
            lsb = code & 0xFF
            self._bus.write_i2c_block_data(self._addr, 0x30, [msb, lsb])

        else:
            # Generic: write 16-bit value to register 0
            msb = (code >> 8) & 0xFF
            lsb = code & 0xFF
            self._bus.write_i2c_block_data(self._addr, 0x00, [msb, lsb])
