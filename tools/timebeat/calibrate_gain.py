#!/usr/bin/env python3
"""calibrate_gain.py — Calibrate ClockMatrix FOD_FREQ gain and phase status.

Applies a series of known frequency offsets to DPLL_3 via FOD_FREQ,
measures the actual drift rate using DPLL_0 phase status, and reports
the gain factor (output_ppb / commanded_ppb).

Must be run on the ClockMatrix host (otcBob1) with Timebeat stopped.
TICC verification is done separately on PiPuss.

Usage:
    # Stop Timebeat first:
    sudo systemctl stop timebeat

    # Run calibration (prints results to stdout):
    python3 calibrate_gain.py --bus 15

    # With custom offsets:
    python3 calibrate_gain.py --bus 15 --offsets 0,10,50,100,500,1000,-100
"""

import argparse
import json
import sys
import time

try:
    import smbus2
except ImportError:
    sys.exit("smbus2 not installed.  pip install smbus2")


# 8A34002 register addresses (confirmed on otcBob1)
DPLL3_MODE = 0xC4B7
DPLL3_FOD_FREQ = 0xC6D8
DPLL0_PHASE_STATUS = 0xC03C + 0xDC  # = 0xC118
DPLL3_STATUS = 0xC03C + 0x1B  # = 0xC057

# Nominal FOD values
M_NOM = 32767500000000
N_NOM = 65535

MODE_PLL = 0x00
MODE_WRITE_FREQ = 0x02


class ClockMatrixCal:
    def __init__(self, bus_num, addr=0x58):
        self.bus = smbus2.SMBus(bus_num)
        self.addr = addr

    def close(self):
        self.bus.close()

    def read(self, reg16, nbytes):
        hi = (reg16 >> 8) & 0xFF
        lo = reg16 & 0xFF
        msg_w = smbus2.i2c_msg.write(self.addr, [hi, lo])
        msg_r = smbus2.i2c_msg.read(self.addr, nbytes)
        self.bus.i2c_rdwr(msg_w, msg_r)
        return bytes(msg_r)

    def write(self, reg16, data):
        hi = (reg16 >> 8) & 0xFF
        lo = reg16 & 0xFF
        msg = smbus2.i2c_msg.write(self.addr, [hi, lo] + list(data))
        self.bus.i2c_rdwr(msg)

    def read_phase(self):
        """Read DPLL_0 phase status as signed 64-bit."""
        data = self.read(DPLL0_PHASE_STATUS, 8)
        return int.from_bytes(data, 'little', signed=True)

    def set_fod(self, m, n=N_NOM):
        data = m.to_bytes(6, 'little') + n.to_bytes(2, 'little')
        self.write(DPLL3_FOD_FREQ, data)

    def set_mode(self, mode_byte):
        self.write(DPLL3_MODE, bytes([mode_byte]))

    def read_mode(self):
        return self.read(DPLL3_MODE, 1)[0]

    def read_status(self):
        return self.read(DPLL3_STATUS, 1)[0]


def measure_drift(cm, duration_s=15, sample_hz=4):
    """Measure phase drift rate in counts/second over duration."""
    interval = 1.0 / sample_hz
    samples = []
    t0 = time.monotonic()

    for i in range(int(duration_s * sample_hz)):
        target = t0 + (i + 1) * interval
        now = time.monotonic()
        if target > now:
            time.sleep(target - now)
        phase = cm.read_phase()
        elapsed = time.monotonic() - t0
        samples.append((elapsed, phase))

    # Linear regression: phase = slope * time + offset
    n = len(samples)
    if n < 4:
        return 0.0, samples
    sum_t = sum(s[0] for s in samples)
    sum_p = sum(s[1] for s in samples)
    sum_tp = sum(s[0] * s[1] for s in samples)
    sum_tt = sum(s[0] ** 2 for s in samples)
    denom = n * sum_tt - sum_t ** 2
    if denom == 0:
        return 0.0, samples
    slope = (n * sum_tp - sum_t * sum_p) / denom
    return slope, samples


