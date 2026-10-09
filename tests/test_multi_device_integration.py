"""End-to-end host runtime tests with two fake USB serial endpoints."""
import json
from pathlib import Path
import threading
from types import SimpleNamespace as NS

import pytest

from watchdogs.multi_device import MultiDeviceRuntime


IDS = ("A1B2C3D4E5F6", "112233445566")
BOOTS = ("0123456789ABCDEF", "1111111111111111")


def wire(record):
    return "WDG:" + json.dumps(record, separators=(",", ":"))


def device_info(index, board, channels):
    return wire({
        "v": 3, "kind": "device_info", "device_id": IDS[index],
        "boot_id": BOOTS[index], "board": board, "flash_bytes": 8388608,
        "psram_bytes": 0, "operations": ["wardrive", "wifi_scan", "hs_sniff", "handshake",
                                          "portal", "evil_twin"],
        "channels": channels,
        "limits": {"targets": 8, "exclusions": 32, "batch": 32, "aps": 128,
                   "clients": 64, "probes": 64},
        "updates": ["usb-ota", "rom"],
    })


def capabilities(c5=True):
    record = {"v": 1, "kind": "capabilities", "wardrive_serial_v1": True}
    if c5:
        record["c5_jobs_v1"] = True
    return wire(record)


def lifecycle(kind, device, token, seq=1, **extra):
    return wire({"v": 3, "kind": kind, "device_id": IDS[device], "boot_id": BOOTS[device],
                 "session": token, "seq": seq, "batch": 0, "state": "running",
                 "wifi_count": 0, "ble_count": 0, "drops": 0, **extra})


class FakeSerial:
    def __init__(self, endpoint, usb_serial, lines=()):
        self.device = endpoint
        self.usb_port_info = NS(vid=0x303A, pid=0x1001, serial_number=usb_serial,
                                location=usb_serial, description="fake USB serial")
        self.is_open = True
        self.lines = list(lines)
        self.commands = []
        self.read_bounds = []

    def send_command(self, command):
        self.commands.append(command)

    def send_command_strict(self, command):
        self.commands.append(command)

    def read_available(self, *, max_bytes=16384, max_lines=64):
        self.read_bounds.append((max_bytes, max_lines))
        result, self.lines = self.lines[:max_lines], self.lines[max_lines:]
        return result

    def close(self):
        self.is_open = False


class FakeLoot:
    def __init__(self, path):
        self.active = True
        self.session_path = Path(path)
        self.session_path.mkdir(parents=True, exist_ok=True)
        self.sniffer_aps = []
        self.sniffer_probes = []

    def save_sniffer_aps(self, values):
        self.sniffer_aps.extend(values)

    def save_sniffer_probes(self, values):
        self.sniffer_probes.extend(values)


class FakeWardrive:
    def __init__(self):
        self.settings = {"c5_devices": {}}
        self.legacy_scan = NS(records=[])
        self.legacy_scan.handle = self.legacy_scan.records.append
        self.scan = self.legacy_scan
        self.observations = []
        self.unique_wifi = {}
        self.live_oui_records = []
        self.host_ble_starts = []
        self.targets = NS(rows={}, state="idle", completed=0, token="", started=False)
        self.detector = NS(oui_prefixes=lambda: [])
        self.host_ble = NS(start=lambda *args, **kwargs: self.host_ble_starts.append((args,kwargs)) or True)
        self.meshcore_ble_blocks_host_scan = lambda: False
        self._host_ble_requires_lease = lambda: False
        self.start_cell = lambda: None
        self.start_auxiliary_collectors = lambda: None
        self.host_ble_error = lambda _message: None

    def observation(self, event, terminal=False):
        self.observations.append((dict(event), terminal))
        if event.get("kind") == "wifi":
            self.unique_wifi.setdefault(event["mac"], event)

    def live_oui(self, event):
        self.live_oui_records.append(dict(event))


