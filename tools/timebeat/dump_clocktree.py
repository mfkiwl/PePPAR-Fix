#!/usr/bin/env python3
"""dump_clocktree.py — Dump Renesas 8A34012 ClockMatrix config and status.

Reads DPLL configuration, status, input monitor, output, and TDC registers
using 2-byte I2C addressing (i2c_rdwr). The 8A34012 does NOT use the 1B
page register mode (0xFC) — that's the 8A34002.

Usage:
    # Dump from ptBoat (bus 16):
    python3 dump_clocktree.py --bus 16

    # Dump from otcBob1 (bus 15):
    python3 dump_clocktree.py --bus 15

    # Save JSON for replay:
    python3 dump_clocktree.py --bus 16 -o clocktree_ptboat.json
"""

import argparse
import json
import sys
import time

try:
    import smbus2
except ImportError:
    sys.exit("smbus2 not installed.  pip install smbus2")

CHIP_ADDR = 0x58

# --- Register map from Timebeat Go source (8A34012) ---

# DPLL module bases
MOD_DPLL = {0: 0xC3B0, 1: 0xC400, 2: 0xC438, 3: 0xC480}

# Offsets within each DPLL module
DPLL_REGS = [
    ("CTRL_0",              0x02, 1),
    ("CTRL_1",              0x03, 1),
    ("CTRL_2",              0x04, 1),
    ("REF_PRIORITY_0",      0x0F, 1),
    ("REF_PRIORITY_1",      0x10, 1),
    ("REF_PRIORITY_2",      0x11, 1),
    ("REF_PRIORITY_3",      0x12, 1),
    ("REF_PRIORITY_4",      0x13, 1),
    ("FASTLOCK_CFG_0",      0x23, 1),
    ("FASTLOCK_CFG_1",      0x24, 1),
    ("WRITE_PHASE_TIMER",   0x2E, 1),
    ("REF_MODE",            0x35, 1),
    ("PHASE_MEASUREMENT_CFG", 0x36, 1),
    ("MODE",                0x37, 1),
]

# DPLL_CTRL module bases (freq/phase write)
MOD_DPLL_CTRL = {0: 0xC600, 1: 0xC63C, 2: 0xC680, 3: 0xC6BC}

DPLL_CTRL_REGS = [
    ("HS_TIE_RESET",        0x00, 1),
    ("MANU_REF_CFG",        0x01, 1),
    ("BW",                  0x04, 4),
    ("PHASE_OFFSET_CFG",    0x14, 6),
    ("FINE_PHASE_ADV_CFG",  0x1A, 2),
    ("FOD_FREQ",            0x1C, 6),
    ("COMBO_SW_VALUE_CNFG", 0x28, 4),
]

# DPLL_PHASE module bases (phase write target)
MOD_DPLL_PHASE = {0: 0xC818, 1: 0xC81C, 2: 0xC820, 3: 0xC824}

# Status module
MOD_STATUS = 0xC03C

# Output registers
OUTPUT_REGS = {
    0: 0xCA14, 1: 0xCA24, 2: 0xCA34, 3: 0xCA44,
    4: 0xCA54, 5: 0xCA64, 6: 0xCA80, 7: 0xCA90,
}

# Decode helpers
PLL_MODES = {
    0: "PLL", 1: "write_phase", 2: "write_freq",
    3: "gpio_inc_dec", 4: "synthesizer", 5: "phase_meas", 6: "disabled"
}
STATE_MODES = {
    0: "automatic", 1: "force_lock", 2: "force_freerun", 3: "force_holdover"
}
REF_MODES = {
    0: "automatic", 1: "manual", 2: "gpio", 3: "slave", 4: "gpio_slave"
}
LOCK_STATES = {
    0: "freerun", 1: "locked", 2: "locking", 3: "holdover",
    4: "write_phase", 5: "write_freq"
}


class ClockMatrix8A34012:
    """I2C access to Renesas 8A34012 using 2-byte register addressing."""

    def __init__(self, bus_num, addr=CHIP_ADDR):
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
        return list(msg_r)

    def write(self, reg16, data):
        hi = (reg16 >> 8) & 0xFF
        lo = reg16 & 0xFF
        msg = smbus2.i2c_msg.write(self.addr, [hi, lo] + list(data))
        self.bus.i2c_rdwr(msg)


def hexd(data):
    return " ".join("%02X" % b for b in data)


def ref_name(val):
    val = val & 0x1F
    if val <= 15:
        return "CLK%d" % val
    return {16: "write_phase", 17: "write_freq", 18: "xo_dpll"}.get(val, "ref_%d" % val)


