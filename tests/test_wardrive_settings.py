import json
import time
from queue import Queue
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from watchdogs.wardrive_settings import (
    DEFAULTS,
    load_settings,
    normalize_settings,
    save_settings,
)
from watchdogs.wardrive_ui import (
    COLLECTOR_SETTINGS,
    LORA_SETTINGS,
    MAIN_SETTINGS,
    WardriveUI,
)


def test_legacy_settings_keep_existing_values_and_enable_lte_by_default(tmp_path):
    (tmp_path / "wardrive_settings.json").write_text(
        json.dumps({"trail": True, "cell_tracking": False, "lte_modem": "no"}))

    settings = load_settings(tmp_path)

    assert settings["trail"] is True
    assert settings["cell_tracking"] is False
    assert settings["lte_modem"] is True
    assert settings["trail_mode"] == "solid"
    assert settings["dot_wifi_mode"] == "recent"
    assert settings["dot_ble_mode"] == "recent"
    assert set(settings) == set(DEFAULTS)


def test_settings_round_trip_lte_off_atomically(tmp_path):
    settings = dict(DEFAULTS, lte_modem=False, cell_tracking=True)
    save_settings(tmp_path, settings)

    assert not (tmp_path / "wardrive_settings.tmp").exists()
    assert load_settings(tmp_path)["lte_modem"] is False
    stored = json.loads((tmp_path / "wardrive_settings.json").read_text())
    assert stored["lte_modem"] is False
    assert set(stored) == set(DEFAULTS)


def test_legacy_disabled_network_dots_migrate_only_generic_layers_off(tmp_path):
    (tmp_path / "wardrive_settings.json").write_text(
        json.dumps({"network_dots": False}))

    settings = load_settings(tmp_path)

    assert settings["dot_wifi_mode"] == "off"
    assert settings["dot_ble_mode"] == "off"
    assert settings["dot_cell_mode"] == "recent"
    assert settings["dot_flock_mode"] == "keep"
    assert settings["dot_axon_mode"] == "keep"
    assert settings["network_dots"] is False


def test_legacy_enabled_network_dots_preserve_persistent_generic_layers(tmp_path):
    (tmp_path / "wardrive_settings.json").write_text(
        json.dumps({"network_dots": True}))

    settings = load_settings(tmp_path)

    assert settings["dot_wifi_mode"] == "keep"
    assert settings["dot_ble_mode"] == "keep"
    assert settings["dot_cell_mode"] == "keep"
    assert settings["network_dots"] is True


def test_invalid_display_values_fall_back_and_round_trip(tmp_path):
    settings = normalize_settings({
        "dot_wifi_mode": "keep",
        "dot_ble_mode": "bogus",
        "dot_fade_seconds": 17,
        "trail_mode": "heat",
    })
    assert settings["dot_wifi_mode"] == "keep"
    assert settings["dot_ble_mode"] == "recent"
    assert settings["dot_fade_seconds"] == 30
    assert settings["trail_mode"] == "heat"
    assert settings["trail"] is True

    save_settings(tmp_path, settings)
    assert load_settings(tmp_path) == settings


def test_collector_defaults_and_invalid_sdr_pair_are_exclusive():
    defaults = normalize_settings({})
    assert defaults["wardrive_lora"] is True
    assert defaults["lora_protocol"] == "meshcore"
    assert defaults["wardrive_adsb"] is True
    assert defaults["wardrive_433"] is False

    conflicted = normalize_settings({
        "wardrive_adsb": True, "wardrive_433": True})
    assert conflicted["wardrive_adsb"] is True
    assert conflicted["wardrive_433"] is False
    assert normalize_settings({"lora_protocol": "invalid"})[
        "lora_protocol"] == "meshcore"


