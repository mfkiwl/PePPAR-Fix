#!/bin/bash
# Build and install the TimeHAT-patched igc kernel driver via DKMS.
#
# The stock Linux igc driver (Intel i225/i226) timestamps both rising and
# falling EXTTS edges, which causes ~2 PPS events/sec on most GPS receivers
# and confuses naive servo loops.  The TimeHAT patch in
# intel-igc-ppsfix/ forces rising-edge-only EXTTS and fixes a related
# PEROUT issue.
#
# PePPAR Fix already filters dual edges in userspace (DualEdgeFilter), so
# this kernel patch is defense in depth — but it's also the foundation
# we'll layer drivers/igc-adjfine-fix on top of, since both touch igc_ptp.c.
#
# Prerequisites:
#   - Raspberry Pi 5 with TimeHAT (i226 NIC)
#   - Raspberry Pi OS Trixie (Debian 13) with kernel 6.12
#   - sudo access
#
# Usage:
#   cd drivers/igc-timehat-edge
#   sudo ./build-and-install.sh           # build, install, defer reload to next boot
#   sudo ./build-and-install.sh --load    # build, install, AND reload module now
#
# After installation, verify with:
#   sudo testptp -d /dev/ptp0 -L1,1
#   sudo testptp -d /dev/ptp0 -e 5

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_DIR="$SCRIPT_DIR/intel-igc-ppsfix"
PKG_NAME="igc"
PKG_VERSION="6.12.0-ppsfix.1"
DKMS_SRC="/usr/src/${PKG_NAME}-${PKG_VERSION}"
LOAD_MODULE=false

if [[ "${1:-}" == "--load" ]]; then
    LOAD_MODULE=true
fi

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (use sudo)." >&2
    exit 1
fi

# --- Sanity checks ----------------------------------------------------------

KVER="$(uname -r)"
echo "=== TimeHAT igc PPS fix — DKMS install ==="
echo "Kernel: $KVER"
echo "DKMS package: $PKG_NAME-$PKG_VERSION"
echo

if [[ ! -f "$SOURCE_DIR/dkms.conf" ]]; then
    echo "ERROR: vendored source missing at $SOURCE_DIR" >&2
    echo "       Expected to find dkms.conf and src/ in that directory." >&2
    exit 1
fi

# Confirm this is a 6.12-series kernel — older or newer kernels will not
# build this source unmodified.
case "$KVER" in
    6.12.*) ;;
    *)
        echo "WARNING: this driver targets kernel 6.12.x; you are on $KVER." >&2
        echo "         Build may fail or produce a module that won't load." >&2
        echo "         Continuing anyway." >&2
        ;;
esac

# --- Prerequisites: dkms + kernel headers -----------------------------------

NEED_INSTALL=()
command -v dkms >/dev/null || NEED_INSTALL+=(dkms)

if [[ ! -f "/lib/modules/$KVER/build/Makefile" ]]; then
    # On Raspberry Pi OS Trixie the kernel headers come from
    # linux-headers-rpi-2712 (CM5/Pi5) or linux-headers-rpi-v8 (older).
    # Fall back to the generic linux-headers-$(uname -r) name if those
    # don't exist.  apt will tell us either way.
    NEED_INSTALL+=(linux-headers-rpi-2712)
fi

