"""Synthetic C5 jobs host-core tests; no serial hardware is opened."""
import base64
import json
from types import SimpleNamespace as NS

import pytest

from watchdogs.c5_jobs import (
    encode_job_start, parse_device_info, parse_job_record, validate_job_request,
)
from watchdogs.device_registry import DeviceRegistry, supported_candidates
from watchdogs.device_session import DeviceSession
from watchdogs.job_controller import JobController
from watchdogs.radio_scheduler import CapacityError, RadioScheduler


DEVICE_ID = "A1B2C3D4E5F6"
BOOT_ID = "0123456789ABCDEF"


def info(device_id=DEVICE_ID, boot_id=BOOT_ID, board="xiao", channels=None, **overrides):
    data = {
        "v": 3, "kind": "device_info", "device_id": device_id, "boot_id": boot_id,
        "board": board, "flash_bytes": 8_388_608, "psram_bytes": 0,
        "operations": ["wardrive", "hs_sniff"],
        "channels": channels if channels is not None else [1, 6, 11, 36, 40],
        "limits": {"targets": 8, "exclusions": 32, "batch": 32, "aps": 128,
                   "clients": 64, "probes": 64},
        "updates": ["usb-ota", "rom"],
    }
    data.update(overrides)
    return data


def wire(d):
    return "WDG:" + json.dumps(d, separators=(",", ":"))


def started(seq=1, **extra):
    return wire({"v": 3, "kind": "started", "device_id": DEVICE_ID,
                 "boot_id": BOOT_ID, "session": "token", "seq": seq,
                 "batch": 0, "state": "running", "wifi_count": 0,
                 "ble_count": 0, "drops": 0, **extra})


def target(mac="00:11:22:33:44:55", channel=1):
    return {"bssid": mac, "channel": channel, "auth": 3, "ssid_hex": "4142"}


class FakeSerial:
    is_open = True

    def __init__(self, lines=()):
        self.lines = list(lines)
        self.commands = []

    def read_available(self, *, max_bytes=16384, max_lines=64):
        self.read_bounds = max_bytes, max_lines
        lines, self.lines = self.lines[:max_lines], self.lines[max_lines:]
        return lines

    def send_command(self, command):
        self.commands.append(command)


def test_device_info_validation_and_job_start_encoding():
    parsed = parse_device_info(wire(info()))
    assert parsed["device_id"] == DEVICE_ID
    request = {"boot_id": BOOT_ID, "op": "wardrive", "band": "wifi24",
               "channels": [1, 6], "targets": [target()]}
    command = encode_job_start("abc_123", request, boot_id=BOOT_ID)
    prefix, token, payload = command.split(" ")
    assert (prefix, token) == ("job_start", "abc_123")
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert decoded["targets"][0]["bssid"] == "00:11:22:33:44:55"
    assert decoded["boot_id"] == BOOT_ID
    invalid = info()
    invalid["limits"]["targets"] = 999
    assert parse_device_info(wire(invalid)) is None
    with pytest.raises(ValueError, match="stale boot_id"):
        validate_job_request(request, boot_id="FFFFFFFFFFFFFFFF")


def test_v3_envelope_uses_frozen_payload_validation_and_monotonic_seq():
    record = parse_job_record(started(), expected_device_id=DEVICE_ID,
                              expected_boot_id=BOOT_ID, last_seq=0)
    assert record["v"] == 3 and record["state"] == "running"
    assert parse_job_record(started(seq=1), last_seq=1) is None
    assert parse_job_record(started(seq=2, wifi_count=-1)) is None
    assert parse_job_record(started(seq=2), expected_device_id="FFFFFFFFFFFF") is None