def test_lora_settings_are_grouped_away_from_wardrive_capture_toggle():
    from watchdogs.app import MENU_CATS

    main_keys = tuple(key for key, _label in MAIN_SETTINGS)
    collector_keys = tuple(key for key, _label in COLLECTOR_SETTINGS)
    lora_keys = tuple(key for key, _label in LORA_SETTINGS)
    assert "_lora_settings" in main_keys
    assert "_meshtastic" not in main_keys
    assert collector_keys == (
        "wardrive_lora", "wardrive_adsb", "wardrive_433")
    assert lora_keys == (
        "lora_protocol", "_meshcore_region", "_meshtastic")

    sniff_labels = next(items for name, items in MENU_CATS if name == "SNIFF")
    addon_commands = next(items for name, items in MENU_CATS if name == "ADDONS")
    assert all(item[1] != "ESP Dual Test" for item in sniff_labels)
    assert all(item[2] != "_meshcore_region" for item in addon_commands)


def test_meshcore_region_picker_returns_to_lora_settings():
    from watchdogs.app import WatchDogsGame
    from watchdogs.lora_manager import MESHCORE_PRESETS

    game = WatchDogsGame.__new__(WatchDogsGame)
    ui = WardriveUI.__new__(WardriveUI)
    game.wardrive = ui
    ui.app = game
    ui.settings_open = True
    ui.settings_page = "lora"
    game._mc_region = next(iter(MESHCORE_PRESETS))
    game._mc_region_screen = False
    game._mc_region_return_to_settings = False

    ui.open_meshcore_region_picker()
    assert game._mc_region_screen
    assert game._mc_region_return_to_settings
    assert not ui.settings_open

    game._close_mc_region_picker()
    assert not game._mc_region_screen
    assert ui.settings_open
    assert ui.settings_page == "lora"


def test_meshtastic_backend_and_adapter_settings_are_normalized():
    defaults = normalize_settings({})
    assert defaults["meshtastic_backend"] == "auto"
    assert defaults["meshtastic_phone_ble_enabled"] is True
    assert defaults["meshtastic_phone_adapter"] == "auto"
    assert defaults["host_ble_adapter"] == "auto"

    settings = normalize_settings({
        "meshtastic_backend": "fork_socket",
        "meshtastic_phone_ble_enabled": False,
        "meshtastic_phone_adapter": "aa:bb:cc:dd:ee:ff",
        "host_ble_adapter": "11:22:33:44:55:66",
    })
    assert settings["meshtastic_backend"] == "fork_socket"
    assert settings["meshtastic_phone_ble_enabled"] is False
    assert settings["meshtastic_phone_adapter"] == "AA:BB:CC:DD:EE:FF"
    assert settings["host_ble_adapter"] == "11:22:33:44:55:66"

    invalid = normalize_settings({
        "meshtastic_backend": "tcp_then_socket",
        "meshtastic_phone_adapter": "hci0",
        "host_ble_adapter": 7,
    })
    assert invalid["meshtastic_backend"] == "auto"
    assert invalid["meshtastic_phone_adapter"] == "auto"
    assert invalid["host_ble_adapter"] == "auto"


def test_collector_toggles_switch_sdr_choice_and_release_owned_lora():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({})
    ui.persist_settings = Mock()
    ui._wdg_owned_lora = True
    ui._wdg_owned_sdr = True
    lora = NS(
        running=True, worker_active=True, radio_owned=True,
        mode="meshcore")

    def stop_lora():
        lora.running = False
        lora.worker_active = False
        lora.radio_owned = False
        return True

    lora.stop = Mock(side_effect=stop_lora)
    sdr = NS(running=True, mode="adsb", stop=Mock())
    ui.app = NS(_lora=lora, _sdr=sdr,
                _term_add=Mock())
    ui.scan = NS(state="idle", mode="")

    ui.toggle_setting("wardrive_433")
    assert ui.settings["wardrive_433"] is True
    assert ui.settings["wardrive_adsb"] is False
    sdr.stop.assert_called_once_with()
    assert ui._wdg_owned_sdr is False

    ui.toggle_setting("wardrive_adsb")
    assert ui.settings["wardrive_adsb"] is True
    assert ui.settings["wardrive_433"] is False

    ui.toggle_setting("wardrive_lora")
    assert ui.settings["wardrive_lora"] is False
    lora.stop.assert_called_once_with()
    assert ui._wdg_owned_lora is False
    assert ui.persist_settings.call_count == 3


