"""Pre-fix WL admission gate using post-fit phase residuals.

Per dayplan I-115539 workstream A (2026-04-29 morning), as a
pragmatic v1 of ``docs/wl-admission-phase-only-future.md``.
Completes the family started yesterday by ``GfStepMonitor``
(post-WL eviction, commit ``a07448d``) and ``IfStepMonitor``
(post-NL eviction, commit ``31cc761``):

  - **GfStepMonitor**: catches wrong WL integers AFTER admission
    via post-fix Δgf cohort-median deviation.
  - **IfStepMonitor**: catches wrong NL integers AFTER admission
    via post-fix IF residual cohort-median deviation.
  - **WlPhaseAdmissionGate** (this): catches wrong WL integers
    BEFORE admission via per-SV post-fit phase residual stats,
    so the wrong integer never lands in the fix set in the first
    place.

Empirical motivation: the day0428night data showed the same SV
(clkPoC3 E27) admitted, evicted, re-admitted at a different wrong
integer **9 times overnight**.  Each cycle wastes ~10 minutes of
convergence.  The eviction monitors work — they catch every
wrong integer — but they can't prevent the next wrong admission
from being attempted.  The closed loop the future-work doc
predicted.

## Architecture — phase consistency as the admission check

When ``MelbourneWubbenaTracker`` proposes admitting an SV (its
``mw_avg / λ_WL`` fractional part is below ``fix_threshold``),
this gate performs a phase-side consistency check before the
admission is allowed to land:

  1. **Per-SV running stats** on the filter's post-fit phase
     residual (the ``'phi'`` entries from
     ``PPPFilter.last_residual_labels``).  On a clean float
     ambiguity that's converging toward a correct integer,
     post-fit phase residuals hover at the mm-cm noise floor.
     PR-driven false-fix candidates show inflated residual
     variance — the filter can't reconcile the phase with the
     wrong integer that PR pushed the float toward.

  2. **Cohort-median subtraction** (if min_cohort_size SVs
     available): receiver clock residual, ZTD residual, and
     other common-mode signals are absorbed by the median.
     Per-SV residual after subtraction isolates the per-SV
     contribution — the wrong-integer signature.

  3. **Trip condition**: |per-SV mean| > threshold OR std >
     threshold over the rolling window.  Either signature
     blocks admission.

## Why "gate" instead of full Kalman + LAMBDA replacement

The textbook PPP-AR approach (option 1 in the future-work doc)
estimates per-SV WL ambiguity via a Kalman filter using
carrier-phase observations only, with PR providing weak Gaussian
priors via inflated variance.  LAMBDA decorrelation + ratio test
gates admission.  That's a 1-2 day rebuild that touches the
filter state vector, the resolver, and the corrections plumbing.

This gate is a 3-4h pragmatic v1 that lives outside the filter:
it CHECKS what MW proposes against an independent phase-only
signal, but doesn't itself drive the integer search.  The
``pr_prior_sigma_m`` constructor parameter is reserved for the
Kalman+LAMBDA upgrade — accepting it now keeps the constructor
signature stable so the upgrade is an internal swap, not an API
change.

If this gate empirically suffices to break the cycling (the 02:00
cron pivot tomorrow morning will tell), the full Kalman+LAMBDA
upgrade may not be needed.  If not, the upgrade path is documented
in the future-work doc.

## Threshold rationale

  - 5 cm matches IfStepMonitor's threshold — same scale as
    "post-fit phase residual at NL ambiguity-error level".  λ_WL
    ≈ 75 cm so a wrong WL integer leaks ~75 cm into the post-fit
    residual; even tiny fractions of that are well above 5 cm.
  - mm-cm noise floor on a clean fix sits comfortably below 5 cm.
  - Configurable via constructor.

Usage pattern (mirrors the other monitors):

    gate = WlPhaseAdmissionGate()
    # Each epoch, after PPPFilter update:
    for label, residual in zip(filt.last_residual_labels, post_resid):
        if label[1] == 'phi':
            gate.ingest(label[0], float(residual))
    # When MW tracker is about to admit an SV:
    for sv in mw.proposed_admissions():
        if not gate.is_phase_consistent(sv):
            # Block — phase residuals don't agree with this integer.
            mw.refuse_admission(sv, reason='wl_phase_admission_gate')
    gate.evict_dropped_svs(currently_observed_svs)

Not thread-safe.  Call from the AntPosEst thread only.
"""

from __future__ import annotations

import logging
from collections import deque
from statistics import median, mean

log = logging.getLogger(__name__)


# Default trip threshold (metres).  Mean OR std of the per-SV
# post-fit phase residual deque exceeding this fails the gate.
# Matches IfStepMonitor's threshold for architectural consistency
# at the phase-residual scale across both layers.
_DEFAULT_THRESHOLD_M = 0.05

# Rolling window for per-SV stats (epochs).  Matches the WL
# admission warmup (typical mw.min_epochs=30).  Long enough that
# stats are stable; short enough that recent transients dominate
# any old-noise tails.
_DEFAULT_WINDOW = 30

# Minimum samples in window before the gate has an opinion.  Below
# this, ``is_phase_consistent`` returns True (don't block on
# insufficient data — let MW make the call alone, same behaviour
# as the gate not existing).
_DEFAULT_MIN_SAMPLES = 10