def test_device_registry_tracks_usb_and_factory_identity_separately():
    registry = DeviceRegistry()
    port = NS(device="/dev/ttyACM0", vid=0x303A, pid=0x1001,
              serial_number="usb-unique", location="1-2", description="CDC")
    candidates = supported_candidates([port])
    assert candidates[0]["usb_identity"]["serial_number"] == "usb-unique"
    entry = registry.upsert(port.device, candidates[0]["usb_identity"], info())
    registry.assign_alias(entry.device_id, "scan-xiao")
    registry.assign_role("scan-xiao", "background-wifi")
    assert registry.identify_usb("/dev/ttyACM9", candidates[0]["usb_identity"]) is entry
    assert registry.get("scan-xiao").roles == {"background-wifi"}
    assert registry.disconnect(port.device).connected is False


def test_device_session_bounds_reads_forwards_legacy_and_drops_invalid_v3():
    serial = FakeSerial(["legacy output", started(), started(seq=1, wifi_count=-1)])
    lines, events = [], []
    session = DeviceSession(serial, "/dev/ttyACM0", info(), on_line=lines.append,
                            on_event=events.append)
    session.c5_jobs_v1 = True
    session.start_job("token", {"boot_id": BOOT_ID, "op": "wardrive",
                                "band": "wifi24", "channels": [1]})
    accepted = session.poll()
    assert serial.read_bounds == (16384, 64)
    assert lines == ["legacy output"]
    assert [event["kind"] for event in accepted] == ["started"]
    assert session.seq == 1 and session.state == "running"


def test_controller_replacement_waits_for_stop_ack():
    registry = DeviceRegistry()
    registry.upsert("/dev/a", {"serial_number": "a"}, info())
    serial = FakeSerial()
    session = DeviceSession(serial, "/dev/a", info())
    session.c5_jobs_v1 = True
    sessions = {DEVICE_ID: session}
    controller = JobController(registry, sessions)
    first = controller.start_group({DEVICE_ID: {"op": "wardrive", "boot_id": BOOT_ID,
                                                "band": "wifi24", "channels": [1]}})
    assert first and session.state == "starting"
    first_token = controller.groups[first].jobs[DEVICE_ID][0]
    controller.handle_event(DEVICE_ID, {"kind": "started", "session": first_token,
                                        "state": "running"})
    second = controller.start_group({DEVICE_ID: {"op": "wifi_scan", "boot_id": BOOT_ID,
                                                  "band": "wifi24", "channels": [1]}})
    assert second and serial.commands[-1] == "job_stop " + session.job_token
    assert not any(command.startswith("job_start") for command in serial.commands[1:])
    session._update_snapshot({"kind": "stopped", "state": "stopped"})
    controller.handle_event(DEVICE_ID, {"kind": "stopped", "session": first_token,
                                        "state": "stopped"})
    assert serial.commands[-1].startswith("job_start ")
    assert controller.active_group == second


def test_scheduler_assigns_every_target_and_emits_helper_restore_plan():
    xiao = info(board="xiao", channels=[1, 6, 11])
    zero = info("112233445566", "1111111111111111", "waveshare-zero", [36, 40])
    spare = info("223344556677", "2222222222222222", "wroom", [1, 36])
    devices = [xiao, zero, spare]
    targets = [target("00:11:22:33:44:01", 1), target("00:11:22:33:44:02", 36)]
    helper_targets = [target("00:11:22:33:44:55", 6)]
    plan = RadioScheduler().plan_wardrive(devices, targets=targets,
                                           helper_targets=helper_targets)
    assert sorted(i for values in plan["coverage"].values() for i in values) == [0, 1]
    assert plan["helper"]["mode"] == "dedicated-hs_sniff"
    assert plan["helper"]["device_id"] == "223344556677"
    assert plan["requests"][DEVICE_ID]["ble"] == "scan"
    assert plan["restore_on_clean"][DEVICE_ID]["ble"] == "scan"