@pytest.mark.parametrize("stop_result", [False, True])
def test_disabling_lora_collector_retains_owner_until_worker_and_lock_release(
        stop_result):
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({"wardrive_lora": True})
    ui._wdg_owned_lora = True
    lora = NS(
        running=False, worker_active=True, radio_owned=True,
        mode="meshcore", stop=Mock(return_value=stop_result))
    ui.app = NS(
        _lora=lora, _meshtastic=None, _term_add=Mock(), msg=Mock())
    ui.scan = NS(state="idle", mode="")

    ui.settings["wardrive_lora"] = False
    assert not ui._apply_collector_setting_change("wardrive_lora")

    assert ui._wdg_owned_lora
    assert any("stop incomplete" in call.args[0]
               for call in ui.app._term_add.call_args_list)
    assert "ownership retained" in ui.app.msg.call_args.args[0]


def test_disabling_lora_collector_honors_negative_stop_ack_after_worker_exit():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({"wardrive_lora": True})
    ui._wdg_owned_lora = True
    lora = NS(
        running=True, worker_active=True, radio_owned=True,
        mode="meshcore")

    def unconfirmed_stop():
        lora.running = False
        lora.worker_active = False
        lora.radio_owned = False
        return False

    lora.stop = Mock(side_effect=unconfirmed_stop)
    ui.app = NS(
        _lora=lora, _meshtastic=None, _term_add=Mock(), msg=Mock())
    ui.scan = NS(state="idle", mode="")

    ui.settings["wardrive_lora"] = False
    assert not ui._apply_collector_setting_change("wardrive_lora")
    assert ui._wdg_owned_lora
    assert "not confirmed" in ui.app._term_add.call_args.args[0]


@pytest.mark.parametrize("close_result", [False, True])
def test_disabling_lora_collector_retains_owner_until_client_close_barrier(
        close_result):
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({"wardrive_lora": True})
    ui._wdg_owned_lora = True
    manager = NS(
        running=True, connected=True, close=Mock(return_value=close_result))
    ui.app = NS(
        _lora=None, _meshtastic=manager, _lora_handoff_pending=None,
        _term_add=Mock(), msg=Mock())
    ui.scan = NS(state="idle", mode="")

    ui.settings["wardrive_lora"] = False
    assert not ui._apply_collector_setting_change("wardrive_lora")

    assert ui._wdg_owned_lora
    manager.close.assert_called_once_with()
    assert any("stop incomplete" in call.args[0]
               for call in ui.app._term_add.call_args_list)


def test_disabling_lora_collector_honors_negative_close_ack_after_worker_exit():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({"wardrive_lora": True})
    ui._wdg_owned_lora = True
    manager = NS(running=True, connected=True)

    def unconfirmed_close():
        manager.running = False
        manager.connected = False
        return False

    manager.close = Mock(side_effect=unconfirmed_close)
    ui.app = NS(
        _lora=None, _meshtastic=manager, _lora_handoff_pending=None,
        _term_add=Mock(), msg=Mock())
    ui.scan = NS(state="idle", mode="")

    ui.settings["wardrive_lora"] = False
    assert not ui._apply_collector_setting_change("wardrive_lora")
    assert ui._wdg_owned_lora
    assert "not confirmed" in ui.app._term_add.call_args.args[0]


def test_disabling_lora_collector_clears_owner_after_confirmed_client_close():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({"wardrive_lora": True})
    ui._wdg_owned_lora = True
    manager = NS(running=True, connected=True)

    def close_manager():
        manager.running = False
        manager.connected = False
        return True

    manager.close = Mock(side_effect=close_manager)
    ui.app = NS(
        _lora=None, _meshtastic=manager, _lora_handoff_pending=None,
        _term_add=Mock(), msg=Mock())
    ui.scan = NS(state="idle", mode="")

    ui.settings["wardrive_lora"] = False
    assert ui._apply_collector_setting_change("wardrive_lora")
    assert not ui._wdg_owned_lora


