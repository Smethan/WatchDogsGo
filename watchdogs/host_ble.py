"""Background BlueZ BLE discovery for All Wardrive, with bounded UI handoff."""
import asyncio
import threading
import time
from collections import OrderedDict
from pathlib import Path
from queue import Empty, Full, Queue
from uuid import UUID

BLE_SCAN_LEASE_SECONDS = 20
BLE_SCAN_LEASE_RENEW_SECONDS = 8
BLE_SCAN_LEASE_CALL_TIMEOUT = 4.5


def shared_adapter_conflict(message):
    """Identify BlueZ discovery failures that indicate controller contention."""
    value = str(message or "").lower()
    return any(marker in value for marker in (
        "org.bluez.error.inprogress",
        "org.bluez.error.busy",
        "operation already in progress",
        "discovery already in progress",
        "resource busy",
    ))


def list_ble_adapters(sysfs=Path("/sys/class/bluetooth")):
    """Return powered or present BlueZ controllers by stable MAC address.

    Linux's ``hciX`` numbering is assigned at discovery time and can change
    after a reboot or USB replug.  Settings therefore persist controller MACs
    and resolve them to the current ``hciX`` only when a scan starts.
    """
    adapters = []
    for address_file in sorted(Path(sysfs).glob("hci*/address")):
        try:
            address = address_file.read_text(encoding="ascii").strip().upper()
        except OSError:
            continue
        if not address:
            continue
        adapters.append((address, address_file.parent.name))
    return adapters


