"""Synthetic ModemManager location and safe GPS ownership tests."""

import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

from watchdogs.modem_location import (
    LOCATION, LOCATION_3GPP_LAC_CI, LOCATION_GPS_NMEA, MODEM, SIGNAL,
    ModemLocationBroker, legacy_modem_service_reason, managed_port_names,
    parse_3gpp_location, split_nmea, technology_from_access)
from watchdogs.gps_manager import GpsManager
from watchdogs.config import GPS_DEVICE


def test_parse_measured_sim7600_location_and_keep_mnc_width():
    cell = parse_3gpp_location("311,480,0,2038216,8308", 1 << 14)
    assert cell.operator_id == "311480"
    assert cell.technology == "LTE" and cell.area == 0x8308
    assert cell.cell_id == 0x2038216
    assert cell.identity == "311480_33544_33784342"
    leading = parse_3gpp_location("310,026,00af,00abc,", 1 << 5)
    assert leading.operator_id == "310026" and leading.area == 0xAF


def test_location_parser_rejects_partial_unknown_and_invalid_values():
    assert parse_3gpp_location("311,480,0,2038216", 1 << 14) is None
    assert parse_3gpp_location("311,480,0,0,8308", 1 << 14) is None
    assert parse_3gpp_location("311,480,0,2038216,0", 1 << 14) is None
    assert parse_3gpp_location("311,480,0,2038216,8308", 0) is None
    assert parse_3gpp_location("31,480,0,2038216,8308", 1 << 14) is None
    assert technology_from_access((1 << 14) | (1 << 15)) == "NR"
    assert technology_from_access(1 << 5) == "WCDMA"
    assert split_nmea("junk\r\n$GPGGA,1\r\n$GPRMC,2\n") == (
        "$GPGGA,1", "$GPRMC,2")


def test_legacy_service_detection_requires_direct_modem_access(tmp_path):
    unit = tmp_path / "uconsole-sim.service"
    script = tmp_path / "setup-sim-gps"
    unit.write_text("[Service]\nExecStart=/usr/local/bin/setup-sim-gps\n")
    script.write_text("echo 'AT+CGPS=1,1' >/dev/ttyUSB3\n")
    assert "outside ModemManager" in legacy_modem_service_reason(unit, script)
    unit.write_text("[Service]\nExecStart=/usr/bin/uconsole-4g enable\nRemainAfterExit=yes\n")
    assert legacy_modem_service_reason(unit, script) == ""


class FakeModem:
    def __init__(self, enabled=LOCATION_3GPP_LAC_CI):
        self.enabled = enabled
        self.setup_calls = []
        self.locations = {
            LOCATION_3GPP_LAC_CI: "311,480,0,2038216,8308",
            LOCATION_GPS_NMEA: "$GPGGA,120000,4000.000,N,09000.000,W,1,8,1.0,10,M,,M,,",
        }

    def Get(self, interface, prop, timeout=0):
        assert interface == LOCATION and prop == "Enabled"
        return self.enabled

    def GetAll(self, interface, timeout=0):
        if interface == MODEM:
            return {"AccessTechnologies": 1 << 14, "SignalQuality": (65, True)}
        if interface == SIGNAL:
            return {"Lte": {"rsrp": -101.5}}
        raise AssertionError(interface)

    def Setup(self, sources, signal_location, timeout=0):
        assert signal_location is False
        self.enabled = int(sources)
        self.setup_calls.append(self.enabled)

    def GetLocation(self, timeout=0):
        return self.locations


class FakeRoot:
    def __init__(self, modem):
        self.modem = modem

    def GetManagedObjects(self, timeout=0):
        return {"/org/freedesktop/ModemManager1/Modem/0": {
            MODEM: {"Manufacturer": "QUALCOMM INCORPORATED",
                    "Model": "SIMCOM_SIM7600G-H", "Revision": "LE20B04",
                    "PrimaryPort": "cdc-wdm0",
                    "Ports": (("cdc-wdm0", 6), ("ttyUSB1", 5),
                              ("ttyUSB2", 3), ("ttyUSB3", 3))},
            LOCATION: {"Capabilities": LOCATION_3GPP_LAC_CI | LOCATION_GPS_NMEA,
                       "Enabled": self.modem.enabled}}}


class FakeBus:
    def __init__(self, root, modem):
        self.root, self.modem = root, modem

    def get_object(self, _service, path):
        return self.root if path == "/org/freedesktop/ModemManager1" else self.modem


