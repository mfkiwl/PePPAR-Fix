# Position Confidence Framework

**Date**: 2026-04-16
**Status**: Implementing
**Prerequisite**: NAV2-PVT wired into serial reader (done), LAMBDA AR (done)

## Problem

AntPosEst's PPP-AR position drifted hundreds of meters overnight
while sigma stayed at 0.03m.  The NL constraints kept sigma small
but didn't prevent the underlying position from walking.  No sanity
check caught this because AntPosEst trusted its own filter exclusively.

## Design: position opinions and confidence competition

Every position estimate is an **opinion** with a confidence envelope.
No single source is unconditionally trusted.  Instead, opinions
compete: agreement strengthens confidence, disagreement triggers
investigation.

### Opinion sources (current and future)

| Source | Resolution | Update rate | Spoofing risk | Notes |
|---|---|---|---|---|
| **NAV2** | ~2-5m (hAcc) | 0.2 Hz | Yes (RF) | F9T secondary engine, independent of peppar-fix |
| **AntPosEst** | ~0.03m (sigma) | 1 Hz | Low (SSR) | PPP-AR with carrier phase, needs corrections |
| **Peer** | varies | ~0.1 Hz | Low (cross-check) | Future: via NTRIP, same ARP or nearby |

### Confidence metrics per opinion

Each opinion carries:

- **Position** (ECEF or LLH)
- **Horizontal accuracy** (1-sigma meters)
- **Vertical accuracy** (1-sigma meters)
- **pDOP** (geometric dilution — low = good geometry)
- **Fix quality** (NAV2: fixType + numSV; AntPosEst: n_nl + ratio)
- **Age** (seconds since last update)
- **Source trust** (configurable weight reflecting spoofing risk,
  correction quality, etc.)

### Confidence competition

The framework doesn't pick a winner — it measures **agreement**.

```
displacement = ||opinion_A.pos - opinion_B.pos||
combined_unc = sqrt(opinion_A.h_acc^2 + opinion_B.h_acc^2)
tension = displacement / combined_unc    # in sigma units
```

- `tension < 2`: agreement — both opinions are consistent
- `tension 2-5`: mild disagreement — flag for logging
- `tension > 5`: strong disagreement — one opinion is wrong

When AntPosEst and NAV2 disagree beyond threshold:
1. Log the disagreement with both positions and accuracies
2. Unfix all NL ambiguities (the integers may be wrong)
3. Reset the PPPFilter to the NAV2 position (or a weighted blend)
4. Let the filter re-converge from float
5. LAMBDA will re-resolve with fresh integers

This is the right response because:
- If AntPosEst drifted (overnight bug): NAV2 is correct, reset fixes it
- If NAV2 is spoofed: AntPosEst was correct, but re-convergence from
  NAV2's position will quickly reveal the inconsistency (carrier phase
  residuals will be huge if the position is wrong by meters)
- If both are wrong: the disagreement itself is the valuable signal

### Future: peer opinions via NTRIP

Two peppar-fix instances sharing an ARP (same antenna via splitter)
can exchange position opinions over NTRIP.  The wire format would
encode the opinion fields above in a custom RTCM message or a
well-known type (Type 1005/1006 for position + extensions).

Peers within 10 km but on different antennas can still cross-check:
their positions should agree to within the ARP separation (known from
survey or from the positions themselves once both converge).  This
gives:

- **Spoofing detection**: a spoofer would need to fool both receivers
  consistently, accounting for their spatial separation
- **Faster fix validation**: if peer A resolves integers and peer B's
  NAV2 agrees with A's AR position, B can use A's position as a
  constraint to speed its own convergence
- **Redundancy**: if one host's PPP filter diverges, the peer provides
  an independent check

The confidence competition framework handles peers naturally — they're
just another opinion source with their own accuracy and trust level.

### NAV2 accuracy notes

NAV2-PVT provides:
- `hAcc`: horizontal accuracy estimate (mm, 1-sigma)
- `vAcc`: vertical accuracy estimate (mm, 1-sigma)
- `pDOP`: position dilution of precision (dimensionless)
- `fixType`: 0=none, 2=2D, 3=3D
- `numSV`: satellites used
- `flags.gnssFixOk`: fix valid flag
- `flags.diffSoln`: differential corrections applied

hAcc and vAcc are the receiver's own estimate of its position
uncertainty, incorporating satellite geometry, signal quality, and
measurement noise.  They're more informative than DOP alone because
they account for actual signal conditions, not just geometry.

Typical values for F9T NAV2 with a good antenna:
- Open sky: hAcc ~1.5m, vAcc ~2.5m, pDOP ~1.5
- Moderate multipath: hAcc ~3-5m, vAcc ~5-8m, pDOP ~2-3

## Implementation

### Phase 1: NAV2 sanity check in AntPosEst (this session)

1. Pass `nav2_store` to `AntPosEstThread`
2. Every 10 epochs (~10s), compare AntPosEst position vs NAV2
3. Compute tension (displacement / combined uncertainty)
4. If tension > 5 for 3 consecutive checks:
   - Unfix all NL ambiguities
   - Reset PPPFilter position to NAV2 position
   - Log: "Position reset: AntPosEst disagreed with NAV2 by Xm"
   - Transition state: RESOLVED → CONVERGING

### Phase 2: Enhanced Nav2PositionStore

Capture all confidence-relevant fields from NAV2-PVT:
- vAcc, pDOP, numSV, fixType, gnssFixOk
- Store as a structured opinion for the confidence framework

### Phase 3: Position opinion abstraction (future)

- `PositionOpinion` dataclass with all confidence fields
- `ConfidenceComparator` that computes tension between any two opinions
- Peer opinion ingestion via NTRIP

## Relation to overnight drift

The overnight drift happened because:
1. Position process noise allowed the position to walk
2. New satellites got NL fixes computed from the drifted position
3. These fixes reinforced the wrong position (circular dependency)
4. Sigma stayed at 0.03m because NL re-constraints kept it tight

NAV2 would have caught this within minutes.  At 0.2 Hz, a 50m drift
(reached within ~1 hour overnight) produces tension > 10 against a
2.5m hAcc NAV2 fix.  The sanity check would have reset the filter
long before the position walked hundreds of meters.
