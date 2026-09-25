import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock, call

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
        self.restore_pending = False

    @property
    def service_restore_pending(self):
        return self.restore_pending

    def service_ready(self):
        self.events.append(("service_ready", self.ready))
        return self.ready

    def suspend_service(self, timeout=10.0):
        self.events.append(("suspend", timeout))
        if self.suspend_ok:
            self.ready = False
            self.restore_pending = True
        return self.suspend_ok

    def resume_service(self, timeout=15.0, *, connect=True):
        if self.ownership is not None:
            assert not self.ownership.held
        self.resume_calls += 1
        self.events.append(("resume", timeout, connect))
        if self.resume_ok:
            self.ready = True
            self.restore_pending = False
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


def test_direct_radio_start_guard_blocks_every_worker_before_thread_start():
    manager = LoRaManager(
        radio_ownership=FakeOwnership(),
        operation_guard=lambda: "Meshtastic update is running")

    assert not manager.start_meshcore()
    assert not manager.worker_active
    assert not manager.running
    assert drain(manager) == [
        ("Meshtastic update is running", "error")]


@pytest.mark.parametrize("failure_point", ["construct", "start"])
def test_direct_radio_thread_creation_failures_restore_idle_state(
        failure_point):
    class NeverStarts:
        def start(self):
            raise RuntimeError("start failed")

        def is_alive(self):
            return False

    def factory(**_kwargs):
        if failure_point == "construct":
            raise RuntimeError("construct failed")
        return NeverStarts()

    manager = LoRaManager(
        radio_ownership=FakeOwnership(), thread_factory=factory)

    assert not manager._start_worker("test", lambda: None)
    assert not manager.running
    assert not manager.worker_active
    assert manager._thread is None
    assert manager._stop_event.is_set()
    error, attr = manager.queue.get_nowait()
    assert attr == "error"
    assert failure_point in error


def test_direct_radio_stop_serializes_with_thread_construction_and_start():
    constructing = threading.Event()
    allow_construction = threading.Event()
    worker_ran = threading.Event()
    start_result = []
    stop_result = []

    def factory(**kwargs):
        constructing.set()
        allow_construction.wait(1.0)
        return threading.Thread(**kwargs)

    manager = LoRaManager(
        radio_ownership=FakeOwnership(), thread_factory=factory)

    def target():
        worker_ran.set()
        manager._stop_event.wait(1.0)

    starter = threading.Thread(
        target=lambda: start_result.append(
            manager._start_worker("test", target)))
    starter.start()
    assert constructing.wait(1.0)

    stopper = threading.Thread(
        target=lambda: stop_result.append(manager.stop(timeout=1.0)))
    stopper.start()
    # stop() must wait behind construction/start; it cannot return while an
    # assigned-but-unstarted worker is still able to begin later.
    time.sleep(0.02)
    assert stopper.is_alive()
    allow_construction.set()
    starter.join(1.0)
    stopper.join(1.0)

    assert start_result == [True]
    assert stop_result == [True]
    assert worker_ran.is_set()
    assert not manager.running
    assert not manager.worker_active


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
        "suspend", "acquire", "radio_init", "spi_close", "release",
    ]
    assert events[2][1] is True
    assert events[3][2] is True
    assert not ownership.held


def test_rejected_service_suspend_never_starts_or_guesses_a_restore_target(
        monkeypatch):
    ownership = FakeOwnership()

    class RejectedService:
        service_restore_pending = False

        def suspend_service(self, timeout=10.0):
            return False

        def resume_service(self, **_kwargs):
            raise AssertionError("no retained snapshot exists to restore")

        def service_ready(self):
            raise AssertionError("readiness must not authorize a restart")

    manager = LoRaManager(
        radio_ownership=ownership, service_handoff=RejectedService())
    monkeypatch.setattr(
        manager, "_init_radio",
        lambda: pytest.fail("SPI must not open after rejected suspension"))

    assert manager._open_radio_session() is None
    assert ownership.acquire_calls == 0
    assert not ownership.held


def test_rejected_service_suspend_retries_only_retained_exact_snapshot(
        monkeypatch):
    ownership = FakeOwnership()
    calls = []

    class PartialService:
        restore_pending = False

        @property
        def service_restore_pending(self):
            return self.restore_pending

        def suspend_service(self, timeout=10.0):
            calls.append(("suspend", timeout))
            self.restore_pending = True
            return False

        def resume_service(self, timeout=15.0, *, connect=True):
            calls.append(("resume", timeout, connect))
            self.restore_pending = False
            return True

    manager = LoRaManager(
        radio_ownership=ownership, service_handoff=PartialService())
    monkeypatch.setattr(
        manager, "_init_radio",
        lambda: pytest.fail("SPI must not open after rejected suspension"))

    assert manager._open_radio_session() is None
    assert calls == [("suspend", 10.0), ("resume", 15.0, True)]
    assert ownership.acquire_calls == 0


def test_direct_start_after_power_off_restores_then_resuspends_snapshot(
        monkeypatch):
    """A power-off token is normalized before a fresh direct SPI claim."""
    events = []
    ownership = FakeOwnership(events)
    service = FakeService(events, ownership=ownership)
    service.ready = False
    service.restore_pending = True
    manager = LoRaManager(
        radio_ownership=ownership, service_handoff=service)
    radio = object()
    monkeypatch.setattr(
        manager, "_init_radio",
        lambda: events.append(("radio_init", ownership.held)) or radio)

    assert manager._open_radio_session() is radio

    assert events == [
        ("resume", 15.0, False),
        ("suspend", 10.0),
        ("acquire", "WatchDogsGo direct radio"),
        ("radio_init", True),
    ]
    assert service.restore_pending
    assert manager._session_service_restore_pending
    assert ownership.held


