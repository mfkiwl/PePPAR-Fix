#!/usr/bin/env python3
"""eeprom_tool.py — Dump and restore Renesas 8A34002 EEPROM via I2C.

The 8A34002 provides indirect EEPROM access through its I2C slave interface:
  - Write EEPROM address, size, offset to control registers
  - Issue a command (read/write)
  - Data passes through a 128-byte buffer

The external EEPROM is a 24FC1025 (128KB = 1 Mbit):
  - I2C addr 0x50: lower 64KB (offsets 0x0000–0xFFFF)
  - I2C addr 0x54: upper 64KB (offsets 0x0000–0xFFFF)

Usage:
    # Dump full EEPROM to file:
    python3 eeprom_tool.py dump --bus 16 -o eeprom_backup.bin

    # Restore EEPROM from file:
    python3 eeprom_tool.py restore --bus 16 -i eeprom_backup.bin

    # Read a single 128-byte block (for testing):
    python3 eeprom_tool.py read --bus 16 --eeprom-addr 0x50 --offset 0 --size 8
"""

import argparse
import sys
import time

try:
    import smbus2
except ImportError:
    sys.exit("smbus2 not installed.  pip install smbus2")

# 8A34002 I2C slave address
CHIP_ADDR = 0x58

# Register addresses (16-bit, accessed via i2c_rdwr)
MOD_EEPROM = 0xCF68
MOD_BYTES = 0xCF80

# Offsets within EEPROM module
EEPROM_I2C_ADDR = 0x0
EEPROM_SIZE = 0x1
EEPROM_OFFSET = 0x2
EEPROM_CMD = 0x4

# Commands (2 bytes, big-endian in the Go code but let's verify)
EEPROM_CMD_READ = 0xEE01
EEPROM_CMD_WRITE = 0xEE02
EEPROM_CMD_WRITE_NO_VRFY = 0xEE03

# 24FC1025 addressing
EEPROM_ADDR_LO = 0x50  # Lower 64KB
EEPROM_ADDR_HI = 0x54  # Upper 64KB
EEPROM_TOTAL_SIZE = 128 * 1024  # 128KB
BLOCK_SIZE = 128  # Max transfer per command (buffer size)


class ClockMatrixI2C:
    """16-bit addressed I2C access to the 8A34002."""

    def __init__(self, bus_num, chip_addr=CHIP_ADDR):
        self.bus = smbus2.SMBus(bus_num)
        self.chip = chip_addr

    def close(self):
        self.bus.close()

    def read_reg(self, reg16, nbytes):
        hi = (reg16 >> 8) & 0xFF
        lo = reg16 & 0xFF
        msg_w = smbus2.i2c_msg.write(self.chip, [hi, lo])
        msg_r = smbus2.i2c_msg.read(self.chip, nbytes)
        self.bus.i2c_rdwr(msg_w, msg_r)
        return list(msg_r)

    def write_reg(self, reg16, data):
        hi = (reg16 >> 8) & 0xFF
        lo = reg16 & 0xFF
        msg = smbus2.i2c_msg.write(self.chip, [hi, lo] + list(data))
        self.bus.i2c_rdwr(msg)


def eeprom_read_block(cm, eeprom_i2c_addr, offset, size):
    """Read up to 128 bytes from EEPROM at given offset.

    Returns list of bytes, or raises on error.
    """
    if size > BLOCK_SIZE:
        raise ValueError("Max block size is %d" % BLOCK_SIZE)

    # Set up the read: I2C addr, size, offset, then command
    cm.write_reg(MOD_EEPROM + EEPROM_I2C_ADDR, [eeprom_i2c_addr])
    cm.write_reg(MOD_EEPROM + EEPROM_SIZE, [size])
    cm.write_reg(MOD_EEPROM + EEPROM_OFFSET, [offset & 0xFF, (offset >> 8) & 0xFF])

    # Issue read command (2 bytes)
    cm.write_reg(MOD_EEPROM + EEPROM_CMD,
                 [EEPROM_CMD_READ & 0xFF, (EEPROM_CMD_READ >> 8) & 0xFF])

    # Wait for completion — poll CMD register until it clears
    for _ in range(100):
        time.sleep(0.01)
        cmd = cm.read_reg(MOD_EEPROM + EEPROM_CMD, 2)
        if cmd == [0x00, 0x00]:
            break
    else:
        raise TimeoutError("EEPROM read command did not complete (cmd=%s)" %
                           " ".join("%02X" % b for b in cmd))

    # Read data from buffer
    data = cm.read_reg(MOD_BYTES, size)
    return data


def eeprom_write_block(cm, eeprom_i2c_addr, offset, data):
    """Write up to 128 bytes to EEPROM at given offset."""
    size = len(data)
    if size > BLOCK_SIZE:
        raise ValueError("Max block size is %d" % BLOCK_SIZE)

    # Write data to buffer first
    cm.write_reg(MOD_BYTES, list(data))

    # Set up: I2C addr, size, offset
    cm.write_reg(MOD_EEPROM + EEPROM_I2C_ADDR, [eeprom_i2c_addr])
    cm.write_reg(MOD_EEPROM + EEPROM_SIZE, [size])
    cm.write_reg(MOD_EEPROM + EEPROM_OFFSET, [offset & 0xFF, (offset >> 8) & 0xFF])

    # Issue write command with verify
    cm.write_reg(MOD_EEPROM + EEPROM_CMD,
                 [EEPROM_CMD_WRITE & 0xFF, (EEPROM_CMD_WRITE >> 8) & 0xFF])

    # Wait for completion
    for _ in range(200):  # Writes are slower
        time.sleep(0.02)
        cmd = cm.read_reg(MOD_EEPROM + EEPROM_CMD, 2)
        if cmd == [0x00, 0x00]:
            break
    else:
        raise TimeoutError("EEPROM write command did not complete (cmd=%s)" %
                           " ".join("%02X" % b for b in cmd))


