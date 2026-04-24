"""Unit tests for LogToBusBridge.

Uses a fake in-memory PeerBus so we can assert on publishes
without spinning up a real socket.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path

from peppar_bus import PeerBus, PeerMessage

from peppar_mon.bridge import LogToBusBridge
from peppar_mon.log_reader import LogReader


class FakeBus(PeerBus):
    """In-process capture.  Collects publishes for test inspection."""

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


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.log = Path(self._tmpdir.name) / "engine.log"
        self.bus = FakeBus()

    def _wait_for_publish(self, predicate, timeout_s=2.0):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_position_published_when_log_line_lands(self):
        """Write one AntPosEst line to the log → bridge publishes
        a position message on the bus."""
        self.log.write_text(
            "2026-04-21 07:00:00,000 INFO [STATE] AntPosEst: → "
            "anchored (initial)\n"
            "2026-04-21 17:48:07,703 INFO   [AntPosEst 4210] "
            "positionσ=0.023m pos=(40.123456, -90.123456, 198.2) "
            "ZTD=+274±3mm tide=135mm(U+130) worstσ=1.5m\n"
        )
        reader = LogReader(self.log)
        reader.start()
        self.addCleanup(reader.stop)
        # Small poll interval so the test is fast.
        bridge = LogToBusBridge(
            reader=reader, bus=self.bus, host="test-host",
            poll_interval_s=0.05,
        )
        bridge.start()
        self.addCleanup(bridge.stop)
        # Wait for position publish to land.
        self.assertTrue(
            self._wait_for_publish(
                lambda: any("position" in t for t, _ in self.bus.published)
            ),
            msg="bridge never published a position message"
        )
        # Every field should get its own publish.
        topics = {t for t, _ in self.bus.published}
        self.assertIn("peppar-fix.test-host.position", topics)
        self.assertIn("peppar-fix.test-host.ztd", topics)
        self.assertIn("peppar-fix.test-host.tide", topics)

    def test_no_publish_when_nothing_changed(self):
        """After an initial publish, an unchanged LogState must not
        produce duplicate publishes — bridge is change-detecting."""
        self.log.write_text(
            "2026-04-21 07:00:00,000 INFO [STATE] AntPosEst: → "
            "anchored (initial)\n"
            "2026-04-21 17:48:07,703 INFO   [AntPosEst 4210] "
            "positionσ=0.023m pos=(40.0, -90.0, 200.0) ZTD=+0±1mm "
            "tide=100mm(U+99) worstσ=1.0m\n"
        )
        reader = LogReader(self.log)
        reader.start()
        self.addCleanup(reader.stop)
        bridge = LogToBusBridge(
            reader=reader, bus=self.bus, host="test-host",
            poll_interval_s=0.05,
        )
        bridge.start()
        self.addCleanup(bridge.stop)
        # Wait for initial publish.
        self.assertTrue(
            self._wait_for_publish(
                lambda: any("position" in t for t, _ in self.bus.published)
            )
        )
        baseline = len(self.bus.published)
        # Let the polling loop run another second — no new log lines,
        # so no new publishes expected.
        time.sleep(0.3)
        self.assertEqual(len(self.bus.published), baseline)


if __name__ == "__main__":
    unittest.main()