def test_scheduler_rejects_incomplete_channel_capacity_and_marks_no_spare_fallback():
    only = info(channels=[1], operations=["wardrive", "hs_sniff"])
    plan = RadioScheduler().plan_wardrive(
        [only], helper_targets=[target(channel=1)])
    assert plan["helper"]["mode"] == "wardrive-wifi-no-ble"
    assert plan["requests"][DEVICE_ID]["ble"] == "off"
    assert plan["restore_on_clean"][DEVICE_ID]["ble"] == "scan"
    with pytest.raises(CapacityError, match="fully covered"):
        RadioScheduler().plan_wardrive([only], targets=[target(channel=36)])


def test_device_session_accepts_firmware_v1_capabilities_and_rejects_foreign_owner():
    caps = wire({"v": 1, "kind": "capabilities", "c5_jobs_v1": True,
                 "wardrive_serial_v1": True})
    serial = FakeSerial([caps, started()])
    session = DeviceSession(serial, "/dev/a", info())
    events = session.poll()
    assert session.c5_jobs_v1 is True
    assert events[0]["kind"] == "capabilities"
    assert all(event.get("kind") != "started" for event in events)
    session.start_job("token", {"boot_id": BOOT_ID, "op": "wardrive",
                                 "band": "wifi24", "channels": [1]})
    foreign = started(seq=1, session="another-token")
    serial.lines = [foreign, started(seq=1)]
    events = session.poll()
    assert [event["session"] for event in events] == ["token"]
    assert session.job_token == "token" and session.state == "running"


def test_legacy_capability_without_jobs_is_forwarded_and_jobs_capability_can_precede_identity():
    legacy = wire({"v": 1, "kind": "capabilities", "wardrive_serial_v1": True})
    job_caps = wire({"v": 1, "kind": "capabilities", "wardrive_serial_v1": True,
                     "c5_jobs_v1": True})
    lines, events = [], []
    serial = FakeSerial([legacy, job_caps])
    session = DeviceSession(serial, "/dev/a", on_line=lines.append, on_event=events.append)
    session.poll()
    assert lines == [legacy] and not session.c5_jobs_v1
    assert session._pending_capabilities["c5_jobs_v1"] is True
    serial.lines = [wire(info())]
    session.poll()
    assert session.c5_jobs_v1 is True
    assert any(event.get("kind") == "device_info" for event in events)
    assert any(event.get("kind") == "capabilities" for event in events)


def test_device_session_boot_change_interrupts_owner_and_keeps_terminal_cache_bounded():
    serial = FakeSerial()
    events = []
    session = DeviceSession(serial, "/dev/a", info(), on_event=events.append)
    session.c5_jobs_v1 = True
    session.start_job("owned", {"boot_id": BOOT_ID, "op": "wardrive",
                                 "band": "wifi24", "channels": [1]})
    rebooted = info(boot_id="FFFFFFFFFFFFFFFF")
    session.set_device_info(rebooted)
    assert session.state == "error" and session.job_token is None
    assert "owned" in session._terminal_tokens
    assert events[-1]["kind"] == "boot_changed"


def test_device_session_watchdog_queries_then_stops_after_seven_seconds():
    now = [10.0]
    serial = FakeSerial()
    events = []
    session = DeviceSession(serial, "/dev/a", info(), clock=lambda: now[0], on_event=events.append)
    session.c5_jobs_v1 = True
    session.start_job("owned", {"boot_id": BOOT_ID, "op": "wardrive",
                                 "band": "wifi24", "channels": [1]})
    now[0] += 7.1
    session.tick()
    assert serial.commands[-2:] == ["job_status owned", "job_stop owned"]
    assert session.state == "stopping"
    assert [event["kind"] for event in events] == ["watchdog_expired"]


