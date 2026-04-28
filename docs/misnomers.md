# Misnomer log

Naming-quality audit.  Sloppy names lead to sloppy thinking; this
file is the running record of identifiers that don't honestly
describe what they do.

**Not for batch renaming.**  Each entry is a candidate to fix
opportunistically when the surrounding code is being touched for
some other reason.  Renaming purely for naming pollutes git blame
and blast-radius for no test signal — only worth doing when you're
already in the diff.

## How to add an entry

```
### `<identifier>` — <severity>

**Where**: `path/to/file.py:LINE` (function `f`, class `C`)
**Claim**: what the name implies.
**Actual**: what the code actually does.
**Why it matters**: the bug or confusion this enables.
**Proposed**: suggested rename (or "see notes" if non-trivial).
**Notes**: anything about timing, scope, dependencies.
```

**Severity scale**:
- **Dangerous** — name implies wrong semantics; future reader
  could write a bug.  Fix sooner.
- **Misleading** — name oversells or omits a critical
  qualifier; reader has to dig.
- **Cosmetic** — imprecise but not misleading; fix only when
  in the diff anyway.

## Seed pass — 2026-04-24

Candidates found while auditing files I'd touched this week.
Initial pass; not exhaustive.

### `ecef_distance_m` — Dangerous

**Where**: `peppar_bus/cohort.py:131`
**Claim**: Distance between two ECEF positions.
**Actual**: Takes LLA tuples (lat_deg, lon_deg, alt_m), not ECEF
(X, Y, Z).  Uses flat-earth approximation, not Pythagoras on
ECEF deltas.
**Why it matters**: A caller who does
`ecef_distance_m(*ecef_a, *ecef_b)` would get a meaningless
number (interpreting X-meters as latitude-degrees, etc.).  The
flat-earth approximation is also caveat-worthy at intra-fleet
scales but already documented in the docstring.
**Proposed**: `lla_distance_flat_m` or
`topocentric_distance_m`.

### `PPPFilter.detect_cycle_slips` — Dangerous

**Where**: `scripts/solve_ppp.py:439`
**Claim**: Detects cycle slips.
**Actual**: Only catches the receiver's own lock-loss
indicator (`lock_duration_ms` decreasing).  Actual cycle-slip
detection (Melbourne-Wübbena jump, geometry-free residual,
arc-gap, etc.) lives in `MelbourneWubbenaTracker.detect_jump`
and the WL drift monitor.
**Why it matters**: A future reader looking for "where do we
detect cycle slips?" finds this method first and might assume
it's the slip detector.  The real detection stack is in
`scripts/peppar_fix/cycle_slip.py` and `ppp_ar.py`.  Using
this method in isolation will miss most slips.
**Proposed**: `detect_lock_loss_slips` or
`slips_from_lock_indicator`.

### `PPPFilter.add_ambiguity(sv, N_init_m)` — Misleading

**Where**: `scripts/solve_ppp.py:400`
**Claim**: Parameter `N_init_m` — capital `N` is the GNSS
convention for an integer cycle count, `_m` says metres.
**Actual**: The value is the float IF ambiguity in metres
(`phi_if_m - pr_if`), not an integer-derived value.  Stored
directly as a real-valued state variable until later
resolution.
**Why it matters**: A reader who knows the convention
(`N_WL`, `N1`, `N_NL` are integers; ambiguity-as-float is
typically `A` or `b`) sees the `N_` prefix and assumes
integer-derived.
**Proposed**: `amb_init_m` or `ambiguity_init_m`.  (Caller-
side variable `N_init` has the same issue but is more local.)

### `PPPFilter.tropo_delay(elevation_deg)` — Misleading

**Where**: `scripts/solve_ppp.py:502`
**Claim**: The tropospheric delay at this elevation.
**Actual**: The **a-priori dry/hydrostatic** tropospheric
slant delay only (`2.3 m * m_h(elev)`).  The wet residual is
estimated as a state variable and applied separately via
`x[IDX_ZTD] * wet_mapping(elev)` at the same callsite.
**Why it matters**: A reader sees `tropo = self.tropo_delay(elev)`
and thinks the full tropo is captured.  The full tropo at
this epoch is `tropo_delay + ztd_state * wet_mapping`.
**Proposed**: `apriori_hydrostatic_slant_m` or
`dry_tropo_slant_m`.

