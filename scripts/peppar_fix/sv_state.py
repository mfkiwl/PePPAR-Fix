"""Per-SV integer-fix lifecycle state machine.

Implements the per-SV state machine specified in
`docs/sv-lifecycle-and-pfr-split.md`.  Orthogonal to the AntPosEst
solution state; answers "does this specific satellite's integer
ambiguity contribute to the fix set?" rather than "is the antenna
position trustworthy?".

Six states form this lifecycle (participle rule: present participle
names active work, past participle names a terminal milestone):

    TRACKING ──admit──► FLOATING ──MW──► CONVERGING ──LAMBDA──► ANCHORING ──Δaz≥8°──► ANCHORED
                         ▲                ▲                     │ ▲                        │
                         │                │                     │ │  false-fix rejection   │
                         │                │                     │ │  or setting-SV drop    │
                         │                │                     │ │  or slip (LOW)         │
                         │                │ slip (LOW)          │ └────────────────────────┘
                         │                ◄─────────────────────┤
                         │                                      ▼
                         │               slip (HIGH)   ┌──────────────┐  cooldown expires
                         └───────────────────◄─────────┤   WAITING    ├────────► FLOATING
                                                       └──────────────┘

Key names:
  * TRACKING   — receiver sees the SV but no observations have yet
                 passed the admit gate (elevation + health + constellation).
  * FLOATING   — admitted; MW tracker accumulating; no integer fix.
  * CONVERGING — integer wide-lane fix landed; narrow-lane still float.
  * ANCHORING  — integer NL fix accepted; this SV is earning the
                 ≥ 8° Δaz geometry validation needed to promote it
                 to an anchor.  Short-term member of the fix set;
                 contributes to the position solution but its NL
                 integer isn't geometry-validated yet.
  * ANCHORED   — integer NL fix has survived ≥ 8° of satellite
                 azimuth motion without a false-fix rejection.
                 Long-term (anchored) member of the fix set;
                 counts toward ANCHORED state at the filter level.
  * WAITING    — temporarily excluded from fix attempts after a
                 high-confidence cycle slip; cooldown-bound,
                 returns to FLOATING on expiry.  Not permanent.

The SV-level participles mirror the filter-level progression in
``AntPosEstState`` (SURVEYING → VERIFYING → CONVERGING → ANCHORING
→ ANCHORED) — one SV is to the filter what one worker is to a
pipeline.  Both share the same verbs.

Transitions not in this diagram are illegal and raise
`InvalidTransition`.  The tracker logs every legal transition as
one `[SV_STATE] <sv>: <from> → <to> (epoch=N, elev=X°, reason=...)`
line — grep-friendly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)


class SvAmbState(Enum):
    """Per-SV integer-fix state — one value per (system, PRN)."""
    TRACKING = "TRACKING"
    FLOATING = "FLOATING"
    CONVERGING = "CONVERGING"
    ANCHORING = "ANCHORING"
    ANCHORED = "ANCHORED"
    WAITING = "WAITING"


# Legal directed edges.  Built once at import; `transition()` consults it.
_LEGAL_EDGES: frozenset[tuple[SvAmbState, SvAmbState]] = frozenset({
    # Admit: receiver-tracked SV passes the elevation/health/constellation
    # gate and enters processing.
    (SvAmbState.TRACKING,    SvAmbState.FLOATING),

    # Normal progression.
    (SvAmbState.FLOATING,    SvAmbState.CONVERGING),
    (SvAmbState.CONVERGING,  SvAmbState.ANCHORING),
    (SvAmbState.ANCHORING,   SvAmbState.ANCHORED),

    # False-fix rejection, setting-SV drop, or slip (LOW-confidence)
    # demotes back toward FLOATING.
    (SvAmbState.CONVERGING,  SvAmbState.FLOATING),
    (SvAmbState.ANCHORING,   SvAmbState.FLOATING),
    (SvAmbState.ANCHORED,    SvAmbState.FLOATING),

    # WAITING (high-confidence cycle slip) from any processing state.
    # TRACKING is pre-processing, so no WAITING edge from there — a
    # slip before admit is not meaningful (no integer state to protect).
    (SvAmbState.FLOATING,    SvAmbState.WAITING),
    (SvAmbState.CONVERGING,  SvAmbState.WAITING),
    (SvAmbState.ANCHORING,   SvAmbState.WAITING),
    (SvAmbState.ANCHORED,    SvAmbState.WAITING),

    # WAITING cooldown expires: re-enter at FLOATING (MW must reconverge).
    (SvAmbState.WAITING,     SvAmbState.FLOATING),
})


class InvalidTransition(ValueError):
    """Raised when a requested transition isn't in the state-machine."""


