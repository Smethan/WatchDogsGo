import asyncio
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock

from watchdogs.host_ble import (
    HostBleScanner,
    advertisement_record,
    list_ble_adapters,
    resolve_ble_adapter,
    shared_adapter_conflict,
)
from watchdogs.notable_detector import NotableDetector
from watchdogs.scan_controller import ScanController


def advertisement(name="Penguin-123", address_type="random", **changes):
    device = NS(address="00:25:DF:00:00:01", details={"props":{"AddressType":address_type}})
    adv = NS(local_name=name, rssi=-52, manufacturer_data={}, service_data={}, service_uuids=[])
    for key, value in changes.items():
        setattr(adv, key, value)
    return device, adv


def test_normalized_host_advertisements_keep_signature_evidence():
    d = advertisement_record(*advertisement(manufacturer_data={0x09c8:b"\x00"}))
    hits = NotableDetector().classify(d, 0)
    assert hits[0]["category"] == "flock" and hits[0]["strength"] == 2
    assert not any(h["category"] == "axon" for h in hits)  # random address
    d = advertisement_record(*advertisement(name="", address_type="public"))
    assert NotableDetector().classify(d, 0)[0]["category"] == "axon"
    device, adv = advertisement(name="", address_type=None)
    device.details = {}
    assert not NotableDetector().classify(advertisement_record(device, adv), 0)


def test_service_data_is_distinct_from_manufacturer_bytes():
    uuid = "0000fc81-0000-1000-8000-00805f9b34fb"
    d = advertisement_record(*advertisement(name="", service_data={uuid:b"BWCDEVICE"}))
    assert any(h["strength"] >= 3 for h in NotableDetector().classify(d, 0))
    d = advertisement_record(*advertisement(name="", manufacturer_data={0xfc81:b"BWCDEVICE"}))
    assert not any(h["strength"] >= 3 for h in NotableDetector().classify(d, 0))


def test_background_ble_lifecycle_and_bounded_handoff():
    holder = {}
    class Scanner:
        def __init__(self, detection_callback, **kwargs):
            self.callback = detection_callback
            holder["scanner"] = self
            self.stopped = False
        async def start(self):
            for index in range(300):
                device, adv = advertisement()
                device.address = f"C0:00:00:00:{index//256:02X}:{index%256:02X}"
                self.callback(device, adv)
                self.callback(device, adv)  # unchanged repeats are throttled
        async def stop(self):
            self.stopped = True

    worker = HostBleScanner(Scanner)
    async def run():
        task = asyncio.create_task(worker._scan("session"))
        # Wait for start acknowledgement without touching any real adapter.
        for _ in range(100):
            if not worker._status.empty():
                break
            await asyncio.sleep(0.001)
        assert worker._records.qsize() == 256 and worker.drops == 44
        events = worker.poll()
        assert events[0] == ("session", "started", None)
        assert len(events) == 65
        worker.stop()
        await asyncio.wait_for(task, 1)
    asyncio.run(run())
    assert holder["scanner"].stopped


def test_bluetooth_start_failure_reports_error_and_cleans_up():
    stopped = Mock()
    class BrokenScanner:
        def __init__(self, **kwargs): pass
        async def start(self): raise RuntimeError("Bluetooth adapter is powered off")
        async def stop(self): stopped()
    worker = HostBleScanner(BrokenScanner)
    worker._run("failed")
    assert worker.poll() == [("failed", "error", "Bluetooth adapter is powered off")]
    assert worker.state == "error"
    stopped.assert_called_once()


