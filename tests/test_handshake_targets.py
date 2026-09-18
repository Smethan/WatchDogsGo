"""Synthetic-only tests for optional active-capture target selection."""
import json
import sys
from unittest.mock import Mock

import pytest

from watchdogs.handshake_capture import COMMANDS
from watchdogs.handshake_targets import (
    MAX_EXCLUSIONS,
    MAX_NETWORKS,
    MAX_TARGETS,
    HandshakeTargets,
    parse_target_record,
)
from watchdogs.serial_manager import SerialLineBuffer
from test_wardrive import game, loot


def hst(kind, token="scan_token", **extra):
    record = {"v": 1, "kind": kind, "scan": token}
    record.update(extra)
    return "HST:" + json.dumps(record, separators=(",", ":"))


def ap(seq, bssid=None, name=None, rssi=-60, channel=6, auth=3, token="scan_token"):
    bssid = bssid or f"02:00:00:00:00:{seq:02X}"
    name = name if name is not None else f"Network {seq}"
    return hst(
        "ap",
        token,
        seq=seq,
        bssid=bssid,
        ssid_hex=name.encode().hex(),
        channel=channel,
        rssi=rssi,
        auth=auth,
    )


def ready_target(now, rows, token="scan_token"):
    target = HandshakeTargets(lambda: now[0])
    target.token = token
    target.state = "scanning"
    target.accept(parse_target_record(hst("scan_started", token)))
    for row in rows:
        target.accept(parse_target_record(row))
    target.accept(parse_target_record(hst("scan_done", token, count=len(rows))))
    assert target.state == "ready"
    return target


def test_hst_records_survive_fragmented_serial_input_and_sanitize_names():
    lines = [
        hst("scan_started"),
        ap(1, bssid="02:aa:bb:cc:dd:01", name="Cafe\nWiFi"),
        hst("scan_done", count=1),
    ]
    stream = ("\r\n".join(lines) + "\n").encode()
    buffer = SerialLineBuffer()
    decoded = []
    for start in range(0, len(stream), 7):
        decoded.extend(buffer.feed(stream[start : start + 7]))
    records = [parse_target_record(line) for line in decoded]
    assert [record["kind"] for record in records] == ["scan_started", "ap", "scan_done"]
    assert records[1]["bssid"] == "02:AA:BB:CC:DD:01"
    assert records[1]["name"] == "Cafe WiFi"


@pytest.mark.parametrize(
    "line",
    [
        "HST:null",
        "HST:[]",
        "HST:{",
        hst("scan_started", token="bad token"),
        hst("scan_started", token="x" * 33),
        hst("unknown"),
        hst("ap", seq=True, bssid="02:00:00:00:00:01", ssid_hex="", channel=1, rssi=-60, auth=3),
        ap(1, bssid="03:00:00:00:00:01"),
        ap(1, bssid="00:00:00:00:00:00"),
        hst("ap", seq=1, bssid="02:00:00:00:00:01", ssid_hex="zz", channel=1, rssi=-60, auth=3),
        hst("ap", seq=1, bssid="02:00:00:00:00:01", ssid_hex="aa" * 33, channel=1, rssi=-60, auth=3),
        hst("scan_done", count=MAX_NETWORKS + 1),
        "HST:" + json.dumps({"v": 1, "kind": "capture_error", "storage": "other", "error": "busy"}),
        "HST:" + json.dumps({"v": 1, "kind": "capture_error", "storage": "sd", "error": "invented"}),
        ap(1) + " " * 512,
    ],
)
def test_hst_parser_rejects_malformed_or_unbounded_records(line):
    assert parse_target_record(line) is None


def test_scan_sequence_token_count_and_timeout_are_fail_closed(monkeypatch):
    now = [10.0]
    monkeypatch.setattr("watchdogs.handshake_targets.secrets.token_hex", lambda _: "new_token")
    target = HandshakeTargets(lambda: now[0])
    command = target.prepare_scan()
    assert command == "hs_scan new_token" and target.state == "waiting"
    target.accept(parse_target_record(hst("scan_started", "old_token")))
    assert not target.started
    assert not target.dispatched("hs_scan old_token")
    assert target.dispatched(command)
    target.accept(parse_target_record(hst("scan_started", "new_token")))
    target.accept(parse_target_record(ap(2, token="new_token")))
    assert target.state == "error" and not target.rows

    target.prepare_scan()
    assert target.dispatched("hs_scan new_token")
    target.accept(parse_target_record(hst("scan_started", "new_token")))
    target.accept(parse_target_record(ap(1, token="new_token")))
    target.accept(parse_target_record(hst("scan_done", "new_token", count=0)))
    assert target.state == "error"

    target.prepare_scan()
    now[0] += 61
    assert target.tick() and target.state == "error"
    assert "timed out" in target.error.lower()


