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
_POLL = None
_Serial = None


def _ensure_imports():
    """Import pyubx2 and pyserial on first use."""
    global _UBXMessage, _UBXReader, _SET, _POLL, _Serial
    if _UBXMessage is not None:
        return
    try:
        from pyubx2 import UBXMessage, UBXReader, SET, POLL
        from serial import Serial
        _UBXMessage = UBXMessage
        _UBXReader = UBXReader
        _SET = SET
        _POLL = POLL
        _Serial = Serial
    except ImportError:
        raise ImportError("requires pyubx2 and pyserial: pip install pyubx2 pyserial")


# ── Signal configuration ──────────────────────────────────────────────────── #
#
# The F9T supports only two frequency bands simultaneously (single RF
# chain).  L2 and L5 are mutually exclusive — both signal configs
# explicitly disable the unused band.  configure_signals() sends all
# keys in a single VALSET so the receiver applies them atomically;
# individual key changes have ordering constraints (see
# docs/f9t-firmware-capabilities.md).
#
# L5 is the preferred second frequency for PPP-AR because:
#   - GPS L5, GAL E5a, BDS B2a share 1176.45 MHz center frequency,
#     giving consistent ionosphere-free combinations across constellations
#   - L5 has better code structure and lower noise than L2C
#   - CNES SSR phase biases match L5I (same carrier as L5Q)
#   - Works on both ZED-F9T (TIM 2.20) and ZED-F9T-20B (TIM 2.25)
# L2 is available on TIM 2.20 only (TIM 2.25 NAKs L2C).

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
    # GLONASS off (NAKs on both TIM 2.20 and 2.25 despite MON-VER string)
    "CFG_SIGNAL_GLO_ENA": 0,
    # SBAS/QZSS off
    "CFG_SIGNAL_SBAS_ENA": 0,
    "CFG_SIGNAL_QZSS_ENA": 0,
}

# L2 signal plan: GPS L1+L2C, GAL E1+E5a, BDS B1+B2.
# Only usable on ZED-F9T (TIM 2.20); ZED-F9T-20B NAKs L2C.
# Uses E5a (not E5b) for Galileo because E5b NAKs on all tested
# firmware versions — see docs/f9t-firmware-capabilities.md.
F9T_SIGNAL_CONFIG = {
    "CFG_SIGNAL_GPS_ENA": 1,
    "CFG_SIGNAL_GPS_L1CA_ENA": 1,
    "CFG_SIGNAL_GPS_L2C_ENA": 1,
    "CFG_SIGNAL_GPS_L5_ENA": 0,
    "CFG_SIGNAL_GAL_ENA": 1,
    "CFG_SIGNAL_GAL_E1_ENA": 1,
    "CFG_SIGNAL_GAL_E5A_ENA": 1,
    "CFG_SIGNAL_GAL_E5B_ENA": 0,
    "CFG_SIGNAL_BDS_ENA": 1,
    "CFG_SIGNAL_BDS_B1_ENA": 1,
    "CFG_SIGNAL_BDS_B2_ENA": 1,
    "CFG_SIGNAL_BDS_B2A_ENA": 0,
    "CFG_SIGNAL_GLO_ENA": 0,
    "CFG_SIGNAL_SBAS_ENA": 0,
    "CFG_SIGNAL_QZSS_ENA": 0,
}

