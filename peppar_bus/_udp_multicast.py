"""UDP multicast implementation of PeerBus.

Zero-infrastructure transport for LAN fleets.  Every participant
joins one multicast group; every publish hits every listener.
TTL=1 by default keeps traffic on the local subnet.

Design notes:
- One socket for send, one for receive (can be the same but separate
  is cleaner and avoids send/recv interleaving edge cases).
- One background thread runs the receive loop and dispatches to
  registered callbacks.  Callbacks run on that thread — expected to
  be cheap (append to a queue, flag a widget for refresh, etc.).
- Heartbeats publish every HEARTBEAT_S (1 s) with PeerIdentity as
  payload on topic ``peppar-fix.<host>.heartbeat``.
- A peer is considered alive if its last heartbeat was within
  STALE_S (5 s) ago.  ``peers()`` filters stale entries.
- Self-loopback is dropped (we don't want to see our own messages
  echoed as peer state).
"""

from __future__ import annotations

import json
import logging
import re
import socket
import struct
import threading
import time
from typing import Callable, Optional

from peppar_bus._abc import PeerBus, PeerMessage, PeerIdentity, mono_ns
from peppar_bus._envelope import decode, encode, match
from peppar_bus import schemas

log = logging.getLogger(__name__)

DEFAULT_GROUP = "239.18.8.13"
DEFAULT_PORT = 12468
DEFAULT_TTL = 1

HEARTBEAT_S = 1.0
STALE_S = 5.0

# Max payload size in one datagram.  UDP's wire limit is 65535 but
# real-world MTU is ~1500 — keep payloads well below that to avoid
# IP fragmentation.  If a payload outgrows this, split it or move
# to a different transport.
MAX_DATAGRAM_BYTES = 1400


