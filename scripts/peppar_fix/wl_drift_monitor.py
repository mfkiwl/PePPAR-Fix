"""Per-SV WL integer-consistency tracker.

⚠ **Misnomer warning** (logged 2026-04-28 in
``docs/misnomers.md``).  Despite the historical name, this class no
longer watches drift.  The MW-residual rolling-mean monitor that
this class used to host was empirically uncorrelated with real slip
events (Z = −0.17 vs BNC AMB integer jumps, p = 0.86 — see
``project_wl_drift_smooth_float_signal_20260428``) and was retired
2026-04-29 (I-202241).  The GF step demoter (``GfStepMonitor``) is
the canonical post-fix integrity gate now.

What this class still does: track the last K_short n_wl values
committed for each SV across re-fix cycles, and classify each SV as
HIGH / MEDIUM / LOW / UNKNOWN consistency on that history.  Output
feeds ``[WL_FIX_LIFE]`` log lines for downstream analysis.  Bob's
thought experiment: a HIGH-consistency SV always re-fixes to the
same integer; a LOW-consistency SV wanders.

A rename to ``IntegerConsistencyTracker`` is queued (option ``a`` of
the I-202241 split: signal-removal first, naming-honesty rename in a
follow-up PR).

Usage pattern:

    monitor = WlDriftMonitor()
    fixed_now = {sv for sv, s in mw._state.items() if s.get('fixed')}
    for sv in fixed_now - prev_fixed:
        monitor.note_fix(sv, n_wl=...)
    for sv in prev_fixed - fixed_now:
        monitor.note_unfix(sv)
    # Read consistency_level / integer_history for [WL_FIX_LIFE] logs.
    prev_fixed = fixed_now

Not thread-safe.  Call from the AntPosEst thread only (matches the
other monitors' threading model).
"""

from __future__ import annotations

import logging
from collections import deque

log = logging.getLogger(__name__)


# WL integer consistency levels — measure how reproducibly an SV's WL
# integer commitment lands at the same value across re-fix cycles.
# Bob's thought experiment: if a HIGH-consistency SV is forced out, on
# re-fix it consistently picks the same integer; LOW does not.
#
# String constants (not Enum) to keep the log line format stable.
CONS_HIGH = "HIGH"      # n_wl history range = 0 across last K_short cycles
CONS_MEDIUM = "MEDIUM"  # range = 1 (legitimate adjacent-integer boundary)
CONS_LOW = "LOW"        # range >= 2 (wandering — wrong integer each cycle)
CONS_UNKNOWN = "UNKNOWN"  # < K_short cycles observed; default to LOW behavior


class WlDriftMonitor:
    """Per-SV WL integer consistency tracker."""

    def __init__(self, k_short: int = 4) -> None:
        # Number of fix cycles to remember per SV for consistency
        # classification.  Too small → unstable label flapping; too
        # large → slow to recognise an SV that recovered after a real
        # slip.  4 picked as compromise.
        self._k_short = int(k_short)
        # sv → deque of last K_short n_wl values committed for this SV.
        # Persists across note_fix/note_unfix cycles so consistency
        # classification has memory.  Cleared only by explicit
        # forget_history() (e.g. after a confirmed real cycle slip).
        self._int_history: dict[str, deque[int]] = {}

    # ── Lifecycle ───────────────────────────────────────────────── #

    def note_fix(self, sv: str, n_wl: int | None = None) -> None:
        """Record a WL integer commit for ``sv``.  Pass ``n_wl`` (the
        integer committed at this fix) to feed consistency
        classification.  Idempotent over no-op when ``n_wl`` is None."""
        if n_wl is not None:
            hist = self._int_history.setdefault(
                sv, deque(maxlen=self._k_short)
            )
            hist.append(int(n_wl))

    def note_unfix(self, sv: str) -> None:
        """Lifecycle hook called on WL un-fix.  The per-SV integer
        history is **preserved** so re-fix sees the previous integer
        commitments and can classify consistency.  Use
        ``forget_history(sv)`` after a confirmed real cycle slip to
        wipe the history (so the SV starts fresh)."""
        # No transient state to clear — kept for API stability while
        # callers are migrated.

    def forget_history(self, sv: str) -> None:
        """Wipe the per-SV integer history.  Call only after a
        confirmed real cycle slip (LLI / GF jump / arc gap) so the
        next fix is classified from scratch.  Routine demotions should
        NOT call this — the whole point is that consistency tracking
        persists across re-fixes."""
        self._int_history.pop(sv, None)

    # ── Consistency classification ─────────────────────────────── #

    def consistency_level(self, sv: str) -> str:
        """Per-SV WL integer consistency over the last K_short fix
        cycles.  Returns one of ``CONS_HIGH | CONS_MEDIUM | CONS_LOW |
        CONS_UNKNOWN``.

        Classification rule on the per-SV integer history deque:
          range = 0  → HIGH    (always the same integer)
          range = 1  → MEDIUM  (adjacent-integer boundary case)
          range >= 2 → LOW     (wandering — wrong integer each cycle)
          < 2 cycles → UNKNOWN (defaults to LOW behavior — conservative)
        """
        hist = self._int_history.get(sv)
        if hist is None or len(hist) < 2:
            return CONS_UNKNOWN
        rng = max(hist) - min(hist)
        if rng == 0:
            return CONS_HIGH
        if rng == 1:
            return CONS_MEDIUM
        return CONS_LOW

    def integer_history(self, sv: str) -> list[int]:
        """Read-only snapshot of the last K_short n_wl values
        committed for ``sv``, oldest first.  Empty list if no
        history."""
        hist = self._int_history.get(sv)
        return list(hist) if hist else []

    # ── Diagnostics ─────────────────────────────────────────────── #

    def summary(self) -> str:
        return (
            f"wl_drift: tracking {len(self._int_history)} SVs "
            f"(k_short={self._k_short})"
        )