def test_worker_construction_and_start_failures_are_terminal_and_retryable():
    class BrokenStart:
        def start(self):
            raise RuntimeError("start failed")

        def is_alive(self):
            return False

    failures = iter((
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("construct failed")),
        lambda **_kwargs: BrokenStart(),
    ))
    worker = HostBleScanner(
        Mock(), thread_factory=lambda **kwargs: next(failures)(**kwargs))

    assert not worker.start("construct")
    assert worker.state == "error"
    assert worker._thread is None
    assert "construct failed" in worker.poll()[0][2]

    assert not worker.start("start")
    assert worker.state == "error"
    assert worker._thread is None
    assert "start failed" in worker.poll()[0][2]

    class Scanner:
        def __init__(self, **_kwargs):
            pass

        async def start(self):
            worker.stop()

        async def stop(self):
            pass

    worker.scanner_factory = Scanner
    worker._thread_factory = threading.Thread
    assert worker.start("retry")
    deadline = time.monotonic() + 1.0
    while worker.worker_active:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert worker.state == "idle"


def test_adapter_mac_resolves_to_current_hci_name(tmp_path):
    controller = tmp_path / "hci7"
    controller.mkdir()
    (controller / "address").write_text("aa:bb:cc:dd:ee:ff\n")

    assert resolve_ble_adapter("AA:BB:CC:DD:EE:FF", tmp_path) == "hci7"
    assert resolve_ble_adapter("auto", tmp_path) is None
    assert list_ble_adapters(tmp_path) == [("AA:BB:CC:DD:EE:FF", "hci7")]


def test_selected_adapter_and_scan_lease_are_used_and_released():
    calls = []

    class Scanner:
        def __init__(self, **kwargs):
            calls.append(("scanner", kwargs))

        async def start(self):
            worker.stop()

        async def stop(self):
            calls.append(("stop", None))

    worker = HostBleScanner(
        Scanner,
        adapter="AA:BB:CC:DD:EE:FF",
        adapter_resolver=lambda value: "hci7",
        lease_acquire=lambda seconds, owner: calls.append(
            ("acquire", seconds, owner)) or (True, ""),
        lease_release=lambda owner: calls.append(("release", owner)),
    )

    asyncio.run(worker._scan("session"))

    assert calls[0][:2] == ("acquire", 20)
    assert calls[1][0] == "scanner"
    assert calls[1][1]["bluez"] == {"adapter": "hci7"}
    assert calls[-2] == ("stop", None)
    assert calls[-1] == ("release", calls[0][2])


def test_phone_priority_denies_scan_without_constructing_scanner():
    scanner = Mock()
    released = Mock()
    worker = HostBleScanner(
        scanner,
        lease_acquire=lambda seconds, owner: (False, "phone connected"),
        lease_release=released,
    )

    asyncio.run(worker._scan("session"))

    assert worker.poll() == [("session", "paused", "phone connected")]
    scanner.assert_not_called()
    released.assert_not_called()


def test_indeterminate_manager_acquire_is_released_by_host_wrapper():
    scanner = Mock()
    releases = []
    worker = HostBleScanner(
        scanner,
        lease_acquire=lambda seconds, owner: (
            False,
            ("acquire timed out; lease may still be active because the "
             "compensating release is unconfirmed"),
        ),
        lease_release=lambda owner: releases.append(owner) or True,
    )

    asyncio.run(worker._scan("session"))

    scanner.assert_not_called()
    assert releases == [worker._lease_owner]
    assert worker._pending_release_owner == ""
    assert worker.state == "paused"
    assert "may still be active" in worker.poll()[0][2]


def test_adapter_resolution_failure_still_releases_scan_lease():
    released = Mock()
    worker = HostBleScanner(
        Mock(),
        adapter_resolver=Mock(side_effect=RuntimeError("adapter disappeared")),
        lease_acquire=lambda seconds, owner: True,
        lease_release=released,
    )

    worker._run("session")

    assert worker.poll() == [("session", "error", "adapter disappeared")]
    released.assert_called_once()


def test_continuous_scan_renews_lease_and_stops_before_pausing(monkeypatch):
    calls = []
    acquisitions = iter(((True, "granted"), (False, "phone connected")))

    class Scanner:
        def __init__(self, **_kwargs):
            self.stopped = False

        async def start(self):
            calls.append("start")

        async def stop(self):
            self.stopped = True
            calls.append("stop")

    def acquire(_seconds, _owner):
        calls.append("acquire")
        return next(acquisitions)

    worker = HostBleScanner(
        Scanner, lease_acquire=acquire,
        lease_release=lambda _owner: calls.append("release"))
    monkeypatch.setattr(
        "watchdogs.host_ble.BLE_SCAN_LEASE_RENEW_SECONDS", 0)

    asyncio.run(worker._scan("session"))

    assert calls == ["acquire", "start", "acquire", "stop", "release"]
    assert worker.state == "paused"
    assert worker.poll() == [
        ("session", "started", None),
        ("session", "paused", "phone connected"),
    ]


