"""Regression coverage for repeated setup and inconsistent ADS-B detection."""

from unittest.mock import Mock

import pytest

from watchdogs import __main__ as startup
from watchdogs import sdr_manager, sdr_tools


@pytest.mark.parametrize("name", sdr_tools.DUMP1090_NAMES)
def test_finds_installed_variants(tmp_path, monkeypatch, name):
    executable = tmp_path / name
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(sdr_tools, "SYSTEM_BIN_DIRS", ())
    assert sdr_tools.find_dump1090() == str(executable)
    assert sdr_manager.SDRManager.has_dump1090()


def test_finds_source_install_outside_sudo_path(tmp_path, monkeypatch):
    executable = tmp_path / "dump1090"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(sdr_tools, "SYSTEM_BIN_DIRS", (str(tmp_path),))
    assert sdr_tools.find_dump1090() == str(executable)
    executable.chmod(0o644)
    assert sdr_tools.find_dump1090() is None


def test_missing_optional_tools_never_rerun_setup(monkeypatch, capsys):
    monkeypatch.setattr(startup, "_check_deps", lambda: [
        ("pyxel", "OK", True, True),
        ("dump1090", "NOT INSTALLED", False, False),
        ("LoRaRF", "NOT INSTALLED", False, False),
    ])
    setup = Mock()
    monkeypatch.setattr(startup, "_run_setup", setup)
    assert startup.preflight()
    assert startup.preflight()
    setup.assert_not_called()
    assert "bash setup.sh" in capsys.readouterr().out


def test_missing_required_dependencies_still_install(monkeypatch):
    checks = Mock(side_effect=[
        [("pyxel", "NOT INSTALLED", False, True)],
        [("pyxel", "OK", True, True), ("dump1090", "missing", False, False)],
    ])
    setup = Mock(return_value=True)
    monkeypatch.setattr(startup, "_check_deps", checks)
    monkeypatch.setattr(startup, "_run_setup", setup)
    assert startup.preflight()
    setup.assert_called_once()


@pytest.mark.parametrize("retry", [False, True])
def test_adsb_launches_resolved_binary_including_retry(monkeypatch, retry):
    binary = "/usr/local/bin/dump1090-fa"
    monkeypatch.setattr(sdr_manager, "find_dump1090", lambda: binary)
    process = Mock()
    process.poll.return_value = 1 if retry else None
    popen = Mock(return_value=process)
    monkeypatch.setattr(sdr_manager.subprocess, "Popen", popen)
    monkeypatch.setattr(sdr_manager.subprocess, "run", Mock())
    monkeypatch.setattr(sdr_manager.time, "sleep", Mock())
    monkeypatch.setattr(sdr_manager.threading, "Thread", Mock())
    manager = sdr_manager.SDRManager()
    assert manager.start_adsb()
    commands = [call.args[0] for call in popen.call_args_list]
    expected = [[binary, "--net", "--quiet"]]
    if retry:
        expected.append([binary, "--net"])
    assert commands == expected
