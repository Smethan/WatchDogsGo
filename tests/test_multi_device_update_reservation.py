"""Updater claims are scoped to one physical serial endpoint."""
import json

import pytest

from test_multi_device_integration import (
    FakeApp, FakeSerial, Harness, device_info,
)


def _candidate(endpoint, serial_number, location):
    return {"endpoint": endpoint, "usb_identity": {
        "vid": 0x303A, "pid": 0x1001,
        "serial_number": serial_number, "location": location,
    }}


def test_two_unnegotiated_endpoints_reserve_only_selected_session(tmp_path):
    h = Harness.__new__(Harness)
    h.app = FakeApp()
    h.serials = [FakeSerial("/dev/blank0", "blank-0"),
                 FakeSerial("/dev/blank1", "blank-1")]
    from watchdogs.multi_device import MultiDeviceRuntime
    h.runtime = MultiDeviceRuntime(h.app, h.serials, clock=lambda: 10.0)
    selected = h.runtime.endpoints["/dev/blank0"]
    peer = h.runtime.endpoints["/dev/blank1"]

    assert h.runtime.reserve(h.serials[0]) is selected
    assert h.runtime.reserved == set()  # no blank factory-ID sentinel
    before = len(h.serials[1].read_bounds)
    h.runtime.poll()
    assert h.runtime.is_reserved(selected)
    assert not h.runtime.is_reserved(peer)
    assert h.serials[0].read_bounds == []
    assert len(h.serials[1].read_bounds) == before + 1


def test_unknown_selected_session_stays_reserved_beside_capable_peer(tmp_path):
    h = Harness(tmp_path)
    unknown = FakeSerial("/dev/unknown", "usb-unknown")
    session = h.runtime.attach(unknown)
    h.app.serial = unknown
    assert h.runtime.available
    assert h.runtime.reserve(unknown) is session

    peer_before = len(h.serials[1].read_bounds)
    h.runtime.poll()
    assert unknown.read_bounds == []
    assert len(h.serials[1].read_bounds) == peer_before + 1


def test_reservation_survives_metadata_identity_assignment(tmp_path):
    h = Harness.__new__(Harness)
    h.app = FakeApp()
    unknown = FakeSerial("/dev/unknown", "usb-unknown")
    from watchdogs.multi_device import MultiDeviceRuntime
    runtime = MultiDeviceRuntime(h.app, [unknown], clock=lambda: 10.0)
    session = runtime.endpoints[unknown.device]
    assert runtime.reserve(unknown) is session

    info = json.loads(device_info(0, "xiao", [1, 6, 11, 36, 40])[4:])
    info["device_id"] = "ABCDEF012345"
    session.set_device_info(info)
    assert runtime.is_reserved(session)
    before = len(unknown.read_bounds)
    runtime.poll()
    assert len(unknown.read_bounds) == before


def test_same_usb_renumbered_candidate_is_hidden_until_release(tmp_path, monkeypatch):
    h = Harness.__new__(Harness)
    h.app = FakeApp()
    original = FakeSerial("/dev/ttyACM0", "usb-move")
    from watchdogs.multi_device import MultiDeviceRuntime
    runtime = MultiDeviceRuntime(h.app, [original], clock=lambda: 10.0)
    session = runtime.endpoints[original.device]
    assert runtime.reserve(original) is session
    opened = []

    def factory(endpoint):
        opened.append(endpoint)
        manager = FakeSerial(endpoint, "usb-move")
        manager.setup = lambda: None
        return manager

    monkeypatch.setattr("watchdogs.multi_device.supported_candidates",
                        lambda: [_candidate("/dev/ttyACM8", "usb-move", "usb-move")])
    monkeypatch.setattr("watchdogs.multi_device.SerialManager", factory)
    runtime.discover()
    assert opened == []

    runtime.release(session)
    assert opened == ["/dev/ttyACM8"]
    assert "/dev/ttyACM8" in runtime.endpoints
    assert runtime.reserved == set()
    assert runtime._reserved_sessions == {}


