"""Meshtastic and PipBoy pairing agents must never own BlueZ together."""

import asyncio
import sys
import threading
import time
import types
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from watchdogs.watch_manager import PairingAgentRequestError, WatchManager


def _bleak_module(find_device, client=None):
    return NS(
        BleakClient=client or Mock(),
        BleakScanner=NS(find_device_by_address=find_device),
    )


async def _found(address, timeout):
    assert timeout == 10
    return NS(address=address, name="PipBoy-Test")


def test_watch_pairing_scans_before_agent_lease_and_stops_when_denied(
        monkeypatch):
    scanner = Mock()

    async def found(address, timeout):
        scanner(address, timeout)
        return await _found(address, timeout)

    client = Mock()
    monkeypatch.setitem(sys.modules, "bleak", _bleak_module(found, client))
    manager = WatchManager(
        pairing_lease_acquire=Mock(return_value=False),
        pairing_lease_release=Mock(),
    )
    manager._run_dbus_agent = Mock()
    manager._is_paired_and_trusted = Mock(return_value=False)

    asyncio.run(manager._async_connect("AA:BB:CC:DD:EE:FF"))

    scanner.assert_called_once_with("AA:BB:CC:DD:EE:FF", 10)
    manager._pairing_lease_acquire.assert_called_once_with(120)
    manager._pairing_lease_release.assert_not_called()
    manager._run_dbus_agent.assert_not_called()
    client.assert_not_called()
    assert any(
        kind == "error" and "BlueZ agent" in text
        for kind, text in manager.poll_events())


def test_missing_device_never_takes_pairing_agent_lease(monkeypatch):
    async def missing(_address, timeout):
        assert timeout == 10
        return None

    monkeypatch.setitem(sys.modules, "bleak", _bleak_module(missing))
    acquire = Mock(return_value=True)
    released = Mock()
    manager = WatchManager(
        pairing_lease_acquire=acquire,
        pairing_lease_release=released,
    )
    manager._run_dbus_agent = Mock()
    manager._is_paired_and_trusted = Mock(return_value=False)

    asyncio.run(manager._async_connect("AA:BB:CC:DD:EE:FF"))

    acquire.assert_not_called()
    released.assert_not_called()
    manager._run_dbus_agent.assert_not_called()
    assert any(
        kind == "error" and "not found" in text
        for kind, text in manager.poll_events())


def test_bonded_watch_reconnect_does_not_take_pairing_agent_lease(
        monkeypatch):
    async def missing(_address, timeout):
        assert timeout == 10
        return None

    monkeypatch.setitem(sys.modules, "bleak", _bleak_module(missing))
    acquire = Mock(return_value=False)
    release = Mock()
    manager = WatchManager(
        pairing_lease_acquire=acquire,
        pairing_lease_release=release,
    )
    manager._is_paired_and_trusted = Mock(return_value=True)
    manager._run_dbus_agent = Mock()

    asyncio.run(manager._async_connect("AA:BB:CC:DD:EE:FF"))

    acquire.assert_not_called()
    release.assert_not_called()
    manager._run_dbus_agent.assert_not_called()
    assert any(
        kind == "error" and "not found" in text
        for kind, text in manager.poll_events())


def test_agent_registration_failure_releases_lease_and_skips_gatt(
        monkeypatch):
    client = Mock()
    monkeypatch.setitem(sys.modules, "bleak", _bleak_module(_found, client))
    released = Mock()
    manager = WatchManager(
        pairing_lease_acquire=Mock(return_value=True),
        pairing_lease_release=released,
    )
    manager._is_paired_and_trusted = Mock(return_value=False)

    def fail_registration():
        manager._agent_cleanup_confirmed.set()
        manager._agent_ready.set()

    manager._run_dbus_agent = fail_registration
    asyncio.run(manager._async_connect("AA:BB:CC:DD:EE:FF"))

    released.assert_called_once_with()
    client.assert_not_called()
    assert any(
        kind == "error" and "could not be registered" in text
        for kind, text in manager.poll_events())


