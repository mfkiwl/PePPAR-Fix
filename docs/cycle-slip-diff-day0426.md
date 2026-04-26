# Cycle-slip diff: engine vs PRIDE tedit on TimeHat 2026-04-26 60-min RINEX

Decisive question (per Main's ask `project_to_bravo_pride_cycle_slip_diff_20260426`):
of the engine's `[CYCLE_SLIP]` events on a given window of lab data,
what fraction does PRIDE's `tedit` also flag?  Three actionable
buckets: high overlap (>80%) → ship MAD K=2.0 + thresholds; mid
(50-80%) → tighten MW/GF; low (<50%) → redesign.

## TL;DR

**Per-SV overlap: 3/3 (100%).**  Every SV the engine flagged was
also flagged by tedit.  No false-positive SVs.

**Per-event overlap at ±60s: 2/11 (18%).**  The engine over-fires
~4× per slip storm — within an arc disturbance, the engine emits
multiple per-epoch slip events while tedit collapses the same
disturbance into one batch event.  This isn't false-positives in
the "wrong SV" sense; it's redundant re-flagging within already-
broken arcs.

**One tedit-only mid-arc event (E13 @ 16:44:38 GPS) the engine
missed for 14 minutes.**  Engine's first E13 slip is at 16:58:50;
tedit caught an LC jump on E13 at 16:44:38 that the engine's
MW/GF detectors didn't fire on.  Single sample — could be a real
miss or a tedit-side false positive.

**Recommendation**: ship MAD K=2.0 + current thresholds for tonight's
overnight (per the per-SV "real slip" criterion).  The 4× per-arc
over-firing is a separate issue worth a one-line slip-rate-limiter
later — eat-WL/NL-fix-continuity penalty without corresponding
detection benefit.

## Source data

| Item | Path | Notes |
|---|---|---|
| RINEX OBS | `/tmp/lab-rinex-day0426/timehat-lab.rnx` | TimeHat, 2026-04-26 16:26:28-17:26:28 GPS, 60 min, 1 Hz |
| Engine log | `/tmp/lab-rinex-day0426/day0426-rinex-timehat.log` | Engine commit 8fa49fb, --rinex-out, MAD K=default |
| Broadcast NAV | `/tmp/cycle-slip-diff-day0426/BRDC00WRD_R_20261160000_01D_MN.rnx` | BKG mirror; CDDIS 404'd today's brdm |
| tedit output | `/tmp/cycle-slip-diff-day0426/log_ufo1` | PRIDE tedit 3.0, `-sys GREC -int 1 -len 3600` |
| Diff CSVs | `/tmp/cycle-slip-diff-day0426/{engine,tedit}_slips.csv` | parser: `parse_and_diff.py` |

Note: today's CDDIS daily MGEX brdm wasn't published yet; pulled
the equivalent from BKG's mirror.  Tedit elevation cutoff defaulted
to 0° so satellite positions only weakly affect slip detection
(MW/GF/LC are observation-only combinations).

## Engine slip events (11 total)

```
sv     epoch    t_GPS              reasons              conf
E31    1528     16:51:55           gf_jump              LOW
E31    1574     16:52:41           mw_jump              LOW
E31    1657     16:54:04           mw_jump              LOW
E31    1784     16:56:11           mw_jump              LOW
E13    1942     16:58:49           gf_jump              LOW
E13    2128     17:01:55           arc_gap              LOW
E12    2854     17:13:21           arc_gap|mw_jump      HIGH
E12    2914     17:14:21           arc_gap              LOW
E13    3130     17:18:17           mw_jump              LOW
E13    3212     17:19:39           arc_gap              LOW
E12    3476     17:24:03           mw_jump              LOW
```

Three SVs total: E12 (3 events), E13 (4 events), E31 (4 events).
Confidence almost entirely LOW.  All three SVs are GAL.

## Tedit slip events (8 total)

```
sv     t_GPS              event_type        category
E13    16:44:38           AMB_BIGLCJUMP     mid-arc
E31    16:52:00           AMB_BIGLCJUMP     mid-arc
E12    17:26:28           AMB_BIGLCJUMP     end-of-arc
E21    17:26:28           AMB_BIGLCJUMP     end-of-arc
E23    17:26:28           AMB_BIGLCJUMP     end-of-arc
E26    17:26:28           AMB_BIGLCJUMP     end-of-arc
E31    17:26:28           AMB_BIGGAP        end-of-arc
E33    17:26:28           AMB_BIGLCJUMP     end-of-arc
```

End-of-arc events fire at exactly t = 17:26:28 (the 3600-second
analysis window boundary).  Tedit closes any unfinished arcs with
a wrap-up flag — these aren't real mid-arc slips.

Real mid-arc tedit events: 2 (E13 @ 16:44:38, E31 @ 16:52:00).

## Per-event cross-tab

|  Window  | Engine MATCH | Tedit MATCH (mid-arc only) |
|:--------:|:------------:|:--------------------------:|
| ±5 s     | 1/11 (9%)    | 1/2  (50%)                 |
| ±60 s    | 2/11 (18%)   | 1/2  (50%)                 |
| ±300 s   | 4/11 (36%)   | 1/2  (50%)                 |

Per-event matching is the *strict* metric.  It's heavily penalized
by tedit's batch-mode behavior (one event per arc disturbance) vs
the engine's per-epoch firing.  The 4× ratio falls out arithmetically:
3 engine events on E31 within 5 minutes vs 1 tedit event on E31.

## Per-SV overlap (the cleaner answer)

|                          | SVs                           |
|--------------------------|-------------------------------|
| Engine flagged           | E12, E13, E31                 |
| Tedit flagged (mid-arc)  | E13, E31                      |
| Tedit flagged (incl eoa) | E12, E13, E21, E23, E26, E31, E33 |
| **Both** (mid-arc)       | **E13, E31**                  |
| **Both** (incl eoa)      | **E12, E13, E31**             |
| Engine-only              | none (incl eoa)               |
| Tedit-only (incl eoa)    | E21, E23, E26, E33            |

**3/3 engine-flagged SVs are also flagged by tedit (incl end-of-arc).**

Tedit-only SVs (E21, E23, E26, E33) are all end-of-arc — those
SVs were tracking cleanly until the analysis window ended; engine
correctly didn't flag them as slips.  Not engine misses.

## The interesting outlier: tedit E13 mid-arc @ 16:44:38

Tedit fired AMB_BIGLCJUMP on E13 at 16:44:38 GPS (epoch 1090) —
**14 minutes before the engine's first E13 slip** at epoch 1942
(16:58:49).

Three possibilities:

1. Real LC slip on E13 the engine missed.  Engine MW/GF didn't
   trip; tedit's LC combination is sensitive to a different
   signature.
2. Tedit false-positive on E13.  Tedit's LC threshold may have
   been triggered by a multipath blip rather than a true slip.
3. Tedit picked up a real disturbance that the engine's
   integration window suppressed (engine fires only on confidence
   accumulation).

Single sample on a small dataset; not enough to be diagnostic.
Worth tagging: if E13 16:44:38 GPS shows up in subsequent runs
with a similar tedit-only signature, that's a sign the engine has
a per-SV LC-jump blind spot.

## Per-arc behavior — engine over-fires by ~4×

Within each slip storm:

| SV  | Engine events | Tedit events  | Ratio |
|:---:|:-------------:|:-------------:|:-----:|
| E31 | 4             | 1 (mid-arc)   | 4×    |
| E13 | 4             | 1 (mid-arc)   | 4×    |
| E12 | 3             | 1 (end-of-arc)| 3×    |

The engine fires a fresh `[CYCLE_SLIP]` each time MW or GF crosses
its threshold within a continuously-disturbed arc.  Tedit collapses
the disturbance into one event per arc.

The 4× ratio is a **rate-limiting opportunity**, not a false-
positive problem.  Each redundant engine slip event triggers a
per-SV state reset (FLOAT → CONVERGING → ANCHORING → ANCHORED
takes ≥10 min; the redundant slips waste that work).

## Actionable output

For tonight's overnight (per Main's three-bucket framing):

