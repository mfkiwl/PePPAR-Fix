# L5I/L5Q Phase Bias — Why You Can't Substitute One for the Other

**Date**: 2026-04-18
**Context**: Can CNES's GPS L5I phase bias be used for F9T L5Q
observations with a λ/4 carrier-phase correction?
**Answer**: No — because the RTCM-published "phase bias" is not a
pure carrier-side offset.
**Supporting data**: 19 GPS SVs, lab run `day0418h` on TimeHat,
2026-04-18 21:10 CDT.

## Why the question matters

CNES (`SSRA00CNE0`) publishes GPS L5 phase biases only under signal
identifier L5I (sig_id=14).  F9T tracks the L5 pilot (Q) component
and reports L5Q (sig_id=15).  Until we can resolve this, we can't
do PPP-AR on GPS with CNES corrections alone — every L5Q phase-bias
lookup returns MISS.

A reasonable first intuition is that the two components share one
carrier (they do, with a 90° relative phase), so CNES's L5I phase
bias should be usable for L5Q with a single constant correction of
λ_L5/4 ≈ 6.4 cm.

That intuition is physically correct for the carrier itself.  It's
wrong for the **value published in the RTCM stream**, for reasons
empirically demonstrable from live data.

## What's actually in an "RTCM phase bias"

A phase bias, per IGS SSR convention, is a scalar correction in
metres applied to the carrier-phase observation before AR.  Nominally
it absorbs:

1. **Carrier-side offset** at the satellite — between I and Q
   components this is exactly λ/4 by definition.
2. **Satellite-side hardware delay** on the tracking chain — I and Q
   code paths go through different correlators/filters in the
   satellite's payload; typical delays are a few cm.
3. **AC-processing datum** — every analysis center pins its phase
   biases against its own orbit/clock + ISB convention.  Two ACs
   using different conventions publish biases whose absolute values
   differ by whatever the convention difference is.  That difference
   is absorbed into the receiver clock estimate when both biases
   come from one AC; when mixed, it shows up per-SV.
4. **Reference-receiver group delays** used during the AC's bias
   calibration — I-channel and Q-channel tracking loops have
   different group delays in real receivers.  Effects are
   per-receiver-firmware and per-signal-code.

Items 2–4 are the part the "pure carrier" intuition misses.  They
can add up to meter-scale numbers.

## Empirical test

**Setup**: TimeHat (ZED-F9T-20B) running commit `7ae1392` with both
SSR streams active:

- Primary: CNES `SSRA00CNE0` — GPS phase biases for L1C, L2W, L5I.
- Bias mount: WHU `OSBC00WHU1` — GPS phase biases for L1C, L2W, L5Q
  (and code biases for the same observables).

Both streams flow into the same `SSRState` dict, keyed by RINEX
code.  For each GPS SV we therefore have two independently-published
values: `CNES_L5I[sv]` and `WHU_L5Q[sv]`.  If the λ/4 hypothesis held,
every `L5Q − L5I` difference would sit at ±6.4 cm.

**Data** (19 GPS SVs at 21:10:55 UTC):

| SV  | CNES L5I (m) | WHU L5Q (m) | Δ = L5Q − L5I (m) |
|-----|-------------:|------------:|------------------:|
| G01 |      −0.1821 |     +0.1559 |            +0.3380 |
| G03 |      +2.0542 |     −0.0440 |            −2.0982 |
| G04 |      +1.8623 |     +0.7020 |            −1.1603 |
| G06 |      +0.3835 |     +0.8353 |            +0.4518 |
| G08 |      +0.2262 |     −0.9397 |            −1.1659 |
| G09 |      +1.3942 |     +0.0072 |            −1.3870 |
| G10 |      +2.3011 |     −0.0772 |            −2.3783 |
| G11 |      −0.0524 |     +0.6673 |            +0.7197 |
| G14 |      +1.8183 |     +0.4094 |            −1.4089 |
| G18 |      +1.7917 |     +0.0866 |            −1.7051 |
| G21 |      +0.9879 |     −0.2316 |            −1.2195 |
| G23 |      +1.3752 |     +0.2163 |            −1.1589 |
| G24 |      −0.2604 |     +0.0731 |            +0.3335 |
| G25 |      +1.4199 |     +0.1710 |            −1.2489 |
| G26 |      +1.7848 |     +0.6766 |            −1.1082 |
| G27 |      −0.9852 |     −0.4638 |            +0.5214 |
| G28 |      −4.0250 |     −0.1366 |            +3.8884 |
| G30 |      +1.5801 |     +0.2494 |            −1.3307 |
| G32 |      +1.8772 |     −0.8059 |            −2.6831 |