def test_agent_thread_start_failure_never_joins_unstarted_thread_and_releases(
        monkeypatch):
    client = Mock()
    monkeypatch.setitem(sys.modules, "bleak", _bleak_module(_found, client))
    released = Mock(return_value=True)
    manager = WatchManager(
        pairing_lease_acquire=Mock(return_value=True),
        pairing_lease_release=released,
    )
    manager._is_paired_and_trusted = Mock(return_value=False)

    class UnstartedThread:
        def start(self):
            raise RuntimeError("thread start failed")

        def is_alive(self):
            return False

        def join(self, _timeout):
            raise AssertionError("an unstarted thread must not be joined")

    manager._agent_thread_factory = lambda **_kwargs: UnstartedThread()

    asyncio.run(manager._async_connect("AA:BB:CC:DD:EE:FF"))

    released.assert_called_once_with()
    assert manager._agent_thread is None
    assert not manager.pairing_lease_release_pending
    client.assert_not_called()
    assert any(
        kind == "error" and "could not start" in text
        for kind, text in manager.poll_events())


def test_disconnect_during_pairing_cleans_agent_and_skips_gatt(monkeypatch):
    client = Mock()
    monkeypatch.setitem(sys.modules, "bleak", _bleak_module(_found, client))
    released = Mock()
    manager = WatchManager(
        pairing_lease_acquire=Mock(return_value=True),
        pairing_lease_release=released,
    )
    manager._is_paired_and_trusted = Mock(return_value=False)

    def agent():
        manager._agent_registered = True
        manager._agent_ready.set()
        manager._agent_stop_event.wait(1)
        manager._agent_registered = False
        manager._agent_cleanup_confirmed.set()

    async def cancel_pairing(_address, *, renew_lease=None):
        assert renew_lease is manager._pairing_lease_acquire
        manager.disconnect()
        return False

    manager._run_dbus_agent = agent
    manager._ensure_paired = cancel_pairing
    asyncio.run(manager._async_connect("AA:BB:CC:DD:EE:FF"))

    released.assert_called_once_with()
    client.assert_not_called()
    assert manager._agent_cleanup_confirmed.is_set()


def test_pairing_requests_are_bound_to_exact_device_and_cancel_state():
    manager = WatchManager()
    manager._pairing_device_suffix = "/dev_AA_BB_CC_DD_EE_FF"

    manager._validate_pairing_device(
        "/org/bluez/hci9/dev_AA_BB_CC_DD_EE_FF")
    with pytest.raises(PairingAgentRequestError) as wrong:
        manager._validate_pairing_device(
            "/org/bluez/hci9/dev_11_22_33_44_55_66")
    assert wrong.value.dbus_error_name == "org.bluez.Error.Rejected"

    manager._stop_event.set()
    with pytest.raises(PairingAgentRequestError) as cancelled:
        manager._validate_pairing_device(
            "/org/bluez/hci9/dev_AA_BB_CC_DD_EE_FF")
    assert cancelled.value.dbus_error_name == "org.bluez.Error.Canceled"


def test_pin_timeout_and_cancel_fail_closed_without_zero_passkey():
    manager = WatchManager()
    device = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    manager._pairing_device_suffix = "/dev_AA_BB_CC_DD_EE_FF"

    with pytest.raises(PairingAgentRequestError) as timeout:
        manager._request_pairing_pin(device, timeout=0)
    assert timeout.value.dbus_error_name == "org.bluez.Error.Canceled"
    assert manager._pin_value is None
    assert manager.pin_requested is False

    outcome = []

    def wait_for_pin():
        try:
            manager._request_pairing_pin(device, timeout=1)
        except PairingAgentRequestError as exc:
            outcome.append(exc)

    waiter = threading.Thread(target=wait_for_pin)
    waiter.start()
    deadline = time.monotonic() + 1
    while not manager.pin_requested and time.monotonic() < deadline:
        time.sleep(0.005)
    assert manager.pin_requested
    manager._agent_cancelled()
    waiter.join(1)

    assert not waiter.is_alive()
    assert len(outcome) == 1
    assert outcome[0].dbus_error_name == "org.bluez.Error.Canceled"
    assert manager._pin_value is None