class FakeApp:
    def __init__(self, loot=None):
        self.serial = None
        self._esp32 = False
        self.state = NS(connected=False)
        self._whitelist = NS(entries=[])
        self.wardrive = FakeWardrive()
        self.loot = loot
        self.messages = []
        self.running = {}
        self.lines = []
        self.hs_events = 0
        self._serial_busy = False
        self._et_scan_pending = False
        self._portal_ssid = ""

    def _term_add(self, line, raw=False):
        self.lines.append((line, raw))

    def msg(self, message, _duration=0):
        self.messages.append(message)

    def _set_running(self, key, running):
        self.running[key] = running

    def _handle_serial_line(self, line, **_kwargs):
        self.lines.append((line, False))

    def _trigger_hs_event(self):
        self.hs_events += 1


class Harness:
    def __init__(self, tmp_path, boards=("xiao", "waveshare-zero"), *, loot=False):
        self.now = 10.0
        app = FakeApp(FakeLoot(tmp_path) if loot else None)
        channels = ([1, 6, 11, 36, 40] if boards[0] == "xiao" else [36, 40, 44, 149])
        second_channels = ([36, 40, 44, 149] if boards[1] == "waveshare-zero" else [1, 6, 11, 36])
        self.serials = [
            FakeSerial("/dev/fake0", "usb-0", [capabilities(), device_info(0, boards[0], channels)]),
            FakeSerial("/dev/fake1", "usb-1", [capabilities(), device_info(1, boards[1], second_channels)]),
        ]
        self.app = app
        self.runtime = MultiDeviceRuntime(app, self.serials, clock=lambda: self.now)
        self.runtime.poll()

    def token(self, index):
        entry = self.runtime.registry.get(IDS[index])
        assert entry and entry.session.job_token
        return entry.session.job_token

    def emit(self, index, line):
        self.serials[index].lines.append(line)
        self.runtime.poll()

    def start_ack(self, index, *, token=None, seq=1):
        self.emit(index, lifecycle("started", index, token or self.token(index), seq,
                                   state="running"))

    def stop_ack(self, index, *, token=None, seq=1):
        self.emit(index, lifecycle("stopped", index, token or self.token(index), seq,
                                   state="stopped"))


def test_two_device_wardrive_negotiation_split_observation_dedup_and_provenance(tmp_path):
    h = Harness(tmp_path, loot=True)
    h.app.gps_fix = True
    h.app.player_lat, h.app.player_lon = 44.5, -89.25
    assert {entry.device_id for entry in h.runtime.devices()} == set(IDS)
    assert all(serial.read_bounds[-1] == (16384, 64) for serial in h.serials)
    assert h.runtime.start("wardrive", passive=True)
    group = h.runtime.groups[h.runtime.background]
    xiao, zero = group.requests[IDS[0]], group.requests[IDS[1]]
    assert xiao["band"] == "wifi24" and xiao["ble"] == "scan"
    assert zero["band"] == "wifi5" and zero["ble"] == "off"
    h.start_ack(0)
    h.start_ack(1)

    for index, rssi in ((0, -70), (1, -45)):
        token = group.tokens[IDS[index]]
        record = {"v": 3, "kind": "wifi", "device_id": IDS[index], "boot_id": BOOTS[index],
                  "session": token, "seq": 2, "batch": 0, "capture_ms": 50,
                  "age_ms": 10, "rssi": rssi, "mac": "AA:BB:CC:DD:EE:01",
                  "ssid_hex": "4E6574", "channel": 6 if index == 0 else 36,
                  "auth": "WPA2_PSK"}
        h.emit(index, wire(record))
    assert len(h.app.wardrive.observations) == 2
    assert len(h.app.wardrive.unique_wifi) == 1
    assert h.app.wardrive.unique_wifi["AA:BB:CC:DD:EE:01"]["rssi"] == -70
    rows = [json.loads(line) for line in (tmp_path / "device_provenance.jsonl").read_text().splitlines()]
    assert {row["device_id"] for row in rows} == set(IDS)
    assert {row["job_token"] for row in rows} == set(group.tokens.values())
    assert all(row["gps_valid"] is True and row["latitude"] == 44.5
               and row["longitude"] == -89.25 for row in rows)