def test_controller_rejects_unmanaged_foreground_and_stops_group_on_member_error():
    registry = DeviceRegistry()
    serial_a, serial_b = FakeSerial(), FakeSerial()
    a_id, b_id = DEVICE_ID, "112233445566"
    a_info, b_info = info(), info(b_id, "1111111111111111", "waveshare-zero", [36])
    registry.upsert("/dev/a", {"serial_number": "a"}, a_info)
    registry.upsert("/dev/b", {"serial_number": "b"}, b_info)
    a = DeviceSession(serial_a, "/dev/a", a_info)
    b = DeviceSession(serial_b, "/dev/b", b_info)
    a.c5_jobs_v1 = b.c5_jobs_v1 = True
    controller = JobController(registry, {a_id: a, b_id: b})
    a.state = "running"  # foreground work with no group owner
    assert controller.start_group({a_id: {"op": "wardrive", "boot_id": BOOT_ID,
                                          "band": "wifi24", "channels": [1]}}) is None
    a.state = b.state = "idle"
    gid = controller.start_group({
        a_id: {"op": "wardrive", "boot_id": BOOT_ID, "band": "wifi24", "channels": [1]},
        b_id: {"op": "wardrive", "boot_id": "1111111111111111", "band": "wifi5", "channels": [36]},
    })
    assert gid
    controller.handle_event(a_id, {"kind": "started", "session": controller.groups[gid].jobs[a_id][0],
                                   "state": "running"})
    controller.handle_event(a_id, {"kind": "error", "session": controller.groups[gid].jobs[a_id][0],
                                   "state": "running", "error": "radio failed"})
    assert controller.groups[gid].states[a_id] == "uncertain_stopping"
    assert "job_stop " in serial_b.commands[-1]


def test_error_from_replacement_predecessor_aborts_pending_job():
    registry = DeviceRegistry()
    serial = FakeSerial()
    registry.upsert("/dev/a", {"serial_number": "a"}, info())
    session = DeviceSession(serial, "/dev/a", info())
    session.c5_jobs_v1 = True
    controller = JobController(registry, {DEVICE_ID: session})
    first = {"boot_id": BOOT_ID, "op": "wardrive", "band": "wifi24", "channels": [1]}
    old_gid = controller.start_group({DEVICE_ID: ("old", first)})
    replacement = {**first, "channels": [6]}
    new_gid = controller.start_group({DEVICE_ID: ("new", replacement)})
    assert old_gid and new_gid and controller._pending is not None
    controller.handle_event(DEVICE_ID, {"kind": "error", "session": "old",
                                         "state": "running", "error": "radio failed"})
    assert controller._pending is None
    assert "job_start new" not in serial.commands
    assert controller.groups[old_gid].states[DEVICE_ID] == "uncertain_stopping"


def test_serial_manager_strict_write_propagates_partial_write_without_flush():
    from watchdogs.serial_manager import SerialManager

    class Connection:
        is_open = True

        def __init__(self, partial=False):
            self.partial = partial
            self.flushes = 0

        def write(self, payload):
            return len(payload) - 1 if self.partial else len(payload)

        def flush(self):
            self.flushes += 1

    manager = SerialManager("/dev/fake")
    conn = Connection()
    manager.serial_conn = conn
    manager.send_command_strict("clear_html")
    assert conn.flushes == 0
    manager.serial_conn = Connection(partial=True)
    with pytest.raises(OSError, match="Incomplete serial command write"):
        manager.send_command_strict("set_html chunk")


def test_scheduler_singleboard_can_plan_both_bands_and_generic_operations():
    device = info(channels=[1, 6, 11, 36, 40, 149, 153, 157, 161],
                  operations=["wardrive", "wifi_scan"])
    scheduler = RadioScheduler()
    background = scheduler.plan_wardrive([device])
    request = background["requests"][DEVICE_ID]
    assert request["band"] == "both"
    assert {149, 153, 157, 161}.issubset(request["channels"])
    assert request["dwell_ms"] == 160
    scan = scheduler.plan([device], "wifi_scan", channels=[149])
    assert scan["requests"][DEVICE_ID]["channels"] == [149]


