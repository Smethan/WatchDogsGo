"""Tab-menu hotkeys use the same activation path as Enter."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

import watchdogs.app as appmod
from watchdogs.app import MENU_CATS, WatchDogsGame, _menu_hotkey_key

MENU_ITEMS = [
    (cat_idx, item_idx, item[0])
    for cat_idx, (_name, items) in enumerate(MENU_CATS)
    for item_idx, item in enumerate(items)
]


def menu_app(cat=0, selection=0):
    app = WatchDogsGame.__new__(WatchDogsGame)
    app.menu_cat = cat
    app.menu_sel = selection
    app.menu_open = True
    app._activate_menu_item = Mock()
    return app


def press(monkeypatch, *keys):
    pressed = set(keys)
    monkeypatch.setattr(appmod.pyxel, "btnp", lambda key: key in pressed)


def test_menu_hotkeys_are_supported_and_unique_per_category():
    for category, items in MENU_CATS:
        keys = [item[0] for item in items]
        assert len(keys) == len(set(keys)), category
        assert all(_menu_hotkey_key(key) is not None for key in keys), category


@pytest.mark.parametrize("cat_idx,item_idx,hotkey", MENU_ITEMS)
def test_every_displayed_hotkey_activates_its_current_tab_item(
        monkeypatch, cat_idx, item_idx, hotkey):
    app = menu_app(cat_idx)
    press(monkeypatch, _menu_hotkey_key(hotkey))
    app._update_menu()
    assert app.menu_sel == item_idx
    app._activate_menu_item.assert_called_once_with(cat_idx, item_idx)


def test_reused_hotkey_is_scoped_to_visible_category(monkeypatch):
    app = menu_app(0)
    press(monkeypatch, appmod.pyxel.KEY_A)
    app._update_menu()
    scan_a = next(i for i, item in enumerate(MENU_CATS[0][1])
                  if item[0] == "a")
    app._activate_menu_item.assert_called_once_with(0, scan_a)

    app._activate_menu_item.reset_mock()
    app.menu_cat = 4
    app._update_menu()
    system_a = next(i for i, item in enumerate(MENU_CATS[4][1])
                    if item[0] == "a")
    app._activate_menu_item.assert_called_once_with(4, system_a)


def test_navigation_and_enter_take_precedence(monkeypatch):
    app = menu_app(0, 1)
    press(monkeypatch, appmod.pyxel.KEY_RIGHT, appmod.pyxel.KEY_1)
    app._update_menu()
    assert (app.menu_cat, app.menu_sel) == (1, 0)
    app._activate_menu_item.assert_not_called()

    press(monkeypatch, appmod.pyxel.KEY_DOWN, appmod.pyxel.KEY_1)
    app._update_menu()
    assert app.menu_sel == 1
    app._activate_menu_item.assert_not_called()

    press(monkeypatch, appmod.pyxel.KEY_RETURN, appmod.pyxel.KEY_1)
    app._update_menu()
    app._activate_menu_item.assert_called_once_with(1, 1)


def test_input_hotkey_opens_same_dialog_as_enter(monkeypatch):
    app = WatchDogsGame.__new__(WatchDogsGame)
    app.menu_cat = 0
    app.menu_sel = 0
    app.menu_open = True
    app.input_mode = False
    press(monkeypatch, appmod.pyxel.KEY_T)
    app._update_menu()
    assert not app.menu_open
    assert app.input_mode
    assert app.input_fields == [{"label": "MAC", "value": ""}]
    assert (app._input_pending_cat, app._input_pending_item) == (0, 2)


def test_toggle_hotkey_keeps_menu_open_and_regular_action_closes_it(
        monkeypatch):
    app = WatchDogsGame.__new__(WatchDogsGame)
    app.menu_cat = 4
    app.menu_sel = 0
    app.menu_open = True
    app._execute_item = Mock()
    press(monkeypatch, appmod.pyxel.KEY_G)
    app._update_menu()
    assert app.menu_open
    gps_idx = next(i for i, item in enumerate(MENU_CATS[4][1])
                   if item[0] == "g")
    app._execute_item.assert_called_once_with(
        MENU_CATS[4][1][gps_idx][2],
        MENU_CATS[4][1][gps_idx][3],
        MENU_CATS[4][1][gps_idx][1], [])

    app._execute_item.reset_mock()
    press(monkeypatch, appmod.pyxel.KEY_X)
    app._update_menu()
    assert not app.menu_open
    stop_idx = next(i for i, item in enumerate(MENU_CATS[4][1])
                    if item[0] == "x")
    app._execute_item.assert_called_once_with(
        MENU_CATS[4][1][stop_idx][2],
        MENU_CATS[4][1][stop_idx][3],
        MENU_CATS[4][1][stop_idx][1], [])


def test_unrecognized_key_does_nothing(monkeypatch):
    app = menu_app(2, 3)
    press(monkeypatch, appmod.pyxel.KEY_Z)
    app._update_menu()
    assert (app.menu_cat, app.menu_sel) == (2, 3)
    app._activate_menu_item.assert_not_called()


def test_menu_availability_tracks_live_addon_switches():
    app = WatchDogsGame.__new__(WatchDogsGame)
    app._lora_enabled = False
    app._sdr_enabled = False
    app._lora = NS(running=False, mode="")
    app._watch = NS(connected=False)

    assert "Enable LoRa" in app._menu_item_unavailable_reason(
        "_meshcore", "meshcore")
    assert "Enable SDR" in app._menu_item_unavailable_reason(
        "_sdr_adsb", "_sdr_adsb")
    assert "Enable SDR" in app._menu_item_unavailable_reason(
        "_sdr_433", "_sdr_433")

    app._lora_enabled = True
    app._sdr_enabled = True
    assert not app._menu_item_unavailable_reason("_meshcore", "meshcore")
    assert not app._menu_item_unavailable_reason("_sdr_adsb", "_sdr_adsb")
    assert not app._menu_item_unavailable_reason("_sdr_433", "_sdr_433")

    app._lora.running = True
    app._lora.mode = "meshtastic"
    assert "busy" in app._menu_item_unavailable_reason(
        "_meshcore", "meshcore")
    app._watch.connected = True
    assert not app._menu_item_unavailable_reason("_meshcore", "meshcore")


@pytest.mark.parametrize("cmd,state_key", [
    ("_wardrive_settings", "_wardrive_settings"),
    ("_evil_twin", "evil_twin"),
    ("_evil_portal", "portal"),
    ("_flipper", "_flipper"),
    ("_watch_connect", "_watch"),
])
def test_implemented_python_menu_items_are_not_permanently_grey(
        cmd, state_key):
    app = WatchDogsGame.__new__(WatchDogsGame)
    app._lora_enabled = False
    app._sdr_enabled = False
    app._lora = NS(running=False, mode="")
    app._watch = NS(connected=False)
    assert not app._menu_item_unavailable_reason(cmd, state_key)


def test_disabled_menu_item_stays_open_until_requirement_is_enabled():
    app = WatchDogsGame.__new__(WatchDogsGame)
    app.menu_open = True
    app._lora_enabled = False
    app._lora = NS(running=False, mode="")
    app._mc_region = "us_ca_narrow"
    app._watch = NS(connected=False)
    app.msg = Mock()
    app._execute_item = Mock()
    addons = next(i for i, (name, _) in enumerate(MENU_CATS)
                  if name == "ADDONS")
    meshcore = next(i for i, item in enumerate(MENU_CATS[addons][1])
                    if item[2] == "_meshcore")

    app._activate_menu_item(addons, meshcore)
    assert app.menu_open
    app._execute_item.assert_not_called()
    assert "Enable LoRa" in app.msg.call_args.args[0]

    app._lora_enabled = True
    app._activate_menu_item(addons, meshcore)
    assert not app.menu_open
    app._execute_item.assert_called_once_with(
        "_meshcore", "meshcore", "Mesh Messenger", [])