def test_each_scan_uses_a_unique_owner_for_acquire_and_release():
    calls = []

    class Scanner:
        def __init__(self, **_kwargs): pass

        async def start(self):
            worker.stop()

        async def stop(self): pass

    worker = HostBleScanner(
        Scanner,
        lease_acquire=lambda seconds, owner: calls.append(
            ("acquire", owner)) or True,
        lease_release=lambda owner: calls.append(("release", owner)))

    for session in ("first", "second"):
        assert worker.start(session)
        deadline = time.monotonic() + 2.0
        while worker.worker_active:
            assert time.monotonic() < deadline
            time.sleep(0.01)

    acquires = [owner for action, owner in calls if action == "acquire"]
    releases = [owner for action, owner in calls if action == "release"]
    assert len(acquires) == 2
    assert len(set(acquires)) == 2
    assert releases == acquires


def test_failed_lease_release_retries_same_owner_before_next_scan():
    calls = []
    release_results = iter((False, True, True))

    class Scanner:
        def __init__(self, **_kwargs):
            pass

        async def start(self):
            worker.stop()

        async def stop(self):
            pass

    def acquire(_seconds, owner):
        calls.append(("acquire", owner))
        return True

    def release(owner):
        calls.append(("release", owner))
        return next(release_results)

    worker = HostBleScanner(
        Scanner, lease_acquire=acquire, lease_release=release)
    assert worker.start("first")
    deadline = time.monotonic() + 1.0
    while worker.worker_active:
        assert time.monotonic() < deadline
        time.sleep(0.01)

    first_owner = calls[0][1]
    assert worker.state == "error"
    assert worker._pending_release_owner == first_owner
    assert any("remains active" in event[2] for event in worker.poll())

    assert worker.start("second")
    deadline = time.monotonic() + 1.0
    while worker.worker_active:
        assert time.monotonic() < deadline
        time.sleep(0.01)

    second_owner = next(
        owner for action, owner in calls
        if action == "acquire" and owner != first_owner)
    assert calls[:3] == [
        ("acquire", first_owner),
        ("release", first_owner),
        ("release", first_owner),
    ]
    assert second_owner != first_owner
    assert worker._pending_release_owner == ""


def test_late_grant_release_exception_is_retained_and_retried(monkeypatch):
    allow_reply = threading.Event()
    calls = []
    release_attempt = 0

    def acquire(_seconds, owner):
        calls.append(("acquire", owner))
        allow_reply.wait(1.0)
        return True

    def release(owner):
        nonlocal release_attempt
        release_attempt += 1
        calls.append(("release", owner))
        if release_attempt == 1:
            raise RuntimeError("release transport failed")
        return True

    worker = HostBleScanner(
        Mock(), lease_acquire=acquire, lease_release=release)
    worker.session = "late"
    monkeypatch.setattr(
        "watchdogs.host_ble.BLE_SCAN_LEASE_CALL_TIMEOUT", 0.02)

    result = asyncio.run(worker._acquire_lease("late-owner", "late"))
    assert result == (False, "Meshtastic BLE scan lease timed out")
    assert worker._pending_acquire_owner == "late-owner"

    # A new generation is blocked while the timed-out acquisition can still
    # grant ownership to the old token.
    assert not worker.start("too-early")
    assert "still pending" in worker.poll()[0][2]

    allow_reply.set()
    deadline = time.monotonic() + 1.0
    while (worker._pending_acquire_owner
           or not worker._pending_release_owner):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert worker._pending_release_owner == "late-owner"
    assert worker.state == "error"

    class Scanner:
        def __init__(self, **_kwargs):
            pass

        async def start(self):
            worker.stop()

        async def stop(self):
            pass

    worker.scanner_factory = Scanner
    assert worker.start("retry")
    deadline = time.monotonic() + 1.0
    while worker.worker_active:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert calls.count(("release", "late-owner")) == 2
    assert worker._pending_release_owner == ""