def test_generic_scheduler_splits_wifi_bands_skips_empty_target_jobs_and_keeps_ble_single_owner():
    xiao = info(channels=[1, 6], operations=["wifi_scan", "ble_scan", "portal"])
    zero = info("112233445566", "1111111111111111", "waveshare-zero", [36, 40],
                operations=["wifi_scan", "ble_scan", "portal"])
    scheduler = RadioScheduler()
    scan = scheduler.plan([xiao, zero], "wifi_scan")
    assert scan["requests"][DEVICE_ID]["band"] == "wifi24"
    assert scan["requests"]["112233445566"]["band"] == "wifi5"
    targeted = scheduler.plan([xiao, zero], "wifi_scan", targets=[target(channel=1)])
    assert list(targeted["requests"]) == [DEVICE_ID]
    ble = scheduler.plan([xiao, zero], "ble_scan")
    assert len(ble["requests"]) == 1
    assert next(iter(ble["requests"].values()))["band"] == "none"
    portal = scheduler.plan([xiao, zero], "portal", targets=[target(channel=1)])
    assert len(portal["requests"]) == 1


def test_registry_usb_identity_replacement_evicts_stale_factory_mapping():
    registry = DeviceRegistry()
    usb = {"serial_number": "same-physical-board", "vid": 0x303A, "pid": 0x1001}
    old = registry.upsert("/dev/ttyACM0", usb, info())
    new_id = "FFEEDDCCBBAA"
    new = registry.upsert("/dev/ttyACM1", usb, info(new_id, "FFFFFFFFFFFFFFFF"))
    assert registry.identify_usb("/dev/ttyACM9", usb) is new
    assert old.connected is False
    assert registry.by_endpoint("/dev/ttyACM0") is None


def test_scheduler_does_not_take_a_radio_from_two_foreground_target_jobs_for_helper():
    xiao = info(channels=[1, 6], operations=["wardrive", "hs_sniff"])
    zero = info("112233445566", "1111111111111111", "waveshare-zero", [36],
                operations=["wardrive", "hs_sniff"])
    plan = RadioScheduler().plan_wardrive(
        [xiao, zero], targets=[target(channel=1), target("00:11:22:33:44:66", 36)],
        helper_targets=[target(channel=6)])
    assert plan["helper"]["mode"] == "unavailable-both-foreground"
    assert all(request["op"] == "wardrive" for request in plan["requests"].values())


def test_stop_timeout_queries_and_retries_bounded_without_renewing_lease():
    now = [0.0]
    serial = FakeSerial()
    events = []
    session = DeviceSession(serial, "/dev/a", info(), clock=lambda: now[0],
                            on_event=events.append)
    session.c5_jobs_v1 = True
    session.start_job("owned", {"boot_id": BOOT_ID, "op": "wardrive",
                                 "band": "wifi24", "channels": [1]})
    assert session.stop_job()
    for _ in range(3):
        now[0] += 5.1
        session.tick()
    stops = [command for command in serial.commands if command == "job_stop owned"]
    assert len(stops) == 3  # Initial request plus exactly two retries.
    assert not any(command.startswith("job_keepalive ") for command in serial.commands)
    assert session.job_token == "owned" and session.state == "stopping"
    assert [event["kind"] for event in events].count("stop_timeout") == 1
    now[0] += 10
    session.tick()
    assert len([command for command in serial.commands if command == "job_stop owned"]) == 3


def test_false_stop_send_reports_failure_and_retains_owner_for_retry():
    class RejectStop(FakeSerial):
        def send_command(self, command):
            super().send_command(command)
            if command.startswith("job_stop "):
                return False

    now = [1.0]
    serial, events = RejectStop(), []
    session = DeviceSession(serial, "/dev/a", info(), clock=lambda: now[0],
                            on_event=events.append)
    session.c5_jobs_v1 = True
    session.start_job("owned", {"boot_id": BOOT_ID, "op": "wardrive",
                                 "band": "wifi24", "channels": [1]})
    assert session.stop_job() is False
    assert session.job_token == "owned" and session.active
    assert any(event["kind"] == "stop_send_failed" for event in events)
    now[0] += 5.1
    session.tick()
    assert serial.commands[-1] == "job_stop owned"


