"""Synthetic Pig Detector evidence, Remote ID and map safety gates."""
import json
import math
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock

from watchdogs.notable_detector import NotableDetector
from watchdogs.c5_jobs import parse_job_record
from watchdogs.pig_detector import PigDetector, estimate_rssi_area
from watchdogs.remote_id import RemoteIdDecoder, basic_id, ble_messages, location, messages
from watchdogs.wardrive_protocol import parse_record
from watchdogs.wardrive_settings import normalize_settings
from watchdogs.wardrive_ui import WardriveUI


def ad(kind, value):
    return bytes((len(value)+1, kind)) + value


def rid_messages():
    # Basic ID bytes match OpenDroneID's Wi-Fi Beacon reference vector:
    # unit_odid_wifi_beacon.cpp, serial USS-Enterprise and ID type 1.
    basic = b"\x02\x14" + b"USS-Enterprise".ljust(20, b"\0") + b"\0"*3
    loc = bytearray(25)
    loc[0:2] = b"\x12\x20"
    loc[5:9] = int(40.1234567*1e7).to_bytes(4, "little", signed=True)
    loc[9:13] = int(-89.7654321*1e7).to_bytes(4, "little", signed=True)
    return basic, bytes(loc)


def test_field_signatures_are_evidence_graded_and_muted():
    detector = NotableDetector()
    base = dict(kind="ble", mac="C2:00:00:00:00:01", addr_type=1, event=0)
    uuid = bytes.fromhex("0000310000001000800000805f9b34fb")[::-1]
    event = dict(base, data_hex=ad(7, uuid).hex())
    hit = detector.classify(event, 1)
    assert [(h["category"], h["strength"]) for h in hit] == [("raven", 2)]
    assert not detector.classify(event, 1, suppressed_rules=["raven-gps-service"])
    assert not detector.classify(dict(base, data_hex=ad(7, uuid[:-1]).hex()), 1)
    hit = detector.classify(dict(base, data_hex=ad(9, b"BodyWorn Remote").hex()), 1)
    assert hit[0]["category"] == "utility" and hit[0]["strength"] == 2
    hit = detector.classify(dict(kind="wifi", mac="00:1D:96:11:22:33", ssid_hex=""), 1)
    assert hit[0]["category"] == "watchguard" and hit[0]["strength"] == 0
    hit = detector.classify(dict(base, data_hex=ad(9, b"Getac BC-02 test").hex()), 1)
    assert hit[0]["category"] == "bodycam_candidate" and hit[0]["strength"] == 0
    assert not detector.classify(dict(kind="wifi", mac="02:1D:96:11:22:33", ssid_hex=""), 1)
    assert not detector.classify(dict(base, data_hex=ad(9, b"Pigvision").hex()), 1)


def test_remote_id_reference_basic_and_bounded_ble_wifi_decode():
    basic, loc = rid_messages()
    assert basic_id(basic) == "USS-Enterprise"
    assert location(loc) == {"latitude": 40.1234567, "longitude": -89.7654321}
    packet = b"\xf2\x19\x02" + basic + loc
    assert messages(packet) == [basic, loc]
    assert messages(packet[:-1]) == []
    assert messages(b"\xf2\x19\x09" + basic + loc) == []
    ble = ad(0x16, b"\xfa\xff\x0d\x07" + basic)
    assert ble_messages(ble.hex()) == [basic]
    assert ble_messages(ad(0x16, b"\xfa\xff\x0d\x07" + basic[:-1]).hex()) == []
    decoder = RemoteIdDecoder()
    assert decoder.accept("12:34:56:21:43:65", [basic], now=0) is None
    decoded = decoder.accept("12:34:56:21:43:65", [loc], now=1)
    assert decoded["identity"] == "USS-Enterprise" and decoded["stable"]
    replacement = basic[:2] + b"New-Aircraft".ljust(20, b"\0") + basic[22:]
    assert decoder.accept("12:34:56:21:43:65", [replacement], now=2) is None
    assert decoder.accept("12:34:56:21:43:65", [loc], now=3)["identity"] == "New-Aircraft"
    assert decoder.accept("12:34:56:21:43:65", [replacement], now=20)["stable"] is True
    assert decoder.accept("12:34:56:21:43:65", [replacement], now=34) is None
    assert decoder.accept("12:34:56:21:43:65", [loc], now=70)["stable"] is False


