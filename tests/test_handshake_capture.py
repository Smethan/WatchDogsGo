"""Synthetic active capture telemetry and background menu tests; no radios."""
import base64
import json
import sys
from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest

from watchdogs.handshake_capture import HandshakeCapture, parse_progress, COMMANDS
from test_wardrive import game, loot, passive_frame, passive_records


def wire(d):
    return "HSC:" + json.dumps(d, separators=(",", ":"))


def status(kind="started", seq=1, session="a", storage="serial", **extra):
    return dict(v=1, kind=kind, seq=seq, session=session, storage=storage,
                wifi_count=0, ble_count=0, drops=0, **extra)


def packet(frame=None, **kw):
    return [dict(d, channel=None, rssi=None) for d in passive_records(
        passive_frame() if frame is None else frame, session="a", **kw)]


def started():
    c = HandshakeCapture()
    c.start(COMMANDS["serial"])
    c.handle(wire(status()))
    return c


@pytest.mark.parametrize("change", [dict(v=True), dict(seq=0), dict(storage="other"),
    dict(drops=-1), dict(session="bad\n"), dict(kind=[]), dict(kind="oops")])
def test_bad_status(change):
    d = status(); d.update(change)
    assert parse_progress(wire(d)) is None


@pytest.mark.parametrize("change", [dict(offset=1), dict(total=2305), dict(data_hex="aa "),
    dict(age_ms=2001), dict(packet=False), dict(capture_ms=-1), dict(data_hex="aa"*241)])
def test_bad_packet(change):
    d = packet(seq=2)[0]; d.update(change)
    assert parse_progress(wire(d)) is None


def test_messages_clients_and_pmkid_dedup():
    c = started()
    for i, key_info in enumerate((0x008a, 0x010a, 0x13ca, 0x030a), 1):
        frame = bytearray(passive_frame(i == 1))
        frame[37:39] = key_info.to_bytes(2, "big")
        frame[49:81] = b"\x42"*32 if i != 4 else b"\x00"*32
        c.handle(wire(packet(frame, seq=i+1, packet=i)[0]))
    c.handle(wire(packet(seq=6, packet=5)[0]))
    other = bytearray(passive_frame()); other[4:10] = bytes.fromhex("020000000099")
    c.handle(wire(packet(other, seq=7, packet=6)[0]))
    run = c.current
    rows = list(run.rows.values())
    assert rows[0]["messages"] == [2,1,1,1] and rows[1]["messages"] == [1,0,0,0]
    assert run.pmkids == 2 and rows[0]["pmkids"] == 1
    assert run.frames == run.eapol == 6
    assert rows[0]["channel"] is None and rows[0]["rssi"] is None
    assert not run.file and not run.events and run.path is None


def test_missing_chunks_duplicates_stop_drain_and_retired_sessions():
    c = started(); run = c.current
    parts = packet(passive_frame()+b"\x00"*600, seq=2)
    c.handle(wire(parts[0])); c.handle(wire(parts[2]))
    assert run.frames == 0 and run.lost == 1 and run.gaps == 1
    d = packet(seq=8, packet=2)[0]
    c.stop(); c.handle(wire(d)); c.handle(wire(d))
    assert run.frames == 1 and run.state == "stopping"
    c.handle(wire(status("stopped", seq=9)))
    assert run.state == "finishing" and run.active
    assert not c.handle("Handshake attack cleanup complete.")
    assert run.state == "stopped" and not run.active
    c.start(COMMANDS["serial"])
    c.handle(wire(status("stats", seq=10)))
    assert c.current.session is None
    c.handle(wire(status("stats", seq=3, session="b")))
    assert c.current.session == "b" and c.current.gaps == 2
    c.handle(wire(dict(d, seq=99)))
    assert c.current.frames == 0


def test_legacy_fallback_replaced_by_telemetry_and_mode_checked():
    c = HandshakeCapture(); c.start(COMMANDS["sd"])
    line = "[HS-SNIFF] EAPOL M1 captured for 'lab' (02:00:00:00:00:02)"
    assert not c.handle(line)
    assert next(iter(c.current.rows.values()))["pmkids"] is None
    c.handle(wire(status()))  # other storage must not attach
    assert c.current.session is None
    c.handle(wire(status(storage="sd")))
    assert not c.current.rows and c.current.eapol == 0
    c.handle(line)
    assert c.current.eapol == 0  # no double count from legacy + telemetry
    c.handle(wire(packet(seq=2)[0]))
    assert c.current.pmkids == 1


@pytest.mark.parametrize("storage", ["sd", "serial"])
def test_capture_menu_start_back_reopen_stop(game, monkeypatch, storage):
    from watchdogs.app import MENU_CATS
    w = game.wardrive; px = sys.modules["pyxel"]
    cmd = COMMANDS[storage]; key = f"_hs_capture_{storage}_menu"
    assert sum(item[3] == key for _, items in MENU_CATS for item in items) == 2
    game._is_running = Mock(return_value=False)
    game._execute_item(cmd, key, "HS Capture", [])
    assert w.capture_screen.open and w.capture.current is None
    monkeypatch.setattr(px, "btnp", lambda k:k == px.KEY_RETURN)
    w.capture_screen.update()
    assert game._pending_cmd == cmd
    w.handle_line("All operations stopped.")
    assert game.serial.send_command.call_args.args == (cmd,)
    assert game.capturing_hs and w.capture.storage == storage
    w.handle_line(wire(status(storage=storage)))
    game.serial.send_command.reset_mock()
    monkeypatch.setattr(px, "btnp", lambda k:k == px.KEY_ESCAPE)
    w.capture_screen.update()
    assert not w.capture_screen.open and w.capture.current.active
    game._execute_item(cmd, key, "HS Capture", [])
    assert w.capture_screen.open and w.capture.current.state == "running"
    game.serial.send_command.assert_not_called()
    monkeypatch.setattr(px, "btnp", lambda k:k == px.KEY_S)
    w.capture_screen.update()
    assert game.serial.send_command.call_args.args == ("stop",)
    assert w.capture.current.state == "stopping"


