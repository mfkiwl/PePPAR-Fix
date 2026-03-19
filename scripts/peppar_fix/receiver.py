"""Receiver driver abstraction for u-blox GNSS receivers.

Centralizes receiver-specific knowledge (signal ID mappings, CFG keys,
timing mode support) so the rest of the codebase can be receiver-agnostic.

Supported receivers:
    - ZED-F9T (PROTVER 29.x, timing-grade)
    - NEO-F10T (PROTVER 34.x, navigation-grade with L5)
"""

from abc import ABC, abstractmethod


class ReceiverDriver(ABC):
    """Abstract interface for a GNSS receiver.

    Subclasses provide receiver-specific signal ID mappings, configuration
    keys, and capabilities.  The rest of the pipeline (serial_reader,
    configure, phc_servo) uses this interface.
    """

    @property
    @abstractmethod
    def name(self):
        """Human-readable receiver name (e.g. 'ZED-F9T')."""

    @property
    @abstractmethod
    def protver(self):
        """Protocol version string (e.g. '29.00')."""

    @property
    @abstractmethod
    def signal_names(self):
        """Map (gnssId, sigId) → signal name string.

        Returns:
            dict mapping (int, int) tuples to strings like 'GPS-L1CA'.
        """

    @property
    @abstractmethod
    def signal_config(self):
        """CFG-SIGNAL key/value dict for dual-frequency PPP-AR.

        Returns:
            dict of CFG key names → int values.
        """

    @property
    @abstractmethod
    def supports_timing_mode(self):
        """Whether the receiver supports CFG-TMODE (fixed-position timing)."""

    @property
    @abstractmethod
    def supports_l5_health_override(self):
        """Whether GPS L5 health override (UBX-21038688) is needed/supported."""

    @property
    @abstractmethod
    def default_baud(self):
        """Default target baud rate for high-rate operation."""

    @property
    def sys_map(self):
        """Map gnssId → SV prefix character."""
        return {0: 'G', 2: 'E', 3: 'C'}

    def message_config(self, port_name):
        """CFG-MSGOUT key/value dict for required UBX messages.

        Args:
            port_name: port suffix like 'UART1', 'USB', etc.

        Returns:
            dict of CFG key names → int values.
        """
        msgs = {
            f"CFG_MSGOUT_UBX_RXM_RAWX_{port_name}": 1,
            f"CFG_MSGOUT_UBX_RXM_SFRBX_{port_name}": 1,
            f"CFG_MSGOUT_UBX_NAV_PVT_{port_name}": 1,
            f"CFG_MSGOUT_UBX_NAV_SAT_{port_name}": 5,
            f"CFG_MSGOUT_UBX_TIM_TP_{port_name}": 1,
        }
        return msgs

    def signal_name(self, gnss_id, sig_id):
        """Look up signal name for a (gnssId, sigId) pair.

        Returns:
            Signal name string, or None if unknown.
        """
        return self.signal_names.get((gnss_id, sig_id))


