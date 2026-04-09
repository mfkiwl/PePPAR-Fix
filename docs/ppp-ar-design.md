# From PPP to PPP-AR

> **Future direction**: see `docs/architecture-vision.md` — AR is an
> extension of AntPosEst, feeding cm-level positions back to DOFreqEst.

Design for adding integer ambiguity resolution to peppar-fix.

## What we have today (float PPP)

The PPPFilter in `solve_ppp.py` estimates float ambiguities as real-
valued states in the EKF.  Each tracked satellite gets an ambiguity
state initialized from `pseudorange - carrier_phase`.  The filter
converges the ambiguity toward its true value over time, but never
constrains it to an integer.

The FixedPosFilter in `solve_ppp.py` uses time-differenced carrier
phase, which cancels ambiguities entirely — no ambiguity states at all.
This is simpler and robust but gives up the precision that resolved
integer ambiguities would provide.

Current SSR source (SSRA00BKG0) provides orbit + clock + code bias
but **zero phase biases**.  Without phase biases, the float ambiguities
absorb an unknown offset that includes the satellite's phase bias
contribution.  The ambiguities are not integer-valued and cannot be
resolved.

## What PPP-AR adds

With phase biases from a single analysis center, the float ambiguity
estimates become close to integer values (within the filter's noise).
PPP-AR rounds them to integers once the float estimates are confident
enough, then fixes them — removing the ambiguity states and
substituting exact integer values.

Benefits:
- **Faster convergence**: float PPP needs 30-60 min to converge
  ambiguities.  PPP-AR can fix ambiguities in 7-14 min once enough
  satellites are tracked continuously.
- **Better precision**: fixed integer ambiguities eliminate a major
  noise source in the carrier-phase measurement model.  For time
  transfer (dt_rx estimation), this means lower-noise clock solutions.
- **More robust**: once fixed, integer ambiguities are exact — they
  don't drift or accumulate noise.  Partial fixing (some satellites
  fixed, others float) still helps.

## What's needed

### 1. Phase bias corrections

Each SSR source's phase bias tells you the fractional-cycle offset the
analysis center applied when computing the satellite clock correction.
Subtracting this from the observed carrier phase makes the remaining
ambiguity integer-valued.

**Available sources with phase biases:**

| Source | Mount | GPS | GAL | BDS | Notes |
|--------|-------|-----|-----|-----|-------|
| CAS | `SSRA01CAS1` | Yes | Yes | Yes | On products.igs-ip.net |
| CNES | `SSRA00CNE1` | Yes | Yes | Yes | On products.igs-ip.net |
| WHU | `SSRA00WHU1` | Yes | Yes | Yes | Needs `OSBC00WHU1` too |
| **Galileo HAS** | IDD mount | Yes | Yes | No | Free, separate registration |

**Critical constraint**: all corrections (orbit, clock, code bias,
phase bias) must come from the **same analysis center**.  Mixing ACs
destroys the integer nature because each AC partitions the satellite
clock / phase bias differently.  The combined IGS stream (SSRA00BKG0)
cannot be used for AR.

### 2. Phase bias application in the filter

Currently `solve_ppp.py` computes the ionosphere-free carrier phase:

```python
phi_if_m = (f1**2 * phi1 - f2**2 * phi2) / (f1**2 - f2**2)
```

For PPP-AR, the phase biases must be applied to each frequency before
forming the IF combination:

```python
pb1 = ssr.get_phase_bias(sv, signal_code_1)  # meters
pb2 = ssr.get_phase_bias(sv, signal_code_2)  # meters
phi1_corrected = phi1 - pb1 / wavelength1     # cycles
phi2_corrected = phi2 - pb2 / wavelength2     # cycles
phi_if_m = IF_combination(phi1_corrected, phi2_corrected)
```

After this correction, the ambiguity `N = phi_if_m - rho` (where rho
is the geometric range + clocks + troposphere) should be close to an
integer number of IF wavelengths.

The `SSRState` class in `ssr_corrections.py` already parses phase bias
messages and stores them via `_parse_phase_bias()`.  The
`get_phase_bias(prn, signal_code)` method exists and returns the bias
in meters.  The code path from SSR message to stored bias is complete
— it just isn't consumed by the filter yet.

### 3. Ambiguity resolution algorithm

Once float ambiguities are close to integers (typically after 5-10
minutes of continuous tracking), we can attempt to fix them.

