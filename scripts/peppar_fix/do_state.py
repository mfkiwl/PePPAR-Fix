"""DO and PHC state persistence — load/save per-device state files.

DO state lives in state/dos/<unique_id>.json.
PHC state lives in state/phcs/<unique_id>.json.

For bundled PHC+DO (i226, E810), the PHC MAC serves as both IDs.
For external DOs (VCOCXO, ClockMatrix), the DO needs its own label.

See docs/state-persistence-design.md for the full entity model.
"""

import glob as _glob
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DO_STATE_DIR = "state/dos"
PHC_STATE_DIR = "state/phcs"


# ── PHC unique ID discovery ─────────────────────────────────────────────── #

def discover_phc_mac(ptp_path):
    """Discover the MAC address of the NIC backing a PTP device.

    Walks sysfs: /sys/class/ptp/ptpN/device/net/*/address

    Returns:
        MAC address string (e.g. "54:49:4d:45:00:6b") or None
    """
    basename = os.path.basename(ptp_path)
    if not basename.startswith("ptp"):
        return None
    net_dir = f"/sys/class/ptp/{basename}/device/net"
    if not os.path.isdir(net_dir):
        return None
    for iface in os.listdir(net_dir):
        addr_path = os.path.join(net_dir, iface, "address")
        try:
            with open(addr_path) as f:
                mac = f.read().strip()
            if mac and mac != "00:00:00:00:00:00":
                return mac
        except OSError:
            continue
    return None


def discover_phc_driver(ptp_path):
    """Discover the driver name for a PTP device.

    Returns driver name (e.g. "igc", "ice") or None.
    """
    basename = os.path.basename(ptp_path)
    if not basename.startswith("ptp"):
        return None
    try:
        sys_path = f"/sys/class/ptp/{basename}/device/driver"
        return os.path.basename(os.readlink(sys_path))
    except (OSError, ValueError):
        return None


def phc_unique_id(ptp_path):
    """Get a stable unique ID for a PHC device.

    Uses MAC address (preferred) or falls back to the device path.
    """
    mac = discover_phc_mac(ptp_path)
    if mac is not None:
        return mac
    return ptp_path


# ── DO state ─────────────────────────────────────────────────────────────── #

def _do_path(unique_id, state_dir=None):
    d = state_dir or DO_STATE_DIR
    # MAC addresses contain colons — replace with dashes for filenames
    safe_id = str(unique_id).replace(":", "-").replace("/", "_")
    return os.path.join(d, f"{safe_id}.json")


