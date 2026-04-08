#!/usr/bin/env python3
"""Tests for the stock-igc 1 Hz PEROUT frequency-mode bug detector.

The detector decides whether to nudge PEROUT period from
1_000_000_000 → 999_999_999 ns to dodge the stock igc special case
that drops 1 Hz requests into hardware frequency mode (which then
produces a 500-ms-shifted output).  We don't want to apply the
workaround on the patched igc module or on any other driver.

These tests use a temporary fake sysfs tree so they don't depend on
which kernel/driver the runner happens to be on.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from peppar_fix.ptp_device import _stock_igc_freq_mode_workaround_needed  # noqa: E402


class FakeSysfs:
    """Build a temporary /sys/class/ptp/ptpN/device/driver layout."""

    def __init__(self, root: Path, ptp_index: int, driver_name: str | None,
                 patched_igc_param: bool):
        self.root = root
        self.ptp_path = f"/dev/ptp{ptp_index}"
        ptp_dir = root / "sys" / "class" / "ptp" / f"ptp{ptp_index}"
        ptp_dir.mkdir(parents=True)
        if driver_name is not None:
            # /sys/class/ptp/ptpN/device → fake PCI device
            pci_dir = root / "sys" / "devices" / "pci0" / "0000:01:00.0"
            pci_dir.mkdir(parents=True)
            os.symlink(str(pci_dir), str(ptp_dir / "device"))
            # /sys/class/ptp/ptpN/device/driver → /sys/bus/.../<driver_name>
            driver_dir = root / "sys" / "bus" / "pci" / "drivers" / driver_name
            driver_dir.mkdir(parents=True)
            os.symlink(str(driver_dir), str(pci_dir / "driver"))
        # /sys/module/igc/parameters/edge_check_delay_us — only when patched
        igc_module = root / "sys" / "module" / "igc" / "parameters"
        igc_module.mkdir(parents=True)
        if patched_igc_param:
            (igc_module / "edge_check_delay_us").write_text("20\n")

    def patch_paths(self):
        """Patch the absolute path lookups inside the detector."""
        sys_module_path = (
            self.root / "sys" / "module" / "igc" / "parameters" / "edge_check_delay_us"
        )

        real_readlink = os.readlink
        real_exists = os.path.exists

        def fake_readlink(path):
            if path.startswith("/sys/class/ptp/"):
                rest = path[len("/sys/class/ptp/"):]
                return real_readlink(str(self.root / "sys" / "class" / "ptp" / rest))
            return real_readlink(path)

        def fake_exists(path):
            if path == "/sys/module/igc/parameters/edge_check_delay_us":
                return real_exists(str(sys_module_path))
            return real_exists(path)

        return patch.multiple(
            "os",
            readlink=fake_readlink,
        ), patch("os.path.exists", side_effect=fake_exists)


class TestStockIgcDetection(unittest.TestCase):
    def _check(self, driver, patched, ptp_index=0):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = FakeSysfs(Path(tmpdir), ptp_index, driver, patched)
            readlink_patch, exists_patch = fake.patch_paths()
            with readlink_patch, exists_patch:
                return _stock_igc_freq_mode_workaround_needed(fake.ptp_path)

    def test_stock_igc_needs_workaround(self):
        """Stock igc → True"""
        self.assertTrue(self._check("igc", patched=False))

    def test_patched_igc_does_not_need_workaround(self):
        """Patched igc (TimeHAT) → False"""
        self.assertFalse(self._check("igc", patched=True))

    def test_ice_driver_does_not_need_workaround(self):
        """E810 / ice driver doesn't have the bug → False"""
        self.assertFalse(self._check("ice", patched=False))
        # Also False if patched igc happens to be loaded too
        self.assertFalse(self._check("ice", patched=True))

    def test_e1000e_does_not_need_workaround(self):
        """Onboard motherboard NICs → False"""
        self.assertFalse(self._check("e1000e", patched=False))

    def test_macb_does_not_need_workaround(self):
        """Raspberry Pi onboard NIC → False"""
        self.assertFalse(self._check("macb", patched=False))

    def test_missing_driver_returns_false(self):
        """Conservative fallback when sysfs is unexpected → False"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "sys" / "class" / "ptp" / "ptp0").mkdir(parents=True)
            # No 'device' symlink — readlink will fail
            real_readlink = os.readlink

            def fake_readlink(path):
                if path.startswith("/sys/class/ptp/"):
                    return real_readlink(
                        str(Path(tmpdir) / "sys" / "class" / "ptp" / path[len("/sys/class/ptp/"):])
                    )
                return real_readlink(path)

            with patch("os.readlink", side_effect=fake_readlink):
                self.assertFalse(_stock_igc_freq_mode_workaround_needed("/dev/ptp0"))

    def test_garbage_path_returns_false(self):
        """Non-/dev/ptpN paths → False, no exception"""
        self.assertFalse(_stock_igc_freq_mode_workaround_needed("/dev/null"))
        self.assertFalse(_stock_igc_freq_mode_workaround_needed("not_a_path"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