def test_same_capture_packet_ids_remain_isolated_per_device(tmp_path):
    h = Harness(tmp_path, loot=True)
    assert h.runtime.start("hs_sniff")
    group = h.runtime.groups[h.runtime.foreground]
    h.start_ack(0)
    h.start_ack(1)
    assert len(h.runtime.captures) == 2
    for index in (0, 1):
        token = group.tokens[IDS[index]]
        frame = bytes([index + 1]) * 241
        for seq, offset in ((2, 0), (3, 240)):
            chunk = frame[offset:offset + 240]
            packet = {"v": 3, "kind": "hs_packet", "device_id": IDS[index],
                      "boot_id": BOOTS[index], "session": token, "seq": seq,
                      "packet": 1, "offset": offset, "total": len(frame),
                      "capture_ms": 123, "age_ms": 5, "channel": 6, "rssi": -45,
                      "data_hex": chunk.hex()}
            h.emit(index, wire(packet))
    assert len(h.runtime.captures) == 2
    for index in (0, 1):
        key = (IDS[index], BOOTS[index], group.tokens[IDS[index]])
        capture = h.runtime.captures[key]
        assert capture.partial is None and capture.frames == 1
        assert capture.path.is_file()


def test_foreground_stops_acknowledged_background_then_restores_after_clean_stop(tmp_path):
    h = Harness(tmp_path)
    assert h.runtime.start("wardrive", passive=True)
    background_id = h.runtime.background
    background = h.runtime.groups[background_id]
    h.start_ack(0)
    h.start_ack(1)
    assert h.runtime.start("wifi_scan")
    foreground_id = h.runtime.foreground
    assert foreground_id != background_id
    assert all(any(command.startswith("job_stop ") for command in s.commands) for s in h.serials)
    # A stale or unrelated stop cannot release the replacement barrier.
    old_tokens = dict(background.tokens)
    h.stop_ack(0, token="foreign_token", seq=2)
    assert not any(command.startswith("job_start ") and b"wifi_scan" in command.encode()
                   for s in h.serials for command in s.commands)
    h.stop_ack(0, token=old_tokens[IDS[0]], seq=2)
    h.stop_ack(1, token=old_tokens[IDS[1]], seq=2)
    # The controller may serialize the next foreground job once its old-member
    # barrier drains; acknowledge all new member starts, then complete it.
    group = h.runtime.groups[foreground_id]
    for index, did in enumerate(IDS):
        if group.tokens[did] == old_tokens[did]:
            continue
        h.start_ack(index, token=group.tokens[did], seq=1)
    h.runtime.stop_foreground()
    for index, did in enumerate(IDS):
        token = group.tokens[did]
        h.stop_ack(index, token=token, seq=2)
    assert h.runtime.background
    restored = h.runtime.groups[h.runtime.background]
    assert restored.state == "starting"
    assert all(restored.requests[did]["ble"] == background.requests[did]["ble"] for did in IDS)


def test_active_group_disconnect_stops_survivor_and_does_not_restore(tmp_path):
    h = Harness(tmp_path)
    assert h.runtime.start("hs_sniff")
    group = h.runtime.groups[h.runtime.foreground]
    h.start_ack(0)
    h.start_ack(1)
    h.serials[0].is_open = False
    h.runtime.poll()
    assert any(command.startswith("job_stop ") for command in h.serials[1].commands)
    assert group.error and "disconnected" in group.error
    assert group.state == "stopping"
    assert not group.restore


def test_update_reservation_excludes_selected_endpoint_but_keeps_peer_polling(tmp_path):
    h = Harness(tmp_path)
    selected = h.serials[0]
    reserved = h.runtime.reserve(selected)
    assert reserved is h.runtime.endpoints[selected.device]
    peer_reads = len(h.serials[1].read_bounds)
    selected_reads = len(selected.read_bounds)
    h.runtime.poll()
    assert len(h.serials[1].read_bounds) == peer_reads + 1
    assert len(selected.read_bounds) == selected_reads


