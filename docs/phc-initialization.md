# PHC Initialization Design

## Goal

The servo should always start with a PHC that needs only minor
frequency and phase adjustments. All heavy lifting — position fix,
PHC frequency estimation, PHC phase stepping — happens in a bootstrap
phase before the servo begins.

## Orchestration

A top-level script (the existing `peppar-fix` shell wrapper or its
replacement) runs two phases:

### Phase 1: Bootstrap

**Cold start (no position file):**

1. Start broadcast ephemeris + SSR NTRIP streams
2. Start PPS capture on the PHC
3. Begin measuring PHC native frequency from PPS-to-PPS intervals
4. Run PPPFilter (full position + clock EKF) until position converges
   (~30-90s with GPS+GAL and NTRIP broadcast ephemeris)
5. The filter's clock state (`dt_rx`) gives us true time relative to
   GNSS, good to a few ns by the time position converges
6. Save position to file
7. Characterize and set the PHC (see below)

**Warm start (position file exists):**

1. Load position from file
2. Start ephemeris + SSR streams
3. Start PPS capture, begin measuring PHC frequency from PPS intervals
4. Run FixedPosFilter for 5-10 epochs (~5-10s) to get a fresh clock
   estimate and sanity-check the stored position
5. Evaluate PHC frequency and phase; intervene only if insane (see below)
6. Leave the drift file alone unless we intervened

### Phase 2: Servo

Starts with:
- Phase error bounded by step accuracy: ~5 µs (i226) or ~30 µs (E810)
- Frequency error < 5 ppb (from drift file, PPS measurement, or
  temperature curve)
- Valid position on disk

No warmup phase, no step logic, no cooldown. PI frequency tracking
from epoch 1.

## PHC evaluation and intervention

After obtaining a fresh clock estimate (either from full PPP
convergence in cold start, or 5-10 FixedPosFilter epochs in warm
start), bootstrap evaluates the PHC:

### Frequency

Sources of frequency information, in priority order:
1. PHC readback (current adjfine setting, via clock_adjtime or similar)
2. PPS-to-PPS interval measurement (4-9 samples from bootstrap epochs)
3. Last adjfine from drift file (saved at previous servo shutdown)
4. Temperature-based estimate from characterization file (P4 feature)

**Heuristic:** If the PHC readback frequency agrees with the drift file
and PPS measurement within a few ppb, leave frequency alone — the
integrator state from the last servo run is better than any fresh
estimate. A brief bootstrap phase cannot produce better integrator
state than hours of prior disciplining.

Only set frequency when it is clearly wrong. Only update the drift
file when frequency was changed. Phase and frequency are evaluated
and intervened on independently: a bad phase does not force a
frequency reset, and vice versa. This preserves whichever state
is still good.

### Phase

