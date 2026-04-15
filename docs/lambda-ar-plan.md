# LAMBDA Ambiguity Resolution — Implementation Plan

**Date**: 2026-04-15
**Status**: Proposed
**Prerequisite**: ZTD state in PPPFilter (done, commit e72e20d)

## Why LAMBDA

The current NL resolver uses per-satellite rounding: for each
satellite independently, check if `|frac(N1)| < threshold` and
`sigma_N1 < threshold`, then round to nearest integer.  This is the
weakest possible AR validation because it ignores **cross-correlations
between ambiguities** through the geometry matrix (position, clock,
ZTD, ISBs).

With the ZTD state, the ambiguity covariance stays elevated because
ZTD is ~95% correlated with height, and height is correlated with
ambiguities through the observation geometry.  Per-satellite sigma_N1
doesn't drop below 0.12 for ~37 minutes despite position sigma
reaching 0.035m.  The off-diagonal covariance contains information
the per-satellite approach wastes.

Production PPP-AR systems (CNES PPP-WIZARD, NRCan CSRS-PPP, GipsyX,
RTKLIB) all use LAMBDA (Least-squares AMBiguity Decorrelation
Adjustment), which:

1. Decorrelates the float ambiguity covariance via Z-transform
2. Searches for integer candidates using the decorrelated space
3. Validates via ratio test: `R = Ω(2nd best) / Ω(best)` where
   Ω is the sum of squared residuals in ambiguity space
4. Accepts if R > threshold (typically 2.0–3.0)

LAMBDA uses the **full covariance matrix** including ZTD-height-
ambiguity cross-terms.  Ambiguities that are tightly constrained
by carrier phase (even if marginally uncertain due to ZTD
correlation) can be resolved because LAMBDA sees that the
correlation structure makes the *joint* integer solution well-
determined even when *marginal* sigmas are large.

## What LAMBDA does

### Input
- Float ambiguity vector `â` (from PPPFilter: `x[N_BASE:]`)
- Ambiguity covariance `Qâ` (from PPPFilter: `P[N_BASE:, N_BASE:]`)

### Step 1: Z-transform decorrelation
Compute an integer transformation matrix `Z` (via LDL^T
decomposition of `Qâ` with integer Gauss transforms) such that
`Z^T Qâ Z` is as diagonal as possible.  This is the key insight
of Teunissen (1995): the search space in decorrelated coordinates
is much smaller.

```
Qz = Z^T Qâ Z      (decorrelated covariance)
ẑ  = Z^T â          (decorrelated float ambiguities)
```

### Step 2: Integer search
Search for the integer vector `ž` that minimizes:

```
Ω(ž) = (ẑ - ž)^T Qz^{-1} (ẑ - ž)
```

The search uses the sequential conditional structure of the LDL^T
decomposition to enumerate candidates efficiently (typically
examining <100 candidates for 10–15 ambiguities).  Keep the best
and second-best candidates.

### Step 3: Ratio test validation
```
R = Ω(ž₂) / Ω(ž₁)
```
Accept ž₁ if R > threshold.  Typical: R > 2.0 for ≥8 ambiguities,
R > 3.0 for fewer.

### Step 4: Back-transform
```
N̂ = Z^{-T} ž₁       (integer ambiguities in original space)
```

Apply as tight constraints to the PPPFilter states (same as current
`_apply_fix`).

## Partial AR (PAR)

If the full set fails the ratio test, iteratively remove the
ambiguity with the largest contribution to Ω and retry.  This is
standard (Li et al., 2015) and naturally handles the case where
one or two ambiguities are poorly determined (e.g., low-elevation
satellite, recent cycle slip) while the rest are solid.

Minimum subset: 4 ambiguities (needed for geometric redundancy
with the base states).

## Implementation

### Phase 1: LAMBDA core (~80 lines)

New file `scripts/lambda_ar.py`:

```python
def lambda_decorrelate(Qa):
    """LDL^T decomposition + integer Gauss transforms.

    Returns Z (integer transform matrix), L, D such that
    Z^T Qa Z ≈ diag (as diagonal as possible).
    """

def lambda_search(z_float, L, D, n_candidates=2):
    """Integer search in decorrelated space.

    Returns list of (candidate, omega) sorted by omega.
    Uses sequential conditional enumeration (Teunissen 1995).
    """

def lambda_resolve(a_float, Qa, ratio_threshold=2.0, min_fixed=4):
    """Full LAMBDA AR with partial AR fallback.

    Returns (fixed_vector, n_fixed, ratio) or (None, 0, ratio).
    """
```

