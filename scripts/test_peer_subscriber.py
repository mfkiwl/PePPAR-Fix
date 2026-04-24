"""Unit tests for peer_subscriber (engine-side consumer helper).

Uses a fake in-memory PeerBus so callbacks fire synchronously
via the test-controlled bus object.  No sockets.
"""

from __future__ import annotations

import unittest

import peer_publisher
import peer_subscriber
from peppar_bus import PeerBus, PeerIdentity, PeerMessage, mono_ns, schemas


class _Bus(PeerBus):
    """In-memory bus.  Tracks subscriptions + lets tests push
    messages through dispatch() to fire the registered callbacks."""

    def __init__(self):
        self.subs: list[tuple[str, object]] = []
        self._peers: list[PeerIdentity] = []

    def publish(self, topic, payload):
        # Not testing publish here.
        pass

    def subscribe(self, topic_pattern, callback):
        self.subs.append((topic_pattern, callback))

    def peers(self):
        return list(self._peers)

    def close(self):
        pass

    # Test helpers
    def set_peers(self, peers):
        self._peers = list(peers)

    def dispatch(self, topic, payload, from_host="h1"):
        """Simulate a received message by firing matching
        subscribers.  Uses the same dot-glob match semantics as the
        real bus (one segment per `*`)."""
        import re
        for pat, cb in self.subs:
            regex_parts = []
            for seg in pat.split("."):
                if seg == "*":
                    regex_parts.append(r"[^.]+")
                else:
                    regex_parts.append(re.escape(seg))
            if re.fullmatch(r"\.".join(regex_parts), topic):
                cb(PeerMessage(
                    from_host=from_host, topic=topic,
                    recv_mono_ns=mono_ns(), payload=payload,
                ))


class SubscriberInitTest(unittest.TestCase):
    def setUp(self):
        # Install a fake bus in peer_publisher's slot so
        # peer_subscriber's get_bus() discovery works.
        self.bus = _Bus()
        peer_publisher._bus = self.bus
        peer_publisher._host = "local"

    def tearDown(self):
        peer_subscriber.shutdown()
        peer_publisher._bus = None
        peer_publisher._host = None

    def test_initialize_registers_callbacks(self):
        ok = peer_subscriber.initialize(
            antenna_ref="UFO1", site_ref="DuPage",
        )
        self.assertTrue(ok)
        self.assertTrue(peer_subscriber.is_active())
        # Two subs: position + ztd
        patterns = [p for p, _ in self.bus.subs]
        self.assertIn("peppar-fix.*.position", patterns)
        self.assertIn("peppar-fix.*.ztd", patterns)
        self.assertEqual(peer_subscriber.get_local_antenna_ref(), "UFO1")
        self.assertEqual(peer_subscriber.get_local_site_ref(), "DuPage")

    def test_initialize_without_bus_is_noop(self):
        peer_publisher._bus = None
        ok = peer_subscriber.initialize()
        self.assertFalse(ok)
        self.assertFalse(peer_subscriber.is_active())


class SubscriptionFlowTest(unittest.TestCase):
    def setUp(self):
        self.bus = _Bus()
        peer_publisher._bus = self.bus
        peer_publisher._host = "local"
        peer_subscriber.initialize(antenna_ref="UFO1", site_ref="DuPage")
        # Install a peer identity so snapshots() can attach it.
        self.bus.set_peers([
            PeerIdentity(host="h2", antenna_ref="UFO1", site_ref="DuPage"),
        ])

    def tearDown(self):
        peer_subscriber.shutdown()
        peer_publisher._bus = None
        peer_publisher._host = None

    def test_position_message_updates_snapshot(self):
        pos = schemas.PositionPayload(
            lat_deg=40.0, lon_deg=-90.0, alt_m=200.0,
        )
        self.bus.dispatch(
            "peppar-fix.h2.position",
            schemas.to_bytes(pos),
            from_host="h2",
        )
        snaps = peer_subscriber.snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].host, "h2")
        self.assertEqual(snaps[0].position.lat_deg, 40.0)
        # Identity picked up from bus.peers() via snapshots()
        self.assertEqual(snaps[0].identity.antenna_ref, "UFO1")

    def test_ztd_message_updates_snapshot(self):
        ztd = schemas.ZTDPayload(ztd_m=-0.274, ztd_sigma_mm=3)
        self.bus.dispatch(
            "peppar-fix.h2.ztd",
            schemas.to_bytes(ztd),
            from_host="h2",
        )
        snaps = peer_subscriber.snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertAlmostEqual(snaps[0].ztd.ztd_m, -0.274, places=6)

    def test_snapshots_include_self(self):
        """When the engine passes include_self=True with its
        build_self_snapshot, cohort computations see the local
        host alongside peers."""
        pos = schemas.PositionPayload(
            lat_deg=40.0, lon_deg=-90.0, alt_m=200.0,
        )
        self.bus.dispatch(
            "peppar-fix.h2.position", schemas.to_bytes(pos),
            from_host="h2",
        )
        self_snap = peer_subscriber.build_self_snapshot(
            antenna_ref="UFO1", site_ref="DuPage",
            lat_deg=40.001, lon_deg=-90.001, alt_m=201.0,
        )
        snaps = peer_subscriber.snapshots(
            include_self=True, self_snapshot=self_snap,
        )
        self.assertEqual(len(snaps), 2)
        self.assertEqual({s.host for s in snaps}, {"h2", "local"})


class CohortQueryTest(unittest.TestCase):
    """End-to-end cohort_* wrappers on top of a live subscriber."""

    def setUp(self):
        self.bus = _Bus()
        peer_publisher._bus = self.bus
        peer_publisher._host = "local"
        peer_subscriber.initialize(antenna_ref="UFO1", site_ref="DuPage")
        self.bus.set_peers([
            PeerIdentity(host="h2", antenna_ref="UFO1", site_ref="DuPage"),
            PeerIdentity(host="h3", antenna_ref="UFO1", site_ref="DuPage"),
        ])
        for host in ("h2", "h3"):
            pos = schemas.PositionPayload(
                lat_deg=40.0, lon_deg=-90.0, alt_m=200.0,
            )
            self.bus.dispatch(
                f"peppar-fix.{host}.position",
                schemas.to_bytes(pos), from_host=host,
            )
            ztd = schemas.ZTDPayload(ztd_m=-0.100)
            self.bus.dispatch(
                f"peppar-fix.{host}.ztd",
                schemas.to_bytes(ztd), from_host=host,
            )

    def tearDown(self):
        peer_subscriber.shutdown()
        peer_publisher._bus = None
        peer_publisher._host = None

    def test_cohort_median_position_with_peers(self):
        got = peer_subscriber.cohort_median_position("UFO1")
        self.assertIsNotNone(got)
        lat_m, lon_m, alt_m, n = got
        self.assertEqual((lat_m, lon_m, alt_m, n),
                         (40.0, -90.0, 200.0, 2))

    def test_cohort_median_ztd_with_peers(self):
        got = peer_subscriber.cohort_median_ztd("DuPage")
        self.assertIsNotNone(got)
        ztd_m, n = got
        self.assertAlmostEqual(ztd_m, -0.100, places=6)
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main()