class UbloxDriver(ReceiverDriver):
    """Base class for u-blox receivers with shared UBX infrastructure.

    Provides common implementations for:
    - Timing mode configuration (CFG-TMODE VALSET)
    - L5 health override raw message
    - Rate configuration
    """

    def build_tmode_fixed_msg(self, ecef):
        """Build UBX CFG-VALSET bytes to switch to fixed-position timing mode.

        Sets TMODE=2 (fixed ECEF) with the given position.  Only valid for
        receivers that support CFG-TMODE (e.g. F9T).

        Args:
            ecef: array-like [x, y, z] in meters (ECEF)

        Returns:
            bytes ready to write to serial port, or None if unsupported/unavailable.
        """
        if not self.supports_timing_mode:
            return None

        try:
            from pyubx2 import UBXMessage
        except ImportError:
            return None

        # CFG-TMODE uses cm + 0.1mm high-precision split
        x_cm = int(ecef[0] * 100)
        y_cm = int(ecef[1] * 100)
        z_cm = int(ecef[2] * 100)
        x_hp = int(round((ecef[0] * 100 - x_cm) * 100))
        y_hp = int(round((ecef[1] * 100 - y_cm) * 100))
        z_hp = int(round((ecef[2] * 100 - z_cm) * 100))

        cfg_data = [
            ("CFG_TMODE_MODE", 2),
            ("CFG_TMODE_POS_TYPE", 0),
            ("CFG_TMODE_ECEF_X", x_cm),
            ("CFG_TMODE_ECEF_Y", y_cm),
            ("CFG_TMODE_ECEF_Z", z_cm),
            ("CFG_TMODE_ECEF_X_HP", x_hp),
            ("CFG_TMODE_ECEF_Y_HP", y_hp),
            ("CFG_TMODE_ECEF_Z_HP", z_hp),
            ("CFG_TMODE_FIXED_POS_ACC", 100),
        ]
        msg = UBXMessage.config_set(7, 0, cfg_data)
        return msg.serialize()

    def build_l5_health_override_msg(self):
        """Build raw UBX CFG-VALSET bytes for GPS L5 health override.

        Source: u-blox App Note UBX-21038688.
        Sets key 0x10320001 = 1 so the receiver substitutes GPS L1 C/A
        health status for L5 (needed because GPS flags L5 as unhealthy).

        Returns:
            bytes, or None if not needed for this receiver.
        """
        if not self.supports_l5_health_override:
            return None

        return bytes([
            0xB5, 0x62,
            0x06, 0x8A,
            0x09, 0x00,
            0x01, 0x07, 0x00, 0x00,
            0x01, 0x00, 0x32, 0x10,
            0x01,
            0xE5, 0x26,
        ])

    def rate_config(self, rate_hz):
        """CFG key/value dict for measurement rate.

        Args:
            rate_hz: measurement rate (1-10 Hz)

        Returns:
            dict of CFG key names → int values.
        """
        meas_ms = int(1000 / rate_hz)
        return {
            "CFG_RATE_MEAS": meas_ms,
            "CFG_RATE_NAV": 1,
            "CFG_RATE_TIMEREF": 0,
        }

    def tmode_survey_config(self, duration_s, accuracy_m):
        """CFG key/value dict for survey-in timing mode.

        Args:
            duration_s: minimum survey-in duration in seconds
            accuracy_m: accuracy threshold in meters

        Returns:
            dict, or None if timing mode not supported.
        """
        if not self.supports_timing_mode:
            return None

        acc_tenths_mm = int(accuracy_m * 1000) * 10
        return {
            "CFG_TMODE_MODE": 1,
            "CFG_TMODE_SVIN_MIN_DUR": duration_s,
            "CFG_TMODE_SVIN_ACC_LIMIT": acc_tenths_mm,
        }


class F9TDriver(UbloxDriver):
    """ZED-F9T timing receiver (PROTVER 29.x).

    The F9T is a timing-grade receiver with:
    - Fixed-position timing mode (CFG-TMODE)
    - TIM-TP with sub-ns qErr
    - Dual-frequency: GPS L1+L5, GAL E1+E5a, BDS B1+B2a
    - Quirk: Galileo E5a uses sigId 3/4 (not standard 5/6)
    - Needs GPS L5 health override (UBX-21038688)
    """

    @property
    def name(self):
        return 'ZED-F9T'

    @property
    def protver(self):
        return '29.00'

    @property
    def signal_names(self):
        # F9T-BOT (TIM 2.25): Galileo E5a uses sigId=3 (I) and sigId=4 (Q),
        # NOT the standard sigId=5/6 used by newer receivers.
        return {
            (0, 0): 'GPS-L1CA', (0, 3): 'GPS-L2CL', (0, 4): 'GPS-L2CM',
            (0, 6): 'GPS-L5I', (0, 7): 'GPS-L5Q',
            (2, 0): 'GAL-E1C', (2, 1): 'GAL-E1B',
            (2, 3): 'GAL-E5aI', (2, 4): 'GAL-E5aQ',
            (2, 5): 'GAL-E5bI', (2, 6): 'GAL-E5bQ',
            (3, 0): 'BDS-B1I', (3, 1): 'BDS-B1C',
            (3, 5): 'BDS-B2aI', (3, 7): 'BDS-B2I',
        }

    @property
    def signal_config(self):
        return {
            "CFG_SIGNAL_GPS_ENA": 1,
            "CFG_SIGNAL_GPS_L1CA_ENA": 1,
            "CFG_SIGNAL_GPS_L5_ENA": 1,
            "CFG_SIGNAL_GPS_L2C_ENA": 0,
            "CFG_SIGNAL_GAL_ENA": 1,
            "CFG_SIGNAL_GAL_E1_ENA": 1,
            "CFG_SIGNAL_GAL_E5A_ENA": 1,
            "CFG_SIGNAL_GAL_E5B_ENA": 0,
            "CFG_SIGNAL_BDS_ENA": 1,
            "CFG_SIGNAL_BDS_B1_ENA": 1,
            "CFG_SIGNAL_BDS_B2A_ENA": 1,
            "CFG_SIGNAL_BDS_B2_ENA": 0,
            "CFG_SIGNAL_GLO_ENA": 0,
            "CFG_SIGNAL_SBAS_ENA": 0,
            "CFG_SIGNAL_QZSS_ENA": 0,
        }

    @property
    def supports_timing_mode(self):
        return True

    @property
    def supports_l5_health_override(self):
        return True

    @property
    def default_baud(self):
        return 460800