def test_broker_adds_only_gps_and_restores_original_mask(monkeypatch):
    modem = FakeModem()
    bus = FakeBus(FakeRoot(modem), modem)
    monkeypatch.setitem(sys.modules, "dbus", SimpleNamespace(
        Interface=lambda obj, _interface: obj, SystemBus=lambda: bus))
    broker = ModemLocationBroker(bus_factory=lambda: bus, poll_interval=.01,
                                 legacy_check=lambda: "")
    assert broker.acquire("gps", gps=True)
    assert broker.acquire("cell", cell=True)
    assert broker.wait_ready(1)
    snapshot = broker.snapshot()
    assert snapshot.identity == "311480_33544_33784342"
    assert snapshot.signal_dbm == -101.5 and snapshot.signal_quality_percent == 65
    deadline = time.time() + 1
    nmea = []
    while time.time() < deadline and not nmea:
        nmea = broker.drain_nmea()
        time.sleep(.01)
    assert nmea and nmea[0].startswith("$GPGGA")
    broker.close()
    assert modem.setup_calls[0] == LOCATION_3GPP_LAC_CI | LOCATION_GPS_NMEA
    assert modem.setup_calls[-1] == LOCATION_3GPP_LAC_CI


def test_broker_releases_only_its_unneeded_source_while_running(monkeypatch):
    modem = FakeModem(enabled=0)
    bus = FakeBus(FakeRoot(modem), modem)
    monkeypatch.setitem(sys.modules, "dbus", SimpleNamespace(
        Interface=lambda obj, _interface: obj, SystemBus=lambda: bus))
    broker = ModemLocationBroker(bus_factory=lambda: bus, poll_interval=.01,
                                 legacy_check=lambda: "")
    assert broker.acquire("gps", gps=True)
    assert broker.acquire("cell", cell=True)
    assert broker.wait_ready(1)
    deadline = time.time() + 1
    both = LOCATION_GPS_NMEA | LOCATION_3GPP_LAC_CI
    while time.time() < deadline and modem.enabled != both:
        time.sleep(.01)
    assert modem.enabled == both
    broker.release("cell")
    deadline = time.time() + 1
    while time.time() < deadline and modem.enabled != LOCATION_GPS_NMEA:
        time.sleep(.01)
    assert modem.enabled == LOCATION_GPS_NMEA
    broker.close()
    assert modem.enabled == 0


def test_managed_ports_come_from_modemmanager_inventory(monkeypatch):
    modem = FakeModem()
    bus = FakeBus(FakeRoot(modem), modem)
    monkeypatch.setitem(sys.modules, "dbus", SimpleNamespace(
        Interface=lambda obj, _interface: obj, SystemBus=lambda: bus))
    assert managed_port_names(bus) == {
        "cdc-wdm0", "ttyUSB1", "ttyUSB2", "ttyUSB3"}


def test_gps_manager_consumes_broker_nmea_and_releases_it(monkeypatch):
    monkeypatch.delenv("WDG_GPS_DEVICE", raising=False)
    monkeypatch.delenv("JANOS_GPS_DEVICE", raising=False)
    broker = SimpleNamespace(
        error="", acquire=lambda *a, **k: True, wait_ready=lambda _timeout: True,
        drain_nmea=lambda: ["$GPGGA,120000,4000.000,N,09000.000,W,1,8,1.0,10,M,,M,,"],
        release=Mock(), close=Mock())
    gps = GpsManager(modem_broker=broker)
    assert gps.setup() and gps.provider == "modemmanager"
    assert gps.read_available()[0].startswith("$GPGGA")
    gps.close()
    broker.release.assert_called_once_with("gps")
    broker.close.assert_called_once()


def test_serial_gps_cleanup_also_closes_cell_broker(monkeypatch):
    monkeypatch.delenv("WDG_GPS_DEVICE", raising=False)
    monkeypatch.delenv("JANOS_GPS_DEVICE", raising=False)
    broker = SimpleNamespace(close=Mock())
    gps = GpsManager(modem_broker=broker)
    gps.provider = "serial"
    gps.close()
    broker.close.assert_called_once()


def test_explicit_modem_tty_is_rejected_without_open(monkeypatch):
    monkeypatch.setenv("WDG_GPS_DEVICE", "/dev/ttyUSB1")
    monkeypatch.setattr("watchdogs.gps_manager.managed_port_names",
                        lambda: {"ttyUSB1", "ttyUSB2"})
    gps = GpsManager(device="/dev/ttyUSB1")
    gps._try_open = Mock(side_effect=AssertionError("managed tty must stay closed"))
    assert not gps.setup()
    assert "owned by ModemManager" in gps.status_reason
    gps._try_open.assert_not_called()