def test_filter_sort_and_hidden_checked_selection_survives():
    now = [1.0]
    rows = [
        ap(1, name="Zulu", rssi=-45),
        ap(2, name="alpha guest", rssi=-80),
        ap(3, name="Alpha Main", rssi=-55),
        ap(4, name="Beta", rssi=-35),
    ]
    target = ready_target(now, rows)
    target.toggle("02:00:00:00:00:01")
    target.query = "ALPHA"
    assert [row["name"] for row in target.visible()] == ["Alpha Main", "alpha guest"]
    assert target.draft == {"02:00:00:00:00:01"}
    target.min_rssi = -60
    assert [row["name"] for row in target.visible()] == ["Alpha Main"]
    target.query = ""
    assert [row["name"] for row in target.visible()] == ["Beta", "Zulu", "Alpha Main"]
    target.sort_name = True
    assert [row["name"] for row in target.visible()] == ["Alpha Main", "Beta", "Zulu"]
    assert target.draft == {"02:00:00:00:00:01"}


def test_selection_limit_and_non_wpa_or_whitelisted_rejection():
    now = [1.0]
    rows = [ap(i) for i in range(1, MAX_TARGETS + 2)]
    target = ready_target(now, rows)
    for i in range(1, MAX_TARGETS + 1):
        target.toggle(f"02:00:00:00:00:{i:02X}")
    with pytest.raises(ValueError, match="up to 16"):
        target.toggle(f"02:00:00:00:00:{MAX_TARGETS + 1:02X}")

    target = ready_target(now, [ap(1, auth=0), ap(2, auth=1), ap(3, auth=3)])
    with pytest.raises(ValueError, match="no WPA handshake"):
        target.toggle("02:00:00:00:00:01")
    with pytest.raises(ValueError, match="no WPA handshake"):
        target.toggle("02:00:00:00:00:02")
    with pytest.raises(ValueError, match="whitelisted"):
        target.toggle("02:00:00:00:00:03", lambda _: True)
    target.toggle("02:00:00:00:00:03")
    with pytest.raises(ValueError, match="whitelisted"):
        target.apply("sd", lambda _: True)


def test_per_storage_choices_and_selected_commands_are_independent():
    now = [20.0]
    target = ready_target(now, [ap(1), ap(2)])
    target.toggle("02:00:00:00:00:01")
    target.apply("sd")
    target.draft = {"02:00:00:00:00:02"}
    target.apply("serial")
    assert target.command("sd", True) == (
        "start_handshake_scope sd scan_token 02:00:00:00:00:01"
    )
    assert target.command("serial", True) == (
        "start_handshake_scope serial scan_token 02:00:00:00:00:02"
    )
    assert target.label("sd") == "SELECTED: 1"
    assert target.label("serial") == "SELECTED: 1"

    target.choices["sd"] = None
    assert target.command("sd", True) == "start_handshake_scope sd all"
    assert target.command("sd", None) == COMMANDS["sd"]
    assert target.command("sd", False) == COMMANDS["sd"]
    assert target.command("serial", True).endswith("02:00:00:00:00:02")


@pytest.mark.parametrize("storage", ["sd", "serial"])
def test_all_nearby_sends_whitelist_exclusions_and_fails_closed(storage):
    target = HandshakeTargets()
    blocked = ["02:00:00:00:00:02", "02:00:00:00:00:01",
               "02:00:00:00:00:02"]
    assert target.command(storage, True, exclusions_supported=True,
                          excluded=blocked) == (
        f"start_handshake_scope {storage} all-except "
        "02:00:00:00:00:02,02:00:00:00:00:01"
    )
    with pytest.raises(ValueError, match="firmware 1.7.11"):
        target.command(storage, True, exclusions_supported=False,
                       excluded=blocked)
    with pytest.raises(ValueError, match="invalid BSSID"):
        target.command(storage, True, exclusions_supported=True,
                       excluded=["not-a-mac"])
    too_many = [f"02:00:00:00:{i // 256:02X}:{i % 256:02X}"
                for i in range(MAX_EXCLUSIONS + 1)]
    with pytest.raises(ValueError, match="up to 32"):
        target.command(storage, True, exclusions_supported=True,
                       excluded=too_many)