def test_remote_id_protocol_old_firmware_fallback_and_managed_envelope():
    basic, loc = rid_messages()
    payload = (b"\xf2\x19\x02" + basic + loc).hex()
    record = dict(v=2, kind="remote_id_wifi", session="rid", seq=3, batch=1,
                  capture_ms=100, age_ms=0, mac="12:34:56:21:43:65", channel=6,
                  rssi=-48, transport="beacon", data_hex=payload, truncated=False)
    assert parse_record("WDG:" + json.dumps(record)) is not None
    managed = dict(record, v=3, device_id="AABBCCDDEEFF",
                   boot_id="0123456789ABCDEF")
    assert parse_job_record("WDG:" + json.dumps(managed))["kind"] == "remote_id_wifi"
    assert parse_record("WDG:" + json.dumps(dict(record, truncated=True))) is not None
    assert parse_record('WDG:{"v":1,"kind":"capabilities","wardrive_serial_v1":true,"remote_id_wifi_v1":true,"remote_id_wifi_max_bytes":228}')
    assert parse_record("WDG:" + json.dumps(dict(record, data_hex="ab"*228))) is not None
    for changes in (dict(channel=0), dict(transport="probe"),
                    dict(data_hex="ab"*229), dict(truncated=1)):
        assert parse_record("WDG:" + json.dumps(dict(record, **changes))) is None
    assert parse_record('WDG:{"v":1,"kind":"capabilities","wardrive_serial_v1":true}')
    assert parse_record('WDG:{"v":1,"kind":"capabilities","wardrive_serial_v1":true,"remote_id_wifi_v1":1}') is None


def test_remote_id_wifi_truncation_never_creates_position(tmp_path):
    settings = normalize_settings({})
    ui = WardriveUI.__new__(WardriveUI)
    ui.pig = PigDetector(settings)
    ui.pig.start(tmp_path)
    ui.remote_id = RemoteIdDecoder()
    basic, loc = rid_messages()
    record = dict(kind="remote_id_wifi", mac="12:34:56:21:43:65", rssi=-48,
                  data_hex=(b"\xf2\x19\x02"+basic+loc).hex(), age_ms=0,
                  truncated=True)
    ui.fixes = NS(at=lambda _: {"latitude": 40, "longitude": -90})
    ui.app = NS(gps=NS(available=True))
    ui.observation(record)
    assert not ui.pig.live
    ui.observation(dict(record, truncated=False))
    assert len(ui.pig.live) == 1
    event = next(iter(ui.pig.live.values()))
    assert event["broadcast_position"]["latitude"] == 40.1234567
    assert event["observer_fix"]["latitude"] == 40


def test_map_position_types_stale_tracks_and_rssi_geometry(tmp_path):
    pig = PigDetector(normalize_settings({"pig_agency_rid": ["USS-ENTERPRISE"]}))
    pig.start(tmp_path)
    heard = dict(kind="ble", mac="00:11:22:33:44:55", addr_type=0,
                 rssi=-45, fix={"latitude": 40, "longitude": -90},
                 observed_at=time.time(), session="c5", seq=1)
    hit = dict(category="utility", label="Possible BodyWorn Remote accessory",
               strength=2, evidence=[{"id":"utility-bodyworn-name", "strength":2}])
    pig.ingest_notable(heard, hit)
    pig.ingest_broadcast("remote_id", "USS-Enterprise",
                         {"latitude": 41, "longitude": -91}, heard["fix"])
    pig.ingest_broadcast("remote_id", "USS-Enterprise",
                         {"latitude": 41.01, "longitude": -91.01}, heard["fix"])
    assert pig.live["notable:utility:ble:00:11:22:33:44:55"]["broadcast_position"] is None
    assert pig.live["broadcast:remote_id:USS-ENTERPRISE"]["tier"] == "configured"
    assert len(pig.broadcast_tracks["broadcast:remote_id:USS-ENTERPRISE"]) == 2
    pig.ingest_broadcast("remote_id", "UNKNOWN", {"latitude": 42, "longitude": -92},
                         heard["fix"], stable=False)
    assert pig.live["broadcast:remote_id:UNKNOWN"]["tier"] == "neutral"
    assert "broadcast:remote_id:UNKNOWN" not in pig.broadcast_tracks
    pig.ingest_rf(dict(frequency_hz=155_000_000, above_threshold=False), heard["fix"])
    assert pig.live["rf:155000000"]["category"] == "rf_coverage"
    pig.ingest_rf(dict(frequency_hz=155_000_000, above_threshold=True), heard["fix"])
    assert pig.live["rf:155000000"]["tier"] == "lead"
    assert "signal_dbm" not in pig.live["rf:155000000"]
    sensor = NS(sid="weather_7", model="Weather", data={"rssi": -22},
                last_seen=time.time())
    pig.ingest_sensor433(sensor, heard["fix"])
    sensor_record = pig.live["sensor433:weather_7"]
    assert sensor_record["tier"] == "neutral"
    assert sensor_record["broadcast_position"] is None
    assert sensor_record["observer_fix"] == heard["fix"]
    pig.ingest_sensor433(sensor, heard["fix"])
    assert pig.live["sensor433:weather_7"] is sensor_record
    now = time.monotonic()
    assert all(item["source"] != "remote_id" for _, item in pig.visible(now+31))
    assert (tmp_path/"pig_observations.jsonl").exists()

    points = []
    for x, y in ((2, 2), (40, 0), (70, 30), (10, 60),
                 (-40, 20), (-20, -40), (40, -50), (100, -20)):
        lat = 40 + y/111320
        lon = -90 + x/(111320*math.cos(math.radians(40)))
        rssi = -30-20*math.log10(max(3, math.hypot(x,y))/3)
        points.append(((lat, lon), rssi))
    area = estimate_rssi_area(points)
    assert area and area["radius_m"] <= 1000
    assert abs(area["latitude"]-40) < 0.001
    assert estimate_rssi_area(points[:5]) is None
    assert estimate_rssi_area([((40+i/10000, -90), -50-i) for i in range(8)]) is None


