# Position-fix strength metric

A runtime metric for how firmly the filter is locked into its current
state — distinct from, and sometimes orthogonal to, our **confidence
that the state is correct**.

## What "strength" means here

The word "strength" in this doc means **resistance to perturbation by
a wrong integer ambiguity** over time.  It is a question of Kalman
dynamics:

- When a biased phase observation enters the filter, the state shifts
  by `Δstate_per_bias ≈ 1 / N_eff` per epoch, where `N_eff` is the
  effective number of independent observations.
- Large `N_eff` = small per-epoch pull per biased observation = more
  strength.
- Tight covariance `P` = small Kalman gain = small per-epoch pull per
  biased observation = more strength.

Strength is the *structural* resistance of the filter.  It does not
speak to whether the state currently being defended is right or wrong.

## Definition

First-cut metric, to be computed and logged every AntPosEst epoch:

```
strength = WL_fixed_count / σ_3d_m
```

Dimensionless (ambiguities-fixed per meter of position uncertainty).
Larger is stronger.

Interpretation at a glance:
- `strength ≈ 50` — weakly locked (~15 WL fixed at 30 cm, or 5 WL at
  10 cm); easy to shift.
- `strength ≈ 500` — firmly locked (~15 WL fixed at 3 cm, or 25 WL at
  5 cm); hard to shift.
- `strength ≈ 5000` — pathologically over-locked (e.g. σ = 3 mm at
  15 WL).  See "Strong + wrong" below.

## Strength vs correctness — the four quadrants

Strength and correctness are orthogonal.  A filter can be:

| | weak | strong |
|---|---|---|
| **right** | early convergence, tightening — the expected path | desired steady state |
| **wrong** | actively being corrected — usually self-resolves | **the trap** — high `strength`, physically wrong, very slow to dislodge |

The pathological "strong + wrong" quadrant is exactly what we've been
chasing: day0422c ptpmon sat at σ ≈ 30 mm with 11 WL fixed and
`R = 92695` — `strength ≈ 370` — locked on an altitude ~ 7 m off from
truth.  Raw `WL / σ` looked excellent while the answer was badly wrong.

**Never use strength alone as an OK signal.**  It must be paired with
an independent consistency axis before any action is taken.

## Consistency axes that pair with strength

| axis | measures | can catch "strong + wrong"? |
|---|---|---|
| ZTD in physical envelope | is absorbed bias breaching reality? | ✓ (used by `ztd_impossible` / `ztd_cycling`) |
| `nav2Δ` (this host's live AR vs F9T native nav2) | truth consistency vs an independent filter inside the receiver | partially |
| cross-host PPP-AR agreement | truth consistency across shared-antenna hosts | ✓ (strong, when available) |
| per-SV PR residual chi² | which SV is pulling | ✓ (fine-grained) |
| per-SV MW post-fix residual drift | wrong WL integer commitment on a specific SV | ✓ (earliest signal) |

A healthy state is `strength HIGH + at least one consistency axis HIGH`.
A trapped state is `strength HIGH + consistency axes LOW`.  A weak
state is `strength LOW` — no judgement needed yet, just keep
converging.

## Runtime uses

The initial implementation just **computes and logs** strength on the
existing `[AntPosEst N]` line, for post-hoc analysis.  Beyond that,
modest uses worth considering once we have a week of data:

1. **Scale defensive monitor thresholds.**  A strongly-locked filter
   has earned the scrutiny; tighten per-SV PR thresholds.  A weakly-
   locked one is still shopping; loosen to avoid spurious rejections.
2. **Gate aggressive actions.**  Don't aggressively flush MW state
   unless `strength` is high enough that re-convergence is fast.
3. **Meta-health indicator.**  Sudden drop in `strength` without a
   trip = fix set degrading silently — worth alerting on.
4. **Trap detection.**  `strength HIGH + nav2Δ LARGE + ZTD creeping`
   is the clean signature of the strong-wrong trap, earlier than any
   single trip fires.

Do **not** use `strength` as a trip threshold by itself.  See the
four-quadrant analysis — it cannot distinguish the state we want
from the state we fear.

## What's deferred: per-SV weighting

The unweighted SV count in the numerator hides structural variation:

- **Higher-elevation SVs** have less troposphere / multipath bias and
  should count for more than low-elev SVs.
- **SVs on less-correlated failure modes** (e.g., L2 vs L5 during a
  TEC storm — L5 is more susceptible) should count for more during
  periods when their cohort is under stress.
- **Older / better-characterised SVs** (satellites with stable clock
  corrections in the SSR stream) should count for more than new
  SVs still within their MW averaging warmup.

A weighted form would look like:

```
strength = (Σ_i w_i) / σ_3d_m
```

with `w_i` encoding elevation, band, fix age, and fleet-local
covariance information.  Per-SV weighting is **deferred** — the
unweighted first cut is sufficient to start gathering data about
how strength evolves through a run, and the weighted form is
straightforward to add once we've picked specific weight axes from
observation.

## Historical note — the "ironic" scaling

The overnight 2026-04-22 run showed the two hosts tracking **fewer**
SVs (TimeHat F9T-10, ptpmon mid-range) weathering pre-dawn TEC
conditions better than the two hosts tracking **more** SVs (clkPoC3,
MadHat, both F9T-20B's).  Physically this happens because:

- Per-SV resistance `1/N_eff` does scale with N (more SVs → less pull
  per bad SV).
- But per-SV failure probability isn't uniform — F9T-20B reaches
  further into the noise floor (lower elev, L5 band) where failure
  probability is higher AND failures correlate during TEC events.
- When failures correlate, the apparent `1/N_eff` protection
  evaporates — N correlated failures behave like one big failure.

The per-SV weighting framework, once implemented, is the natural
place to encode this: the `w_i` for a correlated-failure SV during
a known stress event drops, reducing its contribution to the
strength denominator.