def test_legacy_capability_fallback_remains_visible_without_sharding(tmp_path):
    app = FakeApp()
    legacy = FakeSerial("/dev/legacy", "legacy-usb")
    runtime = MultiDeviceRuntime(app, [legacy], clock=lambda: 10.0)
    runtime.poll()
    # A later legacy capability probe remains routed to the selected endpoint.
    legacy.lines.append(capabilities(c5=False))
    runtime.poll()
    assert app.wardrive.scan is app.wardrive.legacy_scan
    assert any(line.startswith("WDG:") for line, _raw in app.lines)
    assert runtime.devices() == []
    runtime._legacy_line(runtime.endpoints[legacy.device], "SSID: legacy-device")
    assert app.lines[-1][0] == "SSID: legacy-device"
    assert not runtime.start("wardrive", passive=True)
    legacy.lines.append(capabilities(c5=False))
    runtime.poll()
    assert any("Legacy USB /dev/legacy" in line and "single-device fallback" in line
               for line in runtime.health_lines())


def test_selected_legacy_fallback_precedes_target_parsing_and_restores_legacy_scan(tmp_path):
    h = Harness(tmp_path)
    legacy = FakeSerial("/dev/legacy", "usb-legacy", [capabilities(c5=False)])
    h.runtime.attach(legacy)
    h.app.serial = legacy
    h.runtime.poll()
    for entry in h.runtime.devices():
        entry.device_info["operations"] = ["wardrive"]
    assert h.runtime.start("evil_twin", targets=object()) is False
    assert h.app.wardrive.scan is h.app.wardrive.legacy_scan
    # The command path also falls back before validating target arguments.
    assert h.runtime.start_command("start_deauth invalid") is False
    assert not any(command.startswith("job_start") for serial in h.serials for command in serial.commands)


def test_wifi_only_radio_plan_does_not_imply_host_ble_and_operation_mode_is_preserved(tmp_path):
    h = Harness(tmp_path)
    assert h.runtime.start("wardrive", state_key="wardriving", wifi_only=True, passive=True)
    group = h.runtime.groups[h.runtime.background]
    assert all(request["ble"] == "off" for request in group.requests.values())
    h.start_ack(0)
    h.start_ack(1)
    assert h.app.wardrive.host_ble_starts == []

    other = Harness(tmp_path / "second")
    assert other.runtime.start("wardrive", state_key="all_wardrive_host",
                               wifi_only=True, passive=True)
    for i in range(2):
        other.start_ack(i)
    assert len(other.app.wardrive.host_ble_starts) == 1

    scan = Harness(tmp_path / "third")
    assert scan.runtime.start("wifi_scan")
    scan.start_ack(0)
    scan.start_ack(1)
    assert scan.runtime.scan.mode == "wifi_scan"


def test_unknown_wifi_auth_maps_to_unknown_sentinel_not_wpa2_guess(tmp_path):
    h = Harness(tmp_path)
    assert h.runtime.start("wifi_scan")
    group = h.runtime.groups[h.runtime.foreground]
    h.start_ack(0)
    h.start_ack(1)
    record = {"v": 3, "kind": "wifi", "device_id": IDS[0], "boot_id": BOOTS[0],
              "session": group.tokens[IDS[0]], "seq": 2, "batch": 0, "capture_ms": 50,
              "age_ms": 10, "rssi": -55, "mac": "AA:BB:CC:DD:EE:22",
              "ssid_hex": "", "channel": 6, "auth": "UNKNOWN"}
    h.emit(0, wire(record))
    h.runtime.stop_foreground()
    h.stop_ack(0, token=group.tokens[IDS[0]], seq=3)
    h.stop_ack(1, token=group.tokens[IDS[1]], seq=2)
    assert h.app.wardrive.targets.rows["AA:BB:CC:DD:EE:22"]["auth"] == 255


