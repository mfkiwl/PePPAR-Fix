"""Per-SV rolling-mean monitor on post-fix geometry-free phase residual.

Phase-only sibling to ``WlDriftMonitor``.  Tracks the geometry-free
phase combination

    GF(sv, t) = φ_L1(sv, t) · λ_L1 − φ_L5(sv, t) · λ_L5    (metres)

per fixed-WL satellite, captures its value at the moment of WL
commit (``gf_ref``), and watches the rolling mean of
``GF(t) − gf_ref`` over the configured window.  A wrong WL integer
commitment produces a step in GF (6–25 cm depending on which side
of the WL pair was committed wrong); slow ionospheric drift
produces a smooth ramp.  Rolling-mean threshold separates the two
in the time-scale-separation sense BNC's RTKLIB cycle-slip
detector relies on.

The whole point of this monitor — and the reason it was proposed
on 2026-04-28 (``docs/wl-drift-redesign-proposal.md``) — is to
provide a phase-only counterpart to ``WlDriftMonitor`` whose
input signal is uncontaminated by pseudorange noise.  The MW
combination that ``WlDriftMonitor`` watches is ``phase −
pseudorange``; PR multipath and code-bias drift trip it.  GF is
phase-only by construction.

For a 2026-04-27 BNC validation showing ``WlDriftMonitor`` is
statistically uncorrelated with BNC's slip events at chance level
(Z = −0.17, p = 0.86) while the engine's existing GF-based slip
detector (``cycle_slip.py``) gets +12.4 % above-chance correlation,
see ``project_wl_drift_smooth_float_signal_20260428``.

Iono caveat: GF drifts smoothly with TEC.  Night-time iono drift is
~mm / minute — well below the default 5 cm threshold over a
30-epoch (~30 s) window.  Sunrise / storm conditions can produce
10s of cm / minute drift; this monitor will FP under those.  When
that becomes operationally relevant, two hardening options:

  - subtract a model-based iono ramp (Klobuchar / SSR) before
    the rolling mean is computed
  - subtract the cohort median Δ-GF (common-mode iono signature
    across all currently-fixed SVs)

Both deferred until empirical data shows storm-condition FPs are
the bottleneck.

Usage pattern (mirrors ``WlDriftMonitor``):

    monitor = GfPhaseRollingMeanMonitor()
    # Each epoch, after MW tracker has updated this epoch's obs:
    fixed_now = {sv for sv, s in mw._state.items() if s.get('fixed')}
    for sv in fixed_now - prev_fixed:
        gf_now = compute_gf_m(observation_for(sv))
        monitor.note_fix(sv, gf_ref_m=gf_now)
    for sv in prev_fixed - fixed_now:
        monitor.note_unfix(sv)
    for sv in fixed_now:
        gf_now = compute_gf_m(observation_for(sv))
        ev = monitor.ingest(sv, gf_now)
        if ev is not None:
            # Observe-only mode: caller logs and does NOT demote.
            # The whole point of this monitor in 2026-04-28 deployment
            # is to validate vs BNC before becoming a demoter.
            log_gf_drift_event(ev)
    prev_fixed = fixed_now

Not thread-safe.  Call from the AntPosEst thread only (matches the
other monitors' threading model).
"""

from __future__ import annotations

import logging
from collections import deque

log = logging.getLogger(__name__)


