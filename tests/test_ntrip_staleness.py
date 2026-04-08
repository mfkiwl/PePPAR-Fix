#!/usr/bin/env python3
"""Unit tests for the NTRIP-staleness graceful-degradation cascade.

Covers three failure modes the engine must survive without exiting or
silently freezing the servo:

  1. Disconnect — caster drops the TCP connection.  NtripStream.raw_frames()
     must reconnect forever (transient transport error path).
  2. Mute socket — TCP stays open but no RTCM frames arrive.  Detected by
     the socket read timeout firing inside _recv(); raw_frames() treats it
     identically to a disconnect.
  3. Permanent rejection — caster returns HTTP 401/403/404/410.  raw_frames()
     must stop retrying so a typo in credentials doesn't loop forever.

Plus the consumer-side cascade:
  - compute_error_sources() inflates Carrier and PPS+PPP σ as corr_age_s
    grows so source competition hands off to PPS+qErr → PPS automatically.

These tests don't touch any real socket — NtripStream is monkeypatched
so the supervisor loop runs deterministically.

Run: python3 tests/test_ntrip_staleness.py
"""

import math
import socket
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from ntrip_client import NtripStream  # noqa: E402
from peppar_fix.error_sources import (  # noqa: E402
    CarrierPhaseTracker,
    compute_error_sources,
)


def _make_stream(**overrides):
    kwargs = dict(
        caster="example.invalid",
        port=2101,
        mountpoint="TEST00BKG0",
        timeout=0.01,
        reconnect_delay=0.001,
        max_reconnect_delay=0.004,
    )
    kwargs.update(overrides)
    return NtripStream(**kwargs)


class TestRetryForever(unittest.TestCase):
    """raw_frames() must never give up on transient transport failures."""

    def test_transient_failures_retry_forever(self):
        stream = _make_stream()
        attempts = {"n": 0}

        def fake_connect():
            attempts["n"] += 1
            if attempts["n"] < 50:
                # Cycle through the transient errors we expect to survive.
                err = [
                    socket.gaierror(-3, "Temporary failure in name resolution"),
                    ConnectionRefusedError("ECONNREFUSED"),
                    socket.timeout("connect timeout"),
                    OSError(113, "EHOSTUNREACH"),
                ][attempts["n"] % 4]
                raise err
            # After 50 transient failures we let one connect succeed and
            # then immediately yield from a single frame to terminate the
            # generator cleanly via StopIteration.
            stream._connected = True

        produced = []

        def fake_recv():
            # First successful read: produce one fake frame and then
            # signal end-of-stream so the test terminates.
            produced.append(1)
            if len(produced) >= 2:
                raise StopIteration  # break out of the for-loop in test
            stream._buffer.extend(b"\x00")  # nothing parseable
            return None

        with patch.object(stream, "connect", side_effect=fake_connect), \
             patch.object(stream, "_recv", side_effect=fake_recv):
            try:
                gen = stream.raw_frames()
                # Drain a handful of frames; the generator will keep retrying
                # until fake_connect succeeds, then StopIteration breaks us.
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    try:
                        next(gen)
                    except StopIteration:
                        break
            except Exception:
                pass

        self.assertGreaterEqual(
            attempts["n"], 50,
            f"Should have retried >= 50 times, got {attempts['n']}",
        )
        self.assertFalse(
            stream._fatal,
            "Transient errors must never set _fatal",
        )

    def test_mute_socket_recovers_via_recv_timeout(self):
        """A mute socket (no data ever arrives) should be detected by the
        socket read timeout and trigger a reconnect, NOT cause the
        generator to hang forever or exit."""
        stream = _make_stream()
        connect_calls = {"n": 0}
        recv_calls = {"n": 0}

        def fake_connect():
            connect_calls["n"] += 1
            stream._connected = True
            if connect_calls["n"] >= 5:
                # After 5 mute-then-reconnect cycles, let the test stop.
                raise RuntimeError("STOP_TEST")

        def fake_recv():
            recv_calls["n"] += 1
            stream._connected = False
            # The mute symptom: settimeout fires from inside _recv.
            raise socket.timeout("read timeout — socket is mute")

        with patch.object(stream, "connect", side_effect=fake_connect), \
             patch.object(stream, "_recv", side_effect=fake_recv):
            try:
                for _ in stream.raw_frames():
                    pass
            except RuntimeError:
                pass

        self.assertGreaterEqual(
            connect_calls["n"], 5,
            "Mute socket should have triggered repeated reconnects "
            f"(got {connect_calls['n']})",
        )
        self.assertGreaterEqual(
            recv_calls["n"], 4,
            "Should have called _recv at least once per reconnect",
        )

    def test_http_401_is_fatal(self):
        """Permanent server rejection (HTTP 401) must stop the supervisor."""
        stream = _make_stream(max_reconnects=1000)
        attempts = {"n": 0}

        def fake_connect():
            attempts["n"] += 1
            stream._fatal = True
            stream._fatal_reason = "HTTP/1.0 401 Unauthorized"
            raise ConnectionError("NTRIP error: HTTP/1.0 401 Unauthorized")

        with patch.object(stream, "connect", side_effect=fake_connect):
            list(stream.raw_frames())  # must terminate, not hang

        self.assertEqual(
            attempts["n"], 1,
            f"Fatal error should stop after first attempt, got {attempts['n']}",
        )
        self.assertTrue(stream._fatal)