class F10TDriver(UbloxDriver):
    """NEO-F10T navigation receiver (PROTVER 34.x).

    The F10T is a navigation-grade receiver with L5 support:
    - NO timing mode (no CFG-TMODE) — navigation receiver, not timing
    - TIM-TP available but without timing-grade qErr precision
    - Dual-frequency: GPS L1+L5, GAL E1+E5a, BDS B1+B2a
    - Standard Galileo E5a sigIds: 5 (I) and 6 (Q)
    - GPS L5 health override supported (same key 0x10320001)

    Key differences from F9T:
    - Galileo E5aI: sigId=5 (F10T) vs sigId=3 (F9T)
    - Galileo E5aQ: sigId=6 (F10T) vs sigId=4 (F9T)
    - No CFG-TMODE support (build_tmode_fixed_msg returns None)
    - No L2C tracking (same as F9T in our config — we use L5)
    """

    @property
    def name(self):
        return 'NEO-F10T'

    @property
    def protver(self):
        return '34.00'

    @property
    def signal_names(self):
        # F10T (PROTVER 34.x): standard Galileo E5a sigIds (5/6)
        return {
            (0, 0): 'GPS-L1CA',
            (0, 6): 'GPS-L5I', (0, 7): 'GPS-L5Q',
            (2, 0): 'GAL-E1C', (2, 1): 'GAL-E1B',
            (2, 5): 'GAL-E5aI', (2, 6): 'GAL-E5aQ',
            (3, 0): 'BDS-B1I', (3, 1): 'BDS-B1C',
            (3, 5): 'BDS-B2aI', (3, 7): 'BDS-B2I',
        }

    @property
    def signal_config(self):
        # Same dual-frequency PPP-AR config as F9T.
        # F10T does not support L2C at all, so no need to disable it.
        return {
            "CFG_SIGNAL_GPS_ENA": 1,
            "CFG_SIGNAL_GPS_L1CA_ENA": 1,
            "CFG_SIGNAL_GPS_L5_ENA": 1,
            "CFG_SIGNAL_GAL_ENA": 1,
            "CFG_SIGNAL_GAL_E1_ENA": 1,
            "CFG_SIGNAL_GAL_E5A_ENA": 1,
            "CFG_SIGNAL_GAL_E5B_ENA": 0,
            "CFG_SIGNAL_BDS_ENA": 1,
            "CFG_SIGNAL_BDS_B1_ENA": 1,
            "CFG_SIGNAL_BDS_B2A_ENA": 1,
            "CFG_SIGNAL_BDS_B2_ENA": 0,
            "CFG_SIGNAL_GLO_ENA": 0,
            "CFG_SIGNAL_SBAS_ENA": 0,
            "CFG_SIGNAL_QZSS_ENA": 0,
        }

    @property
    def supports_timing_mode(self):
        return False

    @property
    def supports_l5_health_override(self):
        return True

    @property
    def default_baud(self):
        return 460800


# Registry for receiver lookup by name
RECEIVER_DRIVERS = {
    'f9t': F9TDriver,
    'zed-f9t': F9TDriver,
    'f10t': F10TDriver,
    'neo-f10t': F10TDriver,
}


def get_driver(name):
    """Look up a receiver driver by name (case-insensitive).

    Args:
        name: receiver name like 'f9t', 'zed-f9t', 'f10t', 'neo-f10t'

    Returns:
        ReceiverDriver instance

    Raises:
        ValueError: if name is not recognized
    """
    cls = RECEIVER_DRIVERS.get(name.lower())
    if cls is None:
        valid = sorted(set(RECEIVER_DRIVERS.keys()))
        raise ValueError(f"Unknown receiver '{name}'. Valid: {valid}")
    return cls()
