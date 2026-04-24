"""Fleet aggregation for peppar-mon.

When peppar-mon runs in fleet mode it:

1. Tails its own engine log (via the existing LogReader) — produces
   a single-host LogState exactly as the non-fleet monitor does.
2. Publishes its own host's state to a PeerBus (via LogToBusBridge).
3. Subscribes to every peer's state on the same bus (via
   FleetAggregator).
4. Renders both the single-host display AND a fleet summary row.

This module owns (2) and (3).  The single-host side is unchanged.

Fleet summary is computed per tick from the latest-per-peer
snapshots:

- max pairwise position Δ (3D, and horizontal)
- per-host Anchored count across the fleet
- ZTD spread (max - min) across hosts

"Fleet" here means peers on the same PeerBus.  In the lab that's
typically three L5 hosts on UFO1.  Peers are auto-discovered via
heartbeats.
"""

from __future__ import annotations

import dataclasses
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from peppar_bus import (
    PeerBus,
    PeerMessage,
    PeerIdentity,
    schemas,
)


# Peers whose most recent update is older than this are dropped
# from the fleet view.  Match the PeerBus STALE_S so the two
# staleness windows don't fight.
_PEER_STALE_S = 10.0


@dataclass
class PeerSnapshot:
    """Latest-known state of one peer.  Fields fill in as topics
    arrive; a peer that's only sent a heartbeat has all-None
    state."""

    host: str
    identity: Optional[PeerIdentity] = None
    last_recv_mono_ns: int = 0

    position: Optional[schemas.PositionPayload] = None
    ztd: Optional[schemas.ZTDPayload] = None
    sv_state: Optional[schemas.SvStatePayload] = None
    streams: Optional[schemas.StreamsPayload] = None


class FleetAggregator:
    """Subscribes to every peer on a PeerBus and maintains the
    latest snapshot per peer.

    Thread-safe: the bus delivers callbacks on its internal thread;
    this class serializes all mutations via ``_lock``.  Readers
    (e.g. the Textual tick callback) take snapshots without
    blocking the transport.
    """

    def __init__(self, bus: PeerBus) -> None:
        self._bus = bus
        self._snapshots: dict[str, PeerSnapshot] = {}
        self._lock = threading.Lock()
        self._register_subscriptions()

    def _register_subscriptions(self) -> None:
        self._bus.subscribe("peppar-fix.*.position", self._on_position)
        self._bus.subscribe("peppar-fix.*.ztd", self._on_ztd)
        self._bus.subscribe("peppar-fix.*.sv-state", self._on_sv_state)
        self._bus.subscribe("peppar-fix.*.streams", self._on_streams)
        # Heartbeats update PeerBus.peers() directly; we don't need
        # our own handler.  We pick up identity via peers() in
        # snapshot().

    def _on_position(self, msg: PeerMessage) -> None:
        payload = schemas.from_bytes(schemas.PositionPayload, msg.payload)
        with self._lock:
            snap = self._snapshots.setdefault(
                msg.from_host, PeerSnapshot(host=msg.from_host),
            )
            snap.position = payload
            snap.last_recv_mono_ns = msg.recv_mono_ns

    def _on_ztd(self, msg: PeerMessage) -> None:
        payload = schemas.from_bytes(schemas.ZTDPayload, msg.payload)
        with self._lock:
            snap = self._snapshots.setdefault(
                msg.from_host, PeerSnapshot(host=msg.from_host),
            )
            snap.ztd = payload
            snap.last_recv_mono_ns = msg.recv_mono_ns

    def _on_sv_state(self, msg: PeerMessage) -> None:
        payload = schemas.from_bytes(schemas.SvStatePayload, msg.payload)
        with self._lock:
            snap = self._snapshots.setdefault(
                msg.from_host, PeerSnapshot(host=msg.from_host),
            )
            snap.sv_state = payload
            snap.last_recv_mono_ns = msg.recv_mono_ns

    def _on_streams(self, msg: PeerMessage) -> None:
        payload = schemas.from_bytes(schemas.StreamsPayload, msg.payload)
        with self._lock:
            snap = self._snapshots.setdefault(
                msg.from_host, PeerSnapshot(host=msg.from_host),
            )
            snap.streams = payload
            snap.last_recv_mono_ns = msg.recv_mono_ns

    def snapshots(self) -> list[PeerSnapshot]:
        """Return live peer snapshots (stale entries removed).
        Fills in the identity from the bus's peers() call."""
        now_ns = time.monotonic_ns()
        cutoff = now_ns - int(_PEER_STALE_S * 1e9)
        identities = {p.host: p for p in self._bus.peers()}
        with self._lock:
            live: list[PeerSnapshot] = []
            for host, snap in self._snapshots.items():
                if snap.last_recv_mono_ns < cutoff:
                    continue
                # Refresh identity — it may have arrived after
                # the first state messages.
                ident = identities.get(host)
                if ident is not None and snap.identity != ident:
                    snap.identity = ident
                live.append(
                    dataclasses.replace(snap),  # shallow copy for reader safety
                )
            return live