def test_nested_glib_pin_wait_dispatches_bluez_cancel():
    manager = WatchManager()
    device = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    manager._pairing_device_suffix = "/dev_AA_BB_CC_DD_EE_FF"

    class Loop:
        def __init__(self):
            self.quit_called = False

        def run(self):
            manager._agent_cancelled()

        def quit(self):
            self.quit_called = True

    loop = Loop()
    glib = NS(MainLoop=lambda: loop, timeout_add=Mock())

    with pytest.raises(PairingAgentRequestError) as cancelled:
        manager._request_pairing_pin_with_glib(device, glib)

    assert cancelled.value.dbus_error_name == "org.bluez.Error.Canceled"
    assert loop.quit_called
    assert manager._pin_wait_loop is None
    assert manager._pin_value is None


def test_numeric_confirmation_requires_matching_watch_passkey():
    manager = WatchManager()
    device = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    manager._pairing_device_suffix = "/dev_AA_BB_CC_DD_EE_FF"

    class Loop:
        def __init__(self, supplied):
            self.supplied = supplied

        def run(self):
            manager.provide_pin(self.supplied)

        def quit(self):
            pass

    glib = NS(MainLoop=lambda: Loop(123456), timeout_add=Mock())
    manager._confirm_pairing_passkey_with_glib(device, 123456, glib)
    assert manager.pin_requested is False

    glib = NS(MainLoop=lambda: Loop(654321), timeout_add=Mock())
    with pytest.raises(PairingAgentRequestError) as mismatch:
        manager._confirm_pairing_passkey_with_glib(device, 123456, glib)
    assert mismatch.value.dbus_error_name == "org.bluez.Error.Rejected"


def test_numeric_confirmation_cancel_and_wrong_device_fail_closed():
    manager = WatchManager()
    device = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    manager._pairing_device_suffix = "/dev_AA_BB_CC_DD_EE_FF"

    class CancelLoop:
        def run(self):
            manager._agent_cancelled()

        def quit(self):
            pass

    glib = NS(MainLoop=CancelLoop, timeout_add=Mock())
    with pytest.raises(PairingAgentRequestError) as cancelled:
        manager._confirm_pairing_passkey_with_glib(
            device, 123456, glib)
    assert cancelled.value.dbus_error_name == "org.bluez.Error.Canceled"

    with pytest.raises(PairingAgentRequestError) as wrong:
        manager._confirm_pairing_passkey_with_glib(
            "/org/bluez/hci0/dev_11_22_33_44_55_66", 123456, glib)
    assert wrong.value.dbus_error_name == "org.bluez.Error.Rejected"


def test_disconnect_cancels_running_bluetoothctl(monkeypatch):
    manager = WatchManager()

    class Process:
        def __init__(self):
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        def communicate(self, timeout):
            return ("", "")

    process = Process()
    monkeypatch.setattr("subprocess.Popen", Mock(return_value=process))

    async def run():
        task = asyncio.create_task(manager._run_bluetoothctl(
            "pair", "AA:BB:CC:DD:EE:FF", timeout=90))
        await asyncio.sleep(0.01)
        manager.disconnect()
        return await task

    assert asyncio.run(run()) is None
    assert process.terminated


