"""Engine-side helper for consuming peer state from the PeerBus.

Complement to ``peer_publisher``: where that module emits this
host's state, this one ingests peers' state and keeps a rolling
snapshot per peer.  Pure subscriber + storage; consumers (the
fleet-consensus monitors, to land in Part 2) read ``snapshots()``
and compute their own trip conditions.

Piggybacks on the bus that ``peer_publisher.initialize()`` opened
— no second socket, no second heartbeat thread.  Call
``peer_subscriber.initialize()`` AFTER ``peer_publisher.initialize()``
to wire up subscriptions.

Module-level state mirrors peer_publisher's pattern: one bus,
one set of snapshots, all helpers are one-line no-ops when the
subscriber hasn't been initialized.

Cohort queries return the pure-function results from
``peppar_bus.cohort``, so downstream code doesn't need to
reimplement median / grouping logic.

See ``docs/fleet-consensus-monitors.md`` for the full design.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


# 10 s staleness — matches FleetAggregator's _PEER_STALE_S in
# peppar_mon/fleet.py.  A peer with no recent payload is dropped
# from cohort checks so we don't compute median against last-known
# data from a silently-gone host.
_PEER_STALE_S = 10.0


@dataclass
class PeerSnapshot:
    """Latest-known state of one peer.  Mirrors
    ``peppar_mon.fleet.PeerSnapshot`` shape — the cohort helpers
    in ``peppar_bus.cohort`` duck-type against both.
    """

    host: str
    identity: Optional[object] = None          # PeerIdentity
    last_recv_mono_ns: int = 0
    position: Optional[object] = None          # PositionPayload
    ztd: Optional[object] = None               # ZTDPayload


_snapshots: dict[str, PeerSnapshot] = {}
_lock = threading.Lock()
_active = False
_local_antenna_ref: str = ""
_local_site_ref: str = ""


def initialize(*, antenna_ref: str = "", site_ref: str = "") -> bool:
    """Register subscriptions on the bus that ``peer_publisher``
    opened.  Stores local cohort refs so the ``cohort_*`` helpers
    below can be called from anywhere without re-threading config.

    Returns True when subscriptions are active, False if the bus
    isn't available (peer_publisher never initialized or was
    initialized with --peer-bus none).
    """
    global _active, _local_antenna_ref, _local_site_ref
    import peer_publisher
    bus = peer_publisher.get_bus()
    _local_antenna_ref = antenna_ref or ""
    _local_site_ref = site_ref or ""
    if bus is None:
        log.info("peer_subscriber: bus not active, no subscriptions")
        return False
    bus.subscribe("peppar-fix.*.position", _on_position)
    bus.subscribe("peppar-fix.*.ztd", _on_ztd)
    _active = True
    log.info("peer_subscriber: active, subscribed to position + ztd "
             "(local antenna_ref=%r site_ref=%r)",
             _local_antenna_ref, _local_site_ref)
    return True


def get_local_antenna_ref() -> str:
    return _local_antenna_ref


def get_local_site_ref() -> str:
    return _local_site_ref


def shutdown() -> None:
    """Stop accepting updates.  Existing snapshots drain on
    staleness.  Called during engine shutdown for symmetry with
    ``peer_publisher.shutdown``."""
    global _active
    _active = False
    with _lock:
        _snapshots.clear()


def is_active() -> bool:
    return _active


def _on_position(msg) -> None:
    from peppar_bus import schemas
    try:
        payload = schemas.from_bytes(schemas.PositionPayload, msg.payload)
    except Exception:
        log.exception("peer_subscriber: position decode failed")
        return
    with _lock:
        snap = _snapshots.setdefault(
            msg.from_host, PeerSnapshot(host=msg.from_host),
        )
        snap.position = payload
        snap.last_recv_mono_ns = msg.recv_mono_ns


def _on_ztd(msg) -> None:
    from peppar_bus import schemas
    try:
        payload = schemas.from_bytes(schemas.ZTDPayload, msg.payload)
    except Exception:
        log.exception("peer_subscriber: ztd decode failed")
        return
    with _lock:
        snap = _snapshots.setdefault(
            msg.from_host, PeerSnapshot(host=msg.from_host),
        )
        snap.ztd = payload
        snap.last_recv_mono_ns = msg.recv_mono_ns


def snapshots(*, include_self: bool = False,
              self_snapshot: Optional[PeerSnapshot] = None) -> list[PeerSnapshot]:
    """Return live peer snapshots (stale entries removed).

    Optionally includes a self-snapshot so cohort helpers can
    compute "self vs cohort median" in one pass.  Caller builds
    the self-snapshot from the engine's current state and passes
    it in; this module doesn't have access to the filter.

    Identity is refreshed from the bus's ``peers()`` each call
    so newly-arrived heartbeats repopulate cohort metadata that
    might not have been attached when the first position/ztd
    message arrived.
    """
    if not _active:
        return []
    import peer_publisher
    from peppar_bus import PeerIdentity  # noqa
    bus = peer_publisher.get_bus()
    idents = {p.host: p for p in bus.peers()} if bus is not None else {}
    now_ns = time.monotonic_ns()
    cutoff = now_ns - int(_PEER_STALE_S * 1e9)
    out: list[PeerSnapshot] = []
    with _lock:
        for host, snap in _snapshots.items():
            if snap.last_recv_mono_ns < cutoff:
                continue
            # Identity may arrive after the first state messages,
            # so keep refreshing it from peers() until it lands.
            ident = idents.get(host)
            if ident is not None and snap.identity != ident:
                snap.identity = ident
            out.append(dataclasses.replace(snap))  # shallow copy for reader
    if include_self and self_snapshot is not None:
        out.append(self_snapshot)
    return out


def build_self_snapshot(
    *,
    antenna_ref: str,
    site_ref: str,
    lat_deg: Optional[float] = None,
    lon_deg: Optional[float] = None,
    alt_m: Optional[float] = None,
    ztd_m: Optional[float] = None,
) -> PeerSnapshot:
    """Construct a PeerSnapshot representing this engine's own
    current state, with identity populated from the local
    antenna_ref/site_ref.  Passed to ``snapshots(include_self=True)``
    when the cohort check needs to include this host in the
    median computation — which, for consensus-vs-median, it
    usually does."""
    import peer_publisher
    from peppar_bus import PeerIdentity
    from peppar_bus import schemas
    host = peer_publisher.get_host() or ""
    ident = PeerIdentity(
        host=host, antenna_ref=antenna_ref, site_ref=site_ref,
    )
    pos = None
    if lat_deg is not None and lon_deg is not None and alt_m is not None:
        pos = schemas.PositionPayload(
            lat_deg=lat_deg, lon_deg=lon_deg, alt_m=alt_m,
        )
    ztd = None
    if ztd_m is not None:
        ztd = schemas.ZTDPayload(ztd_m=ztd_m)
    return PeerSnapshot(
        host=host, identity=ident,
        last_recv_mono_ns=time.monotonic_ns(),
        position=pos, ztd=ztd,
    )


# ── Cohort query helpers (wrap the pure functions) ────────── #


def cohort_median_position(
    antenna_ref: str, self_snapshot: Optional[PeerSnapshot] = None,
):
    """Return ``(lat_m, lon_m, alt_m, n)`` median across the
    shared-ARP cohort, or None when fewer than 2 peers in-cohort
    have positions.  See ``peppar_bus.cohort.cohort_median_position``.
    """
    from peppar_bus import cohort
    snaps = snapshots(
        include_self=self_snapshot is not None,
        self_snapshot=self_snapshot,
    )
    return cohort.cohort_median_position(snaps, antenna_ref)


def cohort_median_ztd(
    site_ref: str, self_snapshot: Optional[PeerSnapshot] = None,
):
    """Return ``(ztd_m, n)`` median across the shared-atmosphere
    cohort, or None when fewer than 2 peers in-cohort have ZTDs."""
    from peppar_bus import cohort
    snaps = snapshots(
        include_self=self_snapshot is not None,
        self_snapshot=self_snapshot,
    )
    return cohort.cohort_median_ztd(snaps, site_ref)