**LAMBDA method** (standard approach):
1. Extract the float ambiguity vector and its covariance from the EKF
2. Apply integer least-squares (ILS) via the LAMBDA algorithm to find
   the most likely integer combination
3. Validate: compute the ratio of the second-best to best integer
   candidate.  If ratio > threshold (typically 2-3), accept the fix.
4. Apply: replace float ambiguity states with fixed integers, reduce
   the state vector, update the covariance.

**Simpler bootstrapping approach** (for initial implementation):
1. For each satellite, check if |N_float - round(N_float)| < threshold
   (e.g., 0.15 cycles)
2. If the ambiguity's formal sigma (from EKF covariance) is also small
   (e.g., < 0.1 cycles), fix it to the nearest integer
3. Fix satellites one at a time, re-evaluate after each fix

The bootstrapping approach is simpler to implement and adequate for
timing (where we have a known position and only need dt_rx).  Full
LAMBDA is better for position estimation.

### 4. Signal code mapping

The SSR phase biases are indexed by RINEX signal codes (e.g., "1C" for
GPS L1 C/A, "5Q" for GPS L5 Q).  The F9T observation data uses u-blox
signal IDs.  A mapping table is needed:

```python
F9T_TO_RINEX = {
    'GPS-L1CA': '1C',  'GPS-L2CL': '2L',  'GPS-L5Q': '5Q',
    'GAL-E1C': '1C',   'GAL-E5aQ': '5Q',  'GAL-E5bQ': '7Q',
}
```

This mapping exists implicitly in the observation processing but needs
to be explicit for phase bias lookup.

## Implementation plan

### Phase 1: Single-AC SSR source

Switch from the combined SSRA00BKG0 to a single-AC source that
includes phase biases.

1. Register for Galileo HAS IDD (or request access to CAS/CNES on
   products.igs-ip.net)
2. Add the new SSR mount as `--ssr-mount` in the host config
3. Verify phase biases arrive: `ssr.summary()` should show non-zero
   phase bias count
4. Run float PPP with the new source and confirm clock solutions are
   unchanged (phase biases are unused at this stage, but orbit/clock
   from a single AC may differ slightly from the combined product)

**Validation**: compare dt_rx estimates from the single-AC source
against the existing combined source over 24 hours.  They should agree
within ~0.5 ns.

### Phase 2: Phase bias application

Apply phase biases to carrier-phase observations before forming the
IF combination.

1. Add `apply_phase_bias()` to the observation processing in
   `realtime_ppp.py` (or `solve_ppp.py`), using the signal code
   mapping to look up biases
2. Verify that float ambiguity estimates shift toward integer values:
   log `N_float mod 1` for each satellite.  Without phase biases, this
   is uniformly distributed.  With phase biases, it should cluster
   near 0.0 (integer)
3. Monitor convergence: the histogram of `N_float mod 1` should narrow
   over time as the filter converges

**Validation**: plot `N_float mod 1` for each satellite over a 2-hour
run.  With phase biases correctly applied, values should converge
to within ±0.2 of an integer within 10-15 minutes.

### Phase 3: Integer fixing

Add ambiguity resolution to the filter.

1. Implement bootstrapping AR: for each satellite, check if the float
   ambiguity is within 0.15 cycles of an integer AND formal sigma
   < 0.1 cycles.  Fix it.
2. Add `--enable-ar` flag to the engine.  When enabled, attempt
   fixing every epoch after the initial convergence period.
3. Track fix rate: log how many satellites are fixed vs floating
4. Once fixed, remove the ambiguity state and use the integer value
   as a known constant in subsequent measurement updates

**Validation**: compare dt_rx noise between float-only and AR-enabled
runs.  AR should reduce dt_rx noise (visible in the servo's
`source_confidence_ns`).  The improvement should be most visible after
the first 10-15 minutes when ambiguities begin fixing.

### Phase 4: Servo impact

Measure the end-to-end timing improvement.

1. Run disciplined servo with float-only PPP, log TICC chA TDEV
2. Run disciplined servo with PPP-AR, log TICC chA TDEV
3. Compare TDEV at tau = 10-1000s (where PPP corrections dominate
   over oscillator noise)

**Validation**: PPP-AR should show lower TDEV at tau > 10s compared
to float PPP.  The improvement may be modest if the servo bandwidth
is already limited by the oscillator's short-term noise (the TCXO at
1.17 ns TDEV(1s) is the floor regardless of correction quality).

## Testing that PPP-AR works

