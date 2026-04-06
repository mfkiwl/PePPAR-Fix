# ClockMatrix Bootstrap Plan for Timebeat OTC

## Key insight: ClockMatrix supplements the PHC, doesn't replace it

On Timebeat OTC hardware, the Renesas 8A34012 ClockMatrix drives the
i226's 25 MHz PHY clock. The PHC counts these cycles for PTP
timestamps. The ClockMatrix also produces a PPS output from the same
clock tree.

This is a **hybrid architecture**:
- The PHC provides time-of-day (seconds + nanoseconds) for PTP
- The ClockMatrix provides frequency synthesis and a PPS output
- Both must agree with GPS time independently
- When both are correct, they naturally agree with each other

The ClockMatrix gives us finer frequency control (0.111 fppb via FCW)
than PHC adjfine (~1 ppb). The PHC gives us PTP timestamps that no
ClockMatrix register can provide. We need both.

## Two phase relationships

| Relationship | How measured | How steered |
|---|---|---|
| PHC phase vs GPS | EXTTS timestamps F9T PPS | PHC step (adj_setoffset) |
| ClockMatrix PPS OUT vs GPS | TDC or TICC | FCW on DPLL_3 |

On TimeHat (PHC-only), there's one relationship: PHC vs GPS, steered
by adjfine. PEROUT derives PPS from the PHC, so both PTP timestamps
and PPS OUT track together.

On Timebeat OTC, the PHC and ClockMatrix PPS OUT are separate outputs
from the same 25 MHz clock tree, initialized at different times. They
can have a static phase offset relative to each other. When both are
aligned to GPS independently, this offset is near zero.

## Bootstrap sequence

### Step 1: PHC coarse time alignment

Standard EXTTS-based bootstrap:
1. Enable EXTTS on the SDP pin receiving F9T PPS
2. Read PPS timestamps from EXTTS
3. Compute phase error: `pps_error_ns = fractional_error(phc_nsec)`
4. Step PHC: `adj_setoffset(-pps_error_ns)`
5. Verify: read another PPS, confirm error < threshold

Result: PHC within ~8 ns of GPS time (EXTTS resolution limited).

### Step 2: Zero PHC adjfine

Set `adjfine(0)`. The PHC now runs at the raw 25 MHz rate from the
ClockMatrix. Any frequency offset from GPS is now entirely in the
ClockMatrix domain.

### Step 3: Measure OCXO frequency offset

**Important**: The ClockMatrix may already have a non-zero frequency
offset programmed (from EEPROM or Timebeat's last session). We must
either read back the current DPLL_3 FOD_FREQ/FCW and account for it,
or force DPLL_3 to nominal frequency before measuring.

Safest approach: read the current DPLL_3 state. If it's in PLL mode
(Timebeat's default), the output frequency is already OCXO-derived
and close to nominal. The measurement captures the total offset
(OCXO natural + any programmed correction).

Measurement:
1. With adjfine=0, read EXTTS PPS timestamps for N seconds (e.g., 10)
2. Compute frequency error from PPS interval drift: `freq_ppb = Δerror / Δt`
3. This is the 25 MHz offset from GPS in ppb

### Step 4: Set DPLL_3 FCW

1. Switch DPLL_3 to write_freq mode: write 0x10 to MODE register
   (pll_mode=2 in bits[5:3])
2. Set FCW to the measured frequency offset from Step 3
3. Verify: the PPS drift rate should drop to near zero

The FCW directly corrects the 25 MHz output frequency. Since the PHC
is clocked from this 25 MHz (with adjfine=0), the PHC rate also
corrects automatically.

### Step 5: Phase-align ClockMatrix PPS OUT (optional)

If the ClockMatrix PPS OUT has a phase offset from F9T PPS:
- The `DPLL_CTRL_PHASE_OFFSET_CFG` register (36-bit, 50 ps/LSB)
  can apply a static phase correction to the output
- Measure the offset using the Output TDC or TICC
- Write the correction to zero the offset

For now, this step is optional — the PPS OUT should be within ~8 ns
of GPS after the FCW correction (limited by the EXTTS measurement
in Step 3, not the ClockMatrix resolution).

### Step 6: Apply glide slope to FCW

The measured frequency from Step 3 has uncertainty. The glide slope
smoothly transitions from the measured value to the servo's tracking
estimate:

```
fcw_total = fcw_measured + glide_correction(t)
```

where `glide_correction(t)` decays to zero over the glide time
constant (same math as the current adjfine glide slope).

### Step 7: Hand off to engine

The engine receives:
- DPLL_3 in write_freq mode with the current FCW value
- PHC stepped to GPS time with adjfine=0
- The DPLL_3 FCW includes any glide slope residual
- The phase measurement source (TDC or network TICC) is configured

The engine's servo loop:
1. Read phase error (PPS vs steered output)
2. PI controller computes frequency correction
3. Write FCW to DPLL_3
4. The 25 MHz tracks GPS → PHC tracks GPS → PTP timestamps correct

## Register summary

| Register | Address | What |
|---|---|---|
| DPLL_3 MODE | 0xC4B7 | bits[5:3]=2 for write_freq (write 0x10) |
| DPLL_3 FCW | 0xC850 | 6 bytes, 42-bit signed, 0.111 fppb/count |
| DPLL_CTRL_3 PHASE_OFFSET | 0xC6D0 | 36-bit signed, 50 ps/LSB |
| DPLL_CTRL_3 FOD_FREQ | 0xC6D8 | 8 bytes (6B M + 2B N), nominal frequency |

FCW formula: `FCW = (1 - 1/(1 + ppb/1e9)) × 2^53 ≈ ppb × 9,007,199`

## What the engine needs to know

After bootstrap, the engine context must include:
- `clockmatrix_i2c`: I2C bus handle
- `clockmatrix_fcw_current`: the FCW value at handoff (including glide)
- `clockmatrix_dpll_mode_original`: for teardown restoration
- `adjfine_ppb = 0`: PHC adjfine is zeroed, all frequency steering is via FCW
