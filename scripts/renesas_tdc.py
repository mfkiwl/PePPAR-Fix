#!/usr/bin/env python3
"""renesas_tdc.py — Read Renesas 8A34002 ClockMatrix phase measurements via I2C.

Reads DPLL0 phase status (coarse, 50ps resolution) and filter status
(fine, 0.39ps resolution) from the Renesas 8A34002 on Timebeat OTC hardware.

The 8A34002 is behind a PCA9548 I2C mux. It's on virtual bus /dev/i2c-15
at address 0x58 on both otcBob1 and ptBoat.

IMPORTANT: This script shares the I2C bus with Timebeat. To avoid
corrupting the page register (which crashes Timebeat), we:
  1. Read quickly using single-page access
  2. Restore page register to 0x00 after every read
  3. Add a small delay between page set and read

If Timebeat crashes after running this script, power-cycle the host
(soft reboot is insufficient — the chip retains page register state).

Usage:
    # Read 10 phase measurements while Timebeat is running:
    python3 renesas_tdc.py --count 10

    # Continuous 1Hz sampling for 60 seconds (stop Timebeat first):
    sudo systemctl stop timebeat
    python3 renesas_tdc.py --duration 60

    # Read and print raw register dump:
    python3 renesas_tdc.py --dump
"""

import argparse
import sys
import time

try:
    import smbus2
except ImportError:
    print("pip install smbus2", file=sys.stderr)
    sys.exit(1)


# Hardware constants (confirmed on otcBob1 and ptBoat)
I2C_BUS = 15        # Virtual bus behind PCA9548 mux
CHIP_ADDR = 0x58    # 8A34002 I2C address
PAGE_REG = 0xFC     # 1B mode page register offset

# Register addresses (16-bit: upper byte = page, lower byte = offset)
STATUS_PAGE = 0xC0
DPLL0_FILTER_OFFSET = 0x24   # 6 bytes: signed 48-bit in ITDC_UI/128
DPLL0_PHASE_OFFSET = 0xDC    # 5 bytes: signed 36-bit in ITDC_UI

# Default TDC resolution
ITDC_UI_PS = 50  # picoseconds per ITDC_UI (default setting)


class RenesasTDC:
    """Read phase measurements from Renesas 8A34002 ClockMatrix."""

    def __init__(self, bus_num=I2C_BUS, addr=CHIP_ADDR):
        self.bus = smbus2.SMBus(bus_num)
        self.addr = addr

    def close(self):
        self._restore_page()
        self.bus.close()

    def _set_page(self, page):
        self.bus.write_byte_data(self.addr, PAGE_REG, page)

    def _restore_page(self):
        """Restore page register to 0x00 to avoid corrupting Timebeat."""
        try:
            self.bus.write_byte_data(self.addr, PAGE_REG, 0x00)
        except Exception:
            pass

    def read_phase_coarse(self):
        """Read DPLL0_PHASE_STATUS: signed 36-bit, 50ps resolution.

        Returns (raw_itdc_units, nanoseconds) or (None, None) on error.
        """
        try:
            self._set_page(STATUS_PAGE)
            time.sleep(0.001)  # 1ms settle
            data = self.bus.read_i2c_block_data(self.addr, DPLL0_PHASE_OFFSET, 5)
            self._restore_page()

            val = int.from_bytes(bytes(data[:4]), 'little')
            val |= (data[4] & 0x0F) << 32
            # Sign extend from 36 bits
            if val & (1 << 35):
                val -= (1 << 36)

            # Error code check (0x7FFFFFFFF = LOS condition)
            if val == 0x7FFFFFFFF:
                return None, None

            ns = val * ITDC_UI_PS / 1000.0
            return val, ns
        except Exception as e:
            self._restore_page()
            return None, None

    def read_phase_fine(self):
        """Read DPLL0_FILTER_STATUS: signed 48-bit, 0.39ps resolution.

        Returns (raw_units, nanoseconds) or (None, None) on error.
        Fine readings require fbd_integer_mode_en=1 in INPUT_TDC_FBD_CTRL.
        """
        try:
            self._set_page(STATUS_PAGE)
            time.sleep(0.001)
            data = self.bus.read_i2c_block_data(self.addr, DPLL0_FILTER_OFFSET, 6)
            self._restore_page()

            val = int.from_bytes(bytes(data), 'little', signed=True)

            # Error code check (0x7FFFFFFFFFFF = LOS)
            if val == 0x7FFFFFFFFFFF:
                return None, None

            ns = val * ITDC_UI_PS / 128.0 / 1000.0
            return val, ns
        except Exception as e:
            self._restore_page()
            return None, None

    def dump_status_registers(self):
        """Read and display key status registers."""
        try:
            self._set_page(STATUS_PAGE)
            time.sleep(0.001)
            regs = {}
            for name, offset, nbytes in [
                ("GENERAL_STATUS", 0x14, 6),
                ("DPLL0_FILTER_STATUS", 0x24, 6),
                ("DPLL0_PHASE_STATUS", 0xDC, 5),
                ("DPLL1_FILTER_STATUS", 0x2C, 6),
                ("DPLL1_PHASE_STATUS", 0xE4, 5),
            ]:
                data = self.bus.read_i2c_block_data(self.addr, offset, nbytes)
                regs[name] = data
            self._restore_page()
            return regs
        except Exception as e:
            self._restore_page()
            return {}


def main():
    ap = argparse.ArgumentParser(description="Read Renesas 8A34002 TDC phase")
    ap.add_argument("--count", type=int, default=10, help="Number of readings")
    ap.add_argument("--duration", type=int, help="Duration in seconds (overrides --count)")
    ap.add_argument("--interval", type=float, default=1.0, help="Seconds between readings")
    ap.add_argument("--dump", action="store_true", help="Dump raw status registers")
    ap.add_argument("--bus", type=int, default=I2C_BUS)
    ap.add_argument("--addr", type=int, default=CHIP_ADDR)
    ap.add_argument("--csv", action="store_true", help="CSV output")
    args = ap.parse_args()

    tdc = RenesasTDC(args.bus, args.addr)

    try:
        if args.dump:
            regs = tdc.dump_status_registers()
            for name, data in regs.items():
                hex_str = ' '.join(f'{b:02X}' for b in data)
                print(f"  {name:30s}: {hex_str}")
            return

        if args.csv:
            print("timestamp,coarse_itdc,coarse_ns,fine_itdc,fine_ns")

        count = args.count
        if args.duration:
            count = int(args.duration / args.interval)

        for i in range(count):
            raw_c, ns_c = tdc.read_phase_coarse()
            raw_f, ns_f = tdc.read_phase_fine()

            ts = time.strftime('%Y-%m-%dT%H:%M:%S')

            if args.csv:
                c_str = f"{raw_c},{ns_c:.3f}" if raw_c is not None else ","
                f_str = f"{raw_f},{ns_f:.6f}" if raw_f is not None else ","
                print(f"{ts},{c_str},{f_str}")
            else:
                c_str = f"{ns_c:12.3f} ns" if ns_c is not None else "    LOS/error"
                f_str = f"{ns_f:14.6f} ns" if ns_f is not None else "      LOS/error"
                if i == 0:
                    print(f"{'#':>3}  {'Coarse':>14}  {'Fine':>16}")
                    print(f"{'---':>3}  {'-'*14}  {'-'*16}")
                print(f"{i:>3}  {c_str}  {f_str}")

            sys.stdout.flush()
            if i < count - 1:
                time.sleep(args.interval)

    except KeyboardInterrupt:
        pass
    finally:
        tdc.close()


if __name__ == "__main__":
    main()
