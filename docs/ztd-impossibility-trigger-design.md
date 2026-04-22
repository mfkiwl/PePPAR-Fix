# ZTD impossibility as a filter-corruption trigger

*Design doc, 2026-04-22.  Implementation deferred until after the
ar-phase-bias-filter ↔ main rebase lands; target this week.*

## Motivation

The filter's Zenith Tropospheric Delay (ZTD) residual state is a
sensitive indicator of whole-filter health.  Empirical evidence from
day0421f shows that when the filter's position drifts (via wrong
integer commit), it absorbs the position error into the ZTD state
rather than letting the PR residuals grow — the filter's job is to
minimize residuals, and ZTD is a convenient sink.  The result is a
nonphysical ZTD value that directly signals "filter state is
corrupted" even before external sanity checks (NAV2, window_rms, etc.)
would fire.

Compared to the `window_rms` trip (which catches residuals after the
damage has spread across multiple SVs) and `anchor_collapse` (which
catches the total loss of long-term anchors), a ZTD trip catches a
specific class of corruption earlier and with a cleaner signal —
because there's no atmosphere that would produce the observed ZTD
value.

## The 1 ns vulnerability this closes

The exponential blend `known_ecef += 0.001 * (ar_ecef - known_ecef)`
with τ ≈ 1000 s gives us roughly 3 mm/epoch of position migration
per meter of AR delta.  A wrong integer that shifts position by 3 m
and persists for 100 epochs (~100 s at 1 Hz) puts 30 cm of bias into
`known_ecef` — **1 ns of timing error that the FixedPosFilter inherits
and cannot shed**.

Every per-SV monitor and external sanity check (FalseFixMonitor,
NAV2 watchdog, window_rms) needs some time to accumulate evidence
before firing.  ZTD impossibility is a direct physical check on the
filter state itself — no accumulation, no external source.  It fires
as soon as the filter's own state crosses a line no atmosphere can
cross.

## Physical bounds on ZTD residual

The filter's ZTD state is the **residual** from the a priori
Saastamoinen model, not the total zenith delay.  Saastamoinen
gives us ~2300 mm dry + ~200 mm wet at lab elevation; our residual
tracks the delta from that.

| Residual magnitude | Physical interpretation |
|---|---|
| ± 100 mm | Normal weather (humidity, pressure variation) |
| ± 300 mm | Unusual weather (strong front, major pressure change) |
| ± 500 mm | Essentially no atmosphere does this |
| **> ± 700 mm** | Filter is absorbing position error — trip threshold |
| > ± 1000 mm | Pathological — filter has substantially corrupted state |

Day0421f direct evidence of corruption (from the altitude-drift
period earlier this week):

- clkPoC3 at drift: −870 mm, −1004 mm
- MadHat at drift: −999 mm, −1026 mm
- TimeHat (few anchors corrupted): stayed ±56 mm

Healthy operation on TimeHat at the same wall-clock moment showed
ZTD residual ±56 mm, so the corruption signature and healthy
baseline differ by > 15×.  The separation is clean.

## Trigger design

**Condition**: sustained `|ZTD residual| > 700 mm` for
`ztd_trip_sustained_epochs = 60` (~1 min at 1 Hz).

**No latch**.  ZTD drift can happen from bootstrap through ANCHORED
— there's no "earned trust" prerequisite.  The monitor checks
every epoch where the filter has a ZTD state, regardless of
`reached_anchoring` / `reached_anchored` status.  Contrast with
anchor_collapse which only fires post-`reached_anchored` because
the whole concept of anchor collapse requires having had anchors.

**Parameters**:

```python
ztd_trip_threshold_m = 0.7          # residual envelope
ztd_trip_sustained_epochs = 60      # ~1 min at 1 Hz
```

**Event emission**: `FixSetIntegrityMonitor` emits:

```
[FIX_SET_INTEGRITY] TRIPPED reason=ztd_impossible 
    ztd_m=X.XXX threshold=0.700 sustained_epochs=60
```

Same `record_trip()` path as `window_rms` and `anchor_collapse`.
Alarm consumer's recovery action is identical to the existing
`window_rms` path: re-init `PPPFilter` at `known_ecef`, clock=0,
drop ambiguity / MW / NL state.  ZTD state also resets to 0 in the
re-init, closing the loop.

## Cross-filter sanity layer

PPPFilter (inside `AntPosEstThread`, `IDX_ZTD=6`) and FixedPosFilter
(steady-state timing, `IDX_ZTD=2`) carry independent ZTD states.
Under healthy operation they should agree within small tolerance —
both are tracking the same physical ZTD.  Divergence indicates one
of them has absorbed position/clock error the other hasn't.

**Sanity check** (addition to the above, not a replacement):

```
if |PPPFilter.ZTD - FixedPosFilter.ZTD| > ztd_divergence_threshold_m:
    # One filter has absorbed position error the other hasn't.
    # The one furthest from 0 is the corrupted one.
    if abs(PPPFilter.ZTD) > abs(FixedPosFilter.ZTD):
        corrupted = 'pppfilter'     # bg-PPP corrupted
    else:
        corrupted = 'fixedposfilter'  # timing filter corrupted
    # Re-seed only the corrupted one.
```

Parameters:

```python
ztd_divergence_threshold_m = 0.3    # 300 mm cross-filter disagreement
```