### Test 1: Phase bias availability

```python
# After connecting to single-AC SSR source:
print(ssr.summary())
# Should show: "N phase bias" with N > 0
# Example: "49 orbit, 49 clock, 97 code bias, 49 phase bias"
```

### Test 2: Ambiguity integrality

```python
# In the PPP filter, after convergence (>10 min):
for sv, idx in filt.sv_to_idx.items():
    N_float = filt.x[N_BASE + idx]
    sigma = np.sqrt(filt.P[N_BASE + idx, N_BASE + idx])
    frac = N_float - round(N_float)
    print(f"{sv}: N={N_float:.3f}, σ={sigma:.3f}, frac={frac:+.3f}")
# With phase biases: |frac| < 0.2 for most satellites after 10 min
# Without phase biases: |frac| uniformly distributed in [-0.5, 0.5]
```

### Test 3: Fix rate

After enabling AR, track the fraction of satellites with fixed
ambiguities:

```
Epoch 600 (10 min): 0/8 fixed (too early)
Epoch 900 (15 min): 5/9 fixed (AR engaging)
Epoch 1800 (30 min): 8/10 fixed (steady state)
```

A healthy PPP-AR solution fixes >70% of satellites after 15-20 min.

### Test 4: Clock noise reduction

Compare `source_confidence_ns` in the servo CSV between float-only
and AR-enabled runs over 2+ hours:

| Metric | Float PPP | PPP-AR |
|--------|-----------|--------|
| Mean confidence | ~0.12 ns | ~0.05 ns (expected) |
| dt_rx noise (1s) | ~0.3 ns | ~0.1 ns (expected) |

### Test 5: TDEV comparison

TICC chA (disciplined PEROUT) TDEV with float vs AR, both using the
same oscillator and measurement setup:

- At tau=1-5s: no difference (oscillator-limited)
- At tau=10-100s: AR should show lower TDEV (better corrections)
- At tau>300s: both converge to the same long-term stability

## Risks and unknowns

### Single-AC availability

We're adding a dependency on one specific SSR source.  If CAS or CNES
goes down, we have no AR.  Mitigation: support fallback to combined
SSR (float-only) when single-AC is unavailable.

### Phase bias consistency with our IF combination

Our IF combination uses specific signal pairs (L1/L5 on TimeHat,
L1/L2 on ocxo).  The phase biases must match these exact signals.
If the AC provides biases for L1C+L2W but we observe L1C+L5Q, we
need biases for both L1C and L5Q individually (not a combined bias).

### Half-cycle ambiguities

Some F9T carrier-phase measurements may have half-cycle ambiguities
(the `half_cyc` flag in RAWX).  These must be resolved before
attempting integer AR — a half-cycle offset makes the "integer"
actually N+0.5.  The current code already checks this flag but
doesn't always handle it correctly.

### FixedPosFilter (time differencing)

The FixedPosFilter used for clock discipline in Phase 2 of peppar-fix
uses time-differenced carrier phase, which cancels ambiguities.  AR
doesn't directly help this filter — the improvement would come from
switching to an undifferenced filter that uses fixed integer
ambiguities.  This is a larger change than just adding AR to PPPFilter.

For initial implementation, AR could be tested with PPPFilter in
Phase 1 (position bootstrap) to verify the algorithm works, then
adapted for clock estimation in a future undifferenced clock filter.

## AR module: unified architecture with background PPPFilter

### How it fits together

The AR module is **not** a separate component — it's a natural extension
of the PPPFilter that already runs in Phase 1.  The key insight
(2026-04-09 discussion): the same PPPFilter instance that bootstraps
the position in Phase 1 **survives** into Phase 2, continues refining
its position in the background at a slow cadence, and — once phase
biases arrive — resolves integer ambiguities.  As integers fix, the
position estimate tightens from decimeters to centimeters, and that
refinement feeds *gradually* back into FixedPosFilter's `known_ecef`.

The three-source position consensus (see `docs/future-work.md`) and
PPP-AR are the same thread, the same filter, the same state vector:

