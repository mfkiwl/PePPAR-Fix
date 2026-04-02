# From PPP to PPP-AR

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

## Dependencies

- Phase bias SSR source (Galileo HAS IDD or single-AC NTRIP)
- Signal code mapping table (F9T u-blox IDs → RINEX codes)
- Phase bias application in observation processing
- AR algorithm (bootstrapping or LAMBDA)
- `--enable-ar` engine flag and logging