def test_controller_reports_false_stop_send_without_releasing_group_owner():
    class RejectStop(FakeSerial):
        def send_command(self, command):
            super().send_command(command)
            return False if command.startswith("job_stop ") else None

    registry = DeviceRegistry()
    serial = RejectStop()
    registry.upsert("/dev/a", {"serial_number": "a"}, info())
    session = DeviceSession(serial, "/dev/a", info())
    session.c5_jobs_v1 = True
    events = []
    controller = JobController(registry, {DEVICE_ID: session},
                               on_event=lambda name, group, details: events.append((name, details)))
    request = {"boot_id": BOOT_ID, "op": "wardrive", "band": "wifi24", "channels": [1]}
    gid = controller.start_group({DEVICE_ID: ("owned", request)})
    assert gid and session.job_token == "owned"
    assert controller.stop_group(gid) is False
    assert session.job_token == "owned" and session.state == "stopping"
    assert any(name == "stop_failed" for name, _details in events)


def test_controller_attempts_compensating_stop_even_if_status_query_throws():
    class FailingStartAndStatus(FakeSerial):
        def send_command(self, command):
            super().send_command(command)
            if command.startswith(("job_start ", "job_status ")):
                raise OSError("simulated write failure")

    serial = FailingStartAndStatus()
    registry = DeviceRegistry()
    registry.upsert("/dev/a", {"serial_number": "a"}, info())
    session = DeviceSession(serial, "/dev/a", info())
    session.c5_jobs_v1 = True
    controller = JobController(registry, {DEVICE_ID: session})
    request = {"boot_id": BOOT_ID, "op": "wardrive", "band": "wifi24", "channels": [1]}
    gid = controller.start_group({DEVICE_ID: ("safe_token", request)})
    assert gid
    assert any(command == "job_stop safe_token" for command in serial.commands)
    assert session.job_token == "safe_token" and session.active
    assert controller.groups[gid].states[DEVICE_ID] == "uncertain_stopping"


def test_controller_rejects_bad_supplied_token_before_any_device_mutation():
    registry = DeviceRegistry()
    serial = FakeSerial()
    registry.upsert("/dev/a", {"serial_number": "a"}, info())
    session = DeviceSession(serial, "/dev/a", info())
    session.c5_jobs_v1 = True
    events = []
    controller = JobController(registry, {DEVICE_ID: session}, on_event=lambda *args: events.append(args))
    request = {"boot_id": BOOT_ID, "op": "wardrive", "band": "wifi24", "channels": [1]}
    assert controller.start_group({DEVICE_ID: ("bad token", request)}) is None
    assert serial.commands == [] and session.job_token is None
    assert events[-1][0] == "rejected"


def test_device_session_never_falls_back_to_unbounded_reader_signature():
    class UnboundedOnly:
        is_open = True

        def __init__(self):
            self.called = False

        def read_available(self):
            self.called = True
            return ["must not be consumed"]

    serial = UnboundedOnly()
    session = DeviceSession(serial, "/dev/a", info())
    with pytest.raises(TypeError):
        session.poll()
    assert serial.called is False


def test_numeric_target_contract_and_operation_specific_admission():
    request = {'boot_id':BOOT_ID,'op':'handshake','band':'wifi24','channels':[1],
               'ble':'off','targets':[target()], 'exclude':['02:66:77:88:99:AA'],
               'options':{'storage':'serial','scope':'selected'}}
    assert validate_job_request(request)['targets'][0]['auth'] == 3
    broken = json.loads(json.dumps(request))
    broken['targets'][0]['auth'] = 'WPA2'
    with pytest.raises(ValueError,match='auth'):
        validate_job_request(broken)
    broken = json.loads(json.dumps(request))
    broken['exclude'] = ['00:11:22:33:44:55']
    with pytest.raises(ValueError,match='protected'):
        validate_job_request(broken)
    devices = [info(operations=['sae'],operation_limits={'sae':{'targets':1}})]
    with pytest.raises(CapacityError):
        RadioScheduler().plan(devices,'sae', targets=[target(), target('02:11:22:33:44:66')])


