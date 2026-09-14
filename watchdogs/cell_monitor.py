"""Host cellular measurements for All Wardrive.

ModemManager is preferred when its backend implements ``GetCellInfo``. Older
QMI-backed releases expose the D-Bus method but return ``Core.Unsupported``;
those systems use qmicli through qmi-proxy. WDG never opens a modem AT serial
port, because opening one can change DTR/RTS and compete with the data session.
"""
from dataclasses import asdict, dataclass
from queue import Empty, Full, Queue
import re
import shutil
import subprocess
import threading
import time


CELL_TYPES = {1: "CDMA", 2: "GSM", 3: "UMTS", 4: "TDSCDMA", 5: "LTE", 6: "5GNR"}
TYPE_NAMES = {"UMTS": "WCDMA", "5GNR": "NR", "TDSCDMA": "WCDMA"}
QMI_PORT_TYPE = 6


class CellProviderError(RuntimeError):
    """A cellular-provider failure with an explicit retry policy."""

    def __init__(self, message, *, retryable=True):
        super().__init__(message)
        self.retryable = retryable


class PermanentCellError(CellProviderError):
    def __init__(self, message):
        super().__init__(message, retryable=False)


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
    limits = {"GSM": 2**16, "WCDMA": 2**28, "LTE": 2**28, "NR": 2**36}
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
    channel_key = {"GSM": "arfcn", "WCDMA": "uarfcn", "LTE": "earfcn", "NR": "nrarfcn"}[technology]
    signal_keys = {"GSM": ("rx-level",), "WCDMA": ("rscp",),
                   "LTE": ("rsrp", "rssi"), "NR": ("rsrp", "ss-rsrp")}[technology]
    signal = next((_number(plain.get(key)) for key in signal_keys
                   if _number(plain.get(key)) is not None), None)
    return CellObservation(
        technology, operator[0], operator[1], area, cell_id,
        _integer(plain.get(channel_key)), signal, bool(plain.get("serving")),
        "modemmanager",
        pci=_integer(plain.get("physical-ci") or plain.get("pci") or plain.get("psc")),
        rsrp=_number(plain.get("rsrp") or plain.get("ss-rsrp")),
        rsrq=_number(plain.get("rsrq") or plain.get("ss-rsrq")),
        sinr=_number(plain.get("snr") or plain.get("sinr") or plain.get("ss-sinr")),
        band=str(plain.get("band") or ""),
        frequency=_integer(plain.get("frequency")))


def _section(text, heading):
    """Return one qmicli section, stopping at the next unindented heading."""
    match = re.search(rf"(?m)^{re.escape(heading)}\r?$", text)
    if not match:
        return ""
    tail = text[match.end():].lstrip("\r\n")
    lines = []
    for line in tail.splitlines():
        if line and not line[0].isspace():
            break
        lines.append(line)
    return "\n".join(lines)


def _qmi_value(text, label):
    match = re.search(rf"(?m)^\s+{re.escape(label)}:\s*'([^']+)'", text)
    return match.group(1).strip() if match else None


def _qmi_number(text, label):
    value = _qmi_value(text, label)
    match = re.search(r"-?\d+(?:\.\d+)?", value or "")
    return _number(match.group()) if match else None


def _qmi_blocks(section, prefix="Cell"):
    """Return child blocks while preserving qmicli's indentation hierarchy."""
    lines = section.expandtabs(4).splitlines()
    blocks = []
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not re.fullmatch(rf"{re.escape(prefix)} \[\d+\]:", stripped):
            continue
        indent = len(line) - len(stripped)
        body = []
        for child in lines[index + 1:]:
            child_stripped = child.lstrip()
            if child_stripped and len(child) - len(child_stripped) <= indent:
                break
            body.append(child)
        blocks.append("\n".join(body))
    return blocks


def _qmi_band(raw):
    match = re.search(r"\(([^)]+)\)", raw or "")
    return match.group(1) if match else ""


def _qmi_rx_level(raw):
    """Convert the GSM 0..63 RX level reported by qmicli to approximate dBm."""
    match = re.search(r"\('(\d+)'\)", raw or "")
    if not match:
        return None
    level = int(match.group(1))
    if level == 0:
        return -110.0
    if level == 63:
        return -48.0
    if 0 < level < 63:
        return float(level - 111)
    return None


