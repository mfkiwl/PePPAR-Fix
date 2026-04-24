"""Integration tests for UDPMulticastBus.

Uses a real multicast socket (not mocked) because the Linux kernel
socket behaviour IS what we're testing.  Picks a random port per
test to avoid collision with neighbouring runs.

These tests require a Linux host with multicast loopback capability.
Skip if the socket setup fails (CI containers without multicast).
"""

from __future__ import annotations

import random
import socket
import struct
import threading
import time
import unittest

from peppar_bus import (
    PeerIdentity,
    UDPMulticastBus,
    schemas,
)

# Small helper: randomize port so parallel test runs don't collide.
def _random_port() -> int:
    return random.randint(40000, 59999)


def _multicast_available() -> bool:
    """Quick pre-check: can we open a multicast socket at all?"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL,
                     struct.pack("b", 1))
        s.close()
        return True
    except OSError:
        return False


@unittest.skipUnless(_multicast_available(),
                     "multicast socket not available")
class UDPMulticastBusTest(unittest.TestCase):
    def setUp(self):
        # Two peers on the same group + port.  Disable loopback
        # prevents self-hearing; peers only see each other.
        self.port = _random_port()
        self.bus_a = UDPMulticastBus(
            host="host-a", port=self.port,
            identity=PeerIdentity(host="host-a", version="v1",
                                  systems="G+E", antenna_ref="LAB"),
            heartbeat_s=0.1,  # fast heartbeats for test speed
        )
        self.bus_b = UDPMulticastBus(
            host="host-b", port=self.port,
            identity=PeerIdentity(host="host-b", version="v1",
                                  systems="G+E", antenna_ref="LAB"),
            heartbeat_s=0.1,
        )

    def tearDown(self):
        self.bus_a.close()
        self.bus_b.close()

    def test_publish_reaches_peer(self):
        """A publishes on a topic; B's subscribed callback fires."""
        got: list = []
        event = threading.Event()

        def cb(msg):
            got.append(msg)
            event.set()

        self.bus_b.subscribe("peppar-fix.host-a.*", cb)
        # Small delay so subscription is live before publish.
        time.sleep(0.05)
        self.bus_a.publish("peppar-fix.host-a.position", b'{"x":1}')
        self.assertTrue(event.wait(timeout=2.0))
        self.assertEqual(got[0].from_host, "host-a")
        self.assertEqual(got[0].topic, "peppar-fix.host-a.position")

    def test_self_loopback_suppressed(self):
        """A publishes; A's own subscribers must NOT fire.  The
        transport drops self-origin messages before dispatch."""
        got: list = []

        def cb(msg):
            got.append(msg)

        self.bus_a.subscribe("peppar-fix.**", cb)
        time.sleep(0.05)
        self.bus_a.publish("peppar-fix.host-a.position", b'{"x":1}')
        time.sleep(0.3)
        # B's heartbeats may land here via wildcard; filter to only
        # messages from host-a (the self-origin we're testing).
        self_hits = [m for m in got if m.from_host == "host-a"]
        self.assertEqual(self_hits, [])

    def test_heartbeat_populates_peers(self):
        """After running briefly, each bus should list the other
        via ``peers()``."""
        # Heartbeat cadence is 0.1 s; give them 0.5 s to exchange.
        time.sleep(0.5)
        peers_a = list(self.bus_a.peers())
        peers_b = list(self.bus_b.peers())
        hosts_a = {p.host for p in peers_a}
        hosts_b = {p.host for p in peers_b}
        self.assertIn("host-b", hosts_a)
        self.assertIn("host-a", hosts_b)
        # Identity fields round-trip via the heartbeat payload.
        for p in peers_a:
            if p.host == "host-b":
                self.assertEqual(p.version, "v1")
                self.assertEqual(p.antenna_ref, "LAB")

    def test_topic_wildcard_matches(self):
        """Subscription with ``*`` matches exactly one segment."""
        got = []
        event = threading.Event()

        def cb(msg):
            got.append(msg)
            event.set()

        self.bus_b.subscribe("peppar-fix.*.position", cb)
        time.sleep(0.05)
        self.bus_a.publish("peppar-fix.host-a.position", b'{"x":1}')
        self.assertTrue(event.wait(timeout=2.0))
        self.assertEqual(len(got), 1)
        # A different topic shouldn't fire.
        event.clear()
        self.bus_a.publish("peppar-fix.host-a.ztd", b'{"x":1}')
        self.assertFalse(event.wait(timeout=0.3))

    def test_payload_roundtrip_via_schema(self):
        """End-to-end: publish a PositionPayload, peer receives and
        decodes cleanly."""
        received = []
        event = threading.Event()

        def cb(msg):
            pos = schemas.from_bytes(schemas.PositionPayload, msg.payload)
            received.append(pos)
            event.set()

        self.bus_b.subscribe("peppar-fix.host-a.position", cb)
        time.sleep(0.05)
        p = schemas.PositionPayload(
            ts_mono_ns=42, lat_deg=40.0, lon_deg=-90.0, alt_m=200.0,
            ant_pos_est_state="anchored", position_sigma_m=0.02,
        )
        self.bus_a.publish("peppar-fix.host-a.position", schemas.to_bytes(p))
        self.assertTrue(event.wait(timeout=2.0))
        self.assertEqual(received[0].ant_pos_est_state, "anchored")
        self.assertEqual(received[0].lat_deg, 40.0)

    def test_close_stops_delivery(self):
        """After close(), no more subscriber callbacks fire and
        publish is a no-op."""
        got = []

        def cb(msg):
            got.append(msg)

        self.bus_b.subscribe("peppar-fix.**", cb)
        time.sleep(0.05)
        self.bus_b.close()
        time.sleep(0.1)
        before = len(got)
        self.bus_a.publish("peppar-fix.host-a.position", b'{"x":1}')
        time.sleep(0.3)
        # No new messages after close — only whatever was pre-close.
        self.assertEqual(len(got), before)


if __name__ == "__main__":
    unittest.main()
