# Session Handoff — 2026-03-27

Long session covering queue diagnostics, ptp4l supervision, PHC
characterization, and servo tuning.  Two hour-long runs in progress
on TimeHat and ocxo.

## What was accomplished

### Queue depth monitoring and diagnostics
- High-water mark tracking for obs_queue, obs_history, pps_history
- Configurable depth threshold with diagnostic dump (--queue-depth-threshold, --queue-depth-dump)
- HWMs logged every 20 minutes and included in --gate-stats JSON

### Correlation gate fixes
- queue_remains=True drain limited to initialization only (was discarding
  ~50% of E810 epochs in steady state)
- Permanently unmatchable observations dropped immediately instead of
  blocking the FIFO for 11 seconds (startup obs/PPS simultaneous arrival)
- match_pps_event_from_history returns best_recv_dt_s for detection

### PHC divergence handling
- Engine exits code 5 when PPS error persists above track_restep_ns for 3 epochs
- Wrapper handles code 5: degrades clockClass, re-runs PHC bootstrap, restarts
- Bootstrap failure is now blocking with 3 retries (was silent "non-fatal")

### ptp4l clockClass supervision
- Three-layer failsafe: engine (Python UDS), wrapper (pmc command), systemd ExecStopPost
- Three-stage promotion: 248 (boot) → 52 (PHC initialized) → 6 (servo settled)
- Degradation: 6→52 (unsettled), 6→7 (holdover), any→248 (crash/diverge)
- Python PMC client talks directly to ptp4l's Unix domain socket
- Tested end-to-end on TimeHat with ptp4l domain 30
- Docs: docs/ptp4l-supervision.md, deploy/peppar-fix.service

### Holdover preserves adjfine
- No longer zeros adjfine on holdover entry — temperature-stable assumption
- PI servo seeded from preserved adjfine for bumpless re-acquisition
- Future: temperature/frequency curves for smarter holdover

### EXTTS verification at servo startup
- Engine verifies PPS events arrive within 3s before starting servo
- Returns exit code 3 (retryable) on failure
- Design doc: docs/extts-lifecycle.md

### Servo initialization from bootstrap
- **Critical bug fix**: PI servo was initialized at freq=0, discarding
  bootstrap's adjfine (~100 ppb on TimeHat).  Now passes bootstrap
  adjfine as initial_freq.  Convergence went from 15+ minutes to ~6 min.

### E810 PHC characterization
- Bimodal clock_settime: 1.6 ms typical, 16 ms ~30% of calls
- PPS-calibrated lag: ~16 ms (vs 200 µs on i226)
- Profile-driven step parameters: settime_lag_ns, step_error_ns, max_step_time_ms
- E810 bootstrap now converges (4 iterations to ±7 µs, was bouncing ±21 ms)
- Config path fix: profile resolution falls back to script-relative path

### Servo gain tuning
- i226: doubled gains (kp=0.01, ki=0.001) — settles in ~6 min
- E810: reduced gains (kp=0.005, ki=0.001) with 50 ms track_restep_ns
- E810 servo still diverges after ~30 epochs — needs further work

## Current host state

### TimeHat (running)
- 1-hour run in progress (started 00:16, ~30 min elapsed)
- Settled at ~6 minutes, PPS+PPP active, zero stalls
- Error oscillating ±2500 ns with slow convergence
- TICC logging to data/ticc-1hr.csv
- ptp4l clockClass management working (domain 30)
- Log: /tmp/timehat-1hr.log

### ocxo (running, in bootstrap)
- 1-hour run in progress (started 04:42)
- Currently in Phase 1 position bootstrap (σ=0.32m at 2 min)
- Position bootstrap is slow on I2C (~5s/epoch)
- E810 profile now loads correctly (config path fixed)
- Log: /tmp/ocxo-1hr.log

## What to do next (priority order)

### 1. Check hour-long runs
Both should complete by ~01:16 (TimeHat) and ~05:42 (ocxo).
TimeHat should show long-term servo behavior with PPS+PPP.
ocxo will test whether the E810 servo stabilizes over longer timescales.

### 2. Bootstrap frequency intercept design
User's idea: when PHC has large phase error (e.g. ±2 ms on E810),
bootstrap should intentionally set frequency to create a zero crossing
at a predictable time (~60 seconds).  The servo knows to expect this
and gently takes over at the intercept.  This avoids the servo having
to close a large phase error with its own gains.

Key insight: `true_freq = current_adjfine - pps_freq_ppb` gives sub-ppb
accuracy from 10-second PPS measurement.  Phase error ÷ desired
convergence time = intercept frequency offset.

### 3. E810 servo divergence
The E810 servo diverges because:
- Large initial phase error (±2 ms from bootstrap step)
- EKF dt_rx unstable during convergence (I2C epoch spacing varies)
- Servo corrections overcorrect before EKF stabilizes

Possible fixes:
- Bootstrap intercept (item 2) eliminates large phase error
- Increase EKF predict clamping for large dt
- Lower E810 gain_max_scale during initial convergence
- Add a warmup period where servo observes but doesn't correct

### 4. Kernel driver investigation
The stock Ubuntu 6.8 kernel has the old PAGE_SIZE buffering (not the
Schmidt streaming patches — verified by extracting the pristine source
tarball).  But empirical testing shows observations arrive at ~1s cadence.
The driver polls every 20ms and delivers the full page via gnss_insert_raw
after reading all available data.  The user's idle-timeout patch idea
may still be worth implementing for consistency, but isn't the blocker.

### 5. TDEV analysis
Once the hour-long TimeHat run completes with good servo data,
compare TDEV between PPS+qErr and PPS+PPP at tau = 1s, 10s, 100s.
The TICC data at data/ticc-1hr.csv provides sub-ns timestamps.

## Key files changed

| File | Changes |
|------|---------|
| scripts/peppar_fix_engine.py | Queue monitoring, EXTTS verify, servo init fix, pmc integration, exit code 5, config path fix |
| scripts/peppar_fix/correlation_gate.py | queue_remains init-only drain, permanently unmatchable detection, best_recv_dt_s |
| scripts/peppar_fix/pmc.py | NEW: Python PMC client for ptp4l clockClass management |
| scripts/peppar-fix | --pmc/--pmc-domain, clockClass management, bootstrap retry, exit code 5 handler |
| scripts/phc_bootstrap.py | Profile loading, fail-fast on missing PPS, pin programming logging |
| config/receivers.toml | Step characterization params, servo gain tuning for both platforms |
| docs/ptp4l-supervision.md | NEW: Three-layer clockClass supervision design |
| docs/extts-lifecycle.md | NEW: EXTTS initialization lifecycle design |
| docs/phc-initialization.md | E810 characterization data, platform comparison |
| deploy/peppar-fix.service | NEW: Example systemd unit with ExecStopPost failsafe |
