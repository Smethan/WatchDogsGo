import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

from watchdogs.wardrive_settings import (
    DEFAULTS, load_settings, normalize_settings, save_settings,
)
from watchdogs.wardrive_ui import WardriveUI


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


def test_collector_toggles_switch_sdr_choice_and_release_owned_lora():
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({})
    ui.persist_settings = Mock()
    ui._wdg_owned_lora = True
    ui._wdg_owned_sdr = True
    lora = NS(running=True, mode="meshcore", stop=Mock())
    sdr = NS(running=True, mode="adsb", stop=Mock())
    ui.app = NS(_lora=lora, _sdr=sdr,
                _term_add=Mock())
    ui.scan = NS(state="idle", mode="", diagnostic=False)

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