# Minimum cohort size for cohort-median subtraction.  Below this,
# evaluate raw per-SV residuals (no common-mode subtraction).
_DEFAULT_MIN_COHORT = 3


class WlPhaseAdmissionGate:
    """Per-SV phase-residual gate over MW's WL admission decisions.

    Maintains a rolling deque of post-fit phase residuals per SV.
    When asked whether an SV is safe to admit, computes per-SV
    mean and std over the window and trips if either exceeds the
    threshold.  Cohort-median subtraction handles common-mode
    receiver clock + ZTD residuals.
    """

    def __init__(
        self,
        threshold_m: float = _DEFAULT_THRESHOLD_M,
        window_epochs: int = _DEFAULT_WINDOW,
        min_samples: int = _DEFAULT_MIN_SAMPLES,
        min_cohort_size: int = _DEFAULT_MIN_COHORT,
        pr_prior_sigma_m: float | None = None,
    ) -> None:
        self._threshold = float(threshold_m)
        self._window = int(window_epochs)
        self._min_samples = int(min_samples)
        self._min_cohort = int(min_cohort_size)
        # Reserved for the Kalman+LAMBDA upgrade.  No effect in v1
        # (gate-only).  Stored so the constructor signature is
        # stable for the upgrade.
        self._pr_prior_sigma_m = pr_prior_sigma_m
        # sv → deque of post-fit phase residuals (m).  Per-epoch
        # ingest appends; window trims old.
        self._hist: dict[str, deque[float]] = {}

    # ── Per-epoch ingest ──────────────────────────────────────── #

    def ingest(self, sv: str, phi_residual_m: float) -> None:
        """Append one post-fit phase residual sample for ``sv``.

        Caller pulls the residual from
        ``PPPFilter.last_residual_labels`` filtered to entries with
        ``kind == 'phi'``.  Untracked SVs auto-vivify a deque on
        first ingest.
        """
        d = self._hist.get(sv)
        if d is None:
            d = deque(maxlen=self._window)
            self._hist[sv] = d
        d.append(float(phi_residual_m))

    def evict_unobserved(self, observed_svs: set[str]) -> None:
        """Drop history for SVs not observed this epoch.  Caller
        should pass the set of currently observed SVs each epoch
        so the gate's memory matches reality.

        Without this, an SV that drops out of view keeps a stale
        deque indefinitely; on re-emergence the gate would evaluate
        it against ancient data.
        """
        for sv in list(self._hist):
            if sv not in observed_svs:
                self._hist.pop(sv, None)

    # ── Cohort-median helper ──────────────────────────────────── #

    def _cohort_median_recent(self) -> float:
        """Median of the most-recent residual across all tracked
        SVs with at least one sample.  Approximates the common-
        mode contribution (receiver clock residual + ZTD wet
        residual + position bias) absorbed identically into every
        SV's post-fit phase residual.

        Returns 0.0 when fewer than ``min_cohort_size`` SVs have
        recent data — caller falls back to raw per-SV stats.
        """
        recent = [d[-1] for d in self._hist.values() if d]
        if len(recent) < self._min_cohort:
            return 0.0
        return float(median(recent))

    # ── Admission check ───────────────────────────────────────── #

    def is_phase_consistent(self, sv: str) -> bool:
        """Return True iff this SV's recent phase residuals are
        consistent with a clean float ambiguity (i.e., admission
        is safe).

        Returns True (don't block) when:
          - SV is untracked or has fewer than min_samples
            observations (insufficient data — defer to MW alone)
          - per-SV stats after cohort-median subtraction stay below
            threshold for both mean and std

        Returns False (block) when per-SV stats exceed threshold —
        the phase residuals are noisy in a way that's inconsistent
        with the integer MW is proposing.
        """
        d = self._hist.get(sv)
        if d is None or len(d) < self._min_samples:
            return True  # Insufficient data — defer to MW.
        cohort_med = self._cohort_median_recent()
        # Subtract the common-mode contribution from each sample.
        adjusted = [r - cohort_med for r in d]
        m = abs(mean(adjusted))
        # Std via numerically-stable two-pass.
        n = len(adjusted)
        mu = sum(adjusted) / n
        var = sum((x - mu) ** 2 for x in adjusted) / n
        std = var ** 0.5
        return m <= self._threshold and std <= self._threshold

    def evaluation_detail(self, sv: str) -> dict | None:
        """Diagnostic snapshot of the gate's per-SV state.  Useful
        for log emission when a block fires.  Returns None when
        SV has insufficient samples."""
        d = self._hist.get(sv)
        if d is None or len(d) < self._min_samples:
            return None
        cohort_med = self._cohort_median_recent()
        adjusted = [r - cohort_med for r in d]
        n = len(adjusted)
        mu = sum(adjusted) / n
        var = sum((x - mu) ** 2 for x in adjusted) / n
        std = var ** 0.5
        return {
            'sv': sv,
            'n_samples': n,
            'window_epochs': self._window,
            'mean_m': float(mu),
            'std_m': float(std),
            'threshold_m': self._threshold,
            'cohort_median_m': cohort_med,
        }

    # ── Diagnostics ───────────────────────────────────────────── #

    def n_tracking(self) -> int:
        return len(self._hist)

    def summary(self) -> str:
        return (
            f"wl_phase_admission_gate: tracking {len(self._hist)} SVs "
            f"(threshold=±{self._threshold*100:.1f}cm, "
            f"window={self._window}ep)"
        )
