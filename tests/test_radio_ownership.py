import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from watchdogs.lora_manager import LoRaManager
from watchdogs.radio_ownership import RadioOwnership, RadioOwnershipBusy


def watchdogs_game_cls():
    try:
        from watchdogs.app import WatchDogsGame
    except ModuleNotFoundError as exc:
        pytest.skip(f"WatchDogsGame dependency unavailable: {exc.name}")
    return WatchDogsGame


class FakeOwnership:
    def __init__(self, events=None, *, busy=False):
        self.events = events if events is not None else []
        self.busy = busy
        self.held = False
        self.acquire_calls = 0

    def acquire(self, owner):
        self.acquire_calls += 1
        self.events.append(("acquire", owner))
        if self.busy:
            raise RadioOwnershipBusy("radio lock busy")
        self.held = True

    def release(self):
        self.events.append(("release", self.held))
        self.held = False


class FakeService:
    def __init__(self, events=None, *, ready=True, suspend_ok=True,
                 resume_ok=True, ownership=None):
        self.events = events if events is not None else []
        self.ready = ready
        self.suspend_ok = suspend_ok
        self.resume_ok = resume_ok
        self.ownership = ownership
        self.resume_calls = 0

    def service_ready(self):
        self.events.append(("service_ready", self.ready))
        return self.ready

    def suspend_service(self, timeout=10.0):
        self.events.append(("suspend", timeout))
        if self.suspend_ok:
            self.ready = False
        return self.suspend_ok

    def resume_service(self, timeout=15.0, *, connect=True):
        if self.ownership is not None:
            assert not self.ownership.held
        self.resume_calls += 1
        self.events.append(("resume", timeout, connect))
        if self.resume_ok:
            self.ready = True
        return self.resume_ok


def drain(manager):
    rows = []
    while not manager.queue.empty():
        rows.append(manager.queue.get_nowait())
    return rows


def test_radio_ownership_is_exclusive_and_reusable(tmp_path):
    path = tmp_path / "locks" / "aio-sx1262.lock"
    first = RadioOwnership(path)
    second = RadioOwnership(path)

    first.acquire("first")
    assert first.held
    assert path.exists()
    assert "owner=first" in path.read_text()
    with pytest.raises(RadioOwnershipBusy, match="already owned"):
        second.acquire("second")

    first.release()
    assert not first.held
    second.acquire("second")
    assert second.held
    second.release()
    assert path.exists()


def test_radio_ownership_release_failure_stays_conservatively_held(
        tmp_path, monkeypatch):
    ownership = RadioOwnership(tmp_path / "radio.lock")
    ownership.acquire("test")
    real_flock = __import__("fcntl").flock

    def fail_unlock(fd, operation):
        import fcntl
        if operation == fcntl.LOCK_UN:
            raise OSError("unlock failed")
        return real_flock(fd, operation)

    monkeypatch.setattr("watchdogs.radio_ownership.fcntl.flock", fail_unlock)
    with pytest.raises(OSError, match="unlock failed"):
        ownership.release()
    assert ownership.held

    monkeypatch.setattr("watchdogs.radio_ownership.fcntl.flock", real_flock)
    ownership.release()
    assert not ownership.held


def test_direct_session_orders_service_lock_spi_and_release(monkeypatch):
    events = []
    ownership = FakeOwnership(events)
    service = FakeService(events, ownership=ownership)
    manager = LoRaManager(
        radio_ownership=ownership, service_handoff=service)
    radio = object()
    monkeypatch.setattr(
        manager, "_init_radio",
        lambda: events.append(("radio_init", ownership.held)) or radio)
    monkeypatch.setattr(
        manager, "_close_radio_resources",
        lambda value: events.append(
            ("spi_close", value, ownership.held)) or True)
    manager.mode = "meshcore"

    assert manager._open_radio_session() is radio
    assert ownership.held
    manager._cleanup_radio(radio)

    labels = [event[0] for event in events]
    assert labels == [
        "service_ready", "suspend", "acquire", "radio_init",
        "spi_close", "release",
    ]
    assert events[3][1] is True
    assert events[4][2] is True
    assert not ownership.held


def test_init_failure_releases_before_restoring_previous_service(monkeypatch):
    events = []
    ownership = FakeOwnership(events)
    service = FakeService(events, ownership=ownership)
    manager = LoRaManager(
        radio_ownership=ownership, service_handoff=service)
    monkeypatch.setattr(
        manager, "_init_radio",
        lambda: events.append(("radio_init", ownership.held)) or None)
    monkeypatch.setattr(
        manager, "_close_radio_resources",
        lambda value: events.append(
            ("spi_close", value, ownership.held)) or True)

    assert manager._open_radio_session() is None
    labels = [event[0] for event in events]
    assert labels.index("spi_close") < labels.index("release")
    assert labels.index("release") < labels.index("resume")
    assert service.resume_calls == 1
    assert not ownership.held


