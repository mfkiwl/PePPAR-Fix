# BNC `.ppp` log reference

How the BKG NTRIP Client (BNC) PPP engine logs slip / ambiguity / reset
events.  Empirically derived from the day0427night log
(`F9T_PTPMO_20261180000_01D_01S.ppp`, ~24 268 BNC epochs at 2 Hz on
ptpmon, GAL+BDS only).  Use this as a reference when reading BNC
output for cycle-slip ground truth or when building tools that
consume it.

## File location and naming

On `ptpmon`:
- Live: `/home/bob/peppar-bnc-glue/log/F9T_PTPMO_<DDD><HHMM>_01D_01S.ppp`
  (DDD = day-of-year UTC, HHMM = 0000 for the daily file)
- Per-day archives: `bnc.log_YYMMDD` (BNC's own runtime log,
  separate from the PPP solution stream)
- NMEA sibling: `*.nmea`

The PPP file is plain text, append-only, ~55 MB / day for the
GAL+BDS configuration.

## Line types — what each carries

```
$ awk '{
  for(i=1;i<=NF;i++) if($i ~ /^[A-Z]+$/) {print $i; break}
}' bnc.ppp | sort | uniq -c | sort -rn
```

Empirical counts on day0427night (GAL+BDS, ~13.5 h):

| Tag | Count | What it carries |
|---|---|---|
| `AMB` | 303 118 | Per-SV per-epoch IF ambiguity state |
| `RES` | 234 448 | Per-SV per-epoch IF residual (code + phase) |
| `SATNUM` | 48 536 | Per-system SV count per epoch |
| `TRP` | 24 268 | Tropospheric estimate per epoch |
| `PPP` | 24 268 | Epoch boundary marker |
| `RESET` | 912 | Explicit ambiguity-restart event |
| `BANCROFT` | 24 268 | Per-epoch Bancroft-style code-only solution |
| `<MOUNT> X = ...` | 24 268 | Per-epoch PPP solution (ECEF + N/E/U sigmas) |

Note `RESET` is rare; `AMB` is per-epoch per-SV (~12 SVs × 24 268
epochs ≈ 290 000 with some absences).

## Epoch boundary

```
PPP of Epoch 2026-04-28_00:08:48.001
```

Marks the start of one BNC PPP solution epoch.  All lines following
(until the next `PPP of Epoch` line) carry the same timestamp on
the line and belong to that epoch.

## `BANCROFT` — single-point solution

```
2026-04-28_00:08:48.001 BANCROFT:     157473.714  -4756191.209  4232772.028  3.140
```

ECEF X, Y, Z (m) plus a fourth field (clock or solution metric).
Coarse single-point reference; not the PPP fix.

## `SATNUM` — visible SV count by system

```
2026-04-28_00:08:48.001 SATNUM E  6
2026-04-28_00:08:48.001 SATNUM C  0
```

One line per configured system per epoch.  In day0427night only
`E` (Galileo) and `C` (BeiDou) appear — **BNC was not tracking
GPS** for this run.  When validating engine slip events against
BNC, GPS engine events are out-of-scope.

## `RES cIF` / `RES lIF` — per-SV residuals

```
2026-04-28_00:08:48.001 RES cIF E07  -1.2308
2026-04-28_00:08:48.001 RES lIF E07   0.0000
```

`cIF` = code IF combination residual (m).
`lIF` = phase IF combination residual (m).

These are emitted **after** each PPP epoch, one pair per used SV.
Discontinuities in the `lIF` time series for a given SV are direct
evidence of phase events — slips, multipath transients, sub-cycle
disturbances — even when no `RESET` is emitted.

## `AMB lIF` — IF ambiguity state, per SV per epoch

This is the richest stream.  Format:

```
2026-04-28_00:08:48.001 AMB  lIF E07    21.0000    -0.7867 +-  19.9215 el =  57.42 epo =    1
                       │              │           │              │       │           │
                       ts             integer     float-correction  σ(mm)  elev(°)   epoch counter
```

Fields:
- **integer**: the integer part of the IF ambiguity, in cycles.
  The current best integer estimate.
- **float**: the sub-cycle float correction, in cycles.  Total
  ambiguity = integer + float (post-fix).
- **σ**: ambiguity uncertainty in mm.  Tightens as samples
  accumulate within an arc; widens after RESET.
- **elev**: SV elevation, degrees.
- **epo**: an internal counter — empirically increments per epoch
  during a continuous arc (1, 2, 3, …) and **resets to 0 on
  `RESET`** (next-epoch lands at 1).  Caveat: the population
  distribution of `epo` values is dominated by `epo = 1`
  (~80 % of all lines on E07), inconsistent with one `epo = 1`
  per arc.  Likely BNC re-anchors arcs internally for reasons
  beyond `RESET` events (re-fix attempts, partial AR cycles?).
  Treat `epo` as advisory until confirmed against BNC source.

