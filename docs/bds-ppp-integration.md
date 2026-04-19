# BeiDou (BDS) PPP integration — research notes

*Compiled 2026-04-19 while investigating the long-standing "BDS
produces 1500 ns ISB" symptom in CLAUDE.md's "Known Broken Things"
section.  Incorporates overnight evidence, PR #2 and PR #3 fixes,
and the outstanding TGD handling gap.*

## The observed problem

With `--systems gps,gal,bds` and CNES SSRA00CNE0 corrections, the
PPP filter's estimated BDS inter-system bias (`IDX_ISB_BDS`) settles
around **1500 ns** when it should settle within **200 ns**.  ISB
magnitudes of that size leak into position and clock and prevent
clean AR on BDS satellites.

Memory `project_phase1_convergence_threshold` and CLAUDE.md ascribed
this to "BDT/GPST 14-second offset handling" in `broadcast_eph.py`.
That attribution is **wrong or at best only partially correct**:
`broadcast_eph.py:375` `_bds_seconds_of_week()` applies the 14 s
offset correctly when converting the GPST observation epoch into
BDT SOW for tk/dt_clk computation.  The ~1500 ns bias comes from
elsewhere.

## Two real contributors identified

### 1. RTCM 1260 BDS code-bias signal-code map was wrong (FIXED in PR #3, commit 150c495)