if (( ${#NEED_INSTALL[@]} > 0 )); then
    echo "=== Installing prerequisites: ${NEED_INSTALL[*]} ==="
    apt-get update -qq
    apt-get install -y "${NEED_INSTALL[@]}"
fi

if [[ ! -f "/lib/modules/$KVER/build/Makefile" ]]; then
    echo "ERROR: kernel headers still missing at /lib/modules/$KVER/build" >&2
    echo "       Try: sudo apt install linux-headers-\$(uname -r)" >&2
    exit 1
fi

# --- Stage source under /usr/src for DKMS -----------------------------------

if [[ -d "$DKMS_SRC" ]]; then
    echo "=== Removing previous DKMS staging at $DKMS_SRC ==="
    # Best-effort: remove the package from DKMS first so the staging
    # directory isn't being held by an active module entry.
    dkms remove "${PKG_NAME}/${PKG_VERSION}" --all 2>/dev/null || true
    rm -rf "$DKMS_SRC"
fi

echo "=== Staging vendored source to $DKMS_SRC ==="
mkdir -p "$DKMS_SRC"
cp -a "$SOURCE_DIR/." "$DKMS_SRC/"

# --- DKMS build + install ---------------------------------------------------

echo "=== dkms add ==="
dkms add -m "$PKG_NAME" -v "$PKG_VERSION"

echo "=== dkms build ==="
dkms build --force -m "$PKG_NAME" -v "$PKG_VERSION"

echo "=== dkms install ==="
dkms install --force -m "$PKG_NAME" -v "$PKG_VERSION"

# --- Replace the stock module so the patched one wins on next boot ---------

# DKMS installs to /lib/modules/$KVER/updates/dkms/ which has higher
# priority than the in-kernel module path on most distros.  But on
# Raspberry Pi OS the initramfs sometimes pulls the wrong copy.  Belt
# and suspenders: physically replace the stock .ko.xz with the patched
# one (after backing up the original).
STOCK_DIR="/lib/modules/$KVER/kernel/drivers/net/ethernet/intel/igc"
DKMS_KO_XZ="/lib/modules/$KVER/updates/dkms/igc.ko.xz"
DKMS_KO="/lib/modules/$KVER/updates/dkms/igc.ko"

if [[ -f "$STOCK_DIR/igc.ko.xz" && -f "$DKMS_KO_XZ" ]]; then
    if [[ ! -f "$STOCK_DIR/igc.ko.xz.bak" ]]; then
        echo "=== Backing up stock igc.ko.xz to igc.ko.xz.bak ==="
        cp "$STOCK_DIR/igc.ko.xz" "$STOCK_DIR/igc.ko.xz.bak"
    fi
    echo "=== Replacing stock igc.ko.xz with patched copy ==="
    cp "$DKMS_KO_XZ" "$STOCK_DIR/igc.ko.xz"
elif [[ -f "$STOCK_DIR/igc.ko" && -f "$DKMS_KO" ]]; then
    if [[ ! -f "$STOCK_DIR/igc.ko.bak" ]]; then
        echo "=== Backing up stock igc.ko to igc.ko.bak ==="
        cp "$STOCK_DIR/igc.ko" "$STOCK_DIR/igc.ko.bak"
    fi
    echo "=== Replacing stock igc.ko with patched copy ==="
    cp "$DKMS_KO" "$STOCK_DIR/igc.ko"
else
    echo "WARNING: could not locate stock igc module path; relying on" >&2
    echo "         DKMS /updates priority alone." >&2
fi

echo "=== depmod -a ==="
depmod -a "$KVER"

echo "=== update-initramfs -u -k $KVER ==="
update-initramfs -u -k "$KVER" || {
    echo "WARNING: update-initramfs failed; the patched module may not" >&2
    echo "         load on next boot if the initramfs holds a stale copy." >&2
}

# --- Optional: load now -----------------------------------------------------

if $LOAD_MODULE; then
    echo "=== Reloading igc module ==="
    # eth1 (the i226 on the TimeHAT) will go down briefly here.
    if ip link show eth1 &>/dev/null; then
        echo "  eth1 will go down briefly during reload"
    fi
    rmmod igc 2>/dev/null || true
    modprobe igc
    sleep 2
    SRCVER=$(cat /sys/module/igc/srcversion 2>/dev/null || echo unknown)
    echo "  Loaded igc srcversion: $SRCVER"
fi

echo
echo "=== Done ==="
echo "Patched igc-${PKG_VERSION} installed via DKMS."
if ! $LOAD_MODULE; then
    echo "Reboot (or pass --load next time) to activate the patched module."
fi
echo
echo "Verify with:"
echo "  sudo testptp -d /dev/ptp0 -L1,1     # pin 1 -> EXTTS chan 1"
echo "  sudo testptp -d /dev/ptp0 -e 5      # 5 events should be exactly 1 s apart"
