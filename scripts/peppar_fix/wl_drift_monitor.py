"""Wide-lane post-fix residual drift monitor.

Detects wrong WL integer commits by observing that, after an SV's WL
has been fixed, its post-fix Melbourne-Wübbena residual (MW
observation minus committed integer) should hover near zero.  A
wrong integer causes systematic drift as new observations accumulate
inconsistent with the commitment.

This is the per-SV analog of `FalseFixMonitor`'s PR-residual check at
the NL layer: direct evidence that an integer commitment is wrong,
acting **minutes before** the aggregate filter state (ZTD, altitude)
has absorbed enough bias to breach physical envelopes that the host-
level monitors (``ztd_impossible`` / ``ztd_cycling``) watch.

Motivated by the overnight 2026-04-22/23 WL-only run.  All four hosts
reached a high-quality converged state pre-sunrise (L5 fleet agreed
to 0.50 m altitude, σ ≈ 18 mm, ZTD within ±310 mm), then a sunrise
TEC slip storm corrupted two hosts (clkPoC3, MadHat) via wrong WL
re-acquisitions while the other two (TimeHat, ptpmon) rode through.
Post-hoc: the "pull phase" between a bad integer landing and ZTD
breaching threshold was 30–45 minutes on the compromised hosts.  A
per-SV drift monitor firing at 3-minute rolling window would have
caught it in the pull phase.  See
``docs/wl-only-foundation.md`` and the corresponding analysis memo.

The detector is the per-SV form of a one-sample z-test on the
rolling mean: under the null hypothesis (correct integer, MW noise
is zero-mean), the rolling mean is bounded by σ_MW / √N.  A
persistent non-zero rolling mean exceeding threshold falsifies the
null — either the integer is wrong or the bias model is wrong.  An
ensemble chi-squared across the set of fixed-WL SVs is a natural
sibling test (catches systemic issues the per-SV test misses, but
doesn't identify which SV); deferred for a follow-on monitor.

Usage pattern:

    monitor = WlDriftMonitor()
    # Each epoch, after MW tracker has updated this epoch's obs:
    fixed_now = {sv for sv, s in mw._state.items() if s.get('fixed')}
    for sv in fixed_now - prev_fixed:
        monitor.note_fix(sv)
    for sv in prev_fixed - fixed_now:
        monitor.note_unfix(sv)
    for sv in fixed_now:
        residual_cyc = post_fix_residual(mw, sv)
        ev = monitor.ingest(sv, residual_cyc)
        if ev is not None:
            mw.reset(sv)
            monitor.note_unfix(sv)
            sv_state.transition(sv, FLOATING, reason="wl_drift")
    prev_fixed = fixed_now

Not thread-safe.  Call from the AntPosEst thread only (matches the
other monitors' threading model).
"""

from __future__ import annotations

import logging
from collections import deque

log = logging.getLogger(__name__)