`_RTCM_SSR_SIGNAL_MAP` in `scripts/ssr_corrections.py` had six BDS
entries, three with wrong labels and most of the 0–11 sig-id range
missing entirely.  From RTCM 3.3 Amendment 1 Table 3.5-106 (matches
RTKLIB's `ssr_sig_bds[]`):

| sig_id | Signal | RINEX code |
|---|---|---|
| 0 | B1I | L2I |
| 1 | B1Q | L2Q |
| 2 | B1 I+Q | L2X |
| 3 | B3I | L6I |
| 4 | B3Q | L6Q |
| 5 | B3 I+Q | L6X |
| 6 | B2I | L7I |
| 7 | B2Q | L7Q |
| 8 | B2 I+Q | L7X |
| 9 | B2a I | L5I |
| 10 | B2a Q | L5Q |
| 11 | B2a I+Q | L5X |

The old map had sig_id=6 as `L5Q` (B2a Q) and sig_id=9 as `L7I` (B2I).
Our F9T L5-hardware fleet tracks BDS-3 MEO on **B1I + B2a-I** (sig_ids
0 and 9), looking up RINEX codes `C2I` and `C5I`.  Before PR #3:

- `sig_id=0` (B1I) stored as `L2I` → F9T code-bias lookup for `C2I` HIT. OK.
- `sig_id=9` (B2a-I) stored as `L7I` → F9T code-bias lookup for `C5I` MISS.
  **Every F9T BDS L5-band observation went uncorrected.**

A systematic per-SV code-bias gap that's the same magnitude on every
satellite in a constellation produces exactly the 1500-ns ISB
symptom — the filter absorbs a per-constellation offset and stops.

**Impact**: PR #3 may close most or all of the observed 1500 ns ISB
on its own.  Diagnostic run with the fix is the next step.

### 2. BDS broadcast clock is referenced to B3I, not to any L1/L2 IF (OPEN)

Per BDS-3 ICD and ION NAVI 2022 paper [navi.526](https://navi.ion.org/content/69/3/navi.526):

- BDS-3 broadcasts `af0/af1/af2` clock polynomial referenced to
  **B3I** signal.
- `TGD1` = group delay B1I vs B3I (typical 5–15 ns per SV).
- `TGD2` = group delay B2a vs B3I (BDS-3) or B2I vs B3I (BDS-2)
  (typical 10–30 ns per SV).

To use the broadcast (or SSR-corrected) BDS satellite clock on
arbitrary F9T observations:

- Single-freq B1I: `clk_used = clk_poly − TGD1`
- Single-freq B2a-I: `clk_used = clk_poly − TGD2`
- IF combination of B1I+B2a-I:
  `clk_used = clk_poly − α₁·TGD1 − α₂·TGD2`
  where α₁ = f₁² / (f₁² − f₂²), α₂ = f₂² / (f₁² − f₂²) for the
  B1I/B2a-I frequency pair.

Our `scripts/broadcast_eph.py`:

- `_BDS_MAP` at line 204–229 maps only `tgd` (DF513, which is TGD1).
- `_sat_clock` at line 118–138 subtracts a single `tgd` value.  This
  was sized for the GPS "L1 with TGD offset from IF" convention; for
  BDS it's approximately right for B1I-only and wrong for any other
  signal or combination.

Consequences:

- **Per-SV residual** of roughly `α₁·TGD1 + α₂·TGD2 − TGD1` = a few
  ns of variation across the BDS-3 MEO constellation.  Filter absorbs
  into ISB and per-SV phi residuals.
- **Not the dominant cause** of the 1500 ns ISB — the per-SV variance
  is tens of ns, not a constellation-average 1500 ns.  But it limits
  how low ISB can go even after PR #3.

**Open work**:

1. Audit `_BDS_MAP` entries for RTCM 1042 (BDS-2) vs RTCM 1046
   (BDS-3).  Confirm TGD1 field number per message type.
2. Extend `_BDS_MAP` to decode TGD2 (RTCM 1042 field number = DF514
   per RTCM 3.3; BDS-3 via RTCM 1046 uses a different DF — verify).
3. Refactor `_sat_clock` for BDS to return per-frequency clock
   corrections, or provide both TGDs to the caller so the IF
   combination is applied in the PPP filter.

### 3. BDS-2 vs BDS-3 differences worth knowing

Per [navi.705 ION 2025](https://navi.ion.org/content/72/3/navi.705)
and [navi.526 ION 2022](https://navi.ion.org/content/69/3/navi.526):

- BDS-2: GEO + IGSO + MEO.  We filter GEO/IGSO out via
  `BDS_MIN_PRN=19`.
- BDS-3: MEO (mainly).  Visible from mid-latitudes.
- Apparent clock and TGD biases between BDS-2 and BDS-3 — broadcast
  ephemeris clock datum is not fully consistent between generations.
  ACs (including CNES) may align their SSR clock corrections to one
  generation and leave the other with a residual offset.
- Correcting TGD on BDS-3 improves SPP performance 0.3–31.8% per the
  ION 2025 paper, depending on signal and receiver.

## Time-system handling (already correct)

`BDT = GPST − 14 s` as of BDS ICD.  `broadcast_eph.py:375`
`_bds_seconds_of_week` applies this offset when converting GPST
observation epoch → BDT SOW for ephemeris propagation (`tk`) and
clock-polynomial evaluation (`dt_clk`).  This is the standard RTKLIB
approach; no change needed.

## Inter-System Bias (ISB) model (already correct)

Per [MDPI remotesensing 15/9/2252 (2023)](https://www.mdpi.com/2072-4292/15/9/2252)
and [Springer GPS Solutions 2023 doi:10.1007/s10291-023-01474-w](https://link.springer.com/article/10.1007/s10291-023-01474-w):

- Pick one constellation as reference for the receiver clock
  (`IDX_CLK`).
- For every other constellation, estimate an ISB state that absorbs
  the per-constellation datum + receiver-chain group-delay
  difference from the reference.
- Our `solve_ppp.py` does this with `IDX_CLK + IDX_ISB_GAL +
  IDX_ISB_BDS` and GPS as the reference when present.  Architecture
  is sound.

What the ISB must NOT absorb is per-SV biases — those are TGDs and
code biases that should be applied per-SV before the ISB state sees
the observation.  PR #2 + PR #3 plug per-SV code-bias leaks; the
TGD story is the remaining per-SV leak.

## Diagnostic plan (minimum effort to close out the 1500 ns question)

1. Run GAL+BDS on one host with CNES-only (no WHU fusion) on
   commit `9f307fb` (post PR #3).
2. Watch the PPP filter's `IDX_ISB_BDS` state after 10–20 min
   convergence.  Already logged; grep `ISB_BDS` from the engine
   log.
3. Also watch BDS per-SV phi residuals (`NL residuals` log line).
   If they're flat sub-cm, per-SV bias handling is complete.  If
   they show 5–30 cm structure per SV, TGD work is still needed.
4. Decision point: if ISB < 200 ns and residuals are clean, declare
   BDS fit-for-AR and move on to enabling BDS in the three-host AR
   run.  If not, proceed to the TGD implementation work.

## References

- [navi.526 ION 2022 — BDS-2/3 satellite group delay characterization](https://navi.ion.org/content/69/3/navi.526)
- [navi.705 ION 2025 — BDS-3 TGD error modeling for ARAIM](https://navi.ion.org/content/72/3/navi.705)
- [SHAO Zhang 2019 — Apparent clock and TGD biases BDS-2 vs BDS-3](http://center.shao.ac.cn/shao_gnss_ac/publications/2019_publications/Zhang2019_Article_ApparentClockAndTGDBiasesBetwe.pdf)
- [MDPI 2023 — Multi-GNSS PPP ISB characterization](https://www.mdpi.com/2072-4292/15/9/2252)
- [Springer GPS Solutions 2023 — Multi-GNSS PPP receiver-clock handling](https://link.springer.com/article/10.1007/s10291-023-01474-w)
- [Satellite Navigation 2023 — BDS-3 PPP-B2b time transfer](https://satellite-navigation.springeropen.com/articles/10.1186/s43020-023-00097-3)
- BDS ICD (Public Service Signal B1I, B3I) — <https://en.beidou.gov.cn/SYSTEMS/ICD/>