### `PPPFilter.wet_mapping(elevation_deg)` — Cosmetic

**Where**: `scripts/solve_ppp.py:514`
**Claim**: A *wet-specific* tropospheric mapping function.
**Actual**: Default impl is `1/sin(elev)` — identical to the
hydrostatic mapping factor.  With GMF active
(`_GMF_PROVIDER` set), it does return the wet-specific
Boehm 2006 mapping.
**Why it matters**: Until Phase 4 GMF (commit `c00a6dd` /
`b600519`) the "wet" qualifier was aspirational under the
trivial `1/sin(elev)` model.  Reader could think the wet
component is being mapped differently from dry — until
GMF is on, it isn't.  Now that GMF exists, the name is
honest in GMF mode and harmlessly redundant in default mode.
**Proposed**: leave for now; honest under GMF and
GMF is the future.

### `peppar_mon.LogState.antenna_position` — Cosmetic

**Where**: `peppar_mon/log_reader.py:131`
**Claim**: Tuple `(float, float, float)` is a "position".
**Actual**: Stores `(lat_deg, lon_deg, alt_m)` — mixed units
(degrees and metres) in a generic 3-tuple.  Units documented
in the docstring at line 124-130 but not in the type.
**Why it matters**: Mild — readers who need exact units have
to read the docstring.  A typed `NamedTuple` or `@dataclass
AntennaPositionLLA(lat_deg=..., lon_deg=..., alt_m=...)`
would make the unit mismatch visible at every callsite.
**Proposed**: `@dataclass class LLA: lat_deg: float;
lon_deg: float; alt_m: float`, used by `antenna_position`.

## 2026-04-28

### `WlDriftMonitor` / `wl_drift` / `[WL_DRIFT]` — Dangerous

**Where**: `scripts/peppar_fix/wl_drift_monitor.py:1` (class
docstring + log tag); engine call sites in
`scripts/peppar_fix_engine.py:2348-2386`.
**Claim**: Detects "wrong WL integer commits" — implies a phase-
side ambiguity-error detector, in line with the sunrise TEC slip
storm motivation.
**Actual**: Tracks the rolling mean of the **Melbourne-Wübbena
combination residual** post-fix.  MW = phase − pseudorange; the
residual responds to either-side disturbances.  Empirically (3-host
day0427night; Z = −0.17, p = 0.86 against BNC's independent IF-based
slip detector) the firing pattern is **statistically
indistinguishable from random** with respect to phase events.
Direct probe on three anti-correlated SVs (E29, E21, E19) showed
BNC's filter state smoothly drifting through engine wl_drift trips
— the disturbance was PR-side.
**Why it matters**: a future reader sees `wl_drift` events
demoting SVs and assumes phase-domain instability is the root
cause.  Investigation directions follow that framing (cycle-slip
diagnosis, ambiguity-resolution tuning) when the signal is
actually PR multipath / code-bias drift.  Burned hours of
investigation on day0427night until the BNC validator was built.
**Proposed**: not a simple rename — the underlying signal is
wrong for the documented use case.  Two paths in increasing
scope:

  1. Internal scope only: rename class to
     `MwResidualRollingMeanMonitor` and the log tag to `[MW_DRIFT]`
     to honestly describe the signal; keep adaptive thresholding
     by integer-history class (I-153334-main) on top.
  2. Replace the signal with a phase-only counterpart (GF or IF
     residual rolling mean, following BNC's lead).  Then the
     "wl_drift / wrong WL integer" framing becomes legitimate.
     Larger redesign — see proposal in dayplan.

**Notes**: see `project_wl_drift_smooth_float_signal_20260428` and
`project_wl_drift_vs_bnc_finding_20260428` for the full
investigation chain.

## Code-quality issues found alongside (not misnomers)

### `gmf._coeff_sum` — dead code

**Where**: `scripts/regression/gmf.py:218`
**Issue**: Function defined but never called.  The
spherical-harmonic sums it would compute are inlined in
`gmf_at` and `GMFProvider.__init__`.  Author's leftover —
remove next time the file is touched.

## Adding to this list

When you find another candidate while doing other work, add
an entry under a new dated section.  Don't sort or re-organise;
chronological accumulation is fine.  When the underlying
identifier gets renamed, strike through the entry rather than
deleting (so we keep the audit history visible).