# L5 signal plan: GPS L1+L5, GAL E1+E5a, BDS B1+B2a.
# Preferred for PPP-AR (see rationale above).  Works on all F9T firmware.
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
    """L2 signal plan: GPS L1+L2C, GAL E1+E5a, BDS B1+B2.

    Only usable on ZED-F9T (TIM 2.20).  ZED-F9T-20B (TIM 2.25) NAKs
    L2C and is locked to L5.  GAL uses E5a (not E5b) because E5b NAKs
    on all tested firmware — see docs/f9t-firmware-capabilities.md.
    """
    name = "ZED-F9T (L1/L2 profile)"
    protver = "27"
    default_baud = 460800
    supports_timing_mode = True
    supports_l5_health_override = True
    signal_config = F9T_SIGNAL_CONFIG
    if_pairs = (
        ('GPS', 'GPS-L1CA', 'GPS-L2CL', 'G'),
        ('GAL', 'GAL-E1C', 'GAL-E5aQ', 'E'),
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


def _poll_ubx(ser, ubr, cls, msg_id, target_identity, timeout=3.0):
    """Send a UBX POLL message and wait for the response.

    Args:
        ser: serial port
        ubr: UBXReader
        cls: UBX message class (e.g. "MON")
        msg_id: UBX message ID (e.g. "VER")
        target_identity: expected response identity (e.g. "MON-VER")
        timeout: seconds to wait

    Returns:
        parsed message on success, None on timeout
    """
    _ensure_imports()
    poll_msg = _UBXMessage(cls, msg_id, _POLL)
    ser.write(poll_msg.serialize())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw, parsed = ubr.read()
        except Exception:
            continue
        if parsed is None:
            continue
        if parsed.identity == target_identity:
            return parsed
    return None


def query_receiver_identity(port, baud=9600, ser=None, ubr=None):
    """Query receiver unique ID and firmware version.

    Sends UBX-MON-VER and UBX-SEC-UNIQID POLL messages to identify the
    physical receiver.  SEC-UNIQID is the primary key for state
    persistence — it's a hardware-fused ID unique to each u-blox chip.

    Args:
        port: serial port path
        baud: baud rate
        ser, ubr: if provided, reuse an already-open connection

    Returns:
        dict with keys:
            unique_id: int (SEC-UNIQID, 5 bytes as integer) or None
            unique_id_hex: str (hex representation) or None
            module: str (e.g. "ZED-F9T") or "unknown"
            firmware: str (e.g. "TIM 2.20") or "unknown"
            protver: str (e.g. "29.20") or None
        Returns None if the receiver doesn't respond at all.
    """
    _ensure_imports()
    opened_here = False
    if ser is None or ubr is None:
        ser, ubr = open_receiver(port, baud)
        opened_here = True

    result = {
        "unique_id": None,
        "unique_id_hex": None,
        "module": "unknown",
        "firmware": "unknown",
        "protver": None,
    }

    try:
        # Query MON-VER for module name and firmware version
        ver = _poll_ubx(ser, ubr, "MON", "MON-VER", "MON-VER", timeout=3.0)
        if ver is not None:
            sw_version = getattr(ver, "swVersion", b"")
            if isinstance(sw_version, bytes):
                sw_version = sw_version.decode("ascii", errors="replace").rstrip("\x00")
            result["firmware"] = sw_version.strip() if sw_version else "unknown"

            # Module name, FWVER, and protver are in the extension fields.
            # FWVER= is the application firmware name (e.g. "TIM 2.20"),
            # while swVersion is the base core version — always prefer FWVER=.
            for i in range(1, 10):
                ext = getattr(ver, f"extension_{i:02d}", None)
                if ext is None:
                    break
                if isinstance(ext, bytes):
                    ext = ext.decode("ascii", errors="replace").rstrip("\x00")
                ext = ext.strip()
                if ext.startswith("MOD="):
                    result["module"] = ext[4:]
                elif ext.startswith("PROTVER="):
                    result["protver"] = ext[8:]
                elif ext.startswith("FWVER="):
                    result["firmware"] = ext[6:]
        else:
            log.warning("MON-VER poll timed out — receiver may not be responding")

        # Query SEC-UNIQID for hardware unique ID.
        # pyubx2 doesn't recognize the SEC class, so we send the raw
        # UBX-SEC-UNIQID poll (class=0x27 id=0x03 len=0) and parse the
        # response bytes directly from the serial stream.
        import struct
        _cls_id = bytes([0x27, 0x03])
        _length = struct.pack("<H", 0)
        _ck_a = _ck_b = 0
        for _b in _cls_id + _length:
            _ck_a = (_ck_a + _b) & 0xFF
            _ck_b = (_ck_b + _ck_a) & 0xFF
        ser.write(b"\xb5\x62" + _cls_id + _length + bytes([_ck_a, _ck_b]))

        _deadline = time.monotonic() + 3.0
        _buf = b""
        while time.monotonic() < _deadline:
            _chunk = ser.read(256)
            if _chunk:
                _buf += _chunk
                if len(_buf) > 8192:
                    _buf = _buf[-1024:]
                _idx = _buf.find(b"\xb5\x62\x27\x03")
                if _idx >= 0 and len(_buf) >= _idx + 6:
                    _msg_len = struct.unpack_from("<H", _buf, _idx + 4)[0]
                    if len(_buf) >= _idx + 6 + _msg_len + 2:
                        _payload = _buf[_idx + 6:_idx + 6 + _msg_len]
                        if _msg_len >= 9:
                            uid_bytes = _payload[4:9]
                            result["unique_id"] = int.from_bytes(uid_bytes, "little")
                            result["unique_id_hex"] = uid_bytes.hex()
                        break
        if result["unique_id"] is None:
            log.warning("SEC-UNIQID timed out — receiver may not support it")

    finally:
        if opened_here:
            ser.close()

    if result["unique_id"] is None and result["module"] == "unknown":
        return None

    log.info("Receiver identity: %s fw=%s id=%s",
             result["module"], result["firmware"],
             result["unique_id_hex"] or "unknown")
    return result


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
    return configure_rate_ms(ser, ubr, meas_ms)


def configure_rate_ms(ser, ubr, meas_ms):
    """Set measurement rate in milliseconds."""
    rate_hz = 1000 / meas_ms
    return send_cfg(ser, ubr, {
        "CFG_RATE_MEAS": meas_ms,
        "CFG_RATE_NAV": 1,
        "CFG_RATE_TIMEREF": 0,
    }, f"Measurement rate = {rate_hz:.1f} Hz ({meas_ms} ms)")


def configure_messages(ser, ubr, port_id, sfrbx_rate=1):
    """Enable required UBX messages on the specified port.

    Args:
        sfrbx_rate: SFRBX output decimation (0=disabled, 1=every epoch).
                    When 0, PVT and NAV-SAT are also disabled to minimize
                    I2C bandwidth on E810 (15-byte AQ limit, ~1.6 kB/s).
    """
    pname = PORT_SUFFIX.get(port_id, f"port{port_id}")
    messages = {
        f"CFG_MSGOUT_UBX_RXM_RAWX_{pname}": 1,
        f"CFG_MSGOUT_UBX_TIM_TP_{pname}": 1,
        f"CFG_MSGOUT_UBX_RXM_SFRBX_{pname}": sfrbx_rate,
    }
    if sfrbx_rate > 0:
        messages[f"CFG_MSGOUT_UBX_NAV_PVT_{pname}"] = 1
        messages[f"CFG_MSGOUT_UBX_NAV_SAT_{pname}"] = 5
    else:
        messages[f"CFG_MSGOUT_UBX_NAV_PVT_{pname}"] = 0
        messages[f"CFG_MSGOUT_UBX_NAV_SAT_{pname}"] = 0
    # Enable the F9T's secondary navigation engine (NAV2).  This is a
    # completely independent position-fixing chain that runs even when
    # the primary engine is in TIME mode.  We use NAV2-PVT as an
    # independent sanity check on our PPP filter's position — if our
    # filter blows up but NAV2 still agrees with known_ecef, we know
    # the antenna hasn't moved and can re-seed instead of exiting.
    # See docs/architecture-vision.md "Three-source position consensus".
    messages["CFG_NAV2_OUT_ENABLED"] = 1
    messages[f"CFG_MSGOUT_UBX_NAV2_PVT_{pname}"] = 5  # every 5th epoch (~0.2 Hz)
    names = "RAWX, TIM-TP" if sfrbx_rate == 0 else "RAWX, SFRBX, PVT, SAT, TIM-TP, NAV2-PVT"
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
                          timeout_s=10, sfrbx_rate=1,
                          measurement_rate_ms=1000,
                          state_dir=None):
    """Check that dual-frequency observations are arriving; reconfigure if not.

    This is the single entry point for receiver readiness. It:
    1. Queries receiver identity (SEC-UNIQID + MON-VER)
    2. Checks/updates stored receiver state (receiver change detection)
    3. Listens for RAWX and checks for dual-freq GPS+GAL observations
    4. Auto-detects whether L5 or L2 is the active second frequency
    5. If single-frequency only, reconfigures for L1+L5 and retries
    6. Returns the appropriate driver and receiver identity

    Args:
        state_dir: directory for receiver state files (default: state/receivers)

    Returns:
        (driver, identity) tuple where:
            driver: ReceiverDriver instance matching the active signals,
                or None if receiver cannot be brought to dual-frequency state
            identity: dict from query_receiver_identity(), or None if
                the receiver didn't respond to identity queries

    Note on UBX command sequencing: each CFG-VALSET is sent and its
    ACK/NAK awaited synchronously before the next command. This avoids
    misattributing a NAK to the wrong command (see docs/receiver-signals.md).
    """
    from peppar_fix.receiver_state import (
        check_receiver_change, new_receiver_state,
        update_receiver_state, save_receiver_state,
    )

    if systems is None:
        systems = {'gps', 'gal'}

    # Step 0: Query receiver identity before anything else.
    # This is a quick UBX poll (two messages, ~1s) that identifies the
    # physical chip.  We need this before signal checks because a
    # receiver change may invalidate cached state (position, capabilities).
    identity = query_receiver_identity(port, baud)

    if identity is not None and identity.get("unique_id") is not None:
        stored, change_type = check_receiver_change(identity, port, state_dir)
        if change_type == "new":
            log.info("New receiver: %s (id=%s) — creating state",
                     identity["module"], identity.get("unique_id_hex", "?"))
            state = new_receiver_state(identity, port)
            save_receiver_state(state, state_dir)
        elif change_type == "receiver_changed":
            old_mod = stored.get("module", "?") if stored else "?"
            old_id = stored.get("unique_id_hex", "?") if stored else "?"
            log.warning("RECEIVER CHANGED on %s: was %s (id=%s), now %s (id=%s)",
                        port, old_mod, old_id,
                        identity["module"], identity.get("unique_id_hex", "?"))
            log.warning("Last-known position from previous receiver is NOT inherited")
            state = new_receiver_state(identity, port)
            save_receiver_state(state, state_dir)
        elif change_type == "firmware_changed":
            log.warning("Firmware changed on %s (%s): %s -> %s — re-probing capabilities",
                        port, identity["module"],
                        stored.get("firmware", "?"), identity.get("firmware", "?"))
            state, _ = update_receiver_state(stored, identity, port)
            save_receiver_state(state, state_dir)
        else:
            # Same receiver, same firmware — just update last_seen/port
            state, _ = update_receiver_state(stored, identity, port)
            save_receiver_state(state, state_dir)
    else:
        log.warning("Could not identify receiver on %s — state persistence disabled",
                    port)

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
        return driver, identity

    log.warning("Only %d/%d SVs have dual-freq observations — reconfiguring",
                dual, total)

    # Reconfigure: L5 first, L2C fallback.
    #
    # L5 is preferred for PPP-AR: GPS L5 + GAL E5a + BDS B2a all share
    # 1176.45 MHz, CNES SSR phase biases match L5I, and L5 has lower
    # code noise than L2C.  Both ZED-F9T (TIM 2.20) and ZED-F9T-20B
    # (TIM 2.25) accept L5.  L2C is only available on TIM 2.20; the
    # -20B NAKs it.  See docs/f9t-firmware-capabilities.md.
    #
    # configure_signals() sends all signal keys in a single VALSET,
    # so the receiver applies them atomically.  Do NOT set L2C and L5
    # keys individually — the receiver has ordering constraints when
    # switching bands via individual key changes.
    _ensure_imports()
    port_id = {"UART": 1, "UART2": 2, "USB": 3, "SPI": 4, "I2C": 0}
    pid = port_id.get(port_type, 3)

    ser, ubr = open_receiver(port, baud)

    # L5 first (preferred for PPP-AR — see rationale above)
    driver = F9TL5Driver()
    ok = configure_signals(ser, ubr, driver=driver)
    if not ok:
        # L5 NAK'd — fall back to L2C (only possible on TIM 2.20)
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
            return None, identity

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

    # Set measurement rate and enable required messages on the correct port
    configure_rate_ms(ser, ubr, measurement_rate_ms)
    configure_messages(ser, ubr, pid, sfrbx_rate=sfrbx_rate)
    configure_nmea_off(ser, ubr, pid)
    ser.close()

    # Verify: re-check for dual-freq observations
    log.info("Verifying dual-frequency observations after reconfiguration...")
    dual, total, sigs = _check_dual_freq(port, baud, driver, systems,
                                         timeout_s=timeout_s)
    if dual >= 4:
        log.info("Receiver reconfigured OK: %d/%d SVs dual-freq", dual, total)
        return driver, identity

    log.error("Receiver still not producing dual-freq observations "
              "after reconfiguration (%d/%d SVs)", dual, total)
    return None, identity
