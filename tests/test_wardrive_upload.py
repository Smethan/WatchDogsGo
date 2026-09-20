"""WDGWars combined signed-upload contract tests."""
import base64
import hashlib
import hmac
import json
import struct
from unittest.mock import Mock

from plugins import wardrive_upload as upload_module
from plugins.wardrive_upload import DEFAULT_API_URL, WardriveUpload
from watchdogs.lora_manager import LoRaManager
from watchdogs.loot_manager import LootManager


class _Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.value).encode()


def _uploader():
    upload = WardriveUpload.__new__(WardriveUpload)
    upload._api_key = "a" * 64
    upload._api_url = DEFAULT_API_URL
    upload._uploading = True
    upload._log_add = Mock()
    upload._mark_uploaded = Mock(return_value=True)
    upload._handle_upload_badges = Mock()
    upload._badge_sync_worker = Mock()
    return upload


def test_combined_upload_uses_signed_endpoint_and_current_record_schemas(
        tmp_path, monkeypatch):
    session = tmp_path / "2026-09-20_12-00-00"
    session.mkdir()
    (session / "wardriving.csv").write_text(
        "WigleWifi-1.6,appRelease=WatchDogsGo\n"
        "MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type\n"
        "AA:BB:CC:DD:EE:FF,Test,[WPA2-PSK],2026-09-20 12:00:00,6,2437,-45,40.1,-90.2,12,3,,,WIFI\n"
        "11:22:33:44:55:66,Beacon,[BLE],2026-09-20 12:00:01,0,0,-60,40.1,-90.2,12,3,,,BLE\n",
        encoding="utf-8")
    (session / "adsb_aircraft.csv").write_text(
        "timestamp,icao,callsign,lat,lon,alt_ft,speed_kt,heading,squawk\n"
        "1789905600,a1b2c3,,40.2,-90.3,31000,430,270,1200\n"
        "1789905601,A1B2C3,UAL123,40.3,-90.4,32000,440,271,1200\n",
        encoding="utf-8")
    public_key = "0ce8abcd" + "ab" * 28
    (session / "meshcore_nodes.csv").write_text(
        "timestamp,node_id,type,name,lat,lon,rssi,snr,public_key,path_hops\n"
        f"2026-09-20T12:00:02,0ce8abcd,Repeater,Mesh One,40.4,-90.5,-81,7.5,{public_key},2\n",
        encoding="utf-8")

    seen = {}

    def fake_open(request, timeout=0):
        seen["request"] = request
        seen["timeout"] = timeout
        return _Response({
            "ok": True, "imported": 2, "aircraft_imported": 1,
            "meshcore_imported": 1,
        })

    monkeypatch.setattr(upload_module, "_open", fake_open)
    upload = _uploader()
    upload._upload_worker([session])

    request = seen["request"]
    assert request.full_url == "https://wdgwars.pl/api/upload/"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("X-api-key") == upload._api_key
    assert seen["timeout"] == 90

    envelope = json.loads(request.data)
    raw = base64.b64decode(envelope["data"])
    expected = hmac.new(
        upload._api_key.encode(),
        (envelope["nonce"] + envelope["data"]).encode(),
        hashlib.sha256).hexdigest()
    assert envelope["sig"] == expected
    payload = json.loads(raw)
    assert set(payload) == {"networks", "aircraft", "meshcore_nodes"}
    assert {record["type"] for record in payload["networks"]} == {
        "WIFI", "BLE"}
    assert payload["aircraft"] == [{
        "icao": "A1B2C3", "callsign": "UAL123", "lat": 40.3,
        "lon": -90.4, "alt_ft": 32000, "speed_kt": 440,
        "heading": 271, "first_seen": "2026-09-20 12:00:00",
        "type": "ADSB",
    }]
    assert payload["meshcore_nodes"] == [{
        "node_id": public_key[:16], "node_type": "Repeater",
        "name": "Mesh One", "lat": 40.4, "lon": -90.5,
        "rssi": -81.0, "first_seen": "2026-09-20 12:00:02",
        "type": "MESHCORE", "network": "meshcore",
        "public_key": public_key, "path_hops": 2,
    }]
    upload._mark_uploaded.assert_called_once_with(session)
    upload._handle_upload_badges.assert_called_once()


