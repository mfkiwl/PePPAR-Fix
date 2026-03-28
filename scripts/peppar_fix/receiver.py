"""F9T receiver configuration and verification utilities.

Extracted from configure_f9t.py for reuse by peppar-rx-config and other tools.
"""

import logging
import os
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

F9T_SIGNAL_CONFIG = {
    "CFG_SIGNAL_GPS_ENA": 1,
    "CFG_SIGNAL_GPS_L1CA_ENA": 1,
    "CFG_SIGNAL_GPS_L2C_ENA": 1,
    "CFG_SIGNAL_GPS_L5_ENA": 0,
    "CFG_SIGNAL_GAL_ENA": 1,
    "CFG_SIGNAL_GAL_E1_ENA": 1,
    "CFG_SIGNAL_GAL_E5A_ENA": 0,
    "CFG_SIGNAL_GAL_E5B_ENA": 1,
    "CFG_SIGNAL_BDS_ENA": 1,
    "CFG_SIGNAL_BDS_B1_ENA": 1,
    "CFG_SIGNAL_BDS_B2_ENA": 1,
    "CFG_SIGNAL_BDS_B2A_ENA": 0,
    "CFG_SIGNAL_GLO_ENA": 0,
    "CFG_SIGNAL_SBAS_ENA": 0,
    "CFG_SIGNAL_QZSS_ENA": 0,
}

F9T_L5_SIGNAL_CONFIG = {
    "CFG_SIGNAL_GPS_ENA": 1,
    "CFG_SIGNAL_GPS_L1CA_ENA": 1,
    "CFG_SIGNAL_GPS_L2C_ENA": 0,
    "CFG_SIGNAL_GPS_L5_ENA": 1,
    "CFG_SIGNAL_GAL_ENA": 1,
    "CFG_SIGNAL_GAL_E1_ENA": 1,
    "CFG_SIGNAL_GAL_E5A_ENA": 1,
    "CFG_SIGNAL_GAL_E5B_ENA": 0,
    "CFG_SIGNAL_BDS_ENA": 1,
    "CFG_SIGNAL_BDS_B1_ENA": 1,
    "CFG_SIGNAL_BDS_B2_ENA": 0,
    "CFG_SIGNAL_BDS_B2A_ENA": 1,
    "CFG_SIGNAL_GLO_ENA": 0,
    "CFG_SIGNAL_SBAS_ENA": 0,
    "CFG_SIGNAL_QZSS_ENA": 0,
}

# Required UBX messages for peppar-fix operation.
# SFRBX and PVT are optional on bandwidth-limited transports (E810 I2C).
REQUIRED_MESSAGES = {"RXM-RAWX", "RXM-SFRBX", "NAV-PVT", "TIM-TP"}
REQUIRED_MESSAGES_MINIMAL = {"RXM-RAWX", "TIM-TP"}

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


def required_messages(minimal=False):
    """Return the set of required messages.

    Args:
        minimal: If True, return only RAWX+TIM-TP (for bandwidth-limited
                 transports like E810 I2C where SFRBX/PVT are disabled to
                 stay within the 15-byte AQ command throughput ceiling).
    """
    return REQUIRED_MESSAGES_MINIMAL if minimal else REQUIRED_MESSAGES

# Port ID mapping
PORT_SUFFIX = {0: "I2C", 1: "UART1", 2: "UART2", 3: "USB", 4: "SPI"}

SIGNAL_NAMES = {
    (0, 0): "GPS-L1CA",
    (0, 3): "GPS-L2CL",
    (0, 4): "GPS-L2CM",
    (0, 6): "GPS-L5I",
    (0, 7): "GPS-L5Q",
    (2, 0): "GAL-E1C",
    (2, 1): "GAL-E1B",
    (2, 3): "GAL-E5aI",
    (2, 4): "GAL-E5aQ",
    (2, 5): "GAL-E5bI",
    (2, 6): "GAL-E5bQ",
    (3, 0): "BDS-B1I",
    (3, 5): "BDS-B2aI",
    (3, 2): "BDS-B2I",
}

SYS_MAP = {
    0: "gps",
    2: "gal",
    3: "bds",
}


