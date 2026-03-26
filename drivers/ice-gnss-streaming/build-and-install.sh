#!/bin/bash
# Build and install a patched Intel ice driver with GNSS streaming delivery.
#
# The stock ice driver (in-kernel and Intel out-of-tree v2.4.5) accumulates
# GNSS I2C data into a 4 KB page buffer before delivering it to userspace,
# creating ~2-second latency.  This script applies a patch that streams each
# 15-byte I2C chunk immediately, reducing latency to ~20 ms.
#
# Prerequisites:
#   - Ubuntu 24.04 (or similar with kernel 6.x)
#   - E810-XXVDA4T (or other E810 with onboard GNSS)
#   - sudo access
#
# Usage:
#   cd drivers/ice-gnss-streaming
#   ./build-and-install.sh          # clone, patch, build, install
#   ./build-and-install.sh --load   # also unload old module and load patched one
#
# After installation, the patched module loads automatically on next boot.
# For immediate use, pass --load or manually:
#   sudo rmmod irdma 2>/dev/null; sudo rmmod ice; sudo modprobe ice

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PATCH_FILE="$SCRIPT_DIR/0001-ice-gnss-streaming-delivery.patch"
WORK_DIR="${WORK_DIR:-/tmp/ice-gnss-build}"
INTEL_ICE_REPO="https://github.com/intel/ethernet-linux-ice.git"
LOAD_MODULE=false

if [[ "${1:-}" == "--load" ]]; then
    LOAD_MODULE=true
fi

# --- Prerequisites -----------------------------------------------------------

echo "=== Checking prerequisites ==="

if ! command -v gcc &>/dev/null || ! command -v make &>/dev/null; then
    echo "Installing build-essential..."
    sudo apt-get install -y build-essential
fi

KVER="$(uname -r)"
if [[ ! -f "/lib/modules/$KVER/build/Makefile" ]]; then
    echo "Installing kernel headers for $KVER..."
    sudo apt-get install -y "linux-headers-$KVER"
fi

if [[ ! -f "$PATCH_FILE" ]]; then
    echo "ERROR: Patch file not found: $PATCH_FILE"
    echo "Run this script from the drivers/ice-gnss-streaming/ directory."
    exit 1
fi

# --- Clone and patch ---------------------------------------------------------

echo "=== Cloning Intel out-of-tree ice driver ==="
rm -rf "$WORK_DIR"
git clone --depth 1 "$INTEL_ICE_REPO" "$WORK_DIR"

echo "=== Applying GNSS streaming patch ==="
cd "$WORK_DIR"
# The patch uses a/ b/ prefixes relative to the repo root.
patch -p1 < "$PATCH_FILE"

echo "=== Verifying patch applied ==="
if grep -q 'get_zeroed_page' src/ice_gnss.c; then
    echo "ERROR: Patch did not apply cleanly — get_zeroed_page still present"
    exit 1
fi
if grep -q 'ICE_GNSS_TIMER_DELAY_TIME' src/ice_gnss.h; then
    echo "ERROR: Patch did not apply cleanly — TIMER_DELAY_TIME still present"
    exit 1
fi
echo "  Patch verified OK"

# --- Build --------------------------------------------------------------------

echo "=== Building ice module ==="
cd src
make -j"$(nproc)"

if [[ ! -f ice.ko ]]; then
    echo "ERROR: Build failed — ice.ko not produced"
    exit 1
fi
echo "  Built: $(ls -lh ice.ko | awk '{print $5}') ice.ko"

# --- DDP firmware -------------------------------------------------------------

# The out-of-tree driver looks for firmware at updates/intel/ice/ddp/ice.pkg.
# The Ubuntu package provides it compressed at intel/ice/ddp/ice-*.pkg.zst.
DDP_DIR="/lib/firmware/updates/intel/ice/ddp"
if [[ ! -f "$DDP_DIR/ice.pkg" ]]; then
    echo "=== Setting up DDP firmware ==="
    SRC_PKG=$(ls /lib/firmware/intel/ice/ddp/ice-*.pkg.zst 2>/dev/null | head -1)
    if [[ -z "$SRC_PKG" ]]; then
        echo "WARNING: No DDP firmware found in /lib/firmware/intel/ice/ddp/"
        echo "  The E810 may operate with reduced functionality."
    else
        sudo mkdir -p "$DDP_DIR"
        sudo zstd -d "$SRC_PKG" -o "$DDP_DIR/ice.pkg"
        echo "  Decompressed $SRC_PKG -> $DDP_DIR/ice.pkg"
    fi
fi

# --- Install ------------------------------------------------------------------

echo "=== Installing patched module ==="
ICE_DIR="/lib/modules/$KVER/updates/drivers/net/ethernet/intel/ice"
sudo mkdir -p "$ICE_DIR"
sudo cp ice.ko "$ICE_DIR/ice.ko"
sudo depmod -a
echo "  Installed to $ICE_DIR/ice.ko"
echo "  The patched module will load automatically on next boot."

# --- Optional: load now -------------------------------------------------------

if $LOAD_MODULE; then
    echo "=== Loading patched module ==="
    sudo rmmod irdma 2>/dev/null || true
    sudo rmmod ice 2>/dev/null || true
    sudo modprobe ice
    sleep 3
    if [[ -e /dev/gnss0 ]]; then
        echo "  /dev/gnss0 present — GNSS subsystem active"
    else
        echo "  Waiting for /dev/gnss0..."
        for i in $(seq 1 10); do
            sleep 2
            if [[ -e /dev/gnss0 ]]; then
                echo "  /dev/gnss0 present after ${i}x2s"
                break
            fi
        done
    fi
    if [[ -e /dev/gnss0 ]]; then
        echo "  SUCCESS: Patched ice module loaded with GNSS streaming delivery"
    else
        echo "  WARNING: /dev/gnss0 not found — check dmesg for errors"
    fi
else
    echo ""
    echo "Module installed but not loaded.  To activate now:"
    echo "  sudo rmmod irdma 2>/dev/null; sudo rmmod ice; sudo modprobe ice"
    echo "Or reboot."
fi

echo ""
echo "=== Done ==="
echo "To verify the fix, run:"
echo "  python3 scripts/read_stall_probe.py /dev/gnss0"
echo "Max read delta should be ~400 ms (vs ~2100 ms without the patch)."
