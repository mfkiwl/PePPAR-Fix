"""Log-to-bus bridge.

peppar-mon tails its own engine log via LogReader; the bridge
watches LogState for changes and publishes the changed fields to
a PeerBus.  This lets Phase 1 of peer state sharing run without
any engine changes — the monitor is the producer.

Publishing is driven by change-detection: we keep a snapshot of
the last-published values for each topic and only re-publish
when something visible changed.  This keeps the multicast traffic
proportional to the engine's own update rate (every ~10 s when
settled) rather than polling-rate dependent.

When/if the engine grows native PeerBus publishing, the bridge
retires — peppar-mon drops the publisher and keeps only the
subscriber path.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from typing import Optional

from peppar_bus import PeerBus, schemas

from peppar_mon.log_reader import LogReader, LogState


class LogToBusBridge:
    """Watches a LogReader's state and publishes changes to a bus.

    Runs a small polling thread (configurable interval).  Polling
    is cheap compared to the engine's log-write rate: LogState
    exposes the current snapshot as plain attributes; we compare
    a handful of values and publish on change.

    Change detection is keyed by topic.  Example: the engine
    writes one ``[AntPosEst]`` line every ~10 s; that line
    updates position, worst_sigma_m, ztd_m, tide fields all
    at once.  One poll later, the bridge sees all of them new
    and publishes three separate topic messages in quick
    succession.  That's fine — subscribers handle each topic
    independently.
    """

    def __init__(
        self,
        *,
        reader: LogReader,
        bus: PeerBus,
        host: str,
        poll_interval_s: float = 1.0,
    ) -> None:
        self._reader = reader
        self._bus = bus
        self._host = host
        self._poll_interval_s = poll_interval_s
        self._stop_flag = threading.Event()

        # Last-published snapshots per topic; drives change-detect.
        self._last_position: Optional[schemas.PositionPayload] = None
        self._last_ztd: Optional[schemas.ZTDPayload] = None
        self._last_sv_state: Optional[schemas.SvStatePayload] = None
        self._last_streams: Optional[schemas.StreamsPayload] = None
        self._last_tide: Optional[schemas.TidePayload] = None

        self._thread = threading.Thread(
            target=self._loop, name=f"log-bus-bridge-{host}", daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_flag.set()
        self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop_flag.wait(self._poll_interval_s):
            try:
                self._publish_changes()
            except Exception:
                # Don't let a publish glitch stop the bridge.
                import logging
                logging.getLogger(__name__).exception(
                    "bridge publish failed",
                )

    def _publish_changes(self) -> None:
        state = self._reader.state
        now_ns = time.monotonic_ns()

        self._maybe_publish_position(state, now_ns)
        self._maybe_publish_ztd(state, now_ns)
        self._maybe_publish_tide(state, now_ns)
        self._maybe_publish_sv_state(state, now_ns)
        self._maybe_publish_streams(state, now_ns)

    def _maybe_publish_position(self, state: LogState, now_ns: int) -> None:
        if state.antenna_position is None:
            return
        lat, lon, alt = state.antenna_position
        payload = schemas.PositionPayload(
            ts_mono_ns=now_ns,
            ant_pos_est_state=state.ant_pos_est_state or "surveying",
            lat_deg=lat, lon_deg=lon, alt_m=alt,
            position_sigma_m=state.antenna_sigma_m,
            worst_sigma_m=state.worst_sigma_m,
            reached_anchored=state.reached_anchored,
        )
        if _payload_eq(self._last_position, payload):
            return
        self._last_position = payload
        self._bus.publish(
            f"peppar-fix.{self._host}.position",
            schemas.to_bytes(payload),
        )

    def _maybe_publish_ztd(self, state: LogState, now_ns: int) -> None:
        if state.ztd_m is None:
            return
        payload = schemas.ZTDPayload(
            ts_mono_ns=now_ns,
            ztd_m=state.ztd_m,
            ztd_sigma_mm=state.ztd_sigma_mm,
        )
        if _payload_eq(self._last_ztd, payload):
            return
        self._last_ztd = payload
        self._bus.publish(
            f"peppar-fix.{self._host}.ztd", schemas.to_bytes(payload),
        )

    def _maybe_publish_tide(self, state: LogState, now_ns: int) -> None:
        if state.earth_tide_mm is None:
            return
        payload = schemas.TidePayload(
            ts_mono_ns=now_ns,
            total_mm=state.earth_tide_mm,
            u_mm=state.earth_tide_u_mm,
        )
        if _payload_eq(self._last_tide, payload):
            return
        self._last_tide = payload
        self._bus.publish(
            f"peppar-fix.{self._host}.tide", schemas.to_bytes(payload),
        )

    def _maybe_publish_sv_state(self, state: LogState, now_ns: int) -> None:
        if not state.sv_states:
            return
        payload = schemas.SvStatePayload(
            ts_mono_ns=now_ns,
            sv_states=dict(state.sv_states),
            nl_capable="".join(sorted(state.nl_capable_constellations or "")),
        )
        if _payload_eq(self._last_sv_state, payload):
            return
        self._last_sv_state = payload
        self._bus.publish(
            f"peppar-fix.{self._host}.sv-state",
            schemas.to_bytes(payload),
        )

    def _maybe_publish_streams(self, state: LogState, now_ns: int) -> None:
        if state.ssr_mount is None and state.eph_mount is None:
            return
        payload = schemas.StreamsPayload(
            ts_mono_ns=now_ns,
            ssr_mount=state.ssr_mount,
            eph_mount=state.eph_mount,
        )
        if _payload_eq(self._last_streams, payload):
            return
        self._last_streams = payload
        self._bus.publish(
            f"peppar-fix.{self._host}.streams",
            schemas.to_bytes(payload),
        )


def _payload_eq(a, b) -> bool:
    """Change-detect helper.  Ignores ts_mono_ns (always changes)
    so we publish only when display-relevant fields actually
    shifted."""
    if a is None or b is None:
        return False
    da = dataclasses.asdict(a)
    db = dataclasses.asdict(b)
    da.pop("ts_mono_ns", None)
    db.pop("ts_mono_ns", None)
    return da == db
