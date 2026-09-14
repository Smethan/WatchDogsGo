"""Serving-cell, provisional-neighbor, and crash breadcrumb tests."""

import subprocess
import time
from unittest.mock import Mock

from watchdogs.cell_monitor import (
    MAX_SNAPSHOT_AGE, UNKNOWN_CELL_DBM, HostCellScanner, find_unclean_cell_session,
    parse_qmi_neighbors)
from watchdogs.modem_location import ModemLocationSnapshot


def snapshot(**changes):
    values = dict(observed_monotonic=time.monotonic(), observed_utc=20,
                  modem_generation=1,
                  operator_id="311480", technology="LTE", lac=0, tac=33544,
                  cell_id=33784342, nmea=(), signal_dbm=None,
                  signal_quality_percent=65,
                  modem_path="/org/freedesktop/ModemManager1/Modem/0",
                  model="SIMCOM_SIM7600G-H", revision="LE20B04",
                  qmi_device="/dev/cdc-wdm0")
    values.update(changes)
    return ModemLocationSnapshot(**values)


FIX = {"valid": True, "latitude": 40.1, "longitude": -90.2,
       "altitude": 10, "hdop": 1, "received_at": 10}


class Broker:
    def __init__(self, value=None):
        self.value = value or snapshot()
        self.error = ""
        self.enabled_sources = 5
        self.acquire = Mock(return_value=True)
        self.release = Mock()

    def snapshot(self):
        return self.value


def cell_event(scanner):
    return next(event for event in scanner.poll() if event[1] == "cell")


def test_serving_cell_is_batch_aligned_and_crash_sentinel_is_cleaned(tmp_path):
    broker = Broker()
    scanner = HostCellScanner(broker)
    assert scanner.start("abc", tmp_path)
    sentinel = tmp_path / "active_cell_session.json"
    assert sentinel.exists() and find_unclean_cell_session(tmp_path.parent.parent) is None
    scanner.observe_batch(FIX, 1, observed_at=30)
    _session, _kind, (measured, cell, fix) = cell_event(scanner)
    assert measured == 30 and fix == FIX
    assert cell.identity == "311480_33544_33784342"
    assert cell.signal_dbm == UNKNOWN_CELL_DBM
    scanner.note_saved(cell)
    scanner.observe_batch(FIX, 1, observed_at=31)
    assert not any(event[1] == "cell" for event in scanner.poll())
    scanner.stop()
    assert not sentinel.exists()
    broker.release.assert_called_once_with("cell:abc")
    health = (tmp_path / "cell_health.jsonl").read_text()
    assert '"event":"start"' in health and '"event":"stop"' in health


def test_unclean_session_finder_reports_latest_sentinel(tmp_path):
    older = tmp_path / "loot" / "2026-01-01_00-00-00"
    newer = tmp_path / "loot" / "2026-01-02_00-00-00"
    older.mkdir(parents=True); newer.mkdir()
    (older / "active_cell_session.json").write_text("{}")
    expected = newer / "active_cell_session.json"
    expected.write_text("{}")
    assert find_unclean_cell_session(tmp_path) == expected


def test_missing_gps_or_identity_degrades_without_radio_failure(tmp_path):
    broker = Broker(snapshot(operator_id=None, cell_id=None))
    scanner = HostCellScanner(broker)
    scanner.start("abc", tmp_path)
    scanner.observe_batch(None, 1)
    assert scanner.state == "waiting_gps"
    scanner.observe_batch(FIX, 2)
    assert scanner.state == "degraded"
    scanner.stop()


def test_stale_cell_snapshot_is_not_geotagged(tmp_path):
    clock = Mock(return_value=MAX_SNAPSHOT_AGE + 11)
    broker = Broker(snapshot(observed_monotonic=10))
    scanner = HostCellScanner(broker, clock=clock)
    scanner.start("abc", tmp_path)
    scanner.observe_batch(FIX, 1)
    assert scanner.state == "degraded"
    assert "stale" in scanner.error
    assert not any(event[1] == "cell" for event in scanner.poll())
    scanner.stop()


