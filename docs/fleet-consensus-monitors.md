# Fleet consensus monitors

Runtime cross-host checks on state that's **physically shared
across hosts**.  Detects per-host filter pathologies before
single-host physical-envelope monitors (ztd_impossible et al.)
trip, and does it at the tens-of-mm level rather than the
multi-meter physical-threshold level.

Companion to `docs/peer-state-sharing.md` (Bravo's Phase 1+2a
design) and `docs/clock-state-modeling.md` (single-host null-
mode attack via filter tuning).  Fleet consensus is a
runtime-observability layer on top of Phase 2a's publish
stream — it does not change the filter, only watches.

Driven by the 2026-04-24 finding that **ensemble integer
adoption** doesn't work the way I originally framed it
(`project_to_bravo_phase2_reframe_consensus_20260424.md`).
What *is* genuinely shared across hosts is position (for hosts
on the same antenna) and atmosphere (for hosts on same antenna
or nearby antennas).  Consensus monitors exploit those.

## Cohort types — the critical distinction

Not every host in the fleet can be cross-checked against every
other host the same way.  Two levels of physical sharing:

### Shared-ARP cohort

Two or more hosts whose receivers connect to the same physical
antenna via a splitter.  In our lab: TimeHat + clkPoC3 + MadHat
on UFO1.

**Shared physically**:
- The electromagnetic wave reaching the antenna
- Atmosphere on the LOS
- Satellite state  
- **Antenna reference point (ARP) position**

**Not shared**: per-receiver phase origins, clock states,
tracking loop dynamics, hardware delay.

**Consensus checks that work for shared-ARP cohorts**:
- **Position consensus** — all hosts should converge to the
  same ECEF point (the ARP).  Cross-host `|P_A − P_B|` is a
  direct measurement of per-filter agreement.
- **ZTD consensus** — same antenna = same signal zenith path
  = same atmospheric column.  All hosts should estimate the
  same ZTD residual.
- **Per-SV observation quality** (partial) — slip events on
  the same SV across multiple hosts are antenna-level; solo
  slips are receiver-specific.  Diagnostic, not real-time
  action.

### Shared-atmosphere-only cohort

Two or more hosts at similar geographic sites whose receivers
connect to **different antennas** close enough that the
atmospheric columns overlap substantially.  In our lab: L5
fleet (on UFO1) vs ptpmon (on PATCH3).  Both antennas are at
the DuPage county site; atmospheric state is effectively the
same.

**Shared physically**:
- Atmosphere on LOS (tropospheric delay, ionospheric TEC at
  approximately the same pierce point for a given SV)
- Satellite state

**Not shared**:
- Antenna position (different physical points)
- Multipath environment (different antennas)
- Per-receiver anything

**Consensus checks that work for shared-atmosphere-only
cohorts**:
- **ZTD consensus** — atmosphere is shared even when ARP
  isn't.  Divergence between cohort members' ZTD estimates is
  a filter-pathology signal on one of them.
- **Tropospheric gradient consistency** — if the cohort
  implements horizontal tropo gradients (obs-model plan Phase
  5+), those should agree across nearby sites.

**Consensus checks that do NOT work for shared-atmosphere-only
cohorts**:
- Position consensus.  Different antennas → different ARPs.
  Even two antennas 1 m apart would give distinct positions;
  comparing them is not a consensus check, it's a geometry
  test.

### Separate cohort

Two hosts at different geographic sites.  Share only the
satellite state.  Consensus checks are minimal (maybe IFBs,
maybe satellite clock estimates if we ran our own clocks).
Out of scope for this doc; PePPAR Fix's current fleet has no
geographic separation.

## Cohort declaration

Each peer's `PeerIdentity` (Bravo's Phase 1 schema) already
carries `antenna_ref`.  Two hosts are in the **same
shared-ARP cohort** if and only if their `antenna_ref` strings
match exactly (e.g., both `"UFO1"`).

Hosts in different shared-ARP cohorts but at the same physical
site (same `site_ref` — new field) are in a **shared-atmosphere
cohort**.  The design doc for peer state sharing should add a
`site_ref` field to `PeerIdentity` alongside `antenna_ref`.  In
our lab, all hosts share `site_ref = "DuPage"` (or similar).

The FleetAggregator groups hosts by `(site_ref, antenna_ref)`
to form cohorts.  Monitors run per-cohort, using the appropriate
check set.

## Proposed monitors

### PositionConsensusMonitor (shared-ARP cohort only)

**Purpose**: detect per-host filter divergence from the cohort
consensus position before single-host monitors trip.

**Inputs** (per epoch, from peer bus):
- Self position + position σ
- Peer positions + σs (from shared-ARP cohort)

**Output**: trip event when self-vs-cohort-median exceeds
a threshold.

**Algorithm**:

1. Collect cohort members' recent positions (all hosts in same
   `antenna_ref` cohort, with `position` payloads less than
   10 s old).
2. Compute cohort median position (median-ECEF, or median per
   axis).  Require ≥ 2 peer positions (3 cohort members total)
   for a valid consensus.
