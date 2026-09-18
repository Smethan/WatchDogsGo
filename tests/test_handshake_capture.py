"""Synthetic active capture telemetry and background menu tests; no radios."""
import base64
import json
import sys
from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest

from watchdogs.handshake_capture import (
    COMMANDS,
    HandshakeCapture,
    capture_storage,
    parse_progress,
)
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


def test_all_except_command_parsing_and_scope_label():
    command = ("start_handshake_scope serial all-except "
               "02:00:00:00:00:01,02:00:00:00:00:02")
    assert capture_storage(command) == "serial"
    capture = HandshakeCapture();capture.start(command)
    assert capture.current.scope == "ALL NEARBY / 2 WHITELISTED"
    assert capture_storage("start_handshake_scope serial all-except bad") is None


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


def test_serial_artifact_kind_precedes_blocks_and_sequential_artifacts_keep_metadata(tmp_path, monkeypatch):
    """Firmware 1.7.9 classifies each artifact before its binary blocks.

    The classification line must not trigger the legacy missing-metadata
    fallback or carry data from one AP into the next artifact.
    """
    from watchdogs.loot_manager import LootManager

    loot = LootManager(str(tmp_path))
    monkeypatch.setattr(loot, "_try_generate_22000", lambda _: None)
    valid_pcap = b"P" * 64
    valid_hccapx = b"H" * 393
    partial_pcap = b"Q" * 48

    streams = (
        (
            "VALID",
            valid_pcap,
            valid_hccapx,
            "OwnedLab",
            "02:00:00:00:00:11",
        ),
        (
            "PARTIAL",
            partial_pcap,
            None,
            "GuestLab",
            "02:00:00:00:00:22",
        ),
    )
    for kind, pcap, hccapx, ssid, bssid in streams:
        lines = [
            f"CAPTURE_KIND: {kind}",
            "--- PCAP BEGIN ---",
            base64.b64encode(pcap).decode(),
            "--- PCAP END ---",
            f"PCAP_SIZE: {len(pcap)}",
        ]
        if hccapx is not None:
            lines.extend((
                "--- HCCAPX BEGIN ---",
                base64.b64encode(hccapx).decode(),
                "--- HCCAPX END ---",
            ))
        lines.append(f"SSID: {ssid}  AP: {bssid}")
        for line in lines:
            loot._detect_pcap_stream(line)

    pcaps = {path.name: path.read_bytes() for path in loot._handshake_dir.glob("*.pcap")}
    hccapx = {path.name: path.read_bytes() for path in loot._handshake_dir.glob("*.hccapx")}
    assert len(pcaps) == 2
    assert next(data for name, data in pcaps.items() if name.startswith("OwnedLab_020000000011_")) == valid_pcap
    assert next(data for name, data in pcaps.items() if name.startswith("GuestLab_020000000022_")) == partial_pcap
    assert len(hccapx) == 1
    assert next(data for name, data in hccapx.items() if name.startswith("OwnedLab_020000000011_")) == valid_hccapx
    assert not any("unknown" in name for name in (*pcaps, *hccapx))
    loot.close()


def test_serial_artifact_metadata_cannot_inject_bssid_or_path(tmp_path, monkeypatch):
    from watchdogs.loot_manager import LootManager

    loot = LootManager(str(tmp_path))
    monkeypatch.setattr(loot, "_try_generate_22000", lambda _: None)
    payload = base64.b64encode(b"P" * 40).decode()
    hccapx = base64.b64encode(b"H" * 393).decode()
    for line in (
        "--- PCAP BEGIN ---", payload, "--- PCAP END ---", "PCAP_SIZE: 40",
        "--- HCCAPX BEGIN ---", hccapx, "--- HCCAPX END ---",
        "SSID: Trap AP: ../../outside  AP: 02:00:00:00:00:33",
    ):
        loot._detect_pcap_stream(line)

    paths = list(loot._handshake_dir.glob("*"))
    assert len(paths) == 2
    assert all(path.parent == loot._handshake_dir for path in paths)
    assert all("_020000000033_" in path.name for path in paths)
    assert not any(".." in path.name or "/" in path.name for path in paths)

    # A line-split or otherwise malformed legacy commit cannot supply path
    # characters as a BSSID. The fallback may preserve it under "unknown".
    for line in (
        "--- PCAP BEGIN ---", payload, "--- PCAP END ---", "PCAP_SIZE: 40",
        "--- HCCAPX BEGIN ---", hccapx, "--- HCCAPX END ---",
        "SSID: split", "AP: ../../outside", "capture cleanup",
    ):
        loot._detect_pcap_stream(line)
    new_paths = [path for path in loot._handshake_dir.glob("*") if path not in paths]
    assert new_paths and all(path.parent == loot._handshake_dir for path in new_paths)
    assert all("_unknown_" in path.name for path in new_paths)
    loot.close()