def test_stop_while_lease_reply_is_pending_never_starts_bluez():
    entered = threading.Event()
    allow_reply = threading.Event()
    scanner = Mock()
    releases = []

    def acquire(_seconds, _owner):
        entered.set()
        allow_reply.wait(1.0)
        return True

    worker = HostBleScanner(
        scanner, lease_acquire=acquire,
        lease_release=lambda owner: releases.append(owner))

    async def run():
        task = asyncio.create_task(worker._scan("race"))
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        worker.stop()
        allow_reply.set()
        await asyncio.wait_for(task, 1.0)

    asyncio.run(run())

    scanner.assert_not_called()
    assert releases == [worker._lease_owner]


def test_close_waits_for_worker_cleanup():
    class Thread:
        alive = True
        timeout = None

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            self.timeout = timeout
            self.alive = False

    worker = HostBleScanner(Mock())
    thread = Thread()
    worker._thread = thread

    assert worker.close(timeout=3.5)
    assert thread.timeout == 3.5
    assert worker._thread is None
    assert worker.state == "idle"


def test_only_controller_contention_is_a_shared_adapter_failure():
    assert shared_adapter_conflict(
        "org.bluez.Error.InProgress: Operation already in progress")
    assert shared_adapter_conflict("Resource busy")
    assert not shared_adapter_conflict("Bluetooth adapter is powered off")
    assert not shared_adapter_conflict("bleak is not installed")


def test_valid_records_prevent_false_heartbeat_but_silence_still_stops():
    now = [0]
    sent = []
    scan = ScanController(sent.append, lambda:now[0])
    scan.supported = True
    scan.start(diagnostic=True)
    scan.handle(dict(kind="started", session=scan.session, seq=1))
    now[0] = 8
    scan.handle(dict(kind="wifi", session=scan.session, seq=3))
    scan.tick()
    assert scan.state == "running" and scan.false_timeouts == 1 and scan.seq_gaps == 1
    assert sent[-1].startswith("wardrive_keepalive")
    now[0] = 9
    scan.tick()
    assert scan.false_timeouts == 1
    # Duplicate and foreign-session records cannot hide a real timeout.
    scan.handle(dict(kind="wifi", session=scan.session, seq=3))
    scan.handle(dict(kind="stats", session="other", seq=4))
    now[0] = 16
    scan.tick()
    assert scan.state == "stopping" and sent[-1] == "stop"


def test_wifi_only_requires_explicit_firmware_capability():
    sent = []
    scan = ScanController(sent.append, lambda:0)
    scan.handle(dict(kind="capabilities", wardrive_serial_v1=True))
    assert not scan.start(wifi_only=True)
    assert scan.start(diagnostic=True)  # current firmware can run ESP Dual Test
    scan.reset()
    scan.handle(dict(kind="capabilities", wardrive_serial_v1=True, wardrive_wifi_serial_v1=True))
    assert scan.start(wifi_only=True)
    assert sent[-1].startswith("start_wardrive_wifi_serial ")


def test_gap_percentage_counts_missing_sequence_positions_not_repeats():
    scan = ScanController(lambda _:None, lambda:0)
    assert scan.gap_percent == 0
    scan.supported = True
    scan.start()
    scan.handle(dict(kind="started", session=scan.session, seq=1))
    scan.handle(dict(kind="wifi", session=scan.session, seq=5))
    assert scan.seq_gaps == 3 and scan.gap_percent == 60
    scan.handle(dict(kind="wifi", session=scan.session, seq=5))
    scan.handle(dict(kind="wifi", session="old", seq=100))
    assert scan.gap_percent == 60
    scan.reset()
    assert scan.gap_percent == 0
