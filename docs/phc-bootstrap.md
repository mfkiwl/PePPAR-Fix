# PHC Bootstrap Design

## Goal

The servo should always start with a PHC that needs only minor
frequency and phase adjustments.  All heavy lifting — position fix,
PHC frequency estimation, PHC phase stepping — happens in bootstrap
before the servo begins.

"Position bootstrap" and "PHC bootstrap" are parallel concepts: both
produce a warm-start state from cold or stale inputs, and both run
before the engine's steady-state loop.

The name "bootstrap" is apt in the Baron Munchausen sense: we use the
PHC as a tool to set itself.  Optimal stopping repeatedly steps the
PHC and characterizes its step-error distribution in the process, using
the PHC's own readback to learn how to get the best result from it.

There is a dramatic asymmetry in achievable accuracy between the two
quantities bootstrap sets:

| Quantity | Accuracy | Units |
|----------|----------|-------|
| **Frequency** | ±1-2 ppb | 10-second PPS baseline, full-baseline measurement |
| **Phase (i226)** | ±100 µs (100 ppm) | Limited by clock_settime latency variance |
| **Phase (E810)** | ±1-4 ms (1000-4000 ppm) | Bimodal clock_settime, ~70% fast / ~30% slow |

Frequency is set 5-6 orders of magnitude more precisely than phase.
This is why the glide slope exists: rather than fight for better phase
accuracy (diminishing returns from the kernel path), accept the step
result and let the servo close the remaining error smoothly.

## Orchestration

The `peppar-fix` wrapper runs three phases:

1. **Receiver configuration** (`peppar_rx_config.py`)
2. **Position bootstrap** (`peppar_find_position.py`, cold start only)
3. **PHC bootstrap** (`phc_bootstrap.py`, if PTP device present)
4. **Servo** (`peppar_fix_engine.py`)

PHC bootstrap runs on both cold and warm boot paths.  It also runs
on PHC divergence (exit code 5) as a re-bootstrap.

### Cold start (no position file)

1. Position bootstrap: PPPFilter converges position (~30-90s)
2. PHC bootstrap: measures frequency, steps phase, sets glide slope
3. Servo starts

### Warm start (position file exists)

1. PHC bootstrap: loads position, measures frequency, evaluates phase
2. Intervenes only if phase or frequency is outside tolerance
3. Servo starts

## PHC bootstrap internals

`phc_bootstrap.py` runs these stages:

### 1. PPS frequency measurement

Captures N+1 PPS events (default N=10) and computes the total
fractional-second drift over the full N-second baseline:

```
freq_ppb = (last_nsec - first_nsec ± wrap) / N
```

Uses elapsed seconds (not absolute timestamps) to stay within float64
precision.  The full-baseline approach gives √N better precision than
per-interval averaging.

### 2. PPP clock solution

Runs FixedPosFilter for 10 epochs with the stored position to get a
fresh receiver clock estimate (`dt_rx`).  This sanity-checks the
stored position via residual magnitudes.

### 3. Phase and frequency evaluation

Captures one PPS event and computes total phase error (including any
whole-second offset) by using CLOCK_REALTIME as a transfer standard
to identify which GPS second the PPS belongs to.

**Frequency sane:** |PPS freq error| < `freq_tolerance_ppb` (default 10)
**Phase sane:** |phase error| < `step_error_ns` (platform-specific)

If both sane: bless without intervention, return 0.

### 4. Intervention

Three steps when phase or frequency is outside tolerance:

#### Step 1: Phase step with optimal stopping

The step error follows a log-normal distribution (fixed minimum kernel
path + multiplicative scheduling perturbations).  `step_to()` uses
parametric optimal stopping to find the best achievable step:

- **Observation phase** (first 1/e ≈ 37% of search budget): call
  `clock_settime()` repeatedly, measure readback residuals, learn the
  distribution.
- **Threshold**: 5th percentile of observation |residuals|.
- **Selection phase** (remaining 63%): accept the first step with
  |residual| at or below the threshold.

This self-adapts to any PHC without prior characterization.  On the
i226 (~20k attempts/s) the tight jitter produces a tight threshold.
On the E810 (~40 attempts/s) the bimodal latency means the observation
phase learns both modes and the threshold selects the better one.

Search time is configurable per profile (`search_time_s`): 1s for i226,
5s for E810 (the slow E810 clock_settime needs more candidates).

#### Step 2: PPS ground truth

One PPS event measures the true residual φ₀.  The readback used by
optimal stopping has a systematic bias from PTP_SYS_OFFSET asymmetry;
PPS is the hardware-latched truth.

#### Step 3: Glide slope

