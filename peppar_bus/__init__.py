"""peppar_bus — transport-agnostic peer-state bus for PePPAR-Fix.

Shared between peppar-mon and (eventually) peppar_fix_engine.  Must
not import anything from either consumer — the whole point of a
shared bus is that producers and consumers don't have to know about
each other.

Public surface:

  PeerBus              — abstract transport (publish / subscribe / peers / close)
  UDPMulticastBus      — default LAN transport; zero infra
  PeerMessage          — deserialized on-wire envelope
  PeerIdentity         — heartbeat payload
  encode / decode      — envelope serialization (JSON Lines)
  schemas              — typed payload dataclasses for each topic

Transport roadmap: UDPMulticastBus today, TCPP2PBus next, MQTTBus
when fleet scales.  All share this interface.  See
``docs/peer-state-sharing.md`` for the design context.
"""

from peppar_bus._abc import PeerBus, PeerMessage, PeerIdentity
from peppar_bus._envelope import encode, decode
from peppar_bus._udp_multicast import UDPMulticastBus
from peppar_bus import schemas

__all__ = [
    "PeerBus",
    "PeerMessage",
    "PeerIdentity",
    "UDPMulticastBus",
    "encode",
    "decode",
    "schemas",
]

__version__ = "0.0.1"
