"""The standalone diagnostic must use the same safe serial path as WDG."""

import json
import sys

from watchdogs import diagnose_firmware as diagnostic


def test_diagnostic_uses_serial_manager_for_native_jtag_probe(monkeypatch, capsys):
    now = [0.0]
    instances = []

    class Manager:
        def __init__(self, port):
            self.port = port
            self.commands = []
            self.lines = []
            self.closed = False
            instances.append(self)

        def setup(self):
            pass

        def send_command(self, command):
            self.commands.append(command)
            if command == "get_capabilities":
                self.lines.append(
                    "WDG:"
                    + json.dumps(
                        {
                            "v": 1,
                            "kind": "capabilities",
                            "wardrive_serial_v1": True,
                        }
                    )
                )

        def read_available(self, *, max_bytes, max_lines):
            assert max_bytes == 4096 and max_lines == 64
            lines, self.lines = self.lines, []
            return lines

        def close(self):
            self.closed = True

    def monotonic():
        return now[0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(diagnostic, "SerialManager", Manager)
    monkeypatch.setattr(diagnostic.time, "monotonic", monotonic)
    monkeypatch.setattr(diagnostic.time, "sleep", sleep)
    monkeypatch.setattr(sys, "argv", ["diagnose_firmware", "--port", "/dev/ttyACM0"])

    assert diagnostic.main() == 0
    manager = instances[0]
    assert manager.port == "/dev/ttyACM0"
    assert manager.commands == [
        "version",
        "get_capabilities",
        "get_capabilities",
        "get_capabilities",
    ]
    assert manager.closed
    assert "RESULT: All Wardrive SUPPORTED" in capsys.readouterr().out


def test_diagnostic_reports_bounded_degraded_capabilities(monkeypatch, capsys):
    now = [0.0]

    class Manager:
        def __init__(self, port):
            self.lines = []

        def setup(self):
            pass

        def send_command(self, command):
            if command == "get_capabilities":
                self.lines.append(
                    "WDG:"
                    + json.dumps(
                        {
                            "v": 1,
                            "kind": "capabilities",
                            "c5_jobs_v1": False,
                            "max_line": 1024,
                        }
                    )
                )

        def read_available(self, *, max_bytes, max_lines):
            lines, self.lines = self.lines, []
            return lines

        def close(self):
            pass

    monkeypatch.setattr(diagnostic, "SerialManager", Manager)
    monkeypatch.setattr(diagnostic.time, "monotonic", lambda: now[0])

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(diagnostic.time, "sleep", sleep)
    monkeypatch.setattr(sys, "argv", ["diagnose_firmware", "--port", "/dev/ttyACM0"])

    assert diagnostic.main() == 1
    output = capsys.readouterr().out
    assert "RESULT: Firmware replied; C5 jobs are unavailable" in output
    assert "No valid capability reply" not in output


def test_degraded_capability_fallback_is_exact_and_bounded():
    degraded = 'WDG:{"v":1,"kind":"capabilities","c5_jobs_v1":false,"max_line":1024}'
    parsed = diagnostic.parse_diagnostic_capabilities(degraded)
    assert parsed is not None and parsed["c5_jobs_v1"] is False
    assert (
        diagnostic.parse_diagnostic_capabilities(
            degraded.replace('"max_line":1024', '"max_line":true')
        )
        is None
    )
    assert diagnostic.parse_diagnostic_capabilities(degraded + " " * 1024) is None
