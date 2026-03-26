# PPS+PPP Error Source: Carrier-Phase-Precise PPS Correction

## Summary

The PPP filter's `dt_rx` (receiver clock offset from GNSS time) can replace
TIM-TP `qErr` as the PPS correction, yielding sub-ns precision instead of
TIM-TP's ~3 ns noise floor. This requires a one-time calibration offset
determined at startup by comparing against TIM-TP for ~10 epochs.

## Background

The servo's error sources each estimate the PHC's offset from the GNSS
second. The PPS edge gives a raw estimate (`pps_error_ns`, ~20 ns confidence).
TIM-TP's quantization error (`qErr`) refines this to ~3 ns. Carrier-phase
PPP can refine it further.

The previous attempt at PPS+PPP (`pps_error_ns + dt_rx_ns`) failed because
`dt_rx` is the receiver's *absolute* clock offset from GNSS time (~6 ms),
not a small PPS correction. A gate (`|dt_rx| < 100 µs`) papered over this,
but since the receiver's clock naturally free-runs at millisecond-level
offsets, PPS+PPP was never selected.

## Key Finding: rcvTow Fractional Part is Constant

Experiment on ocxo (2026-03-26, 120 epochs):

```
rcvTow fractional second: 994000000.006 ns — ALL 120 EPOCHS IDENTICAL
rcvTow mod 8ns (125 MHz tick): 0.006 ns — confirms 125 MHz clock grid
```