def test_disconnect_after_discovery_prevents_agent_lease_and_pairing(
        monkeypatch):
    manager = WatchManager(
        pairing_lease_acquire=Mock(return_value=True),
        pairing_lease_release=Mock())

    async def found(address, timeout):
        manager.disconnect()
        return await _found(address, timeout)

    client = Mock()
    monkeypatch.setitem(sys.modules, "bleak", _bleak_module(found, client))
    manager._is_paired_and_trusted = Mock(return_value=False)
    manager._run_dbus_agent = Mock()
    manager._ensure_paired = Mock()

    asyncio.run(manager._async_connect("AA:BB:CC:DD:EE:FF"))

    manager._pairing_lease_acquire.assert_not_called()
    manager._run_dbus_agent.assert_not_called()
    manager._ensure_paired.assert_not_called()
    client.assert_not_called()


def test_delayed_scan_lease_grant_after_disconnect_never_starts_scan(
        monkeypatch):
    discover = Mock()

    async def unexpected_discover(**_kwargs):
        discover()
        return {}

    monkeypatch.setitem(
        sys.modules, "bleak",
        NS(BleakScanner=NS(discover=unexpected_discover)))
    released = Mock(return_value=True)
    manager = WatchManager(scan_lease_release=released)

    def acquire(_seconds):
        manager.disconnect()
        return True

    manager._scan_lease_acquire = acquire
    asyncio.run(manager._async_scan())

    discover.assert_not_called()
    released.assert_called_once_with()


def test_delayed_connect_scan_lease_grant_after_disconnect_skips_bluez(
        monkeypatch):
    find_device = Mock()

    async def unexpected_find(*_args, **_kwargs):
        find_device()
        return NS(address="AA:BB:CC:DD:EE:FF", name="PipBoy-Test")

    client = Mock()
    monkeypatch.setitem(
        sys.modules, "bleak", _bleak_module(unexpected_find, client))
    released = Mock(return_value=True)
    manager = WatchManager(scan_lease_release=released)
    manager._is_paired_and_trusted = Mock(return_value=False)

    def acquire(_seconds):
        manager.disconnect()
        return True

    manager._scan_lease_acquire = acquire
    asyncio.run(manager._async_connect("AA:BB:CC:DD:EE:FF"))

    find_device.assert_not_called()
    client.assert_not_called()
    released.assert_called_once_with()


def test_deferred_agent_cleanup_releases_lease_after_confirmed_teardown():
    manager = WatchManager()
    released = Mock()

    def agent():
        manager._agent_stop_event.wait(1)
        manager._agent_cleanup_confirmed.set()

    agent_thread = threading.Thread(target=agent)
    manager._agent_thread = agent_thread
    agent_thread.start()
    cleanup = manager._schedule_agent_cleanup(agent_thread, released)
    cleanup.join(2)

    assert not cleanup.is_alive()
    assert manager._agent_cleanup_thread is None
    assert not agent_thread.is_alive()
    released.assert_called_once_with()
    assert manager._agent_thread is None


def test_false_pairing_release_is_retried_until_confirmed():
    manager = WatchManager()
    results = iter((False, False, True))
    released = Mock(side_effect=lambda: next(results))
    manager._agent_cleanup_confirmed.set()

    cleanup = manager._schedule_agent_cleanup(None, released)
    cleanup.join(2)

    assert not cleanup.is_alive()
    assert released.call_count == 3
    assert not manager.pairing_lease_release_pending


def test_unconfirmed_deferred_release_remains_visible_and_retryable(
        monkeypatch):
    manager = WatchManager()
    released = Mock(return_value=False)
    manager._agent_cleanup_confirmed.set()
    monkeypatch.setattr("watchdogs.watch_manager.time.sleep", lambda _value: None)

    cleanup = manager._schedule_agent_cleanup(None, released)
    cleanup.join(1)

    assert released.call_count == 10
    assert manager.pairing_lease_release_pending
    assert manager.close(timeout=0) is False
    assert any(
        kind == "error" and "remains unreleased" in text
        for kind, text in manager.poll_events())


