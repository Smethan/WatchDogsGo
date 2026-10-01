"""AIO GPS output recovery stays behind gpsd and logs no payloads."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "restore_aio_gps_output", ROOT / "scripts" / "restore_aio_gps_output.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class FakeSocket:
    def __init__(self, reports):
        self.payload = ("\n".join(json.dumps(value) for value in reports)
                        + "\n").encode()
        self.sent = b""

    def settimeout(self, _timeout):
        pass

    def sendall(self, payload):
        self.sent += payload

    def recv(self, _size):
        payload, self.payload = self.payload, b""
        return payload

    def close(self):
        pass


def test_detect_driver_uses_gpsd_device_reports_only():
    fake = FakeSocket([{
        "class": "DEVICES",
        "devices": [{"path": "/dev/serial0", "driver": "u-blox"}],
    }])
    driver = MODULE.detect_driver(
        "127.0.0.1", 2947, "/dev/serial0",
        connector=lambda *_args: fake)
    assert driver == "u-blox"
    assert b'"json":true' in fake.sent


def test_restore_nmea_sends_bounded_command_and_suppresses_payload():
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=(MODULE.ACK + "\n") * 9 + "private receiver payload")

    ok, detail = MODULE.restore_nmea(
        "127.0.0.1", 2947, "/dev/serial0", driver="u-blox",
        ubxtool="/usr/bin/ubxtool", runner=run)
    assert ok and detail == "nmea-restored-acks-9"
    assert calls[0][0] == [
        "/usr/bin/ubxtool", "-P", "10", "-e", "NMEA", "-w", "3",
        "127.0.0.1:2947:/dev/serial0",
    ]
    assert calls[0][1]["timeout"] == 15


def test_restore_nmea_skips_non_ublox_without_running_tool():
    ok, detail = MODULE.restore_nmea(
        "127.0.0.1", 2947, "/dev/serial0", driver="NMEA0183",
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ubxtool should not run")))
    assert ok and detail == "skipped-driver-NMEA0183"