class ReceiverDriver:
    """Receiver-specific signal and capability metadata."""

    name = "Generic u-blox"
    protver = "unknown"
    default_baud = 115200
    supports_timing_mode = False
    supports_l5_health_override = False
    signal_config = SIGNAL_CONFIG
    signal_names = SIGNAL_NAMES
    sys_map = SYS_MAP
    if_pairs = ()

    def signal_name(self, gnss_id, sig_id):
        return self.signal_names.get((gnss_id, sig_id))

    def build_tmode_fixed_msg(self, ecef):
        return None


class F9TDriver(ReceiverDriver):
    name = "ZED-F9T"
    protver = "27"
    default_baud = 460800
    supports_timing_mode = True
    supports_l5_health_override = True
    signal_config = F9T_SIGNAL_CONFIG
    if_pairs = (
        ('GPS', 'GPS-L1CA', 'GPS-L2CL', 'G'),
        ('GAL', 'GAL-E1C', 'GAL-E5bQ', 'E'),
        ('BDS', 'BDS-B1I', 'BDS-B2I', 'C'),
    )

    def build_tmode_fixed_msg(self, ecef):
        _ensure_imports()
        x_cm = int(round(float(ecef[0]) * 100))
        y_cm = int(round(float(ecef[1]) * 100))
        z_cm = int(round(float(ecef[2]) * 100))
        cfg_data = [
            ("CFG_TMODE_MODE", 2),
            ("CFG_TMODE_POS_TYPE", 0),
            ("CFG_TMODE_ECEF_X", x_cm),
            ("CFG_TMODE_ECEF_Y", y_cm),
            ("CFG_TMODE_ECEF_Z", z_cm),
            ("CFG_TMODE_ECEF_X_HP", 0),
            ("CFG_TMODE_ECEF_Y_HP", 0),
            ("CFG_TMODE_ECEF_Z_HP", 0),
            ("CFG_TMODE_FIXED_POS_ACC", 100),
        ]
        return _UBXMessage.config_set(7, 0, cfg_data).serialize()


class F9TL5Driver(F9TDriver):
    name = "ZED-F9T (L1/L5 profile)"
    signal_config = F9T_L5_SIGNAL_CONFIG
    if_pairs = (
        ('GPS', 'GPS-L1CA', 'GPS-L5Q', 'G'),
        ('GAL', 'GAL-E1C', 'GAL-E5aQ', 'E'),
        ('BDS', 'BDS-B1I', 'BDS-B2aI', 'C'),
    )


class F10TDriver(ReceiverDriver):
    name = "NEO-F10T"
    protver = "32"
    default_baud = 115200
    supports_timing_mode = False
    supports_l5_health_override = False


def get_driver(name):
    """Return the receiver driver for a CLI receiver name."""
    key = (name or "f9t").strip().lower()
    if key == "f9t":
        return F9TDriver()
    if key in {"f9t-l5", "f9t_l5"}:
        return F9TL5Driver()
    if key == "f10t":
        return F10TDriver()
    raise ValueError(f"Unknown receiver model: {name}")


# ── Low-level UBX helpers ──────────────────────────────────────────────────── #

def probe_baud(port):
    """Try common baud rates and return the one that produces valid UBX/NMEA."""
    basename = os.path.basename(port)
    if basename.startswith("gnss") and basename[4:].isdigit():
        return None
    from peppar_fix.gnss_stream import open_gnss
    for baud in [9600, 38400, 115200, 230400, 460800]:
        try:
            ser, _device_type = open_gnss(port, baud)
            time.sleep(1.5)
            data = ser.read(500)
            ser.close()
            if b'\xb5\x62' in data or b'$G' in data:
                return baud
        except RuntimeError:
            raise
        except Exception:
            pass
    return None


def open_receiver(port, baud=9600):
    """Open serial port and return (Serial, UBXReader) pair."""
    _ensure_imports()
    from peppar_fix.gnss_stream import open_gnss
    ser, _device_type = open_gnss(port, baud)
    ubr = _UBXReader(ser, protfilter=2)  # UBX protocol only
    return ser, ubr


