"""Per-epoch cohort-median post-fix IF residual monitor — NL-layer demoter.

Same architectural pattern as ``GfStepMonitor`` (the WL-layer
demoter shipped earlier on 2026-04-28), applied at the NL layer.
Replaces the PR-residual-based eviction action of
``FalseFixMonitor`` per dayplan I-221332-main.

Empirical motivation: ``FalseFixMonitor`` evicted 16 NL fixes on
MadHat in 95 minutes via PR-residual rolling-mean (base 2 m at
zenith, 1/sin(elev) below 45°).  Same architectural failure mode
``WlDriftMonitor`` had at the WL layer — PR-domain signal driving
the eviction.  PR multipath, code-bias drift, and receiver
front-end PR shifts dominate the residual; phase-side wrong-fix
events get buried in the PR noise.

This monitor uses post-fit **phase** residuals (the IF combination
output of the filter) — phase-only, free of PR contamination.
Cohort-median across the currently-NL-fixed-SV cohort cancels
common-mode noise (residual receiver clock, ZTD residual, position
bias absorbed by the filter).  A wrong NL fix produces a
sustained per-SV-specific residual that doesn't cancel in the
cohort median.

Note on the phase residual itself: the filter's ``last_residual_labels``
streams ``(sv, kind, elev)`` triples aligned with the ``last``
post-fit residual array.  ``kind == 'phi'`` entries carry the
post-fit IF phase residual in metres — this is the
filter's view of "how far this SV's phase observation is from the
model after the Kalman update".  On a correct fix it hovers near
zero (mm-cm).  On a wrong NL fix, it's offset by some fraction of
the NL wavelength (~10.7 cm for GPS L1+L2, ~10.6 cm for Galileo
L1+L5) — well above the mm-scale post-fit noise floor.

Anti-noise: a single epoch over threshold isn't enough to trip;
the detector requires ``consecutive_epochs`` consecutive over-
threshold residuals before declaring a wrong-fix event.  Default
2 — high enough to dedup single-epoch outliers, low enough that a
real wrong fix (which produces *sustained* high residuals)
doesn't take long to catch.

Caveats:

  - **Multiple SVs wrong in the same direction**: the filter
    absorbs the common bias into clock / ZTD / position states.
    Per-SV residuals are smaller than the full integer-mismatch
    would imply.  Cohort-median is biased toward the wrong-side
    consensus, and subtracting it can hide the mismatched SVs.
    Same trade-off as ``GfStepMonitor``; accepted because the
    common case (one rogue fix among many correct) is the high-
    leverage case.
  - **Cohort size 1**: cohort-median of a single SV is itself,
    residual after subtraction is identically zero — no trip.
    Acts as a safety guard during single-SV warmup periods.
  - **ANCHORED-only residuals**: the filter's labels include all
    SVs that contributed to the update; we filter to NL-fixed
    SVs (caller's responsibility).

Usage pattern (mirrors ``GfStepMonitor``):

    monitor = IfStepMonitor()
    # Each epoch, after the filter update:
    nl_fixed_now = {sv for sv in observed_svs if state.is_nl_fixed(sv)}
    for sv in nl_fixed_now - prev_nl_fixed:
        monitor.note_fix(sv)
    for sv in prev_nl_fixed - nl_fixed_now:
        monitor.note_unfix(sv)
    phi_resid_by_sv = {sv: r for (sv, kind, _), r in zip(labels, resid)
                       if kind == 'phi' and sv in nl_fixed_now}
    events = monitor.update(phi_resid_by_sv)
    for ev in events:
        # NL-layer eviction action: tracker → WAITING, NL unfix,
        # blacklist, ambiguity inflate, MW reset.
        tracker.transition(ev['sv'], WAITING, cooldown_epochs=...)
        nl.unfix(ev['sv'])
        ...
    prev_nl_fixed = nl_fixed_now

Not thread-safe.  Call from the AntPosEst thread only.
"""

from __future__ import annotations

import logging
from statistics import median

log = logging.getLogger(__name__)


# Default trip threshold (metres).  Set to 5 cm — about half a NL
# wavelength (λ_NL ≈ 10.6 cm for GPS L1+L2 and Galileo L1+L5).  A
# wrong NL fix by 1 integer cycle leaks into the post-fit phase
# residual at full λ_NL or at fractions thereof depending on how
# much the filter has absorbed.  5 cm catches even partially-
# absorbed wrong fixes; mm-scale post-fit noise on correct fixes
# stays well below.
_DEFAULT_THRESHOLD_M = 0.05

