"""Per-epoch cohort-median Δgf step detector — GF v2 (demoter).

Replaces the MW-based ``WlDriftMonitor`` in the demotion path.
Empirical motivation: ``WlDriftMonitor``'s post-fix MW residual
rolling mean is uncorrelated with BNC slip events at chance level
(Z = -0.17, p = 0.86 — see
``project_wl_drift_vs_bnc_finding_20260428``).  The MW combination
mixes phase and pseudorange; PR multipath dominates the residual.
``GfPhaseRollingMeanMonitor`` (the v1 phase-only sibling) tripped
on accumulated iono drift on long-fixed SVs because (gf_current -
gf_at_fix) grows linearly with iono drift since fix — verified in
2026-04-28 afternoon deploy.

This detector uses the **first difference** of GF (Δgf), not the
cumulative residual against a fixed reference.  Real cycle slips
appear as a step in Δgf at one epoch (instantaneous, of magnitude
λ_L1 ≈ 19 cm for an L1 slip or λ_L5 ≈ 25.5 cm for an L5 slip).
Slow ionospheric drift appears as a small near-constant Δgf
(~1 mm / epoch at 1 Hz under typical mid-latitude daytime
conditions, well below the trip threshold).

To handle fast iono activity (sunrise TEC, geomagnetic storms),
the detector subtracts the **cohort-median Δgf** computed across
all currently-fixed SVs on this host each epoch.  Common-mode iono
gradients affect all SVs roughly equally; the median cancels them
without needing an explicit Klobuchar / SSR iono model.  A real
slip on a single SV creates a per-SV residual relative to the
cohort median — that's what this detector trips on.

Anti-noise: a single epoch over threshold isn't enough to trip;
the detector requires ``consecutive_epochs`` consecutive over-
threshold residuals before declaring a slip event.  Default 2 —
a real slip's Δgf step persists for at least one more sample
(the next Δgf returns near-zero or near-cohort, but the SV's
*absolute* GF is offset; consecutive-epoch trips catch the
sustained-offset signature directly only if the post-step Δgf
also drifts off-cohort).

Caveats:

  - **Single-epoch slips on otherwise stable SVs**: a clean slip
    that lands and stays produces ONE epoch with high Δgf, then
    Δgf returns to near-zero.  ``consecutive_epochs=2`` would miss
    it.  ``cycle_slip.py``'s instantaneous gf_jump detector still
    runs and catches that case; this monitor is the post-fix
    sustained-offset detector.
  - **Cohort size 1**: with only one fixed SV, cohort median is
    that SV's own Δgf, residual is trivially zero, no trip.  Acts
    as a safety guard during single-SV warmup periods.
  - **Cohort with one outlier**: the outlier (e.g., a slipped SV)
    gets included in the median computation.  Median is robust to
    one outlier among many; with very small cohorts (3-4), one
    outlier shifts the median noticeably.  Trade-off accepted —
    larger cohorts are typical in operations.

Usage pattern:

    monitor = GfStepMonitor()
    # Each epoch, after MW updates have run:
    fixed_now = {sv for sv, s in mw._state.items() if s.get('fixed')}
    for sv in fixed_now - prev_fixed:
        gf_now_sv = compute_gf_m(observation_for(sv))
        monitor.note_fix(sv, gf_now_sv)
    for sv in prev_fixed - fixed_now:
        monitor.note_unfix(sv)
    gf_now_all = {sv: compute_gf_m(obs[sv]) for sv in fixed_now}
    events = monitor.update(gf_now_all)
    for ev in events:
        # Demoter action: flush MW state + readmit hold + state
        # machine transition to FLOATING.
        log_gf_step_event(ev)
        mw.reset(ev['sv'])
        wl_readmit.note_flush(ev['sv'], elev[ev['sv']])
        sv_state.transition(ev['sv'], FLOATING, reason="gf_step")
    prev_fixed = fixed_now

Not thread-safe.  Call from the AntPosEst thread only.
"""

from __future__ import annotations

import logging
from statistics import median

log = logging.getLogger(__name__)


# Default trip threshold (metres).  Set to ~λ_L1 / 4 ≈ 4.76 cm
# rounded to 4 cm — same scale as ``cycle_slip.py``'s
# GF_JUMP_THRESHOLD_M for instantaneous detection.  A real cycle
# slip on one band produces a Δgf step of λ_L1 (19 cm) or λ_L5
# (25.5 cm) — well above this threshold.  Slow daytime iono drift
# (~1 mm / epoch) is well below.
_DEFAULT_THRESHOLD_M = 0.04

# Default consecutive-epochs trip requirement.  Two epochs filters
# out single-epoch noise spikes in Δgf (which can come from
# tracking transients, mass position computation churn, brief
# multipath bursts) without losing real sustained-offset slips.
_DEFAULT_CONSECUTIVE = 2

# Minimum cohort size for cohort-median to be meaningful.  Below
# this, the detector skips evaluation entirely (returns no events).
# Default 2 — with two SVs the median is the average of the two,
# which still cancels common-mode iono perfectly.
_DEFAULT_MIN_COHORT = 2