class GfPhaseRollingMeanMonitor:
    """Per-SV rolling-mean drift detector on post-fix GF residual.

    Tracks ``(sv → deque of post-fix GF residuals in metres)`` for
    every WL-fixed SV.  When the rolling mean of an SV's residuals
    exceeds ``threshold_m`` in magnitude over at least
    ``min_samples`` samples, ``ingest()`` returns a drift event.

    Default threshold (5 cm) is set between the per-epoch GF jump
    threshold used by ``cycle_slip.py`` (~4.76 cm = λ_L1 / 4) and a
    full L5 wavelength (25.5 cm).  Picks up sustained wrong-integer
    drift without false-tripping on per-epoch thermal noise.
    """

    def __init__(
        self,
        window_epochs: int = 30,
        threshold_m: float = 0.05,
        min_samples: int = 15,
        warmup_epochs: int = 30,
        ongoing_period_epochs: int = 60,
    ) -> None:
        self._window = int(window_epochs)
        self._threshold = float(threshold_m)
        self._min_samples = int(min_samples)
        # Post-fix warmup: skip the first ``warmup_epochs`` ingest
        # calls.  Mirrors ``WlDriftMonitor``'s rationale: filter
        # state takes a few seconds to settle past the fix-time
        # transient (in our case, the GF reference itself is
        # captured at fix time so settling is quicker than MW's
        # 30-epoch EMA, but consistency with WlDriftMonitor makes
        # the side-by-side comparison cleaner).
        self._warmup = int(warmup_epochs)
        # Heartbeat cadence for sustained drifts.  After ``enter``,
        # an SV that stays above threshold gets an ``ongoing`` event
        # every ``ongoing_period_epochs`` ingest calls — keeps
        # post-hoc analysis aware of the drift's persistence without
        # the per-epoch volume that prompted main's I-185745 dedup
        # ask.  Set to 0 to disable.
        self._ongoing_period = int(ongoing_period_epochs)
        # sv → reference GF (m) captured at note_fix.  None until
        # note_fix is called with a reference value.
        self._gf_ref: dict[str, float] = {}
        # sv → deque of post-fix GF residuals in metres.
        self._hist: dict[str, deque[float]] = {}
        # sv → ingest call count since note_fix.  Used for warmup.
        self._ingest_count: dict[str, int] = {}
        # sv → True iff the SV is currently in a drift episode
        # (rolling mean above threshold, ``enter`` already emitted,
        # ``exit`` not yet).  Used to dedup per-epoch trips into
        # one ``enter`` + zero-or-more ``ongoing`` + one ``exit``.
        self._in_drift: dict[str, bool] = {}
        # sv → ingest count at time of ``enter``.  Drives ``ongoing``
        # heartbeat cadence.
        self._enter_count: dict[str, int] = {}

    # ── Lifecycle ─────────────────────────────────────────────── #

    def note_fix(self, sv: str, gf_ref_m: float) -> None:
        """Start tracking ``sv`` — call when its WL integer is
        committed.  Pass the current GF observation (metres) as
        ``gf_ref_m``; the monitor stores it as the post-fix
        reference and computes future residuals against it.

        Idempotent: re-notifying clears history and restarts the
        warmup count, capturing a fresh reference.
        """
        self._gf_ref[sv] = float(gf_ref_m)
        self._hist[sv] = deque(maxlen=self._window)
        self._ingest_count[sv] = 0
        # Re-fixing wipes any prior in-drift state without emitting
        # an exit — re-fix is itself the lifecycle boundary.
        self._in_drift.pop(sv, None)
        self._enter_count.pop(sv, None)

    def note_unfix(self, sv: str) -> dict | None:
        """Stop tracking ``sv`` — call when its MW state is reset,
        the SV is dropped, or this monitor flagged it and the
        caller acted.

        Returns a final ``exit`` event if the SV was currently in
        a drift episode at unfix time, else None.  Lets the caller
        close out the [GF_DRIFT] log record even when the trip
        chain ends with the SV dropping rather than recovering.
        """
        was_in_drift = self._in_drift.get(sv, False)
        last_mean = self.rolling_mean(sv) if was_in_drift else None
        ref = self._gf_ref.get(sv)
        n_samples = len(self._hist.get(sv, ()))
        self._gf_ref.pop(sv, None)
        self._hist.pop(sv, None)
        self._ingest_count.pop(sv, None)
        self._in_drift.pop(sv, None)
        self._enter_count.pop(sv, None)
        if was_in_drift and last_mean is not None and ref is not None:
            return {
                'sv': sv,
                'kind': 'exit',
                'reason': 'unfix',
                'drift_m': last_mean,
                'threshold_m': self._threshold,
                'n_samples': n_samples,
                'window_epochs': self._window,
                'gf_ref_m': ref,
            }
        return None

    # ── Observation intake ────────────────────────────────────── #

    def ingest(self, sv: str, gf_current_m: float) -> dict | None:
        """Add one post-fix GF observation for ``sv``.

        Returns a drift event dict on transitions only — no per-
        epoch retrigger.  Event ``kind`` is one of:

          - ``enter``: rolling mean first crossed threshold
          - ``ongoing``: still above threshold, every
            ``ongoing_period_epochs`` ingest calls after enter
          - ``exit``: rolling mean fell back below threshold

        Returns None on untracked SVs, during warmup, before the
        window has ``min_samples``, or on epochs that don't change
        the in-drift state (i.e., normally most calls).

        Each event carries:

          - ``sv``: the offending SV id
          - ``kind``: one of enter / ongoing / exit (above)
          - ``drift_m``: signed rolling mean at this epoch
          - ``threshold_m``: configured trip threshold
          - ``n_samples``: rolling-window sample count
          - ``window_epochs``: configured window size
          - ``gf_ref_m``: the reference value at fix time
        """
        h = self._hist.get(sv)
        ref = self._gf_ref.get(sv)
        if h is None or ref is None:
            return None
        # Warmup: count the call, but don't feed the window until
        # the post-fix transient has settled.
        self._ingest_count[sv] = self._ingest_count.get(sv, 0) + 1
        if self._ingest_count[sv] <= self._warmup:
            return None
        residual = float(gf_current_m) - ref
        h.append(residual)
        if len(h) < self._min_samples:
            return None
        mean = sum(h) / len(h)
        in_drift = self._in_drift.get(sv, False)
        over = abs(mean) > self._threshold

        def _payload(kind: str) -> dict:
            return {
                'sv': sv,
                'kind': kind,
                'drift_m': mean,
                'threshold_m': self._threshold,
                'n_samples': len(h),
                'window_epochs': self._window,
                'gf_ref_m': ref,
            }

        if not in_drift and over:
            # First trip: enter.
            self._in_drift[sv] = True
            self._enter_count[sv] = self._ingest_count[sv]
            return _payload('enter')
        if in_drift and not over:
            # Recovered: exit.
            self._in_drift[sv] = False
            self._enter_count.pop(sv, None)
            return _payload('exit')
        if (in_drift and over and self._ongoing_period > 0
                and self._ingest_count[sv] >=
                self._enter_count.get(sv, 0) + self._ongoing_period):
            # Sustained-drift heartbeat — emits at fixed period
            # after enter so post-hoc analysis sees the persistence
            # without per-epoch noise.
            self._enter_count[sv] = self._ingest_count[sv]
            return _payload('ongoing')
        return None

    # ── Diagnostics ───────────────────────────────────────────── #

    def n_tracking(self) -> int:
        return len(self._hist)

    def rolling_mean(self, sv: str) -> float | None:
        """Current rolling-mean residual for ``sv`` in metres, or
        ``None`` if untracked or window not yet at ``min_samples``.
        Exposed for tests and engine summary logging."""
        h = self._hist.get(sv)
        if h is None or len(h) < self._min_samples:
            return None
        return sum(h) / len(h)

    def summary(self) -> str:
        return (
            f"gf_drift: tracking {len(self._hist)} SVs "
            f"(window={self._window}ep, threshold=±{self._threshold*100:.1f}cm)"
        )


def gf_phase_m(phi1_cyc: float, phi2_cyc: float,
               wl_f1_m: float, wl_f2_m: float) -> float:
    """Geometry-free phase combination in metres.

    Helper extracted for engine call sites and unit tests.  Inputs
    are L1 and L5 carrier phase in cycles plus their wavelengths in
    metres.
    """
    return phi1_cyc * wl_f1_m - phi2_cyc * wl_f2_m
