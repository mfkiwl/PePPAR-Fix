# PPP Carrier Phase Servo Drive

## Overview

The Carrier Phase servo drive uses PPP carrier-phase observations
(dt_rx) for short-term frequency precision (~0.1 ns) while maintaining
phase alignment to GPS via PPS edge measurements.

## The two-oscillator problem

On most hardware, the PPP receiver (F9T) and the PHC use different
crystals.  dt_rx measures the F9T TCXO; the PHC runs on a separate
oscillator:

| Platform | F9T osc | PHC osc | Differential |
|---|---|---|---|
| TimeHat | F9T TCXO | i226 TCXO | ~85 ppb |
| otcBob1 | F9T TCXO | OCXO via ClockMatrix | ~77,000 ppb |

Without correction, the Carrier accumulator drifts at the differential
rate, causing the PHC to walk away from the GPS second boundary.

## Solution: measure D at bootstrap

Both oscillator drift rates are directly measurable against GPS time:

- **R_tcxo** = d(dt_rx)/dt — from the PPP filter's clock estimates
  during bootstrap.  Precision ~0.1 ppb over 10 epochs.
- **R_phc** = d(pps_error)/dt − adjfine — from PPS timestamps during
  bootstrap, removing the known adjfine contribution.  Precision
  ~3.3 ppb per epoch, ~1 ppb over 10 epochs.

The inter-oscillator differential:

    D = R_phc − R_tcxo  (ppb)

D is measured during bootstrap and passed to the engine.  The Carrier
formula becomes:

    carrier_error = (dt_rx − dt_rx_ref) + cumulative_adjfine + D × epochs

No filter lag.  No steady-state phase offset.  The initial bias from
D uncertainty is bounded by σ_D ≈ 3.3/sqrt(N_bootstrap) ppb — about
1 ppb after 10 bootstrap epochs = 1 ns of phase error per epoch.

## Runtime refinement

The engine continuously refines D from the same two measurements:

    r_tcxo = dt_rx[k] − dt_rx[k−1]
    r_phc  = (pps_error[k] − pps_error[k−1]) − adjfine[k]
    D_sample = r_phc − r_tcxo

The running mean of D_samples improves with sqrt(N).  After 1000
epochs (~17 min), σ_D ≈ 0.1 ppb.  If D changes due to temperature,
a sliding window can track it (future work).

## Servo drive taxonomy

| Servo Drive | Observation Source | Precision (1s) | Edge Timestamp Required |
|---|---|---|---|
| PPS Phase | PPS edge via EXTTS/TICC | ~2.3 ns | Yes |
| PPS+qErr Phase | PPS edge + firmware sawtooth | ~0.2 ns | Yes |
| PPP Carrier Phase | PPP dt_rx + D correction | ~0.1 ns | Bootstrap only |
| PPP-AR Carrier Phase | Ambiguity-resolved dt_rx | ~0.01 ns | Bootstrap only |

## Bootstrap handoff

1. Bootstrap measures `pps_freq_ppb` (PHC drift from PPS intervals)
2. Bootstrap runs PPP filter, collects dt_rx at each epoch
3. Compute `R_tcxo` from dt_rx slope, `R_phc` from pps_freq_ppb − adjfine
4. `D = R_phc − R_tcxo`
5. Save D in drift file alongside base_freq
6. Engine initializes CarrierPhaseTracker with D from drift file
7. Carrier source is accurate from epoch 1, no convergence delay

## Phase error bounds

With D known to σ_D precision:
- Phase error per epoch: σ_D ns (not accumulated — D re-estimated each epoch)
- Phase error improves with runtime as D refines

With D unknown (cold start, no bootstrap estimate):
- Falls back to runtime estimation, ~3.3 ppb initially
- PPS/PPS+qErr sources win the competition until D converges

## Failure modes

- **D changes with temperature**: sliding window estimator (future work)
- **adjfine nonlinearity**: cumulative_adjfine drifts from truth;
  detectable by comparing carrier_error to pps_error
- **Satellite loss**: dt_rx_sigma increases, Carrier loses competition
  to PPS-based sources — automatic fallback