def test_disabling_lora_collector_keeps_pending_handoff_owned():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({"wardrive_lora": True})
    ui._wdg_owned_lora = False
    manager = NS(running=True, connected=False, close=Mock())
    ui.app = NS(
        _lora=None, _meshtastic=manager,
        _lora_handoff_pending=("wardrive", 4),
        _cancel_pending_lora_start=Mock(return_value=True),
        _lora_start_pending="", _term_add=Mock(), msg=Mock())
    ui.scan = NS(state="idle", mode="")

    ui.settings["wardrive_lora"] = False
    assert not ui._apply_collector_setting_change("wardrive_lora")

    assert ui._wdg_owned_lora
    manager.close.assert_not_called()
    assert "stop pending" in ui.app.msg.call_args.args[0]


def test_lora_protocol_switch_is_persisted_and_applied():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({})
    ui.persist_settings = Mock()
    ui._wdg_owned_lora = True
    ui.app = NS(_lora_enabled=True, _switch_lora_protocol=Mock())

    ui.cycle_lora_protocol()

    assert ui.settings["lora_protocol"] == "meshtastic"
    ui.app._switch_lora_protocol.assert_called_once_with(
        "meshtastic", start_if_enabled=True)
    ui.persist_settings.assert_called_once_with()


def test_lora_protocol_switch_rejection_does_not_persist_or_claim_owner():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({})
    ui.persist_settings = Mock()
    ui._wdg_owned_lora = True
    ui.app = NS(
        _lora_enabled=True,
        _lora=NS(running=False, mode="", radio_owned=False),
        _meshtastic=NS(connected=False),
        _switch_lora_protocol=Mock(return_value=False))

    assert ui.cycle_lora_protocol() is False
    assert ui.settings["lora_protocol"] == "meshcore"
    assert ui._wdg_owned_lora
    ui.persist_settings.assert_not_called()


def test_meshtastic_backend_switch_reconnects_existing_client_only():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({})
    ui.settings["lora_protocol"] = "meshtastic"
    ui.persist_settings = Mock()
    ui.host_ble = NS(worker_active=False)
    ui._meshtastic_action_results = Queue()
    ui._meshtastic_action_thread = None
    ui._host_ble_retry_pending = False
    manager = NS(
        running=True, backend_mode="auto", last_error="",
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        close=Mock(return_value=True),
        activate_backend_service=Mock(return_value=True),
        set_backend_mode=Mock(return_value=True), start=Mock(return_value=True),
        wait_connected=Mock(return_value=True),
        commit_backend_service_activation=Mock(),
        rollback_backend_service_activation=Mock(return_value=True),
        _service_controller=None)
    ui.app = NS(
        _meshtastic=manager, _term_add=Mock(), msg=Mock(),
        _watch=NS(worker_active=False),
        _lora=NS(running=False, worker_active=False, radio_owned=False),
        _lora_transition_active=lambda: False)

    assert ui.cycle_meshtastic_backend()
    ui._meshtastic_action_thread.join(timeout=2.0)
    ok, _label, _detail, on_success = (
        ui._meshtastic_action_results.get_nowait())
    assert ok
    on_success()

    assert ui.settings["meshtastic_backend"] == "fork_socket"
    manager.close.assert_called_once_with()
    manager.activate_backend_service.assert_called_once_with("fork_socket")
    manager.set_backend_mode.assert_called_once_with("fork_socket")
    manager.start.assert_called_once_with()
    manager.wait_connected.assert_called_once_with(
        timeout=15.0, backend="fork_socket")
    manager.commit_backend_service_activation.assert_called_once_with()
    ui.persist_settings.assert_called_once_with()