def dump_all(cm):
    """Read and display all key registers."""
    config = {}

    # --- Hardware ID ---
    data = cm.read(0x8180, 4)
    print("Hardware Revision (0x8180): %s" % hexd(data))
    config["HW_REVISION"] = {"addr": 0x8180, "data": data}

    # --- DPLL config ---
    print("\n=== DPLL Configuration ===\n")
    for dpll_i in range(4):
        base = MOD_DPLL[dpll_i]
        print("--- DPLL_%d (base 0x%04X) ---" % (dpll_i, base))
        for name, offset, nbytes in DPLL_REGS:
            reg = base + offset
            data = cm.read(reg, nbytes)
            key = "DPLL_%d.%s" % (dpll_i, name)
            config[key] = {"addr": reg, "data": data}

            extra = ""
            if name == "MODE":
                pll_mode = data[0] & 0x07
                state_mode = (data[0] >> 3) & 0x03
                extra = "  pll_mode=%d (%s), state=%d (%s)" % (
                    pll_mode, PLL_MODES.get(pll_mode, "?"),
                    state_mode, STATE_MODES.get(state_mode, "?"))
            elif name == "REF_MODE":
                extra = "  %s" % REF_MODES.get(data[0] & 0x07, "?")
            elif "REF_PRIORITY" in name:
                extra = "  %s" % ref_name(data[0])
            print("  %-22s (0x%04X): %-12s%s" % (name, reg, hexd(data), extra))
        print()

    # --- DPLL_CTRL (frequency/phase write) ---
    print("=== DPLL Control (write targets) ===\n")
    for dpll_i in range(4):
        base = MOD_DPLL_CTRL[dpll_i]
        print("--- DPLL_CTRL_%d (base 0x%04X) ---" % (dpll_i, base))
        for name, offset, nbytes in DPLL_CTRL_REGS:
            reg = base + offset
            data = cm.read(reg, nbytes)
            key = "DPLL_CTRL_%d.%s" % (dpll_i, name)
            config[key] = {"addr": reg, "data": data}
            print("  %-22s (0x%04X): %s" % (name, reg, hexd(data)))
        print()

    # --- Status ---
    print("=== Status ===\n")

    print("Input monitor:")
    for i in range(16):
        reg = MOD_STATUS + 0x08 + i
        data = cm.read(reg, 1)
        key = "STATUS.IN%d_MON" % i
        config[key] = {"addr": reg, "data": data}
        if data[0]:
            print("  CLK%-2d: 0x%02X (active)" % (i, data[0]))

    print("\nDPLL status:")
    for i in range(4):
        # Lock state
        reg_s = MOD_STATUS + 0x18 + i
        data_s = cm.read(reg_s, 1)
        state = data_s[0] & 0x07
        config["STATUS.DPLL%d" % i] = {"addr": reg_s, "data": data_s}

        # Current ref
        reg_r = MOD_STATUS + 0x22 + i
        data_r = cm.read(reg_r, 1)
        config["STATUS.DPLL%d_REF" % i] = {"addr": reg_r, "data": data_r}

        print("  DPLL_%d: %s, ref=%s (raw=0x%02X,0x%02X)" % (
            i, LOCK_STATES.get(state, "state_%d" % state),
            ref_name(data_r[0]), data_s[0], data_r[0]))

    print("\nDPLL filter status (fine phase):")
    for i, off in [(0, 0x44), (1, 0x4C), (2, 0x54), (3, 0x5C)]:
        reg = MOD_STATUS + off
        data = cm.read(reg, 8)
        config["STATUS.DPLL%d_FILTER" % i] = {"addr": reg, "data": data}
        print("  DPLL_%d: %s" % (i, hexd(data)))

    print("\nDPLL phase status (coarse):")
    for i, off in [(0, 0xDC), (1, 0xE4), (2, 0xEC), (3, 0xF4)]:
        reg = MOD_STATUS + off
        data = cm.read(reg, 8)
        config["STATUS.DPLL%d_PHASE" % i] = {"addr": reg, "data": data}
        print("  DPLL_%d: %s" % (i, hexd(data)))

    print("\nTDC status:")
    reg = MOD_STATUS + 0xAC
    data = cm.read(reg, 1)
    config["STATUS.TDC_CFG"] = {"addr": reg, "data": data}
    print("  TDC_CFG: 0x%02X" % data[0])
    for i in range(4):
        reg = MOD_STATUS + 0xAD + i
        data = cm.read(reg, 1)
        config["STATUS.TDC%d" % i] = {"addr": reg, "data": data}
        print("  TDC_%d: 0x%02X" % (i, data[0]))

    print("\nTDC measurements:")
    for i, off in [(0, 0xB4), (1, 0xC4), (2, 0xCC), (3, 0xD4)]:
        reg = MOD_STATUS + off
        nbytes = 16 if i == 0 else 8
        data = cm.read(reg, nbytes)
        config["STATUS.TDC%d_MEAS" % i] = {"addr": reg, "data": data}
        if any(b != 0 for b in data):
            print("  TDC_%d: %s" % (i, hexd(data)))

    print("\nInput frequency status:")
    for i in range(16):
        reg = MOD_STATUS + 0x8C + (i * 2)
        data = cm.read(reg, 2)
        config["STATUS.IN%d_FREQ" % i] = {"addr": reg, "data": data}
        val = data[0] | (data[1] << 8)
        unit = (val >> 14) & 0x03
        units = {0: "1ppb", 1: "10ppb", 2: "100ppb", 3: "1000ppb"}
        freq_val = val & 0x3FFF
        if freq_val & 0x2000:
            freq_val -= 0x4000
        if unit < 3 or freq_val != -8192:  # Skip saturated/inactive
            print("  CLK%-2d: %d %s" % (i, freq_val, units.get(unit, "?")))

    # --- Outputs ---
    print("\n=== Outputs ===\n")
    for out_i, reg in sorted(OUTPUT_REGS.items()):
        data = cm.read(reg, 8)
        config["OUTPUT_%d" % out_i] = {"addr": reg, "data": data}
        print("  OUTPUT_%d (0x%04X): %s" % (out_i, reg, hexd(data)))

    return config


def main():
    ap = argparse.ArgumentParser(
        description="Dump Renesas 8A34012 ClockMatrix registers")
    ap.add_argument("--bus", type=int, required=True,
                    help="I2C bus number (15=otcBob1, 16=ptBoat)")
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=CHIP_ADDR)
    ap.add_argument("-o", "--output", help="Save JSON to file")
    args = ap.parse_args()

    cm = ClockMatrix8A34012(args.bus, args.addr)
    try:
        config = dump_all(cm)
        if args.output:
            with open(args.output, "w") as f:
                json.dump(config, f, indent=2)
            print("\nSaved %d registers to %s" % (len(config), args.output))
    finally:
        cm.close()


if __name__ == "__main__":
    main()
