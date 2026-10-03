"""GPS visible-satellite unknown/zero presentation tests."""

from watchdogs import app as app_module
from watchdogs.app import WatchDogsGame, _gps_visible_text


def test_visible_satellite_text_distinguishes_unknown_from_zero():
    assert _gps_visible_text(0, False) == "--"
    assert _gps_visible_text(0, True) == "0"
    assert _gps_visible_text(12, True) == "12"


def test_wait_dialog_explains_unknown_and_explicit_zero(monkeypatch):
    rendered = []
    monkeypatch.setattr(app_module.pyxel, "rect", lambda *args: None)
    monkeypatch.setattr(app_module.pyxel, "rectb", lambda *args: None)
    monkeypatch.setattr(app_module.pyxel, "line", lambda *args: None)
    monkeypatch.setattr(
        app_module.pyxel, "text",
        lambda _x, _y, text, _color: rendered.append(text))

    game = WatchDogsGame.__new__(WatchDogsGame)
    game.gps_sats = 0
    game.gps_sats_vis = 0
    game.gps_sats_vis_known = False
    game._draw_gps_wait_dialog()
    assert "Visible: -- (waiting for satellite data)" in rendered

    rendered.clear()
    game.gps_sats_vis_known = True
    game._draw_gps_wait_dialog()
    assert "Visible: 0 (receiver reports none)" in rendered
