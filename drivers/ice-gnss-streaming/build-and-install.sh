#!/bin/bash
# Build and install a patched Intel ice driver with GNSS streaming delivery.
#
# The stock ice driver (in-kernel and Intel out-of-tree v2.4.5) accumulates
# GNSS I2C data into a 4 KB page buffer before delivering it to userspace,
# creating ~2-second latency.  This script applies a patch that streams each
# 15-byte I2C chunk immediately, reducing latency to ~20 ms.
#
# This uses the in-kernel driver source (linux-source package) to preserve
# EXTTS, DPLL, irdma, and all other in-kernel features.  An out-of-tree
# patch (0001) is also provided but breaks EXTTS — avoid it for servo use.
#
# Prerequisites:
#   - Ubuntu 24.04 (or similar with kernel 6.x)
#   - E810-XXVDA4T (or other E810 with onboard GNSS)
#   - sudo access
#
# Usage:
#   cd drivers/ice-gnss-streaming
#   ./build-and-install.sh          # extract, patch, build, install
#   ./build-and-install.sh --load   # also unload old module and load patched one
#
# After installation, the patched module loads automatically on next boot.
# For immediate use, pass --load or manually:
#   sudo rmmod irdma 2>/dev/null; sudo rmmod ice; sudo modprobe ice

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PATCH_FILE="$SCRIPT_DIR/0002-ice-gnss-streaming-intree.patch"
LOAD_MODULE=false

if [[ "${1:-}" == "--load" ]]; then
    LOAD_MODULE=true
fi

# --- Prerequisites -----------------------------------------------------------

echo "=== Checking prerequisites ==="

KVER="$(uname -r)"
# Derive the source package version from the kernel version (e.g. 6.8.0-106-generic → 6.8.0)
KSRC_VER="${KVER%%-*}"

if ! command -v gcc &>/dev/null || ! command -v make &>/dev/null; then
    echo "Installing build-essential..."
    sudo apt-get install -y build-essential
fi

if [[ ! -f "/lib/modules/$KVER/build/Makefile" ]]; then
    echo "Installing kernel headers for $KVER..."
    sudo apt-get install -y "linux-headers-$KVER"
fi

TARBALL="/usr/src/linux-source-${KSRC_VER}/linux-source-${KSRC_VER}.tar.bz2"
if [[ ! -f "$TARBALL" ]]; then
    echo "Installing linux-source-${KSRC_VER}..."
    sudo apt-get install -y "linux-source-${KSRC_VER}"
fi

if [[ ! -f "$PATCH_FILE" ]]; then
    echo "ERROR: Patch file not found: $PATCH_FILE"
    echo "Run this script from the drivers/ice-gnss-streaming/ directory."
    exit 1
fi

# --- Extract and patch -------------------------------------------------------

BUILD_DIR="/usr/src/linux-headers-${KVER}/drivers/net/ethernet/intel/ice"

echo "=== Extracting ice driver source from kernel source package ==="
# Extract just the ice driver directory from the kernel source tarball
TMPDIR=$(mktemp -d)
tar xjf "$TARBALL" -C "$TMPDIR" "linux-source-${KSRC_VER}/drivers/net/ethernet/intel/ice/"

echo "=== Applying GNSS streaming patch ==="
cd "$TMPDIR/linux-source-${KSRC_VER}/drivers/net/ethernet/intel/ice"
patch -p1 < "$PATCH_FILE"

echo "=== Verifying patch applied ==="
if grep -q 'get_zeroed_page' ice_gnss.c; then
    echo "ERROR: Patch did not apply cleanly — get_zeroed_page still present"
    rm -rf "$TMPDIR"
    exit 1
fi
echo "  Patch verified OK"

# --- Copy to build directory --------------------------------------------------

echo "=== Copying patched source to build tree ==="
sudo cp *.c *.h "$BUILD_DIR/"
rm -rf "$TMPDIR"

# --- Build --------------------------------------------------------------------

echo "=== Building ice module ==="
sudo make -C "/lib/modules/$KVER/build" M="$BUILD_DIR" modules

if [[ ! -f "$BUILD_DIR/ice.ko" ]]; then
    echo "ERROR: Build failed — ice.ko not produced"
    exit 1
fi
echo "  Built: $(ls -lh "$BUILD_DIR/ice.ko" | awk '{print $5}') ice.ko"

# --- Install ------------------------------------------------------------------

echo "=== Installing patched module ==="
ICE_DIR="/lib/modules/$KVER/updates/drivers/net/ethernet/intel/ice"
sudo mkdir -p "$ICE_DIR"
sudo cp "$BUILD_DIR/ice.ko" "$ICE_DIR/ice.ko"
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
echo "  python3 tools/read_stall_probe.py /dev/gnss0"
echo "Max read delta should be ~400 ms (vs ~2100 ms without the patch)."