def test_meshcore_backend_choice_defers_service_handoff():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({})
    assert ui.settings["lora_protocol"] == "meshcore"
    ui.persist_settings = Mock()
    ui.host_ble = NS(worker_active=False)
    ui._meshtastic_action_results = Queue()
    ui._meshtastic_action_thread = None
    ui._host_ble_retry_pending = False
    manager = NS(
        running=False, connected=False, backend_mode="auto", last_error="",
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        set_backend_mode=Mock(return_value=True), close=Mock(),
        activate_backend_service=Mock(), start=Mock(),
        commit_backend_service_activation=Mock(),
        rollback_backend_service_activation=Mock(),
        _service_controller=None)
    ui.app = NS(
        _meshtastic=manager, _term_add=Mock(), msg=Mock(),
        _watch=NS(worker_active=False),
        _lora=NS(running=False, worker_active=False, radio_owned=False),
        _lora_transition_active=lambda: False)

    assert ui.cycle_meshtastic_backend()
    ui._meshtastic_action_thread.join(timeout=2.0)
    ok, _label, detail, on_success = (
        ui._meshtastic_action_results.get_nowait())
    assert ok
    assert detail == "preference set to fork_socket"
    on_success()

    assert ui.settings["meshtastic_backend"] == "fork_socket"
    manager.set_backend_mode.assert_called_once_with("fork_socket")
    manager.close.assert_not_called()
    manager.activate_backend_service.assert_not_called()
    manager.start.assert_not_called()
    manager.commit_backend_service_activation.assert_not_called()
    manager.rollback_backend_service_activation.assert_not_called()
    ui.persist_settings.assert_called_once_with()
    ui.app._term_add.assert_called_once_with(
        "[MT] Backend preference set to fork_socket; applies when Meshtastic "
        "is selected",
        raw=True)


def test_meshtastic_backend_switch_aborts_before_service_change_if_close_sticks():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({})
    ui.settings["lora_protocol"] = "meshtastic"
    ui.persist_settings = Mock()
    ui.host_ble = NS(worker_active=False)
    ui._meshtastic_action_results = Queue()
    ui._meshtastic_action_thread = None
    manager = NS(
        running=True, backend_mode="auto", last_error="",
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        close=Mock(return_value=False),
        activate_backend_service=Mock(), set_backend_mode=Mock(), start=Mock(),
        _service_controller=None)
    ui.app = NS(
        _meshtastic=manager, _term_add=Mock(), msg=Mock(),
        _watch=NS(worker_active=False),
        _lora=NS(running=False, worker_active=False, radio_owned=False),
        _lora_transition_active=lambda: False)

    assert ui.cycle_meshtastic_backend()
    deadline = time.monotonic() + 2.0
    while ui._meshtastic_action_results.empty():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    ok, _label, detail, on_success = (
        ui._meshtastic_action_results.get_nowait())

    assert not ok
    assert "worker did not stop" in detail
    assert on_success is not None
    manager.activate_backend_service.assert_not_called()
    manager.set_backend_mode.assert_not_called()
    ui.persist_settings.assert_not_called()


def test_backend_switch_rolls_back_and_does_not_persist_before_negotiation():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({})
    ui.settings["lora_protocol"] = "meshtastic"
    ui.persist_settings = Mock()
    ui.host_ble = NS(worker_active=False)
    ui._meshtastic_action_results = Queue()
    ui._meshtastic_action_thread = None
    ui._host_ble_retry_pending = False
    manager = NS(
        running=True, backend_mode="auto", last_error="",
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        close=Mock(return_value=True),
        activate_backend_service=Mock(return_value=True),
        set_backend_mode=Mock(return_value=True),
        start=Mock(return_value=True),
        wait_connected=Mock(side_effect=[False, True]),
        rollback_backend_service_activation=Mock(return_value=True),
        commit_backend_service_activation=Mock(),
        _service_controller=None)
    ui.app = NS(
        _meshtastic=manager, _term_add=Mock(), msg=Mock(),
        _watch=NS(worker_active=False),
        _lora=NS(running=False, worker_active=False, radio_owned=False),
        _lora_transition_active=lambda: False)

    assert ui.cycle_meshtastic_backend()
    ui._meshtastic_action_thread.join(timeout=2.0)
    ok, _label, detail, _on_success = (
        ui._meshtastic_action_results.get_nowait())

    assert not ok
    assert "protocol negotiation" in detail
    manager.rollback_backend_service_activation.assert_called_once_with(
        timeout=15.0)
    manager.commit_backend_service_activation.assert_not_called()
    assert ui.settings["meshtastic_backend"] == "auto"
    ui.persist_settings.assert_not_called()