3. Compute `δ_self = |P_self − P_median|` in meters.
4. If `δ_self > threshold_m` sustained for `N` epochs: trip.

**Thresholds (initial proposal)**:
- `threshold_m = 0.20` (20 cm) — well above honest noise for
  PPP on the same antenna; below the ~meter-scale drifts that
  null-mode can produce.
- `sustained_epochs = 30` — matches WL-drift window pattern.

**Action on trip**: log a `[POS_CONSENSUS_TRIP]` event.  Feeds
into `FixSetIntegrityMonitor.evaluate()` as an additional
cohort-aware trip condition.  Recovery action is the existing
`ztd_cycling` or full re-init path depending on severity.

**What it catches**: early null-mode excursion on one host
before ZTD crosses 700 mm or altitude drifts meters.  Current
engine catches these 30-45 min late; cohort consensus catches
them in tens of seconds to minutes.

### ZtdConsensusMonitor (both cohort types)

**Purpose**: detect filter pathology via ZTD divergence from
peers sharing atmosphere.  Tighter than the single-host
`ztd_impossible` physical-envelope check.

**Inputs**:
- Self ZTD residual + σ
- Peer ZTDs + σs (from shared-atmosphere cohort, which
  includes shared-ARP subset)

**Algorithm**:

1. Collect cohort ZTDs (fresh `ztd` payloads).  Require ≥ 1
   peer (2 cohort members total).
2. Compute cohort median ZTD.
3. Compute `δ_ztd = |ZTD_self − ZTD_median|`.
4. If `δ_ztd > threshold_m` sustained for `N` epochs: trip.

**Thresholds (initial proposal)**:
- `threshold_m = 0.100` (10 cm).  Real atmospheric variation
  within a few km baseline is typically < 1 cm; 10 cm divergence
  between cohort members is a filter pathology signal, not
  atmospheric differential.
- `sustained_epochs = 60` — ZTD is slow-varying so tolerate a
  longer window before tripping.

**Action on trip**: log `[ZTD_CONSENSUS_TRIP]`.  Feeds into
`FixSetIntegrityMonitor` with `reason='ztd_consensus'`.
Recovery could be the existing ZTD reset + NL-drop action, or
a full re-init — depending on severity.  Start with the softer
action.

**What it catches**: filter hijacking ZTD to absorb residuals
(the null-mode mechanism Bravo characterized on ABMF).  Current
`ztd_impossible` catches this at the |ZTD| > 700 mm physical
threshold; consensus catches it at 10 cm relative threshold —
i.e., tens of minutes earlier in the failure mode.

### SlipCoincidenceMonitor (diagnostic; any cohort)

**Purpose**: track solo-vs-shared slip rates per host over
time.  Aggregate signal for receiver health.  Not a real-time
trip.

**Inputs**: `SlipEvent` topic from all cohort members.

**Algorithm**:

1. For each local slip event, check peer slip events on the
   same SV within ±2 s.
2. Tag local event as `solo` or `shared`.
3. Rolling counters per (host, SV): solo count, shared count,
   total tracked duration.
4. Log periodic summaries per host: `[SLIP_SUMMARY]
   solo=N/min shared=M/min over L minutes`.

**Action**: none real-time.  Summary logging only.
Aggregation over days reveals per-receiver systematic
tracking issues.

**What it catches**: hardware or firmware degradation on
specific receivers.  For instance, if TimeHat's solo rate
creeps from 2/hr to 20/hr over weeks, something's wrong with
TimeHat's receiver or tracking-loop parameters.

## Non-monitors: ensemble ideas that were ruled out

For the record so future-us doesn't re-propose:

- **Slip-discrimination flush-skip** (skip MW flush on solo
  slips): 2026-04-24 data shows 98.5% of solo slips produce a
  new integer on re-fix.  The solo slip is real at the
  receiver-PLL level; flush is correct.  See
  `project_to_bravo_phase2_reframe_consensus_20260424.md`.
- **Direct peer-integer adoption** (take peer's N_WL as a
  LAMBDA candidate): per-receiver phase-counter origins mean
  peer N_WL isn't a valid candidate for another receiver.  See
  `docs/clock-state-modeling.md` section on "what IS shared
  across hosts."
- **Shared atmosphere across arbitrary sites**: PePPAR Fix
  doesn't currently have geographically-separated fleet
  members, and satellite-state cross-checks without co-located
  atmosphere are out of scope.

## Integration with existing monitors

Fleet consensus monitors fit alongside the existing single-host
monitors:

| monitor | acts on | threshold scale | latency to fire |
|---|---|---|---|
| `ztd_impossible` (single-host) | absolute ZTD residual | 700 mm | 60 epochs sustained (30-45 min of damage first) |
| `ztd_cycling` (single-host) | repeated `ztd_impossible` | escalation | 2 trips in 1200 epochs |
| `anchor_collapse` (single-host) | post-anchored SV count | 0 anchors | 60 epochs sustained |
| `window_rms` (single-host) | fix-set PR RMS | 5 m | 30-epoch window |
| **PositionConsensusMonitor** (cohort) | self-vs-cohort-median-position | 20 cm | 30 epochs sustained |
| **ZtdConsensusMonitor** (cohort) | self-vs-cohort-median-ZTD | 10 cm | 60 epochs sustained |

