import asyncio
from types import SimpleNamespace as NS
from unittest.mock import Mock

from watchdogs.host_ble import (
    HostBleScanner, advertisement_record, resolve_ble_adapter,
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
    stopped.assert_called_once()


def test_adapter_mac_resolves_to_current_hci_name(tmp_path):
    controller = tmp_path / "hci7"
    controller.mkdir()
    (controller / "address").write_text("aa:bb:cc:dd:ee:ff\n")

    assert resolve_ble_adapter("AA:BB:CC:DD:EE:FF", tmp_path) == "hci7"
    assert resolve_ble_adapter("auto", tmp_path) is None


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
        lease_acquire=lambda seconds: calls.append(("acquire", seconds)) or (True, ""),
        lease_release=lambda: calls.append(("release", None)),
    )

    asyncio.run(worker._scan("session"))

    assert calls[0] == ("acquire", 20)
    assert calls[1][0] == "scanner"
    assert calls[1][1]["bluez"] == {"adapter": "hci7"}
    assert calls[-2:] == [("stop", None), ("release", None)]


def test_phone_priority_denies_scan_without_constructing_scanner():
    scanner = Mock()
    released = Mock()
    worker = HostBleScanner(
        scanner,
        lease_acquire=lambda seconds: (False, "phone connected"),
        lease_release=released,
    )

    asyncio.run(worker._scan("session"))

    assert worker.poll() == [("session", "paused", "phone connected")]
    scanner.assert_not_called()
    released.assert_not_called()


def test_adapter_resolution_failure_still_releases_scan_lease():
    released = Mock()
    worker = HostBleScanner(
        Mock(),
        adapter_resolver=Mock(side_effect=RuntimeError("adapter disappeared")),
        lease_acquire=lambda seconds: True,
        lease_release=released,
    )

    worker._run("session")

    assert worker.poll() == [("session", "error", "adapter disappeared")]
    released.assert_called_once_with()


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
