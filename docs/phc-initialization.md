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

### Step target derivation

The value passed to `clock_settime()` is:

```
V = PHC_pps + (RT_now − RT_pps) + λ
```

Where:
- `PHC_pps` = what the PHC should read at the PPS edge (target_sec × 10⁹)
- `RT_pps` = `clock_gettime(CLOCK_REALTIME)` captured at PPS
- `RT_now` = `clock_gettime(CLOCK_REALTIME)` just before `clock_settime()`
- `λ` = mean `clock_settime()` call-to-PHC-landing lag (`--settime-lag-ns`)

**CLOCK_REALTIME as transfer standard:** The NTP phase error `ε` appears
in both `RT_pps` and `RT_now` and cancels in the subtraction:

```
RT_now − RT_pps = (UTC_now + ε) − (UTC_pps + ε) = UTC_now − UTC_pps
```

The residual error is CLOCK_REALTIME's *frequency* error (< 1 ppb from
NTP) times the transfer interval — negligible over seconds.  See
`docs/stream-timescale-correlation.md` Rule 6 for the full principle
and patrol guidance.

**Why λ matters:** `clock_settime()` doesn't land instantly.  The value
appears on the PHC hardware `λ` nanoseconds after the call.  We aim
`λ` into the future so the PHC reads the correct time at the moment
the write completes.

**Why the readback-based residual is biased:** The readback uses
`PTP_SYS_OFFSET` which has a systematic asymmetry — the system clock
midpoint doesn't perfectly correspond to the PHC snapshot.  On the
i226, this asymmetry is ~177 µs.  The characterization script
(`characterize_phc_step.py`) measures λ relative to its own readback,
so it reports ~23 µs — the remaining ~177 µs is invisible to it.
Only PPS (a hardware latch, no software asymmetry) reveals the true
combined lag.

**Calibrating λ from PPS:** Run bootstrap with several `--settime-lag-ns`
values and plot PPS verify error vs. lag.  The zero crossing is the
correct λ.  For the i226 on TimeHat: **λ ≈ 200 µs** (PPS verify
lands at ±5 µs).  The readback residual at this lag is ~+65 µs, so
`--step-error-ns` must be ≥ 200000 to avoid futile retries.

**Measured step accuracy (2026-03-24):**

Neither NIC supports `PTP_SYS_OFFSET_PRECISE`; both use the
`PTP_SYS_OFFSET` fallback.

With PPS-calibrated lag (λ=200 µs on i226):

| NIC | Step PPS verify | Freq after correction | Bless on 2nd run? |
|-----|----------------|----------------------|-------------------|
| i226 (TimeHat) | ±5 µs | 0.0 ±1.5 ppb | Yes |
| E810 (ocxo) | Not yet calibrated | — | — |

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
