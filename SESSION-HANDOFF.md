# Session Handoff — 2026-03-29 (morning)

## TOP PRIORITY: qErr correlation is broken

The qErr litmus test fails on **100% of epochs on both hosts**. PPS+qErr
has the same variance as raw PPS — the correction adds zero value.

Evidence:
- TimeHat: `raw PPS variance is only 0.999x PPS+qErr variance` on all
  3153 logged litmus checks
- ocxo: `delta_ms=-1000` in qErr match — the TIM-TP is matched to the
  **wrong PPS edge** (1 second off)
- TDEV confirms: PPS+qErr identical to PPS at all tau (except 1.3x at
  tau=1s on TimeHat, likely noise)

Root cause is almost certainly related to the `epoch_offset=-1` issue
documented earlier.  The qErr store matches by GPS time-of-week, but
if the target TOW is off by 1 second (same leap-second contamination
as `_target_timescale_sec`), the qErr from the previous second gets
applied — decorrelating it from the PPS edge it should correct.

The litmus test is a very sensitive indicator: when qErr is properly
correlated, the variance ratio should be >> 1.0 (2-3x improvement).
At 1.0x it's obviously wrong.  Use short runs (~60s) to iterate.

Key files:
- `scripts/peppar_fix_engine.py` `_servo_epoch()` ~line 1521: qErr match
- `scripts/realtime_ppp.py` `serial_reader()` ~line 384: TIM-TP extraction
- `scripts/peppar_fix_engine.py` `QErrStore.match_gps_time()`: the matcher

## What was accomplished this session

### ADJ_SETOFFSET — 10,000x better PHC stepping
- E810: ±2 ns residual (was ±20 ms with clock_settime)
- i226: ±2 µs residual (was ±900 µs)
- Now the default.  Optimal stopping clock_settime is automatic fallback.
- No lag compensation needed.  phc_step_threshold_ns reduced to 10 µs.

### igc driver TX timestamp race bug
- Root cause: igc_ptp_adjfine_i225() writes TIMINCA without lock
- Reproducer: tools/igc_tx_timeout_repro.py (17s trigger)
- Fix: defer TIMINCA write when TX timestamps pending (25yr MTBF)
- Patch applied via DKMS on TimeHat.  adjfine() retries on EBUSY.
- Upstream email sent to intel-wired-lan with patch and reproducer.
- Workaround: lowered ptp4l logSyncInterval from -7 to 0 on TimeHat.

### E810 PEROUT working
- sysfs pin programming fallback (ice rejects PTP_PIN_SETFUNC ioctl)
- udev rule for dialout group write access to sysfs pins
- TICC #3 chA recording E810 disciplined PPS at 1 Hz
- First TICC TDEV: **344 ps at tau=1s, 2.2 ps at tau=1000s**

### Correlation gate fix
- recv_dt-first sort prevents drops from rounded_sec zero-crossing
- Overnight: gate_wait_obs=1220/20780 (6%) — improved from 11%
- epoch_offset=0 in overnight (was -1 in earlier runs)

### TDEV/ADEV analysis tool
- tools/plot_deviation.py: overlays PPS, PPS+qErr, PPS+PPP
- Reusable for any servo CSV comparison

### Position repeatability (PiPuss)
- 10/10 cold-start runs converged
- Scatter: N=28.7m, E=13.9m, U=46.7m (PPP broadcast eph baseline)

### Overnight data collected
- TimeHat: 20,780 epochs, 43,961 TICC (6.1 hours, clean)
- ocxo: 3,992 epochs, 8,525 TICC (2.4 hours, position watchdog)
- PiPuss: 10 converged positions in summary.json

## Known issues

### qErr correlation broken (TOP PRIORITY)
See above.  The litmus test detects it immediately.

### Position watchdog on ocxo
Trips after ~2.4 hours.  Vertical drift from uncompensated troposphere.
Needs: larger threshold, or troposphere state, or fixed-position mode.

### TimeHat missing epochs
1911/3600 in verification run despite 0 gate drops.  Unknown path.
See memory: project_timehat_missing_epochs.md.

### Correlation gate still drops ~6%
epoch_offset=0 now (was -1) but gate_wait_obs=1220 in overnight.
May be residual zero-crossing drops or a different issue.

## Commits pushed
Latest: 40eba2a (Add ADEV/TDEV analysis tool)
All on main, pushed to origin.

## Host state
- TimeHat: idle, igc patched (DKMS), ptp4l at 1 Hz sync
- ocxo: idle, stock ice driver, PEROUT via sysfs
- PiPuss: idle, position repeatability complete
