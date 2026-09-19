"""Whitelist scan lifecycle tests. All serial input is synthetic."""

import time
from pathlib import Path

import pytest

from watchdogs.app_state import Network
from watchdogs.network_manager import NetworkManager
from watchdogs.whitelist_manager import WhitelistManager
from test_wardrive import game, loot


@pytest.fixture
def whitelist_game(game):
    game._whitelist = WhitelistManager(
        Path(game._app_dir) / "whitelist.json")
    game._wl_scan_list = []
    game._wl_scan_sel = 0
    game._wl_scan_results = {}
    game._wl_scan_phase = "idle"
    game._wl_scan_deadline = 0.0
    game._wl_scan_error = ""
    game._wl_scan_partial = False
    game._wl_stop_outcome = "idle"
    game._wl_add_step = "scan_select"
    game._wl_screen = True
    game._fw_version = "1.7.9"
    game._serial_capture_kind = None
    game._et_scan_pending = False
    game._bt_airtag = False
    game._airtag_count = 0
    game._smarttag_count = 0
    game._state_network_by_bssid = {}
    game.net_mgr = NetworkManager(game.state)
    return game


def start_scan(game, kind):
    assert game._wl_start_scan(kind)
    assert game._wl_scan_phase == "waiting_stop"
    assert game.serial.send_command.call_args.args == ("stop",)
    game.wardrive.handle_line("All operations stopped.")
    assert game._wl_scan_phase == "scanning_" + kind
    expected = "scan_networks" if kind == "wifi" else "scan_bt"
    assert game.serial.send_command.call_args.args == (expected,)


def test_known_wifi_serial_result_is_not_lost_with_empty_map_nodes(
        whitelist_game):
    game = whitelist_game
    bssid = "02:00:00:00:00:01"
    game._known_wifi.add(bssid)
    assert not game.wifi_networks
    start_scan(game, "wifi")

    game._handle_serial_line(
        '"1","Home","Vendor","02:00:00:00:00:01",'
        '"6","WPA2","-42","2.4GHz"')
    assert not game.wifi_networks
    assert len(game.state.networks) == 1
    game._handle_serial_line("Scan results printed.")

    assert game._wl_scan_phase == "complete"
    assert [(item["name"], item["mac"], item["rssi"])
            for item in game._wl_scan_list] == [
                ("Home", bssid, -42)]


def test_results_deduplicate_sort_and_filter_whitelist(whitelist_game):
    game = whitelist_game
    blocked = "02:00:00:00:00:03"
    assert game._whitelist.add("wifi", blocked, "blocked")
    game._wl_scan_phase = "scanning_wifi"
    game._wl_observe_wifi(Network(
        ssid="weak", bssid="02:00:00:00:00:01", channel="1", rssi="-80"))
    game._wl_observe_wifi(Network(
        ssid="stronger", bssid="02:00:00:00:00:01", channel="6", rssi="-40"))
    game._wl_observe_wifi(Network(
        ssid="middle", bssid="02:00:00:00:00:02", channel="11", rssi="-55"))
    game._wl_observe_wifi(Network(
        ssid="blocked", bssid=blocked, channel="1", rssi="-10"))
    game._wl_finish_scan()

    assert [(item["name"], item["rssi"], item["extra"])
            for item in game._wl_scan_list] == [
                ("stronger", -40, "Ch:6"),
                ("middle", -55, "Ch:11"),
            ]


def test_wifi_and_ble_use_their_own_completion_records(whitelist_game):
    game = whitelist_game
    game._wl_scan_phase = "scanning_wifi"
    game._wl_handle_scan_status(
        "Summary: 0 AirTags, 0 SmartTags, 2 total devices",
        "summary: 0 airtags, 0 smarttags, 2 total devices")
    assert game._wl_scan_phase == "scanning_wifi"
    game._wl_handle_scan_status(
        "Scan results printed.", "scan results printed.")
    assert game._wl_scan_phase == "complete"

    game._wl_scan_results = {}
    game._wl_scan_phase = "scanning_ble"
    game._wl_observe_ble("02:00:00:00:00:09", -30, "sensor")
    game._wl_handle_scan_status(
        "Scan results printed.", "scan results printed.")
    assert game._wl_scan_phase == "scanning_ble"
    game._wl_handle_scan_status(
        "Summary: 0 AirTags, 0 SmartTags, 1 total devices",
        "summary: 0 airtags, 0 smarttags, 1 total devices")
    assert game._wl_scan_phase == "complete"
    assert game._wl_scan_list[0]["type"] == "ble"


def test_timeout_keeps_partial_results_and_waits_for_stop_ack(
        whitelist_game, monkeypatch):
    game = whitelist_game
    game._wl_scan_phase = "scanning_wifi"
    game._wl_scan_deadline = 10
    game._wl_observe_wifi(Network(
        ssid="partial", bssid="02:00:00:00:00:04",
        channel="3", rssi="-60"))
    monkeypatch.setattr(time, "monotonic", lambda: 11)

    game._wl_tick_scan()
    assert game._wl_scan_phase == "stopping"
    assert game._wl_scan_partial
    assert game._wl_scan_list[0]["name"] == "partial"
    assert game.serial.send_command.call_args.args == ("stop",)
    game.wardrive.handle_line("All operations stopped.")
    assert game._wl_scan_phase == "error"
    assert game._wl_scan_error == "Timed out"


def test_rejection_and_cancel_do_not_leave_scan_running(whitelist_game):
    game = whitelist_game
    game._wl_scan_phase = "scanning_ble"
    game._wl_handle_scan_status(
        "BLE scan start failed: 257", "ble scan start failed: 257")
    assert game._wl_scan_phase == "stopping"
    game.wardrive.handle_line("All operations stopped.")
    assert game._wl_scan_phase == "error"

    game._wl_scan_phase = "idle"
    start_scan(game, "wifi")
    game._wl_cancel_scan()
    assert game._wl_scan_phase == "stopping"
    assert game.serial.send_command.call_args.args == ("stop",)
    game.wardrive.handle_line("All operations stopped.")
    assert game._wl_scan_phase == "idle"


def test_adding_result_removes_it_from_cached_picker(whitelist_game):
    game = whitelist_game
    game._wl_scan_phase = "scanning_wifi"
    game._wl_observe_wifi(Network(
        ssid="Home", bssid="02:00:00:00:00:05",
        channel="6", rssi="-45"))
    game._wl_finish_scan()
    item = game._wl_scan_list[0]
    assert game._whitelist.add(item["type"], item["mac"], item["name"])
    game._wl_refresh_scan_list()
    assert game._wl_scan_list == []
