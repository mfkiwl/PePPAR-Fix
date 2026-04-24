"""Envelope + topic-match unit tests."""

from __future__ import annotations

import json
import unittest

from peppar_bus._envelope import decode, encode, match


class EnvelopeTest(unittest.TestCase):
    def test_roundtrip_structured_payload(self):
        payload = json.dumps({"lat": 40.1, "lon": -90.2}).encode("utf-8")
        wire = encode("timehat", "peppar-fix.timehat.position", payload)
        host, topic, got = decode(wire)
        self.assertEqual(host, "timehat")
        self.assertEqual(topic, "peppar-fix.timehat.position")
        self.assertEqual(json.loads(got.decode("utf-8")),
                         {"lat": 40.1, "lon": -90.2})

    def test_encoded_line_is_newline_terminated(self):
        """JSON Lines convention: one message, one newline."""
        wire = encode("h", "t", b'{"k":1}')
        self.assertTrue(wire.endswith(b"\n"))
        # Exactly one newline, and it's at the end.
        self.assertEqual(wire.count(b"\n"), 1)

    def test_empty_payload_allowed(self):
        wire = encode("h", "t", b"")
        host, topic, got = decode(wire)
        self.assertEqual(host, "h")
        self.assertEqual(topic, "t")
        self.assertEqual(got, b"")

    def test_decode_rejects_malformed(self):
        with self.assertRaises(ValueError):
            decode(b"")
        with self.assertRaises(ValueError):
            decode(b'{"t": "x"}\n')  # missing host
        with self.assertRaises(ValueError):
            decode(b"not json\n")


class TopicMatchTest(unittest.TestCase):
    def test_literal_match(self):
        self.assertTrue(match("peppar-fix.timehat.position",
                              "peppar-fix.timehat.position"))
        self.assertFalse(match("peppar-fix.timehat.position",
                               "peppar-fix.clkpoc3.position"))

    def test_star_matches_one_segment(self):
        self.assertTrue(match("peppar-fix.*.position",
                              "peppar-fix.timehat.position"))
        self.assertTrue(match("peppar-fix.*.position",
                              "peppar-fix.clkpoc3.position"))
        # Two segments where pattern expects one — must not match.
        self.assertFalse(match("peppar-fix.*.position",
                               "peppar-fix.timehat.sv.position"))

    def test_double_star_matches_zero_or_more(self):
        self.assertTrue(match("peppar-fix.**",
                              "peppar-fix.timehat.sv.state"))
        self.assertTrue(match("peppar-fix.**",
                              "peppar-fix.timehat.heartbeat"))
        self.assertTrue(match("peppar-fix.**",
                              "peppar-fix.timehat"))
        self.assertFalse(match("peppar-fix.**",
                               "not-peppar-fix.x"))

    def test_host_wildcard(self):
        self.assertTrue(match("peppar-fix.*.heartbeat",
                              "peppar-fix.timehat.heartbeat"))
        self.assertFalse(match("peppar-fix.timehat.*",
                               "peppar-fix.clkpoc3.position"))


if __name__ == "__main__":
    unittest.main()