def test_backend_switch_does_not_commit_or_rollback_without_close_barrier():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({})
    ui.settings["lora_protocol"] = "meshtastic"
    ui.persist_settings = Mock()
    ui.host_ble = NS(worker_active=False)
    ui._meshtastic_action_results = Queue()
    ui._meshtastic_action_thread = None
    ui._host_ble_retry_pending = False
    manager = NS(
        running=False, backend_mode="auto", last_error="",
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        close=Mock(side_effect=[True, False]),
        activate_backend_service=Mock(return_value=True),
        set_backend_mode=Mock(return_value=True),
        start=Mock(return_value=True),
        wait_connected=Mock(return_value=True),
        rollback_backend_service_activation=Mock(return_value=True),
        commit_backend_service_activation=Mock(),
        _service_controller=None)
    ui.app = NS(
        _meshtastic=manager, _term_add=Mock(), msg=Mock(),
        _watch=NS(worker_active=False),
        _lora=NS(running=False, worker_active=False, radio_owned=False),
        _lora_transition_active=lambda: False)

    assert ui.cycle_meshtastic_backend()
    ui._meshtastic_action_thread.join(timeout=2.0)
    ok, _label, detail, _on_success = (
        ui._meshtastic_action_results.get_nowait())

    assert not ok
    assert "not committed" in detail
    manager.commit_backend_service_activation.assert_not_called()
    manager.rollback_backend_service_activation.assert_not_called()
    assert manager.start.call_count == 1
    ui.persist_settings.assert_not_called()


def test_failed_backend_switch_does_not_restart_previously_stopped_client():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({})
    ui.settings["lora_protocol"] = "meshtastic"
    ui.persist_settings = Mock()
    ui.host_ble = NS(worker_active=False)
    ui._meshtastic_action_results = Queue()
    ui._meshtastic_action_thread = None
    ui._host_ble_retry_pending = False
    manager = NS(
        running=False, backend_mode="auto", last_error="",
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        close=Mock(return_value=True),
        activate_backend_service=Mock(return_value=True),
        set_backend_mode=Mock(return_value=True),
        start=Mock(return_value=True),
        wait_connected=Mock(return_value=False),
        rollback_backend_service_activation=Mock(return_value=True),
        commit_backend_service_activation=Mock(),
        _service_controller=None)
    ui.app = NS(
        _meshtastic=manager, _term_add=Mock(), msg=Mock(),
        _watch=NS(worker_active=False),
        _lora=NS(running=False, worker_active=False, radio_owned=False),
        _lora_transition_active=lambda: False)

    assert ui.cycle_meshtastic_backend()
    ui._meshtastic_action_thread.join(timeout=2.0)
    ok, _label, _detail, _on_success = (
        ui._meshtastic_action_results.get_nowait())

    assert not ok
    assert manager.start.call_count == 1
    manager.rollback_backend_service_activation.assert_called_once_with(
        timeout=15.0)


def test_meshtastic_action_thread_start_failure_releases_transition():
    ui = WardriveUI.__new__(WardriveUI)
    ui._meshtastic_action_thread = None
    ui._meshtastic_action_results = Queue()
    ui._meshtastic_action_thread_factory = lambda **_kwargs: NS(
        start=Mock(side_effect=RuntimeError("thread unavailable")))
    manager = NS(last_error="")
    ui.app = NS(
        _meshtastic=manager, _meshtastic_update_running=False,
        _lora_transition_active=lambda: False,
        _lora=NS(worker_active=False, running=False),
        _begin_meshtastic_transition=Mock(return_value=True),
        _end_meshtastic_transition=Mock(), msg=Mock())
    operation = Mock(return_value=True)

    assert not ui._start_meshtastic_action(
        "Test", operation, "completed")

    ui.app._end_meshtastic_transition.assert_called_once_with()
    operation.assert_not_called()
    assert ui._meshtastic_action_thread is None


