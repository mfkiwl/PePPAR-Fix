"""Elevation-gated WL re-admission — Layer 3 of sv-trust-layers.md.

When the WL drift monitor flushes an SV, its WL integer commitment
was demonstrated wrong — but the physical cause (multipath
signature at a specific elev/az, TEC gradient at current geometry,
receiver-side noise spike) often persists.  Allowing the MW
tracker to immediately re-admit the SV typically produces the
same wrong integer again because the geometry hasn't changed.

This gate blocks MW updates for a flushed SV until it has moved
at least ``min_elev_delta_deg`` in elevation (up or down) from
its state at flush.  Breaks the "re-fix the same wrong integer
at the same geometry" pattern.

The gate is **stateless between ``note_flush`` and
``note_admitted``** beyond the flush-time elevation.  It is not a
timer; it is a geometry gate.  An SV that stops moving
(geostationary or in eclipse near zenith) never releases, which
is the right behavior — if nothing about the SV has changed, our
prior evidence that this SV commits wrong integers is still
current.

Interaction with Layer 1 (per-SV weighting, docs/future-work):
when proactive weighting lands, this gate becomes a binary special
case of the weight function — "weight = 0 until elevation Δ ≥ 2°,
then recovers."  Today's code is the stepping stone.

Usage pattern in the engine loop:

    gate = WlReAdmissionGate()

    # After WL drift monitor flushes SV:
    gate.note_flush(sv, elevations.get(sv))

    # Before calling mw.update() for each observation:
    for obs in observations:
        elev = elevations.get(obs['sv'])
        if gate.is_blocked(obs['sv'], elev):
            continue   # skip MW update — SV held by gate
        mw.update(...)

    # After mw.update may have fixed — if SV is now fixed, gate
    # has nothing more to do for this SV:
    for sv in newly_fixed:
        gate.note_admitted(sv)

Not thread-safe.  Call from the AntPosEst thread only (matches
the other monitors).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class WlReAdmissionGate:
    """Record flush-time elevation; block re-admission until the SV
    has moved enough in elevation to be considered a new opportunity.

    Parameters:
        min_elev_delta_deg: minimum elevation change (signed abs value,
            so a setting SV can also release by dropping) required
            before MW update is permitted again.  2° at typical
            elevation-rate means ~1–2 hours of wait — long enough
            for TEC gradients to shift, short enough that a pass
            at moderate elevation will eventually release.
    """

    def __init__(self, min_elev_delta_deg: float = 2.0) -> None:
        self._min_delta = float(min_elev_delta_deg)
        # sv → elevation at flush time.  Absent → no hold.
        self._flush_elev: dict[str, float] = {}

    # ── Lifecycle ───────────────────────────────────────────────── #

    def note_flush(self, sv: str, elev_deg: float | None) -> None:
        """Record the SV's elevation at the time its WL fix was
        flushed.  The gate blocks re-admission until the SV moves
        at least ``min_elev_delta_deg`` from this elevation.

        If ``elev_deg`` is ``None``, the gate does NOT take a hold
        on this SV (we fail safe — no elevation data means no
        basis for the gate).  Clears any prior hold for the SV.
        """
        if elev_deg is None:
            self._flush_elev.pop(sv, None)
            return
        self._flush_elev[sv] = float(elev_deg)

    def note_admitted(self, sv: str) -> None:
        """Called when the SV has been successfully re-admitted
        (MW re-fixed with the gate's permission).  Clears the
        flush-elev hold so subsequent drift-and-recover cycles
        start fresh.  Idempotent for SVs not currently held."""
        self._flush_elev.pop(sv, None)

    # ── Query ────────────────────────────────────────────────────── #

    def is_blocked(
        self,
        sv: str,
        current_elev_deg: float | None,
    ) -> bool:
        """Return True if this SV is currently held by the gate —
        caller should skip MW update for this SV.

        SVs with no flush record return False (gate has no hold).
        SVs with a flush record but no current elevation return
        False (fail safe — no data to check against).  Otherwise
        True iff the elevation delta from flush time is less than
        ``min_elev_delta_deg``.
        """
        flush_elev = self._flush_elev.get(sv)
        if flush_elev is None:
            return False
        if current_elev_deg is None:
            return False
        delta = abs(float(current_elev_deg) - flush_elev)
        return delta < self._min_delta

    # ── Diagnostics ─────────────────────────────────────────────── #

    def n_held(self) -> int:
        return len(self._flush_elev)

    def held_svs(self) -> list[str]:
        return list(self._flush_elev.keys())

    def flush_elev(self, sv: str) -> float | None:
        return self._flush_elev.get(sv)

    def summary(self) -> str:
        return (
            f"wl_readmit: holding {len(self._flush_elev)} SVs "
            f"(threshold Δelev ≥ {self._min_delta:.1f}°)"
        )