def test_release_unknown_id_reopens_new_session_and_manager_must_match(tmp_path, monkeypatch):
    h = Harness.__new__(Harness)
    h.app = FakeApp()
    original = FakeSerial("/dev/ttyACM0", "usb-unknown")
    from watchdogs.multi_device import MultiDeviceRuntime
    runtime = MultiDeviceRuntime(h.app, [original], clock=lambda: 10.0)
    session = runtime.endpoints[original.device]
    assert session.device_id == ""
    assert runtime.reserve(FakeSerial(original.device, "usb-unknown")) is None
    assert runtime.reserve(original) is session
    opened = []

    def factory(endpoint):
        opened.append(endpoint)
        manager = FakeSerial(endpoint, "usb-unknown")
        manager.setup = lambda: None
        return manager

    monkeypatch.setattr("watchdogs.multi_device.supported_candidates",
                        lambda: [_candidate("/dev/ttyACM9", "usb-unknown", "usb-unknown")])
    monkeypatch.setattr("watchdogs.multi_device.SerialManager", factory)
    runtime.release(session)

    replacement = runtime.endpoints["/dev/ttyACM9"]
    assert opened == ["/dev/ttyACM9"]
    assert replacement is not session
    assert replacement.device_id == ""
    assert not runtime.is_reserved(replacement)
    before = len(replacement.serial.read_bounds)
    runtime.poll()
    assert len(replacement.serial.read_bounds) == before + 1


def test_stale_or_repeated_release_cannot_clear_replacement_claim(tmp_path, monkeypatch):
    h = Harness.__new__(Harness)
    h.app = FakeApp()
    original = FakeSerial("/dev/ttyACM0", "usb-stable")
    from watchdogs.multi_device import MultiDeviceRuntime
    runtime = MultiDeviceRuntime(h.app, [original], clock=lambda: 10.0)
    old_session = runtime.endpoints[original.device]
    assert runtime.reserve(original) is old_session
    opened = []

    def factory(endpoint):
        opened.append(endpoint)
        manager = FakeSerial(endpoint, "usb-stable")
        manager.setup = lambda: None
        return manager

    monkeypatch.setattr("watchdogs.multi_device.supported_candidates",
                        lambda: [_candidate("/dev/ttyACM0", "usb-stable", "usb-stable")])
    monkeypatch.setattr("watchdogs.multi_device.SerialManager", factory)
    runtime.release(old_session)

    replacement = runtime.endpoints["/dev/ttyACM0"]
    assert replacement is not old_session
    assert runtime.reserve(replacement.serial) is replacement
    assert runtime._reserved_sessions[replacement.endpoint] is replacement

    runtime.release(old_session)
    runtime.release(old_session)
    assert opened == ["/dev/ttyACM0"]
    assert runtime.endpoints["/dev/ttyACM0"] is replacement
    assert runtime._reserved_sessions[replacement.endpoint] is replacement
    assert replacement.serial.is_open


def test_release_reacquisition_requires_matching_vid_and_pid(tmp_path, monkeypatch):
    h = Harness.__new__(Harness)
    h.app = FakeApp()
    original = FakeSerial("/dev/ttyACM0", "usb-profile")
    from watchdogs.multi_device import MultiDeviceRuntime
    runtime = MultiDeviceRuntime(h.app, [original], clock=lambda: 10.0)
    runtime.ports = ["/dev/configured"]
    runtime._configured_usb["/dev/configured"] = {
        "vid": 0x303A, "pid": 0x1001,
        "serial_number": "usb-profile", "location": "usb-profile",
    }
    session = runtime.endpoints[original.device]
    assert runtime.reserve(original) is session
    opened = []

    def factory(endpoint):
        opened.append(endpoint)
        manager = FakeSerial(endpoint, "usb-profile")
        manager.setup = lambda: None
        return manager

    monkeypatch.setattr("watchdogs.multi_device.supported_candidates", lambda: [
        _candidate("/dev/ttyACM8", "usb-profile", "usb-profile") | {
            "usb_identity": {"vid": 0x303A, "pid": 0x4001,
                             "serial_number": "usb-profile", "location": "usb-profile"}
        }
    ])
    monkeypatch.setattr("watchdogs.multi_device.SerialManager", factory)
    runtime.release(session)
    assert opened == []
    assert "/dev/ttyACM8" not in runtime.endpoints


def test_runtime_does_not_misclassify_callback_type_error_as_disconnect(tmp_path):
    h = Harness.__new__(Harness)
    h.app = FakeApp()
    manager = FakeSerial("/dev/callback", "usb-callback", ["plain line"])
    from watchdogs.multi_device import MultiDeviceRuntime
    runtime = MultiDeviceRuntime(h.app, [manager], clock=lambda: 10.0)
    session = runtime.endpoints[manager.device]

    def broken_callback(_line):
        raise TypeError("application callback bug")

    session.on_line = broken_callback
    with pytest.raises(TypeError, match="application callback bug"):
        runtime.poll()
