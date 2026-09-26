"""gpsd stream parsing and GpsManager ownership tests."""

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from watchdogs.gps_manager import GpsManager
from watchdogs.gpsd_client import WATCH_COMMAND, GpsdClient


class FakeSocket:
    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.sent = []
        self.closed = False
        self.blocking = None

    def sendall(self, payload):
        self.sent.append(payload)

    def setblocking(self, value):
        self.blocking = value

    def recv(self, _size):
        if not self.chunks:
            raise BlockingIOError
        value = self.chunks.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        self.closed = True


def test_gpsd_client_handles_split_and_malformed_reports():
    sock = FakeSocket([
        b'{"class":"TP',
        (b'V","mode":3,"lat":40.1,"lon":-90.2}\nnot-json\n'
         b'{"class":"SKY","hdop":1.2}\n'),
    ])
    client = GpsdClient(connector=lambda _address, _timeout: sock)
    client.connect()
    reports = client.read_reports()
    assert sock.sent == [WATCH_COMMAND]
    assert sock.blocking is False
    assert [report["class"] for report in reports] == ["TPV", "SKY"]


def test_gpsd_client_reports_orderly_disconnect():
    sock = FakeSocket([b""])
    client = GpsdClient(connector=lambda _address, _timeout: sock)
    client.connect()
    with pytest.raises(ConnectionError, match="closed"):
        client.read_reports()


def _broker():
    return SimpleNamespace(
        acquire=Mock(side_effect=AssertionError("ModemManager must stay unused")),
        close=Mock(), error="")


def test_managed_gpsd_failure_never_falls_back_to_raw_uart(tmp_path, monkeypatch):
    marker = tmp_path / "gpsd.conf"
    marker.write_text("HOST=127.0.0.1\nPORT=2947\nDEVICE=/dev/serial0\n")
    gps = GpsManager(modem_broker=_broker(), modem_enabled=False,
                     gpsd_config=marker)
    monkeypatch.setattr(gps, "_try_gpsd", Mock(return_value=False))
    monkeypatch.setattr(gps, "_probe_nmea",
                        Mock(side_effect=AssertionError("raw UART fallback")))
    monkeypatch.setattr(gps, "_try_open",
                        Mock(side_effect=AssertionError("raw UART fallback")))
    assert not gps.setup()
    assert "gpsd unavailable" in gps.status_reason


def test_gpsd_tpv_and_sky_update_fix_without_nmea(tmp_path, monkeypatch):
    marker = tmp_path / "gpsd.conf"
    marker.write_text("HOST=localhost\nPORT=2947\nDEVICE=/dev/serial0\n")
    gps = GpsManager(modem_broker=_broker(), modem_enabled=False,
                     gpsd_config=marker)
    reports = [
        {"class": "TPV", "mode": 3, "lat": 40.123, "lon": -90.456,
         "altMSL": 201.5, "speed": 10, "time": "2026-09-26T12:00:00Z"},
        {"class": "SKY", "hdop": 0.8,
         "satellites": [{"used": True}, {"used": False}, {"used": True}]},
    ]
    fake = SimpleNamespace(
        read_reports=Mock(return_value=reports), close=Mock())
    gps._gpsd = fake
    gps._available = True
    gps.provider = "gpsd"
    before = time.monotonic()
    assert gps.read_available() == []
    assert gps.fix.valid
    assert gps.fix.latitude == 40.123 and gps.fix.longitude == -90.456
    assert gps.fix.altitude == 201.5
    assert gps.fix.speed_knots == pytest.approx(19.4384449)
    assert gps.fix.satellites == 2 and gps.fix.satellites_visible == 3
    assert gps.fix.hdop == 0.8 and gps.fix.received_at >= before
    assert gps.data_flowing
    assert gps.status_reason == "GPS fix acquired"


def test_gpsd_no_fix_is_live_data_not_a_transport_failure(tmp_path):
    marker = tmp_path / "gpsd.conf"
    marker.write_text("HOST=localhost\n")
    gps = GpsManager(modem_broker=_broker(), modem_enabled=False,
                     gpsd_config=marker)
    gps._gpsd = SimpleNamespace(
        read_reports=Mock(return_value=[{"class": "TPV", "mode": 1}]),
        close=Mock())
    gps._available = True
    gps.provider = "gpsd"
    gps.read_available()
    assert gps.available and not gps.fix.valid and gps.data_flowing
    assert "waiting for satellite fix" in gps.status_reason


def test_gpsd_disconnect_marks_provider_for_reconnect(tmp_path):
    marker = tmp_path / "gpsd.conf"
    marker.write_text("HOST=localhost\n")
    gps = GpsManager(modem_broker=_broker(), modem_enabled=False,
                     gpsd_config=marker)
    fake = SimpleNamespace(
        read_reports=Mock(side_effect=ConnectionError("lost gpsd")), close=Mock())
    gps._gpsd = fake
    gps._available = True
    gps.provider = "gpsd"
    gps.read_available()
    assert not gps.available and gps.provider == ""
    assert gps.status_reason == "lost gpsd"
    fake.close.assert_called_once()


def test_app_reads_aio_power_before_opening_gps():
    source = (Path(__file__).resolve().parents[1] / "watchdogs" / "app.py").read_text()
    status_read = source.index('self._gps_enabled = _aio_st.get("gps", False)')
    setup_call = source.index("self.gps.setup()", status_read)
    assert status_read < setup_call
    assert 'GPS transport: {self.gps.device}' in source
