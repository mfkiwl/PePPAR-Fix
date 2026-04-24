"""Engine-side helper for publishing state to a PeerBus.

Lives in ``scripts/`` so engine modules can ``from peer_publisher
import publish_position`` without threading a bus instance through
every constructor.  Module-level singleton: the bus is installed
once at engine startup via ``initialize()``; every publish call
after that is a one-line no-op if the bus isn't configured.

This keeps Phase 2a engine changes minimal — each call site adds
one line, not a constructor argument chain.  When/if the engine
grows a more general event-bus pattern, this helper can be
reimplemented on top of that without changing callers.

Transport selection happens in ``initialize()``; each mode parses
its own argument string:

    --peer-bus none                      → disabled (default)
    --peer-bus udp-multicast             → default group / port
    --peer-bus udp-multicast:GROUP:PORT  → override group / port

Future modes: ``tcp:host1,host2`` / ``mqtt:broker:1883``.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

_bus = None         # type: object  (PeerBus or None)
_host = None        # str: this engine's published host identifier


def initialize(spec: str, *, host: str, antenna_ref: str = "",
               version: str = "engine") -> bool:
    """Initialize the peer-bus from a CLI flag spec string.

    Returns True when the bus is active, False when disabled.
    Errors during initialization are logged and degrade to disabled
    — a peer-bus misconfiguration must not take the engine down.
    """
    global _bus, _host
    spec = (spec or "").strip().lower()
    if spec in ("", "none"):
        _bus = None
        _host = None
        return False
    try:
        from peppar_bus import PeerIdentity, UDPMulticastBus
    except ImportError as e:
        log.error("peer-bus requested (%r) but peppar_bus import "
                  "failed: %s", spec, e)
        return False
    try:
        if spec.startswith("udp-multicast"):
            parts = spec.split(":", 2)  # ['udp-multicast', 'GROUP', 'PORT']
            kwargs = {}
            if len(parts) >= 2 and parts[1]:
                kwargs["group"] = parts[1]
            if len(parts) >= 3 and parts[2]:
                kwargs["port"] = int(parts[2])
            identity = PeerIdentity(
                host=host, version=version, antenna_ref=antenna_ref,
            )
            _bus = UDPMulticastBus(host=host, identity=identity, **kwargs)
            _host = host
            log.info("peer-bus active: udp-multicast host=%s group=%s port=%s "
                     "antenna_ref=%r",
                     host, kwargs.get("group", "default"),
                     kwargs.get("port", "default"), antenna_ref)
            return True
        log.error("peer-bus: unsupported transport spec %r", spec)
        return False
    except Exception:
        log.exception("peer-bus initialization failed (%r); disabled",
                      spec)
        _bus = None
        _host = None
        return False


def shutdown() -> None:
    """Close the bus cleanly on engine exit.  Safe to call when
    not initialized."""
    global _bus, _host
    if _bus is not None:
        try:
            _bus.close()
        except Exception:
            log.exception("peer-bus close failed")
    _bus = None
    _host = None


def is_active() -> bool:
    return _bus is not None


# ── Typed publish helpers ───────────────────────────────────── #
#
# Each helper no-ops when the bus isn't active.  Payload construction
# happens inline — no need for the engine to know about schema
# dataclasses.


def publish_position(
    *,
    ant_pos_est_state: str,
    lat_deg: Optional[float],
    lon_deg: Optional[float],
    alt_m: Optional[float],
    position_sigma_m: Optional[float],
    worst_sigma_m: Optional[float] = None,
    reached_anchored: bool = False,
) -> None:
    if _bus is None:
        return
    from peppar_bus import mono_ns
    from peppar_bus.schemas import PositionPayload, to_bytes
    payload = PositionPayload(
        ts_mono_ns=mono_ns(),
        ant_pos_est_state=ant_pos_est_state,
        lat_deg=lat_deg, lon_deg=lon_deg, alt_m=alt_m,
        position_sigma_m=position_sigma_m,
        worst_sigma_m=worst_sigma_m,
        reached_anchored=reached_anchored,
    )
    _bus.publish(f"peppar-fix.{_host}.position", to_bytes(payload))


def publish_ztd(*, ztd_m: Optional[float],
                ztd_sigma_mm: Optional[int] = None) -> None:
    if _bus is None or ztd_m is None:
        return
    from peppar_bus import mono_ns
    from peppar_bus.schemas import ZTDPayload, to_bytes
    payload = ZTDPayload(
        ts_mono_ns=mono_ns(), ztd_m=ztd_m, ztd_sigma_mm=ztd_sigma_mm,
    )
    _bus.publish(f"peppar-fix.{_host}.ztd", to_bytes(payload))


def publish_tide(*, total_mm: Optional[int],
                 u_mm: Optional[int] = None) -> None:
    if _bus is None or total_mm is None:
        return
    from peppar_bus import mono_ns
    from peppar_bus.schemas import TidePayload, to_bytes
    payload = TidePayload(
        ts_mono_ns=mono_ns(), total_mm=total_mm, u_mm=u_mm,
    )
    _bus.publish(f"peppar-fix.{_host}.tide", to_bytes(payload))


def publish_sv_state(*, sv_states: dict, nl_capable: str = "") -> None:
    if _bus is None:
        return
    from peppar_bus import mono_ns
    from peppar_bus.schemas import SvStatePayload, to_bytes
    payload = SvStatePayload(
        ts_mono_ns=mono_ns(),
        sv_states=dict(sv_states), nl_capable=nl_capable,
    )
    _bus.publish(f"peppar-fix.{_host}.sv-state", to_bytes(payload))


def publish_integer_fix(*, sv: str, n_wl: Optional[int],
                        n_nl: Optional[int], state: str) -> None:
    if _bus is None:
        return
    from peppar_bus import mono_ns
    from peppar_bus.schemas import IntegerFixPayload, to_bytes
    payload = IntegerFixPayload(
        ts_mono_ns=mono_ns(), sv=sv,
        n_wl=n_wl, n_nl=n_nl, state=state,
    )
    _bus.publish(f"peppar-fix.{_host}.integer-fix.{sv}", to_bytes(payload))


def publish_slip_event(
    *,
    sv: str,
    reasons,
    conf: str,
    elev_deg: Optional[float] = None,
    lock_duration_ms: Optional[int] = None,
    gf_jump_m: Optional[float] = None,
    mw_jump_cyc: Optional[float] = None,
) -> None:
    if _bus is None:
        return
    from peppar_bus import mono_ns
    from peppar_bus.schemas import SlipEventPayload, to_bytes
    payload = SlipEventPayload(
        ts_mono_ns=mono_ns(), sv=sv,
        reasons=list(reasons), conf=conf,
        elev_deg=elev_deg, lock_duration_ms=lock_duration_ms,
        gf_jump_m=gf_jump_m, mw_jump_cyc=mw_jump_cyc,
    )
    _bus.publish(f"peppar-fix.{_host}.slip-event.{sv}", to_bytes(payload))


def publish_streams(*, ssr_mount: Optional[str],
                    eph_mount: Optional[str]) -> None:
    if _bus is None:
        return
    from peppar_bus import mono_ns
    from peppar_bus.schemas import StreamsPayload, to_bytes
    payload = StreamsPayload(
        ts_mono_ns=mono_ns(),
        ssr_mount=ssr_mount, eph_mount=eph_mount,
    )
    _bus.publish(f"peppar-fix.{_host}.streams", to_bytes(payload))