The decorrelation and search are ~60 lines of numpy.  The algorithm
is well-documented (Teunissen 1995, de Jonge & Tiberius 1996, Chang
et al. 2005).  RTKLIB's `lambda.c` is a clean reference (~200 lines
of C, translates directly to numpy).

### Phase 2: Integration with NarrowLaneResolver (~30 lines)

Replace the per-satellite rounding loop in `NarrowLaneResolver.attempt()`
with:

1. Collect all WL-fixed satellites' NL float ambiguities and their
   covariance submatrix from the PPPFilter
2. Convert IF ambiguities to NL cycles:
   `n1_float = (a_if - alpha2 * lambda_WL * N_WL) / lambda_NL`
3. Extract the NL covariance: scale by `1/lambda_NL^2`
4. Call `lambda_resolve(n1_float, Qa_nl)`
5. If resolved, apply fixes to the PPPFilter states

The per-satellite frac/sigma pre-screen stays as a fast reject for
obviously unconverged ambiguities (frac > 0.25 or sigma_N1 > 1.0).
LAMBDA only runs on the candidates that pass the pre-screen.

### Phase 3: Validation tests (~40 lines)

1. **Decorrelation test**: verify Z is integer, Z^T Qa Z is more
   diagonal than Qa
2. **Known-answer test**: construct a float solution where the true
   integers are known, add noise, verify LAMBDA recovers them
3. **Ratio test**: verify ratio > threshold on clean data, ratio < 1
   on ambiguous data
4. **PAR test**: inject one bad ambiguity, verify PAR drops it and
   fixes the rest

### Phase 4: Tuning

- Ratio threshold: start at 2.0, tune based on cross-host agreement
- Pre-screen: frac < 0.25, sigma_N1 < 1.0 (loose, just filters
  garbage)
- Minimum subset for PAR: 4
- Re-run cross-host tests (TimeHat + MadHat + clkPoC3) and compare
  position agreement vs per-satellite rounding

## Expected impact

1. **Faster TTFF**: LAMBDA uses covariance cross-terms that the
   per-satellite approach ignores.  Ambiguities that are marginally
   uncertain individually may be jointly well-determined.  Expect
   TTFF reduction from 37 min to 15–25 min.

2. **Better integer correctness**: The ratio test is a much stronger
   validation than per-satellite frac/sigma.  It considers the
   *relative* quality of the best vs second-best integer solution,
   not just the absolute quality of each ambiguity.

3. **Partial AR resilience**: When one satellite has a bad ambiguity
   (cycle slip, multipath, low elevation), PAR drops just that one
   instead of the whole epoch failing.

4. **ZTD correlation handled naturally**: LAMBDA's decorrelation
   step explicitly accounts for the ZTD-height-ambiguity cross-
   correlation that makes per-satellite sigma_N1 slow to converge.

## Existing per-satellite rounding: keep or remove?

Keep as a fallback / pre-screen.  The per-satellite approach is
simpler and works in degenerate cases (1–3 WL-fixed satellites)
where LAMBDA has too few ambiguities to validate.  LAMBDA runs
when ≥4 WL-fixed satellites pass the pre-screen; below that,
fall back to per-satellite rounding with the current thresholds.

## References

- Teunissen, P.J.G. (1995). "The least-squares ambiguity
  decorrelation adjustment: a method for fast GPS integer
  ambiguity estimation." J. Geodesy 70:65–82.
- de Jonge, P. & Tiberius, C. (1996). "The LAMBDA method for
  integer ambiguity estimation." GPS Solutions 1(2):12–20.
- Chang, X.W. et al. (2005). "MLAMBDA: a modified LAMBDA method
  for integer least-squares estimation." J. Geodesy 79:552–565.
- Li, X. et al. (2015). "Accuracy and reliability of multi-GNSS
  real-time precise positioning." GPS Solutions 19:607–616.
  (Partial AR methodology)
- Verhagen, S. & Teunissen, P.J.G. (2013). "The ratio test for
  future GNSS ambiguity resolution." GPS Solutions 17:535–548.
  (Fixed failure-rate ratio test)
- Takasu, T. RTKLIB `src/lambda.c` — clean C implementation,
  ~200 lines, direct numpy translation.

## Effort estimate

Phase 1 (LAMBDA core): ~80 lines numpy, well-defined algorithm
Phase 2 (integration): ~30 lines, replace inner loop of attempt()
Phase 3 (tests): ~40 lines
Phase 4 (tuning): lab runs on 3 hosts

Total: ~150 lines of new code.  The algorithm is mature and
well-documented.  The main risk is getting the covariance
extraction right (mapping from IF ambiguity covariance to NL
cycle covariance, accounting for the WL integer contribution).