This closes the future-work doc's "Case 4" gap — the ability to
reset bg-PPP without touching FixedPos when bg-PPP is specifically
the corrupted one.  A previously-missing recovery path.

**Order of trip evaluation** in `FixSetIntegrityMonitor.evaluate()`:

1. `window_rms` (existing) — fleet-level residual elevation
2. `anchor_collapse` (existing) — anchor count after
   `reached_anchored`
3. **`ztd_impossible`** (new) — absolute ZTD magnitude, either filter
4. **`ztd_divergence`** (new) — cross-filter ZTD disagreement

Each trip reason mutually exclusive per epoch (first match wins).
`ztd_impossible` takes priority over `ztd_divergence` because it's
an absolute physical violation; the divergence case is more
informational.

## Interactions

**With `reached_anchored` latch**: none.  Unlike `anchor_collapse`,
the ZTD trips don't gate on latch state.  An early-bootstrap filter
can have wild ZTD during convergence; the sustained-epoch window
is what excludes transient startup values.  If the 60-epoch sustain
window is insufficient during Phase 2 bootstrap convergence, raise
it — but don't add a latch.

**With `setting_sv_drop`**: complementary.  setting_sv_drop's
residual-ceiling change (main's 5bffc81) keeps high-elev anchors
*in* the fix set so they maintain elevation diversity needed to
constrain ZTD separately from altitude.  ZTD-impossibility trip is
the fallback when elevation diversity failed anyway and the filter
state drifted.

**With NAV2 watchdog** (FixedPosFilter's existing path): ZTD trip
fires *before* NAV2 divergence for most corruption scenarios.
NAV2's 5 m or 10 m threshold corresponds to much worse position
drift than the ZTD residual would require.  Effectively: ZTD trip
is the first line, NAV2 is the backstop.

**With `window_rms` trip**: ZTD trip fires on filter state directly;
`window_rms` fires on PR residuals.  They catch different failure
modes — ZTD when the filter absorbs position error cleanly,
`window_rms` when the filter leaks residual growth across multiple
SVs.  Both useful, both kept.

## Tests

1. `test_ztd_trip_fires_above_threshold_sustained` — synthetic
   filter with `|ZTD| = 800 mm` for 60 epochs → trip fires with
   reason=ztd_impossible
2. `test_ztd_trip_does_not_fire_below_threshold` — `|ZTD| = 400 mm`
   for 60 epochs → no trip (within extreme-weather envelope)
3. `test_ztd_trip_does_not_fire_transient` — `|ZTD|` spikes to
   1000 mm for 10 epochs then settles → no trip (sustained check)
4. `test_ztd_trip_no_latch_required` — from CONVERGING state,
   `reached_anchoring=False`, `reached_anchored=False`, ZTD
   sustained above threshold → trip fires
5. `test_ztd_divergence_identifies_corrupted_filter` — PPPFilter
   ZTD = +500 mm, FixedPosFilter ZTD = +100 mm, divergence
   threshold 300 mm → emits `reason=ztd_divergence
   corrupted=pppfilter`
6. `test_ztd_divergence_does_not_fire_when_agreed` — both filters
   at +200 mm → no trip
7. `test_ztd_trip_re_init_zeros_ztd` — after `record_trip()` path,
   PPPFilter's IDX_ZTD is 0 again

## Where this lands

On `ar-phase-bias-filter` alongside the post-fix residual
monitoring work ptpmon is queued to pick up.  Both extend
`FixSetIntegrityMonitor`; landing them together minimizes
back-to-back refactors of the same class.

`AntPosEstThread` already reads its PPPFilter's `IDX_ZTD` for
the existing log line (per commit `73ecfd7`).  The trip
evaluation needs access to both filters' ZTD states — requires
a small API addition to make `FixedPosFilter.x[IDX_ZTD]`
reachable from the monitor, or passing both values to
`FixSetIntegrityMonitor.ingest()` each epoch.  Cleaner to have
the monitor pull both in its `evaluate()` step given each
filter already exposes `.x`.

## Open questions

1. **Should `FixedPosFilter` also trigger a PPPFilter re-init?**
   FixedPosFilter is the timing-side filter driving PPS — a
   corruption there is a timing emergency.  Today the re-init
   paths are PPPFilter-only (the NAV2 watchdog re-seeds
   FixedPosFilter from `known_ecef`).  The ZTD trip as drafted
   fires on either filter but the recovery re-init only touches
   PPPFilter.  Should FixedPosFilter have its own reset path,
   or is the existing NAV2 watchdog sufficient?
2. **Time-varying threshold under extreme weather?**  A 700 mm
   residual envelope is generous for temperate climates.
   Deployed sites near coasts, at altitude, or in severe
   meteorological conditions might need a wider envelope.
   Deferred; simple scalar threshold is fine for the lab.
3. **Interaction with pre-rebase `window_rms` trip frequency.**
   `window_rms` already fires ~7 times overnight on ptpmon.
   Adding `ztd_impossible` may overlap with or precede some of
   these.  Need to see in practice whether they're
   complementary (catch different events) or redundant (catch
   the same events at different moments).  First few nights
   post-landing should be instructive.

## Implementation size

~80 lines in `fix_set_integrity_monitor.py` + ~120 lines of
tests.  Half a day with the rename code already settled.