@dataclass
class SvRecord:
    """Per-SV state-machine record.

    Kept minimal — anything that belongs elsewhere (MW state, NL integer,
    filter ambiguity slot) stays there.  This record carries only what
    the state machine itself needs plus enough context for log lines.
    """
    sv: str
    state: SvAmbState = SvAmbState.TRACKING
    # Epoch at which the current state was entered.  Used by the false-fix
    # monitor to scope "recent fix" residual windows.
    state_entered_epoch: int = 0
    # Azimuth at first NL fix.  The anchoring-SV promoter diffs current
    # az against this to promote ANCHORING → ANCHORED.  Populated when
    # transitioning into ANCHORING; None otherwise.
    first_fix_az_deg: Optional[float] = None
    # Most recent observed elevation.  Updated externally (the monitors
    # already have `elevations` per epoch).  Used for log lines and for
    # the setting-SV drop monitor.
    last_elev_deg: Optional[float] = None
    # Short history of (epoch, from_state, to_state, reason) — cap so
    # memory doesn't grow unbounded on long runs.  Useful for offline
    # forensics of cascading transitions.
    history: list[tuple[int, SvAmbState, SvAmbState, str]] = field(default_factory=list)
    # Epoch at which this SV's current WAITING state expires.  None
    # when not waiting.  The tracker's check_squelch_cooldowns()
    # sweeps records and transitions expired ones to FLOATING.
    squelch_expires_epoch: Optional[int] = None
    # Count of unexpected false-fix events this arc (events at
    # elev ≥ reliable_elev_deg, defined in the false-fix monitor).
    # Drives the false-fix monitor's cooldown escalation: 1st → short,
    # 2nd → longer, 3rd+ → effectively rest-of-arc.  Resets when the
    # record is forgotten (tracking loss).
    unexpected_ff_this_arc: int = 0
    # Last epoch at which this SV was observed (MW update or any
    # external observation).  Engine uses this to forget records
    # after an arc-gap threshold, which implicitly resets the
    # unexpected_ff_this_arc counter.
    last_seen_epoch: int = 0

    _HISTORY_CAP: int = 16


