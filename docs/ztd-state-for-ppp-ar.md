# Zenith Tropospheric Delay State for PPP-AR

**Date**: 2026-04-15
**Status**: Proposed
**Motivation**: Cross-host position agreement stuck at ~6m horizontal
despite correct NL integers (tight thresholds, frac < 0.10).

## The problem

Two receivers on the same antenna (via splitter) should converge to
the same position.  After 1 hour with NL fixing:

- Vertical agreement: **0.9m** — good, AR is helping
- Horizontal agreement: **5.0m** — dominated by longitude drift

Both hosts show the same pattern: longitude drifts west at ~5m/hour.
The float positions were closest at ~50 minutes (1.5m MadHat-clkPoC3),
then diverged as atmospheric conditions changed.  NL fixes lock in
whatever float bias exists at the moment of fixing.

The NL integers are correct (verified by tight thresholds: frac
0.006–0.095, sigma_N1 0.016–0.079).  The problem is that the float
position absorbs tropospheric delay as a position bias, and AR locks
in that bias.

## Why troposphere causes position drift

The tropospheric delay for a satellite at elevation angle `e` is
approximately:

```
T(e) = ZTD / sin(e)
```

where ZTD is the zenith tropospheric delay (~2.3m at sea level,
varying by ~5 cm over hours with weather).  The `1/sin(e)` mapping
function means low-elevation satellites see ~3x more delay than
zenith satellites.

Without a ZTD state, the PPPFilter absorbs this delay into position
and clock:
- The vertical component absorbs most of it (ZTD and height are
  ~95% correlated for a static receiver)
- But with an asymmetric satellite geometry, some leaks into
  horizontal — particularly longitude in mid-latitudes where the
  N-S satellite distribution is asymmetric

As ZTD changes (weather, diurnal cycle), the absorbed position bias
changes.  Each receiver's PPPFilter tracks this drift independently,
and since they started at different times or with different initial
conditions, their float positions drift at slightly different rates.
NL fixing then locks in different biases.

## The fix: estimate ZTD as a filter state

Standard PPP-AR implementations estimate ZTD as an additional state
in the EKF.  The measurement model becomes:

```
predicted_range = geometric_range + dt_rx - sat_clk + T(e)
```

where `T(e) = ZTD * M(e)` and `M(e)` is a mapping function (e.g.,
Niell dry + wet, or VMF1).

### PPPFilter changes

Add one state for ZTD (zenith total delay):

```python
# Current state vector (N_BASE = 6):
#   [x, y, z, dt_rx, ISB_GAL, ISB_BDS]
#
# New state vector (N_BASE = 7):
#   [x, y, z, dt_rx, ISB_GAL, ISB_BDS, ZTD]

IDX_ZTD = 6
N_BASE = 7
```

**Initialize**: ZTD ≈ 2.3m (Saastamoinen model from position altitude),
sigma ~0.5m.

**Process noise**: ZTD is a random walk.  Typical values:
- `q_ztd = (5e-5)^2` m²/s — corresponds to ~5 cm/hour RMS drift
- This is the standard IGS PPP value

**Measurement model**: For each satellite observation, the H matrix
row gets an additional column:

```python
H[i, IDX_ZTD] = M(elevation_i)
```

where `M(e)` is the wet mapping function.  For a simple start, use
`M(e) = 1 / sin(e)` (hydrostatic approximation).  Better: Niell wet
mapping function or GMF.

### What we already have

The PPPFilter already applies a Saastamoinen correction as a
*fixed* correction to the pseudorange/carrier observations (in
`solve_ppp.py`).  The ZTD state would estimate the *residual*
tropospheric delay not captured by the a priori model:

```
T(e) = T_apriori(e) + dZTD * M_wet(e)
```

where `T_apriori` is the current Saastamoinen correction, and `dZTD`
is the new state (initialized near zero, drifts to absorb the
model error).  This is cleaner than estimating absolute ZTD because
the a priori model handles ~95% of the delay.

### Expected impact

1. **Position stability**: ZTD absorbs the atmospheric drift that
   currently leaks into position.  Horizontal position should stop
   drifting at ~5m/hour.

2. **NL fixing quality**: With atmospheric bias removed from position,
   the float ambiguities converge to the correct integer faster.
   NL fixes should produce consistent positions across hosts.

3. **Vertical accuracy**: ZTD and height are correlated, so the
   vertical sigma will increase slightly.  But the vertical *bias*
   should decrease because the filter correctly attributes delay
   to atmosphere rather than altitude.

4. **Cross-host agreement**: Two receivers on the same antenna should
   converge to the same ZTD (same atmosphere) and same position.
   The current ~5m horizontal disagreement should shrink to the
   AR-limited level (~1-5 cm with enough NL fixes).

### What won't change

- FixedPosFilter (DOFreqEst): uses time-differenced carrier phase.
  Tropospheric delay cancels in the difference (changes slowly
  relative to 1-second epochs).  No ZTD state needed.

- WL fixing: Melbourne-Wubbena is geometry-free.  Unaffected.

- NL threshold tuning: keep the tight thresholds (0.10/0.08).

## Implementation plan — COMPLETED 2026-04-16

1. **Add `IDX_ZTD` to PPPFilter** — ✅ Done (prior session).
   N_BASE = 7, IDX_ZTD = 6, process noise (5e-5)² m²/s.

2. **Compute elevation angles** — ✅ Done (prior session).
   Already computed in PPPFilter.update() for elevation masking.

3. **Add ZTD term to H matrix** — ✅ Done (commit c1be133, 2026-04-16).
   Added to FixedPosFilter: dZTD state at IDX_ZTD=2, wet mapping
   function 1/sin(e) in H-matrix for PR and TD observations.
   PPPFilter already had it.

4. **Update ambiguity index management** — ✅ Already correct.
   NarrowLaneResolver imports N_BASE from solve_ppp (not hardcoded).

5. **Test** — ✅ Done (2026-04-16, 3-hour 3-host run).
   Results:
   - Cross-host horizontal agreement: <0.2m (target was <1m)
   - Cross-host altitude spread: <0.6m (was 8m without ZTD)
   - Altitude drift: <0.2m/hour (was 3-5m/hour without ZTD)
   - AR fixes held continuously for 2+ hours on all three hosts
   - nav2Δ: 2-4m stable (was 2-6m oscillating without ZTD)

### Possible future refinement

- Upgrade mapping function from 1/sin(e) to Niell wet or GMF.
  Not critical since low-elevation satellites are already excluded.

## Risk assessment

- **Low risk**: The change is additive — one more state, one more
  column in H.  Existing functionality unchanged.
- **Correlation with height**: ZTD-height correlation ~0.95 means
  the filter will be slower to converge in both.  May need to
  increase the initial ZTD sigma or the position process noise
  during convergence.
- **Mapping function quality**: `1/sin(e)` is crude below 15°
  elevation.  We already exclude low-elevation satellites, so
  this should be OK initially.

## References

- Kouba, J. (2009). "A guide to using IGS products." — ZTD
  estimation in PPP, process noise values
- Niell, A.E. (1996). "Global mapping functions for the atmosphere
  delay at radio wavelengths." — Mapping functions
- Zumberge et al. (1997). "Precise point positioning for the
  efficient and robust analysis of GPS data from large networks."
  — Original PPP with ZTD estimation