The consensus monitors fire **30 × to 300 × earlier** than the
physical-threshold single-host monitors (tens of mm vs. hundreds
of mm).  This is the main value.

## Dependencies

- **Bravo's Phase 2a (landed, 443927c)**: engine self-publishes
  `position`, `ztd`, and `heartbeat` under `--peer-bus
  udp-multicast`.  These are the inputs.
- **`PeerIdentity.site_ref` field** (new, small schema
  addition): lets the aggregator distinguish shared-ARP cohorts
  from shared-atmosphere cohorts.  Single string field,
  configured per host at startup.
- **`FleetAggregator` cohort grouping** (extension of existing
  fleet mode): group hosts by `(site_ref, antenna_ref)`; expose
  `cohort_median_position(antenna_ref)` and
  `cohort_median_ztd(site_ref)` APIs for monitors.
- **Engine-side subscriber** to consume aggregator state
  back-into-filter: monitors run on each engine instance
  against its own state + cohort state.

## Implementation sketch

Two parts, each session-scale:

### Part 1: aggregator extensions + subscription

- Add `site_ref` to `PeerIdentity`.
- Extend `FleetAggregator` with cohort-median APIs.
- Engine subscribes to peer `position` + `ztd` topics via
  `peppar_bus` (complement to the already-wired publish path).
- No filter-state changes.  Just consumption and logging.

### Part 2: consensus monitors wired into FixSetIntegrityMonitor (landed cbb7126)

Landed as two new reasons directly on `FixSetIntegrityMonitor`
(no new monitor classes — piggybacks the existing stateless-per-
eval pattern):

- `reason='pos_consensus'` — |self − cohort_median 3D| > 20 cm
  sustained 30 epochs → full re-init (same remediation as
  `anchor_collapse`).
- `reason='ztd_consensus'` — |self ZTD − cohort_median ZTD|
  > 10 cm sustained 60 epochs → NL-drop + ZTD reset (same
  remediation as `ztd_impossible`).

Thresholds + sustained counts are constructor kwargs, tunable
without redeploy.  `evaluate()` gained two new optional kwargs
(`pos_consensus_delta_m`, `ztd_consensus_delta_m`); passing
`None` silently skips the consensus check — so single-host runs
and hosts without `--peer-bus` behave bit-exact as before.

Engine-side, cohort deltas are computed at every AntPosEst
epoch (not at the 10-epoch [COHORT] log cadence) so the
monitor's sustained-epoch counter advances at the right rate.

Tests in `scripts/peppar_fix/test_fix_set_consensus.py` — 10
cases covering sustained-delta trip, below-threshold silence,
timer reset on dip, None passthrough, record_trip clearing both
consensus latches, and consensus pre-empting `ztd_impossible`
when both would trip.

**Deployment note**: for consensus to fire, L5 hosts need
`--peer-bus udp-multicast --peer-antenna-ref UFO1 --peer-site-ref
DuPage` added to engine args.  Also: A/B runs with two different
clock_models on the same cohort will spuriously trip consensus
because the arms settle at different positions — consensus
requires a matched-tuning cohort to be meaningful.

## Validation

- Overnight run with cohort in consensus: should see no trips.
- Induce divergence artificially (e.g., adjust σ_phi on one
  host): should see consensus trips within tens of seconds.
- Compare to current `ztd_impossible` latency on sunrise-storm
  data (yesterday's `day0423j-pcv` logs): expected 10-30
  minutes earlier detection with consensus.

## Open questions

- **Should position consensus use ECEF or LLA?** ECEF gives
  frame-agnostic metric distance; LLA separately on H / V gives
  more interpretable vertical-vs-horizontal breakdown.  Probably
  compute both.
- **Fleet median or fleet mean?** Median is more robust to
  one-host outliers (which is exactly the case we care about).
  Start with median.
- **Cohort size** for consensus to be valid.  For 3-host
  shared-ARP cohort: need all 3 for median to be meaningful
  (with 2, median = either one, no distinguishing).  Should
  downgrade to "pairwise check" when only 2 peers available.
- **Stale peer**: if a peer's heartbeat goes quiet (MadHat's
  igc wedge), the aggregator should drop that peer from the
  cohort automatically.  Already handled in Phase 1's
  `FleetAggregator`; just needs to extend to consensus
  monitors.

## Cross-references

- `docs/peer-state-sharing.md` — Phase 1+2 design doc (Bravo).
- `docs/clock-state-modeling.md` — single-host null-mode attack
  via filter tuning.  Complementary to this doc; consensus
  monitors are runtime observability, filter tuning is
  structural.
- `docs/obs-model-completion-plan.md` — engine roll-up.
- `docs/position-strength-metric.md` — strength metric that
  could feed into consensus weighting (higher-strength hosts
  count more toward consensus).  Future refinement.
- `feedback_math_check_and_set_ceiling.md` — lessons memo that
  started the investigation arc.
