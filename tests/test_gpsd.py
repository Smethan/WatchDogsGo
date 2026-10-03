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


def test_managed_gpsd_precedes_modemmanager(tmp_path, monkeypatch):
    marker = tmp_path / "gpsd.conf"
    marker.write_text("HOST=127.0.0.1\nDEVICE=/dev/serial0\n")
    broker = _broker()
    gps = GpsManager(modem_broker=broker, modem_enabled=True,
                     gpsd_config=marker)
    monkeypatch.setattr(gps, "_try_gpsd", Mock(return_value=True))
    assert gps.setup()
    broker.acquire.assert_not_called()


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
    assert gps.fix.satellites_visible_known
    assert gps.fix.hdop == 0.8 and gps.fix.received_at >= before
    assert gps.data_flowing
    assert gps.status_reason == "GPS fix acquired"
    diagnostics = gps.diagnostics_snapshot()
    assert diagnostics["tpv_age"] is not None
    assert diagnostics["sky_age"] is not None
    assert diagnostics["satellite_view_age"] is not None
    assert diagnostics["satellites_visible_known"] is True
    assert diagnostics["satellites_used"] == 2
    assert diagnostics["satellites_visible"] == 3
    assert diagnostics["first_fix_after"] is None


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


def test_gpsd_sky_progress_precedes_first_tpv_fix(tmp_path):
    marker = tmp_path / "gpsd.conf"
    marker.write_text("HOST=localhost\n")
    gps = GpsManager(modem_broker=_broker(), modem_enabled=False,
                     gpsd_config=marker)
    gps.transport_connected = True
    gps.transport_connected_at = time.monotonic()
    gps._consume_gpsd_reports([{
        "class": "SKY",
        "satellites": [{"used": False}, {"used": False}],
    }])
    diagnostics = gps.diagnostics_snapshot()
    assert gps.navigation_state == "acquiring"
    assert diagnostics["tpv_age"] is None
    assert diagnostics["sky_age"] is not None
    assert diagnostics["satellites_visible"] == 2
    assert diagnostics["first_satellites_after"] is not None


def test_gpsd_sky_uses_list_before_summary_counters(tmp_path):
    marker = tmp_path / "gpsd.conf"
    marker.write_text("HOST=localhost\n")
    gps = GpsManager(modem_broker=_broker(), modem_enabled=False,
                     gpsd_config=marker)
    gps._consume_gpsd_reports([{
        "class": "SKY",
        "nSat": 99,
        "uSat": 88,
        "satellites": [{"used": True}, {"used": False}],
    }])
    assert gps.fix.satellites_visible == 2
    assert gps.fix.satellites == 1
    assert gps.fix.satellites_visible_known


def test_gpsd_sky_uses_nsats_and_usats_without_satellite_list(tmp_path):
    marker = tmp_path / "gpsd.conf"
    marker.write_text("HOST=localhost\n")
    gps = GpsManager(modem_broker=_broker(), modem_enabled=False,
                     gpsd_config=marker)
    gps._consume_gpsd_reports([{
        "class": "SKY", "nSat": 7, "uSat": 3,
    }])
    assert gps.fix.satellites_visible == 7
    assert gps.fix.satellites == 3
    assert gps.fix.satellites_visible_known
    assert gps.diagnostics_snapshot()["satellite_view_age"] is not None


def test_gpsd_sky_fallback_rejects_inconsistent_used_counts(tmp_path):
    marker = tmp_path / "gpsd.conf"
    marker.write_text("HOST=localhost\n")
    gps = GpsManager(modem_broker=_broker(), modem_enabled=False,
                     gpsd_config=marker)
    gps._consume_gpsd_reports([{"class": "SKY", "nSat": 5, "uSat": 2}])
    satellite_view_at = gps.satellite_view_at

    gps._consume_gpsd_reports([{"class": "SKY", "uSat": 4}])
    assert gps.fix.satellites_visible == 5
    assert gps.fix.satellites == 2
    assert gps.satellite_view_at == satellite_view_at

    gps._consume_gpsd_reports([{"class": "SKY", "nSat": 2, "uSat": 8}])
    assert gps.fix.satellites_visible == 2
    assert gps.fix.satellites == 0

    gps._consume_gpsd_reports([{"class": "SKY", "nSat": 3}])
    assert gps.fix.satellites_visible == 3
    assert gps.fix.satellites == 0


def test_gpsd_sky_missing_or_malformed_visibility_stays_unknown(tmp_path):
    marker = tmp_path / "gpsd.conf"
    marker.write_text("HOST=localhost\n")
    gps = GpsManager(modem_broker=_broker(), modem_enabled=False,
                     gpsd_config=marker)
    gps._consume_gpsd_reports([
        {"class": "SKY", "hdop": 2.1},
        {"class": "SKY", "nSat": "0", "uSat": "0"},
        {"class": "SKY", "nSat": -1, "uSat": True},
    ])
    diagnostics = gps.diagnostics_snapshot()
    assert diagnostics["sky_age"] is not None
    assert diagnostics["satellite_view_age"] is None
    assert diagnostics["satellites_visible_known"] is False
    assert gps.fix.satellites_visible == 0
    assert gps.fix.satellites == 0