def test_failed_power_off_snapshot_restore_never_opens_direct_spi(monkeypatch):
    events = []
    ownership = FakeOwnership(events)
    service = FakeService(
        events, ready=False, resume_ok=False, ownership=ownership)
    service.restore_pending = True
    manager = LoRaManager(
        radio_ownership=ownership, service_handoff=service)
    monkeypatch.setattr(
        manager, "_init_radio",
        lambda: pytest.fail("SPI must not open after failed exact restore"))

    assert manager._open_radio_session() is None

    assert events == [("resume", 15.0, False)]
    assert service.restore_pending
    assert ownership.acquire_calls == 0
    assert not ownership.held
    error, attr = manager.queue.get_nowait()
    assert attr == "error"
    assert "retained Meshtastic service state" in error


def test_successful_suspend_without_restore_token_fails_closed(monkeypatch):
    ownership = FakeOwnership()

    class BrokenService:
        service_restore_pending = False

        def suspend_service(self, timeout=10.0):
            return True

    manager = LoRaManager(
        radio_ownership=ownership, service_handoff=BrokenService())
    monkeypatch.setattr(
        manager, "_init_radio",
        lambda: pytest.fail("SPI must not open without a rollback token"))

    assert manager._open_radio_session() is None
    assert ownership.acquire_calls == 0
    error, attr = manager.queue.get_nowait()
    assert attr == "error"
    assert "exact restore snapshot" in error


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


def test_init_failure_restores_actual_unit_when_preference_is_stale(
        monkeypatch):
    events = []
    ownership = FakeOwnership(events)

    class MismatchedService:
        restore_pending = False

        @property
        def service_restore_pending(self):
            return self.restore_pending

        def suspend_service(self, timeout=10.0):
            events.append(("suspend", timeout))
            self.restore_pending = True
            return True

        def resume_service(self, timeout=15.0, *, connect=True, target=None):
            events.append(("resume", timeout, connect, target))
            self.restore_pending = False
            return True

    manager = LoRaManager(
        radio_ownership=ownership, service_handoff=MismatchedService())
    monkeypatch.setattr(manager, "_init_radio", lambda: None)
    monkeypatch.setattr(manager, "_close_radio_resources", lambda _radio: True)

    assert manager._open_radio_session() is None
    # Restoration consumes the manager's retained exact snapshot.  The direct
    # layer never guesses a daemon target from UI preference or readiness.
    assert ("resume", 15.0, True, None) in events
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


def test_lock_contention_emits_once_and_restores_service(monkeypatch):
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
    assert service.resume_calls == 1


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
            self.restore_pending = True
            manager._stop_event.set()
            return True

    service = CancellingService(events, ownership=ownership)
    manager._service_handoff = service
    monkeypatch.setattr(manager, "_init_radio", lambda: pytest.fail(
        "SPI must not be touched after stop is requested"))

    assert manager._open_radio_session() is None
    assert ownership.acquire_calls == 0
    assert not ownership.held
    assert service.resume_calls == 1


def test_stop_requested_after_lock_acquire_releases_then_restores_service(
        monkeypatch):
    events = []
    manager = None

    class CancellingOwnership(FakeOwnership):
        def acquire(self, owner):
            super().acquire(owner)
            manager._stop_event.set()

    ownership = CancellingOwnership(events)
    service = FakeService(events, ownership=ownership)
    manager = LoRaManager(
        radio_ownership=ownership, service_handoff=service)
    monkeypatch.setattr(manager, "_init_radio", lambda: pytest.fail(
        "SPI must not be touched after stop is requested"))

    assert manager._open_radio_session() is None
    assert not ownership.held
    assert service.resume_calls == 1
    labels = [event[0] for event in events]
    assert labels.index("release") < labels.index("resume")


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
        running = True
        worker_active = True
        mode = "meshcore"
        radio_owned = True

        def stop(self):
            events.append("stop_begin")
            stop_entered.set()
            allow_stop.wait(1.0)
            self.radio_owned = False
            events.append("stop_done")
            return True

    class Service:
        backend_mode = "fork_socket"

        def activate_backend_service(self, mode, timeout=15.0):
            assert not game._lora.radio_owned
            events.append(("select", mode, timeout))
            return True

        def start(self): return True
        def wait_connected(self, timeout=15.0): return True
        def commit_backend_service_activation(self):
            events.append("commit")

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
    assert events == [
        "stop_begin", "stop_done", ("select", "fork_socket", 15.0),
        "commit"]
    ok, detail, action, epoch = game._lora_transition_queue.get_nowait()
    assert (ok, detail, action) == (
        True, "Meshtastic service ready", "handoff")
    assert epoch == game._lora_start_epoch


def test_direct_session_restores_exact_snapshot_before_meshtastic_handoff():
    """A completed direct session must consume its retained service token."""
    events = []

    class DirectRadio:
        running = True
        worker_active = True
        radio_owned = True
        mode = "meshcore"

        def stop(self):
            events.append("direct_stop")
            self.running = self.worker_active = self.radio_owned = False
            return True

    class Service:
        backend_mode = "fork_socket"

        def __init__(self):
            # This is the exact snapshot retained by the successful direct
            # session's earlier suspend_service() call.
            self.restore_pending = True

        @property
        def service_restore_pending(self):
            return self.restore_pending

        def resume_service(self, timeout=15.0, *, connect=True):
            events.append(("resume_snapshot", timeout, connect))
            assert connect is False
            self.restore_pending = False
            return True

        def activate_backend_service(self, mode, timeout=15.0):
            assert not self.restore_pending
            events.append(("activate", mode, timeout))
            return True

        def start(self):
            events.append("client_start")
            return True

        def wait_connected(self, timeout=15.0):
            events.append(("connected", timeout))
            return True

        def commit_backend_service_activation(self):
            events.append("commit")

    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._lora = DirectRadio()
    game._meshtastic = Service()
    game._lora_transition_queue = __import__("queue").Queue()

    game._run_meshtastic_handoff()

    assert events == [
        "direct_stop",
        ("resume_snapshot", 15.0, False),
        ("activate", "fork_socket", 15.0),
        "client_start",
        ("connected", 15.0),
        "commit",
    ]
    ok, detail, action, _epoch = game._lora_transition_queue.get_nowait()
    assert (ok, detail, action) == (
        True, "Meshtastic service ready", "handoff")