def test_adsb_and_meshcore_parsers_keep_legacy_rows_and_reject_no_gps(tmp_path):
    (tmp_path / "adsb_aircraft.csv").write_text(
        "timestamp,icao,callsign,lat,lon,alt_ft,speed_kt,heading\n"
        "2026-09-20T10:11:12,AABBCC,TEST1,40,-90,10000,200,180\n"
        "2026-09-20T10:11:13,DDEEFF,NOGPS,0,0,10000,200,180\n",
        encoding="utf-8")
    (tmp_path / "meshcore_nodes.csv").write_text(
        "timestamp,node_id,type,name,lat,lon,rssi,snr\n"
        "2026-09-20T10:11:14,deadbeef,Client,Legacy Node,41,-91,-70,5\n"
        "2026-09-20T10:11:15,bad00000,Client,No GPS,0,0,-60,6\n"
        "2026-09-20T10:11:16,abc,Client,Short ID,42,-92,-50,7\n",
        encoding="utf-8")
    upload = _uploader()

    aircraft = upload._parse_adsb(tmp_path / "adsb_aircraft.csv")
    nodes = upload._parse_meshcore(tmp_path / "meshcore_nodes.csv")

    assert [record["icao"] for record in aircraft] == ["AABBCC"]
    assert aircraft[0]["first_seen"] == "2026-09-20 10:11:12"
    assert [record["node_id"] for record in nodes] == ["deadbeef"]
    assert nodes[0]["network"] == "meshcore"
    assert "public_key" not in nodes[0]


def test_meshcore_loot_saves_public_key_without_breaking_legacy_csv(tmp_path):
    loot = LootManager.__new__(LootManager)
    loot._session = tmp_path
    loot._session_active = True
    loot.update_session_loot = Mock()
    public_key = "12" * 32

    loot.save_meshcore_node(
        "00112233", "Repeater", "Current", 40, -90, -75, 6,
        public_key)
    rows = (tmp_path / "meshcore_nodes.csv").read_text().splitlines()
    assert rows[0].endswith(",public_key,path_hops")
    assert rows[1].endswith("," + public_key + ",0")
    assert "T" not in rows[1].split(",", 1)[0]

    legacy = tmp_path / "meshcore_nodes.csv"
    legacy.write_text(
        "timestamp,node_id,type,name,lat,lon,rssi,snr\n"
        "2026-09-20T12:00:00,old00000,Client,Old,40,-90,-80,5\n",
        encoding="utf-8")
    loot.save_meshcore_node(
        "new00000", "Client", "New", 41, -91, -70, 7, public_key)
    assert len(legacy.read_text().splitlines()[-1].split(",")) == 8


def test_meshcore_radio_passes_public_key_and_route_hops_to_collector():
    manager = LoRaManager()
    manager._get_ed25519_keypair = Mock(
        return_value=(None, b"\xff" * 32))
    manager._on_node = Mock()
    public_key = bytes.fromhex("0ce8abcd" + "ab" * 28)
    latitude = int(40.123456 * 1_000_000)
    longitude = int(-90.654321 * 1_000_000)
    payload = bytearray(
        public_key
        + struct.pack("<I", 123456)
        + b"\x00" * 64
        + bytes([0x11])
        + struct.pack("<ii", latitude, longitude)
        + b"Mesh One\x00")

    manager._decode_mc_advert(payload, -81, 7.5, hops=2)

    manager._on_node.assert_called_once_with(
        "0ce8abcd", "Client", "Mesh One", 40.123456, -90.654321,
        -81, 7.5, public_key, 2)
