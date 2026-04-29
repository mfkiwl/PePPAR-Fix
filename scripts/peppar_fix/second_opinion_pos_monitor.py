"""SecondOpinionPosMonitor — independent-witness check on PPP-AR position.

Per dayplan I-011533-main / 2026-04-28 evening: the existing per-epoch
``_check_nav2`` watchdog in the engine is **horizontal-only by design**
(altitude-based RESETs cascaded on clkPoC3 in earlier work, destroying
convergence).  That deliberate narrowing leaves a gap: an internally-
consistent wrong-integer commitment that absorbs error into altitude
flies under every existing trigger.  Tonight (2026-04-28) the fleet
demonstrated the gap — three hosts at sub-cm σ, ZTD residuals of +2 m,
altitudes off by 3 / 9 / 14 m, none of the FixSetIntegrityAlarm or
horizontal-only NAV2 watchdog triggers caught it.

This monitor watches the **3D** displacement between the engine's
PPP-AR position and NAV2's independent solution.  NAV2 doesn't share
PPP-AR's wrong-integer failure modes, so a sustained 3D nav2Δ above
threshold is a strong signal that the filter has locked into a wrong
basin.

Naming: kept distinct from FixSetIntegrityAlarm (which checks internal
consistency — residual RMS, ZTD bounds, anchor count, peer-cohort).
"Second opinion" captures the independent-witness semantics — not
"absolute" (NAV2 is not absolute truth, just independent).

The reaction is a full re-init at NAV2's LLA — drop NL fixes, reset MW,
reseed lat/lon/alt all together.  This is what the horizontal-only
``_check_nav2`` does too; the only difference is what we're willing to
let trip the gate.  With the GF_STEP / IF_STEP eviction monitors now
catching most spurious wrong-integer commits, the historical concern
(altitude-based reset cascade) is mitigated: the cohort-median demoters
won't allow a fresh basin to lock onto the same wrong altitude.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class SecondOpinionPosMonitor:
    """Per-host check: 3D PPP-AR position vs. NAV2 opinion.

    Usage::

        m = SecondOpinionPosMonitor()
        # When a fresh NAV2 opinion is available:
        ev = m.evaluate(epoch=N, nav2_delta_3d_m=disp_3d)
        if ev:
            # caller does the full re-init at NAV2 LLA

    ``evaluate`` returns ``None`` when the gate is below threshold or
    not yet sustained.  When the streak hits ``sustained_epochs`` and
    the disagreement is sustained, returns an event dict::

        {
          'reason': 'second_opinion_3d',
          'nav2_delta_3d_m': float,
          'threshold_m': float,
          'sustained_epochs': int,
        }

    After a caller acts on an event, call ``note_recovery()`` (or wait
    for the next nav2Δ to drop below threshold — auto-resets).
    """

    def __init__(
        self,
        threshold_m: float = 5.0,
        sustained_epochs: int = 30,
    ) -> None:
        # 5 m on 3D displacement.  The known NAV2 east-bias on shared
        # antenna is ~4 m horizontal; 5 m on 3D allows that as noise
        # while still catching the kind of altitude-trapped lock we
        # observed on 2026-04-28 (3-14 m vertical errors).
        self._threshold = float(threshold_m)
        # 30 epochs ≈ 30 s at 1 Hz.  Long enough to suppress NAV2
        # short-term transients (multipath flicker, momentary geometry
        # change), short enough that an actual wrong-basin lock fires
        # before ZTD is too corrupt to recover from.
        self._sustained = int(sustained_epochs)
        self._streak = 0
        # Latch — only fire once per period of sustained disagreement.
        # Caller's reset action will clear by calling note_recovery,
        # or we auto-clear when the gate drops below threshold.
        self._fired_this_period = False
        self._last_event_epoch: int | None = None

    def evaluate(self, epoch: int, nav2_delta_3d_m: float | None) -> dict | None:
        if nav2_delta_3d_m is None:
            # No NAV2 opinion this epoch — don't reset the streak,
            # NAV2 store typically has 30 s freshness window.
            return None
        if abs(nav2_delta_3d_m) <= self._threshold:
            if self._streak > 0:
                log.info(
                    "[SECOND_OPINION_POS] gate cleared: nav2Δ=%.2fm ≤ %.2fm "
                    "(was streak=%d/%d)",
                    nav2_delta_3d_m, self._threshold,
                    self._streak, self._sustained,
                )
            self._streak = 0
            self._fired_this_period = False
            return None
        self._streak += 1
        if self._streak < self._sustained:
            return None
        if self._fired_this_period:
            return None
        self._fired_this_period = True
        self._last_event_epoch = epoch
        return {
            'reason': 'second_opinion_3d',
            'nav2_delta_3d_m': float(nav2_delta_3d_m),
            'threshold_m': self._threshold,
            'sustained_epochs': self._streak,
        }

    def note_recovery(self) -> None:
        """Caller signals after re-init so we can fire again on next divergence."""
        self._streak = 0
        self._fired_this_period = False

    # ── Diagnostics ─────────────────────────────────────────────── #

    def streak(self) -> int:
        return self._streak

    def is_armed(self) -> bool:
        """True if currently above threshold but not yet at sustained count."""
        return self._streak > 0 and not self._fired_this_period

    def summary(self) -> str:
        return (
            f"second_opinion_pos: threshold=±{self._threshold:.1f}m "
            f"sustained={self._sustained}ep streak={self._streak} "
            f"fired_this_period={self._fired_this_period}"
        )