def test_meshtastic_handoff_never_resumes_when_direct_stop_fails():
    class DirectRadio:
        running = True
        worker_active = True
        mode = "meshcore"
        radio_owned = True

        def stop(self): return False

    class Service:
        backend_mode = "fork_socket"

        def __init__(self): self.activate_calls = 0

        def activate_backend_service(self, mode, timeout=15.0):
            self.activate_calls += 1
            return True

    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._lora = DirectRadio()
    game._meshtastic = Service()
    game._lora_transition_queue = __import__("queue").Queue()

    game._run_meshtastic_handoff()

    assert game._meshtastic.activate_calls == 0
    ok, detail, action, _epoch = game._lora_transition_queue.get_nowait()
    assert not ok
    assert action == "handoff"
    assert "did not release" in detail


def test_meshtastic_negotiation_failure_rolls_back_and_restores_direct_radio():
    class DirectRadio:
        running = True
        worker_active = True
        radio_owned = True
        mode = "meshcore"

        def __init__(self):
            self.set_mc_channels = Mock()
            self.start_meshcore = Mock(side_effect=self._restart)

        def stop(self):
            self.running = self.worker_active = self.radio_owned = False
            return True

        def _restart(self, _region):
            self.running = self.worker_active = True
            return True

    service = NS(
        backend_mode="fork_socket",
        activate_backend_service=Mock(return_value=True),
        start=Mock(return_value=True),
        wait_connected=Mock(return_value=False),
        close=Mock(return_value=True),
        rollback_backend_service_activation=Mock(return_value=True))
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._lora = DirectRadio()
    game._meshtastic = service
    game._lora_transition_queue = __import__("queue").Queue()
    game._mc_channels_list = [NS(name="public")]
    game._mc_region = "us_ca_narrow"
    game.wardrive = NS(host_ble=NS(worker_active=False))
    game._watch = NS(worker_active=False)

    game._run_meshtastic_handoff()

    service.rollback_backend_service_activation.assert_called_once_with(
        timeout=15.0)
    game._lora.start_meshcore.assert_called_once_with("us_ca_narrow")
    ok, detail, action, _epoch = game._lora_transition_queue.get_nowait()
    assert not ok and action == "handoff"
    assert "protocol negotiation" in detail
    assert "previous direct radio restored" in detail


def test_meshtastic_handoff_close_failure_blocks_rollback_and_direct_restore():
    direct = NS(
        running=True, worker_active=True, radio_owned=True, mode="meshcore")

    def stop():
        direct.running = direct.worker_active = direct.radio_owned = False
        return True

    direct.stop = Mock(side_effect=stop)
    service = NS(
        backend_mode="fork_socket",
        activate_backend_service=Mock(return_value=True),
        start=Mock(return_value=True),
        wait_connected=Mock(return_value=False),
        close=Mock(return_value=False),
        rollback_backend_service_activation=Mock(return_value=True))
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._lora = direct
    game._meshtastic = service
    game._lora_transition_queue = __import__("queue").Queue()
    game._restore_lora_power_owner = Mock(return_value=True)

    game._run_meshtastic_handoff()

    service.close.assert_called_once_with()
    service.rollback_backend_service_activation.assert_not_called()
    game._restore_lora_power_owner.assert_not_called()
    ok, detail, action, _epoch = game._lora_transition_queue.get_nowait()
    assert not ok and action == "handoff"
    assert "rollback and direct-radio restore were blocked" in detail


def test_meshtastic_handoff_exception_after_activation_rolls_back_first():
    direct = NS(
        running=True, worker_active=True, radio_owned=True, mode="meshcore")

    def stop():
        direct.running = direct.worker_active = direct.radio_owned = False
        return True

    direct.stop = Mock(side_effect=stop)
    events = []
    service = NS(
        backend_mode="fork_socket",
        activate_backend_service=Mock(return_value=True),
        start=Mock(side_effect=RuntimeError("client boom")),
        close=Mock(side_effect=lambda: events.append("close") or True),
        rollback_backend_service_activation=Mock(
            side_effect=lambda timeout: events.append("rollback") or True))
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._lora = direct
    game._meshtastic = service
    game._lora_transition_queue = __import__("queue").Queue()
    game._restore_lora_power_owner = Mock(
        side_effect=lambda *_args: events.append("direct") or True)

    game._run_meshtastic_handoff()

    assert events == ["close", "rollback", "direct"]
    service.rollback_backend_service_activation.assert_called_once_with(
        timeout=15.0)


def test_meshtastic_handoff_is_blocked_by_host_ble_scan():
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._meshtastic_update_running = False
    game._lora_transition_thread = None
    game._lora = NS(stop=Mock(return_value=True))
    game._meshtastic = NS(
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        resume_service=Mock(return_value=True))
    game.wardrive = NS(host_ble=NS(worker_active=True))
    game._watch = NS(worker_active=False)
    game.msg = Mock()

    assert not game._start_meshtastic_handoff()
    game._lora.stop.assert_not_called()
    game._meshtastic.resume_service.assert_not_called()
    assert "host BLE scan" in game.msg.call_args.args[0]


def test_meshtastic_handoff_is_blocked_by_watch_pairing_lease():
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._meshtastic_update_running = False
    game._lora_transition_thread = None
    game._lora = NS(stop=Mock(return_value=True))
    game._meshtastic = NS(
        ble_scan_lease_active=False, pairing_agent_lease_active=True,
        resume_service=Mock(return_value=True))
    game.wardrive = NS(host_ble=NS(worker_active=False))
    game._watch = NS(worker_active=False)
    game.msg = Mock()

    assert not game._start_meshtastic_handoff()
    game._lora.stop.assert_not_called()
    game._meshtastic.resume_service.assert_not_called()
    assert "pairing lease" in game.msg.call_args.args[0]


