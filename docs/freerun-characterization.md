# Freerun PHC Characterization

## Overview

`peppar-fix --freerun` runs the full pipeline without steering the PHC.
The servo computes what it *would* do and logs everything to CSV, but
never calls `adjfine()`.  This characterizes:

1. **EXTTS timestamping precision** — PPS error noise floor at tau=1s
2. **Free-running oscillator stability** — ADEV/TDEV of phase drift
3. **Error source quality comparison** — PPS vs PPS+qErr vs PPS+PPP

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

## Implementation checklist

- [ ] Add `--freerun` flag to `peppar_fix_engine.py` (skip adjfine, restep)
- [ ] Add `--freerun` to wrapper (pass through, hold clockClass at 248)
- [ ] Add `phc_gettime_ns` column to servo CSV
- [ ] Add auto-stop on PPS error threshold
- [ ] Write `tools/plot_deviation.py` (ADEV, TDEV, MDEV with Plotly)
- [ ] Test freerun on both TimeHat (TCXO) and ocxo (OCXO)