def test_live_oui_hints_are_limited_to_wifi_scan_operations(tmp_path):
    h = Harness(tmp_path)
    assert h.runtime.start("hs_sniff", live_ouis=["AABBCC"])
    group = h.runtime.groups[h.runtime.foreground]
    assert all(request["live_ouis"] == [] for request in group.requests.values())


def test_terminal_error_retires_source_and_stops_peer(tmp_path):
    h = Harness(tmp_path)
    assert h.runtime.start("hs_sniff")
    group = h.runtime.groups[h.runtime.foreground]
    h.start_ack(0)
    h.start_ack(1)
    h.emit(0, lifecycle("error", 0, group.tokens[IDS[0]], seq=2,
                        state="error", error="radio failed"))
    assert IDS[0] in group.disconnected
    assert group.state == "stopping"
    assert any(command == "job_stop " + group.tokens[IDS[1]]
               for command in h.serials[1].commands)
    h.stop_ack(1, token=group.tokens[IDS[1]], seq=2)
    assert group.state == "error"
    assert h.runtime.foreground is None


def test_boot_change_retires_old_group_source_and_stops_surviving_peer(tmp_path):
    h = Harness(tmp_path)
    assert h.runtime.start("hs_sniff")
    group = h.runtime.groups[h.runtime.foreground]
    h.start_ack(0)
    h.start_ack(1)
    changed = json.loads(device_info(0, "xiao", [1, 6, 11, 36, 40])[4:])
    changed["boot_id"] = "ABCDEF0123456789"
    h.emit(0, wire(changed))
    assert IDS[0] in group.disconnected
    assert group.error == "boot changed"
    assert any(command == "job_stop " + group.tokens[IDS[1]]
               for command in h.serials[1].commands)
    h.stop_ack(1, token=group.tokens[IDS[1]], seq=2)
    assert group.state == "error"


def test_portal_preparation_waits_for_required_passive_stops_uses_xiao_manager_and_snapshots_targets(
        tmp_path, monkeypatch):
    h = Harness(tmp_path)
    assert h.runtime.start("wardrive", passive=True)
    background = h.runtime.groups[h.runtime.background]
    h.start_ack(0)
    h.start_ack(1)
    original_targets = [{"bssid": "00:11:22:33:44:55", "channel": 36,
                         "auth": 3, "ssid_hex": "4e6574"}]
    uploads = []
    upload_started = threading.Event()
    upload_finish = threading.Event()

    def fake_upload(html, send_fn):
        uploads.append(html)
        upload_started.set()
        assert upload_finish.wait(1)
        send_fn("test_html_upload")

    from watchdogs import portals
    monkeypatch.setattr(portals, "upload_html_to_esp32", fake_upload)
    assert h.runtime.start_portal("portal", "<h1>login</h1>", "TestNet",
                                  original_targets, "portal_start")
    assert all(any(command.startswith("job_stop ") for command in serial.commands)
               for serial in h.serials)
    assert uploads == []
    original_targets[0]["bssid"] = "AA:AA:AA:AA:AA:AA"
    # Deliberately point the compatibility serial elsewhere. The pending
    # upload must still use the explicitly selected XIAO manager.
    h.app.serial = h.serials[1]
    h.stop_ack(0, token=background.tokens[IDS[0]], seq=2)
    assert uploads == []
    h.stop_ack(1, token=background.tokens[IDS[1]], seq=2)
    assert upload_started.wait(1)
    # The GUI poll remains usable while the custom HTML chunks are sending.
    peer_reads = len(h.serials[1].read_bounds)
    h.runtime.poll()
    assert len(h.serials[1].read_bounds) == peer_reads + 1
    assert h.runtime.foreground is None
    upload_finish.set()
    for _ in range(100):
        if h.runtime._portal_preparation['result'].qsize():
            break
        threading.Event().wait(0.005)
    h.runtime.poll()
    assert uploads == ["<h1>login</h1>"]
    assert "test_html_upload" in h.serials[0].commands
    assert "test_html_upload" not in h.serials[1].commands
    portal_group = h.runtime.groups[h.runtime.foreground]
    assert portal_group.operation == "portal"
    assert portal_group.requests[IDS[0]]["op"] == "portal"
    assert portal_group.requests[IDS[0]]["targets"][0]["bssid"] == "00:11:22:33:44:55"
    assert portal_group.requests[IDS[1]]["op"] == "hs_sniff"
    assert portal_group.requests[IDS[1]]["targets"][0]["bssid"] == "00:11:22:33:44:55"