def test_lora_power_stop_is_blocked_while_host_ble_worker_is_active(
        monkeypatch):
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._lora_enabled = True
    game._lora = NS(running=False, worker_active=False, radio_owned=False)
    game._meshtastic = NS(
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        suspend_service=Mock(return_value=True))
    game.wardrive = NS(host_ble=NS(worker_active=True))
    game._watch = NS(worker_active=False)
    game.msg = Mock()
    toggle = Mock(return_value=True)
    monkeypatch.setattr("watchdogs.app.AioManager.toggle", toggle)

    assert game._toggle_lora() is False

    game._meshtastic.suspend_service.assert_not_called()
    toggle.assert_not_called()
    assert "host BLE scan" in game.msg.call_args.args[0]


def test_watch_autoconnect_readiness_owns_transition_and_blocks_mutations():
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    entered = threading.Event()
    release = threading.Event()

    def readiness():
        entered.set()
        release.wait(2.0)
        return True

    game._meshtastic_transition_lock = threading.Lock()
    game._meshtastic_transition_state = ""
    game._watch_autoconnect_address = "AA:BB:CC:DD:EE:FF"
    game._watch_autoconnect_next = 0.0
    game._watch_autoconnect_result = __import__("queue").Queue(maxsize=1)
    game._watch_autoconnect_thread = None
    game._watch_autoconnect_cancel = threading.Event()
    game._watch_autoconnect_delay = 1.0
    game._watch_autoconnect_last_detail = ""
    game._watch = NS(
        connected=False, worker_active=False, connect=Mock())
    game._meshtastic = NS(
        connected=False, ble_coordination_ready=readiness,
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        resume_service=Mock(return_value=True))
    game._meshtastic_service = NS()
    game._meshtastic_update_running = False
    game._lora_enabled = True
    game._lora_transition_thread = None
    game._lora = NS(
        running=False, worker_active=False, radio_owned=False,
        stop=Mock(return_value=True))
    game.wardrive = NS(
        host_ble=NS(worker_active=False), _meshtastic_action_thread=None)
    game.msg = Mock()

    game._poll_watch_autoconnect()
    assert entered.wait(1.0)
    assert game._meshtastic_transition_busy()

    assert not game._start_meshtastic_update()
    assert game._toggle_lora() is False
    assert not game._start_meshtastic_handoff()
    game._meshtastic.resume_service.assert_not_called()

    release.set()
    game._watch_autoconnect_thread.join(timeout=2.0)
    assert not game._watch_autoconnect_thread.is_alive()
    assert not game._meshtastic_transition_busy()
    game._poll_watch_autoconnect()
    game._watch.connect.assert_called_once_with("AA:BB:CC:DD:EE:FF")


def test_lora_power_off_gpio_failure_restores_actual_service(monkeypatch):
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._aio_available = True
    game._lora = NS(
        running=False, worker_active=False, radio_owned=False, mode="",
        stop=Mock())
    game.wardrive = NS(host_ble=NS(worker_active=False))
    game._watch = NS(worker_active=False)
    game._meshtastic = NS(
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        active_service_target=Mock(return_value="stock"),
        suspend_service=Mock(return_value=True),
        resume_service=Mock(return_value=True))
    game._lora_transition_queue = __import__("queue").Queue()
    monkeypatch.setattr(
        "watchdogs.app.AioManager.toggle", Mock(return_value=False))

    game._run_lora_power_off()

    game._meshtastic.resume_service.assert_called_once_with(
        timeout=15.0, connect=True, target="stock")
    ok, detail, action, _power = game._lora_transition_queue.get_nowait()
    assert not ok and action == "power_off"
    assert "previous owner restored" in detail


def test_direct_session_power_off_reuses_retained_service_snapshot(monkeypatch):
    """Power-off must not overwrite a successful direct session's token."""
    events = []

    class DirectRadio:
        running = True
        worker_active = True
        radio_owned = True
        mode = "meshcore"

        def stop(self):
            events.append("direct_stop")
            self.running = self.worker_active = self.radio_owned = False
            return True

    class Service:
        service_restore_pending = True
        ble_scan_lease_active = False
        pairing_agent_lease_active = False

        def active_service_target(self):
            return None

        def suspend_service(self, timeout=10.0):
            raise AssertionError("retained snapshot must be reused")

    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._aio_available = True
    game._lora = DirectRadio()
    game._meshtastic = Service()
    game.wardrive = NS(host_ble=NS(worker_active=False))
    game._watch = NS(worker_active=False)
    game._lora_transition_queue = __import__("queue").Queue()
    barrier = FakeOwnership()
    game._lora_power_barrier_factory = lambda: barrier

    def cut_rail(_device, enabled):
        assert enabled is False
        assert barrier.held
        events.append("rail_cut")
        return True

    monkeypatch.setattr("watchdogs.app.AioManager.toggle", cut_rail)

    game._run_lora_power_off()

    assert events == ["direct_stop", "rail_cut"]
    assert not barrier.held
    assert game._meshtastic.service_restore_pending
    ok, detail, action, power = game._lora_transition_queue.get_nowait()
    assert (ok, detail, action, power) == (
        True, "LoRa disabled, mesh clients stopped", "power_off",
        {"rail_off": True})


