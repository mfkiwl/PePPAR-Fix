# Future work — phase-only WL admission criteria

## Premise

Today's WL ambiguity admission uses the **Melbourne-Wübbena** combination:

```
MW = (φ_L1 − φ_L5) − (f_L1 ρ_L1 + f_L5 ρ_L5) / (f_L1 + f_L5)
```

MW gives a direct integer estimate from the float `MW / λ_WL`, with the
PR term providing the absolute scale that makes the integer resolvable.
But it imports **all PR-domain noise** (multipath, code-tracking jitter,
code biases) into the admission decision: bad PR can pull the float WL
ambiguity to within 0.15 cyc of a *wrong* integer and the gate accepts it.

This is the admission counterpart to the eviction problem documented in
`docs/wl-drift-redesign-proposal.md` (Charlie, commit `e2ab81a` on the
`charlie` branch).  That work replaces the eviction-side MW-residual
signal with a phase-only **GF** combination so PR noise can't trip
demotions.

The eviction redesign **will catch** wrong admissions and evict them, so
PR-contaminated admissions become visible as repeated eviction-readmit
cycles on the same SV with wandering integers.  Going further — making
the **admission** decision itself phase-only — is a possible defense IF
empirical data after the eviction redesign shows that wrong-admission
cycling is a meaningful operational drag.

## Trigger condition

Pull this work onto the active queue **only if** one of:

1. After GF-eviction lands and runs ≥ 2 nights, the per-SV `[WL_FIX_LIFE]`
   data shows that wrong-admission cycling (LOW-consistency SVs admitted
   then evicted then re-admitted at different integers) accounts for a
   meaningful fraction of the convergence-impeding workload.
2. We see specific failure modes downstream (NL resolution sabotaged by
   wrong-WL contamination, ZTD/altitude excursions from wrong-integer
   absorption) traceable to admissions that should never have been made.

If the GF-eviction monitor cleans up the cycling, this work stays parked.

## Possible defenses (sketch — not committed designs)

Carrier-only WL ambiguity resolution loses MW's direct PR-driven scale, so
a different mathematical anchor is needed.  Three candidates:

1. **Kalman filter on per-SV WL ambiguity float, LAMBDA integer search.**
   Use carrier-phase observations only; PR can supply weak priors but
   doesn't drive the integer decision.  LAMBDA decorrelation + ratio test
   gates admission.  Slower convergence (carrier alone resolves WL over
   ~minutes vs MW's seconds), but immune to PR noise.

2. **GF + external ionosphere prior.**  GF = φ_L1 − φ_L5 contains iono +
   WL ambiguity.  With a smoothed ionosphere state (estimated separately
   or from an external IGS-like prior), GF-only resolves the WL integer
   without PR.  Requires reliable iono modeling on the F9T-class
   receiver with our SSR feed.

3. **Integer LAMBDA with PR-as-priors-only.**  Use PR observations only
   to provide weak Gaussian priors on the integer search space; the
   actual integer commitment comes from carrier-phase consistency.  PR
   noise widens the search box but doesn't fix the integer.

Option (1) is the textbook PPP-AR approach.  Option (2) needs ionosphere
state we don't currently estimate.  Option (3) is a middle path.

## Empirical motivation when revisited

The 2026-04-28 morning `WL_RESID` capture on MadHat showed Pop B SVs
(G20 [-41,-4,18], E03 [-18,-10,6], G06 [-91,-90,-10]) admitting wildly
different integers across cycles — clearly admission-side wrong-integer
acceptance, not just eviction-side noise.  Charlie's GF-eviction redesign
catches them after the fact.  The question this work answers is "could
we have avoided the bad admission in the first place?"

Cross-references:
- `docs/wl-drift-redesign-proposal.md` (eviction-side, charlie branch)
- `docs/misnomers.md` (the wl_drift naming-honesty entry)
- `project_wl_drift_smooth_float_signal_20260428` (BNC AMB probe finding)