@dataclass
class FleetSummary:
    """Cross-host aggregate computed from per-peer snapshots.

    Only populated when there are ≥ 2 peers with the same
    ``antenna_ref`` *and* position.  Otherwise fields are None and
    the widget renders a "no fleet" row.
    """

    n_hosts: int = 0
    # Max pairwise 3D / horizontal position delta among same-
    # antenna peers, in metres.
    max_delta_3d_m: Optional[float] = None
    max_delta_h_m: Optional[float] = None
    # Per-host Anchored counts, sorted by hostname for stable
    # display.  List of (host, count).
    anchored_per_host: list[tuple[str, int]] = field(default_factory=list)
    # ZTD spread (max - min) in mm across same-antenna peers.
    ztd_spread_mm: Optional[float] = None


def compute_summary(snapshots: list[PeerSnapshot]) -> FleetSummary:
    """Pure function: given a peer-snapshot list, produce a
    FleetSummary.  Kept pure so it's unit-testable without spinning
    up a real bus.
    """
    summary = FleetSummary(n_hosts=len(snapshots))
    if len(snapshots) < 2:
        return summary
    # Shared-antenna cohort: peers whose identity.antenna_ref all
    # match (and are non-empty).  On the lab L5 fleet this is all
    # three hosts.  If the fleet is heterogeneous (two antennas in
    # one multicast domain) we only cross-compare within the
    # largest same-antenna group.
    antennas = {}
    for s in snapshots:
        if s.identity is None or not s.identity.antenna_ref:
            continue
        antennas.setdefault(s.identity.antenna_ref, []).append(s)
    if antennas:
        cohort = max(antennas.values(), key=len)
    else:
        cohort = snapshots  # best-effort: compare everything
    # Position deltas (3D and horizontal) — pairwise max
    positions: list[tuple[str, float, float, float]] = []
    for s in cohort:
        p = s.position
        if p is None or p.lat_deg is None or p.lon_deg is None or p.alt_m is None:
            continue
        # Convert to local ENU-ish via flat-earth approximation —
        # at the lab this is valid to mm at intra-fleet scale.
        lat = p.lat_deg
        lon = p.lon_deg
        alt = p.alt_m
        positions.append((s.host, lat, lon, alt))
    if len(positions) >= 2:
        max_3d = 0.0
        max_h = 0.0
        for i in range(len(positions)):
            for j in range(i + 1, len(positions)):
                _, lat_i, lon_i, alt_i = positions[i]
                _, lat_j, lon_j, alt_j = positions[j]
                # 1° lat ≈ 111_320 m; 1° lon ≈ 111_320 × cos(lat) m
                d_lat = (lat_i - lat_j) * 111_320.0
                d_lon = (lon_i - lon_j) * 111_320.0 * math.cos(
                    math.radians((lat_i + lat_j) / 2),
                )
                d_alt = alt_i - alt_j
                d_h = math.hypot(d_lat, d_lon)
                d_3d = math.sqrt(d_h * d_h + d_alt * d_alt)
                if d_3d > max_3d:
                    max_3d = d_3d
                if d_h > max_h:
                    max_h = d_h
        summary.max_delta_3d_m = max_3d
        summary.max_delta_h_m = max_h
    # Anchored counts per host
    counts: list[tuple[str, int]] = []
    for s in sorted(cohort, key=lambda s: s.host):
        if s.sv_state is None:
            continue
        n_anch = sum(1 for v in s.sv_state.sv_states.values() if v == "ANCHORED")
        counts.append((s.host, n_anch))
    summary.anchored_per_host = counts
    # ZTD spread
    ztds = [s.ztd.ztd_m for s in cohort
            if s.ztd is not None and s.ztd.ztd_m is not None]
    if len(ztds) >= 2:
        summary.ztd_spread_mm = (max(ztds) - min(ztds)) * 1000.0
    return summary