```
Phase 1 (bootstrap)
│  PPPFilter runs full-rate, converges to float position ~0.5 m.
│  No phase biases yet (or float-only with them).
│
├──▶ Phase 1 → Phase 2 transition
│    PPPFilter instance survives, passed to BackgroundPPPMonitor thread.
│    Position → known_ecef for FixedPosFilter.
│
Phase 2 (steady state)
│
│  FixedPosFilter runs full-rate: clock estimation at known_ecef.
│  Servo runs on Carrier/PPS+qErr as usual.
│
│  BackgroundPPPMonitor (same PPPFilter, slow cadence, ~30 s):
│   ├── float convergence continues (ambiguities tighten)
│   ├── consensus watchdog: bg_PPP_pos vs known_ecef vs NAV2-PVT
│   │
│   ├── [when phase biases become available from single-AC SSR]
│   │   apply_phase_bias() to observations before IF combination
│   │   float ambiguities cluster near integers
│   │
│   ├── [when integrality test passes for enough SVs]
│   │   bootstrapping AR: fix N_float → N_int for each ready SV
│   │   position estimate jumps to cm-level (the AR fix)
│   │
│   └── [gradual migration]
│       known_ecef += alpha * (ar_position - known_ecef)
│       each epoch, alpha ≈ 0.001 → ~50 min for 95% migration
│       FixedPosFilter sees position move by < 1 mm/epoch
│       dt_rx absorbs each shift smoothly (< 1 ps/s of phase migration)
│       servo sees nothing — well below noise floor
│
│  End result after ~60 min of Phase 2:
│   - Background PPPFilter has cm-level position (AR-fixed)
│   - FixedPosFilter's known_ecef has migrated to match
│   - dt_rx has < 100 ps of position-induced phase bias
│   - Servo has been running continuously, no step, no restart
```

### Why FixedPosFilter doesn't need AR itself

FixedPosFilter uses time-differenced carrier phase which **cancels
ambiguities by design** — no ambiguity states in the EKF at all.  This
is simpler, more robust, and avoids the whole AR complexity in the
hot path.  The trade-off is that FixedPosFilter's dt_rx estimate
inherits a *constant bias* from any error in `known_ecef`, because
position error maps directly to clock error through the satellite
geometry.

The AR module fixes this indirectly: it refines `known_ecef` in the
background until the position-induced bias drops below 100 ps.  The
FixedPosFilter's time-differenced architecture stays untouched.

### Gradual position feed-in: the math

When the background PPPFilter produces a new AR-fixed position
`ar_ecef`, migrate `known_ecef` with exponential blending:

```python
alpha = 0.001  # time constant = 1/alpha ≈ 1000 epochs = 1000 s at 1 Hz
known_ecef += alpha * (ar_ecef - known_ecef)
```

For a 5 m migration (typical float → AR jump):
- Migration rate: 5 m × 0.001 = 5 mm/epoch → phase = 5e-3 × cos(45°) / c ≈ 12 ps/epoch
- F9T PPS noise: ~2300 ps/epoch (σ at τ=1 s)
- Servo bandwidth: ~0.01 Hz (P = 100 s)
- The 12 ps/epoch migration is 200× below the measurement noise floor
  and 10000× below the servo's phase-correction rate.  Invisible.

At 95% convergence (5τ = 5000 epochs ≈ 83 min), `known_ecef` is
within 0.25 m of the AR position.  Phase bias: ~0.6 ns.  At 99%
(7τ ≈ 117 min), within 0.05 m → ~0.1 ns.  At 99.9% (~167 min),
within 5 mm → ~12 ps.

The alpha could be adaptive: use a faster alpha when confidence is
high (many fixed SVs, low formal sigma, large `known_ecef` error) and
slower when confidence is low.  Or just use a fixed alpha and let it
converge on its own timescale — the important thing is *no step*.

### Handling the AR fix event

When the bootstrapping AR first declares integers fixed, the background
PPPFilter's position estimate may jump by 5–50 cm in a single epoch
(the jump from float-to-fixed).  This is NOT fed directly into
`known_ecef` — the exponential blend absorbs it.  The jump appears as a
sudden change in `ar_ecef` that the blend then chases slowly.

If the AR fix is wrong (e.g., wrong integer on one satellite due to
multipath), the position will jump and then bounce when the bad integer
is detected.  The slow blend protects `known_ecef` from this bounce:
by the time the blend has moved 5% of the way to the wrong position,
the AR module will likely have detected and unfixed the bad satellite.

The blend acts as a natural low-pass filter on AR fix/unfix events.

### Interaction with three-source consensus

When AR is active, the consensus comparison becomes richer:

