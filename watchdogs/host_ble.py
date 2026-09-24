"""Background BlueZ BLE discovery for All Wardrive, with bounded UI handoff."""
import asyncio
from collections import OrderedDict
from pathlib import Path
from queue import Empty, Full, Queue
import threading
import time
from uuid import UUID


def resolve_ble_adapter(selection="auto", sysfs=Path("/sys/class/bluetooth")):
    """Resolve a persisted controller MAC to the current BlueZ hci name."""
    value = str(selection or "auto").strip()
    if value.lower() == "auto":
        return None
    wanted = value.upper()
    for address_file in sorted(Path(sysfs).glob("hci*/address")):
        try:
            if address_file.read_text(encoding="ascii").strip().upper() == wanted:
                return address_file.parent.name
        except OSError:
            continue
    raise RuntimeError(f"Bluetooth adapter {wanted} is not available")


def advertisement_record(device, adv):
    """Normalize BlueZ's parsed fields for existing signature detection.

    These are reconstructed AD fields, not a raw over-the-air packet. BlueZ
    may combine advertising and scan-response data. Unknown address types
    stay unknown so random addresses cannot generate manufacturer OUI hits.
    """
    fields = bytearray()

    def add(kind, payload):
        if len(payload) <= 254:
            fields.extend(bytes((len(payload) + 1, kind)) + payload)

    def uuid_bytes(value):
        value = UUID(value)
        text = str(value)
        if text.endswith("-0000-1000-8000-00805f9b34fb"):
            number = int(text[:8], 16)
            size = 2 if number <= 0xffff else 4
            return number.to_bytes(size, "little")
        return value.bytes[::-1]

    if adv.local_name:
        add(9, adv.local_name.encode("utf-8")[:254])
    for company, payload in adv.manufacturer_data.items():
        add(255, company.to_bytes(2, "little") + bytes(payload))
    for service in adv.service_uuids:
        value = uuid_bytes(service)
        add({2:3, 4:5, 16:7}[len(value)], value)
    for service, payload in adv.service_data.items():
        value = uuid_bytes(service)
        add({2:0x16, 4:0x20, 16:0x21}[len(value)], value + bytes(payload))
    details = device.details if isinstance(device.details, dict) else {}
    props = details.get("props", {})
    address_type = {"public":0, "random":1}.get(props.get("AddressType"))
    return dict(kind="ble", mac=device.address.upper(), rssi=int(adv.rssi),
                name=adv.local_name or "", addr_type=address_type, event=0,
                data_hex=fields.hex(), age_ms=0, source="uconsole_ble",
                data_format="bluez_normalized_ad")


class HostBleScanner:
    def __init__(self, scanner_factory=None, *, adapter="auto",
                 adapter_resolver=resolve_ble_adapter,
                 lease_acquire=None, lease_release=None):
        self.scanner_factory = scanner_factory
        self.adapter = adapter
        self.adapter_resolver = adapter_resolver
        self.lease_acquire = lease_acquire
        self.lease_release = lease_release
        self._thread = None
        self._stop = threading.Event()
        self._records = Queue(maxsize=256)
        self._status = Queue()
        self.session = ""
        self.state = "idle"
        self.drops = 0

    def start(self, session, adapter=None):
        if self._thread and self._thread.is_alive():
            return False
        if adapter is not None:
            self.adapter = adapter
        self.session = session
        self._stop = threading.Event()
        self._records = Queue(maxsize=256)
        self._status = Queue()
        self.drops = 0
        self.state = "starting"
        self._thread = threading.Thread(target=self._run, args=(session,), daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        self.state = "idle"

    def _run(self, session):
        try:
            asyncio.run(self._scan(session))
        except Exception as exc:
            if not self._stop.is_set():
                self._status.put((session, "error", str(exc) or type(exc).__name__))

    async def _scan(self, session):
        lease_held = False
        scanner = None
        try:
            if self.lease_acquire is not None:
                result = self.lease_acquire(20)
                allowed, reason = (
                    (result, "Meshtastic phone has Bluetooth priority")
                    if isinstance(result, bool) else result)
                if not allowed:
                    self.state = "paused"
                    self._status.put((session, "paused", str(reason)))
                    return
                lease_held = True

            factory = self.scanner_factory
            if factory is None:
                from bleak import BleakScanner
                factory = BleakScanner
            seen = OrderedDict()

            def received(device, adv):
                if self._stop.is_set():
                    return
                try:
                    record = advertisement_record(device, adv)
                    now = time.monotonic()
                    key = (record["data_hex"], record["addr_type"])
                    old = seen.pop(record["mac"], None)
                    if old and key == old[0] and now-old[1] < 1:
                        seen[record["mac"]] = old
                        return
                    seen[record["mac"]] = (key, now)
                    if len(seen) > 512:
                        seen.popitem(last=False)
                    self._records.put_nowait((session, "ble", (now, record)))
                except (ValueError, TypeError, OverflowError, Full):
                    self.drops += 1

            kwargs = dict(detection_callback=received, scanning_mode="active")
            adapter = self.adapter_resolver(self.adapter)
            if adapter:
                kwargs["bluez"] = {"adapter": adapter}
            scanner = factory(**kwargs)
            await asyncio.wait_for(scanner.start(), timeout=10)
            if not self._stop.is_set():
                self.state = "running"
                self._status.put((session, "started", None))
            while not self._stop.is_set():
                await asyncio.sleep(0.1)
        finally:
            try:
                if scanner is not None:
                    await asyncio.wait_for(scanner.stop(), timeout=5)
            finally:
                if lease_held and self.lease_release is not None:
                    self.lease_release()

    def poll(self):
        events = []
        while True:
            try:
                events.append(self._status.get_nowait())
            except Empty:
                break
        for _ in range(64):
            try:
                events.append(self._records.get_nowait())
            except Empty:
                break
        return events