Set a PHC frequency that drives φ₀ toward zero at the rate the servo
expects for a near-critically-damped handoff:

```
glide_offset = -ζ · ωₙ · φ₀    [ppb, since 1 ppb = 1 ns/s]
```

Where:
- ωₙ = √Ki (servo natural frequency)
- ζ = `glide_zeta` (target damping ratio, default 0.7)
- φ₀ = true phase error from PPS

The total adjfine is `base_freq + glide_offset`.  The drift file stores
only `base_freq` (the on-rate frequency, excluding the transient glide).

The glide is clamped to `track_max_ppb` to stay within the servo's
control authority.  If the glide exceeds this limit, the servo would
saturate and the integral would wind up, destroying the smooth handoff.

**Why glide works:** The servo is initialized with the glide frequency
as `initial_freq`.  The PI servo's integral accumulator starts at
`-initial_freq / Ki`, so the servo "knows" about the glide from epoch 1.
As the phase approaches zero, the servo gradually removes the glide
offset.  The result is a single smooth approach rather than oscillatory
convergence.

**Zero-crossing time:** |φ₀| / |glide_offset| = 1/(ζ·ωₙ) ≈ 45s for
ζ=0.7, Ki=0.001.  This is independent of φ₀ — larger errors get
proportionally larger glide velocities.

## Step target derivation

The value passed to `clock_settime()` is:

```
V = PHC_pps + (RT_now − RT_pps) + λ
```

Where:
- `PHC_pps` = what the PHC should read at the PPS edge (target_sec × 10⁹)
- `RT_pps` = `clock_gettime(CLOCK_REALTIME)` captured at PPS
- `RT_now` = `clock_gettime(CLOCK_REALTIME)` just before `clock_settime()`
- `λ` = mean `clock_settime()` call-to-PHC-landing lag (`settime_lag_ns`)

**CLOCK_REALTIME as transfer standard:** The NTP phase error ε appears
in both RT_pps and RT_now and cancels in the subtraction.  The residual
is CLOCK_REALTIME's frequency error (< 1 ppb from NTP) times the
transfer interval — negligible over seconds.

## Platform-specific parameters

Neither NIC supports `PTP_SYS_OFFSET_PRECISE`; both use the
`PTP_SYS_OFFSET` fallback.

| Parameter | i226 | E810 |
|-----------|------|------|
| `settime_lag_ns` | 200,000 (200 µs) | 16,000,000 (16 ms) |
| `step_error_ns` | 10,000 (10 µs) | 2,000,000 (2 ms) |
| `search_time_s` | 1.0 | 5.0 |
| `track_kp` | 0.03 (ζ≈0.47) | 0.015 (ζ≈0.24) |
| `track_ki` | 0.001 | 0.001 |
| `glide_zeta` | 0.7 | 0.7 |
| `track_max_ppb` | 100,000 | 250,000 |

**E810 bimodal clock_settime (2026-03-27):**

The E810's `clock_settime()` has two latency populations:
- **Fast path (~70%):** 1.2–1.7 ms
- **Slow path (~30%):** 15–17 ms

The slow path corresponds to ice driver internal operations blocking
the PTP ioctl.  Optimal stopping naturally selects fast-path steps
by learning the bimodal distribution during the observation phase.

## Drift file

```json
{
  "adjfine_ppb": 82.3,
  "phc": "/dev/ptp0",
  "timestamp": "2026-03-27T..."
}
```

Written by PHC bootstrap after frequency correction.  Stores the
**base frequency** (on-rate adjfine), not the transient glide offset.
Read by bootstrap on warm start.  If the drift file is stale and
PPS measurement is reliable, bootstrap prefers the PPS-derived
frequency.

## Convergence timeline

**Cold start** (no position, no drift file):

| Time | Event |
|------|-------|
| 0s | Start NTRIP, begin PPS capture |
| 2-5s | Broadcast ephemeris warm |
| 30-90s | Position converges |
| 90s | PHC bootstrap: PPS freq (10s) + PPP (10s) + step (1-5s) + PPS verify (1s) |
| ~115s | Servo starts with glide; zero-crossing ~45s later |
| ~160s | Phase near zero, servo converging |

**Warm start** (position + drift file):

| Time | Event |
|------|-------|
| 0s | Load position, start NTRIP, begin PPS capture |
| 10s | PPS frequency measured |
| 15s | Broadcast ephemeris warm |
| 25s | PPP clock solution (10 epochs) |
| 26-31s | Phase step (1-5s) + PPS verify (1s) |
| ~32s | Servo starts with glide |
| ~77s | Phase near zero |