def test_direct_power_off_failure_restores_snapshot_then_direct_owner(
        monkeypatch):
    """A failed rail cut rebuilds the exact pre-cut direct ownership state."""
    events = []

    class Service:
        ble_scan_lease_active = False
        pairing_agent_lease_active = False

        def __init__(self):
            self.restore_pending = True

        @property
        def service_restore_pending(self):
            return self.restore_pending

        def active_service_target(self):
            return None

        def suspend_service(self, timeout=10.0):
            raise AssertionError("retained snapshot must be reused")

        def resume_service(self, timeout=15.0, *, connect=True):
            events.append(("resume_snapshot", timeout, connect))
            assert connect is False
            self.restore_pending = False
            return True

    service = Service()

    class DirectRadio:
        running = True
        worker_active = True
        radio_owned = True
        mode = "meshcore"

        def __init__(self):
            self.set_mc_channels = Mock()

        def stop(self):
            events.append("direct_stop")
            self.running = self.worker_active = self.radio_owned = False
            return True

        def start_meshcore(self, region):
            assert not service.restore_pending
            events.append(("direct_restart", region))
            # The real LoRaManager suspends the just-restored snapshot before
            # taking SPI again.  Model that retained token here.
            service.restore_pending = True
            self.running = self.worker_active = self.radio_owned = True
            return True

    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._aio_available = True
    game._lora_start_epoch = 0
    game._lora_start_pending = ""
    game._mc_channels_list = [NS(name="public")]
    game._mc_region = "us_ca_narrow"
    game._lora = DirectRadio()
    game._meshtastic = service
    game.wardrive = NS(
        host_ble=NS(worker_active=False), _wdg_owned_lora=True)
    game._watch = NS(worker_active=False)
    game._lora_transition_queue = __import__("queue").Queue()
    game._lora_power_barrier_factory = FakeOwnership
    monkeypatch.setattr(
        "watchdogs.app.AioManager.toggle", Mock(return_value=False))

    game._run_lora_power_off()

    assert events == [
        "direct_stop",
        ("resume_snapshot", 15.0, False),
        ("direct_restart", "us_ca_narrow"),
    ]
    assert service.restore_pending
    assert game.wardrive._wdg_owned_lora is False
    ok, detail, action, power = game._lora_transition_queue.get_nowait()
    assert not ok and action == "power_off"
    assert power == {"rail_off": False}
    assert "previous owner restored" in detail


def test_lora_power_off_partial_service_stop_restores_prior_unit():
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._aio_available = True
    game._lora = NS(
        running=False, worker_active=False, radio_owned=False, mode="",
        stop=Mock())
    game.wardrive = NS(host_ble=NS(worker_active=False))
    game._watch = NS(worker_active=False)
    targets = iter(("wdg", None))
    game._meshtastic = NS(
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        active_service_target=Mock(side_effect=lambda: next(targets)),
        suspend_service=Mock(return_value=False),
        resume_service=Mock(return_value=True))
    game._lora_transition_queue = __import__("queue").Queue()

    game._run_lora_power_off()

    game._meshtastic.resume_service.assert_called_once_with(
        timeout=15.0, connect=True, target="wdg")
    ok, detail, action, _power = game._lora_transition_queue.get_nowait()
    assert not ok and action == "power_off"
    assert "did not release" in detail


def test_lora_power_off_exception_restores_prior_unit():
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._aio_available = True
    game._lora = NS(
        running=False, worker_active=False, radio_owned=False, mode="",
        stop=Mock())
    game.wardrive = NS(host_ble=NS(worker_active=False))
    game._watch = NS(worker_active=False)
    game._meshtastic = NS(
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        active_service_target=Mock(return_value="stock"),
        suspend_service=Mock(side_effect=RuntimeError("stop failed midway")),
        resume_service=Mock(return_value=True))
    game._lora_transition_queue = __import__("queue").Queue()

    game._run_lora_power_off()

    game._meshtastic.resume_service.assert_called_once_with(
        timeout=15.0, connect=True, target="stock")
    ok, detail, action, _power = game._lora_transition_queue.get_nowait()
    assert not ok and action == "power_off"
    assert "previous owner restored" in detail


def test_lora_power_off_is_refused_while_direct_worker_owns_radio(monkeypatch):
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._lora_enabled = True
    game._aio_available = True
    game._lora = NS(
        running=True, worker_active=True, radio_owned=True, mode="meshcore",
        stop=Mock(return_value=False),
    )
    game._meshtastic = NS(suspend_service=Mock(return_value=True))
    game.wardrive = NS(_wdg_owned_lora=True)
    game._lora_transition_queue = __import__("queue").Queue()
    game._lora_transition_thread = None
    game._lora_transition_thread_factory = threading.Thread
    game.msg = Mock()
    game._term_add = Mock()
    toggle = Mock(return_value=True)
    monkeypatch.setattr("watchdogs.app.AioManager.toggle", toggle)

    game._toggle_lora()
    game._lora_transition_thread.join(timeout=2.0)

    game._lora.stop.assert_called_once_with()
    game._meshtastic.suspend_service.assert_not_called()
    toggle.assert_not_called()
    assert game._lora_enabled
    ok, detail, action, _power = game._lora_transition_queue.get_nowait()
    assert not ok and action == "power_off"
    assert "did not release" in detail
    game._end_meshtastic_transition()


def test_lora_power_off_busy_barrier_leaves_rail_on_and_restores_owner(
        monkeypatch):
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._aio_available = True
    game._lora = NS(
        running=False, worker_active=False, radio_owned=False, mode="",
        stop=Mock(return_value=True))
    game.wardrive = NS(host_ble=NS(worker_active=False))
    game._watch = NS(worker_active=False)
    game._meshtastic = NS(
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        active_service_target=Mock(return_value="stock"),
        suspend_service=Mock(return_value=True),
        resume_service=Mock(return_value=True))
    game._lora_transition_queue = __import__("queue").Queue()
    game._lora_power_barrier_factory = lambda: FakeOwnership(busy=True)
    toggle = Mock(return_value=True)
    monkeypatch.setattr("watchdogs.app.AioManager.toggle", toggle)

    game._run_lora_power_off()

    toggle.assert_not_called()
    game._meshtastic.resume_service.assert_called_once_with(
        timeout=15.0, connect=True, target="stock")
    ok, detail, action, _power = game._lora_transition_queue.get_nowait()
    assert not ok and action == "power_off"
    assert "rail remains on" in detail
    assert "previous owner restored" in detail