def test_bluetooth_adapter_settings_use_stable_macs(monkeypatch):
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({})
    ui.persist_settings = Mock()
    ui.host_ble = NS(state="idle")
    ui._meshtastic_action_thread = None
    ui._start_meshtastic_action = Mock()
    manager = NS(
        connected=False, backend="auto", configure_phone_ble=Mock())
    ui.app = NS(_meshtastic=manager, msg=Mock())
    monkeypatch.setattr(
        "watchdogs.wardrive_ui.list_ble_adapters",
        lambda: [("AA:BB:CC:DD:EE:FF", "hci7")])

    ui.cycle_bluetooth_adapter("host_ble_adapter")
    ui.cycle_bluetooth_adapter("meshtastic_phone_adapter")

    assert ui.settings["host_ble_adapter"] == "AA:BB:CC:DD:EE:FF"
    assert ui.settings["meshtastic_phone_adapter"] == "AA:BB:CC:DD:EE:FF"
    manager.configure_phone_ble.assert_called_once_with(
        True, "AA:BB:CC:DD:EE:FF")
    assert ui.persist_settings.call_count == 2


@pytest.mark.parametrize(
    ("worker_active", "lease_active"), ((True, False), (False, True)))
def test_bluetooth_adapter_change_is_blocked_during_active_host_scan(
        worker_active, lease_active, monkeypatch):
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({})
    ui.persist_settings = Mock()
    ui.host_ble = NS(worker_active=worker_active, state="running")
    ui.app = NS(
        _meshtastic_update_running=False,
        _meshtastic=NS(ble_scan_lease_active=lease_active),
        _lora_transition_active=lambda: False,
        _lora=NS(worker_active=False, running=False),
        msg=Mock())
    monkeypatch.setattr(
        "watchdogs.wardrive_ui.list_ble_adapters",
        lambda: [("AA:BB:CC:DD:EE:FF", "hci7")])

    assert ui.cycle_bluetooth_adapter("host_ble_adapter") is False
    assert ui.settings["host_ble_adapter"] == "auto"
    ui.persist_settings.assert_not_called()
    assert "Stop the active host scan" in ui.app.msg.call_args.args[0]


def test_degraded_host_ble_is_locally_inactive_even_if_already_paused():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({})
    ui.scan = NS(state="running", wifi_only=True, session="all")
    ui._host_ble_retry_pending = False
    ui.host_ble = NS(
        state="paused", worker_active=False, drops=0,
        stop=Mock(), poll=Mock(return_value=[(
            "all", "ble", (0.0, {"data_hex": "", "mac": "AA",
                                    "rssi": -40}))]))
    manager = NS(
        host_ble_degraded=True, host_ble_pause_reason="phone priority",
        host_ble_requires_lease=Mock(return_value=True))
    ui.app = NS(_meshtastic=manager, _term_add=Mock(), msg=Mock())
    ui.observation = Mock()

    ui.poll_host_ble(1.0)

    ui.host_ble.stop.assert_called_once_with()
    assert ui.host_ble.state == "paused"
    ui.observation.assert_not_called()
    ui.app._term_add.assert_not_called()