def test_lte_disabled_skips_modemmanager_and_uses_aio_uart(monkeypatch):
    monkeypatch.delenv("WDG_GPS_DEVICE", raising=False)
    monkeypatch.delenv("JANOS_GPS_DEVICE", raising=False)
    monkeypatch.setattr("watchdogs.gps_manager.managed_port_names",
                        Mock(side_effect=AssertionError("MM inventory must not be queried")))
    monkeypatch.setattr("watchdogs.gps_manager.os.path.exists",
                        lambda path: path == "/dev/ttyAMA0")
    broker = SimpleNamespace(
        acquire=Mock(side_effect=AssertionError("MM broker must not start")),
        close=Mock(), error="")
    gps = GpsManager(device="/dev/ttyAMA0", modem_broker=broker,
                     modem_enabled=False)
    gps._probe_nmea = Mock(return_value=True)
    gps._try_open = Mock(return_value=True)

    assert gps.setup()
    gps._probe_nmea.assert_called_once_with("/dev/ttyAMA0", gps._baud)
    gps._try_open.assert_called_once_with("/dev/ttyAMA0")
    broker.acquire.assert_not_called()


def test_cm4_aio_uses_ttys0_without_probing_bluetooth_uart(monkeypatch):
    monkeypatch.delenv("WDG_GPS_DEVICE", raising=False)
    monkeypatch.delenv("JANOS_GPS_DEVICE", raising=False)
    monkeypatch.setattr(GpsManager, "_platform_model",
                        staticmethod(lambda: "Raspberry Pi Compute Module 4 Rev 1.1"))
    monkeypatch.setattr("watchdogs.gps_manager.managed_port_names",
                        Mock(side_effect=AssertionError("MM inventory must not be queried")))
    monkeypatch.setattr("watchdogs.gps_manager.os.path.exists",
                        lambda path: path in ("/dev/ttyS0", "/dev/ttyAMA0"))
    broker = SimpleNamespace(acquire=Mock(), close=Mock(), error="")
    gps = GpsManager(modem_broker=broker, modem_enabled=False)
    gps._probe_nmea = Mock(return_value=True)
    gps._try_open = Mock(return_value=True)

    assert gps.setup()
    gps._probe_nmea.assert_called_once_with("/dev/ttyS0", gps._baud)
    gps._try_open.assert_called_once_with("/dev/ttyS0")
    assert gps.device == "/dev/ttyS0"
    assert not any(call.args[0] == "/dev/ttyAMA0"
                   for call in gps._probe_nmea.call_args_list)


def test_cm5_aio_uses_ttyama0(monkeypatch):
    monkeypatch.setattr(GpsManager, "_platform_model",
                        staticmethod(lambda: "Raspberry Pi Compute Module 5 Rev 1.0"))
    monkeypatch.setattr("watchdogs.gps_manager.os.path.exists", lambda _path: False)
    assert GpsManager._platform_gps_candidates(GPS_DEVICE) == [
        "/dev/ttyAMA0", "/dev/serial0"]


def test_missing_modem_falls_back_to_external_gps_without_raising(monkeypatch):
    monkeypatch.delenv("WDG_GPS_DEVICE", raising=False)
    monkeypatch.delenv("JANOS_GPS_DEVICE", raising=False)
    monkeypatch.setattr("watchdogs.gps_manager.managed_port_names", lambda: set())
    monkeypatch.setattr("watchdogs.gps_manager.os.path.exists",
                        lambda path: path == "/dev/ttyAMA0")
    broker = SimpleNamespace(
        error="No ModemManager modem with location support",
        acquire=Mock(return_value=True), wait_ready=Mock(return_value=False),
        release=Mock(), close=Mock())
    gps = GpsManager(device="/dev/ttyAMA0", modem_broker=broker)
    gps._probe_nmea = Mock(return_value=True)
    gps._try_open = Mock(return_value=True)

    assert gps.setup()
    broker.release.assert_called_once_with("gps")
    broker.close.assert_called_once()
    gps._try_open.assert_called_once_with("/dev/ttyAMA0")


def test_disabling_modem_gps_releases_broker_before_external_reconnect(monkeypatch):
    monkeypatch.delenv("WDG_GPS_DEVICE", raising=False)
    monkeypatch.delenv("JANOS_GPS_DEVICE", raising=False)
    monkeypatch.setattr("watchdogs.gps_manager.managed_port_names",
                        Mock(side_effect=AssertionError("MM inventory must not be queried")))
    monkeypatch.setattr("watchdogs.gps_manager.os.path.exists",
                        lambda path: path == "/dev/ttyAMA0")
    broker = SimpleNamespace(release=Mock(), close=Mock())
    gps = GpsManager(device="/dev/ttyAMA0", modem_broker=broker)
    gps.provider = "modemmanager"
    gps.device = "ModemManager GNSS"
    gps._available = True
    gps._probe_nmea = Mock(return_value=True)
    gps._try_open = Mock(return_value=True)

    assert gps.set_modem_enabled(False)
    assert not gps.modem_enabled
    broker.release.assert_called_once_with("gps")
    broker.close.assert_called_once()
    gps._probe_nmea.assert_called_once_with("/dev/ttyAMA0", gps._baud)
    gps._try_open.assert_called_once_with("/dev/ttyAMA0")
