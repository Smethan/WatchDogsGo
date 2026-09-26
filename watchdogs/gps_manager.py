"""GPS receiver — NMEA parser for UART/USB GPS modules."""

import glob
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import serial

from .config import GPS_BAUD_RATE, GPS_DEVICE
from .gpsd_client import GpsdClient
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
                 modem_broker: Optional[ModemLocationBroker] = None,
                 modem_enabled: bool = True,
                 gpsd_config: Optional[Path] = None) -> None:
        self.device = device
        self._configured_device = device
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
        self.modem_enabled = bool(modem_enabled)
        self.provider = ""
        self.status_reason = ""
        self._gpsd: Optional[GpsdClient] = None
        self._gpsd_config_path = gpsd_config or Path(
            os.environ.get("WDG_GPSD_CONFIG", "/etc/watchdogs/gpsd.conf"))
        gpsd_settings = self._load_gpsd_config(self._gpsd_config_path)
        env_host = os.environ.get("WDG_GPSD_HOST")
        env_port = os.environ.get("WDG_GPSD_PORT")
        self._gpsd_host = env_host or gpsd_settings.get("HOST", "127.0.0.1")
        try:
            self._gpsd_port = int(env_port or gpsd_settings.get("PORT", "2947"))
        except (TypeError, ValueError):
            self._gpsd_port = 2947
        self._gpsd_managed = bool(gpsd_settings or env_host or env_port)
        self._last_data_at = 0.0

    @property
    def available(self) -> bool:
        return self._available

    @property
    def data_flowing(self) -> bool:
        return bool(self._last_data_at
                    and time.monotonic() - self._last_data_at <= 5.0)

    @staticmethod
    def _load_gpsd_config(path: Path) -> dict[str, str]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return {}
        result: dict[str, str] = {}
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() in {"HOST", "PORT", "DEVICE"}:
                result[key.strip()] = value.strip().strip('"\'')
        return result

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def setup(self) -> bool:
        """Try to open GPS serial port. Returns True on success.
        Explicit external devices take priority.  Otherwise use ModemManager's
        GNSS feed, then probe only serial ports that ModemManager does not own.
        Never raises — GPS is optional."""
        if self._available:
            return True
        # ``self.device`` is the active provider label/path and becomes
        # ``ModemManager GNSS`` while the SIM7600 supplies fixes.  Keep the
        # configured UART separately so a runtime LTE-off switch can fall back
        # to the AIO receiver immediately.
        device = self._configured_device
        self.status_reason = ""

        if self._user_configured:
            # An explicit external device is the only ttyUSB path allowed while
            # LTE integration is disabled.  It is a deliberate operator choice;
            # automatic discovery below still skips every ttyUSB device.
            if self.modem_enabled and self._is_managed_port(device):
                self.status_reason = (
                    f"{device} is owned by ModemManager; configure an external GPS")
                log.warning(self.status_reason)
                return False
            console_reason = self._serial_console_reason(device)
            if console_reason:
                self.status_reason = console_reason
                log.warning(console_reason)
                return False
            if self._try_open(device):
                return True
            log.info("GPS device %s (from env) not available — GPS disabled",
                     device)
            return False

        if self.modem_enabled:
            # The internal SIM7600 is accessed only through ModemManager.
            # Starting this broker does not open its GPS/AT/QMI device nodes.
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
        else:
            log.info("LTE modem integration disabled; skipping ModemManager GPS")

        # setup.sh makes gpsd the authoritative raw-UART owner and writes the
        # marker read above. Never fall back to opening that same UART when a
        # managed gpsd endpoint is temporarily unavailable: doing so would
        # recreate the two-reader corruption this path exists to prevent.
        if self._gpsd_managed:
            if self._try_gpsd():
                return True
            self.status_reason = (
                f"gpsd unavailable at {self._gpsd_host}:{self._gpsd_port}")
            return False

        # No usable internal GNSS — probe only the documented platform UART
        # for this Compute Module.  Avoid broad ttyAMA/ttyS discovery because
        # another UART may carry the onboard Bluetooth HCI transport.
        console_reason = ""
        for candidate in self._platform_gps_candidates(device):
            if (self.modem_enabled and self._is_managed_port(candidate)):
                continue
            reason = self._serial_console_reason(candidate)
            if reason:
                console_reason = reason
                log.warning(reason)
                continue
            if (os.path.exists(candidate)
                    and self._probe_nmea(candidate, self._baud)
                    and self._try_open(candidate)):
                self.device = candidate
                return True

        log.info("GPS not on %s, scanning for USB GPS...", device)
        detected = self._auto_detect(exclude={device},
                                     include_tty_usb=self.modem_enabled)
        if detected:
            self.device = detected
            if self._try_open(detected):
                return True
        if console_reason:
            self.status_reason = console_reason
        elif not self.modem_enabled:
            self.status_reason = "LTE modem disabled; no external GPS found"
        log.info("No GPS found — GPS disabled")
        return False

    def _try_gpsd(self) -> bool:
        client = GpsdClient(self._gpsd_host, self._gpsd_port)
        try:
            client.connect()
        except (OSError, ConnectionError) as exc:
            log.debug("gpsd connection failed: %s", exc)
            return False
        self._gpsd = client
        self._available = True
        self.provider = "gpsd"
        self.device = f"gpsd {self._gpsd_host}:{self._gpsd_port}"
        self.status_reason = "gpsd connected; waiting for GPS data"
        log.info("GPS provided by gpsd at %s:%d",
                 self._gpsd_host, self._gpsd_port)
        return True

    def set_modem_enabled(self, enabled: bool, reconnect: bool = True) -> bool:
        """Apply the persistent LTE integration choice without touching scans.

        Disabling the modem releases ModemManager-backed GNSS immediately and
        optionally searches for an external/AIO receiver.  Enabling it leaves
        an already working external receiver alone, but can acquire SIM7600
        GNSS when no other provider is active.
        """
        enabled = bool(enabled)
        if enabled == self.modem_enabled:
            if reconnect and not self.available:
                return self.setup()
            return self.available
        if not enabled:
            self.modem_enabled = False
            if self.provider == "modemmanager":
                self.close()
            else:
                # A cell owner must be stopped by WardriveUI first.  Closing a
                # dormant broker here guarantees OFF means no MM worker remains.
                self.modem_broker.close()
            return self.setup() if reconnect and not self.available else self.available
        self.modem_enabled = True
        return self.setup() if reconnect and not self.available else self.available

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
    def _platform_model() -> str:
        for path in (Path("/proc/device-tree/model"),
                     Path("/sys/firmware/devicetree/base/model")):
            try:
                return path.read_text(errors="replace").replace("\x00", "").strip()
            except OSError:
                continue
        return ""

    @classmethod
    def _platform_gps_candidates(cls, configured: str) -> list[str]:
        """Return only UARTs documented for the installed Compute Module.

        HackerGadgets maps AIOv2 GPS to ttyS0 on CM4 and ttyAMA0 on CM5.
        ``serial0`` is retained as a stable alias fallback.  A non-default
        programmatic device remains authoritative; environment-configured
        devices are handled earlier in :meth:`setup`.
        """
        if configured != GPS_DEVICE:
            return [configured]
        model = cls._platform_model().lower()
        if "compute module 4" in model or "cm4" in model:
            candidates = ["/dev/ttyS0", "/dev/serial0"]
        elif "compute module 5" in model or "cm5" in model:
            candidates = ["/dev/ttyAMA0", "/dev/serial0"]
        else:
            candidates = [configured, "/dev/serial0"]
        result = []
        seen = set()
        for candidate in candidates:
            resolved = os.path.realpath(candidate) if os.path.exists(candidate) else candidate
            if resolved not in seen:
                seen.add(resolved)
                result.append(candidate)
        return result

    @staticmethod
    def _read_cmdline(path: Path) -> Optional[str]:
        try:
            return path.read_text(errors="replace").replace("\x00", " ").strip()
        except OSError:
            return None

    @staticmethod
    def _cmdline_uses_device(cmdline: str, device: str) -> bool:
        """Return whether a kernel command line reserves ``device`` as console."""
        names = {Path(device).name}
        try:
            names.add(Path(os.path.realpath(device)).name)
        except OSError:
            pass
        # Raspberry Pi firmware accepts serial0 in cmdline.txt and resolves it
        # to the concrete tty name before exposing /proc/cmdline.
        if names & {"ttyS0", "ttyAMA0"}:
            names.add("serial0")
        for token in cmdline.split():
            if not token.startswith("console="):
                continue
            name = token.partition("=")[2].partition(",")[0]
            if Path(name).name in names:
                return True
        return False

    @classmethod
    def _serial_console_reason(cls, device: str) -> str:
        """Explain an AIO UART conflict without opening the occupied port.

        The CM4 AIO GPS shares serial0.  When Linux was booted with that UART
        as a console, systemd starts a getty which competes for NMEA bytes and
        can make a powered receiver look absent.  If the persistent cmdline is
        already corrected, only a reboot is needed; otherwise give the exact
        configuration repair instead of reporting a missing GPS.
        """
        active = cls._read_cmdline(Path("/proc/cmdline"))
        if active is None or not cls._cmdline_uses_device(active, device):
            return ""

        persistent_found = False
        persistent_conflict = False
        for path in (Path("/boot/firmware/cmdline.txt"),
                     Path("/boot/cmdline.txt")):
            saved = cls._read_cmdline(path)
            if saved is None:
                continue
            persistent_found = True
            persistent_conflict = (
                persistent_conflict
                or cls._cmdline_uses_device(saved, device))

        if persistent_found and not persistent_conflict:
            return f"{device} is still the serial console; reboot required"
        return (
            f"{device} is the serial console; remove its console= entry from "
            "/boot/firmware/cmdline.txt and reboot")

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
    def _auto_detect(cls, exclude: Optional[set] = None,
                     include_tty_usb: bool = True) -> Optional[str]:
        """Probe serial ports outside ModemManager's physical device."""
        skip = exclude or set()
        # With LTE integration OFF, never query ModemManager and never probe a
        # generic ttyUSB automatically.  The documented AIOv2 platform UART was
        # already tried above; explicit USB GPS paths remain available through
        # WDG_GPS_DEVICE.
        managed = managed_port_names() if include_tty_usb else set()
        candidates = sorted(
            (glob.glob("/dev/ttyUSB*") if include_tty_usb else [])
            + glob.glob("/dev/ttyACM*")
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
        if self._gpsd:
            self._gpsd.close()
            self._gpsd = None
        if self.provider == "modemmanager":
            self.modem_broker.release("gps")
        # The same broker may have been used for cell tracking while an
        # external serial GPS supplied fixes. Always stop it on application
        # cleanup after WardriveUI has released its cell owner.
        self.modem_broker.close()
        self._available = False
        self.provider = ""
        self._last_data_at = 0.0

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
            sentences = self.modem_broker.drain_nmea()
            if sentences:
                self._last_data_at = time.monotonic()
            return sentences
        if self.provider == "gpsd":
            self._read_gpsd()
            return []
        if not self._conn:
            return []
        try:
            waiting = self._conn.in_waiting
            if waiting <= 0:
                return []
            raw = self._conn.read(waiting)
            if raw:
                self._last_data_at = time.monotonic()
            return self._buf.feed(raw)
        except Exception as exc:
            log.debug("GPS read error: %s", exc)
            return []

    def _read_gpsd(self) -> None:
        client = self._gpsd
        if client is None:
            self._available = False
            return
        try:
            reports = client.read_reports()
        except ConnectionError as exc:
            log.warning("GPS gpsd connection lost: %s", exc)
            client.close()
            self._gpsd = None
            self._available = False
            self.provider = ""
            self.status_reason = str(exc)
            return

        saw_navigation = False
        for report in reports:
            report_class = str(report.get("class", ""))
            if report_class == "TPV":
                saw_navigation = self._parse_gpsd_tpv(report) or saw_navigation
            elif report_class == "SKY":
                saw_navigation = self._parse_gpsd_sky(report) or saw_navigation
        if saw_navigation:
            self._last_data_at = time.monotonic()
            self.status_reason = (
                "GPS fix acquired" if self.fix.valid
                else "GPS data flowing; waiting for satellite fix")
        elif not self.data_flowing:
            self.status_reason = "gpsd connected; waiting for GPS data"

    @staticmethod
    def _finite_number(value) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    def _parse_gpsd_tpv(self, report: dict) -> bool:
        try:
            mode = int(report.get("mode", 0))
        except (TypeError, ValueError):
            mode = 0
        lat = self._finite_number(report.get("lat"))
        lon = self._finite_number(report.get("lon"))
        self.fix.valid = bool(mode >= 2 and lat is not None and lon is not None)
        if lat is not None:
            self.fix.latitude = lat
        if lon is not None:
            self.fix.longitude = lon
        for key in ("altMSL", "altHAE", "alt"):
            altitude = self._finite_number(report.get(key))
            if altitude is not None:
                self.fix.altitude = altitude
                break
        speed = self._finite_number(report.get("speed"))
        if speed is not None:
            self.fix.speed_knots = speed * 1.9438444924406
        if report.get("time"):
            self.fix.timestamp = str(report["time"])
        self.fix.fix_quality = 1 if self.fix.valid else 0
        self.fix.received_at = time.monotonic()
        return True

    def _parse_gpsd_sky(self, report: dict) -> bool:
        satellites = report.get("satellites")
        if isinstance(satellites, list):
            self.fix.satellites_visible = len(satellites)
            self.fix.satellites = sum(
                1 for satellite in satellites
                if isinstance(satellite, dict) and satellite.get("used") is True)
        hdop = self._finite_number(report.get("hdop"))
        if hdop is not None:
            self.fix.hdop = hdop
        return True

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