def test_pig_lifecycle_only_stops_owned_legacy_scan(tmp_path):
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({"pig_sdr_profile": "off"})
    ui.pig = PigDetector(ui.settings)
    ui.remote_id = RemoteIdDecoder()
    ui.detector = NotableDetector()
    scan = NS(active=True, mode="wardrive", session="existing", supported=True,
              start=Mock(), stop=Mock(), probe=Mock())
    ui.legacy_scan = scan
    serial = NS(is_open=True, send_command=Mock())
    app = NS(loot=NS(session_dir=str(tmp_path)), c5_pool=None,
             _sdr_enabled=False, _sdr=NS(running=False),
             serial=serial,
             _anything_running_on_esp=Mock(return_value=True),
             _term_add=Mock(), msg=Mock(), _send=Mock())
    ui.app = app
    assert ui.start_pig() and ui.pig.active and not ui.pig.owns_scan
    scan.start.assert_not_called()
    ui.stop_pig()
    scan.stop.assert_not_called()
    app._send.assert_not_called()
    scan.active = False
    scan.start = Mock(return_value=True)
    app._anything_running_on_esp.return_value = False
    assert ui.start_pig() and ui.pig.owns_scan
    ui.pig.owned_session = "new"
    scan.active = True
    scan.session = "new"
    ui.stop_pig()
    scan.stop.assert_called_once()
    serial.send_command.assert_called_once_with("stop")
    app._send.assert_not_called()


def test_pig_never_preempts_another_managed_c5_job(tmp_path):
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({"pig_sdr_profile": "off"})
    ui.pig = PigDetector(ui.settings)
    ui.remote_id = RemoteIdDecoder()
    ui.detector = NotableDetector()
    pool = NS(available=True, groups={"busy": NS(operation="wifi_scan", state="running")},
              start=Mock())
    ui.legacy_scan = NS(active=False, mode="", supported=True, probe=Mock())
    ui.app = NS(loot=NS(session_dir=str(tmp_path)), c5_pool=pool,
                _sdr_enabled=False, _sdr=NS(running=False),
                _term_add=Mock(), msg=Mock())
    assert ui.start_pig()
    pool.start.assert_not_called()
    assert ui.pig.active and not ui.pig.owns_scan
    assert ui.pig.status.startswith("C5 BUSY")


def test_pig_uses_selected_legacy_fallback_when_managed_start_declines(tmp_path):
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = normalize_settings({"pig_sdr_profile": "off"})
    ui.pig = PigDetector(ui.settings)
    ui.remote_id = RemoteIdDecoder()
    ui.detector = NotableDetector()
    scan = NS(active=False, mode="", session="", supported=True,
              start=Mock(), stop=Mock(), probe=Mock())

    def start_legacy(*_args, **_kwargs):
        scan.active, scan.mode, scan.session = True, "wardrive", "legacy"
        return True

    scan.start.side_effect = start_legacy
    ui.legacy_scan = scan
    ui.scan = NS()

    def decline_managed(*_args, **_kwargs):
        ui.scan = scan  # The scheduler selected the legacy endpoint.
        return False

    pool = NS(available=True, groups={}, start=Mock(side_effect=decline_managed),
              stop_all=Mock())
    serial = NS(is_open=True, send_command=Mock())
    app = NS(loot=NS(session_dir=str(tmp_path)), c5_pool=pool,
             _sdr_enabled=False, _sdr=NS(running=False),
             serial=serial,
             _anything_running_on_esp=Mock(return_value=False),
             _term_add=Mock(), msg=Mock(), _send=Mock())
    ui.app = app
    assert ui.start_pig() and ui.pig.owns_scan
    assert ui.pig.owned_session == "legacy"
    pool.start.assert_called_once()
    scan.start.assert_called_once()
    pool.groups["other"] = NS(operation="wifi_scan", state="running")
    ui.stop_pig()
    scan.stop.assert_called_once()
    serial.send_command.assert_called_once_with("stop")
    app._send.assert_not_called()
    pool.stop_all.assert_not_called()
    assert pool.groups["other"].state == "running"