def test_terminal_host_ble_error_allows_explicit_adapter_retry(monkeypatch):
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({})
    ui.persist_settings = Mock()
    ui._host_ble_retry_pending = False
    ui.host_ble = NS(worker_active=False, state="error")
    ui.app = NS(
        _meshtastic_update_running=False,
        _lora_transition_active=lambda: False,
        _lora=NS(worker_active=False, running=False),
        _meshtastic=NS(ble_scan_lease_active=False),
        msg=Mock())
    monkeypatch.setattr(
        "watchdogs.wardrive_ui.list_ble_adapters",
        lambda: [("AA:BB:CC:DD:EE:FF", "hci7")])

    ui.cycle_bluetooth_adapter("host_ble_adapter")

    assert ui.settings["host_ble_adapter"] == "AA:BB:CC:DD:EE:FF"
    assert ui._host_ble_retry_pending
    ui.persist_settings.assert_called_once_with()


def test_phone_ble_toggle_updates_desired_manager_state_while_disconnected():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({})
    ui.persist_settings = Mock()
    ui._host_ble_retry_pending = False
    manager = NS(
        connected=False, backend="auto", configure_phone_ble=Mock())
    ui.app = NS(_meshtastic=manager, msg=Mock())

    ui.toggle_meshtastic_phone_ble()

    assert ui.settings["meshtastic_phone_ble_enabled"] is False
    manager.configure_phone_ble.assert_called_once_with(False, "auto")
    assert ui._host_ble_retry_pending is True
    ui.persist_settings.assert_called_once_with()


@pytest.mark.parametrize(
    ("worker_active", "lease_active"), ((True, False), (False, True)))
def test_phone_ble_toggle_and_shared_retry_are_blocked_during_host_scan(
        worker_active, lease_active):
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({})
    ui.persist_settings = Mock()
    ui._host_ble_retry_pending = False
    ui.host_ble = NS(worker_active=worker_active)
    ui._start_meshtastic_action = Mock()
    manager = NS(
        connected=True, backend="fork_socket",
        ble_scan_lease_active=lease_active,
        configure_phone_ble=Mock(), retry_shared_adapter=Mock())
    ui.app = NS(
        _meshtastic=manager, _meshtastic_update_running=False, msg=Mock())

    assert ui.toggle_meshtastic_phone_ble() is False
    assert ui.retry_meshtastic_shared_adapter() is False

    assert ui.settings["meshtastic_phone_ble_enabled"] is True
    ui.persist_settings.assert_not_called()
    manager.configure_phone_ble.assert_not_called()
    manager.retry_shared_adapter.assert_not_called()
    ui._start_meshtastic_action.assert_not_called()
    assert ui.app.msg.call_count == 2
    assert all("Stop the active host scan" in call.args[0]
               for call in ui.app.msg.call_args_list)


def test_display_controls_persist_and_invalidate_the_right_cache():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({})
    ui.notables = {}
    ui._notable_identities = frozenset()
    ui._notable_revision = 0
    ui.app = NS(
        _cluster_sel=4, _cluster_popup={"old": True},
        _on_map_policy_changed=Mock())
    ui.persist_settings = Mock()
    ui.trail = NS(break_segment=Mock())
    ui._trail_layer = NS(invalidate=Mock())

    ui.cycle_layer_mode("wifi")
    assert ui.settings["dot_wifi_mode"] == "keep"
    assert ui.app._cluster_sel == -1 and ui.app._cluster_popup is None
    ui.app._on_map_policy_changed.assert_called_once_with()

    ui.cycle_fade_seconds()
    assert ui.settings["dot_fade_seconds"] == 60
    assert ui.app._on_map_policy_changed.call_count == 2

    ui.cycle_trail_mode()
    assert ui.settings["trail_mode"] == "solid"
    assert ui.settings["trail"] is True
    ui.trail.break_segment.assert_called_once_with()
    ui._trail_layer.invalidate.assert_called_once_with()

    ui.cycle_trail_mode()  # SOLID -> HEAT is display-only.
    assert ui.settings["trail_mode"] == "heat"
    ui.trail.break_segment.assert_called_once_with()
    assert ui._trail_layer.invalidate.call_count == 2

    ui.cycle_trail_mode()  # HEAT -> OFF closes the active interval.
    assert ui.settings["trail_mode"] == "off"
    assert ui.trail.break_segment.call_count == 2
    assert ui.persist_settings.call_count == 5
