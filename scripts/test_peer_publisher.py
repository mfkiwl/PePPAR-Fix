"""Unit tests for peer_publisher (engine-side helper).

Covers:
- initialize() spec parsing (enables / disables / errors)
- every publish_* helper no-ops when disabled
- every publish_* helper routes through the configured bus when
  enabled, with the right topic shape and a decodable payload
"""

from __future__ import annotations

import unittest

import peer_publisher
from peppar_bus import PeerBus, schemas


class _Capture(PeerBus):
    """In-memory bus that captures publishes for inspection."""

    def __init__(self):
        self.published: list[tuple[str, bytes]] = []

    def publish(self, topic, payload):
        self.published.append((topic, payload))

    def subscribe(self, topic_pattern, callback):
        pass

    def peers(self):
        return []

    def close(self):
        pass


class InitializeTest(unittest.TestCase):
    def tearDown(self):
        peer_publisher.shutdown()

    def test_none_disables(self):
        self.assertFalse(peer_publisher.initialize("none", host="h"))
        self.assertFalse(peer_publisher.is_active())

    def test_empty_disables(self):
        self.assertFalse(peer_publisher.initialize("", host="h"))

    def test_unknown_spec_logs_and_disables(self):
        """An unrecognized transport spec must NOT raise — it
        degrades to 'disabled' so the engine still boots."""
        self.assertFalse(peer_publisher.initialize("quantum:bridge", host="h"))
        self.assertFalse(peer_publisher.is_active())

    def test_udp_multicast_enables(self):
        ok = peer_publisher.initialize(
            "udp-multicast", host="test-host", antenna_ref="UFO1",
        )
        self.assertTrue(ok)
        self.assertTrue(peer_publisher.is_active())
        peer_publisher.shutdown()
        self.assertFalse(peer_publisher.is_active())


class PublishRoutingTest(unittest.TestCase):
    """With a captured bus installed, each publish_* helper must
    produce the right topic shape and a valid payload."""

    def setUp(self):
        self.bus = _Capture()
        # Install the capture bus directly.
        peer_publisher._bus = self.bus
        peer_publisher._host = "test-host"

    def tearDown(self):
        peer_publisher._bus = None
        peer_publisher._host = None

    def test_publish_position(self):
        peer_publisher.publish_position(
            ant_pos_est_state="anchored",
            lat_deg=40.1, lon_deg=-90.2, alt_m=198.247,
            position_sigma_m=0.023, worst_sigma_m=1.5,
            reached_anchored=True,
        )
        self.assertEqual(len(self.bus.published), 1)
        topic, payload = self.bus.published[0]
        self.assertEqual(topic, "peppar-fix.test-host.position")
        got = schemas.from_bytes(schemas.PositionPayload, payload)
        self.assertEqual(got.ant_pos_est_state, "anchored")
        self.assertEqual(got.reached_anchored, True)

    def test_publish_slip_event(self):
        peer_publisher.publish_slip_event(
            sv="G08", reasons=["gf_jump", "mw_jump"], conf="HIGH",
            elev_deg=45.2, lock_duration_ms=12000,
            gf_jump_m=0.08, mw_jump_cyc=1.3,
        )
        topic, payload = self.bus.published[0]
        self.assertEqual(topic, "peppar-fix.test-host.slip-event.G08")
        got = schemas.from_bytes(schemas.SlipEventPayload, payload)
        self.assertEqual(got.sv, "G08")
        self.assertEqual(got.conf, "HIGH")
        self.assertEqual(got.reasons, ["gf_jump", "mw_jump"])

    def test_publish_integer_fix(self):
        peer_publisher.publish_integer_fix(
            sv="E11", n_wl=-3, n_nl=42, state="ANCHORING",
        )
        topic, payload = self.bus.published[0]
        self.assertEqual(topic, "peppar-fix.test-host.integer-fix.E11")
        got = schemas.from_bytes(schemas.IntegerFixPayload, payload)
        self.assertEqual((got.n_wl, got.n_nl, got.state), (-3, 42, "ANCHORING"))

    def test_publish_ztd_tide_sv_streams(self):
        peer_publisher.publish_ztd(ztd_m=-0.274, ztd_sigma_mm=3)
        peer_publisher.publish_tide(total_mm=135, u_mm=131)
        peer_publisher.publish_sv_state(
            sv_states={"G05": "FLOATING"}, nl_capable="GE",
        )
        peer_publisher.publish_streams(
            ssr_mount="SSRA00CNE0", eph_mount="BCEP00BKG0",
        )
        topics = [t for t, _ in self.bus.published]
        self.assertEqual(topics, [
            "peppar-fix.test-host.ztd",
            "peppar-fix.test-host.tide",
            "peppar-fix.test-host.sv-state",
            "peppar-fix.test-host.streams",
        ])


class DisabledNoopTest(unittest.TestCase):
    """When bus is not installed, every publish_* must silently
    do nothing — no exceptions, no state mutation."""

    def setUp(self):
        peer_publisher._bus = None
        peer_publisher._host = None

    def test_position_noop(self):
        peer_publisher.publish_position(
            ant_pos_est_state="x",
            lat_deg=None, lon_deg=None, alt_m=None,
            position_sigma_m=None,
        )
        # No exception = pass.

    def test_all_helpers_noop(self):
        peer_publisher.publish_ztd(ztd_m=0.1)
        peer_publisher.publish_tide(total_mm=100)
        peer_publisher.publish_sv_state(sv_states={})
        peer_publisher.publish_integer_fix(
            sv="X", n_wl=0, n_nl=0, state="TRACKING",
        )
        peer_publisher.publish_slip_event(
            sv="X", reasons=[], conf="LOW",
        )
        peer_publisher.publish_streams(ssr_mount=None, eph_mount=None)


if __name__ == "__main__":
    unittest.main()