def test_lora_power_off_holds_barrier_through_gpio_cut(monkeypatch):
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._aio_available = True
    game._lora = NS(
        running=False, worker_active=False, radio_owned=False, mode="",
        stop=Mock(return_value=True))
    game.wardrive = NS(host_ble=NS(worker_active=False))
    game._watch = NS(worker_active=False)
    game._meshtastic = NS(
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        active_service_target=Mock(return_value=None),
        suspend_service=Mock(return_value=True))
    game._lora_transition_queue = __import__("queue").Queue()
    barrier = FakeOwnership()
    game._lora_power_barrier_factory = lambda: barrier

    def cut(_device, enabled):
        assert enabled is False
        assert barrier.held
        return True

    monkeypatch.setattr("watchdogs.app.AioManager.toggle", cut)

    game._run_lora_power_off()

    assert not barrier.held
    assert [event[0] for event in barrier.events] == ["acquire", "release"]
    assert game._lora_transition_queue.get_nowait()[0] is True


def test_lora_power_off_release_failure_blocks_every_new_owner(monkeypatch):
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._aio_available = True
    game._lora_enabled = True
    game._lora_power_intent = True
    game._lora_power_on_pending = False
    game._lora_start_pending = ""
    game._lora_handoff_pending = None
    game._lora_start_epoch = 0
    game._lora_transition_thread = None
    game._lora_power_ownership_uncertain = False
    game._lora_power_uncertain_barrier = None
    game._lora = NS(
        running=False, worker_active=False, radio_owned=False, mode="",
        stop=Mock(return_value=True), queue=__import__("queue").Queue())
    game.wardrive = NS(host_ble=NS(worker_active=False))
    game._watch = NS(worker_active=False)
    game._meshtastic = NS(
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        active_service_target=Mock(return_value=None),
        suspend_service=Mock(return_value=True))
    game._lora_transition_queue = __import__("queue").Queue()
    game._term_add = Mock()
    game.msg = Mock()
    game._mc_event_queue = __import__("queue").Queue()
    game._mc_bubbles = []
    game._mc_screen = False

    class UncertainBarrier(FakeOwnership):
        def release(self):
            self.events.append(("release_failed", self.held))
            raise OSError("unlock uncertain")

    barrier = UncertainBarrier()
    game._lora_power_barrier_factory = lambda: barrier
    monkeypatch.setattr(
        "watchdogs.app.AioManager.toggle", Mock(return_value=True))

    game._run_lora_power_off()

    ok, detail, action, power = game._lora_transition_queue.get_nowait()
    assert not ok and action == "power_off"
    assert power == {"rail_off": True}
    assert "ownership is uncertain" in detail
    assert game._lora_power_ownership_uncertain
    assert game._lora_power_uncertain_barrier is barrier
    game._lora_transition_queue.put((ok, detail, action, power))
    game._poll_lora()
    assert game._lora_enabled is False
    assert game._lora_power_intent is False
    assert game.wardrive._wdg_owned_lora is False
    assert game._toggle_lora() is False
    assert "restart WatchDogsGo" in game.msg.call_args.args[0]


def test_meshcore_power_on_claims_ownership_only_after_ready_event(monkeypatch):
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._aio_available = True
    game._lora_enabled = False
    game._meshtastic_update_running = False
    game._meshtastic_transition_lock = threading.Lock()
    game._meshtastic_transition_state = ""
    game._lora_transition_queue = __import__("queue").Queue()
    game._lora_transition_thread = None
    game._lora_power_on_pending = False
    game._lora_start_pending = ""
    game._mc_channels_list = [NS(name="public")]
    game._mc_region = "us_ca_narrow"
    game._mc_node_name = "WDG"
    game.player_lat = game.player_lon = 0.0
    game.gps_fix = False
    game._mc_event_queue = __import__("queue").Queue()
    game._mc_bubbles = []
    game._mc_screen = False
    game.msg = Mock()
    game._term_add = Mock()
    game._watch = NS(worker_active=False)
    game._meshtastic = NS(
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        close=Mock(return_value=True))
    lora = NS(
        running=False, worker_active=False, radio_owned=False, mode="",
        queue=__import__("queue").Queue(), set_mc_channels=Mock(),
        send_meshcore_advert=Mock())

    def start(_region):
        lora.running = True
        lora.mode = "meshcore"
        return True

    lora.start_meshcore = Mock(side_effect=start)
    game._lora = lora
    game.wardrive = NS(
        settings={"wardrive_lora": True, "lora_protocol": "meshcore"},
        host_ble=NS(worker_active=False), _wdg_owned_lora=False)
    toggle = Mock(return_value=True)
    monkeypatch.setattr("watchdogs.app.AioManager.toggle", toggle)

    assert game._toggle_lora()
    assert game._lora_enabled
    assert game._lora_power_on_pending
    assert game._meshtastic_transition_busy()
    assert not game.wardrive._wdg_owned_lora

    lora.queue.put(("Sniffer started: test", "success"))
    game._poll_lora()

    assert game.wardrive._wdg_owned_lora
    assert not game._lora_power_on_pending
    assert not game._meshtastic_transition_busy()
    toggle.assert_called_once_with("lora", True)


def test_cancelled_meshcore_start_completion_releases_radio_without_claiming():
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._lora_start_epoch = 4
    game._lora_start_pending = ("wardrive", 4)
    game._lora_handoff_pending = None
    game._lora_transition_thread = None
    game._lora_power_intent = True
    game._lora_power_on_pending = False
    game._lora_transition_queue = __import__("queue").Queue()
    game._mc_event_queue = __import__("queue").Queue()
    game._mc_bubbles = []
    game._mc_screen = False
    game.msg = Mock()
    game._term_add = Mock()
    lora = NS(
        queue=__import__("queue").Queue(), stop=Mock(return_value=True))
    game._lora = lora
    game.wardrive = NS(
        settings={"wardrive_lora": False}, _wdg_owned_lora=False)

    assert game._cancel_pending_lora_start(collector_only=True)
    lora.queue.put(("Sniffer started: meshcore", "success"))
    game._poll_lora()

    lora.stop.assert_called_once_with()
    assert game.wardrive._wdg_owned_lora is False
    assert game._lora_start_pending == ""


