"""ClockMatrix phase source — reads PFD phase error from a DPLL locked to PPS.

Configures a DPLL in PLL mode with CLK2 (F9T PPS) as reference.
Reads the DPLL phase status register for the PFD error in ITDC_UI
units (50 ps per count). This gives the phase difference between
the PPS reference and the DPLL's output.

For peppar-fix, a second DPLL (the "actuator DPLL") is steered via
FCW. Both DPLLs share the same OCXO clock tree, so the phase error
measured by this DPLL reflects the steered output's drift from PPS.
"""

import logging
import time

from peppar_fix.interfaces import PhaseSource
from peppar_fix.clockmatrix import ClockMatrixI2C

log = logging.getLogger(__name__)

# DPLL module bases
_DPLL_BASES = {0: 0xC3B0, 1: 0xC400, 2: 0xC438, 3: 0xC480}

# Status module base
_MOD_STATUS = 0xC03C

# DPLL offsets
_DPLL_MODE_OFFSET = 0x37
_DPLL_REF_MODE_OFFSET = 0x35
_DPLL_REF_P0_OFFSET = 0x0F

# Phase status offsets from MOD_STATUS
_DPLL_PHASE_STATUS_OFFSETS = {0: 0xDC, 1: 0xE4, 2: 0xEC, 3: 0xF4}

# PLL mode bits
_PLL_MODE_SHIFT = 3
_PLL_MODE_MASK = 0x07 << _PLL_MODE_SHIFT
_PLL_MODE_PLL = 0

# TDC resolution
ITDC_UI_PS = 50  # 50 ps per ITDC_UI count

# The phase status register is 36-bit signed (in an 8-byte field)
_SIGN_BIT_36 = 1 << 35
_MASK_36 = (1 << 36) - 1


class ClockMatrixPhaseSource(PhaseSource):
    """Phase measurement via DPLL PFD locked to CLK2 (F9T PPS).

    The DPLL's Phase Frequency Detector (PFD) continuously measures
    the phase error between CLK2 (PPS reference) and the DPLL's
    feedback output. This gives us PPS-vs-clock-tree phase at 50 ps
    resolution, updated every PPS edge.

    Args:
        i2c: ClockMatrixI2C instance
        dpll_id: DPLL to use for measurement (default 2, must not be
                 the same as the actuator DPLL)
        pps_clk: CLK input number for PPS (default 2 = CLK2)
    """

    def __init__(self, i2c: ClockMatrixI2C, dpll_id: int = 2,
                 pps_clk: int = 2):
        self._i2c = i2c
        self._dpll_id = dpll_id
        self._pps_clk = pps_clk
        self._base = _DPLL_BASES[dpll_id]
        self._phase_reg = _MOD_STATUS + _DPLL_PHASE_STATUS_OFFSETS[dpll_id]
        self._original_mode: int | None = None
        self._original_ref_mode: int | None = None
        self._original_ref_p0: int | None = None

    def setup(self) -> None:
        """Configure DPLL for PLL mode locked to PPS."""
        mode_reg = self._base + _DPLL_MODE_OFFSET
        ref_mode_reg = self._base + _DPLL_REF_MODE_OFFSET
        ref_p0_reg = self._base + _DPLL_REF_P0_OFFSET

        self._original_mode = self._i2c.read(mode_reg, 1)[0]
        self._original_ref_mode = self._i2c.read(ref_mode_reg, 1)[0]
        self._original_ref_p0 = self._i2c.read(ref_p0_reg, 1)[0]

        # Set reference to CLK2 (PPS), manual mode
        self._i2c.write(ref_p0_reg, [self._pps_clk])
        self._i2c.write(ref_mode_reg, [0x01])  # manual

        # Set PLL mode in bits[5:3]
        new_mode = (self._original_mode & ~_PLL_MODE_MASK) | \
                   (_PLL_MODE_PLL << _PLL_MODE_SHIFT)
        self._i2c.write(mode_reg, [new_mode])

        log.info("ClockMatrix phase DPLL_%d: PLL mode, ref=CLK%d (PPS)",
                 self._dpll_id, self._pps_clk)

        # Wait for lock acquisition
        for i in range(10):
            time.sleep(1)
            status = self._i2c.read(
                _MOD_STATUS + 0x18 + self._dpll_id, 1)[0]
            state = status & 0x07
            if state == 1:  # locked
                log.info("ClockMatrix DPLL_%d: locked to CLK%d after %ds",
                         self._dpll_id, self._pps_clk, i + 1)
                return
            log.debug("ClockMatrix DPLL_%d: state=%d, waiting...",
                      self._dpll_id, state)

        log.warning("ClockMatrix DPLL_%d: not locked after 10s (state=%d)",
                    self._dpll_id, state)

    def teardown(self) -> None:
        """Restore original DPLL configuration."""
        mode_reg = self._base + _DPLL_MODE_OFFSET
        ref_mode_reg = self._base + _DPLL_REF_MODE_OFFSET
        ref_p0_reg = self._base + _DPLL_REF_P0_OFFSET

        if self._original_mode is not None:
            self._i2c.write(mode_reg, [self._original_mode])
        if self._original_ref_mode is not None:
            self._i2c.write(ref_mode_reg, [self._original_ref_mode])
        if self._original_ref_p0 is not None:
            self._i2c.write(ref_p0_reg, [self._original_ref_p0])

        log.info("ClockMatrix phase DPLL_%d: restored", self._dpll_id)

    def read_phase_ns(self) -> float | None:
        """Read PFD phase error in nanoseconds.

        Returns the phase error between CLK2 (PPS) and the DPLL output.
        The raw register is 36-bit signed in ITDC_UI (50 ps) units,
        stored in an 8-byte little-endian field.

        Sign convention: positive = steered clock late (needs to speed up).
        """
        data = self._i2c.read(self._phase_reg, 8)
        raw = int.from_bytes(data, 'little')

        # Extract 36-bit signed value
        val = raw & _MASK_36
        if val & _SIGN_BIT_36:
            val -= (1 << 36)

        # Convert ITDC_UI to nanoseconds
        return val * ITDC_UI_PS / 1000.0

    def read_phase_ps(self) -> int | None:
        """Read PFD phase error in picoseconds (integer, no float loss)."""
        data = self._i2c.read(self._phase_reg, 8)
        raw = int.from_bytes(data, 'little')
        val = raw & _MASK_36
        if val & _SIGN_BIT_36:
            val -= (1 << 36)
        return val * ITDC_UI_PS

    @property
    def resolution_ns(self) -> float:
        return ITDC_UI_PS / 1000.0  # 0.050 ns = 50 ps
