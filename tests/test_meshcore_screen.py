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


def test_meshtastic_messenger_uses_daemon_channels_and_filters_contacts(
        monkeypatch):
    app = _messenger()
    app.wardrive = NS(settings={"lora_protocol": "meshtastic"})
    app._meshtastic = NS(
        running=True, connected=True, packets_received=4,
        local_name="WDG MT",
        channels=[{"index": 0, "name": "Primary"},
                  {"index": 2, "name": "Road"}],
        send_text=Mock(return_value=True), request_discovery=Mock(return_value=True))
    app._mc_nodes = [
        {"id": "meshcore:1", "name": "MC", "protocol": "meshcore"},
        {"id": "meshtastic:!00000002", "address": "!00000002",
         "name": "Nearby", "protocol": "meshtastic", "type": "Meshtastic"},
    ]
    app._mc_active_ch = 1
    app._mc_input = "hello"
    pressed = {appmod.pyxel.KEY_RETURN}
    monkeypatch.setattr(appmod.pyxel, "btn", lambda key: key in pressed)
    monkeypatch.setattr(appmod.pyxel, "btnp", lambda key: key in pressed)

    app._update_mc_screen()
    app._meshtastic.send_text.assert_called_once_with("hello", channel=2)
    assert app._mesh_nodes() == [app._mc_nodes[1]]


def test_meshtastic_contact_action_opens_direct_message_without_meshcore_key(
        monkeypatch):
    app = _messenger()
    app.wardrive = NS(settings={"lora_protocol": "meshtastic"})
    app._meshtastic = NS(
        running=True, connected=True, packets_received=0,
        local_name="WDG MT", channels=[{"index": 0, "name": "Primary"}])
    node = {"id": "meshtastic:!00000002", "address": "!00000002",
            "name": "Nearby", "protocol": "meshtastic",
            "type": "Meshtastic"}
    app._mc_nodes = [node]
    app._mc_nodes_panel = True
    app._mc_node_action = True
    app._mc_node_action_sel = 0
    pressed = {appmod.pyxel.KEY_RETURN}
    monkeypatch.setattr(appmod.pyxel, "btn", lambda key: key in pressed)
    monkeypatch.setattr(appmod.pyxel, "btnp", lambda key: key in pressed)

    app._update_mc_nodes_panel()
    assert app._mc_dm_target is node
    assert not app._mc_nodes_panel
