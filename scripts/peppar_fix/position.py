"""Position save/load for GNSS bootstrap and servo cold start."""

import json
import logging
import os
from datetime import datetime, timezone

import numpy as np

from solve_pseudorange import ecef_to_lla

log = logging.getLogger("peppar_fix.position")


def save_position(path, ecef, sigma_m, source, note=""):
    """Save position to JSON file (ECEF + LLA for human readability).

    Args:
        path: file path to write
        ecef: numpy array [x, y, z] in meters (ECEF)
        sigma_m: position sigma in meters (convergence quality proxy)
        source: string describing origin (e.g. 'ppp_bootstrap', 'known_pos')
        note: optional human-readable note
    """
    lat, lon, alt = ecef_to_lla(ecef[0], ecef[1], ecef[2])
    data = {
        "lat": round(lat, 7),
        "lon": round(lon, 7),
        "alt_m": round(alt, 3),
        "ecef_m": [round(float(ecef[0]), 3),
                    round(float(ecef[1]), 3),
                    round(float(ecef[2]), 3)],
        "sigma_m": round(float(sigma_m), 4),
        "timestamp": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        "source": source,
        "note": note,
    }
    tmp = path + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def load_position(path):
    """Load position from JSON file.

    Returns:
        numpy array [x, y, z] in ECEF meters, or None if file missing/invalid.
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        ecef = np.array(data["ecef_m"], dtype=float)
        if ecef.shape != (3,):
            return None
        return ecef
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        log.warning(f"Failed to load position from {path}: {e}")
        return None