def test_disabling_collector_cancels_power_on_and_releases_transition():
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._lora_start_epoch = 11
    game._lora_start_pending = ("power_on", 11)
    game._lora_handoff_pending = None
    game._lora_transition_thread = None
    game._lora_power_intent = True
    game._lora_power_on_pending = True
    game._meshtastic_transition_lock = threading.Lock()
    game._meshtastic_transition_state = ""
    assert game._begin_meshtastic_transition("lora_power")
    game._lora_transition_queue = __import__("queue").Queue()
    game._mc_event_queue = __import__("queue").Queue()
    game._mc_bubbles = []
    game._mc_screen = False
    game.msg = Mock()
    game._term_add = Mock()
    lora = NS(
        queue=__import__("queue").Queue(), stop=Mock(return_value=True))
    game._lora = lora
    game.wardrive = NS(
        settings={"wardrive_lora": False}, _wdg_owned_lora=False)

    assert game._cancel_pending_lora_start(collector_only=True)
    lora.queue.put(("Sniffer started: meshcore", "success"))
    game._poll_lora()

    lora.stop.assert_called_once_with()
    assert game._lora_power_on_pending is False
    assert not game._meshtastic_transition_busy()
    assert game.wardrive._wdg_owned_lora is False


def test_cancelled_meshtastic_completion_closes_client_without_claiming():
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._lora_start_epoch = 8
    game._lora_start_pending = ""
    game._lora_handoff_pending = ("wardrive", 8)
    game._lora_transition_thread = None
    game._lora_power_intent = True
    game._lora_power_on_pending = False
    game._lora_transition_queue = __import__("queue").Queue()
    game._lora_transition_queue.put(
        (True, "Meshtastic service ready", "wardrive", 8))
    game._mc_event_queue = __import__("queue").Queue()
    game._mc_bubbles = []
    game._mc_screen = False
    game.msg = Mock()
    game._term_add = Mock()
    game._meshtastic = NS(close=Mock(return_value=True))
    game._lora = NS(queue=__import__("queue").Queue())
    game.wardrive = NS(
        settings={"wardrive_lora": False}, _wdg_owned_lora=False)

    assert game._cancel_pending_lora_start(collector_only=True)
    game._poll_lora()

    game._meshtastic.close.assert_called_once_with()
    assert game.wardrive._wdg_owned_lora is False
    assert game._lora_handoff_pending is None


def test_lora_power_off_invalidates_and_waits_for_pending_start():
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._lora_enabled = True
    game._lora_power_intent = True
    game._lora_start_epoch = 2
    game._lora_start_pending = ("power_on", 2)
    game._lora_handoff_pending = None
    game._meshtastic_transition_lock = threading.Lock()
    game._meshtastic_transition_state = ""
    old_thread = NS(is_alive=Mock(return_value=True))
    game._lora_transition_thread = old_thread
    created = {}

    def factory(**kwargs):
        created.update(kwargs)
        return NS(start=Mock())

    game._lora_transition_thread_factory = factory
    game._meshtastic = NS(
        ble_scan_lease_active=False, pairing_agent_lease_active=False)
    game._watch = NS(worker_active=False)
    game.wardrive = NS(host_ble=NS(worker_active=False))
    game.msg = Mock()

    assert game._toggle_lora()

    assert game._lora_start_epoch == 3
    assert game._lora_power_intent is False
    assert created["target"] == game._run_lora_power_off
    assert created["args"] == ((old_thread,),)
    game._lora_transition_thread.start.assert_called_once_with()


def test_power_off_keeps_barrier_until_displaced_handoff_exits(monkeypatch):
    """A helper overrun cannot become an untracked service mutator."""
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    release_handoff = threading.Event()
    handoff_started = threading.Event()
    game._lora_transition_queue = __import__("queue").Queue()
    game._lora_start_epoch = 2
    game._lora_start_pending = ""
    game._lora_handoff_pending = ("handoff", 2)

    def blocked_handoff():
        handoff_started.set()
        release_handoff.wait(2.0)
        # A failed handoff may restore its previous direct owner immediately
        # before it reports completion.  Power-off must re-sample after join.
        game._lora.running = True
        game._lora.mode = "meshcore"
        game._lora_transition_queue.put(
            (False, "stale handoff restored direct radio", "handoff", 2))

    old_thread = threading.Thread(target=blocked_handoff, daemon=True)
    old_thread.start()
    assert handoff_started.wait(1.0)

    game._lora_transition_thread = old_thread
    game._lora_transition_threads = [old_thread]
    game._lora_transition_thread_factory = threading.Thread
    game._meshtastic_transition_lock = threading.Lock()
    game._meshtastic_transition_state = ""
    game._meshtastic_update_running = False
    game._lora_power_ownership_uncertain = False
    game._lora_enabled = True
    game._lora_power_intent = True
    game._lora_power_on_pending = False
    game._aio_available = True
    game._lora = NS(
        running=False, worker_active=False, radio_owned=False, mode="",
        stop=Mock(return_value=True), queue=__import__("queue").Queue())
    game._meshtastic = NS(
        service_restore_pending=False,
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        active_service_target=Mock(return_value=None),
        suspend_service=Mock(return_value=True),
        close=Mock(return_value=True))
    game._meshtastic_service = NS()
    game._watch = NS(worker_active=False)
    game.wardrive = NS(
        settings={"wardrive_lora": False},
        host_ble=NS(worker_active=False), _meshtastic_action_thread=None,
        _wdg_owned_lora=False)
    game._lora_power_barrier_factory = FakeOwnership
    game._mc_event_queue = __import__("queue").Queue()
    game._mc_bubbles = []
    game._mc_screen = False
    game.msg = Mock()
    game._term_add = Mock()
    toggle = Mock(return_value=True)
    monkeypatch.setattr("watchdogs.app.AioManager.toggle", toggle)

    assert game._toggle_lora()
    poweroff = game._lora_transition_thread
    assert poweroff is not old_thread
    time.sleep(0.03)
    assert poweroff.is_alive()
    assert game._meshtastic_transition_busy()
    toggle.assert_not_called()

    # Every competing owner-changing path remains behind the same barrier.
    assert game._toggle_lora() is False
    assert game._start_meshtastic_update() is False
    assert game._lora_transition_thread is poweroff

    release_handoff.set()
    old_thread.join(timeout=1.0)
    poweroff.join(timeout=2.0)
    assert not old_thread.is_alive()
    assert not poweroff.is_alive()
    toggle.assert_called_once_with("lora", False)
    game._lora.stop.assert_called_once_with()

    # The stale handoff completion is drained while power-off still owns the
    # transition; it cannot claim the radio or close a newer client.
    game._poll_lora()
    game._meshtastic.close.assert_not_called()
    assert game.wardrive._wdg_owned_lora is False
    assert not game._meshtastic_transition_busy()
    assert not game._lora_transition_active()


