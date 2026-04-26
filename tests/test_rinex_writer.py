"""Tests for rinex_writer.RinexWriter — round-trip through rinex_reader.

Why this file exists: we need to produce RINEX OBS files the regression
harness (and PRIDE-PPPAR) can consume, for the cross-AC bug-vs-datum
investigation.  The cheapest correctness test is to write a synthetic
session with known observation values, parse it back through our
existing rinex_reader, and verify the values come back unchanged.
"""

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from peppar_fix.rinex_writer import RinexWriter
from regression.rinex_reader import parse_header, iter_epochs as parse_epochs


@pytest.fixture
def tmp_rnx(tmp_path):
    return tmp_path / "session.rnx"


def _make_writer(tmp_rnx):
    return RinexWriter(
        tmp_rnx,
        marker_name="UFO1",
        approx_xyz=(157544.0, -4756190.0, 4232770.0),
        antenna_type="SFESPK6618H     NONE",
        receiver_model="ZED-F9T",
        receiver_fw="TIM 2.25",
        receiver_serial="3b41fabd5b",
        antenna_serial="UFO1-A",
        observer="test",
        agency="test-lab",
        interval_s=1.0,
    )


def test_header_round_trip(tmp_rnx):
    """Write minimal session, parse header back, check fields."""
    w = _make_writer(tmp_rnx)
    epoch = datetime(2026, 4, 25, 22, 0, 0, tzinfo=timezone.utc)
    raw_obs = {
        "G16": {
            "GPS-L1CA": {"pr": 23000000.0, "cp": 120500000.0, "cno": 45.0,
                         "half_cyc": True, "lock_ms": 5000},
            "GPS-L5Q":  {"pr": 23000000.5, "cp": 90100000.0, "cno": 42.0,
                         "half_cyc": True, "lock_ms": 5000},
        },
    }
    w.write_epoch(epoch, raw_obs)
    w.close()

    hdr = parse_header(tmp_rnx)
    assert hdr.version == "3.04"
    assert hdr.marker == "UFO1"
    assert hdr.approx_xyz == pytest.approx((157544.0, -4756190.0, 4232770.0))
    assert "G" in hdr.sys_obs_types
    # GPS column declarations include both L1C and L5Q phase types
    assert "L1C" in hdr.sys_obs_types["G"]
    assert "L5Q" in hdr.sys_obs_types["G"]


def test_observation_values_round_trip(tmp_rnx):
    """Values written should come back via the reader within float precision."""
    w = _make_writer(tmp_rnx)
    epoch = datetime(2026, 4, 25, 22, 0, 0, tzinfo=timezone.utc)
    raw_obs = {
        "G16": {
            "GPS-L1CA": {"pr": 23456789.123, "cp": 123456789.456,
                         "cno": 45.0, "half_cyc": True, "lock_ms": 5000},
        },
        "E04": {
            "GAL-E1C":  {"pr": 24112233.987, "cp": 126987654.321,
                         "cno": 41.0, "half_cyc": True, "lock_ms": 5000},
            "GAL-E5aQ": {"pr": 24112234.654, "cp": 94888777.222,
                         "cno": 39.0, "half_cyc": True, "lock_ms": 5000},
        },
    }
    w.write_epoch(epoch, raw_obs)
    w.close()

    epochs = list(parse_epochs(tmp_rnx))
    assert len(epochs) == 1
    obs = epochs[0].obs

    # G16 L1CA: PR + CP within 1 mm (RINEX has 3-decimal precision)
    g16_pr = obs["G16"]["C1C"][0]
    g16_cp = obs["G16"]["L1C"][0]
    assert g16_pr == pytest.approx(23456789.123, abs=1e-3)
    assert g16_cp == pytest.approx(123456789.456, abs=1e-3)

    # E04 dual-band
    e04_pr1 = obs["E04"]["C1C"][0]
    e04_pr2 = obs["E04"]["C5Q"][0]
    e04_cp1 = obs["E04"]["L1C"][0]
    e04_cp2 = obs["E04"]["L5Q"][0]
    assert e04_pr1 == pytest.approx(24112233.987, abs=1e-3)
    assert e04_pr2 == pytest.approx(24112234.654, abs=1e-3)
    assert e04_cp1 == pytest.approx(126987654.321, abs=1e-3)
    assert e04_cp2 == pytest.approx(94888777.222, abs=1e-3)


def test_lli_set_on_lock_drop(tmp_rnx):
    """A drop in lock_ms between epochs should set LLI bit 0."""
    w = _make_writer(tmp_rnx)
    e1 = datetime(2026, 4, 25, 22, 0, 0, tzinfo=timezone.utc)
    e2 = datetime(2026, 4, 25, 22, 0, 1, tzinfo=timezone.utc)
    obs = {
        "G16": {"GPS-L1CA": {"pr": 23e6, "cp": 1.2e8, "cno": 45.0,
                              "half_cyc": True, "lock_ms": 5000}},
    }
    w.write_epoch(e1, obs)
    obs2 = {
        "G16": {"GPS-L1CA": {"pr": 23e6, "cp": 1.2e8, "cno": 45.0,
                              "half_cyc": True, "lock_ms": 100}},  # dropped
    }
    w.write_epoch(e2, obs2)
    w.close()

    epochs = list(parse_epochs(tmp_rnx))
    assert len(epochs) == 2
    # Epoch 1 LLI should be 0 (no prior); epoch 2 LLI should be 1
    lli_e1 = epochs[0].obs["G16"]["L1C"][1]
    lli_e2 = epochs[1].obs["G16"]["L1C"][1]
    assert lli_e1 == 0
    assert lli_e2 == 1


def test_half_cyc_sets_lli_bit_1(tmp_rnx):
    """half_cyc=False sets LLI bit 1 (half-cycle ambiguity)."""
    w = _make_writer(tmp_rnx)
    epoch = datetime(2026, 4, 25, 22, 0, 0, tzinfo=timezone.utc)
    obs = {
        "G16": {"GPS-L1CA": {"pr": 23e6, "cp": 1.2e8, "cno": 45.0,
                              "half_cyc": False, "lock_ms": 5000}},
    }
    w.write_epoch(epoch, obs)
    w.close()
    epochs = list(parse_epochs(tmp_rnx))
    lli = epochs[0].obs["G16"]["L1C"][1]
    assert lli & 2, f"LLI bit 1 should be set; got LLI={lli}"


def test_unknown_signal_is_skipped(tmp_rnx):
    """An unknown internal sig name gets dropped, doesn't crash."""
    w = _make_writer(tmp_rnx)
    epoch = datetime(2026, 4, 25, 22, 0, 0, tzinfo=timezone.utc)
    raw_obs = {
        "G16": {
            "GPS-L1CA": {"pr": 23e6, "cp": 1.2e8, "cno": 45.0,
                         "half_cyc": True, "lock_ms": 5000},
            "GPS-NONESUCH": {"pr": 1.0, "cp": 2.0, "cno": 0.0,
                             "half_cyc": True, "lock_ms": 0},
        },
    }
    w.write_epoch(epoch, raw_obs)
    w.close()
    epochs = list(parse_epochs(tmp_rnx))
    # Known signal still came through
    assert epochs[0].obs["G16"]["L1C"][0] == pytest.approx(1.2e8, abs=1e-3)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
