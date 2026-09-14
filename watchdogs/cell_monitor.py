"""Host cellular measurements for All Wardrive.

The preferred path mirrors Android's CellInfo API through ModemManager's
GetCellInfo method.  SIM7600 AT+CPSI is a serving-cell-only fallback.
"""
from dataclasses import asdict, dataclass
from pathlib import Path
from queue import Empty, Full, Queue
import re
import subprocess
import threading
import time


CELL_TYPES = {1: "CDMA", 2: "GSM", 3: "UMTS", 4: "TDSCDMA", 5: "LTE", 6: "5GNR"}
TYPE_NAMES = {"UMTS": "WCDMA", "5GNR": "NR", "TDSCDMA": "WCDMA"}


def _integer(value, hexadecimal=False):
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    try:
        return int(text, 16 if hexadecimal or text.lower().startswith("0x") else 10)
    except (TypeError, ValueError):
        return None


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tenths_db(value):
    number = _number(value)
    return number / 10 if number is not None and abs(number) >= 200 else number


@dataclass(frozen=True)
class CellObservation:
    technology: str
    mcc: str
    mnc: str
    area: int
    cell_id: int
    channel: int | None
    signal_dbm: float | None
    serving: bool
    provider: str
    pci: int | None = None
    rsrp: float | None = None
    rsrq: float | None = None
    sinr: float | None = None
    band: str = ""
    frequency: int | None = None

    @property
    def operator_id(self):
        return self.mcc + self.mnc

    @property
    def identity(self):
        return f"{self.operator_id}_{self.area}_{self.cell_id}"

    def record(self):
        value = asdict(self)
        value["identity"] = self.identity
        value["operator_id"] = self.operator_id
        return value


def _operator_parts(value):
    text = str(value or "").replace("-", "").strip()
    if (not text.isdigit() or len(text) not in (5, 6)
            or int(text[:3]) <= 0 or int(text[3:]) <= 0):
        return None
    return text[:3], text[3:]


def _valid_identity(technology, area, cell_id):
    limits = {"GSM":2**16, "WCDMA":2**28, "LTE":2**28, "NR":2**36}
    return (area is not None and cell_id is not None
            and 0 < area < 2**24 and 0 < cell_id < limits[technology])


def modemmanager_cell(data):
    """Normalize one ModemManager GetCellInfo dictionary."""
    plain = {str(k).replace("_", "-"): v for k, v in dict(data).items()}
    raw_type = plain.get("cell-type")
    try:
        technology = CELL_TYPES.get(int(raw_type), str(raw_type or "").upper())
    except (TypeError, ValueError):
        technology = str(raw_type or "").upper()
    technology = TYPE_NAMES.get(technology, technology)
    if technology not in ("GSM", "WCDMA", "LTE", "NR"):
        return None
    operator = _operator_parts(plain.get("operator-id"))
    if not operator:
        return None
    area_key = "tac" if technology in ("LTE", "NR") else "lac"
    area = _integer(plain.get(area_key), hexadecimal=True)
    cell_id = _integer(plain.get("ci") or plain.get("nci"), hexadecimal=True)
    if not _valid_identity(technology, area, cell_id):
        return None
    channel_key = {"GSM":"arfcn", "WCDMA":"uarfcn", "LTE":"earfcn", "NR":"nrarfcn"}[technology]
    signal_keys = {"GSM":("rx-level",), "WCDMA":("rscp",),
                   "LTE":("rsrp", "rssi"), "NR":("rsrp", "ss-rsrp")}[technology]
    signal = next((_number(plain.get(key)) for key in signal_keys if _number(plain.get(key)) is not None), None)
    return CellObservation(technology, operator[0], operator[1], area, cell_id,
        _integer(plain.get(channel_key)), signal, bool(plain.get("serving")), "modemmanager",
        pci=_integer(plain.get("physical-ci") or plain.get("pci") or plain.get("psc")),
        rsrp=_number(plain.get("rsrp") or plain.get("ss-rsrp")),
        rsrq=_number(plain.get("rsrq") or plain.get("ss-rsrq")),
        sinr=_number(plain.get("snr") or plain.get("sinr") or plain.get("ss-sinr")),
        band=str(plain.get("band") or ""), frequency=_integer(plain.get("frequency")))


