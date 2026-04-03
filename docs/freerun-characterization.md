# Freerun PHC Characterization

## Overview

`peppar-fix --freerun` runs the full pipeline without steering the PHC.
The servo computes what it *would* do and logs everything to CSV, but
never calls `adjfine()`.

**WARNING: freerun TDEV from EXTTS alone is unreliable.**  Both i226
and E810 EXTTS have ~8 ns effective resolution that masks real timing
noise.  Freerun EXTTS data shows what the quantized feedback path
reports, not the oscillator's true stability.  Always pair freerun
with a TICC capture for ground truth.  See CLAUDE.md "EXTTS TDEV
measurements are unreliable."

Freerun characterizes:

1. **Free-running oscillator stability** — ADEV/TDEV of phase drift,
   **measured by TICC** (not EXTTS)
2. **EXTTS measurement noise** — by comparing EXTTS TDEV against
   simultaneous TICC TDEV, the gap reveals PHC quantization error
3. **Error source quality comparison** — PPS vs PPS+qErr vs PPS+PPP
   correction quality (but not disciplined TDEV — that requires
   a disciplined run measured on TICC)

## Engine behavior with --freerun

- Bootstrap runs normally: phase step if outside `phc_step_threshold_ns`,
  frequency set to bootstrap estimate.  This gets the PHC close enough
  for correlation to work.
- After bootstrap: `adjfine()` is never called.  PHC drifts freely.
- Resteps are skipped (don't reset the drift we're measuring).
- clockClass stays at 248 (freerun) throughout — never promote.
- CSV logging is identical to a disciplined run.  Same columns, same
  tools for analysis.

## Additional logging

Log `clock_gettime(PHC)` at each epoch as a column (`phc_gettime_ns`).
This gives an independent phase record separate from the EXTTS path.

## Runtime

The run must be long enough to give tight error bars through the tau
region where even good OCXOs lose to GNSS.  A double-oven OCXO
(e.g., the E810's) has ADEV floor around 1e-12 at tau=1-100s and
crosses the GPS ADEV (~1e-12 at tau=1000s) somewhere around
tau=100-1000s.  For good statistics at tau=1000s, need ~10x that
duration: **3-4 hours**.

A TCXO (TimeHat i226, ±280 ppb) drifts ~280 ns/s.  At that rate,
PPS error reaches 500 µs in ~30 minutes and the correlation window
stays comfortable.  TCXO ADEV crosses GPS much earlier (~tau=10s),
so a **30-60 minute** freerun suffices.

Auto-stop when PPS error exceeds a threshold (default: 100 µs for
OCXO, 500 µs for TCXO) so the correlation gate doesn't start
dropping observations.

## Analysis: tools/plot_deviation.py

Takes one or more servo CSVs and produces interactive Plotly HTML
with overlaid deviation plots.

### Usage

```bash
# Compare freerun vs disciplined
python3 tools/plot_deviation.py \
    --freerun data/freerun-20260329.csv \
    --disciplined data/tdev-4h-20260328.csv \
    -o deviation-comparison.html

# Single-run analysis
python3 tools/plot_deviation.py data/freerun-20260329.csv
```

### Plots produced

1. **ADEV overlay**: freerun (oscillator) vs disciplined (servo).  The
   crossover tau where the servo improves on freerun validates servo
   gain tuning.  Too aggressive → servo adds noise at short tau.
   Too gentle → doesn't help at long tau.

2. **TDEV overlay of three error sources** (from a single freerun CSV):
   - `pps_error_ns` — raw PPS phase vs GPS (EXTTS noise + oscillator)
   - `pps_error_ns ± qerr_ns` — PPS corrected by TIM-TP sawtooth
   - `source_error_ns` from PPS+PPP — carrier-phase-derived correction
   This directly shows the improvement from each correction layer.

3. **Frequency offset**: slope of `pps_error_ns` over time = crystal
   accuracy in ppb.  Reported as a scalar and plotted as a time series
   (shows temperature sensitivity if the run spans a thermal cycle).

### Detrending

For ADEV/TDEV of freerun data, detrend the linear frequency offset
before computing deviations.  The slope dominates otherwise and masks
the oscillator's wander characteristics.  Report both the raw (with
drift) and detrended deviations.

## PHC timestamping resolution and noise

A freerun capture can characterize the PHC's EXTTS capture path if a
TICC is simultaneously recording the same PPS edge.

### Method

The TICC captures the same PPS edge as the PHC EXTTS, at ~60 ps
single-shot resolution.  At each epoch:

  `PHC_noise(k) = EXTTS(k) - TICC(k)`

Since the PPS source jitter is common to both, it cancels.  The
residual isolates:

1. **PHC counter quantization** — the tick grid (8 ns on i226 at
   125 MHz, sub-ns on E810)
2. **EXTTS latch jitter** — noise in the hardware timestamp capture
3. **Cable/splitter differential delay** — a static bias (removable
   by detrending)

### Requirements

- Pass `--ticc-port /dev/ticcN --ticc-log data/ticc-run.csv` to
  the engine.  The engine captures both TICC channels in-process,
  with shared lifecycle and host monotonic timestamps.  Do NOT use
  a separate TICC capture process — this causes cross-process
  coordination issues and timing ambiguity.
- TICC must be connected with one channel on the same PPS signal
  that feeds the PHC EXTTS input.  On TimeHat, TICC #1 chB carries
  the F9T PPS.  On ocxo, TICC #2 chB carries the F9T PPS.
- The TICC and EXTTS timestamps share host monotonic time from the
  same process, enabling direct epoch correlation.
- The TICC-to-EXTTS time offset is arbitrary (different cable lengths,
  different capture latencies).  Only the variation matters, not the
  absolute offset.

### Output

- Histogram of `EXTTS - TICC` residuals (shows quantization grid)
- TDEV of the residual (shows capture noise floor vs tau)
- Allan deviation of the residual (should be flat — white noise)

This characterization answers: "what is the best TDEV the servo can
achieve at tau=1s on this platform?"  On i226 with 8 ns ticks, this
is the quantization noise floor.  With qErr correction, the effective
resolution improves.

## Implementation checklist

- [x] Add `--freerun` flag to `peppar_fix_engine.py` (skip adjfine, restep)
- [x] Add `--freerun` to wrapper (pass through, hold clockClass at 248)
- [x] Add `phc_gettime_ns` column to servo CSV
- [x] Add auto-stop on PPS error threshold (`--freerun-max-error-ns`)
- [ ] Write `tools/plot_deviation.py` (ADEV, TDEV, MDEV with Plotly)
- [ ] Add EXTTS-TICC noise analysis to plot_deviation.py
- [ ] Test freerun on both TimeHat (TCXO) and ocxo (OCXO)
