# PPP-AR: Why IF Ambiguities Are Not Integer, and How to Fix Them

> Supersedes the Phase B/C integrality approach in `ppp-ar-design.md`.
> Written after the 2026-04-13 Phase B test confirmed mean|frac| = 0.25
> (random) on both TimeHat and MadHat despite correctly applied CNES
> phase biases.

## The problem

The PPPFilter's ambiguity state per satellite is the IF-combined
ambiguity in meters:

```
A_IF = alpha1 * lambda1 * N1 - alpha2 * lambda2 * N2
```

where alpha1, alpha2 are the IF combination coefficients and N1, N2 are
the true integer ambiguities on each frequency.  Even with perfect phase
biases (making N1 and N2 exactly integer), A_IF is **not** an integer
multiple of any single wavelength because alpha1*lambda1 and
alpha2*lambda2 are incommensurable.

The integrality check `|A_IF mod 1|` was testing an undefined quantity.
This is why mean|frac| = 0.25 (uniform random) regardless of phase bias
quality.

## The math: wide-lane / narrow-lane decomposition

Expand the IF ambiguity using N_WL = N1 - N2 (wide-lane, integer):

```
A_IF = c/(f1^2 - f2^2) * (f1*N1 - f2*N2)
     = c/(f1^2 - f2^2) * (f1*(N_WL + N2) - f2*N2)
     = c/(f1^2 - f2^2) * ((f1 - f2)*N2 + f1*N_WL)
     = c/(f1 + f2) * N2  +  c*f1/(f1^2 - f2^2) * N_WL
```

But it's conventional to express in terms of N1 (the narrow-lane
integer) rather than N2:

```
A_IF = lambda_NL * N1  +  alpha2 * lambda_WL * N_WL
```

where:
    lambda_NL = c / (f1 + f2)     narrow-lane wavelength
    lambda_WL = c / (f1 - f2)     wide-lane wavelength

Derivation: substitute N2 = N1 - N_WL into the expanded form.

### Numerical values

| Pair | lambda_NL | lambda_WL | alpha2 |
|------|-----------|-----------|--------|
| GPS L1/L5 | 10.70 cm | 75.15 cm | 1.794 |
| GAL E1/E5a | 10.70 cm | 75.15 cm | 1.794 |
| GAL E1/E5b | 10.94 cm | 81.44 cm | 1.671 |
| GPS L1/L2 | 10.70 cm | 86.19 cm | 1.545 |

The narrow-lane wavelength (~10.7 cm) is the precision target.  With
N_WL known, resolving N1 from the float IF ambiguity requires
|A_IF - true| < lambda_NL/2 = 5.35 cm, which the PPPFilter achieves
after a few minutes of convergence.

## Two-step ambiguity resolution

### Step 1: Wide-lane fixing via Melbourne-Wubbena

The Melbourne-Wubbena (MW) combination is geometry-free and
ionosphere-free:

```
MW = (f1*phi1 - f2*phi2)/(f1 - f2)  -  (f1*P1 + f2*P2)/(f1 + f2)
   = lambda_WL * N_WL  +  code_noise  +  phase_bias_residual
```

With SSR phase biases applied to phi1, phi2 before forming MW, the
phase_bias_residual vanishes and MW converges to lambda_WL * N_WL as
code noise averages down.

**Convergence**: code noise on MW is amplified by ~f1/(f1-f2), but
lambda_WL is large (75-86 cm).  Rounding MW/lambda_WL to the nearest
integer needs |code_noise| < lambda_WL/2 = 37-43 cm.  With typical
pseudorange noise of 0.3 m per epoch, averaging ~30 epochs (30 s)
gives std = 0.3/sqrt(30) = 5.5 cm, well within the rounding margin.

**Implementation**: per-satellite exponential filter on MW with tau = 60 s.
After 60 s, fix N_WL = round(MW_avg / lambda_WL).  Validate by checking
|MW_avg/lambda_WL - N_WL| < 0.25.

Wide-lane fixing is independent of the PPPFilter — it uses raw
per-frequency observables that are already available in the observation
processing loop.

### Step 2: Narrow-lane fixing from IF ambiguity

Once N_WL is fixed, extract N1 from the PPPFilter's float IF ambiguity:

```
N1_float = (A_IF_float - alpha2 * lambda_WL * N_WL) / lambda_NL
```

If N1_float is close to integer (|frac| < 0.15) AND the formal sigma
of A_IF is small enough (sigma_A / lambda_NL < 0.1), fix:

```
N1_fixed = round(N1_float)
A_IF_fixed = lambda_NL * N1_fixed  +  alpha2 * lambda_WL * N_WL
```

Then constrain the PPPFilter ambiguity state to A_IF_fixed (either
replace the state or add a very tight pseudo-observation).

### Corrected integrality diagnostic

The existing `mean|frac|` metric should compute:

