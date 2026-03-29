# Session Handoff — 2026-03-29 (overnight)

Major session: ADJ_SETOFFSET discovery, igc driver bug, PEROUT on E810.

## What was accomplished

### ADJ_SETOFFSET for PHC stepping
- Discovered ADJ_SETOFFSET gives **±2 ns precision on E810** and
  **±2 µs on i226** — 10,000x better than clock_settime.
- E810 bimodality was entirely in the clock_settime path.  Gone with
  ADJ_SETOFFSET.  No lag compensation needed.
- Now the default PHC step method.  Optimal stopping clock_settime
  retained as automatic fallback.
- phc_step_threshold_ns reduced to 10 µs on E810 (was 2 ms).

### igc driver TX timestamp race bug
- **Root cause**: igc_ptp_adjfine_i225() writes IGC_TIMINCA without
  any lock, racing with hardware TX timestamp capture when ptp4l sends
  sync packets.  At 128 Hz sync: ~30 min MTBF.
- **Reproducer**: tools/igc_tx_timeout_repro.py triggers in 17 seconds.
- **Fix**: defer TIMINCA write when TX timestamps pending.  Extends
  stress MTBF from 17s to 43s, real-world to ~25 years.  Applied via
  DKMS on TimeHat.
- **EBUSY retry**: adjfine() now retries up to 50 times on EBUSY from
  the patched driver.
- **Workaround**: lowered ptp4l logSyncInterval from -7 (128 Hz) to 0
  (1 Hz) on TimeHat.
- **Upstream**: email sent to intel-wired-lan with patch and reproducer.

### E810 PEROUT via sysfs
- ice driver rejects PTP_PIN_SETFUNC ioctl but accepts sysfs writes
  to /sys/class/ptp/ptpN/pins/SMA1.
- Bootstrap now auto-programs SMA1 for PEROUT via sysfs fallback.
- udev rule deployed on ocxo for dialout group write access.
- TICC #3 chA now records E810 disciplined PPS at 1 Hz.

### Correlation gate fix
- recv_dt-first sort prevents observation drops from rounded_sec
  zero-crossing.  gate_wait_obs: 11% → 0.03% in verification run.
- Some residual drops at 6% over the overnight run — the fix helps
  but doesn't fully eliminate the fundamental epoch_offset=-1 issue.

### Receiver initialization unified
- peppar_ensure_receiver.py replaces peppar_rx_config.py in wrapper.
- Single entry point: dual-freq check + signal config + L5 health +
  warm restart + message routing + rate config.
- Detected driver name flows to peppar_find_position.py via --receiver.
- PTP_DEV no longer defaults to /dev/ptp0 — must be explicit.

### Position repeatability (PiPuss)
- 10/10 cold-start runs converged (30 min each, ~5 hours total).
- Scatter (1σ): N=28.7m, E=13.9m, **U=46.7m**
- Mean altitude: 189m (vs ~200m truth)
- This is the PPP broadcast ephemeris baseline for PPP-AR comparison.

### Credentials cleaned up
- NTRIP password removed from CLAUDE.md.
- Pre-commit hook scans for credential patterns.

## Overnight data collected

### TimeHat (still running)
- peppar-overnight-20260328-2359: ~20k epochs, ~42k TICC samples
- gate_wait_obs=1220 (~6% drop from epoch_offset=-1 issue)
- ptp4l at 1 Hz sync (was 128 Hz — lowered for igc bug mitigation)
- Should finish ~6:20 AM CDT

### ocxo
- peppar-overnight-20260329-0344: 3993 epochs, 8525 TICC samples
- Stopped at ~2.2 hours: POSITION WATCHDOG ALARM (vertical drift)
- peppar-overnight2-20260329-0646: 941 epochs (restart, also watchdog)
- The first run's data is usable for TDEV at tau up to ~1000s

### PiPuss
- data/pos-repeat-20260328-2255/summary.json — 10 converged positions

## Known issues to investigate

### epoch_offset = -1 (TimeHat)
All epochs show epoch_offset=-1.  This is a systematic 1-second offset
between target_sec and PPS rounded_sec, likely from leap second
contamination in gps_time.timestamp().  The correlation gate handles
it but the rounded_sec zero-crossing still drops ~6% of observations.
Root fix: correct _target_timescale_sec or match by recv_dt only.

### Position watchdog on ocxo
The FixedPosFilter's position drifts enough to trigger the 100m
watchdog after 2+ hours.  Likely the same vertical bias from
uncompensated troposphere.  May need: larger watchdog threshold,
troposphere state in the filter, or a known-position mode that
bypasses the watchdog.

### TimeHat missing epochs
1911/3600 consumed despite 0 gate drops in verification run.
Unknown drop path — needs instrumentation.

## Commits pushed (since last handoff)

Latest: f488ca0 (Retry adjfine on EBUSY from patched igc driver)