def parse_cpsi(text):
    """Parse a documented SIM7500/SIM7600 AT+CPSI response."""
    match = re.search(r"\+CPSI:\s*([^\r\n]+)", text, re.I)
    if not match:
        return None
    fields = [field.strip() for field in match.group(1).split(",")]
    technology = TYPE_NAMES.get(fields[0].upper(), fields[0].upper())
    if technology in ("NO SERVICE", "NO SERVICE ONLINE") or len(fields) < 6:
        return None
    operator = _operator_parts(fields[2])
    if not operator or technology not in ("GSM", "WCDMA", "LTE"):
        return None
    area = _integer(fields[3], hexadecimal=True)
    cell_id = _integer(fields[4], hexadecimal=(fields[4].lower().startswith("0x")
                                               or bool(re.search(r"[a-f]", fields[4], re.I))))
    if not _valid_identity(technology, area, cell_id):
        return None
    if technology == "LTE":
        # LTE,Online,MCC-MNC,TAC,SCellID,PCellID,Band,EARFCN,DLBW,ULBW,RSRQ,RSRP,RSSI,RSSNR
        if len(fields) < 14:
            return None
        return CellObservation(technology, operator[0], operator[1], area, cell_id,
            _integer(fields[7]), _tenths_db(fields[11]), True, "sim7600_at",
            pci=_integer(fields[5]), rsrq=_tenths_db(fields[10]), rsrp=_tenths_db(fields[11]),
            sinr=_tenths_db(fields[13]), band=fields[6])
    if technology == "WCDMA":
        # WCDMA,Online,MCC-MNC,LAC,CellID,Band,PSC,UARFCN,SSC,EcIo,RSCP,...
        if len(fields) < 11:
            return None
        return CellObservation(technology, operator[0], operator[1], area, cell_id,
            _integer(fields[7]), _number(fields[10]), True, "sim7600_at",
            pci=_integer(fields[6]), rsrp=_number(fields[10]), rsrq=_number(fields[9]), band=fields[5])
    # GSM,Online,MCC-MNC,LAC,CellID,ARFCN/Band,RxLev,...
    channel = None
    channel_match = re.search(r"\d+", fields[5]) if len(fields) > 5 else None
    if channel_match:
        channel = int(channel_match.group())
    signal = _number(fields[6]) if len(fields) > 6 else None
    return CellObservation(technology, operator[0], operator[1], area, cell_id,
        channel, signal, True, "sim7600_at")


class ModemManagerProvider:
    """CellInfo provider using the system D-Bus API introduced in MM 1.20."""
    name = "modemmanager"

    def __init__(self, bus=None):
        if bus is None:
            import dbus
            bus = dbus.SystemBus()
        self.bus = bus
        root = bus.get_object("org.freedesktop.ModemManager1", "/org/freedesktop/ModemManager1")
        import dbus
        objects = dbus.Interface(root, "org.freedesktop.DBus.ObjectManager").GetManagedObjects()
        interface = "org.freedesktop.ModemManager1.Modem"
        path = next((path for path, props in objects.items() if interface in props), None)
        if path is None:
            raise RuntimeError("No ModemManager modem")
        obj = bus.get_object("org.freedesktop.ModemManager1", path)
        self.modem = dbus.Interface(obj, interface)

    def scan(self):
        result = []
        for item in self.modem.GetCellInfo(timeout=8):
            try:
                cell = modemmanager_cell(item)
                if cell:
                    result.append(cell)
            except (TypeError, ValueError, OverflowError):
                continue
        if not result:
            raise RuntimeError("No identified cells returned")
        return result

    def close(self):
        pass


class Sim7600AtProvider:
    """Serving-cell fallback using a caller-supplied secondary AT port."""
    name = "sim7600_at"

    def __init__(self, port):
        import serial
        self.serial = serial.Serial(port, 115200, timeout=2, write_timeout=2, exclusive=True)

    def scan(self):
        self.serial.reset_input_buffer()
        self.serial.write(b"AT+CPSI?\r")
        self.serial.flush()
        deadline = time.monotonic() + 3
        chunks = []
        while time.monotonic() < deadline:
            line = self.serial.readline().decode("ascii", "replace")
            if line:
                chunks.append(line)
                if line.strip() in ("OK", "ERROR"):
                    break
        cell = parse_cpsi("".join(chunks))
        if not cell:
            raise RuntimeError("SIM7600 returned no identified serving cell")
        return [cell]

    def close(self):
        self.serial.close()