def wait_ack(ubr, cls_name="CFG", msg_name="VALSET", timeout=3.0):
    """Wait for UBX-ACK-ACK or UBX-ACK-NAK.

    Reads raw bytes from the underlying stream to avoid the cost of
    full pyubx2 deserialization on every observation message while
    waiting for a 10-byte ACK.  Falls back to pyubx2 parsing if the
    stream doesn't expose a raw read interface.
    """
    stream = getattr(ubr, '_stream', None)
    raw_read = getattr(stream, 'read_raw', None) if stream else None
    if raw_read is None:
        # Fallback: use pyubx2 (slower but always works)
        return _wait_ack_parsed(ubr, cls_name, msg_name, timeout)

    # Scan raw bytes for ACK-ACK (b5 62 05 01) or ACK-NAK (b5 62 05 00).
    # This bypasses pyubx2 deserialization — just a byte pattern search.
    # os.read() on the blocking fd does the waiting; no polling or sleeping.
    ACK_ACK = b'\xb5\x62\x05\x01'
    ACK_NAK = b'\xb5\x62\x05\x00'
    buf = b''
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = raw_read(256)
        except Exception:
            continue
        if not chunk:
            continue
        buf += chunk
        # Keep buffer bounded — we only need to find a 10-byte ACK
        if len(buf) > 4096:
            buf = buf[-256:]
        if ACK_ACK in buf:
            return True
        if ACK_NAK in buf:
            log.warning("NAK received for %s-%s", cls_name, msg_name)
            return False
    return False


def _wait_ack_parsed(ubr, cls_name, msg_name, timeout):
    """Fallback ACK wait using full pyubx2 parsing."""
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
            log.warning("NAK received for %s-%s", cls_name, msg_name)
            return False
    return False