def test_configuration_failure_releases_before_restoring_service(monkeypatch):
    events = []
    ownership = FakeOwnership(events)
    service = FakeService(events, ownership=ownership)
    manager = LoRaManager(
        radio_ownership=ownership, service_handoff=service)

    class Radio:
        def setFrequency(self, _freq):
            events.append(("configure", ownership.held))
            raise RuntimeError("configuration failed")

    radio = Radio()
    monkeypatch.setattr(manager, "_init_radio", lambda: radio)
    monkeypatch.setattr(
        manager, "_close_radio_resources",
        lambda value: events.append(
            ("spi_close", value, ownership.held)) or True)
    manager.mode = "sniffer"

    manager._run_sniffer(868_100_000, 7, 5, 125_000, "test")

    labels = [event[0] for event in events]
    assert labels.index("configure") < labels.index("spi_close")
    assert labels.index("spi_close") < labels.index("release")
    assert labels.index("release") < labels.index("resume")
    assert service.resume_calls == 1
    assert not ownership.held


def test_lock_contention_emits_once_and_does_not_restart_service(monkeypatch):
    events = []
    ownership = FakeOwnership(events, busy=True)
    service = FakeService(events, ownership=ownership)
    manager = LoRaManager(
        radio_ownership=ownership, service_handoff=service)
    init_calls = []
    monkeypatch.setattr(manager, "_init_radio", lambda: init_calls.append(1))

    assert manager._open_radio_session() is None
    errors = [text for text, attr in drain(manager) if attr == "error"]
    assert len(errors) == 1
    assert "stop the other radio user" in errors[0]
    assert ownership.acquire_calls == 1
    assert init_calls == []
    assert service.resume_calls == 0


def test_generic_lock_failure_restores_previously_ready_service(monkeypatch):
    events = []

    class BrokenOwnership(FakeOwnership):
        def acquire(self, owner):
            self.acquire_calls += 1
            self.events.append(("acquire", owner))
            raise PermissionError("lock directory denied")

    ownership = BrokenOwnership(events)
    service = FakeService(events, ownership=ownership)
    manager = LoRaManager(
        radio_ownership=ownership, service_handoff=service)
    monkeypatch.setattr(manager, "_init_radio", lambda: pytest.fail(
        "SPI must not be touched after lock acquisition fails"))

    assert manager._open_radio_session() is None
    assert service.resume_calls == 1
    assert [event[0] for event in events][-1] == "resume"


def test_stop_requested_during_service_suspend_never_claims_radio(monkeypatch):
    events = []
    ownership = FakeOwnership(events)
    manager = LoRaManager(radio_ownership=ownership)

    class CancellingService(FakeService):
        def suspend_service(self, timeout=10.0):
            self.events.append(("suspend", timeout))
            self.ready = False
            manager._stop_event.set()
            return True

    service = CancellingService(events, ownership=ownership)
    manager._service_handoff = service
    monkeypatch.setattr(manager, "_init_radio", lambda: pytest.fail(
        "SPI must not be touched after stop is requested"))

    assert manager._open_radio_session() is None
    assert ownership.acquire_calls == 0
    assert not ownership.held


def test_init_failure_does_not_resume_when_lock_release_is_uncertain(
        monkeypatch):
    events = []

    class UnreleasableOwnership(FakeOwnership):
        def release(self):
            self.events.append(("release_failed", self.held))
            raise OSError("release uncertain")

    ownership = UnreleasableOwnership(events)
    service = FakeService(events, ownership=ownership)
    manager = LoRaManager(
        radio_ownership=ownership, service_handoff=service)
    monkeypatch.setattr(manager, "_init_radio", lambda: None)
    monkeypatch.setattr(manager, "_close_radio_resources", lambda _radio: True)

    assert manager._open_radio_session() is None
    assert ownership.held
    assert service.resume_calls == 0
    errors = [text for text, attr in drain(manager) if attr == "error"]
    assert len(errors) == 1
    assert "Failed to release" in errors[0]


def test_uncertain_spi_close_retains_lock_and_does_not_resume(monkeypatch):
    ownership = FakeOwnership()
    service = FakeService(ownership=ownership)
    manager = LoRaManager(
        radio_ownership=ownership, service_handoff=service)

    def failed_init():
        manager._resource_close_uncertain = True
        return None

    monkeypatch.setattr(manager, "_init_radio", failed_init)

    assert manager._open_radio_session() is None
    assert ownership.held
    assert service.resume_calls == 0
    errors = [text for text, attr in drain(manager) if attr == "error"]
    assert errors == [
        "Could not confirm SX1262 SPI close; ownership lock retained"]
    assert not manager.start_scanner()
    error, attr = manager.queue.get_nowait()
    assert attr == "error" and "restart WatchDogsGo" in error