class GfStepMonitor:
    """Per-epoch cohort-median Δgf step detector.  Demoter — emits
    trip events when an SV's Δgf differs from the cohort median by
    more than ``threshold_m`` for ``consecutive_epochs`` epochs.
    """

    def __init__(
        self,
        threshold_m: float = _DEFAULT_THRESHOLD_M,
        consecutive_epochs: int = _DEFAULT_CONSECUTIVE,
        min_cohort_size: int = _DEFAULT_MIN_COHORT,
    ) -> None:
        self._threshold = float(threshold_m)
        self._consecutive = int(consecutive_epochs)
        self._min_cohort = int(min_cohort_size)
        # sv → previous-epoch GF observation (m).  Used to compute
        # Δgf on the next epoch.
        self._prev_gf: dict[str, float] = {}
        # sv → consecutive-over-threshold counter.  Resets to zero
        # any epoch the SV's residual is at or below threshold.
        self._streak: dict[str, int] = {}
        # sv → True iff the SV has been tripped and is awaiting
        # external demotion.  Prevents re-triggering until the
        # caller's demotion path calls note_unfix.
        self._tripped: set[str] = set()

    # ── Lifecycle ─────────────────────────────────────────────── #

    def note_fix(self, sv: str, gf_initial_m: float) -> None:
        """Start tracking ``sv`` — call when its WL integer is
        committed.  Stores the initial GF observation as the
        previous-epoch reference for the first Δgf.

        Idempotent: re-notifying clears prior state and starts
        fresh.
        """
        self._prev_gf[sv] = float(gf_initial_m)
        self._streak[sv] = 0
        self._tripped.discard(sv)

    def note_unfix(self, sv: str) -> None:
        """Stop tracking ``sv`` — call when MW state is reset, the
        SV is dropped, or this monitor flagged it and the caller
        acted on the trip event."""
        self._prev_gf.pop(sv, None)
        self._streak.pop(sv, None)
        self._tripped.discard(sv)

    # ── Per-epoch update ──────────────────────────────────────── #

    def update(self, gf_now: dict[str, float]) -> list[dict]:
        """Process one epoch's GF observations across the fixed
        cohort.  Returns one trip event per SV that just crossed
        the consecutive-epochs threshold.

        ``gf_now`` is a dict ``{sv: gf_m}`` of currently-fixed SVs
        with valid GF observations this epoch.  SVs missing from
        ``gf_now`` get their previous-epoch reference held over —
        the next time they're observed, Δgf is computed against
        the held value (potentially a stale gap, but the existing
        cycle-slip-flush detector with its arc-gap reasoning
        handles those separately).

        Event shape:
          ``sv``             — the offending SV id
          ``residual_m``     — Δgf - cohort_median (signed)
          ``cohort_median_m`` — the cohort-median Δgf
          ``delta_gf_m``     — this SV's Δgf
          ``threshold_m``    — configured trip threshold
          ``consecutive_epochs`` — streak length at trip time
          ``cohort_size``    — number of SVs that contributed to
                              the median this epoch
        """
        # Compute Δgf for each tracked SV present this epoch.
        # SVs not in ``self._prev_gf`` were never note_fix'd; we
        # ignore them entirely (the engine may pass the full
        # observed-SV dict including not-yet-fixed SVs — those
        # shouldn't influence the cohort or be evaluated).  SVs
        # in self._prev_gf but absent from gf_now hold their
        # prev_gf — they don't contribute to this cohort but are
        # still tracked for next time.
        deltas: dict[str, float] = {}
        for sv, gf_cur in gf_now.items():
            if sv not in self._prev_gf:
                continue  # not note_fix'd — ignore
            prev = self._prev_gf[sv]
            deltas[sv] = float(gf_cur) - prev
            self._prev_gf[sv] = float(gf_cur)

        if len(deltas) < self._min_cohort:
            # Cohort too small — common-mode estimation isn't
            # meaningful.  Don't update streaks; reset them so we
            # don't trip on the next sufficient cohort with a stale
            # buildup.
            for sv in deltas:
                self._streak[sv] = 0
            return []

        # Cohort-median Δgf (common-mode iono estimate).
        cohort_median = median(deltas.values())
        cohort_size = len(deltas)

        events: list[dict] = []
        for sv, delta in deltas.items():
            residual = delta - cohort_median
            over = abs(residual) > self._threshold

            if over:
                self._streak[sv] = self._streak.get(sv, 0) + 1
            else:
                self._streak[sv] = 0
                # Recovery from a previously-tripped state — the
                # caller is expected to have already called
                # note_unfix when the trip fired, so this branch
                # is mostly defensive.  But if for some reason
                # the SV is still flagged, drop the flag so a
                # future genuine trip can fire.
                self._tripped.discard(sv)

            if (self._streak.get(sv, 0) >= self._consecutive
                    and sv not in self._tripped):
                self._tripped.add(sv)
                events.append({
                    'sv': sv,
                    'residual_m': residual,
                    'cohort_median_m': cohort_median,
                    'delta_gf_m': delta,
                    'threshold_m': self._threshold,
                    'consecutive_epochs': self._streak[sv],
                    'cohort_size': cohort_size,
                })

        return events

    # ── Diagnostics ───────────────────────────────────────────── #

    def n_tracking(self) -> int:
        return len(self._prev_gf)

    def streak(self, sv: str) -> int:
        """Current consecutive-over-threshold streak for ``sv``,
        zero if the last epoch's residual was below threshold or
        the SV is untracked."""
        return self._streak.get(sv, 0)

    def summary(self) -> str:
        return (
            f"gf_step: tracking {len(self._prev_gf)} SVs "
            f"(threshold=±{self._threshold*100:.1f}cm, "
            f"consecutive={self._consecutive}ep)"
        )