def load_do_state(unique_id, state_dir=None):
    """Load DO state file.

    Returns dict or None if not found.
    """
    path = _do_path(unique_id, state_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        log.info("Loaded DO state from %s", path)
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to load DO state from %s: %s", path, e)
        return None


def save_do_state(state, state_dir=None):
    """Save DO state file. Atomic write via tmp+replace."""
    uid = state.get("unique_id")
    if uid is None:
        log.warning("Cannot save DO state without unique_id")
        return
    d = state_dir or DO_STATE_DIR
    os.makedirs(d, exist_ok=True)
    path = _do_path(uid, state_dir)
    tmp = path + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
        f.write('\n')
    os.replace(tmp, path)
    log.info("Saved DO state to %s", path)


def new_do_state(unique_id, label=None):
    """Create a fresh DO state dict."""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return {
        "unique_id": unique_id,
        "label": label or unique_id,
        "characterization": None,
        "adjustment": None,
        "last_known_freq_offset_ppb": None,
        "last_known_temp_c": None,
        "updated": now,
    }


def save_do_freq_offset(unique_id, adjfine_ppb, state_dir=None):
    """Update the DO's last-known frequency offset.

    This replaces the adjfine_ppb field in the old data/drift.json.
    """
    state = load_do_state(unique_id, state_dir)
    if state is None:
        state = new_do_state(unique_id)
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    state["last_known_freq_offset_ppb"] = adjfine_ppb
    state["updated"] = now
    save_do_state(state, state_dir)


def save_do_characterization(unique_id, characterization, state_dir=None):
    """Store DO characterization (from build_do_characterization.py).

    Args:
        unique_id: DO unique ID
        characterization: dict with asd, psd, tdev_1s, noise_floor_ns, etc.
    """
    state = load_do_state(unique_id, state_dir)
    if state is None:
        state = new_do_state(unique_id)
    state["characterization"] = characterization
    state["updated"] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    save_do_state(state, state_dir)


# ── PHC state ────────────────────────────────────────────────────────────── #

def _phc_path(unique_id, state_dir=None):
    d = state_dir or PHC_STATE_DIR
    safe_id = str(unique_id).replace(":", "-").replace("/", "_")
    return os.path.join(d, f"{safe_id}.json")


def load_phc_state(unique_id, state_dir=None):
    """Load PHC state file.

    Returns dict or None if not found.
    """
    path = _phc_path(unique_id, state_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        log.info("Loaded PHC state from %s", path)
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to load PHC state from %s: %s", path, e)
        return None


def save_phc_state(state, state_dir=None):
    """Save PHC state file. Atomic write via tmp+replace."""
    uid = state.get("unique_id")
    if uid is None:
        log.warning("Cannot save PHC state without unique_id")
        return
    d = state_dir or PHC_STATE_DIR
    os.makedirs(d, exist_ok=True)
    path = _phc_path(uid, state_dir)
    tmp = path + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
        f.write('\n')
    os.replace(tmp, path)
    log.info("Saved PHC state to %s", path)


def new_phc_state(ptp_path):
    """Create a fresh PHC state dict from a device path.

    Discovers MAC, driver, and links to a bundled DO.
    """
    mac = discover_phc_mac(ptp_path)
    driver = discover_phc_driver(ptp_path)
    uid = mac or ptp_path
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    return {
        "unique_id": uid,
        "device": ptp_path,
        "driver": driver,
        "mac": mac,
        "extts": None,
        "perout": None,
        "do_unique_id": uid,  # Bundled: PHC and DO share ID
        "last_known_device": ptp_path,
        "last_seen": now,
    }


# ── Drift file migration ────────────────────────────────────────────────── #

def migrate_drift_file(drift_path, ptp_path, state_dir=None):
    """Migrate legacy data/drift.json into DO and receiver state.

    Reads drift.json, splits fields:
    - adjfine_ppb → DO state (last_known_freq_offset_ppb)
    - tcxo_freq_corr_ppb → returned for receiver state update
    - dt_rx_ns → returned for receiver state update

    Returns:
        (tcxo_freq_corr_ppb, dt_rx_ns) or (None, None) if no drift file.
    """
    if not os.path.exists(drift_path):
        return None, None
    try:
        with open(drift_path) as f:
            drift = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None, None

    do_uid = phc_unique_id(ptp_path)
    adjfine = drift.get("adjfine_ppb")
    if adjfine is not None:
        save_do_freq_offset(do_uid, adjfine, state_dir)
        log.info("Migrated adjfine_ppb=%.1f from %s to DO state %s",
                 adjfine, drift_path, do_uid)

    return drift.get("tcxo_freq_corr_ppb"), drift.get("dt_rx_ns")


def load_drift_from_state(ptp_path, do_state_dir=None):
    """Load drift info from DO state, returning a dict compatible with
    the legacy drift.json format for backward compatibility.

    Returns dict with adjfine_ppb, phc, timestamp or None.
    """
    do_uid = phc_unique_id(ptp_path)
    state = load_do_state(do_uid, do_state_dir)
    if state is None:
        return None
    adjfine = state.get("last_known_freq_offset_ppb")
    if adjfine is None:
        return None
    return {
        "adjfine_ppb": adjfine,
        "phc": ptp_path,
        "timestamp": state.get("updated", ""),
    }