def test_cleanup_worker_start_failure_retains_lease_for_later_retry():
    manager = WatchManager()
    released = Mock(return_value=True)
    manager._agent_cleanup_confirmed.set()

    class BrokenThread:
        def start(self):
            raise RuntimeError("cleanup start failed")

    manager._agent_cleanup_thread_factory = lambda **_kwargs: BrokenThread()

    assert manager._schedule_agent_cleanup(None, released) is None
    assert manager._agent_cleanup_thread is None
    assert manager.pairing_lease_release_pending
    released.assert_not_called()
    assert any(
        kind == "error" and "lease remains retained" in text
        for kind, text in manager.poll_events())


def test_deferred_cleanup_blocks_new_connect_until_old_lease_is_released(
        monkeypatch):
    manager = WatchManager()
    release_started = threading.Event()
    allow_release = threading.Event()

    def release_lease():
        release_started.set()
        allow_release.wait(1)

    def agent():
        manager._agent_stop_event.wait(1)
        manager._agent_cleanup_confirmed.set()

    agent_thread = threading.Thread(target=agent)
    manager._agent_thread = agent_thread
    agent_thread.start()
    cleanup = manager._schedule_agent_cleanup(agent_thread, release_lease)
    assert release_started.wait(1)

    thread_factory = Mock()
    monkeypatch.setattr(threading, "Thread", thread_factory)
    manager.connect("AA:BB:CC:DD:EE:FF")
    thread_factory.assert_not_called()

    allow_release.set()
    cleanup.join(1)
    assert not cleanup.is_alive()
    assert manager._agent_cleanup_thread is None


def test_scan_preflight_runs_off_the_calling_thread():
    manager = WatchManager()
    entered = threading.Event()
    unblock = threading.Event()

    def blocked_existing():
        entered.set()
        unblock.wait(1)
        return None

    manager.check_existing = blocked_existing
    started = time.monotonic()
    manager.scan()
    elapsed = time.monotonic() - started

    assert elapsed < 0.1
    assert entered.wait(1)
    manager.disconnect()
    unblock.set()
    assert manager.close(1)


def test_scan_after_disconnect_reconnects_known_watch_with_address():
    manager = WatchManager()
    address = "AA:BB:CC:DD:EE:FF"
    manager.disconnect()
    manager.check_existing = Mock(return_value=address)
    manager._stop_event.wait = Mock(return_value=False)
    manager._run_bluetoothctl = Mock(
        return_value=asyncio.sleep(0, result=(0, "", "")))
    connected = []

    async def record_connect(selected):
        connected.append(selected)

    manager._async_connect = record_connect
    manager.scan()
    worker = manager._thread
    worker.join(1)

    assert not worker.is_alive()
    assert connected == [address]
    assert manager.device_address == address
    assert manager._agent_stop_event.is_set() is False


def test_close_waits_for_cleanup_worker_created_during_main_join():
    manager = WatchManager()
    cleanup_created = threading.Event()

    def cleanup():
        cleanup_created.set()
        time.sleep(0.05)

    def main_worker():
        manager._stop_event.wait(1)
        cleanup_thread = threading.Thread(target=cleanup)
        manager._agent_cleanup_thread = cleanup_thread
        cleanup_thread.start()

    manager._thread = threading.Thread(target=main_worker)
    manager._thread.start()

    assert manager.close(1)
    assert cleanup_created.is_set()
    assert not manager._agent_cleanup_thread.is_alive()


