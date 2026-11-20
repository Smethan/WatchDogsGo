"""The companion screen keeps UI input separate from the service runtime."""

from types import SimpleNamespace

import pyxel

from watchdogs import pwnagotchi_screen as screen_module
from watchdogs.pwnagotchi_screen import (
    PwnagotchiScreen,
    draw_map_card,
    face_ascii,
    network_rows,
)


class FakeBridge:
    def __init__(self, state):
        self.current = state
        self.calls = []
        self.reads = 0

    def state(self):
        return self.current

    def mark_read(self):
        self.reads += 1

    def submit(self, action, value=None):
        self.calls.append((action, value))
        return True


def _view():
    return {
        "connected": True,
        "backend": "bettercap",
        "interface": "wlan1",
        "service_active": True,
        "service_enabled": True,
        "adapters": [
            {"name": "wlan1", "mode": "monitor"},
            {"name": "wlan2", "mode": "managed"},
        ],
        "snapshot": {
            "controls": {"supported": ["deauth", "associate"], "deauth": False},
            "radio": {"aps": [{"ssid": "lab", "bssid": "02:00:00:00:00:01"}]},
        },
        "events": [],
    }


def test_face_and_network_rows_are_safe_for_pyxel_font():
    assert face_ascii("(◕‿‿◕)") == "(o^o)"
    assert face_ascii("(☓‿‿☓)") == "(x_x)"
    assert face_ascii("(☢_☢)").isascii()
    assert network_rows({"radio": {"aps": [{"ssid": "lab"}]}}) == [{"ssid": "lab"}]
    assert network_rows({"radio": {"ap_bssids": ["02:00:00:00:00:01"]}}) == [
        {"bssid": "02:00:00:00:00:01"}
    ]


def test_screen_controls_submit_nonblocking_actions(monkeypatch):
    bridge = FakeBridge(_view())
    app = SimpleNamespace(_dragon_drain=SimpleNamespace(running=False))
    screen = PwnagotchiScreen(app, bridge)
    screen.show()
    assert bridge.reads == 1

    pressed = {pyxel.KEY_D}
    monkeypatch.setattr(screen_module.pyxel, "btnp", lambda key: key in pressed)
    screen.update()
    assert bridge.calls[-1] == ("deauth", True)

    pressed = {pyxel.KEY_A}
    screen.update()
    assert bridge.calls[-1] == ("stop", None)

    pressed = {pyxel.KEY_DOWN, pyxel.KEY_RETURN}
    screen.update()
    assert bridge.calls[-1] == ("adapter", "wlan2")


def test_screen_blocks_radio_change_while_dragon_drain_runs(monkeypatch):
    bridge = FakeBridge(_view())
    app = SimpleNamespace(_dragon_drain=SimpleNamespace(running=True))
    screen = PwnagotchiScreen(app, bridge)
    screen.show()
    monkeypatch.setattr(
        screen_module.pyxel, "btnp", lambda key: key == pyxel.KEY_RETURN
    )
    screen.adapter_selection = 1
    screen.update()
    assert bridge.calls == []
    assert "Stop Dragon Drain" in screen.note


def test_map_card_is_hidden_without_pwnagotchi():
    class Pixel:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def draw(*args):
                self.calls.append((name, args))

            return draw

    px = Pixel()
    draw_map_card(px, {"connected": False, "backend": None})
    assert px.calls == []
    draw_map_card(px, _view())
    assert any(name == "text" and args[2] == "PWNAGOTCHI" for name, args in px.calls)


def test_all_screen_tabs_render_live_controls_activity_and_networks(monkeypatch):
    class Pixel:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            def draw(*args):
                self.calls.append((name, args))

            return draw

    view = _view()
    view["events"] = [{"ts": 0, "kind": "decision", "text": "Deauth submitted"}]
    bridge = FakeBridge(view)
    screen = PwnagotchiScreen(SimpleNamespace(_big_font=None), bridge)
    px = Pixel()
    monkeypatch.setattr(screen_module, "pyxel", px)
    for tab in range(3):
        px.calls.clear()
        screen.tab = tab
        screen.draw()
        labels = [args[2] for name, args in px.calls if name == "text"]
        assert "PWNAGOTCHI / LOCAL COMPANION" in labels
        assert all(name != "text" or 0 <= args[1] < 360 for name, args in px.calls)
        if tab == 0:
            assert any("Active now" in text for text in labels)
            assert any("wlan1" in text for text in labels)
        elif tab == 1:
            assert "Deauth submitted" in labels
        else:
            assert "lab" in labels