def send_cfg(ser, ubr, key_values, description="", layers=7):
    """Send a VALSET configuration and wait for ACK.

    UBX ACK/NAK responses are sequenced against commands but asynchronous.
    We MUST wait for the ACK/NAK from each command before sending the next,
    or a NAK could be misattributed to the wrong command. A timeout (3s)
    is treated as equivalent to NAK. See docs/receiver-signals.md.

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
    basename = os.path.basename(port)
    is_kernel_gnss = basename.startswith("gnss") and basename[4:].isdigit()
    for attempt in range(retries):
        time.sleep(wait_s)
        if is_kernel_gnss:
            try:
                log.info(f"  Reopening kernel GNSS device {port} after reset")
                return open_receiver(port, 115200)
            except Exception:
                log.info(f"  Reopen attempt {attempt + 1} failed, retrying...")
                continue
        else:
            baud = probe_baud(port)
            if baud is not None:
                log.info(f"  Receiver found at {baud} baud after reset")
                return open_receiver(port, baud)
            log.info(f"  Probe attempt {attempt + 1} failed, retrying...")
    raise RuntimeError(f"Cannot find receiver on {port} after reset")


def _driver_band_summary(driver):
    """Return a short human-readable summary of the receiver IF plan."""
    parts = []
    for sys_name, f1, f2, _rinex_prefix in getattr(driver, "if_pairs", ()):
        parts.append(f"{sys_name} {f1}+{f2}")
    return ", ".join(parts) if parts else driver.name


def configure_signals(ser, ubr, driver=None):
    """Enable dual-frequency signals for the selected receiver profile."""
    driver = driver or get_driver("f9t")
    return send_cfg(
        ser,
        ubr,
        driver.signal_config,
        f"Signals: {_driver_band_summary(driver)}",
    )


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


def configure_messages(ser, ubr, port_id, minimal=False):
    """Enable required UBX messages on the specified port.

    Args:
        minimal: If True, enable only RAWX+TIM-TP.  Used for
                 bandwidth-limited transports (E810 I2C, 15-byte AQ limit).
                 SFRBX, PVT, and NAV-SAT are omitted to stay within the
                 ~1.5 kB/s I2C throughput ceiling.
    """
    pname = PORT_SUFFIX.get(port_id, f"port{port_id}")
    messages = {
        f"CFG_MSGOUT_UBX_RXM_RAWX_{pname}": 1,
        f"CFG_MSGOUT_UBX_TIM_TP_{pname}": 1,
    }
    if not minimal:
        messages[f"CFG_MSGOUT_UBX_RXM_SFRBX_{pname}"] = 1
        messages[f"CFG_MSGOUT_UBX_NAV_PVT_{pname}"] = 1
        messages[f"CFG_MSGOUT_UBX_NAV_SAT_{pname}"] = 5
    names = "RAWX, TIM-TP" if minimal else "RAWX, SFRBX, PVT, SAT, TIM-TP"
    return send_cfg(ser, ubr, messages, f"UBX messages on {pname}: {names}")


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

def listen_for_messages(ser, ubr, required=None, timeout_map=None, driver=None):
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
    driver = driver or get_driver("f9t")
    SIG_NAMES = driver.signal_names
    SYS_NAMES = {0: 'gps', 2: 'gal', 3: 'bds'}
    FREQ_BAND = {}
    for _sys_name, sig1, sig2, _rinex_prefix in getattr(driver, "if_pairs", ()):
        FREQ_BAND[sig1] = sig1.split("-")[-1]
        FREQ_BAND[sig2] = sig2.split("-")[-1]

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
                   do_reset=True, receiver="f9t"):
    """Run full receiver configuration sequence.

    This is the programmatic equivalent of the old configure_f9t.py main().
    Returns True on success.
    """
    _ensure_imports()
    port_id = {"UART": 1, "UART2": 2, "USB": 3, "SPI": 4, "I2C": 0}[port_type]
    driver = get_driver(receiver)

    ser, ubr = open_receiver(port, baud)

    if do_reset:
        factory_reset(ser, ubr)
        ser.close()
        ser, ubr = reopen_after_reset(port, wait_s=5)

    configure_signals(ser, ubr, driver=driver)
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

    seen, missing, _ = listen_for_messages(
        ser,
        ubr,
        timeout_map={m: 15 for m in REQUIRED_MESSAGES},
        driver=driver,
    )
    ser.close()

    if missing:
        log.warning(f"Missing messages after configure: {missing}")
        return False

    log.info("All expected messages confirmed.")
    return True


# ── Startup signal validation ─────────────────────────────────────────────── #

def _check_dual_freq(port, baud, driver, systems, timeout_s=8):
    """Listen for one RAWX epoch and check for dual-frequency observations.

    Returns (dual_count, total_count, sig_ids_seen) where dual_count is
    the number of GPS+GAL SVs with both f1 and f2 observations.
    """
    _ensure_imports()
    from collections import defaultdict
    ser, ubr = open_receiver(port, baud)
    SIG_NAMES = driver.signal_names

    # Build role lookup from driver's IF pairs
    sig_roles = {}
    for _sys, sig_f1, sig_f2, prefix in driver.if_pairs:
        sig_roles[sig_f1] = ('f1', prefix)
        sig_roles[sig_f2] = ('f2', prefix)

    deadline = time.monotonic() + timeout_s
    dual_count = 0
    total_count = 0
    sig_ids_seen = set()

    while time.monotonic() < deadline:
        try:
            raw, parsed = ubr.read()
        except Exception:
            continue
        if parsed is None:
            continue
        if not hasattr(parsed, 'identity') or parsed.identity != 'RXM-RAWX':
            continue

        # Process one RAWX epoch
        sv_roles = defaultdict(set)
        n = getattr(parsed, 'numMeas', 0)
        for i in range(1, n + 1):
            i2 = f"{i:02d}"
            gnss_id = getattr(parsed, f'gnssId_{i2}', None)
            sig_id = getattr(parsed, f'sigId_{i2}', None)
            sv_id = getattr(parsed, f'svId_{i2}', None)
            if gnss_id is None or sig_id is None:
                continue
            sig_ids_seen.add((gnss_id, sig_id))
            sig_name = SIG_NAMES.get((gnss_id, sig_id))
            if sig_name is None or sig_name not in sig_roles:
                continue
            role, prefix = sig_roles[sig_name]
            # Only count GPS and GAL for the check (BDS optional)
            sys_name = SYS_MAP.get(gnss_id)
            if systems and sys_name not in systems:
                continue
            sv = f"{prefix}{int(sv_id):02d}"
            sv_roles[sv].add(role)

        for sv, roles in sv_roles.items():
            total_count += 1
            if 'f1' in roles and 'f2' in roles:
                dual_count += 1
        break  # one epoch is enough

    ser.close()
    return dual_count, total_count, sig_ids_seen


def _detect_second_freq(sig_ids_seen):
    """Determine whether receiver is outputting L5/E5a or L2/E5b signals.

    Returns 'l5' if L5/E5a signals detected, 'l2' if L2/E5b detected,
    None if only L1/E1 (single-frequency).
    """
    # GPS sigId 7 = L5Q, sigId 3 = L2CL
    # GAL sigId 4 = E5aQ, sigId 6 = E5bQ
    has_l5 = (0, 7) in sig_ids_seen or (2, 4) in sig_ids_seen
    has_l2 = (0, 3) in sig_ids_seen or (2, 6) in sig_ids_seen
    if has_l5:
        return 'l5'
    if has_l2:
        return 'l2'
    return None


def ensure_receiver_ready(port, baud, port_type="USB", systems=None,
                          timeout_s=10, minimal_messages=False):
    """Check that dual-frequency observations are arriving; reconfigure if not.

    This is the single entry point for receiver readiness. It:
    1. Listens for RAWX and checks for dual-freq GPS+GAL observations
    2. Auto-detects whether L5 or L2 is the active second frequency
    3. If single-frequency only, reconfigures for L1+L5 and retries
    4. Returns the appropriate driver for the detected signal plan

    Args:
        minimal_messages: If True, configure only RAWX+TIM-TP on the GNSS
            port.  Used for E810 I2C where the 15-byte AQ command limit
            caps throughput at ~1.5 kB/s.

    Returns:
        driver: ReceiverDriver instance matching the active signals
        None if receiver cannot be brought to dual-frequency state

    Note on UBX command sequencing: each CFG-VALSET is sent and its
    ACK/NAK awaited synchronously before the next command. This avoids
    misattributing a NAK to the wrong command (see docs/receiver-signals.md).
    """
    if systems is None:
        systems = {'gps', 'gal'}

    # First check: are dual-freq observations already arriving?
    log.info("Checking receiver signal status...")
    dual, total, sigs = _check_dual_freq(port, baud, F9TL5Driver(), systems,
                                         timeout_s=timeout_s)

    if dual >= 4:
        freq_type = _detect_second_freq(sigs)
        if freq_type == 'l5':
            driver = F9TL5Driver()
        elif freq_type == 'l2':
            driver = F9TDriver()
        else:
            driver = F9TL5Driver()
        log.info("Receiver OK: %d/%d SVs dual-freq (%s), using %s",
                 dual, total, freq_type or "?", driver.name)
        return driver

    log.warning("Only %d/%d SVs have dual-freq observations — reconfiguring",
                dual, total)

    # Reconfigure: try L1+L5 first, fall back to L1+L2C if NAK'd.
    # Newer F9T firmware (-20B) supports L5; older (-00B) only has L2C.
    _ensure_imports()
    port_id = {"UART": 1, "UART2": 2, "USB": 3, "SPI": 4, "I2C": 0}
    pid = port_id.get(port_type, 3)

    ser, ubr = open_receiver(port, baud)

    # Try L5 first (preferred — better geometry, no health override needed on newer FW)
    driver = F9TL5Driver()
    ok = configure_signals(ser, ubr, driver=driver)
    if not ok:
        # L5 NAK'd — this firmware only supports L2C
        log.info("L5 signal config NAK'd — falling back to L2C")
        driver = F9TDriver()
        ok = configure_signals(ser, ubr, driver=driver)
    if not ok:
        log.warning("L2C also NAK'd — trying with factory reset")
        ser.close()
        ser, ubr = open_receiver(port, baud)
        factory_reset(ser, ubr)
        ser.close()
        ser, ubr = reopen_after_reset(port, wait_s=5)
        driver = F9TDriver()
        ok = configure_signals(ser, ubr, driver=driver)
        if not ok:
            log.error("Signal configuration failed even after factory reset")
            ser.close()
            return None

    # GPS L5 health override — only needed for L5 drivers
    if isinstance(driver, F9TL5Driver):
        l5_ok = configure_gps_l5_health(ser, ubr)
        if l5_ok:
            log.info("GPS L5 health override applied — warm restarting")
            warm_restart(ser)
            ser.close()
            ser, ubr = reopen_after_reset(port, wait_s=10)
        else:
            log.info("GPS L5 health override not supported by this firmware")
    else:
        log.info("L2C driver — no L5 health override needed")

    # Enable required messages on the correct port
    configure_messages(ser, ubr, pid, minimal=minimal_messages)
    configure_nmea_off(ser, ubr, pid)
    ser.close()

    # Verify: re-check for dual-freq observations
    log.info("Verifying dual-frequency observations after reconfiguration...")
    dual, total, sigs = _check_dual_freq(port, baud, driver, systems,
                                         timeout_s=timeout_s)
    if dual >= 4:
        log.info("Receiver reconfigured OK: %d/%d SVs dual-freq", dual, total)
        return driver

    log.error("Receiver still not producing dual-freq observations "
              "after reconfiguration (%d/%d SVs)", dual, total)
    return None