def resolve_ble_adapter(selection="auto", sysfs=Path("/sys/class/bluetooth")):
    """Resolve a persisted controller MAC to the current BlueZ hci name."""
    value = str(selection or "auto").strip()
    if value.lower() == "auto":
        return None
    wanted = value.upper()
    for address, name in list_ble_adapters(sysfs):
        if address == wanted:
            return name
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
                 lease_acquire=None, lease_release=None,
                 thread_factory=None):
        self.scanner_factory = scanner_factory
        self.adapter = adapter
        self.adapter_resolver = adapter_resolver
        self.lease_acquire = lease_acquire
        self.lease_release = lease_release
        self._thread_factory = thread_factory or threading.Thread
        self._thread = None
        self._state_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._lease_release_lock = threading.Lock()
        self._stop = threading.Event()
        self._records = Queue(maxsize=256)
        self._status = Queue()
        self.session = ""
        self.state = "idle"
        self.drops = 0
        self.require_lease = True
        self._lease_generation = 0
        self._lease_owner = ""
        self._pending_release_owner = ""
        self._pending_acquire_owner = ""

    def start(self, session, adapter=None, *, require_lease=True):
        with self._start_lock:
            with self._state_lock:
                if self._thread and self._thread.is_alive():
                    return False
                pending_acquire = self._pending_acquire_owner
                pending_release = self._pending_release_owner
            if pending_acquire:
                self._set_start_error(
                    session,
                    "A previous Meshtastic BLE lease request is still "
                    f"pending for {pending_acquire}; retry after it resolves")
                return False
            if (pending_release
                    and not self._release_lease(
                        pending_release, session=session, report=False)):
                self._set_start_error(
                    session,
                    "The previous Meshtastic BLE scan lease remains active "
                    f"for {pending_release}; could not start a new scan")
                return False
            with self._state_lock:
                # A late acquisition completion can publish unresolved state
                # while the compensating release above is in flight.
                if self._pending_acquire_owner or self._pending_release_owner:
                    pending = (self._pending_acquire_owner
                               or self._pending_release_owner)
                    self._set_start_error_locked(
                        session,
                        "A previous Meshtastic BLE lease remains unresolved "
                        f"for {pending}; could not start a new scan")
                    return False
                if adapter is not None:
                    self.adapter = adapter
                self.session = session
                self.require_lease = bool(require_lease)
                self._stop = threading.Event()
                self._records = Queue(maxsize=256)
                self._status = Queue()
                self.drops = 0
                self.state = "starting"
                self._lease_generation += 1
                self._lease_owner = f"host-ble-{self._lease_generation}"
                try:
                    thread = self._thread_factory(
                        target=self._run, args=(session,), daemon=True)
                    self._thread = thread
                    thread.start()
                except Exception as exc:
                    self._thread = None
                    self.state = "error"
                    self._status.put((
                        session, "error",
                        "Could not start Bluetooth scan worker: "
                        + (str(exc) or type(exc).__name__)))
                    return False
            return True

    def _set_start_error_locked(self, session, message):
        """Publish a terminal start error with ``_state_lock`` held."""
        self.session = session
        self._records = Queue(maxsize=256)
        self._status = Queue()
        self.state = "error"
        self._status.put((session, "error", str(message)))

    def _set_start_error(self, session, message):
        with self._state_lock:
            self._set_start_error_locked(session, message)

    def stop(self):
        self._stop.set()
        self.state = "idle"

    def close(self, timeout=7.0):
        """Stop discovery and wait for its final daemon-lease release."""
        self.stop()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        stopped = not thread or not thread.is_alive()
        if stopped:
            self._thread = None
        if not stopped:
            return False
        with self._state_lock:
            pending_acquire = self._pending_acquire_owner
            pending_release = self._pending_release_owner
            session = self.session
        if pending_acquire:
            self._report_release_error(
                session, pending_acquire,
                "the lease request has not completed")
            return False
        if pending_release and not self._release_lease(
                pending_release, session=session):
            return False
        return True

    @property
    def worker_active(self):
        return bool(self._thread and self._thread.is_alive())

    def _run(self, session):
        with self._state_lock:
            if not self.session:
                self.session = session
        try:
            asyncio.run(self._scan(session))
        except Exception as exc:
            if not self._stop.is_set():
                with self._state_lock:
                    if self.session == session:
                        self.state = "error"
                self._status.put((session, "error", str(exc) or type(exc).__name__))
        finally:
            with self._state_lock:
                if self.session == session:
                    if self.state == "error":
                        pass
                    elif self._stop.is_set():
                        self.state = "idle"
                    elif self.state in ("starting", "running"):
                        # Any unrequested worker exit is terminal.  Keeping a
                        # dead scan labelled RUNNING prevents the explicit UI
                        # retry path from being useful.
                        self.state = "error"

    def _report_release_error(self, session, owner, detail):
        message = (
            f"Meshtastic BLE scan lease for {owner} remains active: {detail}")
        with self._state_lock:
            if self.session == session:
                self.state = "error"
        self._status.put((session, "error", message))

    def _release_lease(self, owner, *, session, report=True):
        """Release one exact owner, retaining it until success is confirmed."""
        if self.lease_release is None:
            with self._state_lock:
                self._pending_release_owner = owner
            if report:
                self._report_release_error(
                    session, owner, "no release callback is available")
            return False
        detail = "the daemon did not confirm release"
        with self._lease_release_lock:
            try:
                released = self.lease_release(owner) is not False
            except Exception as exc:
                released = False
                detail = str(exc) or type(exc).__name__
            with self._state_lock:
                if released:
                    if self._pending_release_owner == owner:
                        self._pending_release_owner = ""
                else:
                    self._pending_release_owner = owner
        if not released and report:
            self._report_release_error(session, owner, detail)
        return released

    async def _acquire_lease(self, owner, session):
        """Keep BlueZ responsive while the bounded control request runs."""
        result_queue = Queue(maxsize=1)

        def call():
            try:
                result_queue.put((True, self.lease_acquire(
                    BLE_SCAN_LEASE_SECONDS, owner)))
            except Exception as exc:
                result_queue.put((False, exc))

        threading.Thread(
            target=call, name="wdg-host-ble-lease", daemon=True).start()
        deadline = time.monotonic() + BLE_SCAN_LEASE_CALL_TIMEOUT
        while time.monotonic() < deadline:
            try:
                succeeded, value = result_queue.get_nowait()
            except Empty:
                await asyncio.sleep(0.05)
                continue
            if not succeeded:
                raise value
            return value

        # The underlying manager request is bounded in production, but an
        # injected or wedged callback may outlive this wrapper. If it grants
        # late, issue a compensating release instead of leaving phone BLE
        # yielded until the daemon's expiry timer fires.
        with self._state_lock:
            self._pending_acquire_owner = owner

        def release_late_grant():
            succeeded, value = result_queue.get()
            allowed = (succeeded and (
                value if isinstance(value, bool) else bool(value[0])))
            if allowed:
                self._release_lease(owner, session=session)
            with self._state_lock:
                if self._pending_acquire_owner == owner:
                    self._pending_acquire_owner = ""

        threading.Thread(
            target=release_late_grant,
            name="wdg-host-ble-late-lease",
            daemon=True,
        ).start()
        return False, "Meshtastic BLE scan lease timed out"

    async def _scan(self, session):
        lease_held = False
        scanner = None
        pause_reason = ""
        with self._state_lock:
            pending_acquire = self._pending_acquire_owner
            pending_release = self._pending_release_owner
        if pending_acquire:
            self._report_release_error(
                session, pending_acquire,
                "a previous lease request has not completed")
            return
        if (pending_release
                and not self._release_lease(
                    pending_release, session=session, report=False)):
            self._report_release_error(
                session, pending_release,
                "the previous release retry failed")
            return
        with self._state_lock:
            if not self._lease_owner:
                self._lease_generation += 1
                self._lease_owner = f"host-ble-{self._lease_generation}"
            lease_owner = self._lease_owner
        try:
            if self.require_lease and self.lease_acquire is not None:
                result = await self._acquire_lease(lease_owner, session)
                allowed, reason = (
                    (result, "Meshtastic phone has Bluetooth priority")
                    if isinstance(result, bool) else result)
                if not allowed:
                    # The manager can time out after the acquire packet was
                    # sent and then fail to confirm its compensating release.
                    # Treat that as ownership until this wrapper retries the
                    # exact-token release; a plain policy denial remains free
                    # of unnecessary release traffic.
                    if "lease may still be active" in str(reason).lower():
                        lease_held = True
                    self.state = "paused"
                    self._status.put((session, "paused", str(reason)))
                    return
                lease_held = True
                if self._stop.is_set():
                    return

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
            if self._stop.is_set():
                return
            try:
                await asyncio.wait_for(scanner.start(), timeout=10)
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                kind = ("shared_error" if shared_adapter_conflict(message)
                        else "error")
                if self.session == session:
                    self.state = "error"
                self._status.put((session, kind, message))
                return
            if not self._stop.is_set():
                self.state = "running"
                self._status.put((session, "started", None))
            renew_at = (
                time.monotonic() + BLE_SCAN_LEASE_RENEW_SECONDS
                if lease_held else float("inf"))
            while not self._stop.is_set():
                await asyncio.sleep(0.1)
                if time.monotonic() < renew_at:
                    continue
                result = await self._acquire_lease(lease_owner, session)
                allowed, reason = (
                    (result, "Meshtastic phone has Bluetooth priority")
                    if isinstance(result, bool) else result)
                if not allowed:
                    pause_reason = str(
                        reason or "Meshtastic phone has Bluetooth priority")
                    break
                renew_at = time.monotonic() + BLE_SCAN_LEASE_RENEW_SECONDS
        finally:
            try:
                if scanner is not None:
                    await asyncio.wait_for(scanner.stop(), timeout=5)
            finally:
                if lease_held:
                    self._release_lease(lease_owner, session=session)
        if (pause_reason and not self._stop.is_set()
                and self.state != "error"):
            self.state = "paused"
            self._status.put((session, "paused", pause_reason))

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
