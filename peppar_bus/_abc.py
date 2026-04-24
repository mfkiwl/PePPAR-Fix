"""PeerBus abstract transport interface + message dataclasses."""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional


@dataclass(frozen=True)
class PeerIdentity:
    """Heartbeat payload — who a peer is.

    ``host`` should be unique in the fleet.  Hostname is fine for
    lab use; for WAN fleets the engine's receiver UID is the safer
    choice (state/receivers/<uid>.json format).

    ``antenna_ref`` is an opaque string naming the antenna the peer
    is observing.  Hosts with the same antenna_ref are sharing a
    splitter and their integer fixes must agree (mod PCO).  The
    fleet aggregator uses this to decide whether cross-host
    integer comparison is meaningful.
    """

    host: str
    version: str = "unknown"
    systems: str = ""          # e.g. "G+E+C"
    antenna_ref: str = ""
    first_seen_mono_ns: int = 0


@dataclass(frozen=True)
class PeerMessage:
    """One received message off the bus.  Callers receive these via
    ``PeerBus.subscribe(callback=...)``."""

    from_host: str
    topic: str
    recv_mono_ns: int   # monotonic clock at receive
    payload: bytes      # undeserialized; caller picks schema


class PeerBus(abc.ABC):
    """Abstract transport.

    Implementations: UDPMulticastBus (lab), TCPP2PBus (production
    v1), MQTTBus (production v2).  All three share this interface
    so engine and monitor code is transport-agnostic.

    Thread safety: implementations MUST tolerate publish from one
    thread while subscribe callbacks fire on a different (transport-
    internal) thread.  Callbacks MUST NOT block the transport thread
    for long (do work in a queue if needed).
    """

    @abc.abstractmethod
    def publish(self, topic: str, payload: bytes) -> None:
        """Emit one message to any subscribers matching ``topic``.

        Payload is already serialized (typically JSON bytes).  The
        transport wraps it in its own envelope and adds the local
        host identifier automatically.
        """

    @abc.abstractmethod
    def subscribe(
        self,
        topic_pattern: str,
        callback: Callable[[PeerMessage], None],
    ) -> None:
        """Register a callback to fire on messages matching the
        pattern.

        Pattern syntax is transport-defined but the common convention
        is dot-separated with ``*`` as a single-level wildcard:

            peppar-fix.*.position     — any host's position topic
            peppar-fix.timehat.*      — every topic from timehat
            peppar-fix.*.*            — everything

        Multiple subscriptions may match one message; all matching
        callbacks fire.
        """

    @abc.abstractmethod
    def peers(self) -> Iterable[PeerIdentity]:
        """Snapshot of peers that have sent a heartbeat recently.

        Stale peers (no heartbeat in some implementation-defined
        window) are excluded.  Caller takes this as informational —
        a peer can appear/disappear between calls.
        """

    @abc.abstractmethod
    def close(self) -> None:
        """Stop the transport.  After close, publish is a no-op and
        new subscribe registrations are rejected.  Existing callbacks
        stop firing."""


def mono_ns() -> int:
    """Current monotonic clock in nanoseconds.  Used for message
    timestamps; not the same axis as wall-clock or GPS time."""
    return time.monotonic_ns()
