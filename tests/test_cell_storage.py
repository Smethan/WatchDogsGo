"""Compatibility tests for cellular rows recorded by older WDG releases."""
import csv
from dataclasses import asdict
from unittest.mock import Mock

from plugins.wardrive_upload import WardriveUpload
from watchdogs.gps_manager import GpsFix
from watchdogs.loot_manager import LootManager


def test_historical_cell_wigle_rows_repeat_and_keep_mnc_width(tmp_path):
    loot = LootManager.__new__(LootManager)
    loot._session = tmp_path
    loot._session_active = True
    loot._gps = None
    fix = asdict(GpsFix(valid=True, latitude=40.1, longitude=-90.2, hdop=1))
    cell = {
        "technology": "LTE", "mcc": "310", "mnc": "026", "area": 175,
        "cell_id": 1234, "channel": 100, "signal_dbm": -91,
        "serving": True, "provider": "historical",
        "identity": "310026_175_1234", "operator_id": "310026",
    }
    assert loot.save_wardriving_cell(cell, observation_fix=fix, observed_at=10)
    assert loot.save_wardriving_cell(cell, observation_fix=fix, observed_at=20)
    with (tmp_path / "wardriving.csv").open(newline="") as stream:
        assert stream.readline().startswith("WigleWifi-1.6")
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2
    assert rows[0]["MAC"] == "310026_175_1234"
    assert rows[0]["Type"] == rows[0]["AuthMode"] == "LTE"


def test_wdgwars_parser_preserves_historical_cell_type(tmp_path):
    modern = tmp_path / "modern.csv"
    modern.write_text(
        "WigleWifi-1.6,appRelease=WatchDogsGo\n"
        "MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type\n"
        "310260_1_2,310260,LTE,2026-01-01 00:00:00,100,,-91,40,-90,2,5,,,LTE\n")
    upload = WardriveUpload.__new__(WardriveUpload)
    upload._log_add = Mock()
    cell = upload._parse_csv(modern)[0]
    assert cell["type"] == cell["auth"] == "LTE"
    assert cell["accuracy"] == 5
