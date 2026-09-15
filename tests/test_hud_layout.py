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