def test_typed_serial_artifact_requires_complete_valid_transaction(tmp_path, monkeypatch):
    from watchdogs.loot_manager import LootManager, _TARGET_PCAP_MAX

    loot = LootManager(str(tmp_path))
    assert _TARGET_PCAP_MAX == 2136
    monkeypatch.setattr(loot, "_try_generate_22000", lambda _: None)
    pcap = base64.b64encode(b"P" * 40).decode()
    hccapx = base64.b64encode(b"H" * 393).decode()

    loot._detect_pcap_stream("CAPTURE_KIND: PARTIAL")
    loot._detect_pcap_stream("--- PCAP BEGIN ---")
    loot._detect_pcap_stream("SSID payload --- PCAP END ---")
    assert loot._pcap_collecting  # marker text inside an untrusted line is data
    loot._reset_pcap_stream()

    # Without the sole metadata commit, an ordinary cleanup/log line must not
    # invoke the legacy unknown-name fallback for a typed transaction.
    for line in (
        "CAPTURE_KIND: VALID",
        "--- PCAP BEGIN ---", pcap, "--- PCAP END ---", "PCAP_SIZE: 40",
        "--- HCCAPX BEGIN ---", hccapx, "--- HCCAPX END ---",
        "Handshake attack cleanup complete.",
    ):
        loot._detect_pcap_stream(line)
    assert not list(loot._handshake_dir.glob("*"))

    # A new transaction discards that incomplete predecessor. Wrong declared
    # length and malformed base64 are rejected before either file is created.
    for stream in (
        (
            "CAPTURE_KIND: PARTIAL", "--- PCAP BEGIN ---", pcap,
            "--- PCAP END ---", "PCAP_SIZE: 41",
            "SSID: BadSize  AP: 02:00:00:00:00:44",
        ),
        (
            "CAPTURE_KIND: PARTIAL", "--- PCAP BEGIN ---", "not_base64!",
            "--- PCAP END ---", "PCAP_SIZE: 40",
            "SSID: BadBase64  AP: 02:00:00:00:00:55",
        ),
        (
            "CAPTURE_KIND: VALID", "--- PCAP BEGIN ---", pcap,
            "--- PCAP END ---", "PCAP_SIZE: 40",
            "--- HCCAPX BEGIN ---", base64.b64encode(b"short").decode(),
            "--- HCCAPX END ---", "SSID: BadHccapx  AP: 02:00:00:00:00:66",
        ),
    ):
        for line in stream:
            loot._detect_pcap_stream(line)
        assert not list(loot._handshake_dir.glob("*"))

    at_limit = b"L" * _TARGET_PCAP_MAX
    for line in (
        "CAPTURE_KIND: PARTIAL", "--- PCAP BEGIN ---",
        base64.b64encode(at_limit).decode(), "--- PCAP END ---",
        f"PCAP_SIZE: {_TARGET_PCAP_MAX}",
        "SSID: AtLimit  AP: 02:00:00:00:00:77",
    ):
        loot._detect_pcap_stream(line)
    saved = list(loot._handshake_dir.glob("*.pcap"))
    assert len(saved) == 1 and saved[0].read_bytes() == at_limit

    too_large = b"X" * (_TARGET_PCAP_MAX + 1)
    for line in (
        "CAPTURE_KIND: PARTIAL", "--- PCAP BEGIN ---",
        base64.b64encode(too_large).decode(), "--- PCAP END ---",
        f"PCAP_SIZE: {_TARGET_PCAP_MAX + 1}",
        "SSID: TooLarge  AP: 02:00:00:00:00:88",
    ):
        loot._detect_pcap_stream(line)
    assert list(loot._handshake_dir.glob("*.pcap")) == saved
    loot.close()


def test_typed_artifact_events_distinguish_valid_pmkid_and_partial(game, tmp_path, monkeypatch):
    from watchdogs.loot_manager import LootManager

    game.loot = LootManager(str(tmp_path))
    monkeypatch.setattr(game.loot, "_try_generate_22000", lambda _: None)
    game._trigger_hs_event = Mock()
    game.msg = Mock()
    game._fw_version = "1.7.9"
    game.net_mgr = NS(parse_network_line=lambda _: None)
    game._bt_airtag = False

    def feed(kind, ssid, suffix, include_hccapx=False, declared=40):
        lines = [
            f"CAPTURE_KIND: {kind}",
            "--- PCAP BEGIN ---", base64.b64encode(b"P" * 40).decode(),
            "--- PCAP END ---", f"PCAP_SIZE: {declared}",
        ]
        if include_hccapx:
            lines.extend((
                "--- HCCAPX BEGIN ---", base64.b64encode(b"H" * 393).decode(),
                "--- HCCAPX END ---",
            ))
        lines.append(f"SSID: {ssid}  AP: 02:00:00:00:00:{suffix}")
        for line in lines:
            game._handle_serial_line(line)

    feed("VALID", "ValidLab", "11", include_hccapx=True)
    feed("PMKID", "PmkidLab", "22")
    feed("PARTIAL", "PartialLab", "33")
    feed("VALID", "RejectedLab", "44", include_hccapx=True, declared=41)

    game._trigger_hs_event.assert_called_once_with()
    messages = [call.args[0] for call in game.msg.call_args_list]
    assert any("PMKID capture saved" in message for message in messages)
    assert any("Partial capture saved" in message for message in messages)
    assert any("artifact rejected" in message for message in messages)
    assert len(list(game.loot._handshake_dir.glob("*.pcap"))) == 3
    assert len(list(game.loot._handshake_dir.glob("*.hccapx"))) == 1

    # Untyped metadata retains the historical game event for old firmware.
    game._handle_serial_line("SSID: LegacyLab  AP: 02:00:00:00:00:55")
    assert game._trigger_hs_event.call_count == 2
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


def test_empty_capture_with_drops_explains_usb_ring_fix(game, monkeypatch):
    w = game.wardrive
    w.capture.start(COMMANDS['serial'])
    w.capture.handle(wire(status()))
    w.capture.handle(wire(status('stats', seq=10, storage='serial')).replace('"drops":0', '"drops":9'))
    w.capture_screen.show('serial')
    game._fw_version = '1.7.5'
    px = sys.modules['pyxel']; text = Mock()
    for name in ('cls', 'rect', 'line'):
        monkeypatch.setattr(px, name, Mock())
    monkeypatch.setattr(px, 'text', text)
    w.capture_screen.draw()
    assert any('1.7.6 fixes the USB buffer' in c.args[2] for c in text.call_args_list)
