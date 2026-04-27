# SSR phase-bias step handling

## What's happening

SSR phase-bias messages (RTCM 3 SSR 1265-1270 or IGS-SSR 4076 subtypes
6/8/etc.) carry per-(SV, signal) phase-bias corrections.  These
corrections align the integer-resolved ambiguity space across receivers
so wide-lane and narrow-lane integers match what the analysis center's
own batch processing resolved.

The bias is **piecewise continuous within a "bias segment"** and may
**step by an integer number of cycles at a segment boundary**.  Steps
are intentional and happen at:

- AC datum changes (day boundary, new orbit/clock product release)
- Yaw maneuvers / eclipse seasons (satellite attitude flips, integer
  rollover by AC convention)
- AC's choice of integer rollover when the cumulative bias exceeds
  some bound

The RTCM phase-bias message includes a **discontinuity counter**
(IDF120 in 1265-style, similar in IGS-SSR 4076-7) that increments on
every new segment.  A receiving processor is expected to read the
counter, detect "this is a new segment, don't compare values across
the boundary," and reset any per-(SV, signal) state that accumulated
on the prior segment.

## Why this matters for our slip detection

Our Melbourne-Wubbena tracker
(`scripts/ppp_ar.py:MelbourneWubbenaTracker`) computes the MW combination
on **bias-corrected** carrier phase:

```
phi_corrected[f] = phi_raw[f] - phase_bias[f] / lambda[f]
MW = (f1·phi1_corrected - f2·phi2_corrected) / (f1 - f2)
   - (f1·pr1 + f2·pr2) / (f1 + f2)
```

Across a bias-segment boundary, `phase_bias[f]` jumps by integer cycles,
so `phi_corrected[f]` jumps by the same integer cycles, so MW jumps
by an integer (or near-integer) number of WL wavelengths.

`MelbourneWubbenaTracker.detect_jump()` compares the current MW to a
running average over the prior segment.  A multi-cycle bias step looks
like a multi-cycle MW jump → false `mw_jump` slip event fires.  The
receiver was tracking solidly the whole time (`lock_duration_ms` at the
~64.5 s u-blox cap, same SV with no real discontinuity) and the GF
combination on **raw** phase agrees (`gf_jump` ≈ 0.1-0.5 cm — far below
the slip threshold).

Observed in practice on 2026-04-19 (`project_to_main_cycle_slip_diff_result_20260426`)
and confirmed on 2026-04-26 (this engine smoke test produced
`mw=7.04c`, `mw=5.57c`, `mw=-2.72c` simultaneously on E11/E12/E36 with
GF deltas all sub-cm).

The downstream cost is large: each false slip resets the per-SV state
machine (FLOATING → CONVERGING → ANCHORING re-runs, ≥10 min per cycle).
Multiple SVs slipping at the same instant during a bias-segment
boundary destabilizes the filter into a wrong ZTD basin from which
it doesn't recover.

## Path A — current shortcut (commit TBD, 2026-04-26)

In `scripts/realtime_ppp.py` at the bias-application site (around
line 880, where `pb_f1` and `pb_f2` are looked up and applied), we
already track per-(SV, signal) bias values for the `[PB_APPLIED]`
diagnostic log.  Path A extends that tracker:

- Compare the new bias value to the previous value for the same
  (SV, signal).
- If `|delta_bias_cycles| > 0.5`, set `obs['phase_bias_stepped'] = True`
  for the obs that's about to be emitted.
- `MelbourneWubbenaTracker.detect_jump()` reads the flag and returns
  `None` (no jump check) when set.

Properties:

- **No new state in MW tracker** — flag rides on the obs dict that's
  already plumbed end-to-end.
- **No state plumbing into SSR layer** — the SSR ingest already has
  the prior-bias dict it needs.
- **MW WL fix state preserved across the boundary** — we just skip
  the slip-detection comparison for one epoch.  The MW running
  average itself updates with the bias-corrected phase as before, so
  subsequent epochs (post-segment-start) form a fresh comparison
  baseline naturally over `_MIN_EPOCHS_FOR_JUMP`.

Limitations of Path A:

1. **Only catches bias steps the engine SEES.**  If the AC publishes
   a step but our SSR stream drops the message, we never know.  The
   first fresh-bias message after the drop will look like an
   instantaneous step from a stale value.  In practice this is rare
   on a healthy stream; correction-stream lag manifests as
   missing-bias rather than wrong-bias.

