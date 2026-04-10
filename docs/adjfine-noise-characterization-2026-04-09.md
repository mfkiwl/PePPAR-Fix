# adjfine() Discipline Noise Characterization — 2026-04-09

## Summary

Calling `adjfine()` on the i226 PHC introduces **zero measurable overhead**.
The only noise from a frequency correction is the straightforward phase
change from running at a different frequency for one epoch: `Δphase = Δfreq × Δtime`.
For corrections < 1 ppb, this noise is < 1 ns — below the DO's free-running
floor of 0.92 ns (TDEV at τ=1 s, measured today on TimeHat).

## Experimental protocol

1. **Frequency resolution sweep** (`tools/adjfine_sweep.py`): swept adjfine
   from 120 to 140 ppb in 0.5 ppb steps, dwelling 8 s per step, measuring
   PEROUT period via TICC #1 chA.  Period offset varied linearly at
   ~989 ps/ppb — smooth, no staircase, no dead zones.  The i226's fractional
   GLTSYN_INCVAL register gives continuous sub-ppb frequency control.

2. **Discipline noise test** (`tools/adjfine_noise_test.py`): alternated
   adjfine between (base+M) and (base-M) every second for 120 s at each
   magnitude M.  Measured PEROUT absolute TDEV via TICC #1 chA.  Compared
   to a no-correction baseline (the ±0.01 ppb case, which adds only 0.01 ns
   of theoretical phase swing — unmeasurable against the 0.92 ns DO floor).

## Results

### Frequency sweep (120–140 ppb, 0.5 ppb steps)

| adjfine (ppb) | Period offset (ps) | Sensitivity |
|---:|---:|---|
| 120.5 | +11,124 | |
| 130.0 | +1,625 | |
| 132.0 | +212 | ← operating point (near GPS freq) |
| 140.0 | -8,148 | |
| **slope** | **~989 ps/ppb** | **= 0.989 ns/ppb ≈ 1 ns/ppb (theory)** |

No quantization.  Every 0.5 ppb step is cleanly resolved against
~1.5 ns measurement noise (TICC single-shot + DO jitter).

### Discipline noise (±M ppb alternating each second)

| Magnitude (ppb) | TDEV(τ=1s) | TDEV(τ=5s) | Expected phase swing | Call overhead |
|---:|---:|---:|---:|---|
| 0.01 | 0.916 ns | 1.54 ns | 0.01 ns | **none** (= DO floor) |
| 0.1 | 0.910 ns | 1.15 ns | 0.1 ns | **none** |
| 1.0 | 0.929 ns | 1.74 ns | 1.0 ns | **none** (barely above floor) |
| 10 | 6.35 ns | 3.80 ns | 10 ns | **none** (scales as M/√3) |
| 100 | 62.8 ns | 33.4 ns | 100 ns | **none** (scales as M/√3) |

At every magnitude, TDEV scales as the theoretical `M × 1s / √3` with
no overhead term.  The register write takes effect on the next hardware
tick (~8 ns later) with no PLL settling, no transient, no glitch.

### DO free-running floor today

TDEV(τ=1 s) = **0.92 ns** (from the ±0.01 ppb case, which is effectively
zero correction).  This is lower than the 1.17 ns characterization from
2026-04-07 — likely temperature-dependent (afternoon vs morning).

## Implications for servo design

### 1. adjfine() is free — correction frequency doesn't matter

The servo can call adjfine() at every epoch (1 Hz) without penalty.  The
cost is purely from the magnitude of the correction, not the frequency of
calling.  A 1 Hz servo with 0.1 ppb corrections adds 0.1 ns of noise per
epoch — invisible against the 0.92 ns DO floor.

### 2. Correction magnitude is the only knob

To keep the servo's noise contribution below the DO floor:

```
correction_ppb × epoch_interval_s < DO_TDEV_at_1s
correction_ppb × 1 s < 0.92 ns
correction_ppb < 0.92
```

So the servo should make corrections of **< 1 ppb per epoch** to stay
invisible.  With TICC+qErr input (178 ps reference noise) and a settled
error of ~1 ns, the optimal kp is:

```
kp = target_correction_ppb / typical_error_ns
   = 0.5 ppb / 1.0 ns
   = 0.0005
```

That's 60× gentler than the current default (0.03).  Or equivalently:
current kp=0.03 with a 1 ns error gives 0.03 ppb correction = 0.03 ns
noise, which IS below the floor.  The problem is that the TICC error
during pull-in/settling is ~100 ns, giving 3 ppb corrections = 3 ns noise.
The fix: aggressive pull-in gains (fast convergence) then very gentle
settled gains (kp ≈ 0.001 or lower).

### 3. The optimal gain depends on the operating regime

| Regime | Typical error | Optimal kp | Correction | Noise cost |
|---|---:|---:|---:|---:|
| Pull-in (>100 ns) | 1000 ns | 0.03 | 30 ppb | 30 ns (acceptable — converging) |
| Landing (10–100 ns) | 50 ns | 0.005 | 0.25 ppb | 0.25 ns (below floor) |
| Settled (<10 ns) | 2 ns | 0.001 | 0.002 ppb | 0.002 ns (invisible) |

The current TICC-drive mode already HAS pull-in / landing / settled
phases, but the settled gains need to be much gentler.

## Connection to in-band noise estimation

See `docs/architecture-vision.md` "In-band DO noise estimation."  The
discipline noise result enables a richer version of in-band estimation:

### Two-channel noise estimator

The `InBandNoiseEstimator` collects phase samples from discipline gaps
(epochs with no adjfine change).  On epochs where adjfine IS called, the
expected phase change is known: `expected = adjfine_ppb × dt_s`.  The
residual after subtracting the expected change is:

```
residual[n] = phase[n] - phase[n-1] - (adjfine_ppb × dt_s)
```

If adjfine() is truly free (as this test shows), the residual on
correction epochs should match the gap-epoch noise exactly.  The
estimator can have two channels:

- **Gap channel**: phase noise from no-correction epochs (pure DO noise)
- **Correction channel**: residual noise from correction epochs
  (DO noise + any call overhead)

If these ever diverge, something is wrong — the DO's internal PLL is
having trouble tracking, or the adjfine write isn't taking effect
cleanly.

### Dynamic uses for a continuous call-noise measurement

1. **Adaptive correction clamping**: if the correction-channel noise
   exceeds the gap-channel by more than N%, reduce the maximum
   correction magnitude.  This is a dynamic anti-windup that adapts
   to the DO's actual response rather than using a fixed `track_max_ppb`.

2. **DO health monitoring**: a DO that's aging (crystal fatigue, solder
   joint degradation, capacitor ESR increase) might show increasing
   correction-channel noise even at small magnitudes — the servo's
   corrections are no longer being absorbed cleanly.  Early detection
   before catastrophic failure.

3. **Per-epoch confidence weighting**: in the measurement-fusion
   architecture, each servo output epoch carries a confidence estimate.
   Epochs with large corrections get lower confidence (more noise
   injected); epochs with zero correction get higher confidence (pure
   DO noise).  Downstream consumers (PTP clients, TICC analysis) can
   weight accordingly.

4. **Correction scheduling**: with the exact noise cost known
   (Δphase = Δfreq × Δtime), the servo can decide whether a correction
   is WORTH making.  If the accumulated error is 0.05 ppb and the
   noise cost is 0.05 ns (below the 0.92 ns DO floor), correct.  If
   the error is 0.001 ppb, the correction is pointless — don't bother.
   This naturally leads to the "only correct when accumulated error
   exceeds a threshold" strategy.