def test_dbus_setup_failure_signals_ready_and_cleanup(monkeypatch):
    dbus = types.ModuleType("dbus")
    dbus.__path__ = []
    service = types.ModuleType("dbus.service")
    mainloop = types.ModuleType("dbus.mainloop")
    mainloop.__path__ = []
    glib_mainloop = types.ModuleType("dbus.mainloop.glib")
    glib_mainloop.DBusGMainLoop = Mock()
    mainloop.glib = glib_mainloop
    dbus.service = service
    dbus.mainloop = mainloop
    dbus.SystemBus = Mock(side_effect=RuntimeError("system bus unavailable"))
    gi = types.ModuleType("gi")
    gi.__path__ = []
    repository = types.ModuleType("gi.repository")
    repository.GLib = NS()
    gi.repository = repository
    monkeypatch.setitem(sys.modules, "dbus", dbus)
    monkeypatch.setitem(sys.modules, "dbus.service", service)
    monkeypatch.setitem(sys.modules, "dbus.mainloop", mainloop)
    monkeypatch.setitem(sys.modules, "dbus.mainloop.glib", glib_mainloop)
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)

    manager = WatchManager()
    manager._run_dbus_agent()

    assert manager._agent_ready.is_set()
    assert manager._agent_cleanup_confirmed.is_set()
    assert manager._agent_registered is False
    assert any(
        kind == "log" and "system bus unavailable" in text
        for kind, text in manager.poll_events())


def test_bluez_release_confirms_cleanup_and_wakes_waiters():
    manager = WatchManager()
    loop = Mock()
    manager._glib_loop = loop
    manager._agent_registered = True
    manager._pin_requested = True
    manager._pin_value = 123456

    manager._agent_released()

    assert manager._agent_registered is False
    assert manager._agent_cleanup_confirmed.is_set()
    assert manager._agent_stop_event.is_set()
    assert manager._pin_event.is_set()
    assert manager.pin_requested is False
    assert manager._pin_value is None
    loop.quit.assert_called_once_with()


def test_second_connect_is_ignored_while_worker_is_active(monkeypatch):
    manager = WatchManager()
    thread_factory = Mock()
    monkeypatch.setattr(threading, "Thread", thread_factory)
    manager._thread = NS(is_alive=lambda: True)

    assert manager.connect("AA:BB:CC:DD:EE:FF") is False

    thread_factory.assert_not_called()


def test_false_scan_lease_release_is_retained_and_retried_before_connect(
        monkeypatch):
    release_results = iter((False, True))
    released = Mock(side_effect=lambda: next(release_results))
    manager = WatchManager(
        scan_lease_acquire=Mock(return_value=True),
        scan_lease_release=released,
    )

    allowed, _reason = manager._take_scan_lease()
    assert allowed
    assert manager._drop_scan_lease() is False
    assert manager.scan_lease_release_pending

    thread = Mock()
    thread.is_alive.return_value = False
    monkeypatch.setattr(threading, "Thread", Mock(return_value=thread))

    assert manager.connect("AA:BB:CC:DD:EE:FF") is True
    assert released.call_count == 2
    assert not manager.scan_lease_release_pending
    thread.start.assert_called_once_with()


def test_exceptional_scan_lease_release_is_visible_and_close_retries():
    released = Mock(side_effect=(RuntimeError("socket reset"), True))
    manager = WatchManager(
        scan_lease_acquire=Mock(return_value=True),
        scan_lease_release=released,
    )

    allowed, _reason = manager._take_scan_lease()
    assert allowed
    assert manager._drop_scan_lease() is False
    assert manager.scan_lease_release_pending
    events = manager.poll_events()
    assert any(
        kind == "error" and "new Bluetooth work is blocked" in text
        for kind, text in events)

    assert manager.close(timeout=0) is True
    assert released.call_count == 2
    assert not manager.scan_lease_release_pending


def test_connect_reports_thread_start_failure(monkeypatch):
    class BrokenThread:
        def start(self):
            raise RuntimeError("thread unavailable")

    manager = WatchManager()
    monkeypatch.setattr(threading, "Thread", Mock(return_value=BrokenThread()))

    assert manager.connect("AA:BB:CC:DD:EE:FF") is False
    assert manager.worker_active is False
    assert any(
        kind == "error" and "could not start" in text
        for kind, text in manager.poll_events())
