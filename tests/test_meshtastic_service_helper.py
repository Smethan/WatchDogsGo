"""Privilege-boundary tests for the Meshtastic helper and its client."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from watchdogs import meshtastic_service as service

ROOT = Path(__file__).resolve().parents[1]
HELPER_SOURCE = ROOT / "scripts" / "watchdogs_meshtastic_helper.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("wdg_test_meshtastic_helper", HELPER_SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def result(payload=None, *, returncode=0, stderr=""):
    return NS(
        returncode=returncode,
        stdout=json.dumps(payload or {"ok": True}),
        stderr=stderr,
    )


def test_controller_builds_only_fixed_helper_commands():
    controller = service.MeshtasticServiceController(
        runner=lambda *a, **k: result(), geteuid=lambda: 1000)
    helper = str(service.MESHTASTIC_HELPER)
    assert controller._command("status", "wdg") == [
        "sudo", "-n", helper, "status", "wdg"]
    assert controller._command("start", "stock") == [
        "sudo", "-n", helper, "start", "stock"]
    assert controller._command("install-tag", "v2.8.1-wdg.1") == [
        "sudo", "-n", helper, "install-tag", "v2.8.1-wdg.1"]
    assert controller._command("rollback") == [
        "sudo", "-n", helper, "rollback"]
    assert controller._command("version") == [helper, "version"]

    for operation, argument in (
        ("shell", None),
        ("start", "evil.service"),
        ("install-tag", "/tmp/package.deb"),
        ("install-tag", "v2.8.1-wdg.1;id"),
        ("rollback", "anything"),
    ):
        with pytest.raises(ValueError):
            controller._command(operation, argument)


def test_controller_parses_status_and_preserves_helper_errors():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if "status" in command:
            return result({
                "ok": True,
                "target": "wdg",
                "service": "meshtasticd-wdg.service",
                "load_state": "loaded",
                "active_state": "active",
                "unit_file_state": "enabled",
                "package_version": "2.8.1+wdg1",
            })
        return result({"ok": True, "helper_version": 1})

    controller = service.MeshtasticServiceController(
        runner=runner, geteuid=lambda: 0)
    assert controller.version() == 1
    status = controller.status("wdg")
    assert status.installed and status.active and status.enabled
    assert status.package_version == "2.8.1+wdg1"
    assert all(command[0] == str(service.MESHTASTIC_HELPER) for command in calls)

    failing = service.MeshtasticServiceController(
        runner=lambda *a, **k: result(returncode=1, stderr="health failed"),
        geteuid=lambda: 0)
    with pytest.raises(RuntimeError, match="health failed"):
        failing.install_tag("v2.8.1-wdg.1")


def test_source_helper_accepts_only_closed_cli(monkeypatch, capsys):
    helper = load_helper()
    monkeypatch.setattr(helper, "_service_status", lambda target: {
        "target": target,
        "service": helper.TARGET_SERVICES[target],
        "load_state": "loaded",
        "active_state": "inactive",
        "unit_file_state": "disabled",
        "package_version": None,
    })
    assert helper.main(["version"]) == 0
    assert json.loads(capsys.readouterr().out)["helper_version"] == 1
    assert helper.main(["status", "stock"]) == 0
    assert json.loads(capsys.readouterr().out)["service"] == "meshtasticd.service"

    rejected = (
        ["start", "other.service"],
        ["install-tag", "/tmp/evil.deb"],
        ["install-tag", "v2.8.1-wdg.1", "/tmp/evil.deb"],
        ["rollback", "/tmp/backup"],
        ["exec", "id"],
    )
    for argv in rejected:
        assert helper.main(list(argv)) == 1
        assert "Usage:" in capsys.readouterr().err


def test_helper_refuses_update_without_verified_rollback_package(
        tmp_path, monkeypatch):
    helper = load_helper()
    backup_root = tmp_path / "backups"
    installed = tmp_path / "installed"
    backup_root.mkdir(mode=0o700)
    installed.mkdir(mode=0o700)
    monkeypatch.setattr(helper, "BACKUP_ROOT", backup_root)
    monkeypatch.setattr(helper, "INSTALLED_CACHE", installed)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda path: None)
    monkeypatch.setattr(helper, "_installed_version", lambda: "2.8.0+wdg1")
    monkeypatch.setattr(helper, "_tree_fingerprint", lambda path: None)
    monkeypatch.setattr(helper, "_copy_state_to_backup", lambda path: {})
    monkeypatch.setattr(helper, "_service_snapshot", dict)
    prepared = NS(package_version="2.8.1+wdg1")
    validator = NS(validate_prepared_release=lambda *a, **k: None)
    with pytest.raises(helper.HelperError, match="no verified rollback package"):
        helper._create_backup("v2.8.1-wdg.1", prepared, validator, {})


def test_install_failure_runs_automatic_rollback(tmp_path, monkeypatch):
    helper = load_helper()
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    stage = cache / "v2.8.1-wdg.1"
    stage.mkdir(mode=0o700)
    package = stage / "meshtasticd-wdg_2.8.1+wdg1_arm64.deb"
    package.write_bytes(b"deb")
    package.chmod(0o600)
    prepared = NS(
        tag="v2.8.1-wdg.1", package_version="2.8.1+wdg1",
        package_path=package, directory=stage)
    validator = NS(validate_prepared_release=lambda *a, **k: prepared)
    backup = tmp_path / "backup"
    backup.mkdir(mode=0o700)
    metadata = {"format": 1}
    rolled_back = []
    monkeypatch.setattr(helper, "CACHE_ROOT", cache)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda path: None)
    monkeypatch.setattr(helper, "_load_validator", lambda: validator)
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_lock_transaction", lambda: _Context())
    monkeypatch.setattr(helper, "_create_backup", lambda *a: (backup, metadata))
    monkeypatch.setattr(helper, "_service_snapshot", lambda: {
        "wdg": {"load_state": "loaded"},
        "stock": {"load_state": "loaded"},
    })
    monkeypatch.setattr(helper, "_run", lambda *a, **k: NS(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(helper, "_installed_version", lambda: "wrong-version")
    monkeypatch.setattr(
        helper, "_restore_transaction",
        lambda backup_arg, metadata_arg, validator_arg: rolled_back.append(backup_arg))
    with pytest.raises(helper.HelperError, match="was rolled back"):
        helper._install_tag("v2.8.1-wdg.1")
    assert rolled_back == [backup]


class _Context:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
