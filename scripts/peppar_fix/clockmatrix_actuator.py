"""ClockMatrix frequency actuator — steers Renesas 8A34012 via FCW.

Writes a Frequency Control Word (FCW) to the DPLL_FREQ register in
write_freq mode (pll_mode=2). The FCW provides direct, linear frequency
control with 0.111 fppb resolution and ±244 ppm range.

The DPLL must be switched to write_freq mode (pll_mode=2 in MODE
register bits[5:3]) before FCW writes take effect. On teardown, the
original mode is restored.

See docs/timebeat-otc-register-map.md and
docs/timebeat-integration-paths.md for hardware details.
"""

import logging
import math

from peppar_fix.interfaces import FrequencyActuator
from peppar_fix.clockmatrix import ClockMatrixI2C

log = logging.getLogger(__name__)

# DPLL module bases
_DPLL_BASES = {0: 0xC3B0, 1: 0xC400, 2: 0xC438, 3: 0xC480}

# DPLL_FREQ module bases (FCW register)
_DPLL_FREQ_BASES = {0: 0xC838, 1: 0xC840, 2: 0xC848, 3: 0xC850}

# DPLL_MODE offset within DPLL module
_DPLL_MODE_OFFSET = 0x37

# MODE register bit layout:
#   bits[2:0] = state_mode (0=auto, 1=freerun, 2=locked, 3=holdover)
#   bits[5:3] = pll_mode (0=PLL, 1=write_phase, 2=write_freq, ...)
_PLL_MODE_SHIFT = 3
_PLL_MODE_MASK = 0x07 << _PLL_MODE_SHIFT
_PLL_MODE_WRITE_FREQ = 2

# FCW encoding: FCW = (1 - 1/(1 + ffo)) × 2^53
# For small ffo: FCW ≈ ffo × 2^53
# Range: ±(2^41 - 1), Resolution: 1/2^53 ≈ 0.111 fppb
_FCW_SCALE = 2**53
_FCW_MAX = 2**41 - 1
_FCW_MIN = -(2**41)

# Maximum range in ppb: ±244 ppm = ±244,000 ppb
_MAX_ADJ_PPB = 244_000.0


def ppb_to_fcw(ppb: float) -> int:
    """Convert frequency offset in ppb to 42-bit FCW.

    Uses Timebeat's formula: FCW = (1 - 1/(1 + ffo)) × 2^53
    where ffo = ppb / 1e9 (fractional frequency offset).
    """
    ffo = ppb / 1e9
    if ffo == 0:
        return 0
    fcw = (1 - 1 / (1 + ffo)) * _FCW_SCALE
    return int(max(_FCW_MIN, min(_FCW_MAX, fcw)))


def fcw_to_ppb(fcw: int) -> float:
    """Convert 42-bit FCW back to ppb.

    Inverse of ppb_to_fcw: ffo = 1 / (1 - fcw/2^53) - 1
    """
    if fcw == 0:
        return 0.0
    ratio = fcw / _FCW_SCALE
    if ratio >= 1.0:
        return _MAX_ADJ_PPB * 1000  # overflow
    ffo = 1 / (1 - ratio) - 1
    return ffo * 1e9