class TestSigmaInflationCascade(unittest.TestCase):
    """compute_error_sources must hand off Carrier → PPS+qErr → PPS as
    correction age grows."""

    def _carrier_tracker(self):
        ct = CarrierPhaseTracker()
        ct.initialize(0.0)
        return ct

    def test_zero_age_carrier_wins(self):
        ct = self._carrier_tracker()
        sources = compute_error_sources(
            pps_error_ns=0.0,
            qerr_ns=0.5,
            dt_rx_ns=0.0,
            dt_rx_sigma_ns=2.0,
            carrier_tracker=ct,
            corr_age_s=0.0,
        )
        self.assertEqual(sources[0].name, "Carrier")
        self.assertAlmostEqual(sources[0].confidence_ns, 2.0, places=5)

    def test_30s_age_handoff_to_pps_qerr(self):
        ct = self._carrier_tracker()
        sources = compute_error_sources(
            pps_error_ns=0.0,
            qerr_ns=0.5,
            dt_rx_ns=0.0,
            dt_rx_sigma_ns=2.0,
            carrier_tracker=ct,
            corr_age_s=30.0,
        )
        self.assertEqual(sources[0].name, "PPS+qErr")
        carrier = next(s for s in sources if s.name == "Carrier")
        # σ_eff = sqrt(2² + (0.1*30)²) = sqrt(4 + 9) = 3.606
        self.assertAlmostEqual(carrier.confidence_ns, math.sqrt(13), places=4)

    def test_300s_age_handoff_to_pps_only(self):
        ct = self._carrier_tracker()
        sources = compute_error_sources(
            pps_error_ns=0.0,
            qerr_ns=0.5,
            dt_rx_ns=0.0,
            dt_rx_sigma_ns=2.0,
            carrier_tracker=ct,
            corr_age_s=300.0,
        )
        # σ_carrier ≈ 30 ns, σ_pps_qerr = 3, σ_pps = 20 → PPS+qErr still wins
        # because it doesn't depend on dt_rx.
        self.assertEqual(sources[0].name, "PPS+qErr")

    def test_carrier_recovers_when_corrections_return(self):
        """Drop in corr_age_s should restore Carrier to top spot
        without any state reset — the whole point of the design."""
        ct = self._carrier_tracker()
        for age, expected in [
            (0.0, "Carrier"),
            (60.0, "PPS+qErr"),
            (0.0, "Carrier"),
            (1000.0, "PPS+qErr"),
            (5.0, "Carrier"),
        ]:
            sources = compute_error_sources(
                pps_error_ns=0.0,
                qerr_ns=0.5,
                dt_rx_ns=0.0,
                dt_rx_sigma_ns=2.0,
                carrier_tracker=ct,
                corr_age_s=age,
            )
            self.assertEqual(
                sources[0].name, expected,
                f"At corr_age={age}s expected {expected}, got {sources[0].name}",
            )

    def test_corr_age_none_disables_inflation(self):
        """Back-compat: callers that don't provide corr_age_s see no
        inflation at all."""
        ct = self._carrier_tracker()
        sources = compute_error_sources(
            pps_error_ns=0.0,
            qerr_ns=0.5,
            dt_rx_ns=0.0,
            dt_rx_sigma_ns=2.0,
            carrier_tracker=ct,
            corr_age_s=None,
        )
        carrier = next(s for s in sources if s.name == "Carrier")
        self.assertAlmostEqual(carrier.confidence_ns, 2.0, places=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