Compare the filter's clock estimate to the PHC's PPS timestamp:
- Filter gives `dt_rx` (receiver clock offset from true GNSS time)
- PHC EXTS gives `phc_sec, phc_nsec` (PHC's reading of the PPS edge)
- Combined with the known RAWX epoch time and target timescale (TAI),
  we know exactly where the PHC should be and where it is

**Decision:**
- If |phase_error| < step_uncertainty: don't step (quick reboot case)
- If |phase_error| >= step_uncertainty: step the PHC, compensating for
  the measured mean latency of clock_settime(), retrying within a time
  budget until the target error is met or time runs out

`step_uncertainty` comes from PHC characterization (see below).

**Step retry parameters** (CLI with defaults):
- `--max-step-time-ms` (default: 500): maximum wall time to spend
  retrying the step. Each attempt takes ~6 µs (i226) or ~1-17 ms
  (E810), so this allows many retries.
- `--target-step-error-ns` (default: 5000): stop retrying when the
  readback residual is within this bound. If the time budget expires
  first, accept the best attempt so far.

The step function repeatedly sets the PHC and reads it back. Each
`clock_settime` overwrites the PHC — there is no "keeping the best."
If the readback meets the target, stop and accept. If not, try
again (the previous result is already gone). If the time budget
expires, we're stuck with whatever the last attempt left.

This means the target must be realistic for the NIC. Too tight and
we timeout, stuck with a random last attempt that could be worse
than what we'd have gotten with a looser target. Too loose and we
accept on the first try without benefiting from retry.

**Measured step accuracy (2026-03-24):**

Neither NIC supports `PTP_SYS_OFFSET_PRECISE`; both use the
`PTP_SYS_OFFSET` fallback.

With retry (target=5000 ns, budget=500 ms):

| NIC | Hit rate | Avg attempts | Mean residual | Stdev |
|-----|----------|-------------|---------------|-------|
| i226 (TimeHat) | 100% | ~1 | +216 ns | 2.2 µs |
| E810 (ocxo) | 100% | 5.3 | +1,397 ns | 2.8 µs |

With tighter target (500 ns, budget=500 ms):

| NIC | Hit rate | Avg attempts | Timeout residual |
|-----|----------|-------------|-----------------|
| i226 (TimeHat) | 100% | 4.9 | — |
| E810 (ocxo) | 70% | 34.1 | mean 19 µs, max 25 µs |

The 500 ns target is too tight for the E810 — 30% of trials
timeout and land at ~19 µs, worse than the 5 µs target. The
5000 ns target is the right default: both NICs hit it 100% of the
time. Per-PHC override can tighten this when characterization
shows the NIC can do better (i226 can reliably hit 500 ns).

## PHC characterization

### What we're measuring

The precision with which userspace can set and read the PHC time.
This determines the lower bound on how accurately we can transfer
our GNSS-derived time to the PHC.

### Method

No TICC or external reference needed. Pure userspace-to-hardware
round-trip:

1. Read PHC time via `PTP_SYS_OFFSET_PRECISE` (or `PTP_SYS_OFFSET`
   if precise not supported). This gives the PHC time relative to
   CLOCK_MONOTONIC with sub-µs accuracy.
2. Compute a target PHC time.
3. Set the PHC via `clock_settime()`.
4. Read the PHC again.
5. Residual = (actual PHC after set) - (target PHC time).
6. Repeat N times (e.g. 100 trials with random target offsets).
7. Record mean and variance of residual.

**Mean residual:** Systematic latency from `clock_settime()` call to
hardware register update. Deterministic, compensatable. Expected to
be a few µs.

**Variance:** The limit on step accuracy. If sub-µs (expected for
direct PTP ioctl path), we can transfer phase to ~100-500 ns after
mean compensation.

### Reading the PHC

`PTP_SYS_OFFSET_PRECISE` does a single atomic cross-timestamp between
the PHC and system clock inside the NIC hardware. Best accuracy.
The E810 supports this (ART cross-timestamping). The i226 may only
support `PTP_SYS_OFFSET`, which takes multiple samples and picks the
tightest pair — noisier but still adequate.

Check support: `ioctl(fd, PTP_SYS_OFFSET_PRECISE2, ...)` — if it
returns EOPNOTSUPP, fall back to `PTP_SYS_OFFSET`.

### Output

Per-host, per-NIC characterization file (e.g.
`data/phc_char_timehat_ptp0.json`):

```json
{
  "host": "TimeHat",
  "phc": "/dev/ptp0",
  "nic": "i226",
  "method": "PTP_SYS_OFFSET",
  "n_trials": 100,
  "mean_residual_ns": 2283.0,
  "variance_ns2": 50000.0,
  "stdev_ns": 223.6,
  "step_uncertainty_ns": 500,
  "timestamp": "2026-03-24T..."
}
```

`step_uncertainty_ns` = a conservative bound (e.g. mean + 3*stdev)
used by bootstrap to decide whether to step.

### Recharacterization

Run the characterization tool:
- On first deployment to a new host
- After NIC firmware updates
- After kernel updates that might affect PTP ioctl latency
- Periodically if step residuals during operation look worse than
  expected

## Drift file

Similar to ntpd's `/etc/ntp.drift`:

```json
{
  "adjfine_ppb": 82.3,
  "temperature_c": 42.1,
  "timestamp": "2026-03-24T...",
  "host": "TimeHat",
  "phc": "/dev/ptp0"
}
```

Written by the servo on clean shutdown. Read by bootstrap on warm
start. If the file is stale (hours old) and temperature has changed
significantly, the frequency estimate may be off by more than a fresh
PPS measurement — bootstrap should prefer PPS-measured frequency in
that case.

## Temperature-frequency characterization (P4)

Future: continuously log adjfine vs temperature during servo
operation. Build a per-host, per-NIC curve. Use it to predict
initial frequency from current temperature when the drift file is
stale. This is software TCXO compensation.

## Cold start time to lock

Expected timeline for cold start (no position, no drift file):

| Time | What happens |
|------|-------------|
| 0s | Start NTRIP streams, begin PPS capture |
| 2-5s | Broadcast ephemeris warm (GPS+GAL) |
| 5s | First RAWX epoch, LS init → clock good to ~100 ns |
| 5-10s | Could step PHC here if only time matters |
| 30-90s | Position converges to < 0.5m sigma |
| 90s | Save position, set PHC frequency + phase, start servo |

For warm start with valid position file:

| Time | What happens |
|------|-------------|
| 0s | Load position, start NTRIP, begin PPS capture |
| 2-5s | Broadcast ephemeris warm |
| 5s | First FixedPosFilter epoch, clock good to ~100 ns |
| 10s | 5 epochs done, evaluate PHC, step if needed |
| 12s | Start servo |

## Relationship to existing code

### What stays
- `peppar_find_position.py` — cold start position bootstrap
- `peppar_fix_engine.py` — steady-state servo (Phase 2)
- `solve_ppp.py` — PPPFilter and FixedPosFilter
- `peppar_fix/ptp_device.py` — PHC ioctl access

### What changes
- `peppar-fix` wrapper script — orchestrates bootstrap → servo
- `characterize_phc.py` — rewrite to use PTP_SYS_OFFSET_PRECISE,
  no TICC, measure clock_settime() latency
- New: warm start PHC evaluator (5-10 epoch FixedPosFilter run
  with PHC comparison)
- New: drift file read/write
- `peppar_fix_engine.py` — remove all step/restep/warmup logic;
  servo starts in tracking mode unconditionally

### What's new
- `phc_bootstrap.py` or similar — PHC frequency + phase initialization
- Drift file management
- PHC characterization file management

## Open questions

- Does the i226 support PTP_SYS_OFFSET_PRECISE?
- What is the actual clock_settime() variance on TimeHat and ocxo?
- Can we read the current adjfine setting back from the PHC?
- How do we handle the case where bootstrap's PPP clock estimate
  disagrees with the drift file by more than expected? (Stale drift
  file? Changed oscillator? Wrong position?)