@pytest.mark.parametrize("storage", ["sd", "serial"])
def test_capture_screen_protects_wifi_whitelist_in_all_mode(game, monkeypatch, storage):
    screen = game.wardrive.capture_screen
    game.wardrive.scan.capture_targets_supported = True
    game.wardrive.scan.capture_exclusions_supported = True
    game._whitelist.entries = [
        type("Entry", (), {"type": "wifi", "mac": "02:00:00:00:00:01"})(),
        type("Entry", (), {"type": "ble", "mac": "02:00:00:00:00:02"})(),
    ]
    game._is_running = Mock(return_value=False)
    screen.show(storage)
    px = sys.modules["pyxel"]
    monkeypatch.setattr(px, "btnp", lambda key: key == px.KEY_RETURN)
    screen.update()
    assert game._pending_cmd == (
        f"start_handshake_scope {storage} all-except 02:00:00:00:00:01"
    )


def test_selected_mode_never_falls_back_to_all_after_capability_or_snapshot_change():
    now = [30.0]
    target = ready_target(now, [ap(1)])
    target.toggle("02:00:00:00:00:01")
    target.apply("serial")
    for supported in (None, False):
        with pytest.raises(ValueError, match="firmware 1.7.9"):
            target.command("serial", supported)
    with pytest.raises(ValueError, match="whitelisted"):
        target.command("serial", True, lambda _: True)

    now[0] += 301
    assert "rescan needed" in target.label("serial")
    with pytest.raises(ValueError, match="new scan"):
        target.command("serial", True)

    now[0] -= 301
    target.disconnect()
    assert "rescan needed" in target.label("serial")
    with pytest.raises(ValueError, match="new scan"):
        target.command("serial", True)
    assert target.choices["serial"] is not None


@pytest.mark.parametrize("storage", ["sd", "serial"])
def test_picker_scan_select_apply_and_start_for_both_capture_modes(game, monkeypatch, storage):
    screen = game.wardrive.capture_screen
    target = game.wardrive.targets
    game.wardrive.scan.capture_targets_supported = True
    game._is_running = Mock(return_value=False)
    px = sys.modules["pyxel"]

    def press(key):
        monkeypatch.setattr(px, "btnp", lambda candidate: candidate == key)
        screen.update()

    screen.show(storage)
    press(px.KEY_N)
    assert screen.picker
    press(px.KEY_R)
    command = game._pending_cmd
    token = target.token
    assert command == "hs_scan " + token
    assert game.serial.send_command.call_args.args == ("stop",)

    game.wardrive.handle_line("All operations stopped.")
    assert target.state == "scanning"
    assert game.serial.send_command.call_args.args == (command,)
    for line in (
        hst("scan_started", token),
        ap(1, name="Owned lab", token=token),
        hst("scan_done", token, count=1),
    ):
        assert game.wardrive.handle_line(line)
    assert target.state == "ready"

    press(px.KEY_SPACE)
    assert target.draft == {"02:00:00:00:00:01"}
    press(px.KEY_RETURN)
    assert not screen.picker
    assert target.label(storage) == "SELECTED: 1"

    press(px.KEY_RETURN)
    selected = f"start_handshake_scope {storage} {token} 02:00:00:00:00:01"
    assert game._pending_cmd == selected
    game.wardrive.handle_line("All operations stopped.")
    assert game.serial.send_command.call_args.args == (selected,)
    assert game.wardrive.capture.storage == storage
    assert game.wardrive.capture.current.scope == "SELECTED: 1"
    assert game.wardrive.capture.current.state == "starting"


def test_capture_error_stops_starting_run_without_switching_scope(game):
    target = game.wardrive.targets
    target.choices["serial"] = ("kept_token", ("02:00:00:00:00:01",))
    command = "start_handshake_scope serial kept_token 02:00:00:00:00:01"
    game.wardrive.capture.start(command)
    game.capturing_hs = True
    error = "HST:" + json.dumps(
        {"v": 1, "kind": "capture_error", "storage": "serial", "error": "scan_expired"},
        separators=(",", ":"),
    )
    assert game.wardrive.handle_line(error)
    assert game.wardrive.capture.current.state == "error"
    assert not game.capturing_hs
    assert target.choices["serial"] is not None
    assert "expired" in game.wardrive.capture.current.note.lower()