def test_portal_preparation_stops_only_its_required_passive_owner(tmp_path):
    h = Harness(tmp_path)
    assert h.runtime.start("wardrive", passive=True)
    background = h.runtime.groups[h.runtime.background]
    h.start_ack(0)
    h.start_ack(1)
    # The second board is still a useful C5 endpoint, but cannot be allocated
    # as a portal helper, so its passive owner is not required to stop.
    h.runtime.registry.get(IDS[1]).device_info["operations"] = ["wifi_scan"]
    target = {"bssid": "00:11:22:33:44:55", "channel": 36,
              "auth": 3, "ssid_hex": "4e6574"}
    assert h.runtime.start_portal("portal", None, "TestNet", [target], "portal_start")
    assert any(command == "job_stop " + background.tokens[IDS[0]]
               for command in h.serials[0].commands)
    assert not any(command == "job_stop " + background.tokens[IDS[1]]
                   for command in h.serials[1].commands)
    h.stop_ack(0, token=background.tokens[IDS[0]], seq=2)
    group = h.runtime.groups[h.runtime.foreground]
    assert list(group.requests) == [IDS[0]]
    assert "clear_html" in h.serials[0].commands
    assert "clear_html" not in h.serials[1].commands


def test_portal_stop_ack_deadline_sets_error_and_never_launches_job(tmp_path):
    h = Harness(tmp_path)
    assert h.runtime.start("wardrive", passive=True)
    h.start_ack(0)
    h.start_ack(1)
    before = sum(command.startswith("job_start ") for serial in h.serials
                 for command in serial.commands)
    assert h.runtime.start_portal("portal", None, "TestNet", [], "portal_start")
    assert h.app._attack_step == "preparing"
    h.now += 10.1
    h.runtime.poll()
    after = sum(command.startswith("job_start ") for serial in h.serials
                for command in serial.commands)
    assert after == before
    assert h.runtime._portal_preparation is None
    assert h.runtime.registry.get(IDS[0]).device_id not in h.runtime.legacy_owned
    assert h.app._attack_step == "error"


