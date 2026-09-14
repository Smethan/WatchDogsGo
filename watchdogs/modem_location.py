"""Crash-resistant ModemManager location broker for the uConsole SIM7600.

ModemManager remains the sole owner of the modem control plane.  This module
only uses the cached ``Modem.Location`` D-Bus interface; it never opens an AT,
GPS, or QMI device node.  One worker owns its SystemBus connection so a slow
or restarting modem can never block the Pyxel thread.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from pathlib import Path
import threading
import time
from typing import Callable, Optional


MM_SERVICE = "org.freedesktop.ModemManager1"
MM_ROOT = "/org/freedesktop/ModemManager1"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
PROPERTIES = "org.freedesktop.DBus.Properties"
MODEM = "org.freedesktop.ModemManager1.Modem"
MODEM_3GPP = "org.freedesktop.ModemManager1.Modem.Modem3gpp"
LOCATION = "org.freedesktop.ModemManager1.Modem.Location"
SIGNAL = "org.freedesktop.ModemManager1.Modem.Signal"

LOCATION_3GPP_LAC_CI = 1 << 0
LOCATION_GPS_NMEA = 1 << 2
PORT_QMI = 6

# MMModemAccessTechnology values from ModemManager's public enum.
ACCESS_GSM_FAMILY = sum(1 << bit for bit in range(1, 5))
ACCESS_WCDMA_FAMILY = sum(1 << bit for bit in range(5, 10))
ACCESS_LTE = 1 << 14
ACCESS_5GNR = 1 << 15


@dataclass(frozen=True)
class CellIdentity:
    operator_id: str
    technology: str
    lac: Optional[int]
    tac: Optional[int]
    cell_id: int

    @property
    def area(self) -> Optional[int]:
        return self.tac if self.technology in ("LTE", "NR") else self.lac

    @property
    def identity(self) -> Optional[str]:
        if self.area is None:
            return None
        return f"{self.operator_id}_{self.area}_{self.cell_id}"


@dataclass(frozen=True)
class ModemLocationSnapshot:
    observed_monotonic: float
    observed_utc: float
    modem_generation: int
    operator_id: Optional[str]
    technology: Optional[str]
    lac: Optional[int]
    tac: Optional[int]
    cell_id: Optional[int]
    nmea: tuple[str, ...]
    signal_dbm: Optional[float]
    signal_quality_percent: Optional[int]
    modem_path: str
    model: str
    revision: str
    qmi_device: Optional[str]

    @property
    def area(self) -> Optional[int]:
        return self.tac if self.technology in ("LTE", "NR") else self.lac

    @property
    def identity(self) -> Optional[str]:
        if not self.operator_id or self.area is None or self.cell_id is None:
            return None
        return f"{self.operator_id}_{self.area}_{self.cell_id}"


def technology_from_access(mask: int) -> Optional[str]:
    """Return the most capable active 3GPP technology in an MM bitmask."""
    if mask & ACCESS_5GNR:
        return "NR"
    if mask & ACCESS_LTE:
        return "LTE"
    if mask & ACCESS_WCDMA_FAMILY:
        return "WCDMA"
    if mask & ACCESS_GSM_FAMILY:
        return "GSM"
    return None


def _hex_field(value: str) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def parse_3gpp_location(value: object, access_technologies: int) -> Optional[CellIdentity]:
    """Parse ModemManager's ``MCC,MNC,LAC,CI,TAC`` location string.

    MCC and MNC are decimal strings.  LAC, CI and TAC are hexadecimal.  MNC
    stays a string so values such as ``026`` retain their required width.
    """
    fields = str(value or "").strip().split(",")
    if len(fields) != 5:
        return None
    mcc, mnc = fields[0].strip(), fields[1].strip()
    if not (mcc.isdigit() and len(mcc) == 3 and mnc.isdigit()
            and len(mnc) in (2, 3)):
        return None
    technology = technology_from_access(int(access_technologies or 0))
    if technology is None:
        return None
    lac, cell_id, tac = map(_hex_field, fields[2:])
    if cell_id is None or cell_id <= 0:
        return None
    area = tac if technology in ("LTE", "NR") else lac
    limits = {"GSM": 2**16, "WCDMA": 2**28, "LTE": 2**28, "NR": 2**36}
    if area is None or area <= 0 or area >= 2**24 or cell_id >= limits[technology]:
        return None
    return CellIdentity(mcc + mnc, technology, lac, tac, cell_id)


def split_nmea(value: object) -> tuple[str, ...]:
    """Normalize the cached GPS-NMEA location value."""
    lines = []
    for line in str(value or "").replace("\r", "\n").split("\n"):
        sentence = line.strip()
        if sentence.startswith("$") and len(sentence) <= 256:
            lines.append(sentence)
    return tuple(lines)


def legacy_modem_service_reason(
        unit_path: Path = Path("/etc/systemd/system/uconsole-sim.service"),
        script_path: Path = Path("/usr/local/bin/setup-sim-gps")) -> str:
    """Detect the old service that races ModemManager for an AT port."""
    try:
        unit = unit_path.read_text(errors="replace")
    except OSError:
        return ""
    direct = "AT+CGPS" in unit.upper() or "/DEV/TTYUSB" in unit.upper()
    references_script = "setup-sim-gps" in unit
    script = ""
    if references_script:
        try:
            script = script_path.read_text(errors="replace")
        except OSError:
            pass
    direct = direct or "AT+CGPS" in script.upper() or "/DEV/TTYUSB3" in script.upper()
    if not direct:
        return ""
    return ("legacy uconsole-sim service sends AT/GNSS commands outside "
            "ModemManager; run scripts/migrate_uconsole_sim_service.sh --apply")


def _managed_objects(bus):
    import dbus
    root = bus.get_object(MM_SERVICE, MM_ROOT)
    return dbus.Interface(root, OBJECT_MANAGER).GetManagedObjects(timeout=3)


def managed_port_names(bus=None) -> Optional[set[str]]:
    """Return all device-node names managed by ModemManager.

    ``None`` means the inventory could not be obtained.  Callers should fail
    closed for generic ttyUSB probing in that case.
    """
    try:
        if bus is None:
            import dbus
            bus = dbus.SystemBus()
        objects = _managed_objects(bus)
        names: set[str] = set()
        for interfaces in objects.values():
            modem = interfaces.get(MODEM)
            if not modem:
                continue
            names.update(str(name) for name, _kind in modem.get("Ports", ()))
            primary = str(modem.get("PrimaryPort", ""))
            if primary:
                names.add(primary)
        return names
    except Exception:
        return None


def _valid_dbm(value: object) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and -200 <= number <= 0 else None


def _signal_dbm(signal_props: dict, technology: Optional[str]) -> Optional[float]:
    names = {"LTE": ("Lte", ("rsrp", "rssi")),
             "NR": ("Nr5g", ("rsrp", "ss-rsrp", "rssi")),
             "WCDMA": ("Umts", ("rscp", "rssi")),
             "GSM": ("Gsm", ("rssi",))}
    interface, keys = names.get(technology, ("", ()))
    values = dict(signal_props.get(interface, {}) or {})
    normalized = {str(key).lower(): value for key, value in values.items()}
    for key in keys:
        value = _valid_dbm(normalized.get(key))
        if value is not None:
            return value
    return None


class ModemLocationBroker:
    """Share cached ModemManager GNSS and serving-cell state safely."""

    def __init__(self, *, bus_factory: Optional[Callable] = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 poll_interval: float = 1.0,
                 legacy_check: Callable[[], str] = legacy_modem_service_reason):
        self._bus_factory = bus_factory
        self._clock = clock
        self._wall = wall_clock
        self._poll_interval = poll_interval
        self._legacy_check = legacy_check
        self._lock = threading.Lock()
        self._requests: dict[str, int] = {}
        self._snapshot: Optional[ModemLocationSnapshot] = None
        self._nmea = deque(maxlen=512)
        self._nmea_seen = deque(maxlen=128)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._ready = threading.Event()
        self.state = "idle"
        self.error = ""
        self.modem_generation = 0
        self.capabilities = 0
        self.enabled_sources = 0
        self.added_sources = 0
        self.model = ""
        self.revision = ""
        self.modem_path = ""
        self.qmi_device: Optional[str] = None

    def acquire(self, owner: str, *, gps: bool = False, cell: bool = False) -> bool:
        reason = self._legacy_check()
        if reason:
            self.state, self.error = "failed", reason
            self._ready.set()
            return False
        sources = (LOCATION_GPS_NMEA if gps else 0) | (LOCATION_3GPP_LAC_CI if cell else 0)
        if not sources:
            return True
        with self._lock:
            self._requests[owner] = sources
            if not self._thread or not self._thread.is_alive():
                self._stop = threading.Event()
                self._wake = threading.Event()
                self._ready = threading.Event()
                self.state, self.error = "starting", ""
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name="wdg-modem-location")
                self._thread.start()
            else:
                self._wake.set()
        return True

    def release(self, owner: str) -> None:
        with self._lock:
            self._requests.pop(owner, None)
        self._wake.set()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        self._ready.wait(timeout)
        return self.state == "running"

    def snapshot(self) -> Optional[ModemLocationSnapshot]:
        with self._lock:
            return self._snapshot

    def drain_nmea(self) -> list[str]:
        with self._lock:
            result = list(self._nmea)
            self._nmea.clear()
        return result

    def close(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)
        if thread and thread.is_alive():
            self.state = "degraded"
            self.error = "ModemManager location worker did not stop cleanly"
        else:
            self.state = "idle"
            self._thread = None
            self.added_sources = 0
            with self._lock:
                self._requests.clear()

    stop = close

    def _desired_sources(self) -> int:
        with self._lock:
            desired = 0
            for sources in self._requests.values():
                desired |= sources
            return desired

    def _select_modem(self, objects):
        candidates = []
        for path, interfaces in objects.items():
            modem = interfaces.get(MODEM)
            if not modem or LOCATION not in interfaces:
                continue
            text = (str(modem.get("Manufacturer", "")) + " "
                    + str(modem.get("Model", ""))).upper()
            preferred = int("SIM7600" in text or "SIMCOM" in text)
            candidates.append((preferred, str(path), modem, interfaces[LOCATION]))
        if not candidates:
            raise RuntimeError("No ModemManager modem with location support")
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return candidates[0][1:]

    def _connect(self):
        import dbus
        bus = self._bus_factory() if self._bus_factory else dbus.SystemBus()
        path, modem, location_props = self._select_modem(_managed_objects(bus))
        obj = bus.get_object(MM_SERVICE, path)
        loc = dbus.Interface(obj, LOCATION)
        props = dbus.Interface(obj, PROPERTIES)
        capabilities = int(location_props.get("Capabilities", 0))
        enabled = int(location_props.get("Enabled", 0))
        ports = [(str(name), int(kind)) for name, kind in modem.get("Ports", ())]
        qmi = next(("/dev/" + name for name, kind in ports if kind == PORT_QMI), None)
        return bus, path, modem, loc, props, capabilities, enabled, qmi

    def _restore_sources(self, props, loc) -> None:
        if not self.added_sources:
            return
        try:
            current = int(props.Get(LOCATION, "Enabled", timeout=3))
            target = current & ~self.added_sources
            if target != current:
                loc.Setup(target, False, timeout=3)
        except Exception:
            # Shutdown is best-effort; ModemManager owns the persistent state.
            pass

    def _run(self) -> None:
        bus = path = modem = loc = props = None
        backoffs = (1, 2, 5, 10, 30)
        failures = 0
        props_next = 0.0
        access = 0
        quality = None
        signal_props = {}
        try:
            while not self._stop.is_set():
                try:
                    if loc is None:
                        (bus, path, modem, loc, props, capabilities,
                         enabled, qmi) = self._connect()
                        self.modem_generation += 1
                        self.capabilities = capabilities
                        self.enabled_sources = enabled
                        self.modem_path = path
                        self.model = str(modem.get("Model", ""))
                        self.revision = str(modem.get("Revision", ""))
                        self.qmi_device = qmi
                    desired = self._desired_sources()
                    if desired & ~self.capabilities:
                        missing = desired & ~self.capabilities
                        raise RuntimeError(f"ModemManager location sources unsupported: 0x{missing:x}")
                    current = int(props.Get(LOCATION, "Enabled", timeout=3))
                    # Remove only source bits this broker previously added and
                    # whose last owner has released them. Bits that were already
                    # enabled when WDG connected always remain untouched.
                    newly_added = desired & ~current
                    no_longer_needed = self.added_sources & ~desired
                    target = (current | desired) & ~no_longer_needed
                    if target != current:
                        loc.Setup(target, False, timeout=3)
                        current = target
                    self.added_sources = (
                        (self.added_sources | newly_added) & current)
                    self.enabled_sources = current

                    now = self._clock()
                    if now >= props_next:
                        modem_props = dict(props.GetAll(MODEM, timeout=3))
                        access = int(modem_props.get("AccessTechnologies", 0))
                        raw_quality = modem_props.get("SignalQuality", ())
                        try:
                            quality = int(raw_quality[0])
                        except (IndexError, TypeError, ValueError):
                            quality = None
                        try:
                            signal_props = dict(props.GetAll(SIGNAL, timeout=3))
                        except Exception:
                            signal_props = {}
                        props_next = now + 5

                    locations = dict(loc.GetLocation(timeout=3))
                    cell = parse_3gpp_location(locations.get(LOCATION_3GPP_LAC_CI), access)
                    nmea = split_nmea(locations.get(LOCATION_GPS_NMEA))
                    technology = cell.technology if cell else technology_from_access(access)
                    snapshot = ModemLocationSnapshot(
                        observed_monotonic=now, observed_utc=self._wall(),
                        modem_generation=self.modem_generation,
                        operator_id=cell.operator_id if cell else None,
                        technology=technology,
                        lac=cell.lac if cell else None,
                        tac=cell.tac if cell else None,
                        cell_id=cell.cell_id if cell else None,
                        nmea=nmea, signal_dbm=_signal_dbm(signal_props, technology),
                        signal_quality_percent=quality, modem_path=path,
                        model=self.model, revision=self.revision, qmi_device=self.qmi_device)
                    with self._lock:
                        self._snapshot = snapshot
                        for sentence in nmea:
                            if sentence not in self._nmea_seen:
                                self._nmea.append(sentence)
                                self._nmea_seen.append(sentence)
                    self.state, self.error = "running", ""
                    self._ready.set()
                    failures = 0
                    self._wake.wait(self._poll_interval)
                    self._wake.clear()
                except Exception as exc:
                    self.state = "degraded"
                    self.error = str(exc) or type(exc).__name__
                    self._ready.set()
                    failures = min(failures + 1, len(backoffs))
                    loc = props = None
                    delay = backoffs[failures - 1]
                    self._wake.wait(delay)
                    self._wake.clear()
        finally:
            if self.added_sources:
                if loc is None or props is None:
                    try:
                        (_bus, _path, _modem, loc, props, _caps,
                         _enabled, _qmi) = self._connect()
                    except Exception:
                        loc = props = None
                if loc is not None and props is not None:
                    self._restore_sources(props, loc)