def test_busy_response_cannot_confirm_new_job_and_existing_owner_retry_is_preserved():
    serial = FakeSerial()
    events = []
    session = DeviceSession(serial, '/dev/fake', info(), on_event=events.append)
    session.c5_jobs_v1 = True
    session.start_job('token', {'boot_id':BOOT_ID, 'op':'wardrive',
                               'band':'wifi24', 'channels':[1]})
    serial.lines.append(started(kind='status', request_error='external_busy'))
    session.poll()
    assert session.job_token is None and session.state == 'error' and session.external_busy
    assert events[-1]['kind'] == 'error'
    session.tick()
    assert serial.commands[-1] == 'get_device_info'
    session.set_device_info(info(legacy_busy=False))
    session.start_job('next', {'boot_id':BOOT_ID, 'op':'wardrive',
                              'band':'wifi24', 'channels':[1]})
    serial.lines.append(started(session='next', seq=2))
    session.poll()
    serial.lines.append(started(kind='status', session='next', seq=3, request_error='token_conflict'))
    session.poll()
    assert session.job_token == 'next' and session.state == 'running'


def test_channel_status_starting_does_not_announce_running():
    serial = FakeSerial([started(kind='status', state='starting', channel=6)])
    session = DeviceSession(serial, '/dev/fake', info())
    session.c5_jobs_v1 = True
    session.start_job('token', {'boot_id':BOOT_ID, 'op':'wardrive',
                               'band':'wifi24', 'channels':[6]})
    session.poll()
    assert session.state == 'starting' and session.job_token == 'token'
    assert session.applied_channel == 6
    serial.lines.append(started(kind='heartbeat',seq=2))
    session.poll()
    assert session.applied_channel == 6
    assert parse_job_record(started(kind='status',channel=True)) is None
    assert parse_job_record(started(kind='status',channel=199)) is None


def test_named_losses_are_validated_monotonic_and_reset_for_new_job():
    losses = {'queue': 4, 'age': 2, 'output': 1, 'saturation': 3}
    assert parse_job_record(started(losses=losses))['losses'] == losses
    for invalid in ({**losses, 'queue': True}, {**losses, 'age': -1},
                    {**losses, 'output': 2**32}, {'queue': 1}):
        assert parse_job_record(started(losses=invalid)) is None
    serial = FakeSerial([started(losses=losses, drops=10)])
    session = DeviceSession(serial, '/dev/fake', info())
    session.c5_jobs_v1 = True
    request = {'boot_id': BOOT_ID, 'op': 'wardrive', 'band': 'wifi24', 'channels': [1]}
    session.start_job('token', request)
    session.poll()
    assert session.losses == losses
    serial.lines.append(started(seq=2, kind='heartbeat', losses=dict.fromkeys(losses, 0)))
    session.poll()
    assert session.losses == losses
    serial.lines.append(started(seq=3, kind='stopped', state='stopped', losses=losses))
    session.poll()
    session.start_job('next', request)
    assert session.losses == dict.fromkeys(losses, 0)


def test_device_memory_limits_and_full_wire_size_are_checked():
    limits = {'observation_queue': 64, 'hs_frames': 8, 'max_frame_bytes': 2304,
              'job_internal_bytes': 49152, 'min_internal_heap': 32768}
    assert parse_device_info(wire(info(memory_limits=limits)))['memory_limits'] == limits
    assert parse_device_info(wire(info(memory_limits={'hs_frames': True}))) is None
    oversized = info(padding='x' * 1024)
    assert parse_device_info(wire(oversized)) is None