class ClockMatrixActuator(FrequencyActuator):
    """Frequency steering via Renesas 8A34012 DPLL FCW register.

    On setup(), switches the target DPLL to write_freq mode.
    On teardown(), restores the original DPLL mode.

    Args:
        i2c: ClockMatrixI2C instance (bus and address configured)
        dpll_id: DPLL number (0-3). Default 3 for otcBob1.
    """

    def __init__(self, i2c: ClockMatrixI2C, dpll_id: int = 3):
        self._i2c = i2c
        self._dpll_id = dpll_id
        self._mode_reg = _DPLL_BASES[dpll_id] + _DPLL_MODE_OFFSET
        self._freq_reg = _DPLL_FREQ_BASES[dpll_id]
        self._original_mode: int | None = None
        self._current_ppb = 0.0

    def setup(self) -> None:
        """Switch DPLL to write_freq mode. Preserves original mode for teardown.

        If the DPLL is already in write_freq mode (e.g., bootstrap set it up),
        we preserve the existing FCW and just record the original mode for
        teardown. This avoids zeroing the bootstrap frequency.
        """
        current_mode = self._i2c.read(self._mode_reg, 1)[0]
        current_pll = (current_mode >> _PLL_MODE_SHIFT) & 0x07

        if current_pll == _PLL_MODE_WRITE_FREQ:
            # Bootstrap already set write_freq mode — inherit its FCW
            self._original_mode = current_mode & ~_PLL_MODE_MASK  # PLL mode for teardown
            self._current_ppb = self.read_frequency_ppb()
            log.info("ClockMatrix DPLL_%d: already in write_freq mode, "
                     "inheriting FCW=%.1f ppb",
                     self._dpll_id, self._current_ppb)
            return

        self._original_mode = current_mode
        log.info("ClockMatrix DPLL_%d: original MODE=0x%02X (pll_mode=%d)",
                 self._dpll_id, self._original_mode, current_pll)

        # Write FCW=0 before mode switch to avoid frequency jump
        self._write_fcw_raw(0)

        # Set pll_mode=2 (write_freq) in bits[5:3]
        new_mode = self._i2c.read_modify_write(
            self._mode_reg, _PLL_MODE_MASK,
            _PLL_MODE_WRITE_FREQ << _PLL_MODE_SHIFT)

        readback_pll = (new_mode >> _PLL_MODE_SHIFT) & 0x07
        if readback_pll != _PLL_MODE_WRITE_FREQ:
            raise RuntimeError(
                "Failed to set write_freq mode: MODE=0x%02X (pll_mode=%d)" %
                (new_mode, readback_pll))

        log.info("ClockMatrix DPLL_%d: write_freq mode active (MODE=0x%02X)",
                 self._dpll_id, new_mode)
        self._current_ppb = 0.0

    def teardown(self) -> None:
        """Restore original DPLL mode."""
        self._write_fcw_raw(0)
        if self._original_mode is not None:
            self._i2c.write(self._mode_reg, [self._original_mode])
            log.info("ClockMatrix DPLL_%d: restored MODE=0x%02X",
                     self._dpll_id, self._original_mode)
        self._current_ppb = 0.0

    def adjust_frequency_ppb(self, ppb: float) -> float:
        """Write FCW for the given ppb offset. Returns actual ppb applied."""
        clamped = max(-_MAX_ADJ_PPB, min(_MAX_ADJ_PPB, ppb))
        fcw = ppb_to_fcw(clamped)
        self._write_fcw_raw(fcw)
        self._current_ppb = fcw_to_ppb(fcw)
        return self._current_ppb

    def read_frequency_ppb(self) -> float:
        """Read current FCW and convert to ppb."""
        data = self._i2c.read(self._freq_reg, 6)
        raw = int.from_bytes(data, 'little')
        # Extract 42-bit signed: bits[41:0]
        fcw = raw & 0x3FFFFFFFFFF
        if fcw & (1 << 41):
            fcw -= (1 << 42)
        self._current_ppb = fcw_to_ppb(fcw)
        return self._current_ppb

    @property
    def max_adj_ppb(self) -> float:
        return _MAX_ADJ_PPB

    @property
    def resolution_ppb(self) -> float:
        return 1.0 / 9_007_199.254  # 0.111 fppb

    def _write_fcw_raw(self, fcw: int) -> None:
        """Write 42-bit signed FCW, preserving reserved bits in byte 5."""
        current = self._i2c.read(self._freq_reg, 6)
        reserved = current[5] & 0xFC  # upper 6 bits of byte 5 are reserved

        if fcw < 0:
            raw = (1 << 48) + fcw
        else:
            raw = fcw
        buf = list((raw & 0xFFFFFFFFFFFF).to_bytes(6, 'little'))
        buf[5] = (buf[5] & 0x03) | reserved
        self._i2c.write(self._freq_reg, buf)
