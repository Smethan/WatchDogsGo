"""Bottom HUD layout boundaries."""

from watchdogs.app import HUD_FONT_W, _bottom_menu_hint, _fit_hud_text


def test_bottom_hud_hint_fits_between_cell_and_gps():
    cell_text = "CELL:12345"
    left = 275 + len(cell_text) * HUD_FONT_W + 8
    right = 640 - 108 - 6

    x, text = _bottom_menu_hint(left, right)

    assert x >= left
    assert x + len(text) * HUD_FONT_W <= right
    assert text == "[TAB]Menu [`]Loot [S]Stop"


def test_bottom_hud_uses_complete_compact_hint_beside_lora():
    cell_text = "CELL:12345"
    left = 275 + len(cell_text) * HUD_FONT_W + 8
    right = 640 - 206

    x, text = _bottom_menu_hint(left, right)

    assert x >= left
    assert x + len(text) * HUD_FONT_W <= right
    assert text == "[TAB]Menu [S]Stop"


def test_active_tool_text_is_bounded_before_gps():
    left, right = 330, 526
    x, text = _fit_hud_text(
        "WiFi+BLE stopping with a deliberately long state", left, right)

    assert x == left
    assert x + len(text) * HUD_FONT_W <= right


def test_active_p25_count_has_hud_priority_with_other_tools(monkeypatch):
    from types import SimpleNamespace as NS
    from unittest.mock import Mock
    import watchdogs.app as appmod

    px = Mock(frame_count=0)
    monkeypatch.setattr(appmod, "pyxel", px)
    app = appmod.WatchDogsGame.__new__(appmod.WatchDogsGame)
    app._loot_totals = {}
    app.ble_devices = []
    app.wifi_networks = []
    app.markers = []
    app.wardrive = NS(
        cell=NS(unique=()), scan=NS(active=True, mode="wardrive", state="running"),
        pig=NS(active=True, status="LISTENING"),
        p25_status_text=Mock(return_value="P25:42"),
    )
    app._current_hacked_count = lambda: 0
    app.state = NS(submitted_forms=0, evil_twin_captured_data=(),
                   portal_running=False, evil_twin_running=False)
    app.gps_fix = True
    app.gps = NS(available=True)
    app.player_lat = 33.0
    app.player_lon = -97.0
    app._lora = NS(running=False)
    app._reticulum = NS(running=False, ready=False)
    app._lora_enabled = True
    app.wifi_scanning = True
    app.ble_scanning = True
    app.sniffing = False
    app.capturing_hs = False
    app._sdr = NS(running=False, mode="")
    app.menu_open = False

    app._draw_hud_bottom()

    app.wardrive.p25_status_text.assert_called_once_with(compact=True)
    p25_rows = [call.args for call in px.text.call_args_list
                if call.args[2].startswith("P25:42")]
    assert len(p25_rows) == 1
    x, _, text, _ = p25_rows[0]
    assert x + len(text) * HUD_FONT_W <= 640 - 206