QMI = """[/dev/cdc-wdm0] Successfully got cell location info
Intrafrequency LTE Info
\tPLMN: '311048'
\tTracking Area Code: '33544'
\tGlobal Cell ID: '33784342'
\tEUTRA Absolute RF Channel Number: '2100' (LTE band 4)
\tServing Cell ID: '76'
\tCell [0]:
\t\tPhysical Cell ID: '75'
\t\tRSRQ: '-15.0' dB
\t\tRSRP: '-110.0' dBm
\t\tRSSI: '-79.0' dBm
\tCell [1]:
\t\tPhysical Cell ID: '76'
\t\tRSRQ: '-12.0' dB
\t\tRSRP: '-105.0' dBm
\t\tRSSI: '-69.0' dBm
Interfrequency LTE Info
\tFrequency [0]:
\t\tEUTRA Absolute RF Channel Number: '5230'
\t\tCell [0]:
\t\t\tPhysical Cell ID: '101'
\t\t\tRSRQ: '-16.0' dB
\t\t\tRSRP: '-115.0' dBm
"""


def test_qmi_neighbors_use_canonical_operator_and_never_invent_cell_id():
    result = parse_qmi_neighbors(QMI, snapshot(), FIX, 30)
    assert result.serving_signal_dbm == -105
    assert {(item.channel, item.pci) for item in result.candidates} == {
        (2100, 75), (5230, 101)}
    assert all(item.operator_id == "311480" and item.provisional
               for item in result.candidates)
    assert all("33784342" not in item.key for item in result.candidates)


class FailingProcess:
    returncode = 1
    def communicate(self, timeout): return "", "QMI proxy unavailable"
    def poll(self): return self.returncode


def test_experimental_qmi_failure_trips_once_and_serving_continues(tmp_path, monkeypatch):
    monkeypatch.setattr("watchdogs.cell_monitor.shutil.which", lambda _name: "/usr/bin/qmicli")
    broker = Broker()
    scanner = HostCellScanner(broker, qmi_interval=0,
                              popen_factory=lambda *a, **k: FailingProcess())
    scanner.start("abc", tmp_path, experimental_neighbors=True)
    scanner.observe_batch(FIX, 1)
    deadline = time.time() + 1
    events = []
    while time.time() < deadline and not scanner.neighbors_paused:
        events += scanner.poll()
        time.sleep(.01)
    assert scanner.neighbors_paused == "QMI proxy unavailable"
    scanner.observe_batch(FIX, 2)
    assert cell_event(scanner)[2][1].identity == "311480_33544_33784342"
    scanner.stop()


class TimeoutProcess:
    returncode = None
    def __init__(self): self.terminated = self.killed = False
    def communicate(self, timeout):
        raise subprocess.TimeoutExpired(["qmicli"], timeout)
    def terminate(self): self.terminated = True; self.returncode = -15
    def kill(self): self.killed = True; self.returncode = -9
    def wait(self, timeout): return self.returncode
    def poll(self): return self.returncode


def test_qmi_timeout_terminates_child_and_opens_breaker(tmp_path, monkeypatch):
    monkeypatch.setattr("watchdogs.cell_monitor.shutil.which", lambda _name: "/usr/bin/qmicli")
    process = TimeoutProcess()
    scanner = HostCellScanner(Broker(), qmi_timeout=.01,
                              popen_factory=lambda *a, **k: process)
    scanner.start("abc", tmp_path, experimental_neighbors=True)
    scanner.observe_batch(FIX, 1)
    deadline = time.time() + 1
    while time.time() < deadline and not scanner.neighbors_paused:
        scanner.poll(); time.sleep(.01)
    assert process.terminated and "timed out" in scanner.neighbors_paused
    scanner.stop()


def test_default_path_never_starts_qmi(tmp_path):
    popen = Mock(side_effect=AssertionError("qmicli must stay disabled"))
    scanner = HostCellScanner(Broker(), popen_factory=popen)
    scanner.start("abc", tmp_path, experimental_neighbors=False)
    scanner.observe_batch(FIX, 1)
    assert cell_event(scanner)
    scanner.stop()
    popen.assert_not_called()
