"""Cell identity, WiGLE storage, and background-provider tests."""
import csv
import time
from dataclasses import asdict
from unittest.mock import Mock

from watchdogs.cell_monitor import (CellObservation, HostCellScanner,
    modemmanager_cell, parse_cpsi)
from watchdogs.gps_manager import GpsFix
from watchdogs.loot_manager import LootManager
from plugins.wardrive_upload import WardriveUpload


def test_modemmanager_serving_and_neighbor_cells():
    serving = modemmanager_cell({"cell-type":5, "serving":True,
        "operator-id":"310260", "tac":"00AF", "ci":"0ABCDEF",
        "earfcn":66486, "physical-ci":42, "rsrp":-91.5, "rsrq":-12.0})
    neighbor = modemmanager_cell({"cell-type":3, "serving":False,
        "operator-id":"310260", "lac":"00B0", "ci":"10", "uarfcn":10688,
        "psc":12, "rscp":-85})
    assert serving.identity == "310260_175_11259375"
    assert serving.technology == "LTE" and serving.serving and serving.signal_dbm == -91.5
    assert neighbor.technology == "WCDMA" and not neighbor.serving
    assert modemmanager_cell({"cell-type":5, "operator-id":"310260", "tac":"AF"}) is None


def test_sim7600_lte_and_no_service():
    cell = parse_cpsi("AT+CPSI?\r\n+CPSI: LTE,Online,310-260,0x00AF,0ABCDEF,42,LTE BAND 66,66486,5,5,-120,-915,-650,120\r\nOK\r\n")
    assert cell.identity == "310260_175_11259375"
    assert cell.channel == 66486 and cell.pci == 42
    assert cell.rsrp == -91.5 and cell.serving
    assert parse_cpsi("+CPSI: NO SERVICE, Online\r\nOK\r\n") is None


def test_cell_wigle_rows_repeat_and_keep_mnc_width(tmp_path):
    loot = LootManager.__new__(LootManager)
    loot._session = tmp_path
    loot._session_active = True
    loot._gps = None
    fix = asdict(GpsFix(valid=True, latitude=40.1, longitude=-90.2, hdop=1))
    cell = CellObservation("LTE", "310", "026", 175, 1234, 100, -91,
                           True, "test").record()
    assert loot.save_wardriving_cell(cell, observation_fix=fix, observed_at=10)
    assert loot.save_wardriving_cell(cell, observation_fix=fix, observed_at=20)
    with (tmp_path / "wardriving.csv").open(newline="") as stream:
        assert stream.readline().startswith("WigleWifi-1.6")
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2
    assert rows[0]["MAC"] == "310026_175_1234"
    assert rows[0]["Type"] == rows[0]["AuthMode"] == "LTE"


def test_background_scanner_delivers_serving_and_neighbors():
    cells = [CellObservation("LTE", "310", "260", 1, 2, 3, -90, True, "test"),
             CellObservation("LTE", "310", "260", 1, 4, 3, -95, False, "test")]
    class Provider:
        name = "fixture"
        def scan(self): return cells
        def close(self): pass
    scanner = HostCellScanner(Provider, interval=60)
    assert scanner.start("session")
    deadline = time.time() + 2
    events = []
    while time.time() < deadline and not any(e[1] == "cells" for e in events):
        events += scanner.poll()
        time.sleep(.01)
    scanner.stop()
    update = next(e for e in events if e[1] == "cells")
    assert update[0] == "session" and len(update[2][1]) == 2


def test_wdgwars_parser_preserves_cell_type_and_reads_legacy(tmp_path):
    modern = tmp_path / "modern.csv"
    modern.write_text(
        "WigleWifi-1.6,appRelease=WatchDogsGo\n"
        "MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type\n"
        "310260_1_2,310260,LTE,2026-01-01 00:00:00,100,,-91,40,-90,2,5,,,LTE\n")
    legacy = tmp_path / "legacy.csv"
    legacy.write_text(
        "WigleWifi-1.4,appRelease=WatchDogsGo\n"
        "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"
        "00:11:22:33:44:55,old,[ESS],2026-01-01 00:00:00,6,-50,40,-90,2,5,WIFI\n")
    upload = WardriveUpload.__new__(WardriveUpload)
    upload._log_add = Mock()
    cell = upload._parse_csv(modern)[0]
    wifi = upload._parse_csv(legacy)[0]
    assert cell["type"] == cell["auth"] == "LTE" and cell["accuracy"] == 5
    assert wifi["type"] == "WIFI" and wifi["auth"] == "OPEN"
