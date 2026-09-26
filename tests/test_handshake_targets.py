"""Synthetic-only tests for optional active-capture target selection."""
import json
import sys
from unittest.mock import Mock

import pytest

from test_wardrive import game as game
from test_wardrive import loot as loot
from watchdogs.app_state import Network
from watchdogs.handshake_capture import COMMANDS
from watchdogs.handshake_targets import (
    MAX_EXCLUSIONS,
    MAX_NETWORKS,
    MAX_TARGETS,
    HandshakeTargets,
    parse_target_record,
)
from watchdogs.serial_manager import SerialLineBuffer


def hst(kind, token="scan_token", **extra):
    record = {"v": 1, "kind": kind, "scan": token}
    record.update(extra)
    return "HST:" + json.dumps(record, separators=(",", ":"))


def ap(seq, bssid=None, name=None, rssi=-60, channel=6, auth=3,
       token="scan_token", **extra):
    bssid = bssid or f"02:00:00:00:00:{seq:02X}"
    name = name if name is not None else f"Network {seq}"
    values = {
        "seq": seq,
        "bssid": bssid,
        "ssid_hex": name.encode().hex(),
        "channel": channel,
        "rssi": rssi,
        "auth": auth,
    }
    values.update(extra)
    return hst("ap", token, **values)


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


def test_hst_target_intel_is_bounded_and_published_with_scan_snapshot():
    now = [10.0]
    target = HandshakeTargets(lambda: now[0])
    target.token = "intel_scan"
    target.state = "scanning"
    target.accept(parse_target_record(hst("scan_started", "intel_scan")))
    target.accept(parse_target_record(ap(
        1, token="intel_scan", clients=3, packets=421, probes=2)))
    target.accept(parse_target_record(hst(
        "scan_done", "intel_scan", count=1, intel=True,
        packets=913, probes=7, intel_age_ms=1500)))
    assert target.state == "ready"
    row = next(iter(target.rows.values()))
    assert (row["clients"], row["packets"], row["probes"]) == (3, 421, 2)
    assert target.intel_available
    assert (target.intel_packets, target.intel_probes,
            target.intel_age_ms) == (913, 7, 1500)

    malformed = json.loads(ap(1, clients=51)[4:])
    assert parse_target_record("HST:" + json.dumps(malformed)) is None


def test_evil_twin_scan_keeps_same_ssid_metrics_bssid_keyed(game):
    target = game.wardrive.targets
    target.token = "evil_twin_scan"
    target.state = "scanning"
    game._et_scan_pending = True
    game._et_net_selected = set()
    game._attack_mode = "evil_twin"
    rows = (
        hst("scan_started", "evil_twin_scan"),
        ap(1, token="evil_twin_scan", name="Mesh", clients=1,
           packets=100, probes=3),
        ap(2, token="evil_twin_scan", name="Mesh", clients=4,
           packets=900, probes=3),
        hst("scan_done", "evil_twin_scan", count=2, intel=True,
            packets=1000, probes=3, intel_age_ms=250),
    )
    for row in rows:
        assert game.wardrive.handle_line(row)

    assert not game._et_scan_pending
    assert game._et_net_screen and game._attack_step == "select_net"
    assert [net.bssid for net in game._attack_scan_results] == [
        "02:00:00:00:00:01", "02:00:00:00:00:02"]
    assert [(net.client_count, net.packet_count)
            for net in game._attack_scan_results] == [(1, 100), (4, 900)]
    assert game.state.sniffer_packets == 1000
    assert game.state.sniffer_probe_count == 3


def test_stopped_sniffer_exports_results_only_after_final_ack(game):
    game._sniffer_results_pending = True
    game._request_sniffer_intel = Mock(return_value=True)
    game._evil_twin_starting = False
    game._evil_twin_start_deadline = 0.0
    assert game.wardrive.handle_line("All operations stopped.")
    game._request_sniffer_intel.assert_called_once_with()


def test_evil_twin_scan_error_leaves_a_dismissible_error_state(game):
    target = game.wardrive.targets
    target.token = "evil_twin_scan"
    target.state = "scanning"
    game._et_scan_pending = True
    game._attack_mode = "evil_twin"

    assert game.wardrive.handle_line(hst(
        "scan_error", "evil_twin_scan", count=0,
        error="scan_failed_or_cancelled"))

    assert not game._et_scan_pending
    assert game._attack_step == "error"
    assert target.state == "error"
    assert "scan failed" in game.msg.call_args.args[0].lower()


def test_evil_twin_confirmation_blocks_any_whitelisted_deauth_target(
        game, monkeypatch):
    game._et_net_screen = True
    game._et_net_sel = 0
    game._et_net_selected = {0, 1}
    game._portal_select_screen = False
    game._attack_scan_results = [
        Network(index="1", ssid="Clone", bssid="02:00:00:00:00:01"),
        Network(index="2", ssid="Aux", bssid="02:00:00:00:00:02"),
    ]
    game._whitelist = Mock()
    game._whitelist.is_blocked.side_effect = lambda mac: mac.endswith("02")
    game._show_portal_selection = Mock()
    px = sys.modules["pyxel"]
    monkeypatch.setattr(px, "btnp", lambda key: key == px.KEY_RETURN)

    game._update_picker_overlay()

    assert game._et_net_screen
    game._show_portal_selection.assert_not_called()
    assert "selection blocked" in game.msg.call_args.args[0]


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