```
Δ_AR_vs_known  = | bg_PPP_AR_position - known_ecef |
Δ_NAV2_vs_known = | F9T_NAV2_PVT - known_ecef |
Δ_AR_vs_NAV2    = | bg_PPP_AR_position - F9T_NAV2_PVT |

case 1: Δ_AR_vs_known large, Δ_NAV2_vs_known ≈ 0, Δ_AR_vs_NAV2 large
        → AR is wrong (bad fix), known_ecef and NAV2 agree.
        Reset AR, keep FixedPos.

case 2: Δ_AR_vs_known ≈ Δ_NAV2_vs_known, both moderate (5-50 cm)
        → AR and NAV2 agree on a position different from known_ecef.
        This IS the expected AR convergence — known_ecef is the stale
        float value and both independent sources say the true position
        is nearby.  Increase blend alpha briefly to converge faster.

case 3: all three agree (Δ < 5 cm)
        → AR has converged AND known_ecef has migrated.  Steady state.
        Position-induced phase bias < 100 ps.  Log it.
```

This gives us a safety net for the AR fix itself: if the AR produces
a wrong fix, the F9T secondary engine (NAV2) will disagree and we can
reject it before it pollutes `known_ecef`.

### Revised implementation phases

(Supersedes the 4-phase plan above — same content but restructured
to flow from the architectural discussion.)

**Phase A — Single-AC SSR source with phase biases**

Switch from the combined BKG `SSRA00BKG0` (no phase biases) to a
single-AC source that provides them.  Best candidates (all verified
to have phase biases):
- CAS: `SSRA01CAS1` — 159 phase biases for GPS+GAL+BDS, confirmed
  working with our credentials on the Australian mirror
  (`ntrip.data.gnss.ga.gov.au:443`).
- CNES: `SSRA00CNE0` on `products.igs-ip.net:2101` — RTCM
  1265/1266/1267 confirmed in sourcetable.  Requires access to the
  `products.igs-ip.net` realm (Bob applied 2026-04-07; pending).

Action: update the SSR mount config, verify phase biases arrive,
confirm dt_rx is comparable to the combined product.  No code change
needed — `SSRState._parse_phase_bias()` already handles the RTCM
message types.

**Phase B — Phase bias application + integrality monitoring**

Apply phase biases in the observation processing path, before the IF
combination.  Monitor `N_float mod 1` per satellite.  With correct
phase biases, the histogram should collapse from uniform-in-[−0.5, 0.5]
to clustered near 0.

Action: `apply_phase_bias()` function in `solve_ppp.py` using the
signal-code mapping table.  Log `mean |N mod 1|` per epoch (the
"ambiguity integrality" metric already logged: see the overnight logs
`Ambiguity integrality: mean|frac|=0.283 (n=11, <0.15 = ready for AR)`
— this metric already exists in the engine for monitoring, it's just
that the threshold is never actionable because we don't have phase
biases yet).

**Phase C — Bootstrapping AR + gradual position migration**

Implement the simplest possible integer fixing: per-satellite threshold
test on `|N_float - round(N_float)| < 0.15` AND `σ_N < 0.1`.  Fix one
at a time.  When ≥ 70% of SVs are fixed, declare "AR convergence" and
begin the exponential position migration.

Action: AR module as an extension of PPPFilter (new method:
`filt.attempt_ar()`).  `BackgroundPPPMonitor` thread calls it every
N epochs.  Gradual migration: `known_ecef += alpha * (ar_pos - known_ecef)`
applied in the main thread's observation-processing loop.

**Phase D — Servo validation**

Measure the end-to-end phase improvement via TICC chA TDEV
comparison:
- Float-only PPP: TDEV at τ=100-1000 s shows whatever the Carrier
  source can deliver with current noise levels.
- PPP-AR: should show measurably lower TDEV at τ > 100 s where the
  position-induced phase bias was the limiting factor.
- The most dramatic improvement: the *constant offset* between TICC
  chA−chB should shrink from the current ~5-20 ns (position error)
  to < 1 ns (cm-level position), visible as a simple mean-shift in
  the time series.

## Dependencies

- Phase bias SSR source (CAS `SSRA01CAS1` or CNES `SSRA00CNE0`)
- Signal code mapping table (F9T u-blox IDs → RINEX codes)
- Phase bias application in observation processing
- AR algorithm (bootstrapping for initial implementation)
- BackgroundPPPMonitor thread (shared with position consensus)
- Gradual position migration in FixedPosFilter's observation loop
- `--enable-ar` engine flag and logging
- NAV2-PVT parsing (shared with position consensus, for AR validation)