def test_gpsd_sky_empty_list_is_explicit_zero(tmp_path):
    marker = tmp_path / "gpsd.conf"
    marker.write_text("HOST=localhost\n")
    gps = GpsManager(modem_broker=_broker(), modem_enabled=False,
                     gpsd_config=marker)
    gps._consume_gpsd_reports([{
        "class": "SKY", "nSat": 9, "uSat": 4, "satellites": [],
    }])
    assert gps.fix.satellites_visible == 0
    assert gps.fix.satellites == 0
    assert gps.fix.satellites_visible_known
    assert gps.satellite_view_at > 0


def test_gpsd_sky_malformed_update_preserves_known_view(tmp_path):
    marker = tmp_path / "gpsd.conf"
    marker.write_text("HOST=localhost\n")
    gps = GpsManager(modem_broker=_broker(), modem_enabled=False,
                     gpsd_config=marker)
    gps._consume_gpsd_reports([{"class": "SKY", "nSat": 4, "uSat": 2}])
    satellite_view_at = gps.satellite_view_at
    gps._consume_gpsd_reports([{"class": "SKY", "nSat": None}])
    assert gps.fix.satellites_visible == 4
    assert gps.fix.satellites == 2
    assert gps.fix.satellites_visible_known
    assert gps.satellite_view_at == satellite_view_at


def test_satellite_view_becomes_unknown_on_transport_reset(tmp_path):
    marker = tmp_path / "gpsd.conf"
    marker.write_text("HOST=localhost\n")
    gps = GpsManager(modem_broker=_broker(), modem_enabled=False,
                     gpsd_config=marker)
    gps._consume_gpsd_reports([{"class": "SKY", "nSat": 5, "uSat": 1}])
    gps._mark_transport_connected()
    assert gps.fix.satellites_visible == 0
    assert gps.fix.satellites == 0
    assert not gps.fix.satellites_visible_known
    assert gps.satellite_view_at == 0.0
    assert gps.diagnostics_snapshot()["satellite_view_age"] is None


def test_nmea_gsv_marks_satellite_view_known(tmp_path):
    marker = tmp_path / "gpsd.conf"
    gps = GpsManager(modem_broker=_broker(), modem_enabled=False,
                     gpsd_config=marker)
    gps.process_sentences(["$GPGSV,1,1,00*79"])
    assert gps.fix.satellites_visible == 0
    assert gps.fix.satellites_visible_known
    assert gps.satellite_view_at > 0


def test_gps_power_observation_is_telemetry_only(tmp_path):
    marker = tmp_path / "gpsd.conf"
    marker.write_text("HOST=localhost\n")
    gps = GpsManager(modem_broker=_broker(), modem_enabled=False,
                     gpsd_config=marker)
    before = time.monotonic()
    gps.note_power_state(True)
    diagnostics = gps.diagnostics_snapshot()
    assert diagnostics["power_enabled"] is True
    assert diagnostics["power_observed_age"] is not None
    assert gps.power_observed_at >= before


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
    gps.transport_connected = True
    gps.fix.satellites_visible = 6
    gps.fix.satellites_visible_known = True
    gps.satellite_view_at = time.monotonic()
    gps.read_available()
    assert not gps.available and gps.provider == "gpsd"
    assert not gps.transport_connected
    assert gps.socket_disconnects == 1
    assert gps.status_reason == "lost gpsd"
    assert not gps.fix.satellites_visible_known
    assert gps.satellite_view_at == 0.0
    fake.close.assert_called_once()


def test_valid_fix_freshness_and_explicit_no_fix_are_distinct(tmp_path, monkeypatch):
    marker = tmp_path / "gpsd.conf"
    marker.write_text("HOST=localhost\n")
    gps = GpsManager(modem_broker=_broker(), modem_enabled=False,
                     gpsd_config=marker)
    gps.transport_connected = True
    gps._last_data_at = time.monotonic()
    gps._consume_gpsd_reports([
        {"class": "TPV", "mode": 3, "lat": 40.0, "lon": -90.0},
    ])
    assert gps.navigation_state == "valid_fix"
    gps._consume_gpsd_reports([{"class": "TPV", "mode": 1}])
    assert not gps.fix.valid
    assert gps.navigation_state == "explicit_no_fix"


def test_app_reads_aio_power_before_opening_gps():
    source = (Path(__file__).resolve().parents[1] / "watchdogs" / "app.py").read_text()
    status_read = source.index('self._gps_enabled = _aio_st.get("gps", False)')
    setup_call = source.index("self.gps.setup()", status_read)
    assert status_read < setup_call
    assert 'GPS transport: {self.gps.device}' in source


def test_terminal_is_initialized_before_plugins_can_start_workers():
    source = (Path(__file__).resolve().parents[1] / "watchdogs" / "app.py").read_text()
    terminal_lock = source.index("self._term_lock = threading.Lock()")
    plugins = source.index("self._plugins = discover_plugins()")
    assert terminal_lock < plugins
