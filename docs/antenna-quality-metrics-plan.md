# Antenna and Mount Quality Metrics — Collection and Assessment Plan

Collect per-satellite signal quality data as peppar-fix runs, then
post-process it to produce antenna and mount quality assessments.
Builds on the testAnt framework (bobvan/testAnt) but integrated into
peppar-fix so metrics accumulate during normal operation without a
separate capture rig.

## Prior art: testAnt

The testAnt repo has a complete antenna evaluation pipeline that we
should reuse concepts from, not reimplement:

| testAnt component | What it does | Reuse in peppar-fix? |
|---|---|---|
| `log_snr.py` | Logs per-SV C/N0, elevation, azimuth from NAV-SAT + GSV | Model for our per-SV collector |
| `analyze_snr.py` | Per-antenna C/N0 statistics, time series, comparisons | Post-processing template |
| `analyze_rawx.py` | Code-minus-carrier (CMC) multipath, cycle slip raster, slip rate vs C/N0 and elevation | Key analysis — port to peppar-fix |
| `report_card.py` | One-page PDF: polar C/N0 heatmap, CMC noise floor, ADEV, lock loss, SV count | Target output format |
| `report_plots.py` | Shared metrics: C/N0 mean, multipath (CMC), ADEV τ=1s, carrier lock loss %, SV count | Metric definitions to adopt |

testAnt runs as a standalone dual-receiver comparison rig.  peppar-fix
runs as a single-receiver discipline loop.  The key difference: testAnt
captures dedicated SNR/RAWX CSVs with a separate logger; peppar-fix
should collect the same data as a side effect of normal operation.

## What peppar-fix already has (but doesn't log)

The codebase exploration reveals significant data computed and discarded
each epoch:

| Data | Where computed | Logged? |
|---|---|---|
| Cycle slips (per-SV, lock_duration_ms) | `solve_ppp.py:291` | No — slip detected, ambiguity reset, nothing recorded |
| C/N0 per SV per frequency | `realtime_ppp.py` from RXM-RAWX `cno` field | No — used for measurement weighting, then discarded |
| Elevation per SV | `solve_ppp.py:303` `compute_elevation()` | No — used for weighting, then discarded |
| Azimuth per SV | Not computed | — |
| Melbourne-Wübbena per SV | `ppp_ar.py:32-115` | No — tracked internally for AR, not logged |
| NAV-SAT messages | Received if enabled (every 5 epochs) | No — counted, then discarded |
| Per-SV ambiguity status | Filter state `sv_to_idx` | No — only `n_meas` total logged |
| Code-minus-carrier multipath | Not computed | — |

The servo CSV has 43+ columns but zero per-SV detail.

## Plan

### Phase 1: Per-SV metrics CSV (sidecar file)

Add a **per-SV sidecar CSV** alongside the existing servo CSV.  One row
per satellite per epoch.  This keeps the servo CSV lean while capturing
the full per-SV picture.

**File**: `data/<run>_sv_metrics.csv`

**Columns**:

```
gps_second,sv,constellation,frequency,
elevation_deg,azimuth_deg,
cno_dBHz,
lock_duration_ms,cycle_slip,
cmc_m,
mw_wl_frac,mw_wl_sigma,
carrier_phase_m,pseudorange_m,
used_in_filter,ambiguity_fixed
```

**Where the data comes from**:

| Column | Source | New code needed? |
|---|---|---|
| `elevation_deg` | `PPPFilter.compute_elevation()` — already computed, just not returned | Return from filter.update() |
| `azimuth_deg` | Same satellite position vector — compute atan2 | ~5 lines in compute_elevation() |
| `cno_dBHz` | RAWX `cno` field, already in obs dict | Pass through |
| `lock_duration_ms` | RAWX `locktime` field, already used for slip detection | Pass through |
| `cycle_slip` | `lock_duration_ms` decrease detection in solve_ppp.py | Already detected, just flag it |
| `cmc_m` | `pseudorange - carrier_phase * wavelength` (detrended) | ~10 lines, same as testAnt's analyze_rawx.py |
| `mw_wl_frac` | `ppp_ar.py` MW tracker — already computed per SV | Expose from MelbourneWubbena state |
| `used_in_filter` | Whether the SV passed quality checks this epoch | Already known |
| `ambiguity_fixed` | AR status (float/WL-fixed/NL-fixed) | From ppp_ar state |

**Implementation**: Add a `SvMetricsLogger` class in the engine that
receives per-SV data from the filter update and writes the sidecar CSV.
Enable with `--sv-metrics-log <path>`.  Off by default (zero overhead
when not needed).

### Phase 2: NAV-SAT extraction (optional, complementary)

NAV-SAT provides elevation and azimuth directly from the receiver's
navigation solution (no satellite position computation needed).  It
also provides tracking state flags and per-signal C/N0.

If the filter's computed elevation/azimuth prove noisy (e.g., during
position convergence), use NAV-SAT as the authoritative source.

**Implementation**: In `gnss_stream.py`, parse NAV-SAT messages and
emit per-SV records to the same sidecar CSV.  NAV-SAT arrives every
5 epochs (configured in receiver driver), so interpolate or hold
elevation/azimuth between updates.

