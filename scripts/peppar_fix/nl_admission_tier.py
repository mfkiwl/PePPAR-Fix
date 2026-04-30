"""Reputation-tiered NL admission gate.

Per dayplan I-172719 (2026-04-29 afternoon).  The natural follow-on
to ``WlPhaseAdmissionGate``: same wrong-integer-admission failure
mode the WL gate broke, one layer up.

## Empirical motivation

Today's lunch event (2026-04-29 11:00-12:00 CDT, post-WPAG fleet)
showed all three hosts reach ANCHORING simultaneously, hold for
~30-60 min, then fall out within an 8-min window during a
post-solar-noon TEC disturbance.  Diagnostic findings:

  - **WL held** (counts dipped but didn't collapse; no GF_STEP storm)
  - **No SSR phase-bias step** (would have triggered GF_STEP)
  - **No cycle-slip storm** (1 legitimate slip on C27, late in window)
  - **NL admit/evict ratios collapsed to ~1:1** — every admission
    ended in eviction within minutes (MadHat 27/27, clkPoC3 11/11)
  - **Altitudes drifted to 206-212m** vs NAV2's stable 197-198m
  - **ZTD jumped −2 → −1534 mm** in a few epochs on TimeHat
  - **SecondOpinionPosMonitor fired every 3-5 min** on clkPoC3

Mechanism: ZTD slowly absorbs sub-cm bias during atmospheric
flux; per-SV NL float ambiguities get pulled toward integers
consistent with the (now-biased) ZTD; LAMBDA fixes those wrong
integers (internally consistent with current filter state); ZTD
absorbs more bias, integrity trips → re-bootstrap → next admission
lands at a *different* wrong integer.  The current uniform LAMBDA
ratio + P_bootstrap test is **conditional on the filter's current
state** — when the filter is biased, the test passes for biased
integers.

## Design — per-SV trust tiers from NL admission integer-history

  TRUSTED        — int_history has ≥ k_long admissions, all at the
                   same integer (range = 0).  The SV has been a
                   long-term member or has been evicted/re-admitted
                   at the same integer repeatedly.

  PROVISIONAL    — int_history has 2 to k_long − 1 admissions, all at
                   the same integer or adjacent integers (range ≤ 1).
                   Building track record.

  NEW            — int_history empty or has only one admission, OR
                   admission-history range > 1 over the recent deque
                   (the SV has been admitted at multiple different
                   integers — an active wrong-integer cycler).

### Tier-conditional LAMBDA admission threshold

```
Tier         R bar    P bar      Notes
─────────────────────────────────────────────────────────────────
TRUSTED      ≥ 3.0    ≥ 0.95     SV has earned its place; loose gate
PROVISIONAL  ≥ 5.0    ≥ 0.99     Building reputation; standard gate
NEW          ≥ 10.0   ≥ 0.999    No reputation; strict cold-start gate
```

### TRUSTED-with-different-integer demotion (the circuit-breaker)

When LAMBDA proposes a *different* integer for a TRUSTED SV, the
gating tier for that admission is NEW — the strictest gate.  This
is the load-bearing mechanic: drift-induced wrong-integer attempts
on previously TRUSTED SVs face the strict bar that prevents them
from landing.  PROVISIONAL stays PROVISIONAL (range ≤ 1 already
tolerates one-step drift); NEW stays NEW.

### Trust decay on real cycle slip

The integer reference itself changes on a real slip; trust must
reset.  ``forget_history(sv)`` mirrors WlDriftMonitor's pattern;
caller wipes per-SV history when the upstream slip detector fires
(GF_STEP, IF_STEP, cycle-slip-flush — all three represent real
phase discontinuities).  Without this reset, an SV that genuinely
re-acquires after a slip would be falsely held to its pre-slip
integer expectation.

## Asymmetry vs WlDriftMonitor.consistency_level (intentional)

  - WL CONS_HIGH requires range = 0 over k_short=4
  - NL TRUSTED requires range = 0 over k_long=4

These match.  But:

  - WL CONS_MEDIUM allows range = 1
  - NL PROVISIONAL also allows range ≤ 1, but covers the WHOLE
    sub-TRUSTED middle (admissions count from 2 up to k_long − 1)

NL PROVISIONAL is a wider class than WL MEDIUM because NL admission
is slower and less stable than WL re-fix; allowing range ≤ 1 with
a count under k_long gives the SV runway to drift through an
adjacent integer without immediate demotion to NEW.  TRUSTED still
demands range = 0 — the strictest internal consistency.

## Behaviour during sustained disturbance

During an active TEC event with no TRUSTED scaffold (cold start
during stress, or a fresh restart mid-event), the strict NEW
gate (10.0/0.999) plus PROVISIONAL gate (5.0/0.99) will reject
most candidates.  The filter may stall instead of admitting wrong
integers — that is the right behaviour, not a defect to chase.
K_long = 4 SVs accumulate trust in ~30 min of stable sky, so
post-event recovery is brisk once the disturbance passes.

## ANCHORED-as-trust-shortcut (I-004810-main)

Reaching ``SvAmbState.ANCHORED`` is stronger evidence than four
matching admits — the SV cleared the Δaz=15° geometry validation,
which catches many wrong-integer cycles that pure-history matching
would not.  ``note_anchored(sv)`` records this earned reputation
as a per-SV ``past_anchored`` flag.

Effect on classification: when ``tier_for`` would otherwise return
``NEW`` (empty / single / wide-range history), past-anchored SVs
return ``PROVISIONAL`` instead.  This is a one-step boost at the
bottom of the ladder — it gives previously well-behaved SVs a
faster path back through the gate after an integrity-trip
re-bootstrap, without skipping the geometry-validated TRUSTED
threshold above.

The boost is naturally bounded: once the SV accumulates two
same-integer admits, ``tier_for`` returns ``PROVISIONAL`` on its
own and the flag is irrelevant.  The boost is also defeated for
wide-range histories at the ``tier_for_proposed`` level — if the
proposed integer would push the (history ∪ {proposed}) range past
1, the gate falls back to ``NEW`` regardless of the flag.

Reset semantics: ``forget_history(sv)`` (called on confirmed real
cycle slips) clears ``past_anchored`` along with the integer
history.  This is the load-bearing protection — the spec says
"real-slip eviction with different integer should not skip the
trust ladder", and slip → ``forget_history`` → flag cleared
enforces it.  Integrity trips that don't fire the slip detector
do NOT clear the flag — the SV is still considered trustworthy
in the absence of phase-discontinuity evidence.

## Usage pattern

    nl_tier = NlAdmissionTier(k_long=4)

    # Per LAMBDA-fix attempt, after lambda_resolve returns ratio + P:
    for sv, n_nl_proposed in proposed.items():
        gate_tier = nl_tier.tier_for_proposed(sv, n_nl_proposed)
        if (ratio < nl_tier.ratio_threshold(gate_tier)
                or p_bootstrap < nl_tier.pbootstrap_threshold(gate_tier)):
            # Reject this candidate — joint LAMBDA passed but doesn't
            # clear this SV's tier-conditional bar.
            continue
        # Admit
        nl_tier.note_admit(sv, n_nl_proposed)

    # On real cycle slip detected upstream:
    nl_tier.forget_history(sv)

Not thread-safe.  Call from the AntPosEst thread only (matches the
other monitors' threading model).
"""