class SvStateTracker:
    """Maintains one SvRecord per (system, PRN).

    Threading model: the tracker is *not* thread-safe.  Call from the
    AntPosEst / AR thread only.  The monitors (false-fix monitor,
    setting-SV drop monitor, fix-set integrity alarm) and the AR paths
    (MW, NL, slip) all run in that single thread.

    The tracker is authoritative for state transitions — it refuses
    illegal moves.  Callers pass a free-form `reason` that lands in
    the log line and in the history record.
    """

    def __init__(self, wl_only: bool = False):
        self._records: dict[str, SvRecord] = {}
        # WL-only mode: clamp per-SV lifecycle at CONVERGING.  Any
        # promotion to ANCHORING / ANCHORED is silently refused
        # (logged at INFO, returns without mutation).  See
        # docs/wl-only-foundation.md.  Default False preserves the
        # full lifecycle.
        self._wl_only = bool(wl_only)

    # ── Lookup / iteration ──────────────────────────────────────── #

    def get(self, sv: str) -> SvRecord:
        """Return the record for this SV, creating a TRACKING record if new."""
        rec = self._records.get(sv)
        if rec is None:
            rec = SvRecord(sv=sv)
            self._records[sv] = rec
        return rec

    def state(self, sv: str) -> SvAmbState:
        """Current state of this SV (creates TRACKING record on first query)."""
        return self.get(sv).state

    def svs_in(self, *states: SvAmbState) -> list[str]:
        """List of SVs currently in any of the given states."""
        wanted = set(states)
        return [sv for sv, rec in self._records.items() if rec.state in wanted]

    def count_in(self, *states: SvAmbState) -> int:
        return sum(1 for rec in self._records.values() if rec.state in set(states))

    def all_records(self):
        """Iterable view of (sv, record) pairs — read-only intent."""
        return self._records.items()

    # ── Fix-set membership helpers ───────────────────────────────── #

    def anchoring_svs(self) -> list[str]:
        """SVs with an integer fix that haven't yet been geometry-
        validated — short-term (anchoring) members of the fix
        set.  SvAmbState.ANCHORING."""
        return self.svs_in(SvAmbState.ANCHORING)

    def anchored_svs(self) -> list[str]:
        """SVs whose integer fix has survived ≥ 8° Δaz — long-term
        (anchored) members of the fix set.  SvAmbState.ANCHORED."""
        return self.svs_in(SvAmbState.ANCHORED)

    def fix_set_members(self) -> list[str]:
        """All SVs currently contributing an integer fix to the
        position solution (union of ANCHORING + ANCHORED)."""
        return self.svs_in(SvAmbState.ANCHORING, SvAmbState.ANCHORED)

    # ── State mutations ─────────────────────────────────────────── #

    def transition(
        self,
        sv: str,
        to: SvAmbState,
        *,
        epoch: int,
        reason: str = "",
        az_deg: Optional[float] = None,
        elev_deg: Optional[float] = None,
        cooldown_epochs: Optional[int] = None,
    ) -> None:
        """Move SV into `to` state, enforcing the legal-edge set.

        Idempotent for self-transitions (caller may re-declare a state
        without triggering a log line or history entry).  Illegal moves
        raise InvalidTransition — the caller decides whether to catch
        or let it crash tests.

        When `to` is WAITING, `cooldown_epochs` sets the per-SV
        duration until `check_squelch_cooldowns` transitions it back
        to FLOATING.  Callers that don't pass this value get the
        legacy 60-epoch default.  Logged in the reason frag as
        `squelch=<N>s`.
        """
        rec = self.get(sv)
        if elev_deg is not None:
            rec.last_elev_deg = float(elev_deg)
        if rec.state == to:
            return
        # WL-only clamp: silently refuse promotion into the NL-fixed
        # states.  The resolver is separately gated off in WL-only
        # mode so this branch should not normally fire; the clamp is
        # belt-and-suspenders for any residual caller (monitors,
        # tests) that tries to push past the WL terminus.
        if self._wl_only and to in (SvAmbState.ANCHORING, SvAmbState.ANCHORED):
            log.info(
                "[WL-ONLY] refusing %s: %s → %s (wl_only gate, reason=%s)",
                sv, rec.state.value, to.value, reason or "?",
            )
            return
        edge = (rec.state, to)
        if edge not in _LEGAL_EDGES:
            raise InvalidTransition(
                f"{sv}: illegal transition {rec.state.value} → {to.value}"
                f" (epoch={epoch}, reason={reason or '?'})"
            )
        from_state = rec.state
        rec.state = to
        rec.state_entered_epoch = int(epoch)
        if to is SvAmbState.ANCHORING and az_deg is not None:
            rec.first_fix_az_deg = float(az_deg)
        # Per-SV WAITING cooldown: set the expiry epoch on entry to
        # WAITING; clear it on any other transition.
        if to is SvAmbState.WAITING:
            dur = int(cooldown_epochs) if cooldown_epochs is not None else 60
            rec.squelch_expires_epoch = int(epoch) + dur
        else:
            rec.squelch_expires_epoch = None
        if len(rec.history) >= rec._HISTORY_CAP:
            rec.history.pop(0)
        rec.history.append((int(epoch), from_state, to, reason))
        elev_frag = (
            f"elev={rec.last_elev_deg:.0f}°" if rec.last_elev_deg is not None else "elev=?"
        )
        dur_frag = ""
        if to is SvAmbState.WAITING and cooldown_epochs is not None:
            dur_frag = f", squelch={int(cooldown_epochs)}s"
        # peppar-mon contract: peppar_mon/log_reader.py:_SV_STATE_LINE_RE
        # parses this format.  The synthetic ``→ SET`` variant emitted
        # from peppar_fix_engine.py (stale-obs sweep) is matched by the
        # same regex; peppar-mon treats SET as removal from sv_states.
        # Keep the prefix, sv-id format, and arrow stable.
        log.info(
            "[SV_STATE] %s: %s → %s (epoch=%d, %s%s, reason=%s)",
            sv, from_state.value, to.value, epoch, elev_frag,
            dur_frag, reason or "?",
        )

    def check_squelch_cooldowns(self, epoch: int) -> list[str]:
        """Sweep WAITING records; transition those whose cooldown
        has expired back to FLOATING.

        Returns the list of SVs that transitioned.  Called each eval
        cycle from the engine main loop.
        """
        recovered: list[str] = []
        for sv, rec in list(self._records.items()):
            if rec.state is not SvAmbState.WAITING:
                continue
            if rec.squelch_expires_epoch is None:
                continue
            if epoch < rec.squelch_expires_epoch:
                continue
            try:
                self.transition(sv, SvAmbState.FLOATING,
                                epoch=epoch, reason="squelch cooldown expired")
                recovered.append(sv)
            except InvalidTransition:
                pass
        return recovered

    def mark_seen(self, sv: str, epoch: int) -> None:
        """Record that the receiver observed this SV at `epoch`.

        Engine calls this from the observation ingest path.  Used by
        `forget_stale(...)` to identify records whose SVs haven't been
        seen in a while (arc-gap → drop the record so the next arc
        starts with a zeroed unexpected-false-fix counter).
        """
        rec = self.get(sv)
        rec.last_seen_epoch = int(epoch)

    def forget_stale(self, epoch: int, stale_after_epochs: int) -> list[str]:
        """Forget records whose `last_seen_epoch` is older than
        `stale_after_epochs` ago.

        Returns the list of forgotten SVs.  Implements arc-boundary
        detection: when an SV hasn't been observed for long enough
        that it's probably set, drop its record so the next arc
        starts clean.

        Callers that need the prior state (to emit a transition log
        line on drop) should use ``forget_stale_with_states``.
        """
        return [sv for sv, _ in self.forget_stale_with_states(
            epoch, stale_after_epochs)]

    def forget_stale_with_states(self, epoch: int,
                                 stale_after_epochs: int
                                 ) -> list[tuple[str, SvAmbState]]:
        """Same as ``forget_stale`` but returns ``(sv, prev_state)`` pairs.

        Lets the engine emit a synthetic ``→ SET`` transition log line
        per dropped SV so peppar-mon (and any other current-state
        viewer) can remove the SV from its display.  Without this,
        SVs that physically set out of the sky stay visible in
        downstream tools forever in their last-observed state.
        """
        stale_cutoff = int(epoch) - int(stale_after_epochs)
        dropped: list[tuple[str, SvAmbState]] = []
        for sv, rec in list(self._records.items()):
            # Records with last_seen_epoch == 0 were never observed
            # (TRACKING records created by monitor lookups).  Leave them.
            if rec.last_seen_epoch <= 0:
                continue
            if rec.last_seen_epoch < stale_cutoff:
                dropped.append((sv, rec.state))
                self._records.pop(sv, None)
        return dropped

    def update_elev(self, sv: str, elev_deg: float) -> None:
        """Record the most recent elevation for this SV.

        Separate entry point so monitors that see elevations every epoch
        can stream them in without synthesizing fake state changes.
        """
        self.get(sv).last_elev_deg = float(elev_deg)

    def forget(self, sv: str) -> None:
        """Drop this SV's record entirely.

        Used when the receiver loses tracking for long enough that
        re-initialising state on re-acquisition is cleaner than resuming
        from stale history.  Cycle slips should use
        `transition(..., to=FLOATING)` (LOW-conf) or
        `transition(..., to=WAITING)` (HIGH-conf) instead; `forget`
        is for true disappearance.
        """
        self._records.pop(sv, None)

    # ── Diagnostics ─────────────────────────────────────────────── #

    def summary(self) -> str:
        """One-line state-count summary for periodic logging."""
        counts = {st: 0 for st in SvAmbState}
        for rec in self._records.values():
            counts[rec.state] += 1
        parts = [f"{st.value}={counts[st]}" for st in SvAmbState if counts[st]]
        return "sv_state: " + (" ".join(parts) if parts else "empty")
