"""Map-only identity deduplication tests."""

import csv

from watchdogs.loot_manager import LootManager


def _write_wigle(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        stream.write("WigleWifi-1.4\n")
        writer = csv.DictWriter(stream, fieldnames=LootManager._WIGLE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _row(mac, rssi, lat, lon, kind="WIFI", name="node"):
    return {
        "MAC": mac, "SSID": name, "AuthMode": "[WPA2][ESS]",
        "FirstSeen": "2026-01-01 00:00:00", "Channel": "6",
        "Frequency": "", "RSSI": str(rssi),
        "CurrentLatitude": str(lat), "CurrentLongitude": str(lon),
        "AltitudeMeters": "0", "AccuracyMeters": "1", "RCOIs": "",
        "MfgrId": "", "Type": kind,
    }


def _manager(root):
    manager = LootManager.__new__(LootManager)
    manager._base = root
    manager._gps_points_cache = None
    manager._gps_points_ts = 0.0
    return manager


def test_map_points_keep_strongest_identity_across_sessions(tmp_path):
    mac = "00:11:22:33:44:55"
    _write_wigle(tmp_path / "2026-01-01_00-00-00" / "wardriving.csv",
                 [_row(mac, -35, 40.0, -90.0, name="strong")])
    _write_wigle(tmp_path / "2026-01-02_00-00-00" / "wardriving.csv",
                 [_row(mac.lower(), -70, 41.0, -91.0, name="weak")])

    points = _manager(tmp_path).get_gps_points()

    assert len(points) == 1
    assert points[0]["label"] == "strong"
    assert (points[0]["lat"], points[0]["lon"]) == (40.0, -90.0)


def test_map_point_rssi_tie_prefers_later_session(tmp_path):
    mac = "00:11:22:33:44:55"
    _write_wigle(tmp_path / "2026-01-01_00-00-00" / "wardriving.csv",
                 [_row(mac, -50, 40.0, -90.0, name="old")])
    _write_wigle(tmp_path / "2026-01-02_00-00-00" / "wardriving.csv",
                 [_row(mac, -50, 42.0, -92.0, name="new")])

    assert _manager(tmp_path).get_gps_points()[0]["label"] == "new"


def test_map_points_deduplicate_ble_sources_without_rewriting_files(tmp_path):
    session = tmp_path / "2026-01-01_00-00-00"
    mac = "AA:BB:CC:DD:EE:FF"
    _write_wigle(session / "wardriving.csv",
                 [_row(mac, -80, 40.0, -90.0, kind="BLE", name="weak")])
    bt_path = session / "bt_devices.csv"
    with bt_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=("mac", "name", "rssi", "lat", "lon"))
        writer.writeheader()
        writer.writerow({"mac": mac.lower(), "name": "strong", "rssi": -40,
                         "lat": 41.0, "lon": -91.0})
    before = (session / "wardriving.csv").read_bytes(), bt_path.read_bytes()

    points = _manager(tmp_path).get_gps_points()

    assert len(points) == 1 and points[0]["label"] == "strong"
    assert before == ((session / "wardriving.csv").read_bytes(), bt_path.read_bytes())


def test_map_points_do_not_collapse_missing_identities(tmp_path):
    _write_wigle(tmp_path / "2026-01-01_00-00-00" / "wardriving.csv", [
        _row("", -50, 40.0, -90.0, name="one"),
        _row("", -40, 41.0, -91.0, name="two"),
    ])

    assert [point["label"] for point in _manager(tmp_path).get_gps_points()] == [
        "one", "two"]
