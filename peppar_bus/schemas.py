"""Typed payload schemas for each topic.

One dataclass per message type.  Dataclasses serialize to JSON via
``dataclasses.asdict``; helpers here convert to/from bytes.

Schema versioning: every message has a ``schema_version`` field.
Consumers must tolerate unknown fields (forward-compat) and missing
fields (backward-compat — treat as None).  Bumping a version
signals incompatible change; consumers SHOULD refuse.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field, fields
from typing import Optional


SCHEMA_VERSION = 1


@dataclass
class HeartbeatPayload:
    """Published periodically to advertise presence + identity.

    Topic: ``peppar-fix.<host>.heartbeat``
    Cadence: every 1 s (UDPMulticastBus), negotiable per transport.
    """

    schema_version: int = SCHEMA_VERSION
    ts_mono_ns: int = 0
    engine_version: str = "unknown"
    systems: str = ""
    antenna_ref: str = ""


@dataclass
class PositionPayload:
    """Current AntPosEst filter output.

    Topic: ``peppar-fix.<host>.position``
    Cadence: driven by engine's AntPosEst log cadence (every ~10 s
    when settled, faster during bootstrap).
    """

    schema_version: int = SCHEMA_VERSION
    ts_mono_ns: int = 0
    ts_gps_iso: str = ""        # GPS time ISO-8601 if known
    ant_pos_est_state: str = "surveying"
    lat_deg: Optional[float] = None
    lon_deg: Optional[float] = None
    alt_m: Optional[float] = None
    position_sigma_m: Optional[float] = None
    worst_sigma_m: Optional[float] = None
    reached_anchored: bool = False


@dataclass
class SvStatePayload:
    """Per-SV state-machine snapshot.

    Topic: ``peppar-fix.<host>.sv-state``
    Cadence: on-change (each SV transition fires one message).  For
    the initial MVP publish a full snapshot periodically and
    on-change; incremental updates are a future optimization.

    ``sv_states`` maps SV ids to SvAmbState names (TRACKING,
    FLOATING, CONVERGING, ANCHORING, ANCHORED, WAITING).
    """

    schema_version: int = SCHEMA_VERSION
    ts_mono_ns: int = 0
    sv_states: dict[str, str] = field(default_factory=dict)
    nl_capable: str = ""        # e.g. "GE" or "GEC"


@dataclass
class IntegerFixPayload:
    """One SV's current NL integer fix.

    Topic: ``peppar-fix.<host>.integer-fix.<sv>``
    Cadence: on-change (fix lands, fix falls, etc.).

    ``n_nl`` is the narrow-lane integer that identifies the fix;
    together with ``n_wl`` it uniquely specifies the per-SV
    ambiguity set in the L1/L5 (or L1/L2) pair.  Consumers
    comparing across shared-antenna peers expect identical values.
    """

    schema_version: int = SCHEMA_VERSION
    ts_mono_ns: int = 0
    sv: str = ""
    n_wl: Optional[int] = None
    n_nl: Optional[int] = None
    state: str = "FLOATING"      # SvAmbState name at emit time


@dataclass
class ZTDPayload:
    """Current residual ZTD above Saastamoinen a priori.

    Topic: ``peppar-fix.<host>.ztd``
    Cadence: with each AntPosEst log emission.
    """

    schema_version: int = SCHEMA_VERSION
    ts_mono_ns: int = 0
    ztd_m: Optional[float] = None
    ztd_sigma_mm: Optional[int] = None


@dataclass
class TidePayload:
    """Current solid Earth tide magnitude.

    Topic: ``peppar-fix.<host>.tide``
    Cadence: every AntPosEst log emission.  Mostly diagnostic —
    two hosts at the same lat/lon/epoch should match.
    """

    schema_version: int = SCHEMA_VERSION
    ts_mono_ns: int = 0
    total_mm: Optional[int] = None
    u_mm: Optional[int] = None


@dataclass
class StreamsPayload:
    """NTRIP correction stream identifiers.

    Topic: ``peppar-fix.<host>.streams``
    Cadence: once at startup + on reconnect.  Mid-run swaps are
    rare (flagged as low priority in docs).
    """

    schema_version: int = SCHEMA_VERSION
    ts_mono_ns: int = 0
    ssr_mount: Optional[str] = None
    eph_mount: Optional[str] = None


# ── Serialization helpers ─────────────────────────────────────── #


def to_bytes(payload) -> bytes:
    """Convert a dataclass payload to JSON bytes for publish.
    ``None`` fields are included (not stripped) so consumers can
    distinguish 'not set' from 'not in this schema version'."""
    return json.dumps(
        dataclasses.asdict(payload), separators=(",", ":"),
    ).encode("utf-8")


def from_bytes(cls, data: bytes):
    """Deserialize JSON bytes into a dataclass instance.  Unknown
    keys ignored (forward-compat); missing keys take the
    dataclass default (backward-compat)."""
    raw = json.loads(data.decode("utf-8"))
    known = {f.name for f in fields(cls)}
    filtered = {k: v for k, v in raw.items() if k in known}
    return cls(**filtered)