# Default consecutive-epochs trip requirement.  Two epochs filters
# out single-epoch noise spikes.  Lower than ``GfStepMonitor``'s
# implicit need for time-scale separation because wrong-fix-driven
# IF residuals are sustained by definition (the wrong integer
# doesn't go away).
_DEFAULT_CONSECUTIVE = 2

# Minimum cohort size for cohort-median to be meaningful.  Below
# this, the detector skips evaluation entirely.  Bumped from 2 → 4
# per I-140938-main (2026-04-29 morning): with n=2 the median IS
# the mean, so any disagreement makes BOTH SVs look like outliers.
# clkPoC3 09:00:09 lost ANCHORED E23 + E33 simultaneously this way.
# At n=4 the median has at least one robustness-budget sample
# either side, preventing the small-cohort pathology entirely.
_DEFAULT_MIN_COHORT = 4

# Per-SV warmup epochs before the SV's residual contributes to the
# cohort median.  Per I-140938-main (2026-04-29 morning): a freshly
# admitted SV's residual hasn't settled yet — its float ambiguity
# is still converging — so its residual is large and unreliable.
# Including it in the cohort median pollutes the reference and can
# carry well-behaved ANCHORED SVs out with it.  MadHat 08:56:12
# lost E23 (anchored 422 s, residual within threshold) because
# freshly-admitted E21 with residual=+0.164 m polluted the median.
# Default 30 epochs matches WlDriftMonitor's warmup convention.
# Note: SVs in warmup are still EVALUATED for trips themselves —
# they just don't contribute to the cohort median.
_DEFAULT_WARMUP = 30

# Multiplier applied to the trip threshold for ANCHORED SVs.  Per
# Bob's note on I-140938-main: 'I like the idea of protecting SVs
# that have earned state ANCHORED. Even if they're loaners as we
# saw this morning, I think they have proven value.'  ANCHORED SVs
# have survived the Δaz=15° validation that promotes them out of
# ANCHORING — that's earned trust which a 2-epoch borderline
# cohort trip shouldn't throw away.
_DEFAULT_ANCHORED_MULT = 2.0