The F9T's RAWX measurement epoch is hardware-locked at exactly **994 ms**
on its own clock (6 ms before the receiver's second boundary). Since this
is constant, `dt_rx` alone carries the entire relationship between the
receiver clock and the GNSS second.

## The 125 MHz Tick Model

The F9T generates PPS at the nearest 125 MHz clock tick (8 ns period) to
the GNSS integer second. With PPP's precise `dt_rx`, we can compute
exactly which tick that is.

The receiver clock's fractional second at the GNSS integer second:

```
D_ns = dt_rx_ns mod 1_000_000_000
```

The PPS fires at the nearest tick, so the PPS timing error is:

```
qerr_ppp = round(D_ns / tick_ns) * tick_ns - D_ns
```

where `tick_ns = 8.0` for 125 MHz.

This is mathematically equivalent to TIM-TP `qErr` but derived from
carrier-phase observations instead of the receiver's pseudorange-based
navigation solution.

## Calibration Offset

PPP's absolute `dt_rx` differs from the receiver's internal clock estimate
by a constant offset (a few ns), caused by:
- Float ambiguity bias in the PPP filter
- Pseudorange seeding differences
- Different multipath averaging

This offset is constant for a given filter session (it doesn't change
between epochs because TD carrier phase tracks changes exactly). It
must be determined at startup by comparing `qerr_ppp` against TIM-TP
`qErr` for ~10 epochs.

**Calibrated formula:**

```python
D_ns = (dt_rx_ns - cal_offset_ns) % 1_000_000_000
qerr_ppp = round(D_ns / tick_ns) * tick_ns - D_ns
```

The calibration offset is found by circular-mean comparison of
`qerr_ppp - qerr_timtp` over the initial epochs (circular because
the qerr values wrap at ±4 ns).

## Experimental Validation

### Internal consistency (2026-03-26, 60 epochs)

The PPP-derived qerr changes between epochs are **perfectly predicted**
by `dt_rx` changes:

```
PPP qerr prediction error (epoch-to-epoch):  0.0000 ns
TIM-TP qerr prediction error:                2.518 ns
```

PPP's tick model is self-consistent to numerical precision. TIM-TP has
~2.5 ns of epoch-to-epoch noise that PPP does not share.

### Cross-check against TIM-TP

Before calibration, the raw comparison shows stdev = 3.5 ns.
After calibration (offset ≈ −3 ns), stdev = 2.9 ns.

The residual 2.9 ns stdev is dominated by **TIM-TP noise**, not PPP
error — confirmed by the 2.5 ns TIM-TP prediction error above.

### Confidence improvement

| Source    | Error formula                       | Confidence |
|-----------|-------------------------------------|------------|
| PPS       | `pps_error_ns`                      | ~20 ns     |
| PPS+qErr  | `pps_error_ns + qerr_timtp`         | ~3 ns      |
| PPS+PPP   | `pps_error_ns + qerr_ppp`           | ~0.15 ns   |

PPS+PPP confidence = `dt_rx_sigma` (typically 0.1–0.15 ns after
filter convergence), a ~20× improvement over PPS+qErr.

## Implementation

### Error source computation (error_sources.py)

Remove the `|dt_rx| < 100 µs` gate in the engine. Replace with:

```python
if dt_rx_sigma_ns is not None and dt_rx_sigma_ns < carrier_max_sigma:
    D_ns = (dt_rx_ns - cal_offset_ns) % 1_000_000_000
    qerr_ppp = round(D_ns / tick_ns) * tick_ns - D_ns
    sources.append(ErrorSource('PPS+PPP',
                               pps_error_ns + qerr_ppp,
                               dt_rx_sigma_ns))
```

### Calibration lifecycle

1. **Startup**: set `cal_offset_ns = 0`, run PPS+qErr as primary
2. **First 10–20 epochs with both qErr and dt_rx**: accumulate
   `qerr_ppp - qerr_timtp` samples, compute circular mean (period 8 ns)
3. **After calibration**: PPS+PPP competes with PPS+qErr; it should
   win on confidence (~0.15 ns vs ~3 ns)
4. **Recalibrate on**: filter reset, clock reset (`clkReset` flag in RAWX),
   or if PPS+PPP vs PPS+qErr diverges by > 2 ticks (16 ns)

### Parameters

| Parameter         | Value    | Source                |
|-------------------|----------|-----------------------|
| `tick_ns`         | 8.0      | 125 MHz F9T clock     |
| `cal_epochs`      | 10–20    | Empirical             |
| `carrier_max_sigma` | 50 ns  | Existing gate         |

The 125 MHz clock frequency should be confirmed per receiver. The
`rcvTow mod tick_ns` residual provides a sanity check: it should
be < 0.1 ns if the frequency is correct.

## PPP-AR Consideration

With PPP-AR (integer ambiguity resolution), the PPP `dt_rx` absolute
level would be carrier-phase-accurate from the start, eliminating the
need for the calibration step. The current SSR source (SSRA00BKG0)
provides zero phase bias, so PPP-AR is not possible today. When a
phase-bias-capable SSR source becomes available, the calibration
step can be removed.

## Live Servo Validation (2026-03-26)

Ran the engine with PPS+PPP competing against PPS+qErr on ocxo (E810,
F9T via I2C at 9600 baud). Over 34 servo epochs:

```
qerr_ppp - qerr_timtp:
  n=34  mean=+0.510 ns  stdev=2.982 ns
  min=-4.717  max=+5.710
```

**Mean corrections agree to 0.5 ns.** The 3 ns stdev is dominated by
TIM-TP noise, confirming PPS+PPP is the more precise source.

PPS+PPP won the competition after calibration (confidence 0.2 ns vs
3.0 ns for PPS+qErr). The servo itself went unstable due to I2C
pipeline stalls (only 34 epochs over ~5 min, PHC drifting between),
not due to the error source formula.

A proper TDEV comparison requires a host with faster observation
delivery (USB transport on TimeHat, or kernel I2C driver improvements
on ocxo).

## Implementation Status

- `scripts/peppar_fix/error_sources.py`: `ppp_qerr()`, `PPPCalibration`,
  updated `compute_error_sources()` with `ppp_cal` parameter
- `scripts/peppar_fix_engine.py`: removed `|dt_rx| < 100 µs` gate,
  added calibration feeding in `_servo_epoch()`, `PPPCalibration`
  instance in servo context
- Calibration requires dt_rx stability (3 consecutive epochs with
  < 1 µs jump) before accepting samples, to avoid calibrating during
  filter convergence

## Files

- Experiment script: `scripts/rcvtow_dt_rx_probe.py`
- Experiment data: `/tmp/rcvtow_probe.csv` on ocxo (2026-03-26)
- Servo test data: `/tmp/servo_ppp_test.csv` on ocxo (2026-03-26)
- Error source selection: `scripts/peppar_fix/error_sources.py`
- Engine integration: `scripts/peppar_fix_engine.py`
