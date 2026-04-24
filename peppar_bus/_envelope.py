"""On-wire envelope: JSON Lines.

Every transport wraps the caller's payload in this envelope:

    {"h": <host>, "t": <topic>, "p": <payload>}

- Each message is exactly one line terminated by ``\\n`` — streaming-
  friendly on TCP, atomic on UDP (one datagram = one line).
- Payload ``p`` is a string (base64-encoded bytes) or a nested JSON
  value.  We default to nested JSON because that's what callers
  publish — structured payloads, not opaque bytes.  If a caller
  ever needs binary, wrap in base64 at the call site.
- Host ``h`` is set by the transport on publish, not by the caller.
"""

from __future__ import annotations

import json


def encode(host: str, topic: str, payload: bytes) -> bytes:
    """Encode a message for the wire.  Payload is bytes (typically
    from ``json.dumps(...).encode()``); we accept bytes for
    interface symmetry with decode().
    """
    envelope = {
        "h": host,
        "t": topic,
        "p": json.loads(payload.decode("utf-8")) if payload else None,
    }
    return (json.dumps(envelope, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes) -> tuple[str, str, bytes]:
    """Decode one wire line.  Returns (host, topic, payload_bytes).
    Raises ValueError on malformed input."""
    text = line.decode("utf-8").rstrip("\n")
    if not text:
        raise ValueError("empty line")
    envelope = json.loads(text)
    host = envelope.get("h")
    topic = envelope.get("t")
    payload = envelope.get("p")
    if not isinstance(host, str) or not isinstance(topic, str):
        raise ValueError("envelope missing host or topic")
    payload_bytes = (
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if payload is not None
        else b""
    )
    return host, topic, payload_bytes


def match(pattern: str, topic: str) -> bool:
    """Dot-separated glob matching.

    ``*`` matches exactly one dot-separated segment; ``**`` matches
    zero or more segments.  Examples::

        match("peppar-fix.*.position", "peppar-fix.timehat.position") == True
        match("peppar-fix.*.position", "peppar-fix.timehat.sv.position") == False
        match("peppar-fix.**", "peppar-fix.timehat.sv.state") == True
        match("peppar-fix.timehat.*", "peppar-fix.clkpoc3.position") == False
    """
    pat_parts = pattern.split(".")
    topic_parts = topic.split(".")

    def _match(p: list[str], t: list[str]) -> bool:
        if not p:
            return not t
        if p[0] == "**":
            # Zero or more segments — try every split.
            if len(p) == 1:
                return True  # trailing ** matches everything
            for i in range(len(t) + 1):
                if _match(p[1:], t[i:]):
                    return True
            return False
        if not t:
            return False
        if p[0] == "*" or p[0] == t[0]:
            return _match(p[1:], t[1:])
        return False

    return _match(pat_parts, topic_parts)