def cmd_read(args):
    """Read a single block and print it."""
    cm = ClockMatrixI2C(args.bus)
    try:
        eeprom_addr = int(args.eeprom_addr, 0)
        data = eeprom_read_block(cm, eeprom_addr, args.offset, args.size)
        hex_str = " ".join("%02X" % b for b in data)
        print("EEPROM[0x%02X] offset 0x%04X (%d bytes):" % (eeprom_addr, args.offset, args.size))
        # Print in 16-byte rows
        for i in range(0, len(data), 16):
            row = data[i:i+16]
            addr = args.offset + i
            hex_part = " ".join("%02X" % b for b in row)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
            print("  %04X: %-48s  %s" % (addr, hex_part, ascii_part))
    finally:
        cm.close()


def cmd_dump(args):
    """Dump full 128KB EEPROM to binary file."""
    cm = ClockMatrixI2C(args.bus)
    try:
        total = EEPROM_TOTAL_SIZE
        buf = bytearray()
        blocks = total // BLOCK_SIZE

        print("Dumping %dKB EEPROM to %s ..." % (total // 1024, args.output))
        for i in range(blocks):
            abs_offset = i * BLOCK_SIZE
            # Lower 64KB uses addr 0x50, upper uses 0x54
            if abs_offset < 65536:
                eeprom_addr = EEPROM_ADDR_LO
                offset = abs_offset
            else:
                eeprom_addr = EEPROM_ADDR_HI
                offset = abs_offset - 65536

            data = eeprom_read_block(cm, eeprom_addr, offset, BLOCK_SIZE)
            buf.extend(data)

            # Progress
            pct = (i + 1) * 100 // blocks
            if (i + 1) % 8 == 0 or i == blocks - 1:
                print("  %3d%% (%d/%d blocks)" % (pct, i + 1, blocks), flush=True)

        with open(args.output, "wb") as f:
            f.write(buf)
        print("Done. Wrote %d bytes to %s" % (len(buf), args.output))
    finally:
        cm.close()


def cmd_restore(args):
    """Restore EEPROM from binary file."""
    with open(args.input, "rb") as f:
        image = f.read()

    if len(image) != EEPROM_TOTAL_SIZE:
        print("WARNING: file is %d bytes, expected %d" % (len(image), EEPROM_TOTAL_SIZE),
              file=sys.stderr)
        if not args.force:
            sys.exit("Use --force to write anyway")

    cm = ClockMatrixI2C(args.bus)
    try:
        blocks = len(image) // BLOCK_SIZE
        print("Restoring %d bytes from %s to EEPROM ..." % (len(image), args.input))

        for i in range(blocks):
            abs_offset = i * BLOCK_SIZE
            if abs_offset < 65536:
                eeprom_addr = EEPROM_ADDR_LO
                offset = abs_offset
            else:
                eeprom_addr = EEPROM_ADDR_HI
                offset = abs_offset - 65536

            block_data = image[abs_offset:abs_offset + BLOCK_SIZE]
            eeprom_write_block(cm, eeprom_addr, offset, block_data)

            pct = (i + 1) * 100 // blocks
            if (i + 1) % 8 == 0 or i == blocks - 1:
                print("  %3d%% (%d/%d blocks)" % (pct, i + 1, blocks), flush=True)

        print("Done. Wrote %d bytes to EEPROM." % len(image))
        print("Power cycle the board to load new config.")
    finally:
        cm.close()


def main():
    ap = argparse.ArgumentParser(description="Renesas 8A34002 EEPROM dump/restore tool")
    ap.add_argument("--bus", type=int, default=16, help="I2C bus number (default: 16)")
    sub = ap.add_subparsers(dest="command", required=True)

    # read
    p_read = sub.add_parser("read", help="Read a single EEPROM block")
    p_read.add_argument("--eeprom-addr", default="0x50", help="EEPROM I2C address (default: 0x50)")
    p_read.add_argument("--offset", type=int, default=0, help="Offset within EEPROM")
    p_read.add_argument("--size", type=int, default=128, help="Bytes to read (max 128)")

    # dump
    p_dump = sub.add_parser("dump", help="Dump full EEPROM to file")
    p_dump.add_argument("-o", "--output", required=True, help="Output file path")

    # restore
    p_restore = sub.add_parser("restore", help="Restore EEPROM from file")
    p_restore.add_argument("-i", "--input", required=True, help="Input file path")
    p_restore.add_argument("--force", action="store_true", help="Write even if file size is wrong")

    args = ap.parse_args()

    if args.command == "read":
        cmd_read(args)
    elif args.command == "dump":
        cmd_dump(args)
    elif args.command == "restore":
        cmd_restore(args)


if __name__ == "__main__":
    main()