def parse_qmicli_cell_location(text):
    """Parse valid cells from qmicli's NAS cell-location response.

    LTE/UMTS neighbor entries lack a globally unique cell ID in this response,
    so they are excluded instead of inventing WiGLE identities. GERAN neighbor
    entries include PLMN/LAC/CID and can be retained.
    """
    cells = []
    lte = _section(text, "Intrafrequency LTE Info")
    if lte:
        operator = _operator_parts(_qmi_value(lte, "PLMN"))
        area = _integer(_qmi_value(lte, "Tracking Area Code"))
        cell_id = _integer(_qmi_value(lte, "Global Cell ID"))
        channel_raw = _qmi_value(lte, "EUTRA Absolute RF Channel Number")
        serving_pci = _integer(_qmi_value(lte, "Serving Cell ID"))
        serving_block = next(
            (block for block in _qmi_blocks(lte)
             if _integer(_qmi_value(block, "Physical Cell ID")) == serving_pci), "")
        rsrp = _qmi_number(serving_block, "RSRP")
        rsrq = _qmi_number(serving_block, "RSRQ")
        rssi = _qmi_number(serving_block, "RSSI")
        signal = rsrp if rsrp is not None else rssi
        if operator and _valid_identity("LTE", area, cell_id) and signal is not None:
            cells.append(CellObservation(
                "LTE", operator[0], operator[1], area, cell_id,
                _integer(channel_raw), signal, True, "qmi_proxy",
                pci=serving_pci, rsrp=rsrp, rsrq=rsrq,
                band=_qmi_band(channel_raw)))

    nr = _section(text, "5GNR cell information")
    if nr:
        operator = _operator_parts(_qmi_value(nr, "PLMN"))
        area = _integer(_qmi_value(nr, "Tracking Area Code"))
        cell_id = _integer(_qmi_value(nr, "Global Cell ID"))
        rsrp = _qmi_number(nr, "RSRP")
        if operator and _valid_identity("NR", area, cell_id) and rsrp is not None:
            cells.append(CellObservation(
                "NR", operator[0], operator[1], area, cell_id, None, rsrp, True,
                "qmi_proxy", pci=_integer(_qmi_value(nr, "Physical Cell ID")),
                rsrp=rsrp, rsrq=_qmi_number(nr, "RSRQ"),
                sinr=_qmi_number(nr, "SNR")))

    umts = _section(text, "UMTS Info")
    if umts:
        operator = _operator_parts(_qmi_value(umts, "PLMN"))
        area = _integer(_qmi_value(umts, "Location Area Code"))
        cell_id = _integer(_qmi_value(umts, "Cell ID"))
        signal = _qmi_number(umts, "RSCP")
        if operator and _valid_identity("WCDMA", area, cell_id) and signal is not None:
            cells.append(CellObservation(
                "WCDMA", operator[0], operator[1], area, cell_id,
                _integer(_qmi_value(umts, "UTRA Absolute RF Channel Number")),
                signal, True, "qmi_proxy",
                pci=_integer(_qmi_value(umts, "Primary Scrambling Code")),
                rsrp=signal, rsrq=_qmi_number(umts, "ECIO")))

    geran = _section(text, "GERAN Info")
    if geran:
        entries = [(geran, True)] + [(block, False) for block in _qmi_blocks(geran)]
        for entry, serving in entries:
            operator = _operator_parts(_qmi_value(entry, "PLMN"))
            area = _integer(_qmi_value(entry, "Location Area Code"))
            cell_id = _integer(_qmi_value(entry, "Cell ID"))
            rx_line = next((line for line in entry.splitlines() if "RX Level:" in line), "")
            signal = _qmi_rx_level(rx_line)
            if operator and _valid_identity("GSM", area, cell_id) and signal is not None:
                cells.append(CellObservation(
                    "GSM", operator[0], operator[1], area, cell_id,
                    _integer(_qmi_value(entry, "GERAN Absolute RF Channel Number")),
                    signal, serving, "qmi_proxy",
                    pci=_integer(_qmi_value(entry, "Base Station Identity Code"))))
    return cells


def _managed_objects(bus=None):
    if bus is None:
        import dbus
        bus = dbus.SystemBus()
    import dbus
    root = bus.get_object(
        "org.freedesktop.ModemManager1", "/org/freedesktop/ModemManager1")
    objects = dbus.Interface(
        root, "org.freedesktop.DBus.ObjectManager").GetManagedObjects()
    return bus, objects


def discover_qmi_device(bus=None):
    """Return the QMI control device owned by a SIMCom modem."""
    _bus, objects = _managed_objects(bus)
    interface = "org.freedesktop.ModemManager1.Modem"
    for props in objects.values():
        modem = props.get(interface)
        if not modem:
            continue
        identity = (str(modem.get("Model", "")) + " "
                    + str(modem.get("Manufacturer", ""))).upper()
        if "SIM7600" not in identity and "SIMCOM" not in identity:
            continue
        ports = [str(name) for name, kind in modem.get("Ports", ())
                 if int(kind) == QMI_PORT_TYPE]
        if ports:
            return "/dev/" + sorted(ports)[0]
        primary = str(modem.get("PrimaryPort", ""))
        if primary.startswith("cdc-wdm"):
            return "/dev/" + primary
    raise PermanentCellError(
        "No SIM7600 QMI control device found; cellular disabled")