def main():
    ap = argparse.ArgumentParser(description="Calibrate ClockMatrix FOD gain")
    ap.add_argument("--bus", type=int, default=15)
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=0x58)
    ap.add_argument("--offsets", default="0,10,50,100,500,1000,-100,-500",
                    help="Comma-separated ppb offsets to test")
    ap.add_argument("--duration", type=int, default=15,
                    help="Seconds per offset (default: 15)")
    ap.add_argument("--json", type=str, help="Save results to JSON file")
    args = ap.parse_args()

    offsets = [int(x) for x in args.offsets.split(",")]
    cm = ClockMatrixCal(args.bus, args.addr)

    try:
        # Verify state
        mode = cm.read_mode()
        status = cm.read_status()
        print("DPLL_3: MODE=0x%02X (pll_mode=%d) status=0x%02X" % (
            mode, mode & 0x07, status))

        # Setup: nominal FOD, switch to write_freq
        print("Setting nominal FOD and switching to write_freq mode...")
        cm.set_fod(M_NOM)
        time.sleep(0.1)
        cm.set_mode(MODE_WRITE_FREQ)
        time.sleep(0.5)

        mode = cm.read_mode()
        print("DPLL_3: MODE=0x%02X (pll_mode=%d)" % (mode, mode & 0x07))
        if (mode & 0x07) != 2:
            print("ERROR: Failed to switch to write_freq mode")
            return

        # Wait for settle
        print("Settling for 3s...")
        time.sleep(3)

        results = []
        print()
        print("%-10s  %14s  %14s  %10s" % (
            "Offset_ppb", "Phase_rate_c/s", "Delta_from_0", "Gain"))
        print("%-10s  %14s  %14s  %10s" % (
            "----------", "--------------", "--------------", "----------"))

        baseline_rate = None

        for ppb in offsets:
            # Apply offset
            delta_m = int(M_NOM * ppb * 1e-9)
            m = M_NOM + delta_m
            cm.set_fod(m)
            time.sleep(1)  # settle

            # Measure
            rate, samples = measure_drift(cm, duration_s=args.duration)

            if ppb == 0 and baseline_rate is None:
                baseline_rate = rate

            delta = rate - (baseline_rate or 0)

            # Gain = actual_ppb / commanded_ppb
            # phase rate in counts/s, need to know ps_per_count to get ppb
            # For now report raw counts/s and delta
            gain_str = ""
            if ppb != 0 and baseline_rate is not None:
                # We'll compute gain after we know ps_per_count
                gain_str = "%.6f" % (delta / ppb) if ppb != 0 else ""

            print("%-10d  %14.1f  %14.1f  %10s" % (ppb, rate, delta, gain_str))

            results.append({
                "offset_ppb": ppb,
                "phase_rate_counts_per_s": rate,
                "delta_from_baseline": delta,
                "n_samples": len(samples),
            })

        # Restore
        print()
        print("Restoring nominal FOD and PLL mode...")
        cm.set_fod(M_NOM)
        time.sleep(0.1)
        cm.set_mode(MODE_PLL)
        time.sleep(0.1)
        print("DPLL_3 MODE: 0x%02X" % cm.read_mode())

        # Compute calibration from linear fit of offset_ppb vs delta_counts
        if len(results) > 2:
            print()
            print("=== Calibration Summary ===")
            # Use non-zero offsets for gain calculation
            cal_points = [r for r in results if r["offset_ppb"] != 0]
            if cal_points:
                gains = [r["delta_from_baseline"] / r["offset_ppb"]
                         for r in cal_points]
                avg_gain = sum(gains) / len(gains)
                print("Avg counts_per_ppb: %.1f" % avg_gain)
                print("(To get ps_per_count, compare with TICC drift rate)")
                print()
                print("If TICC shows X ps/s drift at offset Y ppb:")
                print("  ps_per_count = X / (delta_counts_at_Y_ppb / duration)")

        if args.json:
            with open(args.json, "w") as f:
                json.dump({"results": results, "duration_s": args.duration,
                           "bus": args.bus, "addr": args.addr}, f, indent=2)
            print("Saved to %s" % args.json)

    finally:
        # Always try to restore PLL mode
        try:
            cm.set_fod(M_NOM)
            cm.set_mode(MODE_PLL)
        except Exception:
            pass
        cm.close()


if __name__ == "__main__":
    main()
