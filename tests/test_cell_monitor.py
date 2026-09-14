"""Cell identity, WiGLE storage, and background-provider tests."""
import csv
import sys
import time
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from watchdogs.cell_monitor import (AutoCellProvider, CellObservation,
    HostCellScanner, PermanentCellError, QmiProxyProvider, discover_qmi_device,
    modemmanager_cell, parse_qmicli_cell_location)
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


QMICLI_LTE = """[/dev/cdc-wdm0] Successfully got cell location info
Intrafrequency LTE Info
\tUE In Idle: 'no'
\tPLMN: '310260'
\tTracking Area Code: '175'
\tGlobal Cell ID: '11259375'
\tEUTRA Absolute RF Channel Number: '66486' (LTE band 66)
\tServing Cell ID: '42'
\tCell [0]:
\t\tPhysical Cell ID: '41'
\t\tRSRQ: '-15.0' dB
\t\tRSRP: '-105.0' dBm
\t\tRSSI: '-75.0' dBm
\tCell [1]:
\t\tPhysical Cell ID: '42'
\t\tRSRQ: '-12.0' dB
\t\tRSRP: '-91.5' dBm
\t\tRSSI: '-65.0' dBm
Interfrequency LTE Info
\tUE In Idle: 'no'
"""


def test_qmicli_lte_keeps_only_globally_identified_serving_cell():
    cells = parse_qmicli_cell_location(QMICLI_LTE)
    assert len(cells) == 1
    cell = cells[0]
    assert cell.identity == "310260_175_11259375"
    assert cell.channel == 66486 and cell.pci == 42
    assert cell.rsrp == -91.5 and cell.rsrq == -12 and cell.serving
    assert cell.provider == "qmi_proxy"
    assert parse_qmicli_cell_location("Successfully got cell location info\n") == []


def test_qmicli_umts_and_nr_serving_cells():
    text = """UMTS Info
\tCell ID: '1234'
\tPLMN: '310260'
\tLocation Area Code: '42'
\tUTRA Absolute RF Channel Number: '10688'
\tPrimary Scrambling Code: '17'
\tRSCP: '-85' dBm
\tECIO: '-9' dBm
5GNR cell information
\tPLMN: '310260'
\tTracking Area Code: '66051'
\tGlobal Cell ID: '1234567890'
\tPhysical Cell ID: '321'
\tRSRQ: '-12.5 dB'
\tRSRP: '-96.5 dBm'
\tSNR: '18.0 dB'
"""
    cells = parse_qmicli_cell_location(text)
    assert [(cell.technology, cell.signal_dbm) for cell in cells] == [
        ("NR", -96.5), ("WCDMA", -85)]
    assert cells[0].sinr == 18 and cells[1].channel == 10688


def test_qmicli_geran_neighbor_requires_full_identity():
    text = """GERAN Info
\tCell ID: '1234'
\tPLMN: '310260'
\tLocation Area Code: '42'
\tGERAN Absolute RF Channel Number: '512'
\tBase Station Identity Code: '7'
\tRX Level: -80 dBm > level > -79 dBm ('31')
\tCell [0]:
\t\tCell ID: '1235'
\t\tPLMN: '310260'
\t\tLocation Area Code: '42'
\t\tGERAN Absolute RF Channel Number: '513'
\t\tBase Station Identity Code: '8'
\t\tRX Level: -90 dBm > level > -89 dBm ('21')
\tCell [1]:
\t\tCell ID: 'unavailable'
\t\tPLMN: 'unavailable'
\t\tLocation Area Code: 'unavailable'
\t\tGERAN Absolute RF Channel Number: '514'
\t\tBase Station Identity Code: '9'
\t\tRX Level: -95 dBm > level > -94 dBm ('16')
"""
    cells = parse_qmicli_cell_location(text)
    assert [cell.identity for cell in cells] == [
        "310260_42_1234", "310260_42_1235"]
    assert cells[0].serving and not cells[1].serving


def test_qmi_discovery_selects_sim7600_control_port(monkeypatch):
    modem_path = "/org/freedesktop/ModemManager1/Modem/0"
    interface = "org.freedesktop.ModemManager1.Modem"
    root = Mock()
    root.GetManagedObjects.return_value = {modem_path: {interface: {
        "Model": "SIMCOM_SIM7600G-H", "Manufacturer": "QUALCOMM INCORPORATED",
        "PrimaryPort": "cdc-wdm0", "Device": "/sys/devices/usb1/1-3",
        "Ports": (("cdc-wdm0", 6), ("ttyUSB1", 4), ("wwan0", 8)),
    }}}
    bus = Mock()
    bus.get_object.return_value = root
    monkeypatch.setitem(sys.modules, "dbus", SimpleNamespace(Interface=lambda obj, _name: obj))

    assert discover_qmi_device(bus) == "/dev/cdc-wdm0"


def test_qmi_provider_uses_proxy_and_never_an_at_port():
    runner = Mock(return_value=SimpleNamespace(
        returncode=0, stdout=QMICLI_LTE, stderr=""))
    provider = QmiProxyProvider(
        "/dev/cdc-wdm0", binary="/usr/bin/qmicli", runner=runner)
    assert provider.scan()[0].identity == "310260_175_11259375"
    command = runner.call_args.args[0]
    assert command == ["/usr/bin/qmicli", "--device=/dev/cdc-wdm0",
                       "--device-open-proxy", "--nas-get-cell-location-info"]
    assert all("ttyUSB" not in argument for argument in command)


def test_qmi_provider_classifies_unsupported_as_permanent():
    runner = Mock(return_value=SimpleNamespace(
        returncode=1, stdout="", stderr="error: operation not supported"))
    provider = QmiProxyProvider(
        "/dev/cdc-wdm0", binary="/usr/bin/qmicli", runner=runner)
    with pytest.raises(PermanentCellError):
        provider.scan()


def test_auto_provider_switches_once_from_modemmanager_to_qmi():
    first = Mock(name="modemmanager")
    first.name = "modemmanager"
    first.scan.side_effect = RuntimeError("operation not supported")
    second = Mock(name="qmi_proxy")
    second.name = "qmi_proxy"
    second.scan.return_value = parse_qmicli_cell_location(QMICLI_LTE)
    provider = AutoCellProvider(lambda: first, lambda: second)
    assert provider.scan()[0].provider == "qmi_proxy"
    assert provider.name == "qmi_proxy"
    first.close.assert_called_once()
    assert provider.scan()[0].provider == "qmi_proxy"
    assert first.scan.call_count == 1


def test_auto_provider_uses_qmi_when_modemmanager_is_unavailable():
    second = Mock(name="qmi_proxy")
    second.name = "qmi_proxy"
    second.scan.return_value = parse_qmicli_cell_location(QMICLI_LTE)
    provider = AutoCellProvider(
        Mock(side_effect=RuntimeError("D-Bus unavailable")), lambda: second)
    assert provider.fallback_used and provider.name == "qmi_proxy"
    assert provider.scan()[0].identity == "310260_175_11259375"


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


def test_background_scanner_marks_permanent_failure_non_retryable():
    class Provider:
        name = "fixture"
        def scan(self): raise PermanentCellError("unsupported")
        def close(self): pass
    scanner = HostCellScanner(Provider, interval=60)
    scanner.start("session")
    deadline = time.time() + 2
    events = []
    while time.time() < deadline and not any(e[1] == "error" for e in events):
        events += scanner.poll()
        time.sleep(.01)
    error = next(e for e in events if e[1] == "error")
    assert error[2] == {"message": "unsupported", "retryable": False}


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
