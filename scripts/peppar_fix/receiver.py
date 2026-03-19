"""F9T receiver configuration and verification utilities.

Extracted from configure_f9t.py for reuse by peppar-rx-config and other tools.
"""

import logging
import sys
import time

log = logging.getLogger(__name__)

# Lazy imports — pyubx2/pyserial may not be installed
_UBXMessage = None
_UBXReader = None
_SET = None
_Serial = None


def _ensure_imports():
    """Import pyubx2 and pyserial on first use."""
    global _UBXMessage, _UBXReader, _SET, _Serial
    if _UBXMessage is not None:
        return
    try:
        from pyubx2 import UBXMessage, UBXReader, SET
        from serial import Serial
        _UBXMessage = UBXMessage
        _UBXReader = UBXReader
        _SET = SET
        _Serial = Serial
    except ImportError:
        raise ImportError("requires pyubx2 and pyserial: pip install pyubx2 pyserial")


# ── Signal configuration ──────────────────────────────────────────────────── #

SIGNAL_CONFIG = {
    # GPS
    "CFG_SIGNAL_GPS_ENA": 1,
    "CFG_SIGNAL_GPS_L1CA_ENA": 1,
    "CFG_SIGNAL_GPS_L5_ENA": 1,
    "CFG_SIGNAL_GPS_L2C_ENA": 0,
    # Galileo
    "CFG_SIGNAL_GAL_ENA": 1,
    "CFG_SIGNAL_GAL_E1_ENA": 1,
    "CFG_SIGNAL_GAL_E5A_ENA": 1,
    "CFG_SIGNAL_GAL_E5B_ENA": 0,
    # BeiDou
    "CFG_SIGNAL_BDS_ENA": 1,
    "CFG_SIGNAL_BDS_B1_ENA": 1,
    "CFG_SIGNAL_BDS_B2A_ENA": 1,
    "CFG_SIGNAL_BDS_B2_ENA": 0,
    # GLONASS off (FDMA)
    "CFG_SIGNAL_GLO_ENA": 0,
    # SBAS/QZSS off
    "CFG_SIGNAL_SBAS_ENA": 0,
    "CFG_SIGNAL_QZSS_ENA": 0,
}

# Required UBX messages for peppar-fix operation
REQUIRED_MESSAGES = {"RXM-RAWX", "RXM-SFRBX", "NAV-PVT", "TIM-TP"}

# Worst-case repetition times (seconds) for required messages.
# RAWX/PVT/TIM-TP repeat every measurement epoch (1s at 1 Hz).
# SFRBX repeats per subframe (~6s GPS, ~2s Galileo).  NAV-SAT every 5 epochs.
# Use generous timeouts to avoid false negatives.
MESSAGE_TIMEOUTS = {
    "RXM-RAWX": 5,
    "RXM-SFRBX": 15,
    "NAV-PVT": 5,
    "TIM-TP": 5,
}

# Port ID mapping
PORT_SUFFIX = {1: "UART1", 2: "UART2", 3: "USB", 4: "SPI"}


# ── Low-level UBX helpers ──────────────────────────────────────────────────── #

def probe_baud(port):
    """Try common baud rates and return the one that produces valid UBX/NMEA."""
    _ensure_imports()
    for baud in [9600, 38400, 115200, 230400, 460800]:
        try:
            ser = _Serial(port, baudrate=baud, timeout=2)
            ser.reset_input_buffer()
            time.sleep(1.5)
            data = ser.read(500)
            ser.close()
            if b'\xb5\x62' in data or b'$G' in data:
                return baud
        except Exception:
            pass
    return None


def open_receiver(port, baud=9600):
    """Open serial port and return (Serial, UBXReader) pair."""
    _ensure_imports()
    ser = _Serial(port, baudrate=baud, timeout=1)
    ubr = _UBXReader(ser, protfilter=2)  # UBX protocol only
    return ser, ubr