class IfStepMonitor:
    """Per-epoch cohort-median post-fit IF residual detector.
    NL-layer demoter — emits trip events when an SV's post-fit
    phase residual differs from the cohort median by more than
    ``threshold_m`` for ``consecutive_epochs`` epochs.
    """

    def __init__(
        self,
        threshold_m: float = _DEFAULT_THRESHOLD_M,
        consecutive_epochs: int = _DEFAULT_CONSECUTIVE,
        min_cohort_size: int = _DEFAULT_MIN_COHORT,
        warmup_epochs: int = _DEFAULT_WARMUP,
        anchored_threshold_mult: float = _DEFAULT_ANCHORED_MULT,
    ) -> None:
        self._threshold = float(threshold_m)
        self._consecutive = int(consecutive_epochs)
        self._min_cohort = int(min_cohort_size)
        self._warmup = int(warmup_epochs)
        self._anchored_mult = float(anchored_threshold_mult)
        # sv → True when the monitor is actively tracking this SV
        # (note_fix called, note_unfix not yet).  Used to gate
        # update() so caller can pass arbitrary residual dicts.
        self._tracked: set[str] = set()
        # sv → epochs since note_fix.  SVs with epochs_since_fix <
        # warmup_epochs do not contribute to the cohort median (but
        # are still evaluated for trips themselves).
        self._epochs_since_fix: dict[str, int] = {}
        # sv → consecutive-over-threshold counter.  Resets to zero
        # any epoch the SV's residual is at or below threshold.
        self._streak: dict[str, int] = {}
        # sv → True iff the SV has been tripped and is awaiting
        # external eviction.  Prevents re-triggering until the
        # caller's eviction path calls note_unfix.
        self._tripped: set[str] = set()

    # ── Lifecycle ─────────────────────────────────────────────── #

    def note_fix(self, sv: str) -> None:
        """Start tracking ``sv`` — call when its NL integer is
        committed and the SV transitions into ANCHORING (or
        ANCHORED on direct promotion).

        Idempotent: re-notifying clears prior streak / trip state
        AND restarts the warmup count.
        """
        self._tracked.add(sv)
        self._streak[sv] = 0
        self._epochs_since_fix[sv] = 0
        self._tripped.discard(sv)

    def note_unfix(self, sv: str) -> None:
        """Stop tracking ``sv`` — call when NL state is reset, the
        SV is dropped, or this monitor flagged it and the caller
        acted on the trip event."""
        self._tracked.discard(sv)
        self._streak.pop(sv, None)
        self._epochs_since_fix.pop(sv, None)
        self._tripped.discard(sv)

    # ── Per-epoch update ──────────────────────────────────────── #

    def update(
        self,
        residuals: dict[str, float],
        anchored_svs: set[str] | None = None,
    ) -> list[dict]:
        """Process one epoch's post-fit phase residuals.

        ``residuals`` is a dict ``{sv: phi_resid_m}`` of currently-
        NL-fixed SVs with valid phase residuals this epoch.  SVs
        not in the monitor's tracked set are ignored (caller may
        pass the full filter residual dict; we filter internally).

        ``anchored_svs`` is the set of SVs currently in the
        ANCHORED state (long-term members; have survived the
        Δaz=15° validation).  These get a 2× threshold per
        I-140938-main / Bob's earned-trust note.  Pass None to
        disable ANCHORED protection (all SVs use the base threshold).

        Returns one trip event per SV that just crossed the
        consecutive-epochs threshold.  Event shape:

          ``sv``                  — the offending SV id
          ``residual_m``          — post-fit IF phase residual at
                                    this epoch (signed)
          ``cohort_residual_m``   — Δ from cohort median (signed)
          ``cohort_median_m``     — the cohort-median residual
          ``threshold_m``         — effective trip threshold
                                    (base × ANCHORED multiplier
                                    if applicable)
          ``threshold_base_m``    — configured base threshold
          ``anchored``            — bool, was ANCHORED multiplier
                                    applied?
          ``consecutive_epochs``  — streak length at trip time
          ``cohort_size``         — number of post-warmup tracked
                                    SVs that contributed to the
                                    median
          ``cohort_size_total``   — number of tracked SVs total
                                    (includes warming-up SVs that
                                    were excluded from the median)
        """
        anchored = anchored_svs or set()

        # Filter to tracked SVs and increment per-SV epoch counter.
        tracked_resids: dict[str, float] = {}
        for sv, r in residuals.items():
            if sv not in self._tracked:
                continue
            tracked_resids[sv] = float(r)
            self._epochs_since_fix[sv] = (
                self._epochs_since_fix.get(sv, 0) + 1
            )

        # Cohort = tracked SVs that have completed warmup.  Warming-
        # up SVs are still EVALUATED (so they can trip on their own
        # bad signal) but don't pollute the median reference.
        cohort_resids = {
            sv: r for sv, r in tracked_resids.items()
            if self._epochs_since_fix.get(sv, 0) > self._warmup
        }

        if len(cohort_resids) < self._min_cohort:
            # Cohort too small — cohort-median isn't meaningful.
            # Reset streaks so a sufficient cohort later doesn't
            # trip on stale buildup.
            for sv in tracked_resids:
                self._streak[sv] = 0
            return []

        # Cohort-median residual (common-mode absorbed by filter
        # state but not perfectly — residual leakage cancels here).
        cohort_median = median(cohort_resids.values())
        cohort_size = len(cohort_resids)
        cohort_size_total = len(tracked_resids)

        events: list[dict] = []
        for sv, r in tracked_resids.items():
            cohort_residual = r - cohort_median
            sv_anchored = sv in anchored
            effective_threshold = (
                self._threshold * self._anchored_mult if sv_anchored
                else self._threshold
            )
            over = abs(cohort_residual) > effective_threshold

            if over:
                self._streak[sv] = self._streak.get(sv, 0) + 1
            else:
                self._streak[sv] = 0
                # Defensive: drop trip flag if somehow still set.
                self._tripped.discard(sv)

            if (self._streak.get(sv, 0) >= self._consecutive
                    and sv not in self._tripped):
                self._tripped.add(sv)
                events.append({
                    'sv': sv,
                    'residual_m': r,
                    'cohort_residual_m': cohort_residual,
                    'cohort_median_m': cohort_median,
                    'threshold_m': effective_threshold,
                    'threshold_base_m': self._threshold,
                    'anchored': sv_anchored,
                    'consecutive_epochs': self._streak[sv],
                    'cohort_size': cohort_size,
                    'cohort_size_total': cohort_size_total,
                })

        return events

    # ── Diagnostics ───────────────────────────────────────────── #

    def n_tracking(self) -> int:
        return len(self._tracked)

    def streak(self, sv: str) -> int:
        """Current consecutive-over-threshold streak for ``sv``,
        zero if the last epoch's residual was below threshold or
        the SV is untracked."""
        return self._streak.get(sv, 0)

    def summary(self) -> str:
        return (
            f"if_step: tracking {len(self._tracked)} NL-fixed SVs "
            f"(threshold=±{self._threshold*100:.1f}cm, "
            f"consecutive={self._consecutive}ep)"
        )