```python
# WRONG (what we had):
frac = A_IF - round(A_IF)          # meaningless for IF ambiguity

# RIGHT:
N1_float = (A_IF - alpha2 * lambda_WL * N_WL) / lambda_NL
frac = N1_float - round(N1_float)  # should converge toward 0
```

This requires N_WL to be known first (Step 1).  Before wide-lane
fixing, the integrality check is not meaningful.

## What changes in the code

### No filter restructure needed

The PPPFilter stays as-is: IF observations, float ambiguity states.
The AR module sits alongside it, consuming the same raw observations
to form MW and using the filter's float ambiguity to resolve integers.

### New components

1. **MelbourneWubbenaTracker** (new class, ~80 lines)
   - Per-satellite exponential filter on MW
   - Input: per-frequency phi, P, wavelengths (already available at
     line 759-760 of realtime_ppp.py)
   - Output: N_WL_fixed per satellite, with confidence
   - Standalone: no dependency on PPPFilter states

2. **NarrowLaneResolver** (new class, ~60 lines)
   - Input: PPPFilter float ambiguity + N_WL_fixed
   - Computes N1_float, checks integrality + sigma
   - Fixes ambiguities one at a time
   - Applies fix to PPPFilter state

3. **Observation dict extension** (~5 lines changed)
   - Add `phi_f1_cyc`, `phi_f2_cyc`, `pr_f1_m`, `pr_f2_m`,
     `wl_f1`, `wl_f2` to the observation dict passed to the filter
   - Currently only `pr_if` and `phi_if_m` are passed through

4. **Integrality diagnostic fix** (~10 lines changed)
   - Use the corrected N1_float formula instead of raw A_IF mod 1
   - Only evaluate for satellites with fixed N_WL

### What does NOT change

- PPPFilter: state vector, predict, update — untouched
- FixedPosFilter: completely untouched (time-differenced, no ambiguities)
- Servo: untouched (better dt_rx flows automatically)
- NTRIP/SSR pipeline: already working (confirmed 2026-04-13)

## Data flow

```
Per epoch:
  Receiver → per-frequency observables (phi1, phi2, P1, P2)
      │
      ├─→ IF combination → PPPFilter.update()
      │       └── float A_IF per satellite
      │
      ├─→ MW combination → MelbourneWubbenaTracker.update()
      │       └── N_WL_fixed per satellite (after ~60 s)
      │
      └─→ NarrowLaneResolver.attempt()
              ├── input: A_IF_float, N_WL_fixed, sigma_A
              ├── compute N1_float per satellite
              ├── if |frac| < 0.15 and sigma < threshold:
              │     fix A_IF → constrain PPPFilter state
              └── log fix rate, integrality stats
```

## Validation plan

### Test 1: MW wide-lane convergence
Run with CNES SSR.  Log MW/lambda_WL per satellite.  After 60 s of
averaging, the fractional part should cluster near 0 for Galileo
(where both phase biases hit) and stay random for GPS f2 (where L5Q
bias is missing — CNES provides L5I).

### Test 2: Corrected integrality
After N_WL is fixed (Test 1 passes), compute N1_float and log
|N1_float mod 1|.  Should converge below 0.15 for Galileo satellites
within 5-10 minutes.

### Test 3: Integer fixing
Fix N1 for satellites passing the threshold.  Verify:
- Fixed ambiguity reduces post-fit residuals
- dt_rx noise decreases (compare fixed vs float epochs)
- Fix rate reaches > 70% of Galileo satellites after 15 min

### Test 4: End-to-end servo improvement
Compare TICC chA TDEV at tau = 100-1000 s between float-only and
AR-enabled runs.  Expect improvement at mid-tau where position error
currently limits accuracy.

## Implementation estimate

| Component | Lines | Depends on |
|-----------|-------|------------|
| MelbourneWubbenaTracker | ~80 | observation dict extension |
| Observation dict extension | ~15 | — |
| NarrowLaneResolver | ~60 | MW tracker, PPPFilter access |
| Integrality diagnostic fix | ~15 | MW tracker |
| Engine integration + flag | ~40 | all above |
| **Total** | **~210** | |

All new code.  No modifications to existing filter math.

## Risk: GPS L5 bias mismatch

CNES provides L5I phase biases; F9T tracks L5Q.  The I-vs-Q
inter-component bias is satellite-specific (~2 ns), so GPS cannot
do PPP-AR with the current SSR source.

Galileo is fully matched (L1C, L5Q, L7Q all hit).  Initial AR should
be Galileo-only.  GPS satellites still contribute to the float solution
(position + clock) but their ambiguities stay float.

This is fine for timing: Galileo-only AR still fixes ~8-10 ambiguities,
which constrains the receiver clock estimate well.  GPS contributes
geometry (many satellites) while Galileo contributes precision
(fixed ambiguities).