from __future__ import annotations

import logging
from collections import deque

log = logging.getLogger(__name__)


# Per-SV NL admission trust tiers — string constants (not Enum) so
# log lines are stable and the values are self-describing in greps.
TIER_TRUSTED = "TRUSTED"
TIER_PROVISIONAL = "PROVISIONAL"
TIER_NEW = "NEW"


# Tier-conditional admission thresholds.  Numbers from
# ``docs/wl-admission-phase-only-future.md`` §"Tier-conditional
# admission threshold"; calibration TBD against overnight data.
_RATIO_BAR = {
    TIER_TRUSTED:     3.0,
    TIER_PROVISIONAL: 5.0,
    TIER_NEW:         10.0,
}
_P_BOOTSTRAP_BAR = {
    TIER_TRUSTED:     0.95,
    TIER_PROVISIONAL: 0.99,
    TIER_NEW:         0.999,
}


class NlAdmissionTier:
    """Per-SV NL trust tier classifier with tier-conditional gating."""

    def __init__(self, k_long: int = 4) -> None:
        # Number of recent NL admissions to remember per SV.  At NL
        # admission cadence (slower than WL re-fix), 4 entries cover
        # ~30+ min of stable membership — earned but achievable.
        self._k_long = int(k_long)
        # sv → deque of last k_long n_nl values committed for this SV.
        # Persists across NL un-fix/re-fix cycles so admission-cycling
        # patterns are visible.  Reset only by ``forget_history`` (real
        # cycle slip detected upstream).
        self._int_history: dict[str, deque[int]] = {}
        # sv → has the SV ever reached SvAmbState.ANCHORED?  Set by
        # ``note_anchored``, cleared by ``forget_history`` (real slip).
        # Drives the ANCHORED-shortcut: NEW-classified past-anchored
        # SVs are upgraded to PROVISIONAL by ``tier_for``.  See
        # I-004810-main and the module docstring "ANCHORED-as-trust-
        # shortcut" section.
        self._past_anchored: dict[str, bool] = {}

    # ── Lifecycle ──────────────────────────────────────────────── #

    def note_admit(self, sv: str, n_nl: int) -> None:
        """Record a successful NL admission for ``sv`` at integer
        ``n_nl``.  Call **after** the admission decision has been
        made and the integer is committed."""
        hist = self._int_history.setdefault(
            sv, deque(maxlen=self._k_long)
        )
        hist.append(int(n_nl))

    def forget_history(self, sv: str) -> None:
        """Wipe the per-SV integer history AND the past-anchored
        flag.  Call **only** after a confirmed real cycle slip
        (GF_STEP, IF_STEP, cycle-slip-flush — all represent phase
        discontinuities that invalidate the ambiguity reference).
        Routine NL evictions should NOT call this — the whole point
        is that trust persists across re-admit cycles.

        Clearing ``past_anchored`` here is the load-bearing
        protection that the I-004810-main shortcut design depends on:
        a real slip means the integer reference itself moved, and the
        SV must climb the trust ladder again from ``NEW``."""
        self._int_history.pop(sv, None)
        self._past_anchored.pop(sv, None)

    def note_anchored(self, sv: str) -> None:
        """Record that ``sv`` has reached ``SvAmbState.ANCHORED`` — i.e.
        cleared the AnchoringSvPromoter's Δaz=15° geometry validation.
        Idempotent: calling repeatedly leaves the flag True.

        Called by the engine on the ``ANCHORING → ANCHORED`` SvAmbState
        transition.  Cleared by ``forget_history`` (real slip)."""
        self._past_anchored[sv] = True

    # ── Tier classification ────────────────────────────────────── #

    def tier_for(self, sv: str) -> str:
        """The tier of ``sv``'s admission history alone, ignoring any
        proposed integer.  Used for visibility (logs, classifier
        labels).

        ANCHORED-shortcut: when the history-only base would be ``NEW``
        but the SV has previously reached ``ANCHORED`` (via
        ``note_anchored``), return ``PROVISIONAL`` instead.  See
        I-004810-main."""
        hist = self._int_history.get(sv)
        if hist is None or len(hist) < 2:
            base = TIER_NEW
        else:
            rng = max(hist) - min(hist)
            if len(hist) >= self._k_long and rng == 0:
                return TIER_TRUSTED
            if rng <= 1:
                return TIER_PROVISIONAL
            base = TIER_NEW
        if base == TIER_NEW and self._past_anchored.get(sv, False):
            return TIER_PROVISIONAL
        return base

    def tier_for_proposed(self, sv: str, n_nl_proposed: int) -> str:
        """The tier that GOVERNS admission of ``sv`` at integer
        ``n_nl_proposed``.  Differs from ``tier_for`` only when the
        SV is currently TRUSTED but the proposed integer doesn't
        match its history — the load-bearing circuit-breaker.

        Mapping:
          TRUSTED + same integer    → TRUSTED   (loose gate; earned)
          TRUSTED + different       → NEW       (strict gate; suspect)
          PROVISIONAL + range stays → PROVISIONAL (already tolerates
                                                   range ≤ 1)
          PROVISIONAL + new range>1 → NEW       (would push out of
                                                  PROVISIONAL anyway)
          NEW                       → NEW
        """
        base = self.tier_for(sv)
        hist = self._int_history.get(sv)
        if base == TIER_TRUSTED and hist is not None:
            # TRUSTED requires range = 0 — every entry is the same
            # integer.  A proposed integer that doesn't match demotes
            # to NEW for this admission.
            if n_nl_proposed != hist[0]:
                return TIER_NEW
            return TIER_TRUSTED
        if base == TIER_PROVISIONAL and hist is not None:
            # Would the proposed integer push the (sliding-window)
            # range past 1?  Note: tier_for_proposed is a hypothetical
            # check — we don't actually mutate hist here.  The next
            # note_admit may evict the oldest entry, but we evaluate
            # against the current window plus the proposed value.
            cur_min = min(hist)
            cur_max = max(hist)
            new_min = min(cur_min, int(n_nl_proposed))
            new_max = max(cur_max, int(n_nl_proposed))
            # If the deque is at capacity, the oldest entry evicts on
            # append — be optimistic about the post-append range:
            # check if dropping the oldest entry could keep the range
            # ≤ 1.  Conservative implementation: compute against the
            # current window.  The deque length is small (k_long = 4),
            # so the conservative answer is good enough.
            if (new_max - new_min) <= 1:
                return TIER_PROVISIONAL
            return TIER_NEW
        return TIER_NEW

    # ── Tier → threshold lookups ───────────────────────────────── #

    @staticmethod
    def ratio_threshold(tier: str) -> float:
        """LAMBDA ratio bar for ``tier``."""
        return _RATIO_BAR[tier]

    @staticmethod
    def pbootstrap_threshold(tier: str) -> float:
        """LAMBDA P_bootstrap bar for ``tier``."""
        return _P_BOOTSTRAP_BAR[tier]

    def admits_at(self, sv: str, n_nl_proposed: int,
                  ratio: float, p_bootstrap: float) -> tuple[bool, str]:
        """Composite admission decision.  Returns ``(admit, tier)``
        where ``tier`` is the gating tier (post-TRUSTED-mismatch
        demotion) and ``admit`` is True iff the joint LAMBDA stats
        clear the tier's bars.

        Used by NarrowLaneResolver._attempt_lambda's per-SV
        post-LAMBDA filter."""
        tier = self.tier_for_proposed(sv, n_nl_proposed)
        ok = (ratio >= _RATIO_BAR[tier]
              and p_bootstrap >= _P_BOOTSTRAP_BAR[tier])
        return ok, tier

    # ── Diagnostics ────────────────────────────────────────────── #

    def integer_history(self, sv: str) -> list[int]:
        """Read-only snapshot of the last k_long n_nl values
        committed for ``sv``, oldest first.  Empty list if no
        history."""
        hist = self._int_history.get(sv)
        return list(hist) if hist else []

    def n_tracking(self) -> int:
        """Number of SVs with at least one recorded admission."""
        return len(self._int_history)

    def summary(self) -> str:
        return (
            f"nl_tier: tracking {len(self._int_history)} SVs "
            f"(k_long={self._k_long})"
        )