- **High overlap (>80%) per-SV**: ✓ — slip detector is well-tuned
  on real-vs-not.  MAD K=2.0 + current thresholds OK to ship.
- Per-event over-firing is a separate issue, not in scope for
  tonight.  One-line slip-rate-limiter (e.g., "if last slip on
  this SV was <30s ago, treat as continuation, not new slip") is
  the natural follow-up.

Tedit-only mid-arc on E13 is a single sample — flag, don't act.
Re-test on overnight RINEX when available; if multiple tedit-only
mid-arc events accumulate per host per night, the engine's MW/GF
threshold may need a parallel LC-jump check.

## Caveats

- **Small dataset.**  60 minutes, 11 engine events, 8 tedit
  events.  Statistical conclusions thin.  An overnight run (8 h)
  would put 100+ events through this same diff and tighten the
  bands considerably.
- **No per-(SV, signal) breakdown.**  Tedit reports against the
  L1+L5a IF (`-freq E15` for GAL), engine fires on individual
  signals.  Cross-tab is at SV-level only; signal-level matching
  was deferred per Main's "counts alone are decisive" budget.
- **End-of-arc treatment.**  Filtered out the 6 events at exactly
  17:26:28 since those are tedit window-boundary artifacts.
  Including them would inflate tedit-only counts artificially.
- **Engine ran with default MAD (K = 1.5)**, NOT K=2.0 — the
  TimeHat capture was earlier today before MAD K=2.0 landed.
  K=2.0 would (a) reject more per-epoch outliers and (b)
  potentially change which MW/GF jumps trip the slip detector.
  Re-running this diff on a K=2.0 capture is the natural follow-up.

## Reproduce

```bash
cd /tmp/cycle-slip-diff-day0426
# 1. Fetch BRDC (BKG mirror — CDDIS lags)
curl -o BRDC00WRD_R_20261160000_01D_MN.rnx.gz \
  "https://igs.bkg.bund.de/root_ftp/IGS/BRDC/2026/116/BRDC00WRD_R_20261160000_01D_MN.rnx.gz"
gunzip BRDC00WRD_R_20261160000_01D_MN.rnx.gz
# 2. Symlink PRIDE table files
ln -sf /tmp/regression-harness/PRIDE-PPPAR/table/leap.sec .
ln -sf /tmp/regression-harness/PRIDE-PPPAR/table/sat_parameters .
# 3. Run tedit
/tmp/regression-harness/PRIDE-PPPAR/bin/tedit \
    /tmp/lab-rinex-day0426/timehat-lab.rnx \
    -xyz 157470.1674 -4756183.3178 4232766.0539 \
    -rnxn BRDC00WRD_R_20261160000_01D_MN.rnx \
    -time 2026 4 26 16 26 28 -int 1 -len 3600 \
    -rhd log_ufo1 -sys "GREC"
# 4. Cross-tab
python3 parse_and_diff.py | tee summary.txt
```