### The cleanest cycle-slip signal: integer-jump events

The integer ambiguity column changes only when BNC's tracker
abandons the prior integer in favour of a new one — i.e., on
real ambiguity discontinuities.  Walk an SV's `AMB lIF` lines
in time order; any adjacent-line change in the integer column
is a cycle-slip event.

Example (E07 around 00:09:06):

```
00:09:00  int=21    float=-1.37  σ= 7.5  epo=7
00:09:02  int=21    float=-1.51  σ= 7.0  epo=8
00:09:04  int=21    float=-1.80  σ= 6.7  epo=9
00:09:06  RESET AMB  lIF E07
00:09:06  int=-4    float=+11.5  σ= 6.3  epo=0
00:09:08  int=-4    float=+12.4  σ= 6.0  epo=1
```

Integer jump 21 → −4 = 25-cycle slip.  RESET was emitted
alongside in this case; many integer jumps occur **without**
an explicit RESET, presumably when BNC's slip-repair logic
adjusts the integer in place.

### Sample distinct E07 integers across day0427night

```
$ grep "AMB  lIF E07" bnc.ppp | awk '{print $5}' | sort -u | wc -l
12
```

Twelve distinct integer values across one SV's night-long arc.
Each transition = one cycle-slip event observed by BNC's solver.

## `RESET AMB lIF` — explicit ambiguity restart

```
2026-04-28_00:09:06.001 RESET AMB  lIF E07
```

BNC's PPP engine has decided this SV's IF ambiguity is no longer
trustworthy and is restarting the float estimate from scratch.
Triggers (RTKLIB internals):
- Cycle-slip detector tripped (GF combination threshold or MW)
- IF residual exceeded a quality threshold
- SV came back into view after a long enough gap

`RESET` is **strictly fewer than** the count of real cycle slips
in the underlying signal — many slips are repaired silently with
just an integer adjustment in the next `AMB` line.  Use `RESET`
as a coarse / lower-bound signal; use `AMB` integer-jumps for
the comprehensive picture.

## `TRP` — tropospheric estimate

```
2026-04-28_00:08:48.001 TRP   2.3355  -0.0000 +-  0.1000
```

Total ZTD (m), gradient (?), σ.  One per epoch.  Useful as a
cross-engine check against the engine's `[AntPosEst]` ZTD
estimate.

## `<MOUNT> X = ...` — PPP solution

```
2026-04-28_00:08:48.001 F9T_PTPMON X = 157473.4672 +- 2.4896 \
    Y = -4756191.2216 +- 6.1453 Z = 4232772.0159 +- 2.6486 \
    dN = -0.0121 +- 3.6982 dE = -0.2474 +- 2.4743 dU = -0.0052 +- 5.5838
```

The PPP fix.  ECEF X/Y/Z + 1-σ in metres, plus dN/dE/dU offsets
from the prior epoch + their σ.

## Summary table for ground-truth selection

| Engine question | Best BNC signal |
|---|---|
| Was this SV's WL integer wrong post-fix? | `AMB lIF` integer jumps (RESET as coarse) |
| Did this SV slip phase? | `AMB lIF` integer jumps + `RES lIF` discontinuity |
| Did this SV's tracking restart? | `RESET AMB lIF <SV>` |
| What's the PPP fix's view of position? | `<MOUNT> X = ...` |
| What's the PPP ZTD estimate? | `TRP` |
| How many SVs per system at epoch t? | `SATNUM` |

## Caveats for cross-engine validation

1. **System coverage** — verify `SATNUM` lines for the systems you
   want to validate.  Today's BNC: GAL + BDS only.  GPS engine
   events have no BNC ground truth in this configuration.
2. **TIM / firmware difference** — BNC's F9T-PTP runs TIM 2.20
   (different from lab F9T-20Bs on TIM 2.25).  Different signals
   tracked, different observation noise.
3. **Cadence mismatch** — BNC outputs at 2 Hz; engine `[AntPosEst]`
   summary at 10-epoch intervals (≈ 10 s for 1 Hz raw data).  Use
   AMB / RES per-epoch streams (also 2 Hz) for the closest cadence
   match.
4. **Ambiguity reference** — BNC's IF ambiguity is a single
   combined value; the engine's WL ambiguity is separate.  Direct
   comparison of integer values is meaningless across engines; only
   the *change events* line up.

— Charlie, 2026-04-28