def test_rejected_power_on_uses_safe_power_off_worker(monkeypatch):
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._aio_available = True
    game._lora_enabled = False
    game._meshtastic_update_running = False
    game._meshtastic_transition_lock = threading.Lock()
    game._meshtastic_transition_state = ""
    game._lora_transition_queue = __import__("queue").Queue()
    game._lora_transition_thread = None
    game._lora_transition_thread_factory = threading.Thread
    game._lora_power_on_pending = False
    game._lora_start_pending = ""
    game._mc_channels_list = []
    game._mc_region = "us_ca_narrow"
    game.player_lat = game.player_lon = 0.0
    game.gps_fix = False
    game.msg = Mock()
    game._term_add = Mock()
    game._watch = NS(worker_active=False)
    game._meshtastic = NS(
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        active_service_target=Mock(return_value=None),
        suspend_service=Mock(return_value=True), close=Mock(return_value=True))
    game._lora = NS(
        running=False, worker_active=False, radio_owned=False, mode="",
        set_mc_channels=Mock(), start_meshcore=Mock(return_value=False),
        stop=Mock(return_value=True))
    game.wardrive = NS(
        settings={"wardrive_lora": True, "lora_protocol": "meshcore"},
        host_ble=NS(worker_active=False), _wdg_owned_lora=False)
    game._lora_power_barrier_factory = lambda: FakeOwnership()
    toggle = Mock(return_value=True)
    monkeypatch.setattr("watchdogs.app.AioManager.toggle", toggle)

    assert not game._toggle_lora()
    game._lora_transition_thread.join(timeout=2.0)
    assert not game._lora_transition_thread.is_alive()
    ok, _detail, action, _power = game._lora_transition_queue.get_nowait()
    assert ok and action == "power_off"
    assert toggle.call_args_list == [
        call("lora", True),
        call("lora", False),
    ]


def test_locked_protocol_switch_returns_explicit_result_on_every_branch():
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game.msg = Mock()
    game._term_add = Mock()
    game._meshtastic_update_running = False
    game._lora_enabled = False
    game._lora_transition_thread = None
    game._lora = NS(running=False, mode="", stop=Mock(return_value=True))
    game._meshtastic = NS(close=Mock(return_value=True))

    assert game._switch_lora_protocol_locked(
        "meshcore", start_if_enabled=True) is True
    assert game._switch_lora_protocol_locked(
        "meshtastic", start_if_enabled=True) is True
    assert game._switch_lora_protocol_locked(
        "meshcore", start_if_enabled=False) is True
    assert game._switch_lora_protocol_locked(
        "invalid", start_if_enabled=True) is False

    game._meshtastic_update_running = True
    assert game._switch_lora_protocol_locked(
        "meshcore", start_if_enabled=True) is False


def test_cleanup_releases_bluetooth_workers_before_meshtastic_socket():
    events = []
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game.wardrive = NS(
        on_stop=lambda: events.append("wardrive_stop"),
        host_ble=NS(close=lambda timeout: events.append(
            ("host_close", timeout)) or True),
        close_passive=lambda: events.append("passive_close"))
    game._watch = NS(
        connected=True, worker_active=True,
        close=lambda timeout: events.append(("watch_close", timeout)) or True)
    game._meshtastic = NS(close=lambda: events.append("manager_close") or True)
    game.serial = None
    game.gps = NS(close=lambda: events.append("gps_close"))
    game._lora_transition_thread = None
    game._lora = NS(running=False, worker_active=False)
    game._sdr = NS(running=False)
    game._plugins = []
    game.loot = None
    game._term_add = Mock()

    game._cleanup()

    assert events.index(("host_close", 7.0)) < events.index("manager_close")
    assert events.index(("watch_close", 7.0)) < events.index("manager_close")


def test_cleanup_joins_watch_readiness_before_meshtastic_socket():
    events = []
    game_cls = watchdogs_game_cls()
    game = game_cls.__new__(game_cls)
    game._watch_autoconnect_cancel = threading.Event()

    def readiness_worker():
        game._watch_autoconnect_cancel.wait(2.0)
        events.append("watch_readiness_stopped")

    game._watch_autoconnect_thread = threading.Thread(
        target=readiness_worker, daemon=True)
    game._watch_autoconnect_thread.start()
    game._watch = NS(
        connected=False, worker_active=False,
        pairing_lease_release_pending=True,
        close=lambda timeout: events.append(("watch_close", timeout)) or True)
    game._meshtastic = NS(close=lambda: events.append("manager_close") or True)
    game.serial = None
    game.gps = NS(close=lambda: None)
    game._lora_transition_thread = None
    game._lora = NS(running=False, worker_active=False)
    game._sdr = NS(running=False)
    game._plugins = []
    game.loot = None
    game._term_add = Mock()

    game._cleanup()

    assert events == [
        "watch_readiness_stopped", ("watch_close", 7.0), "manager_close"]