class UDPMulticastBus(PeerBus):
    """LAN-only pub/sub transport via IPv4 multicast."""

    def __init__(
        self,
        *,
        host: str,
        group: str = DEFAULT_GROUP,
        port: int = DEFAULT_PORT,
        ttl: int = DEFAULT_TTL,
        identity: Optional[PeerIdentity] = None,
        heartbeat_s: float = HEARTBEAT_S,
    ) -> None:
        self._host = host
        self._group = group
        self._port = port
        self._ttl = ttl
        self._identity = identity or PeerIdentity(
            host=host, first_seen_mono_ns=mono_ns(),
        )
        self._heartbeat_s = heartbeat_s

        self._subs: list[tuple[re.Pattern, str, Callable[[PeerMessage], None]]] = []
        self._subs_lock = threading.Lock()
        self._peers: dict[str, tuple[PeerIdentity, int]] = {}  # host → (id, last_mono_ns)
        self._peers_lock = threading.Lock()
        self._stop_flag = threading.Event()

        self._send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                                         socket.IPPROTO_UDP)
        self._send_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL,
                                    struct.pack("b", self._ttl))
        # LOOP=1 so multiple bus instances on one host (including
        # a single test process) can hear each other.  We drop our
        # own messages via the envelope host-header check in the
        # receive loop, which is the robust fix anyway (handles
        # the multi-NIC case where a packet can return via a
        # different interface).
        self._send_sock.setsockopt(socket.IPPROTO_IP,
                                    socket.IP_MULTICAST_LOOP, 1)

        self._recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                                         socket.IPPROTO_UDP)
        self._recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            # SO_REUSEPORT is Linux/BSD; skip if unsupported.
            pass
        self._recv_sock.bind(("", self._port))
        mreq = struct.pack("=4sl", socket.inet_aton(self._group),
                            socket.INADDR_ANY)
        self._recv_sock.setsockopt(socket.IPPROTO_IP,
                                    socket.IP_ADD_MEMBERSHIP, mreq)
        # Short timeout so the receive loop can check _stop_flag often.
        self._recv_sock.settimeout(0.2)

        self._recv_thread = threading.Thread(
            target=self._receive_loop, name=f"peer-bus-recv-{host}", daemon=True,
        )
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name=f"peer-bus-hb-{host}", daemon=True,
        )
        self._recv_thread.start()
        self._heartbeat_thread.start()

    # ── Public interface ──────────────────────────────────────── #

    def publish(self, topic: str, payload: bytes) -> None:
        if self._stop_flag.is_set():
            return
        line = encode(self._host, topic, payload)
        if len(line) > MAX_DATAGRAM_BYTES:
            log.warning("peer-bus payload on topic %s is %d bytes "
                        "(> MAX_DATAGRAM_BYTES=%d); likely fragmented",
                        topic, len(line), MAX_DATAGRAM_BYTES)
        try:
            self._send_sock.sendto(line, (self._group, self._port))
        except OSError as e:
            log.warning("peer-bus send failed: %s", e)

    def subscribe(
        self,
        topic_pattern: str,
        callback: Callable[[PeerMessage], None],
    ) -> None:
        if self._stop_flag.is_set():
            return
        with self._subs_lock:
            self._subs.append((_compile(topic_pattern), topic_pattern, callback))

    def peers(self):
        now = mono_ns()
        cutoff_ns = now - int(STALE_S * 1e9)
        with self._peers_lock:
            return [
                ident for ident, last in self._peers.values()
                if last >= cutoff_ns
            ]

    def close(self) -> None:
        self._stop_flag.set()
        try:
            self._recv_sock.close()
        except OSError:
            pass
        try:
            self._send_sock.close()
        except OSError:
            pass

    # ── Background threads ────────────────────────────────────── #

    def _receive_loop(self) -> None:
        while not self._stop_flag.is_set():
            try:
                data, _addr = self._recv_sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                # Socket closed during shutdown; normal.
                return
            try:
                from_host, topic, payload = decode(data)
            except (ValueError, json.JSONDecodeError) as e:
                log.debug("peer-bus decode failed: %s", e)
                continue
            if from_host == self._host:
                # Self-loopback should be off but catch it anyway.
                continue
            now = mono_ns()
            # Update peer liveness on every message, not just
            # heartbeats — any recent traffic implies the peer is
            # alive.
            with self._peers_lock:
                existing = self._peers.get(from_host)
                if existing is not None:
                    self._peers[from_host] = (existing[0], now)
            # Heartbeat payloads carry a fresh PeerIdentity; update
            # the peers table.
            if topic.endswith(".heartbeat"):
                try:
                    hb = schemas.from_bytes(schemas.HeartbeatPayload, payload)
                except Exception as e:
                    log.debug("heartbeat parse failed: %s", e)
                else:
                    ident = PeerIdentity(
                        host=from_host,
                        version=hb.engine_version,
                        systems=hb.systems,
                        antenna_ref=hb.antenna_ref,
                        site_ref=hb.site_ref,
                        first_seen_mono_ns=(
                            self._peers.get(from_host, (None, 0))[1] or now
                        ),
                    )
                    with self._peers_lock:
                        self._peers[from_host] = (ident, now)
            # Dispatch to matching subscribers.
            msg = PeerMessage(
                from_host=from_host, topic=topic,
                recv_mono_ns=now, payload=payload,
            )
            with self._subs_lock:
                matching = [cb for rx, _pat, cb in self._subs
                            if rx.fullmatch(topic)]
            for cb in matching:
                try:
                    cb(msg)
                except Exception:
                    log.exception("peer-bus subscriber raised")

    def _heartbeat_loop(self) -> None:
        while not self._stop_flag.wait(self._heartbeat_s):
            hb = schemas.HeartbeatPayload(
                ts_mono_ns=mono_ns(),
                engine_version=self._identity.version,
                systems=self._identity.systems,
                antenna_ref=self._identity.antenna_ref,
                site_ref=self._identity.site_ref,
            )
            self.publish(
                f"peppar-fix.{self._host}.heartbeat",
                schemas.to_bytes(hb),
            )


def _compile(pattern: str) -> re.Pattern:
    """Compile a dot-separated glob to a regex.  ``*`` matches one
    segment; ``**`` matches zero or more segments; literal dots
    separate segments.

    We compile once at subscribe time so the receive loop just runs
    fullmatch per message.  Correctness matches ``_envelope.match``'s
    recursive version; this regex form is faster for the hot path.
    """
    parts = pattern.split(".")
    out = []
    for p in parts:
        if p == "**":
            out.append(r"(?:[^.]+(?:\.[^.]+)*)?")
        elif p == "*":
            out.append(r"[^.]+")
        else:
            out.append(re.escape(p))
    # Join with literal dots, but handle the case where ``**`` sat
    # between dots and collapsed either side.  Simple approach: join
    # with ``\.`` and accept that ``**`` wrapped itself in an
    # optional group.
    return re.compile(r"\.".join(out))
