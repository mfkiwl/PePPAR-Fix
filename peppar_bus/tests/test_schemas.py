"""Schema serialization round-trips + forward/backward compatibility."""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass

from peppar_bus.schemas import (
    HeartbeatPayload,
    IntegerFixPayload,
    PositionPayload,
    SCHEMA_VERSION,
    SlipEventPayload,
    StreamsPayload,
    SvStatePayload,
    TidePayload,
    ZTDPayload,
    from_bytes,
    to_bytes,
)


class RoundtripTest(unittest.TestCase):
    def test_position_roundtrip(self):
        p = PositionPayload(
            ts_mono_ns=123456789,
            ts_gps_iso="2026-04-24T02:15:00.103Z",
            ant_pos_est_state="anchored",
            lat_deg=40.12345678,
            lon_deg=-90.12345678,
            alt_m=198.247,
            position_sigma_m=0.023,
            worst_sigma_m=1.45,
            reached_anchored=True,
        )
        got = from_bytes(PositionPayload, to_bytes(p))
        self.assertEqual(got, p)

    def test_sv_state_roundtrip(self):
        p = SvStatePayload(
            ts_mono_ns=1,
            sv_states={"G05": "FLOATING", "G10": "ANCHORED"},
            nl_capable="GE",
        )
        got = from_bytes(SvStatePayload, to_bytes(p))
        self.assertEqual(got, p)

    def test_integer_fix_with_nones(self):
        """WL-only hosts have n_nl=None; must roundtrip cleanly."""
        p = IntegerFixPayload(
            ts_mono_ns=1, sv="G10", n_wl=-18, n_nl=None, state="CONVERGING",
        )
        got = from_bytes(IntegerFixPayload, to_bytes(p))
        self.assertEqual(got, p)

    def test_slip_event_multi_detector(self):
        """A HIGH-confidence slip (two detectors fired) carries a
        reasons list + both jump magnitudes."""
        p = SlipEventPayload(
            ts_mono_ns=5, sv="G08",
            reasons=["gf_jump", "mw_jump"], conf="HIGH",
            elev_deg=45.2, lock_duration_ms=12000,
            gf_jump_m=0.08, mw_jump_cyc=1.3,
        )
        got = from_bytes(SlipEventPayload, to_bytes(p))
        self.assertEqual(got, p)
        self.assertEqual(got.reasons, ["gf_jump", "mw_jump"])

    def test_slip_event_low_conf_minimal(self):
        """LOW-confidence solo events set only the fields their
        detector populates — others stay None."""
        p = SlipEventPayload(
            ts_mono_ns=10, sv="E13", reasons=["mw_jump"], conf="LOW",
            mw_jump_cyc=0.9,
        )
        got = from_bytes(SlipEventPayload, to_bytes(p))
        self.assertEqual(got, p)
        self.assertIsNone(got.gf_jump_m)
        self.assertIsNone(got.elev_deg)


class CompatibilityTest(unittest.TestCase):
    def test_forward_compat_ignores_unknown_keys(self):
        """A future schema version may add fields.  Today's decoder
        must ignore unknown keys, not crash."""
        wire = json.dumps({
            "schema_version": SCHEMA_VERSION + 1,
            "ts_mono_ns": 5,
            "ztd_m": 0.274,
            "ztd_sigma_mm": 3,
            "future_field_we_dont_know": "ignored",
        }).encode("utf-8")
        got = from_bytes(ZTDPayload, wire)
        self.assertEqual(got.ztd_m, 0.274)
        self.assertEqual(got.ztd_sigma_mm, 3)

    def test_backward_compat_missing_keys_take_defaults(self):
        """An older emitter that didn't know about a new field.  The
        decoder should fill in the dataclass default, not crash."""
        wire = json.dumps({
            "schema_version": 0,  # pretend ancient
            "ts_mono_ns": 5,
            # tide.total_mm / u_mm missing
        }).encode("utf-8")
        got = from_bytes(TidePayload, wire)
        self.assertIsNone(got.total_mm)
        self.assertIsNone(got.u_mm)
        self.assertEqual(got.ts_mono_ns, 5)


class SmokeTest(unittest.TestCase):
    def test_every_payload_has_schema_version(self):
        for cls in (HeartbeatPayload, PositionPayload, SvStatePayload,
                    IntegerFixPayload, ZTDPayload, TidePayload,
                    SlipEventPayload, StreamsPayload):
            p = cls()
            self.assertEqual(p.schema_version, SCHEMA_VERSION,
                             msg=f"{cls.__name__} schema_version wrong")
            # to_bytes must succeed on a default-constructed instance.
            to_bytes(p)


if __name__ == "__main__":
    unittest.main()
