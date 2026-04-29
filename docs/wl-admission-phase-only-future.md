# Admission redesign — phase-only WL (done) + reputation-tiered NL (proposed)

> Filename retained for git history.  Scope expanded 2026-04-29 from
> WL-only to cover both layers (WL and NL admission), since the same
> wrong-integer-admission failure mode repeats at each.

## Premise — admission-side wrong-integer cycling

Both WL and NL admission can commit to wrong integers, then later get
evicted, then re-admitted at a different (also wrong) integer.  The
cycle prevents the filter from settling at the right answer because
each new admission corrupts state (ZTD, altitude) that the next
admission then commits relative to.

The two layers have different math, different signals, and need
different fixes — but the architectural pattern is the same:
admission-side wrong-integer cycling treated by a tighter gate.

## WL admission — DONE (2026-04-29 morning)

Old behaviour: WL ambiguity admission via the **Melbourne-Wübbena**
combination,

```
MW = (φ_L1 − φ_L5) − (f_L1 ρ_L1 + f_L5 ρ_L5) / (f_L1 + f_L5)
```

PR was a load-bearing input to the integer decision; PR noise
(multipath, code-tracking jitter, code biases) imported to admission.

Replacement: **`WlPhaseAdmissionGate` (`scripts/peppar_fix/wl_phase_admission_gate.py`,
Charlie's `228affb` + `de03e51`).**  Pre-WL-fix consistency check on the
post-fit phase residual; rejects MW-proposed admissions whose phase
residual mean exceeds threshold.  Threshold loosened from 0.05m to
0.15m mean / 0.05m std on `de03e51` after empirical false-positive
data on the morning lunch run.

Empirical proof it works (today's lunch event, 11:30-12:00 CDT
during post-solar-noon TEC stress): WL fix counts dipped during the
event peak (e.g. MadHat 19/20 → 14/22) but never collapsed; only 2
GF_STEP fires across all three hosts in the entire 30-minute window,
both catching the same legitimate cycle slip on a low-elevation BDS
SV (C27 elev 11.4° at 11:57:49, agreed across MadHat and clkPoC3).
WL was the layer that held while NL drifted.

## NL admission — PROPOSED (load-bearing for next sustained-anchor work)

### Empirical motivation (today)

The lunch run (2026-04-29 11:00-12:00 CDT, main `65c24f6`) showed all
three hosts reach ANCHORING simultaneously and hold for ~30-60 min,
then fall out within an 8-minute window (11:44-11:52) during a
post-solar-noon TEC disturbance.  Diagnostic findings:

- **WL held** (counts dipped but didn't collapse; no GF_STEP storm)
- **No SSR phase-bias step** (would have triggered GF_STEP)
- **No cycle-slip storm** (1 legitimate slip on C27, late in window)
- **NL admit/evict ratios collapsed to ~1:1** (MadHat 27/27, clkPoC3
  10/5 → 11/11) — every admission ended in eviction
- **Altitudes drifted to 206-212m** vs NAV2's stable 197-198m
- **ZTD jumped** −2 → −862 → −1534 mm in a few epochs on TimeHat;
  similar pattern on MadHat / clkPoC3
- **SecondOpinionPosMonitor fired repeatedly** (clkPoC3 every 3-5 min
  for the full window); each reset returned to NAV2 LLA but the
  filter walked back into the wrong basin within the next 30s

The mechanism: ZTD slowly absorbs sub-cm bias during atmospheric
flux; per-SV NL float ambiguities get pulled toward integers
consistent with the (now-biased) ZTD; LAMBDA fixes those wrong
integers (they're internally consistent with current filter state);
ZTD absorbs more bias, integrity trips → re-bootstrap → next
admission lands at a *different* wrong integer.

The current NL admission gate is a LAMBDA ratio test that's checked
**conditional on the filter's current state.**  When the filter is
biased, the test still passes for biased integers.  We need an
**external** check — one whose information source isn't the filter
itself.

### Proposed NL admission redesign — reputation-tiered gating

Per-SV trust score, accumulated from NL admission integer-history.
Already instrumented as part of today's morning work (`[NL_ADMIT]`
log line carries `int_history=[N1, N2, ...]` per SV; populated and
shipped on `04f366e`).  The current admission code doesn't yet
**read** this history when deciding whether to admit; this proposal
wires it in.

#### Trust tiers

  TRUSTED        — `int_history` has ≥ K_long admissions, all at
                   the same integer (range = 0 over the deque).
                   The SV has been a long-term member *or* has been
                   evicted-then-re-admitted at the same integer
                   repeatedly.  Both signals indicate the SV is
                   telling us the same thing consistently.

  PROVISIONAL    — `int_history` has 2 to K_long − 1 admissions, all
                   at the same integer or adjacent integers
                   (range ≤ 1).  Building track record but not yet
                   load-bearing trust.
                   **Note**: PROVISIONAL allows `range ≤ 1`, while
                   WL's `WlDriftMonitor.CONS_HIGH` requires `range =
                   0`.  Asymmetry is intentional — NL admission cadence
                   is slower than WL re-fix and the LAMBDA decorrelation
                   space is larger, so adjacent-integer wobble during
                   trust accumulation is the expected normal.  The
                   NL TRUSTED bar (range = 0 over K_long) is therefore
                   stricter than WL HIGH despite the looser PROVISIONAL.

  NEW            — `int_history` empty or only one admission, OR
                   admission-history range > 1 over the recent
                   deque (the SV has been admitted at multiple
                   different integers — an active wrong-integer
                   cycler, no trust earned).

#### Tier-conditional admission threshold

Different tiers admit at different LAMBDA ratio / P_bootstrap bars:

```
Tier         R bar    P bar      Notes
─────────────────────────────────────────────────────────────────
TRUSTED      ≥ 3.0    ≥ 0.95     SV has earned its place; loose gate
PROVISIONAL  ≥ 5.0    ≥ 0.99     Building reputation; standard gate
NEW          ≥ 10.0   ≥ 0.999    No reputation; strict cold-start gate
```

Numbers above are **starting points**, calibrated tomorrow against
overnight admission cycling data.  The current uniform gate is
~5.0/0.95 — equivalent to treating every SV as PROVISIONAL.

#### Trust decay

Trust must reset on real cycle slip (the integer reference itself
changes).  Mirrors `WlDriftMonitor.forget_history(sv)` — caller
wipes per-SV history when the upstream slip detector fires.  Without
this reset, an SV that genuinely re-acquires after a slip would be
falsely held to its pre-slip integer expectation.

#### Why this works during the lunch-event scenario

When the TEC disturbance hits and the filter starts drifting:

- Existing TRUSTED SVs continue to admit at the relaxed bar (R ≥ 3),
  but their `int_history` is at a stable integer — admission of the
  *same* integer is what trust unlocks.  An attempt to admit a
  *different* integer for a TRUSTED SV demotes it to NEW for that
  cycle, requiring R ≥ 10.  Drift-induced wrong-integer admissions
  on previously TRUSTED SVs face the strictest gate.
- New SVs trying to enter face the strict gate.  During an active
  disturbance, ratio ≥ 10 is unlikely — keeps the wrong integers out.
- Integrity trip recovery: after re-bootstrap, all SVs retain their
  `int_history`.  TRUSTED SVs can rapidly re-admit at their known
  integer; only fresh / cycling SVs face the cold-start bar.

This gives the filter a stable scaffold to recover onto, instead of
re-admitting fresh wrong integers and re-entering the trap.

## Defenses considered and rejected

### Cohort-consensus admission (rejected as primary)

Cross-host agreement on per-SV NL integer would be a strong external
signal.  Rejected as **primary** defense for two reasons:

1. **Doesn't generalize.**  Many real installations are single-host;
   they need an admission gate that works without any peer cohort.
2. **Bad cohort is a small step from bad fix set.**  When the fleet
   is in correlated failure (today's lunch event was exactly this —
   sky-side disturbance hit all three hosts simultaneously), the
   cohort consensus *is* the wrong answer.  Treating it as truth
   would amplify the failure rather than catch it.

May still be useful as a *side check* (it's free information when
peer-bus is up), but never load-bearing.

### Freezing NL admission during SO_POS active streak (rejected)

Refuse new NL admissions while `SecondOpinionPosMonitor` shows
nav2Δ above its trip threshold.  Rejected: by the time SO_POS is
firing, the drift has already happened.  Reactive, not preventive.
The reputation-tiered gate prevents the wrong admission *before*
SO_POS would have noticed the divergence.

### ZTD-residual quality gate (kept as side check)

Refuse new admissions when |ZTD residual| exceeds threshold.  Useful
as a defensive backstop (when the filter is in a known-biased state,
don't add to the bias), but isn't sufficient alone — admissions
during the early bias-accumulation window (before ZTD threshold)
are exactly what creates the bias.

### Cold-start during sustained disturbance (expected behavior)

A fresh restart in the middle of an active disturbance event — TEC
storm, sustained ZTD wander, post-power cold start during weather —
will land most candidates at NEW tier with no prior `int_history`.
The strict NEW gate (R ≥ 10, P ≥ 0.999) plus PROVISIONAL gate
(R ≥ 5, P ≥ 0.99) will reject most LAMBDA proposals during the
disturbance.

**This is the right behavior, not a defect.**  Better to stall the
filter on float-only WL + pseudorange than to anchor at wrong NL
integers and re-enter the trap.  Recovery time after the disturbance
clears: K_long = 4 same-integer admissions per SV is achievable in
~30 min of stable sky, so post-event the trust scaffold rebuilds
briskly.  Today's empirical question (answerable from tonight's
overnight if a TEC event happens): how long until first ANCHORING
after a disturbance ends?  Worth measuring; not worth tuning around.

### Trust decay sources

`forget_history(sv)` must be called on every event that invalidates
the ambiguity reference.  Three call sites:

1. **GF_STEP** — phase-domain WL cycle slip detected.  WL integer
   reference broken; all NL ambiguities downstream are also
   suspect.
2. **IF_STEP** — phase-domain NL post-fix integrity trip.  NL
   integer reference broken directly.
3. **cycle-slip-flush** (existing engine path).  LLI / GF /
   MW-jump / arc-gap / locktime-drop slip detector — same logical
   class as GF_STEP, different signal source.

Without trust decay on these three, an SV that genuinely re-acquires
after a slip would be falsely held to its pre-slip integer
expectation; admissions would face the lenient TRUSTED bar against
a possibly-different post-slip integer.  Direct mirror of WL's
`WlDriftMonitor.forget_history()` pattern.

## Open questions

- **K_long** for the TRUSTED-tier deque length.  Mirrors WL's
  `k_short=4`; for NL, suggest 4 to start (admission rate is much
  slower than WL, so 4 entries cover ~30+ min of stable membership).
- **Trust gate on ANCHORED state** vs `int_history`-only.  An SV
  that earned ANCHORED but was evicted has stronger evidence than
  one that's only PROVISIONAL by integer count.  Could short-circuit
  the trust tier upward on past-ANCHORED status.
- **Hysteresis between tiers.**  When `int_history` grows past the
  TRUSTED threshold mid-evaluation, do we promote immediately or
  wait until the next admission cycle?  Suggest immediate — no
  reason to delay a decision the data already supports.

## Cross-references

- `scripts/peppar_fix/wl_phase_admission_gate.py` — WL admission
  gate (today's `WlPhaseAdmissionGate`)
- `[NL_ADMIT]` engine emits `int_history` per SV — already shipped
  in `ppp_ar.py:_apply_lambda_fix` and `_apply_rounding_fix`
- `[NL_EVICT]` symmetric on the eviction side — already shipped
- `WlDriftMonitor` (now WL integer-consistency tracker) for the
  pattern of trust accumulation feeding eviction-side monitors
- `docs/wl-drift-redesign-proposal.md` — eviction-side parallel
- 2026-04-29 lunch event analysis (`day0429lunch.log` on each host,
  searches: `SECOND_OPINION_POS`, `FIX_SET_INTEGRITY.*TRIPPED`,
  `[NL_ADMIT]`, `[NL_EVICT]`)