@pytest.mark.parametrize("runner,args", [
    ("_run_sniffer", (868_100_000, 7, 5, 125_000, "test", 0, 0)),
    ("_run_scanner", ()),
    ("_run_tracker", ()),
])
def test_every_direct_worker_enters_shared_radio_session(
        monkeypatch, runner, args):
    manager = LoRaManager(
        radio_ownership=FakeOwnership(), service_handoff=FakeService())
    calls = []
    monkeypatch.setattr(
        manager, "_open_radio_session", lambda: calls.append(runner) or None)

    getattr(manager, runner)(*args)

    assert calls == [runner]


def test_running_worker_holds_lock_until_spi_close_and_normal_stop(
        monkeypatch):
    events = []
    ownership = FakeOwnership(events)
    service = FakeService(events, ownership=ownership)
    manager = LoRaManager(
        radio_ownership=ownership, service_handoff=service)
    entered_wait = threading.Event()

    class Radio:
        RX_SINGLE = 1

        def setFrequency(self, _freq): pass
        def setLoRaModulation(self, *_args): pass
        def request(self, _mode): return True

        def wait(self, _timeout):
            entered_wait.set()
            manager._stop_event.wait(1.0)

        def available(self): return 0

    radio = Radio()
    monkeypatch.setattr(manager, "_init_radio", lambda: radio)
    monkeypatch.setattr(
        manager, "_close_radio_resources",
        lambda value: events.append(
            ("spi_close", value, ownership.held)) or True)

    assert manager.start_sniffer(label="hold-test")
    assert entered_wait.wait(1.0)
    assert ownership.held
    assert manager.stop(timeout=1.0)

    assert not ownership.held
    assert service.resume_calls == 0
    labels = [event[0] for event in events]
    assert labels.index("spi_close") < labels.index("release")


def test_stop_timeout_keeps_worker_and_refuses_success():
    ownership = FakeOwnership()
    ownership.held = True
    manager = LoRaManager(radio_ownership=ownership)

    class StuckThread:
        def is_alive(self): return True
        def join(self, timeout): self.timeout = timeout

    stuck = StuckThread()
    manager._thread = stuck
    manager.running = True

    assert not manager.stop(timeout=0.01)
    assert manager._thread is stuck
    assert manager.running
    assert ownership.held


def test_meshtastic_handoff_runs_off_thread_and_resumes_after_stop():
    events = []
    stop_entered = threading.Event()
    allow_stop = threading.Event()

    class DirectRadio:
        radio_owned = True

        def stop(self):
            events.append("stop_begin")
            stop_entered.set()
            allow_stop.wait(1.0)
            self.radio_owned = False
            events.append("stop_done")
            return True

    class Service:
        def resume_service(self, timeout=15.0, *, connect=True):
            assert not game._lora.radio_owned
            events.append(("resume", timeout, connect))
            return True

    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._lora = DirectRadio()
    game._meshtastic = Service()
    game._lora_transition_queue = __import__("queue").Queue()
    game._lora_transition_thread = None
    game._lora_transition_thread_factory = threading.Thread

    started_at = time.monotonic()
    assert game._start_meshtastic_handoff()
    assert time.monotonic() - started_at < 0.25
    assert stop_entered.wait(1.0)
    assert not any(isinstance(event, tuple) for event in events)

    allow_stop.set()
    game._lora_transition_thread.join(timeout=2.0)
    assert not game._lora_transition_thread.is_alive()
    assert events == ["stop_begin", "stop_done", ("resume", 15.0, True)]
    assert game._lora_transition_queue.get_nowait() == (
        True, "Meshtastic service ready")


def test_meshtastic_handoff_never_resumes_when_direct_stop_fails():
    class DirectRadio:
        radio_owned = True

        def stop(self): return False

    class Service:
        def __init__(self): self.resume_calls = 0

        def resume_service(self, timeout=15.0, *, connect=True):
            self.resume_calls += 1
            return True

    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._lora = DirectRadio()
    game._meshtastic = Service()
    game._lora_transition_queue = __import__("queue").Queue()

    game._run_meshtastic_handoff()

    assert game._meshtastic.resume_calls == 0
    ok, detail = game._lora_transition_queue.get_nowait()
    assert not ok
    assert "did not release" in detail


def test_lora_power_off_is_refused_while_direct_worker_owns_radio(monkeypatch):
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._lora_enabled = True
    game._aio_available = True
    game._lora = NS(
        running=True, worker_active=True, radio_owned=True,
        stop=Mock(return_value=False),
    )
    game._meshtastic = NS(close=Mock())
    game.wardrive = NS(_wdg_owned_lora=True)
    game.msg = Mock()
    game._term_add = Mock()
    toggle = Mock(return_value=True)
    monkeypatch.setattr("watchdogs.app.AioManager.toggle", toggle)

    game._toggle_lora()

    game._lora.stop.assert_called_once_with()
    game._meshtastic.close.assert_not_called()
    toggle.assert_not_called()
    assert game._lora_enabled
    assert "power remains ON" in game.msg.call_args.args[0]