**Summary statistics**:

| Quantity              | Value |
|-----------------------|-------|
| n                     | 19    |
| mean(Δ)               | **−0.7263 m** |
| sd(Δ)                 | **1.4597 m**  |
| min(Δ)                | −2.6831 m |
| max(Δ)                | +3.8884 m (G28) |
| Expected if λ/4 only  | ±0.0637 m |

## Interpretation

Per-SV deltas span a 6.6 m range with 1.46 m standard deviation —
two orders of magnitude larger than λ/4.  They are **not** centered
on zero or on any simple rational fraction of the wavelength.  If
the difference were dominated by the carrier-side 90° offset, every
SV would land within a few cm of a single value.  Instead:

- The mean (−0.73 m) is consistent with an AC-datum offset between
  CNES and WHU.  A host applying CNES biases only, *or* WHU biases
  only, absorbs this datum into its receiver clock estimate.  A host
  that mixes L5I from CNES with L5Q from WHU cannot — each SV gets
  a different effective offset.
- The per-SV scatter (1.46 m) reflects I- vs Q-channel group delay
  at the ACs' reference receivers plus satellite-side I/Q processing
  differences.  These are per-SV, per-AC constants that don't cancel
  in the substitution.
- G28's +3.9 m outlier is the extreme case where the two ACs'
  calibration networks happened to disagree strongly on that SV,
  probably because one of them is tracking GO3 (G28) with a fleet
  that has a systematically different I/Q delay than the other.

Substituting CNES's L5I bias for an L5Q observation, with or
without a λ/4 correction, injects an effectively random per-SV
error at the 1.5 m level.  Applied consistently across many SVs,
this biases the PPP filter exactly like an observation noise
increase by ~1.5 m per SV — strong enough to destroy integer
ambiguity resolution and detectable as nav2Δ drift within minutes
(confirmed in commit `b71e2b1` / `15f9b01` on 2026-04-16, and again
in `day0418g` on 2026-04-18 where code biases for L5Q were also
missing from CNES).

## Consequence for system design

The physics intuition that "L5I and L5Q differ only by λ/4" is
correct at the level of the raw carrier signal.  It is not correct
at the level of the RTCM-published "phase bias", which bundles
several AC-specific and receiver-specific effects that only cancel
when all biases used by the filter come from one internally
consistent source.

Therefore:

- **Single-AC fidelity is preferred** when available.  If we find
  an AC that publishes GPS L5Q phase biases directly keyed to the
  F9T signal, use only that AC's stream.
- **Dual-mount fusion is a valid workaround** when no single AC
  provides the needed code coverage.  But the fusion must take
  *all* biases (code + phase) for a given constellation from a
  single AC, never mix code from one with phase from another for
  the same observable — see `docs/ssr-mount-survey.md` for the
  CNES + WHU pairing we actually use.
- **A single-cycle offset correction never suffices** for RTCM
  biases.  The difference between two ACs' published values is not
  reducible to a λ/N correction; it's a per-SV vector determined by
  the ACs' calibration conventions.

## Data for this analysis

The comparison script is a single awk pipeline over the TimeHat
`/tmp/day0418h-timehat.log` entries matching `Phase bias: G[0-9]+ L5`.
Raw data preserved in that log for reproduction.  Re-run with:

```sh
ssh TimeHat "grep 'Phase bias:' /tmp/day0418h-timehat.log | grep -E 'G[0-9]+ L5'"
```

## References

- `docs/ssr-mount-survey.md` — the mount-pairing plan this supports.
- Memory `project_gps_l5i_l5q_bias_fix.md` — the 2026-04-16 attempt
  that this analysis post-hoc explains.
- Geng et al. 2024 (doi:10.1007/s10291-023-01610-6) — WHU OSB
  stream validated for F9T.