class ModemManagerProvider:
    """CellInfo provider using the system D-Bus API introduced in MM 1.20."""
    name = "modemmanager"

    def __init__(self, bus=None):
        bus, objects = _managed_objects(bus)
        self.bus = bus
        interface = "org.freedesktop.ModemManager1.Modem"
        path = next((path for path, props in objects.items() if interface in props), None)
        if path is None:
            raise PermanentCellError("No ModemManager modem; cellular disabled")
        import dbus
        obj = bus.get_object("org.freedesktop.ModemManager1", path)
        self.modem = dbus.Interface(obj, interface)

    def scan(self):
        result = []
        try:
            items = self.modem.GetCellInfo(timeout=8)
        except Exception as exc:
            raise CellProviderError(f"ModemManager CellInfo failed: {exc}") from exc
        for item in items:
            try:
                cell = modemmanager_cell(item)
                if cell:
                    result.append(cell)
            except (TypeError, ValueError, OverflowError):
                continue
        if not result:
            raise CellProviderError("ModemManager returned no identified cells")
        return result

    def close(self):
        pass


class QmiProxyProvider:
    """Read-only NAS cell-location query sharing ModemManager's QMI proxy."""
    name = "qmi_proxy"

    def __init__(self, device=None, *, runner=None, binary=None):
        self.device = device or discover_qmi_device()
        self.binary = binary or shutil.which("qmicli")
        self.runner = runner or subprocess.run
        if not self.binary:
            raise PermanentCellError(
                "qmicli is not installed (install libqmi-utils); cellular disabled")

    def scan(self):
        command = [self.binary, "--device=" + self.device, "--device-open-proxy",
                   "--nas-get-cell-location-info"]
        try:
            result = self.runner(command, capture_output=True, text=True,
                                 timeout=12, check=False)
        except subprocess.TimeoutExpired as exc:
            raise CellProviderError("QMI cell query timed out; will retry") from exc
        except OSError as exc:
            raise PermanentCellError(
                f"Cannot run qmicli: {exc}; cellular disabled") from exc
        if result.returncode:
            detail = " ".join(
                (result.stderr or result.stdout or "QMI query failed").split())[:240]
            permanent = any(marker in detail.lower() for marker in
                            ("not supported", "invalid option", "unknown option"))
            if permanent:
                raise PermanentCellError(detail + "; cellular disabled")
            raise CellProviderError(detail + "; will retry")
        cells = parse_qmicli_cell_location(result.stdout)
        if not cells:
            raise CellProviderError(
                "QMI returned no identified serving cell; will retry")
        return cells

    def close(self):
        pass


class AutoCellProvider:
    """Prefer ModemManager CellInfo, then use synchronized QMI access once."""
    name = "auto"

    def __init__(self, modemmanager_factory=None, qmi_factory=None):
        self.modemmanager_factory = modemmanager_factory or ModemManagerProvider
        self.qmi_factory = qmi_factory or QmiProxyProvider
        self.fallback_used = False
        try:
            self.active = self.modemmanager_factory()
        except Exception:
            self.active = self.qmi_factory()
            self.fallback_used = True
        self.name = self.active.name

    def scan(self):
        try:
            return self.active.scan()
        except Exception as first:
            if self.fallback_used:
                raise
            self.active.close()
            self.fallback_used = True
            try:
                self.active = self.qmi_factory()
                self.name = self.active.name
                return self.active.scan()
            except PermanentCellError:
                raise
            except Exception as second:
                raise CellProviderError(
                    f"CellInfo unavailable ({first}); QMI proxy unavailable ({second})",
                    retryable=getattr(second, "retryable", True)) from second

    def close(self):
        self.active.close()


class HostCellScanner:
    """Background scanner with a bounded queue consumed by the Pyxel loop."""

    def __init__(self, provider_factory=None, interval=30):
        self.provider_factory = provider_factory or AutoCellProvider
        self.interval = interval
        self._thread = None
        self._stop = threading.Event()
        self._events = Queue(maxsize=256)
        self.session = ""
        self.state = "idle"
        self.provider = ""
        self.error = ""
        self.retryable = True
        self.drops = 0

    def start(self, session):
        if self._thread and self._thread.is_alive():
            return self.session == session
        self.session = session
        self.state = "starting"
        self.error = ""
        self.retryable = True
        self.drops = 0
        self._events = Queue(maxsize=256)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(session,), daemon=True, name="wardrive-cell")
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
                self._put((session, "error", {
                    "message": str(exc) or type(exc).__name__,
                    "retryable": getattr(exc, "retryable", True)}))
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
