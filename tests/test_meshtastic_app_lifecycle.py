"""App-level barriers around watch Bluetooth and the Meshtastic socket."""

import threading
import time
from queue import Queue
from types import SimpleNamespace as NS
from unittest.mock import Mock

from watchdogs.app import WatchDogsGame


def _cleanup_game(*, watch_close=True, host_close=True):
    game = WatchDogsGame.__new__(WatchDogsGame)
    game._watch_autoconnect_cancel = threading.Event()
    game._watch_autoconnect_thread = None
    game.wardrive = NS(
        on_stop=Mock(),
        host_ble=NS(close=Mock(return_value=host_close)),
        close_passive=Mock(),
    )
    game._watch = NS(
        connected=True,
        worker_active=True,
        pairing_lease_release_pending=False,
        scan_lease_release_pending=False,
        close=Mock(return_value=watch_close),
    )
    game._meshtastic = NS(close=Mock(return_value=True))
    game.serial = None
    game.gps = NS(close=Mock())
    game._lora_transition_thread = None
    game._lora = NS(running=False, worker_active=False)
    game._sdr = NS(running=False)
    game._plugins = []
    game.loot = None
    game._term_add = Mock()
    return game


def test_cleanup_preserves_control_socket_when_watch_cleanup_fails():
    game = _cleanup_game(watch_close=False)

    game._cleanup()

    game._watch.close.assert_called_once_with(timeout=7.0)
    game._meshtastic.close.assert_not_called()
    assert any(
        "Preserving the control socket" in call.args[0]
        for call in game._term_add.call_args_list)


def test_cleanup_preserves_control_socket_when_host_ble_cleanup_fails():
    game = _cleanup_game(host_close=False)

    game._cleanup()

    game.wardrive.host_ble.close.assert_called_once_with(timeout=7.0)
    game._meshtastic.close.assert_not_called()


def test_cleanup_waits_for_every_registered_radio_transition():
    """Shutdown cannot leave a displaced service mutator running."""
    game = _cleanup_game()
    release = threading.Event()
    entered = [threading.Event(), threading.Event()]

    def blocked(marker):
        marker.set()
        release.wait(1.0)

    transitions = [
        threading.Thread(target=blocked, args=(marker,), daemon=True)
        for marker in entered
    ]
    for transition in transitions:
        transition.start()
    assert all(marker.wait(1.0) for marker in entered)
    game._lora_transition_thread = transitions[-1]
    game._lora_transition_threads = list(transitions)

    releaser = threading.Timer(0.03, release.set)
    releaser.start()
    game._cleanup()
    releaser.join(timeout=1.0)

    assert all(not transition.is_alive() for transition in transitions)
    assert game._lora_transition_threads == []
    game._meshtastic.close.assert_called_once_with()


def test_cleanup_waits_for_protected_meshtastic_update(monkeypatch):
    """Exit cannot interrupt package validation or automatic rollback."""
    game = _cleanup_game()
    install_entered = threading.Event()
    release_install = threading.Event()

    def install_tag(_tag):
        install_entered.set()
        assert release_install.wait(2.0)
        return {"package_version": "2.8.1+wdg1"}

    monkeypatch.setattr(
        "watchdogs.meshtastic_updates.meshtastic_releases",
        lambda: [{"tag_name": "v2.8.1-wdg.1"}],
    )
    game._meshtastic_update_running = False
    game._meshtastic_update_result = Queue()
    game._meshtastic_update_thread = None
    game._meshtastic_update_thread_factory = threading.Thread
    game._meshtastic_transition_lock = threading.Lock()
    game._meshtastic_transition_state = ""
    game._lora_power_ownership_uncertain = False
    game._lora_enabled = True
    game._lora_start_epoch = 0
    game._lora_start_pending = ""
    game._lora_handoff_pending = None
    game._lora_transition_threads = []
    game._lora = NS(
        running=False, worker_active=False, radio_owned=False,
        stop=Mock(return_value=True), queue=Queue())
    game._watch.connected = False
    game._watch.worker_active = False
    game.wardrive.host_ble.worker_active = False
    game.wardrive._meshtastic_action_thread = None
    game._meshtastic = NS(
        service_restore_pending=False,
        ble_scan_lease_active=False,
        pairing_agent_lease_active=False,
        running=False,
        connected=False,
        active_backend="",
        backend_mode="fork_socket",
        active_service_target=Mock(return_value="wdg"),
        close=Mock(return_value=True),
        start=Mock(return_value=True),
        wait_connected=Mock(return_value=True),
        resume_service=Mock(return_value=True),
    )
    game._meshtastic_service = NS(
        require_current=Mock(),
        status=Mock(return_value=NS(package_version="2.8.0+wdg1")),
        install_tag=Mock(side_effect=install_tag),
        select=Mock(),
        rollback=Mock(),
    )
    game.msg = Mock()

    assert game._start_meshtastic_update()
    update_thread = game._meshtastic_update_thread
    assert update_thread is not None
    assert update_thread.daemon is False
    assert install_entered.wait(1.0)
    assert game._meshtastic.close.call_count == 1

    cleanup = threading.Thread(target=game._cleanup)
    cleanup.start()
    time.sleep(0.03)
    assert cleanup.is_alive()
    assert game._meshtastic.close.call_count == 1

    release_install.set()
    cleanup.join(timeout=2.0)
    update_thread.join(timeout=1.0)

    assert not cleanup.is_alive()
    assert not update_thread.is_alive()
    assert game._meshtastic_update_thread is None
    assert game._meshtastic.close.call_count == 2


def test_watch_autoconnect_rejection_keeps_target_and_backs_off():
    game = WatchDogsGame.__new__(WatchDogsGame)
    game._meshtastic_transition_lock = threading.Lock()
    game._meshtastic_transition_state = ""
    game._watch_autoconnect_cancel = threading.Event()
    game._watch_autoconnect_address = "AA:BB:CC:DD:EE:FF"
    game._watch_autoconnect_delay = 1.0
    game._watch_autoconnect_next = 0.0
    game._watch_autoconnect_last_detail = ""
    game._watch = NS(connect=Mock(return_value=False))

    before = time.monotonic()
    assert game._connect_watch_autoconnect(
        "AA:BB:CC:DD:EE:FF") is False

    assert game._watch_autoconnect_address == "AA:BB:CC:DD:EE:FF"
    assert game._watch_autoconnect_next >= before + 0.9
    assert game._watch_autoconnect_delay == 2.0


def test_watch_autoconnect_clears_target_only_after_worker_starts():
    game = WatchDogsGame.__new__(WatchDogsGame)
    game._meshtastic_transition_lock = threading.Lock()
    game._meshtastic_transition_state = ""
    game._watch_autoconnect_cancel = threading.Event()
    game._watch_autoconnect_address = "AA:BB:CC:DD:EE:FF"
    game._watch_autoconnect_delay = 4.0
    game._watch_autoconnect_last_detail = "waiting"
    game._watch = NS(connect=Mock(return_value=True))

    assert game._connect_watch_autoconnect(
        "AA:BB:CC:DD:EE:FF") is True

    assert game._watch_autoconnect_address == ""
    assert game._watch_autoconnect_delay == 1.0
    assert game._watch_autoconnect_last_detail == ""