def _udev_properties(port):
    """Read the port-role tags installed for ModemManager."""
    try:
        result = subprocess.run(
            ["udevadm", "info", "--query=property", "--name", str(port)],
            capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode:
        return {}
    return dict(line.split("=", 1) for line in result.stdout.splitlines()
                if "=" in line)


def discover_udev_secondary_at(modem_device, *, port_names=None,
                               property_reader=None, device_root=Path("/dev")):
    """Find a udev-tagged secondary AT port on the same physical modem.

    QMI-controlled SIM7600s may omit their AT ports from Modem.Ports even
    though ModemManager's udev rules identify those interfaces precisely.
    """
    property_reader = property_reader or _udev_properties
    if port_names is None:
        port_names = sorted(path.name for path in Path("/sys/class/tty").glob("ttyUSB*"))
    modem_path = Path(str(modem_device))
    candidates = []
    for name in port_names:
        port = Path(device_root) / str(name)
        if not port.exists():
            continue
        props = property_reader(port)
        if props.get("ID_MM_PORT_TYPE_AT_SECONDARY") != "1":
            continue
        devpath = props.get("DEVPATH", "")
        try:
            Path("/sys" + devpath).relative_to(modem_path)
        except (TypeError, ValueError):
            continue
        candidates.append(str(port))
    if candidates:
        return sorted(candidates)[0]
    raise RuntimeError("No udev-tagged SIM7600 secondary AT port")


def discover_secondary_at(bus=None):
    """Return a secondary AT port from ModemManager or its udev tags."""
    if bus is None:
        import dbus
        bus = dbus.SystemBus()
    import dbus
    root = bus.get_object("org.freedesktop.ModemManager1", "/org/freedesktop/ModemManager1")
    objects = dbus.Interface(root, "org.freedesktop.DBus.ObjectManager").GetManagedObjects()
    interface = "org.freedesktop.ModemManager1.Modem"
    for props in objects.values():
        modem = props.get(interface)
        if not modem:
            continue
        model = (str(modem.get("Model", "")) + " " + str(modem.get("Manufacturer", ""))).upper()
        if "SIM7600" not in model and "SIMCOM" not in model:
            continue
        primary = str(modem.get("PrimaryPort", ""))
        # MMModemPortType AT is enum value 2. Avoid the primary bearer/control port.
        candidates = [str(name) for name, kind in modem.get("Ports", ())
                      if int(kind) == 2 and str(name) != primary]
        if candidates:
            return "/dev/" + sorted(candidates)[0]
        device = str(modem.get("Device", ""))
        if device:
            return discover_udev_secondary_at(device)
    raise RuntimeError("No free SIM7600 secondary AT port")


class AutoCellProvider:
    """Prefer WiGLE-like CellInfo; degrade to documented serving-cell AT."""
    name = "auto"

    def __init__(self):
        self.active = ModemManagerProvider()
        self.name = self.active.name
        self.fallback_used = False

    def scan(self):
        try:
            return self.active.scan()
        except Exception as first:
            if self.fallback_used:
                raise
            self.active.close()
            try:
                self.active = Sim7600AtProvider(discover_secondary_at())
                self.name = self.active.name
                self.fallback_used = True
                return self.active.scan()
            except Exception as second:
                raise RuntimeError(f"CellInfo unavailable ({first}); AT fallback unavailable ({second})") from second

    def close(self):
        self.active.close()


class HostCellScanner:
    """Background scanner with a bounded queue consumed by the Pyxel loop."""
    def __init__(self, provider_factory=None, interval=10):
        self.provider_factory = provider_factory or AutoCellProvider
        self.interval = interval
        self._thread = None
        self._stop = threading.Event()
        self._events = Queue(maxsize=256)
        self.session = ""
        self.state = "idle"
        self.provider = ""
        self.error = ""
        self.drops = 0

    def start(self, session):
        if self._thread and self._thread.is_alive():
            return self.session == session
        self.session = session
        self.state = "starting"
        self.error = ""
        self.drops = 0
        self._events = Queue(maxsize=256)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(session,), daemon=True,
                                        name="wardrive-cell")
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        self.state = "idle"

    def _put(self, value):
        try:
            self._events.put_nowait(value)
        except Full:
            self.drops += 1

    def _run(self, session):
        provider = None
        try:
            provider = self.provider_factory()
            self.provider = getattr(provider, "name", type(provider).__name__)
            self._put((session, "started", self.provider))
            while not self._stop.is_set():
                measured = time.time()
                cells = provider.scan()
                self.provider = getattr(provider, "name", self.provider)
                self._put((session, "cells", (measured, cells)))
                if self._stop.wait(self.interval):
                    break
        except Exception as exc:
            if not self._stop.is_set():
                self._put((session, "error", str(exc) or type(exc).__name__))
        finally:
            if provider:
                try:
                    provider.close()
                except Exception:
                    pass

    def poll(self):
        events = []
        for _ in range(64):
            try:
                events.append(self._events.get_nowait())
            except Empty:
                break
        return events
