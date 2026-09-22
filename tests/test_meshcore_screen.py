"""MeshCore contacts overlay remains usable before any node is heard."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import watchdogs.app as appmod
from watchdogs.app import WatchDogsGame


def _messenger():
    app = WatchDogsGame.__new__(WatchDogsGame)
    app._mc_screen = True
    app._mc_nodes_panel = False
    app._mc_nodes = []
    app._mc_node_sel = 0
    app._mc_node_action = False
    app._mc_note_editing = False
    app._mc_dm_target = None
    app._mc_log = []
    app._mc_input = ""
    app._mc_scroll = 0
    app._mc_chan_picker = False
    app._mc_channels_list = []
    app._mc_active_ch = 0
    app._mc_node_name = "test"
    app._lora = NS(running=True, packets_received=0)
    app._get_char_input = Mock(return_value="a")
    return app


def test_empty_contacts_panel_is_visible_and_escape_returns_to_chat(monkeypatch):
    app = _messenger()
    pressed = set()
    monkeypatch.setattr(appmod.pyxel, "btn", lambda key: key in pressed)
    monkeypatch.setattr(appmod.pyxel, "btnp", lambda key: key in pressed)

    pressed.update((appmod.pyxel.KEY_LCTRL, appmod.pyxel.KEY_H))
    app._update_mc_screen()
    assert app._mc_nodes_panel and app._mc_screen

    drawn = []
    original_pyxel = appmod.pyxel
    fake_pyxel = NS(frame_count=0, cls=Mock(), rect=Mock(), rectb=Mock(),
                    line=Mock(), text=lambda _x, _y, value, _color:
                    drawn.append(value))
    monkeypatch.setattr(appmod, "pyxel", fake_pyxel)
    app._draw_mc_screen()
    assert "CONTACTS (0)" in drawn
    assert "No MeshCore nodes heard yet." in drawn
    assert "ESC or Ctrl+H: return to chat" in drawn

    # A visible empty panel owns input; Escape dismisses just that panel.
    monkeypatch.setattr(appmod, "pyxel", original_pyxel)
    pressed.clear()
    pressed.add(appmod.pyxel.KEY_ESCAPE)
    app._update_mc_screen()
    assert not app._mc_nodes_panel and app._mc_screen
    pressed.clear()
    app._update_mc_screen()
    assert app._mc_input == "a"


def test_contacts_panel_accepts_node_that_arrives_after_opening(monkeypatch):
    app = _messenger()
    pressed = set()
    monkeypatch.setattr(appmod.pyxel, "btn", lambda key: key in pressed)
    monkeypatch.setattr(appmod.pyxel, "btnp", lambda key: key in pressed)
    app._mc_nodes_panel = True

    app._update_mc_screen()
    assert app._mc_input == ""  # empty panel owns input, even with no nodes
    app._mc_nodes.append({"id": "abc", "name": "nearby"})
    pressed.add(appmod.pyxel.KEY_RETURN)
    app._update_mc_screen()
    assert app._mc_node_action and app._mc_nodes_panel
