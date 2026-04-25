# Cross-AC SSR diagnostic — 2026-04-25

*Investigating why our PPP+SSR solution lands 6–9 m west of Leica
truth on a clear lab day with matched-bias GAL-only signals.*

## Setup

- 3-host UFO1 fleet (TimeHat / clkPoC3 / MadHat) on shared SparkFun
  Spike antenna via splitter, plus ptpmon on PATCH3 (separate antenna,
  excluded from UFO1 truth scoring).
- Test instrument: `scripts/diag_seed_sensitivity.py` (committed
  2026-04-25).  Seeds the engine at known-pos ± offset E,N,U; engine
  pulls toward whatever attractor SSR + obs-model defines; we read the
  end position vs Leica GRX 1200 truth.
- Engine config: `--wl-only --systems gal --clock-model random_walk
  --sigma-phi-if 1.0 --phase-windup --gmf` plus per-test SSR overrides.
- Sensitivity floor: ~1 m, established by day0425c full no-SSR matrix.
  Anything ≥10 m offset is cleanly resolvable; we use ±30 m for
  diagnostic runs since the bias signal is ~9 m.

## Established baselines (day0425c, day0425d)

| SSR provider | UFO1 fleet attractor vs Leica E | inter-host spread |
|---|---|---|
| F9T NAV2 (autonomous, no PPP) | -0.5 to +0.2 m | 0.7 m |
| PPP no-SSR | +0.5 to +3 m (drifting null mode) | <0.5 m |
| PPP + CNES SSRA00CNE0 | **−6 to −8 m** (systematic) | 0.15 m |

**Adding our PPP+SSR makes the position 6-9 m worse than the
autonomous code-only fix.**  Inter-host agreement under SSR is tight,
which rules out per-host noise — it's a systematic obs-model bias on
the SSR application path or in CNES products.

## CAS attempt — invalid for diagnostic

Tried CAS (SSRA01CAS1 on the GA Australian mirror) as a second
provider: if CAS pulls in the same direction as CNES → bug is in our
SSR application code; opposite direction → CNES product reference
issue.  Result was unusable:

- CAS uses IGS-SSR proprietary message IDs (`4076_NNN`) where CNES
  uses standard RTCM (1060/1066/1243/1261).
- Engine routes the messages but warns once at startup:
  ```
  WARNING Phase bias: unmapped signal E sig_id=2 (bias=-0.3377 m)
          — add to signal map
  ```
  CAS's IGS-SSR signal-ID encoding for Galileo doesn't have an entry
  in the engine's bias-map table.  That single GAL signal's bias is
  silently dropped.
- Result: filter divergence not convergence.  +30 m seed → ended -64
  to -337 m E of truth (UFO1 inter-host spread **279 m**).  -30 m
  seed → ended +370 to +384 m E of truth.  σ converged to ~1 m but
  position is wildly wrong.
- The single dropped phase bias (-0.34 m) can't account for 300 m of
  divergence — there's almost certainly a second compatibility issue
  too (orbit/clock IOD matching against broadcast under IGS-SSR
  encoding, message-vintage handling, or per-SV ID translation).

**Conclusion: CAS is not a usable diagnostic until the engine's
IGS-SSR signal-map and IOD-matching paths are fixed.**

## Path forward

### Near-term: cleaner provider comparisons that work today

1. **WHU OSBC00WHU1 paired with CNES orbit/clock** *(in flight as
   day0425g)* — WHU publishes biases only.  Engine has
   `--ssr-bias-ntrip-conf` for exactly this case.  Caveat: CNES still
   provides its own biases on the primary mount, so the test is
   "CNES orbit/clock + (CNES + WHU) biases merged" not pure
   substitution.  Useful as long as WHU's biases differ enough from
   CNES's to shift the result.
2. **Galileo HAS via E6-B** — would be the ideal independent
   diagnostic, but our F9T's don't track E6.  Out of scope without
   different hardware.
3. **MADOCA-PPP** — Japanese AC, different format again.  Same
   compatibility risks as CAS.

### Engine work to enable cleaner diagnostics

A. **`--no-primary-biases` flag** (~10 lines in engine arg parsing
   + bias router) — when set, drop biases from `--ssr-ntrip-conf`
   while still consuming its orbit/clock.  Enables the clean 4-cell
   2x2:

   | orbit/clock | biases | tells us |
   |---|---|---|
   | CNES | CNES | baseline (today: −6 to −8 m E) |
   | CNES | WHU  | does WHU's bias datum shift it toward truth? |
   | broadcast | CNES | does CNES orbit/clock contribute the bias? |
   | broadcast | WHU | clean WHU-only test |

B. **IGS-SSR signal-map fix for CAS / MADOCA** — extends the bias
   table with Galileo `sig_id=2` (likely E1B) under the IGS-SSR
   encoding (vs the RTCM 3.3 encoding we have).  Requires looking at
   the IGS-SSR signal-ID convention table and the CAS source-table
   metadata to confirm.  Likely 5–10 lines of map + a regression
   test.  Independent of (A); can land separately.

C. **IOD matching diagnostics** — add per-epoch logging of "SVs with
   no SSR orbit correction matched" so we can tell when the engine
   is using broadcast orbits unexpectedly because of IOD mismatch.
   Helps diagnose the second part of the CAS divergence.

### Per-correction-class binary search (if 2x2 doesn't isolate)

If the 4-cell 2x2 narrows the bias to "biases" not "orbit/clock"
but doesn't distinguish code-bias vs phase-bias, a finer split
needs engine flags `--no-ssr-code-bias` and `--no-ssr-phase-bias`
(toggle one class while keeping the other).  Same shape: ~10
lines in the bias-router.  Defer until 2x2 results say it's
needed.

## References

- `scripts/diag_seed_sensitivity.py` — test harness
- `/tmp/seed_sensitivity-day0425c-noSSR.csv` — no-SSR baseline
- `/tmp/seed_sensitivity-day0425d-CNES.csv` — CNES full matrix
- `/tmp/seed_sensitivity-day0425e-CAS-broken.csv` — CAS divergence
- `docs/ssr-mount-survey.md` — strategic ranking of SSR providers
- `docs/ssr-requirements-by-receiver.md` — per-F9T-variant signal
  coverage requirements
- Memory `project_seed_sensitivity_test_floor_20260425.md` — full
  matrix calibration result