def test_global_stop_cancels_pending_upload_without_later_start(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    upload_started, release_upload = threading.Event(), threading.Event()
    completed = threading.Event()

    def blocked_upload(_html, send_fn):
        upload_started.set()
        release_upload.wait(1)
        try:
            send_fn("test_html_upload")
        finally:
            completed.set()

    from watchdogs import portals
    monkeypatch.setattr(portals, "upload_html_to_esp32", blocked_upload)
    assert h.runtime.start_portal("portal", "custom html", "TestNet", [], "portal_start")
    assert upload_started.wait(1)
    before = sum(command.startswith("job_start ") for serial in h.serials
                 for command in serial.commands)
    h.runtime.stop_all()
    release_upload.set()
    assert completed.wait(1)  # Cancellation ends the worker without a job start.
    h.runtime.poll()
    after = sum(command.startswith("job_start ") for serial in h.serials
                for command in serial.commands)
    assert after == before
    assert h.runtime._portal_preparation is None
    assert not h.runtime.legacy_owned
    assert h.app._attack_step == "error"


def test_failed_strict_portal_write_prevents_any_job_start(tmp_path):
    h = Harness(tmp_path)
    owner = h.runtime.registry.get(IDS[0]).session.serial
    def partial_write(command):
        owner.commands.append(command)
        raise OSError("Incomplete serial command write")
    owner.send_command_strict = partial_write
    assert h.runtime.start_portal("portal", None, "TestNet", [], "portal_start")
    for _ in range(100):
        if h.runtime._portal_preparation['result'].qsize():
            break
        threading.Event().wait(0.005)
    h.runtime.poll()
    assert owner.commands[-1] == "clear_html"
    assert not any(command.startswith("job_start") for serial in h.serials for command in serial.commands)
    assert h.app._attack_step == "error"


def test_duplicate_exchange_reward_is_suppressed_but_each_device_artifact_is_kept(
        tmp_path, monkeypatch):
    h = Harness(tmp_path, loot=True)
    assert h.runtime.start("handshake")
    group = h.runtime.groups[h.runtime.foreground]
    h.start_ack(0)
    h.start_ack(1)

    def same_artifact(_self, _frame):
        return {"bssid": "AA:BB:CC:DD:EE:01", "station": "AA:BB:CC:DD:EE:02",
                "replay": 7, "hccapx": b"artifact"}

    monkeypatch.setattr("watchdogs.multi_device.ExchangeValidator.ingest", same_artifact)
    for index in (0, 1):
        token = group.tokens[IDS[index]]
        frame = bytes([index + 1]) * 24
        packet = {"v": 3, "kind": "hs_packet", "device_id": IDS[index],
                  "boot_id": BOOTS[index], "session": token, "seq": 2,
                  "packet": 1, "offset": 0, "total": 24, "capture_ms": 123,
                  "age_ms": 5, "channel": 6, "rssi": -45, "data_hex": frame.hex()}
        h.emit(index, wire(packet))
    assert h.app.hs_events == 1
    files = list((tmp_path / "handshakes").rglob("*.hccapx"))
    assert len(files) == 2
    assert {path.parts[-4] for path in files} == set(IDS)


def test_completed_group_and_sniffer_collector_retention_is_bounded(tmp_path):
    h = Harness(tmp_path)
    for index in range(40):
        gid = f"terminal-{index}"
        h.runtime.groups[gid] = NS(state="stopped")
        h.runtime.controller.groups[gid] = NS()
        h.runtime.sniffers[(IDS[0], gid)] = NS()
    h.runtime.foreground = "terminal-0"
    h.runtime._prune_groups()
    assert len(h.runtime.groups) == 32
    assert "terminal-0" in h.runtime.groups
    assert len(h.runtime.sniffers) == 32


def test_explicit_port_reconnect_follows_usb_identity_and_avoids_reserved_endpoint(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    runtime = h.runtime
    runtime.ports = ['/dev/fake0']
    runtime._configured_usb['/dev/fake0'] = {'vid': 0x303A, 'pid': 0x1001,
        'serial_number': 'usb-0', 'location': 'usb-0'}
    candidates = [
        {'endpoint':'/dev/fake0','usb_identity':{'vid':0x303A,'pid':0x1001,
            'serial_number':'unrelated','location':'elsewhere'}},
        {'endpoint':'/dev/fake8','usb_identity':{'vid':0x303A,'pid':0x1001,
            'serial_number':'usb-0','location':'usb-0'}},
    ]
    monkeypatch.setattr('watchdogs.multi_device.supported_candidates', lambda:candidates)
    opened = []
    def factory(endpoint):
        opened.append(endpoint)
        manager = FakeSerial(endpoint, 'usb-0', [device_info(0,'xiao',[1,6,11,36]),capabilities()])
        manager.setup = lambda:None
        return manager
    monkeypatch.setattr('watchdogs.multi_device.SerialManager', factory)
    runtime.reserved.add(IDS[0])
    runtime.discover()
    assert not opened  # ROM/OTA re-enumeration remains owned by updater.
    runtime.reserved.clear()
    runtime._disconnected(runtime.sessions[IDS[0]])
    runtime.discover()
    assert opened == ['/dev/fake8']
    runtime.poll()
    assert runtime.registry.get(IDS[0]).endpoint == '/dev/fake8'
    assert not any(command.startswith('job_start') for command in runtime.sessions[IDS[0]].serial.commands)


def test_error_during_cleanup_keeps_failed_owner_until_final_drain(tmp_path):
    h = Harness(tmp_path, loot=True)
    assert h.runtime.start('hs_sniff', state_key='hs_sniff')
    h.start_ack(0)
    h.start_ack(1)
    group = h.runtime.current
    first_token, second_token = h.token(0), h.token(1)
    h.emit(0, lifecycle('error', 0, first_token, 2, state='stopping',
                        message='radio_start_failed'))
    h.stop_ack(1, token=second_token, seq=2)
    assert h.runtime.foreground == group.identity
    assert group.state == 'stopping' and IDS[0] not in group.disconnected
    assert (IDS[0], BOOTS[0], first_token) in h.runtime.captures
    h.stop_ack(0, token=first_token, seq=3)
    assert h.runtime.foreground is None and group.state == 'error'
    assert not h.runtime.captures


def test_passive_device_disconnect_preserves_peer_job_and_records(tmp_path):
    h = Harness(tmp_path)
    assert h.runtime.start('wardrive', passive=True)
    h.start_ack(0)
    h.start_ack(1)
    group = h.runtime.current
    token = h.token(1)
    before = len(h.serials[1].commands)
    h.runtime._disconnected(h.runtime.sessions[IDS[0]])
    assert not any(command.startswith('job_stop ') for command in h.serials[1].commands[before:])
    assert h.runtime.sessions[IDS[1]].job_token == token
    assert h.runtime.background == group.identity and group.state == 'running'
    h.emit(1, lifecycle('heartbeat', 1, token, seq=2))
    assert h.runtime.sessions[IDS[1]].state == 'running'
    h.runtime.stop_all()
    h.stop_ack(1, token=token, seq=3)
    assert group.state == 'error'


def test_lost_start_and_stop_ack_status_recovery_opens_and_closes_capture(tmp_path):
    h = Harness(tmp_path, loot=True)
    assert h.runtime.start('hs_sniff')
    group = h.runtime.current
    tokens = [h.token(0), h.token(1)]
    for index in (0, 1):
        h.emit(index, lifecycle('status', index, tokens[index], 1, state='running'))
        assert (IDS[index], BOOTS[index], tokens[index]) in h.runtime.captures
    assert group.state == 'running'
    h.runtime.stop_foreground()
    for index in (0, 1):
        h.emit(index, lifecycle('status', index, tokens[index], 2, state='stopped'))
    assert not h.runtime.captures
    assert group.state == 'stopped' and h.runtime.foreground is None


def test_passive_radio_error_cleans_only_failed_member_until_global_stop(tmp_path):
    h = Harness(tmp_path)
    assert h.runtime.start('wardrive', passive=True)
    h.start_ack(0)
    h.start_ack(1)
    group = h.runtime.current
    tokens = [h.token(0), h.token(1)]
    before = len(h.serials[1].commands)
    h.emit(0, lifecycle('error', 0, tokens[0], 2, state='stopping',
                       message='channel_apply_failed'))
    assert not any(command.startswith('job_stop ') for command in h.serials[1].commands[before:])
    assert group.state == 'running' and h.runtime.sessions[IDS[1]].job_token == tokens[1]
    h.stop_ack(0, token=tokens[0], seq=3)
    assert group.state == 'running'
    h.runtime.stop_all()
    h.stop_ack(1, token=tokens[1], seq=2)
    assert group.state == 'error'


def test_external_legacy_owner_is_excluded_from_allocation_and_updates_wait_for_idle(tmp_path):
    h = Harness(tmp_path)
    metadata = json.loads(device_info(0, 'xiao', [1,6,11,36,40])[4:])
    metadata['legacy_busy'] = True
    h.emit(0, wire(metadata))
    assert {device.device_id for device in h.runtime.devices()} == {IDS[1]}
    assert h.runtime.reserve(h.serials[0]) is False
    assert h.serials[0].commands[-2:] == ['stop', 'get_device_info']
    metadata['legacy_busy'] = False
    h.emit(0, wire(metadata))
    assert h.runtime.reserve(h.serials[0]) is h.runtime.sessions[IDS[0]]