class WlDriftMonitor:
    """Per-SV rolling-mean drift detector on post-fix MW residual.

    Tracks ``(sv → deque of post-fix residuals in cycles)`` for every
    WL-fixed SV.  When the rolling mean of an SV's residuals exceeds
    ``threshold_cyc`` in magnitude over at least ``min_samples``
    samples, ``ingest()`` returns a drift event.  Caller is
    responsible for acting on the event (flushing MW, demoting the
    SV) and calling ``note_unfix``.

    Parameters are in MW cycles (λ_WL ≈ 0.75 m for L1-L5, so 1 cycle
    is a large drift — threshold ``0.15`` cyc ≈ 11 cm is well below
    typical WL measurement noise after 60-epoch averaging, and 7×
    below a single wrong-integer offset).
    """

    def __init__(
        self,
        window_epochs: int = 30,
        threshold_cyc: float = 0.25,
        min_samples: int = 15,
        warmup_epochs: int = 30,
    ) -> None:
        self._window = int(window_epochs)
        self._threshold = float(threshold_cyc)
        self._min_samples = int(min_samples)
        # Post-fix warmup: don't feed the rolling window for this
        # many ingest calls after ``note_fix``.  The MW tracker's EMA
        # (tau ≈ 60 epochs) takes ~30 epochs to settle to the
        # post-fix mean even when the integer commitment is correct,
        # because fixes happen at ``|frac| < 0.15`` rather than
        # exactly zero.  Ingesting during that settling window
        # produces correct-integer residuals that legitimately
        # drift from 0.14 → 0 — indistinguishable from wrong-
        # integer drift without context.  Warmup suppresses the
        # ambiguity.  Day0423a showed ~270 drift events per host
        # in 1h20m without warmup (3.4/min) — 90% false positives
        # kicking marginal-frac fixes out of the set faster than
        # they could re-acquire.
        self._warmup = int(warmup_epochs)
        # sv → deque of cycles.  Present ⇔ SV is being monitored
        # (fixed, post-note_fix, pre-note_unfix).
        self._hist: dict[str, deque[float]] = {}
        # sv → count of ingest calls received since note_fix.  Used
        # to skip the warmup window.  Reset on note_fix, cleared on
        # note_unfix.
        self._ingest_count: dict[str, int] = {}

    # ── Lifecycle ───────────────────────────────────────────────── #

    def note_fix(self, sv: str) -> None:
        """Start tracking ``sv`` — call when its WL integer is
        committed.  Idempotent: re-notifying an already-tracked SV
        clears its history and restarts the warmup count (fresh
        window after re-fix)."""
        self._hist[sv] = deque(maxlen=self._window)
        self._ingest_count[sv] = 0

    def note_unfix(self, sv: str) -> None:
        """Stop tracking ``sv`` — call when its MW state is reset,
        the SV is dropped, or the drift monitor itself flagged it
        and the caller acted."""
        self._hist.pop(sv, None)
        self._ingest_count.pop(sv, None)

    # ── Observation intake ──────────────────────────────────────── #

    def ingest(self, sv: str, residual_cyc: float) -> dict | None:
        """Add one post-fix residual sample for ``sv``.

        Returns a drift event dict when the rolling-mean magnitude
        exceeds ``threshold_cyc`` over ≥ ``min_samples`` samples,
        else ``None``.  The event carries:

          - ``sv``: the offending SV id
          - ``drift_cyc``: signed rolling mean (sign tells direction)
          - ``threshold_cyc``: the configured trip threshold
          - ``n_samples``: number of samples in the rolling window
          - ``window_epochs``: configured window size

        Untracked SVs (no ``note_fix``) return ``None`` silently.
        """
        h = self._hist.get(sv)
        if h is None:
            return None
        # Warmup: count the call, but don't feed it to the window
        # until the EMA has had time to settle past the fix-time
        # fractional offset.
        self._ingest_count[sv] = self._ingest_count.get(sv, 0) + 1
        if self._ingest_count[sv] <= self._warmup:
            return None
        h.append(float(residual_cyc))
        if len(h) < self._min_samples:
            return None
        mean = sum(h) / len(h)
        if abs(mean) <= self._threshold:
            return None
        return {
            'sv': sv,
            'drift_cyc': mean,
            'threshold_cyc': self._threshold,
            'n_samples': len(h),
            'window_epochs': self._window,
        }

    # ── Diagnostics ─────────────────────────────────────────────── #

    def n_tracking(self) -> int:
        return len(self._hist)

    def rolling_mean(self, sv: str) -> float | None:
        """Current rolling mean for ``sv``, or ``None`` if untracked
        or window not yet filled to ``min_samples``.  Exposed for
        tests and for engine-level summary logging."""
        h = self._hist.get(sv)
        if h is None or len(h) < self._min_samples:
            return None
        return sum(h) / len(h)

    def summary(self) -> str:
        return (
            f"wl_drift: tracking {len(self._hist)} SVs "
            f"(window={self._window}ep, threshold=±{self._threshold:.2f}cyc)"
        )
