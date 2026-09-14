"""GPS receiver — NMEA parser for UART/USB GPS modules."""

import time
import glob
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import serial

from .config import GPS_DEVICE, GPS_BAUD_RATE
from .modem_location import ModemLocationBroker, managed_port_names

log = logging.getLogger(__name__)


@dataclass
class GpsFix:
    """Snapshot of current GPS state."""
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0
    speed_knots: float = 0.0
    satellites: int = 0
    satellites_visible: int = 0
    fix_quality: int = 0       # 0=no fix, 1=GPS, 2=DGPS
    hdop: float = 99.9
    timestamp: str = ""        # UTC time from NMEA (hhmmss.ss)
    received_at: float = 0.0
    valid: bool = False


class _LineBuffer:
    """Accumulate raw bytes and yield complete NMEA sentences."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, raw: bytes) -> List[str]:
        self._buf += raw
        lines: List[str] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            decoded = line.decode("ascii", errors="replace").strip()
            if decoded.startswith("$"):
                lines.append(decoded)
        # Prevent unbounded growth if no newlines arrive
        if len(self._buf) > 1024:
            self._buf = self._buf[-512:]
        return lines


class GpsManager:
    """Manage an external serial GPS or ModemManager's cached GNSS feed."""

    def __init__(self, device: str = GPS_DEVICE,
                 baud: int = GPS_BAUD_RATE,
                 modem_broker: Optional[ModemLocationBroker] = None) -> None:
        self.device = device
        self._baud = baud
        self._user_configured = bool(
            os.environ.get("WDG_GPS_DEVICE")
            or os.environ.get("JANOS_GPS_DEVICE"))
        self._conn: Optional[serial.Serial] = None
        self._buf = _LineBuffer()
        self.fix = GpsFix()
        self._available = False
        self._gsv_visible: dict = {}  # constellation prefix → satellite count
        self.modem_broker = modem_broker or ModemLocationBroker()
        self.provider = ""
        self.status_reason = ""

    @property
    def available(self) -> bool:
        return self._available

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def setup(self) -> bool:
        """Try to open GPS serial port. Returns True on success.
        Explicit external devices take priority.  Otherwise use ModemManager's
        GNSS feed, then probe only serial ports that ModemManager does not own.
        Never raises — GPS is optional."""
        device = self.device
        self.status_reason = ""

        if self._user_configured:
            if self._is_managed_port(device):
                self.status_reason = (
                    f"{device} is owned by ModemManager; configure an external GPS")
                log.warning(self.status_reason)
                return False
            if self._try_open(device):
                return True
            log.info("GPS device %s (from env) not available — GPS disabled",
                     device)
            return False

        # The internal SIM7600 is accessed only through ModemManager.  Starting
        # this broker does not open its GPS/AT/QMI device nodes.
        if self.modem_broker.acquire("gps", gps=True):
            if self.modem_broker.wait_ready(5):
                self.device = "ModemManager GNSS"
                self.provider = "modemmanager"
                self._available = True
                log.info("GPS provided by ModemManager")
                return True
            self.status_reason = self.modem_broker.error
            self.modem_broker.release("gps")
            self.modem_broker.close()
        else:
            self.status_reason = self.modem_broker.error

        # No usable internal GNSS — probe the configured default and external
        # ports only after excluding every device node managed by MM.
        if (not self._is_managed_port(device) and os.path.exists(device)
                and self._probe_nmea(device, self._baud)):
            if self._try_open(device):
                return True

        log.info("GPS not on %s, scanning for USB GPS...", device)
        detected = self._auto_detect(exclude={device})
        if detected:
            self.device = detected
            if self._try_open(detected):
                return True
        log.info("No GPS found — GPS disabled")
        return False

    @staticmethod
    def _probe_nmea(device: str, baud: int) -> bool:
        """Quick check if a serial port outputs NMEA sentences."""
        try:
            conn = serial.Serial(port=device, baudrate=baud, timeout=2)
            data = conn.read(512)
            conn.close()
            return b"$GP" in data or b"$GN" in data
        except Exception:
            return False

    def _try_open(self, device: str) -> bool:
        """Try to open a serial port as GPS. Returns True on success."""
        if not os.path.exists(device):
            return False
        if not os.access(device, os.R_OK):
            log.warning("No read access to %s", device)
            return False
        try:
            self._conn = serial.Serial(
                port=device,
                baudrate=self._baud,
                timeout=0,
            )
            self._conn.reset_input_buffer()
            self._available = True
            self.provider = "serial"
            log.info("GPS opened: %s @ %d baud", device, self._baud)
            return True
        except Exception as exc:
            log.debug("GPS open failed on %s: %s", device, exc)
            return False

    @staticmethod
    def _is_managed_port(device: str) -> bool:
        names = managed_port_names()
        name = Path(device).name
        # A missing inventory must fail closed for ttyUSB because that class is
        # commonly an AT/GPS interface of a composite modem.
        return name in names if names is not None else name.startswith("ttyUSB")

    @staticmethod
    def _unsafe_acm(path: str) -> bool:
        """Exclude known control devices whose open can reset hardware."""
        name = Path(path).name
        if not name.startswith("ttyACM"):
            return False
        try:
            node = (Path("/sys/class/tty") / name / "device").resolve()
            for parent in (node, *node.parents):
                vendor_path = parent / "idVendor"
                if vendor_path.is_file():
                    vendor = vendor_path.read_text().strip().lower()
                    product = ((parent / "product").read_text(errors="replace").lower()
                               if (parent / "product").is_file() else "")
                    return vendor == "303a" or "uconsole" in product or "clockwork" in product
        except OSError:
            return True
        return False

    @classmethod
    def _auto_detect(cls, exclude: Optional[set] = None) -> Optional[str]:
        """Probe serial ports outside ModemManager's physical device."""
        skip = exclude or set()
        managed = managed_port_names()
        candidates = sorted(
            glob.glob("/dev/ttyUSB*")
            + glob.glob("/dev/ttyACM*")
            + glob.glob("/dev/ttyAMA*")
        )
        for path in candidates:
            if path in skip:
                continue
            name = Path(path).name
            if (managed is None and name.startswith("ttyUSB")) or (
                    managed is not None and name in managed) or cls._unsafe_acm(path):
                continue
            if not os.access(path, os.R_OK):
                continue
            try:
                conn = serial.Serial(port=path, baudrate=9600, timeout=2)
                data = conn.read(512)
                conn.close()
                if b"$GP" in data or b"$GN" in data:
                    log.info("GPS auto-detected on %s", path)
                    return path
                log.debug("GPS probe %s — no NMEA data", path)
            except Exception:
                continue
        return None

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        if self.provider == "modemmanager":
            self.modem_broker.release("gps")
        # The same broker may have been used for cell tracking while an
        # external serial GPS supplied fixes. Always stop it on application
        # cleanup after WardriveUI has released its cell owner.
        self.modem_broker.close()
        self._available = False
        self.provider = ""

    @property
    def fd(self) -> int:
        """File descriptor for urwid watch_file."""
        if self._conn is None:
            raise RuntimeError("GPS port not open")
        return self._conn.fileno()

    # ------------------------------------------------------------------
    # Reading & parsing
    # ------------------------------------------------------------------

    def read_available(self) -> List[str]:
        """Non-blocking read — return complete NMEA sentences."""
        if self.provider == "modemmanager":
            return self.modem_broker.drain_nmea()
        if not self._conn:
            return []
        try:
            waiting = self._conn.in_waiting
            if waiting <= 0:
                return []
            raw = self._conn.read(waiting)
            return self._buf.feed(raw)
        except Exception as exc:
            log.debug("GPS read error: %s", exc)
            return []

    def process_sentences(self, sentences: List[str]) -> None:
        """Parse NMEA sentences and update self.fix."""
        for s in sentences:
            try:
                self._parse(s)
            except Exception as exc:
                log.debug("GPS parse error: %s — %s", s.strip(), exc)

    def _parse(self, sentence: str) -> None:
        old_timestamp = self.fix.timestamp
        # Strip checksum
        if "*" in sentence:
            sentence = sentence.split("*")[0]
        parts = sentence.split(",")
        if len(parts) < 3:
            return
        kind = parts[0]
        if kind in ("$GPGGA", "$GNGGA"):
            self._parse_gga(parts)
        elif kind in ("$GPRMC", "$GNRMC"):
            self._parse_rmc(parts)
        elif kind in ("$GPGSV", "$GLGSV", "$GNGSV", "$GBGSV", "$GAGSV"):
            self._parse_gsv(parts)
        if kind in ("$GPGGA", "$GNGGA", "$GPRMC", "$GNRMC"):
            if self.fix.timestamp != old_timestamp or not self.fix.valid:
                self.fix.received_at = time.monotonic()


    def _parse_gga(self, p: List[str]) -> None:
        """$GPGGA: time, lat, N/S, lon, E/W, quality, sats, hdop, alt, ..."""
        if len(p) < 10:
            return
        self.fix.fix_quality = int(p[6]) if p[6] else 0
        self.fix.valid = self.fix.fix_quality > 0 and bool(p[2] and p[3] and p[4] and p[5])
        if p[1]:
            self.fix.timestamp = p[1]
        self.fix.satellites = int(p[7]) if p[7] else 0
        self.fix.hdop = float(p[8]) if p[8] else 99.9
        if p[2] and p[3]:
            self.fix.latitude = self._to_decimal(p[2], p[3])
        if p[4] and p[5]:
            self.fix.longitude = self._to_decimal(p[4], p[5])
        if p[9]:
            self.fix.altitude = float(p[9])

    def _parse_rmc(self, p: List[str]) -> None:
        """$GPRMC: time, status, lat, N/S, lon, E/W, speed, ..."""
        if len(p) < 8:
            return
        self.fix.valid = (p[2] == "A") and bool(p[3] and p[4] and p[5] and p[6])
        if p[1]:
            self.fix.timestamp = p[1]
        if p[2] == "A":
            if p[3] and p[4]:
                self.fix.latitude = self._to_decimal(p[3], p[4])
            if p[5] and p[6]:
                self.fix.longitude = self._to_decimal(p[5], p[6])
            if p[7]:
                self.fix.speed_knots = float(p[7])

    def _parse_gsv(self, p: List[str]) -> None:
        """$xxGSV: total_msgs, msg_num, sats_in_view, ..."""
        if len(p) < 4:
            return
        prefix = p[0][:3]  # $GP, $GL, $GN, $GB, $GA
        total_visible = int(p[3]) if p[3] else 0
        self._gsv_visible[prefix] = total_visible
        self.fix.satellites_visible = sum(self._gsv_visible.values())

    @staticmethod
    def _to_decimal(value: str, direction: str) -> float:
        """Convert NMEA ddmm.mmmm to decimal degrees."""
        dot = value.index(".")
        degrees = int(value[:dot - 2])
        minutes = float(value[dot - 2:])
        result = degrees + minutes / 60.0
        if direction in ("S", "W"):
            result = -result
        return result