def test_variant_switch_preserves_previous_results_and_disconnect(game, monkeypatch):
    w = game.wardrive; game._is_running = Mock(return_value=True)
    w.capture.start(COMMANDS["sd"]); w.capture.handle(wire(status(storage="sd")))
    w.capture.handle(wire(packet(seq=2)[0]))
    # Actual _execute_item must see handshake_start as false even with old HS active.
    original = game._execute_item
    def execute(cmd, key, name, values):
        assert key == "handshake_start"
        game._is_running.return_value = False
        original(cmd, key, name, values)
    game._execute_item = execute
    w.capture_screen.show("serial")
    px = sys.modules["pyxel"]
    monkeypatch.setattr(px, "btnp", lambda k:k == px.KEY_RETURN)
    w.capture_screen.update()
    assert game._pending_cmd == COMMANDS["serial"]
    w.handle_line("All operations stopped.")
    assert w.capture.runs["sd"].pmkids == 1 and not w.capture.runs["sd"].active
    assert w.capture.storage == "serial" and w.capture.current.state == "starting"
    game.serial = None; w.tick()
    assert w.capture.current.state == "disconnected" and not game.capturing_hs


def test_legacy_file_output_not_swallowed_and_end_flag_cleared(game, tmp_path):
    from watchdogs.loot_manager import LootManager
    game.loot = LootManager(str(tmp_path))
    w = game.wardrive; w.capture.start(COMMANDS["serial"])
    w.handle_line(wire(status()))
    game._fw_version = "1.7.4"; game.net_mgr = NS(parse_network_line=lambda _:None)
    game._bt_airtag = False; game._trigger_hs_event = Mock()
    raw = b"synthetic PCAP preserved byte-for-byte"
    lines = ["--- PCAP BEGIN ---", base64.b64encode(raw).decode(), "--- PCAP END ---",
             "SSID: lab AP: 02:00:00:00:00:02"]
    for line in lines:
        assert not w.handle_line(line)
        game._handle_serial_line(line)
    paths = list(game.loot._handshake_dir.glob("*.pcap"))
    assert len(paths) == 1 and paths[0].read_bytes() == raw
    game.capturing_hs = True
    game._handle_serial_line("Handshake attack task finished.")
    assert not game.capturing_hs and w.capture.current.state == "stopped"
    game._handle_serial_line("No complete handshake captured")
    assert not game.capturing_hs
    game.loot.close()


def test_capture_draw_unknown_values_and_progress_late(game, monkeypatch):
    w = game.wardrive; w.capture.start(COMMANDS["sd"])
    w.capture.handle("[HS-SNIFF] EAPOL M2 captured for 'lab' (02:00:00:00:00:02)")
    w.capture_screen.show("sd")
    px = sys.modules["pyxel"]; text = Mock()
    for name in ("cls", "rect", "line"):
        monkeypatch.setattr(px, name, Mock())
    monkeypatch.setattr(px, "text", text)
    w.capture_screen.draw()
    labels = [call.args[2] for call in text.call_args_list]
    assert "N/A" in labels and "--" in labels
    assert any("requires SD card on ESP32" in s for s in labels)
    w.capture.handle(wire(status(storage="sd")))
    w.capture.current.last_progress = -100
    text.reset_mock(); w.capture_screen.draw()
    assert any("capture has not been stopped" in c.args[2] for c in text.call_args_list)
    assert w.capture.current.active


def test_early_stop_ack_waits_for_file_cleanup_before_next_mode(game):
    w = game.wardrive
    w.capture.start(COMMANDS["serial"])
    w.handle_line(wire(status()))
    game._start_scan_cmd(COMMANDS["sd"], "handshake_start", "HS Capture")
    w.handle_line(wire(status("stopped", seq=2)))
    assert w.capture.current.state == "finishing"
    w.handle_line("All operations stopped.")
    assert w.capture_stop_ack and game._pending_cmd == COMMANDS["sd"]
    assert w.capture.current.active and w.capture.storage == "serial"
    w.tick()
    assert game._pending_cmd == COMMANDS["sd"]
    w.handle_line("Handshake attack cleanup complete.")
    # Dispatch next tick, so the old cleanup line cannot clear the new HS flag.
    w.tick()
    assert game._pending_cmd is None and w.capture.storage == "sd"
    assert w.capture.current.state == "starting" and game.capturing_hs


def test_old_firmware_cleanup_and_forced_stop():
    c = HandshakeCapture(); c.start(COMMANDS["serial"])
    c.handle("Handshake attack cleanup...")
    assert c.current.cleanup_pending
    c.handle("Handshake attack task forcefully stopped.")
    assert c.current.state == "error" and not c.current.active
    c.handle(wire(status("stopped", seq=5)))
    assert c.current.state == "error"