### Phase 3: Post-processing tools

Port testAnt's analysis scripts to work with peppar-fix's sidecar CSV
format.  The core analyses, adapted from testAnt:

#### 3a. Polar C/N0 heatmap

Bin C/N0 by (azimuth, elevation) into a polar plot.  testAnt's
`polar_cno_heatmap()` in `report_plots.py` does exactly this.

**What it reveals**: Antenna gain pattern, obstructions (buildings,
trees), mount-induced nulls.  A good antenna shows uniform C/N0
above 10° elevation with no azimuthal holes.

#### 3b. Code-minus-carrier (CMC) multipath analysis

CMC = pseudorange − carrier_phase × wavelength (detrended to remove
ambiguity and clock).  The residual is dominated by code multipath.

testAnt's `analyze_rawx.py` produces:
- CMC time series per signal (detrended)
- CMC std vs elevation bins → multipath elevation profile
- CMC polar skyplot → directional multipath map
- CMC noise floor per signal (overall σ)

**What it reveals**: Multipath environment.  Ground-bounce multipath
shows up as elevated CMC at low elevation.  Reflective surfaces nearby
show as azimuthal hot spots.

#### 3c. Cycle slip analysis

From the `cycle_slip` flag in the sidecar CSV:
- Slip rate per SV, per constellation, per signal
- Slip rate vs elevation → low-elevation SVs slip more
- Slip rate vs C/N0 → weak signals slip more
- Slip raster plot (time × SV, color = signal)
- Slip rate by azimuth → directional obstructions cause slips

testAnt's `analyze_rawx.py` already has slip timeline and
slip-vs-quality plots.

**What it reveals**: Signal quality problems.  High slip rates at
specific azimuths indicate obstructions.  High slip rates at all
azimuths indicate antenna/cable problems or RF interference.

#### 3d. SV count time series

Per-constellation tracked SV count over time.  testAnt logs this
in its SNR CSV.

**What it reveals**: Sky visibility.  Gaps in SV count at specific
times of day indicate periodic obstructions (e.g., a building that
blocks certain orbital planes).

#### 3e. Melbourne-Wübbena stability

From the MW tracker state:
- MW fractional part convergence time per SV
- MW σ per SV → per-SV code noise quality indicator
- MW σ vs elevation → code noise elevation dependence

**What it reveals**: Dual-frequency code quality, which affects
wide-lane AR fix time.

### Phase 4: Report card (PDF)

Adapt testAnt's `report_card.py` to produce a one-page PDF from a
peppar-fix run.  The testAnt report card already defines the five
summary metrics:

| Metric | Unit | Worst | Best |
|---|---|---|---|
| C/N0 mean | dBHz | 25 | 50 |
| Multipath (CMC) | m | 1.0 | 0.03 |
| ADEV @ τ=1 s | ns | 10 | 0.1 |
| Carrier lock loss | % | 10 | 0.01 |
| Satellite count | SVs | 4 | 30 |

Layout: five-metric gauge bar at top, polar C/N0 heatmap, five
sparkline time series (C/N0, CMC, ADEV, lock loss, SV count),
slip raster at bottom.  24-hour minimum for full evaluation; shorter
runs marked PROVISIONAL.

## Implementation priority

1. **Phase 1** (per-SV sidecar CSV) — highest value, moderate effort.
   Most data is already computed; the main work is plumbing it out of
   the filter and into a CSV writer.  ~200 lines of new code.

2. **Phase 3a+3c** (polar C/N0 + cycle slip analysis) — immediate
   diagnostic value.  Port from testAnt with format adaptation.

3. **Phase 3b** (CMC multipath) — requires Phase 1 CMC column.
   testAnt's analyze_rawx.py is the template.

4. **Phase 4** (report card PDF) — the payoff.  Once the data
   pipeline works, the report card is mostly layout.

5. **Phase 2** (NAV-SAT) and **Phase 3d-3e** — nice to have,
   lower priority.

## File changes

| File | Change |
|---|---|
| `scripts/solve_ppp.py` | Return per-SV elevation, azimuth, slip flag from `filter.update()` |
| `scripts/realtime_ppp.py` | Pass per-SV C/N0, lock_duration through to engine |
| `scripts/peppar_fix_engine.py` | Add `SvMetricsLogger`, `--sv-metrics-log` flag |
| `scripts/ppp_ar.py` | Expose MW state and AR status per SV |
| `tools/antenna_report.py` | New — post-processing: reads sidecar CSV, produces plots + PDF |

## Relationship to testAnt

testAnt is a dedicated antenna comparison rig (two receivers, A/B
testing).  peppar-fix is a clock discipline system.  The antenna
quality metrics here are a byproduct of normal operation, not the
primary goal.  The two systems share analysis logic but differ in
data collection:

- **testAnt**: Separate `log_snr.py` capture, offline analysis only,
  dual-receiver comparison (REF vs DUT).
- **peppar-fix**: Sidecar CSV during live operation, single receiver,
  self-assessment rather than comparison.

Long-term, the shared analysis code (CMC computation, polar heatmaps,
slip statistics, report card layout) should live in a shared library.
Short-term, copy and adapt from testAnt.