def wait_ack(ubr, cls_name="CFG", msg_name="VALSET", timeout=3.0):
    """Wait for UBX-ACK-ACK or UBX-ACK-NAK."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw, parsed = ubr.read()
        except Exception:
            continue
        if parsed is None:
            continue
        if parsed.identity == "ACK-ACK":
            return True
        if parsed.identity == "ACK-NAK":
            log.warning(f"NAK received for {cls_name}-{msg_name}")
            return False
    return False


def send_cfg(ser, ubr, key_values, description="", layers=7):
    """Send a VALSET configuration and wait for ACK.

    Args:
        layers: 1=RAM, 2=BBR, 4=Flash, 7=all (default).
    """
    _ensure_imports()
    cfg_data = list(key_values.items())
    msg = _UBXMessage.config_set(layers, 0, cfg_data)
    log.info(f"  {description}...")
    ser.write(msg.serialize())
    ack = wait_ack(ubr, "CFG", "VALSET", timeout=3.0)
    if ack:
        log.info(f"  {description}... OK")
    else:
        log.warning(f"  {description}... TIMEOUT (no ACK)")
    return ack


# ── Receiver commands ──────────────────────────────────────────────────────── #

def factory_reset(ser, ubr):
    """Issue a controlled software reset with factory defaults."""
    _ensure_imports()
    log.info("  Factory reset...")
    msg = _UBXMessage(
        "CFG", "CFG-RST", _SET,
        navBbrMask=0xFFFF, resetMode=1, reserved0=0,
    )
    ser.write(msg.serialize())
    time.sleep(1)
    log.info("  Factory reset... OK (receiver rebooting)")


def warm_restart(ser):
    """Issue a warm restart (keeps ephemeris, applies config changes)."""
    _ensure_imports()
    msg = _UBXMessage(
        "CFG", "CFG-RST", _SET,
        navBbrMask=0x0001, resetMode=1, reserved0=0,
    )
    ser.write(msg.serialize())


def reopen_after_reset(port, wait_s=5, retries=2):
    """Probe baud and reopen receiver after a reset.

    Returns (Serial, UBXReader) or raises RuntimeError.
    """
    for attempt in range(retries):
        time.sleep(wait_s)
        baud = probe_baud(port)
        if baud is not None:
            log.info(f"  Receiver found at {baud} baud after reset")
            return open_receiver(port, baud)
        log.info(f"  Probe attempt {attempt + 1} failed, retrying...")
    raise RuntimeError(f"Cannot find receiver on {port} after reset")


def configure_signals(ser, ubr):
    """Enable dual-frequency signals for PPP-AR."""
    return send_cfg(ser, ubr, SIGNAL_CONFIG,
                    "Signals: GPS L1+L5, GAL E1+E5a, BDS B1+B2a")


def configure_gps_l5_health(ser, ubr):
    """Override GPS L5 health status so receiver tracks L5 signals.

    Source: u-blox App Note UBX-21038688 "GPS L5 configuration".
    A NAK means the key is unsupported -- GPS L5 simply won't be tracked.
    """
    raw_msg = bytes([
        0xB5, 0x62,              # UBX sync
        0x06, 0x8A,              # class=CFG, id=VALSET
        0x09, 0x00,              # length = 9
        0x01, 0x07, 0x00, 0x00,  # version=1, layers=RAM+BBR+Flash, reserved
        0x01, 0x00, 0x32, 0x10,  # key 0x10320001 (little-endian)
        0x01,                    # value = 1 (enable override)
        0xE5, 0x26,              # Fletcher checksum
    ])
    log.info("  GPS L5 health override (UBX-21038688)...")
    ser.write(raw_msg)
    ack = wait_ack(ubr, "CFG", "VALSET", timeout=3.0)
    if ack:
        log.info("  GPS L5 health override... OK")
    else:
        log.warning("  GPS L5 health override... NAK (L5 will not be tracked)")
    return ack


def configure_rate(ser, ubr, rate_hz):
    """Set measurement and navigation rate."""
    meas_ms = int(1000 / rate_hz)
    return send_cfg(ser, ubr, {
        "CFG_RATE_MEAS": meas_ms,
        "CFG_RATE_NAV": 1,
        "CFG_RATE_TIMEREF": 0,
    }, f"Measurement rate = {rate_hz} Hz ({meas_ms} ms)")


def configure_messages(ser, ubr, port_id):
    """Enable required UBX messages on the specified port."""
    pname = PORT_SUFFIX.get(port_id, f"port{port_id}")
    messages = {
        f"CFG_MSGOUT_UBX_RXM_RAWX_{pname}": 1,
        f"CFG_MSGOUT_UBX_RXM_SFRBX_{pname}": 1,
        f"CFG_MSGOUT_UBX_NAV_PVT_{pname}": 1,
        f"CFG_MSGOUT_UBX_NAV_SAT_{pname}": 5,
        f"CFG_MSGOUT_UBX_TIM_TP_{pname}": 1,
    }
    return send_cfg(ser, ubr, messages,
                    f"UBX messages on {pname}: RAWX, SFRBX, PVT, SAT, TIM-TP")


def configure_nmea_off(ser, ubr, port_id):
    """Disable NMEA output on the port to save bandwidth (best-effort)."""
    pname = PORT_SUFFIX.get(port_id, f"port{port_id}")
    nmea_off = {}
    for nmea_msg in ["GGA", "GLL", "GSA", "GSV", "RMC", "VTG"]:
        nmea_off[f"CFG_MSGOUT_NMEA_ID_{nmea_msg}_{pname}"] = 0
    result = send_cfg(ser, ubr, nmea_off, f"Disable NMEA output on {pname}")
    if not result:
        log.info("    (NMEA disable failed -- non-critical)")
    return True


def configure_tmode(ser, ubr, survey_dur_s, survey_acc_m):
    """Configure survey-in for Time Mode."""
    acc_tenths_mm = int(survey_acc_m * 1000) * 10
    return send_cfg(ser, ubr, {
        "CFG_TMODE_MODE": 1,
        "CFG_TMODE_SVIN_MIN_DUR": survey_dur_s,
        "CFG_TMODE_SVIN_ACC_LIMIT": acc_tenths_mm,
    }, f"Survey-in: {survey_dur_s}s, {survey_acc_m}m accuracy")


def configure_uart_baud(ser, ubr, baud):
    """Set UART1 baud rate for high-rate output."""
    _ensure_imports()
    cfg_data = [("CFG_UART1_BAUDRATE", baud)]
    msg = _UBXMessage.config_set(7, 0, cfg_data)
    log.info(f"  UART1 baud rate = {baud}...")
    ser.write(msg.serialize())
    time.sleep(0.2)
    ser.baudrate = baud
    time.sleep(0.5)
    ser.reset_input_buffer()
    log.info(f"  UART1 baud rate = {baud}... OK")
    return True


# ── Passive verification ───────────────────────────────────────────────────── #

def listen_for_messages(ser, ubr, required=None, timeout_map=None):
    """Passively listen and report which UBX messages arrive.

    For each required message, waits up to its timeout (from timeout_map).
    Returns as soon as all required messages are seen, or when the longest
    timeout expires.

    Args:
        required: set of message identities to look for (default: REQUIRED_MESSAGES)
        timeout_map: dict of message identity -> timeout_s (default: MESSAGE_TIMEOUTS)

    Returns:
        (seen, missing, signal_info) where:
          seen: set of message identities observed
          missing: required messages not observed within their timeouts
          signal_info: dict with 'systems' (set of constellation names),
                       'dual_freq_svs' (count), 'rate_hz' (estimated)
    """
    if required is None:
        required = REQUIRED_MESSAGES
    if timeout_map is None:
        timeout_map = MESSAGE_TIMEOUTS

    max_timeout = max(timeout_map.get(m, 10) for m in required)
    deadline = time.monotonic() + max_timeout

    seen = set()
    pending = set(required)
    per_msg_deadline = {
        m: time.monotonic() + timeout_map.get(m, max_timeout)
        for m in required
    }

    # Signal analysis state
    rawx_times = []  # monotonic times of RAWX arrivals
    systems_seen = set()
    sig_pairs = {}   # sv -> set of signal roles seen
    SIG_NAMES = {
        (0, 0): 'GPS-L1CA', (0, 6): 'GPS-L5I', (0, 7): 'GPS-L5Q',
        (2, 0): 'GAL-E1C', (2, 1): 'GAL-E1B',
        (2, 3): 'GAL-E5aI', (2, 4): 'GAL-E5aQ',
        (3, 0): 'BDS-B1I', (3, 5): 'BDS-B2aI',
    }
    SYS_NAMES = {0: 'gps', 2: 'gal', 3: 'bds'}
    FREQ_BAND = {
        'GPS-L1CA': 'L1', 'GPS-L5I': 'L5', 'GPS-L5Q': 'L5',
        'GAL-E1C': 'E1', 'GAL-E1B': 'E1', 'GAL-E5aI': 'E5a', 'GAL-E5aQ': 'E5a',
        'BDS-B1I': 'B1', 'BDS-B2aI': 'B2a',
    }

    while time.monotonic() < deadline and pending:
        # Check per-message deadlines
        now = time.monotonic()
        expired = {m for m in pending if now > per_msg_deadline[m]}
        pending -= expired

        if not pending:
            break

        try:
            raw, parsed = ubr.read()
        except Exception:
            continue
        if parsed is None:
            continue

        ident = parsed.identity
        if ident not in seen:
            seen.add(ident)
            log.info(f"    Detected: {ident}")
        pending.discard(ident)

        # Analyze RAWX for signal/constellation info
        if ident == 'RXM-RAWX':
            rawx_times.append(time.monotonic())
            numMeas = getattr(parsed, 'numMeas', 0)
            for i in range(1, numMeas + 1):
                i2 = f"{i:02d}"
                gnss_id = getattr(parsed, f'gnssId_{i2}', None)
                sig_id = getattr(parsed, f'sigId_{i2}', None)
                sv_id = getattr(parsed, f'svId_{i2}', None)
                if gnss_id is None or sig_id is None:
                    continue
                sys_name = SYS_NAMES.get(gnss_id)
                if sys_name:
                    systems_seen.add(sys_name)
                sig_name = SIG_NAMES.get((gnss_id, sig_id))
                if sig_name is None:
                    continue
                band = FREQ_BAND.get(sig_name, '?')
                sv_key = f"{gnss_id}:{sv_id}"
                if sv_key not in sig_pairs:
                    sig_pairs[sv_key] = set()
                sig_pairs[sv_key].add(band)

    # Count dual-freq SVs (have signals on two distinct frequency bands)
    dual_freq_count = sum(1 for bands in sig_pairs.values() if len(bands) >= 2)

    # Estimate rate from RAWX intervals
    rate_hz = None
    if len(rawx_times) >= 3:
        intervals = [rawx_times[i+1] - rawx_times[i]
                     for i in range(len(rawx_times) - 1)]
        avg_interval = sum(intervals) / len(intervals)
        if avg_interval > 0:
            rate_hz = round(1.0 / avg_interval)

    missing = required - seen
    signal_info = {
        'systems': systems_seen,
        'dual_freq_svs': dual_freq_count,
        'rate_hz': rate_hz,
    }

    return seen, missing, signal_info


def full_configure(port, baud=9600, port_type="USB", rate_hz=1,
                   survey_dur_s=300, survey_acc_m=5.0, target_baud=460800,
                   do_reset=True):
    """Run full receiver configuration sequence.

    This is the programmatic equivalent of the old configure_f9t.py main().
    Returns True on success.
    """
    _ensure_imports()
    port_id = 1 if port_type == "UART" else 3

    ser, ubr = open_receiver(port, baud)

    if do_reset:
        factory_reset(ser, ubr)
        ser.close()
        ser, ubr = reopen_after_reset(port, wait_s=5)

    configure_signals(ser, ubr)
    l5_ok = configure_gps_l5_health(ser, ubr)

    if l5_ok:
        log.info("  Warm restart for L5 health override...")
        warm_restart(ser)
        ser.close()
        ser, ubr = reopen_after_reset(port, wait_s=10)

    configure_rate(ser, ubr, rate_hz)
    configure_messages(ser, ubr, port_id)
    configure_nmea_off(ser, ubr, port_id)
    configure_tmode(ser, ubr, survey_dur_s, survey_acc_m)

    if port_type == "UART" and target_baud != baud:
        configure_uart_baud(ser, ubr, target_baud)

    log.info("  Configuration saved to RAM + BBR + Flash.")

    seen, missing, _ = listen_for_messages(ser, ubr, timeout_map={
        m: 15 for m in REQUIRED_MESSAGES
    })
    ser.close()

    if missing:
        log.warning(f"Missing messages after configure: {missing}")
        return False

    log.info("All expected messages confirmed.")
    return True
