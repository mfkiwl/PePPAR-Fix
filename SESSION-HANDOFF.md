# Session Handoff — 2026-03-29 (afternoon)

## What was accomplished this session

### qErr correlation fixed (was top priority)

Root cause: the old TOW-based qErr matching (`round(rcvTow)`) paired
TIM-TP samples with the wrong PPS edge. The litmus test showed 0.999x
(no improvement) because the wrong qErr was applied.

**Fix**: match TIM-TP to PPS by host monotonic time. TIM-TP arrives
~900 ms before the PPS edge it describes. `QErrStore.match_pps_mono()`
finds the closest qErr at that offset — no GPS time, no receiver clock
bias, no round/floor ambiguity.

**Verification** (servo-free, `tools/qerr_offset_sweep.py`):
- Offset 0.9s, sign +, **ratio 2.0–3.7x** variance reduction
- Works at both +200 and -200 ppb adjfine (no sign dependence)
- Adjacent offsets (0.0s, 1.9s) also show partial correlation due to
  smooth F9T sawtooth, but 0.9s is the clear best

**Key finding**: `round()` was correct for TOW matching (not `floor()`).
RAWX rcvTow includes ~-10 ms receiver clock bias, so rcvTow ≈ N.990
and `round(N.990) = N+1` = correct PPS second. But TOW matching was
fundamentally fragile — monotonic matching is robust.

### Litmus test fixed

Old litmus used `detrended_variance()` (linear fit removal) on raw
`pps_error_ns`. This failed because:
1. During servo pull-in, the nonlinear glide created ~600 ns residuals
   that drowned the ~3 ns qErr signal
2. In steady state, servo adjfine changes between epochs added phase
   steps unrelated to qErr

New litmus:
- `diff_variance()` — first-difference variance, immune to any smooth trend
- Subtracts cumulative adjfine rate before differencing to remove servo footprint
- Always logs the ratio (no silent zone between 1.0 and 2.0)

### TIM-TP timing characterized

Message ordering within each F9T burst (after PPS fires):
```
PPS(N) fires → RAWX(rcvTow≈(N-1).990) at +50ms → TIM-TP(towMS=(N+1)*1000) at +100ms → PVT(iTOW=N*1000) at +100ms
```

TIM-TP in each burst describes the PPS **two** seconds ahead of
`floor(rcvTow)`. It predicts qErr for the **following** PPS edge
(the one ~900 ms in the future).

### New diagnostic tools

- `tools/qerr_offset_sweep.py` — servo-free PPS+TIM-TP correlation
  sweep across monotonic time offsets. The definitive qErr test.
- `tools/qerr_servo_impact.py` — analyze how much variance the servo
  adds at each run phase (pull-in, glide, tracking, steady)
- `tools/qerr_correlation_check.py` — direct Pearson r of pps_error
  vs qerr from servo CSV data

### CLAUDE.md updated

Added Lab Test Protocol section: prefer `scripts/peppar-fix` wrapper
for testing over running component scripts directly.

## Known issues

### Litmus still shows ~1.0 during pull-in with rate compensation

The rate-compensated first-difference litmus in the engine showed ~1.0
during a short test (80 epochs, mostly pull-in). The sweep tool without
the servo shows 2–3.7x. The rate compensation model (subtracting
cumulative adjfine) may be imperfect, or the 32-sample window needs
more settled epochs. Needs a longer run to confirm the litmus passes
in steady state with the new monotonic matching.

### Position watchdog on ocxo

Trips after ~2.4 hours. Vertical drift from uncompensated troposphere.
Needs: larger threshold, or troposphere state, or fixed-position mode.

### TimeHat missing epochs

1911/3600 in verification run despite 0 gate drops. Unknown path.
See memory: project_timehat_missing_epochs.md.

### Correlation gate still drops ~6%

epoch_offset=0 now (was -1) but gate_wait_obs=1220 in overnight.
May be residual zero-crossing drops or a different issue.

### ocxo repo was out of date

Had to rsync full scripts/ directory to ocxo. The ocxo repo at
`~/git/PePPAR-Fix` may drift again. Consider a deploy script or
git pull workflow.

## Commits pushed

- c15d3b4: Fix qErr correlation: match by host monotonic time, not GPS TOW

All on main, pushed to origin.

## Host state

- TimeHat: idle, adjfine restored to 94.7 ppb (from drift.json),
  EXTTS enabled, igc patched (DKMS), ptp4l at 1 Hz sync
- ocxo: idle, scripts synced, stock ice driver, PEROUT via sysfs
- PiPuss: idle, position repeatability complete