2. **Single-epoch suppression only.**  If the AC's new bias is wrong
   for multiple epochs (rare, but possible), the second epoch's MW
   comparison against the now-fresh average would re-fire the false
   slip.  Path B (below) addresses this by carrying the discontinuity
   indicator forward.

3. **Threshold is heuristic.**  0.5 cycles separates "small AC bias
   jitter" (sub-cycle) from "segment boundary" (integer cycles).
   In practice AC biases jitter at the ~0.05 cycle level so 0.5 is
   conservative.  But if an AC ever publishes sub-cycle bias jumps
   that are still "intentional" by their convention, we'd mis-handle.

4. **The MW tracker still consumes bias-corrected phase** for its
   running average.  Across many bias steps, the average drifts
   along with the AC convention rather than tracking the receiver
   alone.  Path B fixes this by separating slip-detection MW
   (raw-phase) from AR MW (bias-corrected).

## Path B — the right architectural fix (TODO)

Two complementary changes:

### B.1 Read the RTCM discontinuity counter

The SSR phase-bias message carries a per-(SV, signal) discontinuity
indicator that the AC controls explicitly.  Our SSR parser
(`scripts/ssr_corrections.py`) currently extracts the bias VALUE but
discards the discontinuity indicator (verify in code).

Plumb the indicator through `ssr.get_phase_bias()` so the bias
application site at `realtime_ppp.py:880` receives both the value
and the indicator.  When the indicator changes for a (SV, signal),
that is **the AC's authoritative signal** that a new segment has
begun — more reliable than our threshold-based delta detection in
Path A.

This handles:

- Sub-cycle bias steps that are still segment-boundaries by AC
  convention (Path A's heuristic threshold misses these).
- Stream-drop scenarios where Path A would mis-attribute a stale
  value as continuous (the indicator persists across the drop and
  can be checked on resume).

### B.2 Separate raw-phase MW for slip detection

The MW combination is mathematically the same regardless of which
phase you feed in, but the *interpretation* differs:

- **Raw-phase MW**: jumps only when the receiver's tracking loop
  slips.  Correct for slip detection.  Cannot resolve to integer
  (no AC alignment).
- **Bias-corrected MW**: jumps on receiver slip OR AC bias-segment
  boundary.  Required for AR (its WL ambiguity is the integer the
  AC has aligned).

Maintain TWO `MelbourneWubbenaTracker` instances:

- `mw_tracker_raw` (slip detection): consumes `phi*_raw_cyc`, used
  by `CycleSlipMonitor` only.  Immune to bias-segment boundaries.
- `mw_tracker_ar` (current single tracker): consumes `phi*_cyc`
  (bias-corrected), used by AR.  Reset on real slips (via
  `flush_sv_phase`) but otherwise carries integer ambiguity state.

This pattern matches what we already do for GF (line 245-252 of
`cycle_slip.py` — GF detector uses raw, AR-side ambiguity-resolution
uses bias-corrected through the filter).

### When to do Path B

- After tonight's overnight succeeds (need a stable baseline before
  refactoring slip-detection internals).
- Before the next major SSR-stream A/B (Path B closes the
  "are these slips real or AC-boundary?" question definitively;
  Path A only catches the obvious cases).
- Before pushing for NL ambiguity resolution at scale.  Path A is
  enough for WL-only.  NL benefits from the cleaner slip signal
  Path B provides.

Estimate: ~half day for B.1 + B.2 together with regression tests.

## Cross-references

- Bravo's day0426 cycle-slip diff that first quantified the over-firing:
  `project_to_main_cycle_slip_diff_result_20260426.md` (memory)
- Bravo's trajectory analysis showing the basin-trapping cost of
  state-machine churn:
  `project_to_main_trajectory_analysis_day0426_20260426.md` (memory)
- 2026-04-19 ptpmon 7-SV simultaneous gf_jump flush (the original
  observation that drove GF to use raw phase): comment in
  `scripts/peppar_fix/cycle_slip.py` lines 245-254
- Slip-rate-limiter (helps with subsequent fires on same SV but does
  not address the initial multi-SV bias-step storm): commit cc3eec6
