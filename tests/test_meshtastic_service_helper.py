"""Privilege-boundary tests for the Meshtastic helper and its client."""

from __future__ import annotations

import fcntl
import importlib.util
import io
import json
import os
import shutil
import signal
import stat
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, call

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


def candidate_live_state(helper, tmp_path, monkeypatch):
    """Create two live state roots plus the helper's protected copy layout."""
    live_config = tmp_path / "live-etc-meshtasticd"
    live_state = tmp_path / "live-var-lib-meshtasticd"
    live_config.mkdir()
    live_state.mkdir()
    (live_config / "config.yaml").write_text(
        "General:\n  MACAddress: 02:00:A1:B2:C3:D4\n", encoding="utf-8")
    (live_state / "identity.bin").write_bytes(b"original identity")
    monkeypatch.setattr(helper, "MESHTASTIC_CONFIG_DIR", live_config)
    monkeypatch.setattr(helper, "MESHTASTIC_STATE_DIR", live_state)
    monkeypatch.setattr(helper, "STATE_PATHS", (live_config, live_state))
    snapshot = tmp_path / "candidate-state-snapshot"
    snapshot.mkdir()
    presence = helper._copy_state_to_backup(snapshot)
    before = helper._current_state_fingerprints()
    return live_config, live_state, snapshot, presence, before


def test_controller_builds_only_fixed_helper_commands():
    controller = service.MeshtasticServiceController(
        runner=lambda *a, **k: result(), geteuid=lambda: 1000)
    helper = str(service.MESHTASTIC_HELPER)
    assert controller._command("status", "wdg") == [
        "sudo", "-n", helper, "status", "wdg"]
    assert controller._command("start", "stock") == [
        "sudo", "-n", helper, "start", "stock"]
    assert controller._command("select-service", "stock") == [
        "sudo", "-n", helper, "select-service", "stock"]
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
        return result({"ok": True, "helper_version": 6})

    controller = service.MeshtasticServiceController(
        runner=runner, geteuid=lambda: 0)
    assert controller.version() == 6
    controller.require_current()
    status = controller.status("wdg")
    assert status.installed and status.active and status.enabled
    assert status.package_version == "2.8.1+wdg1"
    assert all(command[0] == str(service.MESHTASTIC_HELPER) for command in calls)

    failing = service.MeshtasticServiceController(
        runner=lambda *a, **k: result(returncode=1, stderr="health failed"),
        geteuid=lambda: 0)
    with pytest.raises(RuntimeError, match="health failed"):
        failing.install_tag("v2.8.1-wdg.1")


def test_controller_rejects_pre_hardening_helper_version():
    controller = service.MeshtasticServiceController(
        runner=lambda *a, **k: result({"ok": True, "helper_version": 5}),
        geteuid=lambda: 0)

    with pytest.raises(RuntimeError, match="outdated.*setup.sh"):
        controller.require_current()


def test_controller_exposes_structured_install_rollback_outcome():
    payload = {
        "ok": False,
        "action": "install-tag",
        "error_type": "install_failed",
        "error": "candidate health failed; previous package restored",
        "rollback_restored": True,
        "backup": "/var/backups/meshtasticd-wdg/20260925T120000Z-test",
    }
    controller = service.MeshtasticServiceController(
        runner=lambda *a, **k: result(payload, returncode=2),
        geteuid=lambda: 0)

    with pytest.raises(service.MeshtasticInstallError) as caught:
        controller.install_tag("v2.8.1-wdg.1")

    assert caught.value.rollback_restored is True
    assert caught.value.backup == Path(payload["backup"])


@pytest.mark.parametrize("mutation", [
    {"rollback_restored": "yes"},
    {"backup": "/tmp/untrusted"},
    {"ok": True},
    {"unexpected": "field"},
])
def test_controller_rejects_malformed_structured_install_failure(mutation):
    payload = {
        "ok": False,
        "action": "install-tag",
        "error_type": "install_failed",
        "error": "failed",
        "rollback_restored": False,
        "backup": None,
    }
    payload.update(mutation)
    controller = service.MeshtasticServiceController(
        runner=lambda *a, **k: result(payload, returncode=2),
        geteuid=lambda: 0)

    with pytest.raises(RuntimeError) as caught:
        controller.install_tag("v2.8.1-wdg.1")

    assert not isinstance(caught.value, service.MeshtasticInstallError)


@pytest.mark.parametrize(
    ("method", "action"),
    (("start", "start"), ("stop", "stop"),
     ("enable", "enable"), ("disable", "disable")),
)
def test_controller_uses_authoritative_mutation_reply(method, action):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        assert "status" not in command
        return result({
            "ok": True,
            "action": action,
            "target": "wdg",
            "service": "meshtasticd-wdg.service",
            "load_state": "loaded",
            "active_state": "active" if action == "start" else "inactive",
            "unit_file_state": "enabled",
            "package_version": "2.8.1+wdg1",
        })

    controller = service.MeshtasticServiceController(
        runner=runner, geteuid=lambda: 0)
    status = getattr(controller, method)("wdg")

    assert status.target == "wdg"
    assert len(calls) == 1


def test_controller_uses_authoritative_select_reply():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return result({
            "ok": True,
            "action": "select-service",
            "selected": "stock",
            "target": "stock",
            "service": "meshtasticd.service",
            "load_state": "loaded",
            "active_state": "active",
            "unit_file_state": "enabled",
            "package_version": None,
        })

    controller = service.MeshtasticServiceController(
        runner=runner, geteuid=lambda: 0)
    assert controller.select("stock").active
    assert len(calls) == 1


def test_controller_transaction_timeout_exceeds_helper_phases():
    seen = []

    def runner(command, **kwargs):
        seen.append(kwargs["timeout"])
        return result({"ok": True, "package_version": "2.8.1+wdg1"})

    controller = service.MeshtasticServiceController(
        runner=runner, geteuid=lambda: 0)
    controller.install_tag("v2.8.1-wdg.1")
    controller.rollback()

    assert seen == [service.TRANSACTION_TIMEOUT, service.TRANSACTION_TIMEOUT]
    assert service.TRANSACTION_TIMEOUT > 300 + 45 + 30 + 30


@pytest.mark.parametrize("target", ["wdg", "stock"])
def test_source_helper_stops_status_queries_after_missing_load_state(
        target, monkeypatch):
    helper = load_helper()
    calls = []

    def prop(service_name, property_name):
        calls.append((service_name, property_name))
        assert property_name == "LoadState"
        return "not-found"

    monkeypatch.setattr(helper, "_systemd_property", prop)
    monkeypatch.setattr(helper, "_installed_version", lambda: None)

    status = helper._service_status(target)

    assert status["load_state"] == "not-found"
    assert status["active_state"] == "inactive"
    assert status["unit_file_state"] == "not-found"
    assert calls == [(helper.TARGET_SERVICES[target], "LoadState")]


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
    assert json.loads(capsys.readouterr().out)["helper_version"] == 6
    assert helper.main(["status", "stock"]) == 0
    assert json.loads(capsys.readouterr().out)["service"] == "meshtasticd.service"

    rejected = (
        ["start", "other.service"],
        ["select-service", "other.service"],
        ["install-tag", "/tmp/evil.deb"],
        ["install-tag", "v02.8.1-wdg.1"],
        ["install-tag", "v2.8.1-wdg.1", "/tmp/evil.deb"],
        ["prepare-first-tag", "/tmp/evil.deb"],
        ["prepare-first-tag", "v2.08.1-wdg.1"],
        ["adopt-installed", "/tmp/evil.deb"],
        ["adopt-installed", "v2.8.1-wdg.1", "/tmp/evil.deb"],
        ["rollback", "/tmp/backup"],
        ["exec", "id"],
    )
    for argv in rejected:
        assert helper.main(list(argv)) == 1
        assert "Usage:" in capsys.readouterr().err


def test_source_helper_emits_structured_nonzero_install_outcome(
        monkeypatch, capsys):
    helper = load_helper()
    backup = Path("/var/backups/meshtasticd-wdg/20260925T120000Z-test")
    monkeypatch.setattr(
        helper, "_install_tag",
        Mock(side_effect=helper.InstallTransactionError(
            "candidate failed and rollback failed",
            rollback_restored=False, backup=backup)))

    assert helper.main(["install-tag", "v2.8.1-wdg.1"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "ok": False,
        "action": "install-tag",
        "error_type": "install_failed",
        "error": "candidate failed and rollback failed",
        "rollback_restored": False,
        "backup": str(backup),
    }


def test_source_helper_serializes_direct_service_mutations(monkeypatch):
    helper = load_helper()
    events = []

    class Lock:
        def __enter__(self):
            events.append("lock-enter")

        def __exit__(self, *_args):
            events.append("lock-exit")

    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_lock_transaction", Lock)
    monkeypatch.setattr(
        helper, "_run",
        lambda command, **_kwargs: events.append(tuple(command)))
    monkeypatch.setattr(
        helper, "_service_status",
        lambda target: events.append(("status", target)) or {
            "target": target,
            "service": helper.TARGET_SERVICES[target],
            "load_state": "loaded",
            "active_state": "active",
            "unit_file_state": "enabled",
            "package_version": "2.8.1+wdg1",
        })

    helper._set_service("start", "wdg")

    assert events == [
        "lock-enter",
        ("systemctl", "start", "meshtasticd-wdg.service"),
        ("status", "wdg"),
        "lock-exit",
    ]


def _prepare_test_transaction_lock(helper, tmp_path, monkeypatch):
    lock_directory = tmp_path / "watchdogs"
    lock_directory.mkdir(mode=0o750)
    lock_directory.chmod(0o2750)
    lock_path = lock_directory / "meshtastic-update.lock"
    lock_path.write_bytes(b"")
    lock_path.chmod(0o600)
    expected_gid = os.getgid()
    real_fstat = os.fstat

    def trusted_fstat(descriptor):
        info = real_fstat(descriptor)
        return NS(
            st_mode=info.st_mode,
            st_uid=0,
            st_gid=expected_gid if stat.S_ISDIR(info.st_mode) else 0,
            st_nlink=info.st_nlink,
            st_dev=info.st_dev,
            st_ino=info.st_ino,
        )

    monkeypatch.setattr(helper, "LOCK_DIRECTORY", lock_directory)
    monkeypatch.setattr(helper, "LOCK_PATH", lock_path)
    monkeypatch.setattr(
        helper.grp, "getgrnam", lambda _name: NS(gr_gid=expected_gid))
    monkeypatch.setattr(helper.os, "fstat", trusted_fstat)
    monkeypatch.setattr(helper.os, "fchown", lambda *_args: None)
    return lock_path


def test_transaction_lock_serializes_precreated_protected_inode(
        tmp_path, monkeypatch):
    helper = load_helper()
    _prepare_test_transaction_lock(helper, tmp_path, monkeypatch)

    with helper._lock_transaction():
        with pytest.raises(helper.HelperError, match="Another Meshtastic"):
            helper._lock_transaction()
    with helper._lock_transaction():
        pass


def test_transaction_lock_rejects_unlinked_replacement_inode(
        tmp_path, monkeypatch):
    helper = load_helper()
    lock_path = _prepare_test_transaction_lock(helper, tmp_path, monkeypatch)
    real_flock = fcntl.flock
    replaced = False

    def replace_after_lock(descriptor, operation):
        nonlocal replaced
        real_flock(descriptor, operation)
        if not replaced:
            replaced = True
            lock_path.unlink()
            lock_path.write_bytes(b"replacement")
            lock_path.chmod(0o600)

    monkeypatch.setattr(helper.fcntl, "flock", replace_after_lock)

    with pytest.raises(helper.HelperError, match="changed while being acquired"):
        helper._lock_transaction()


@pytest.mark.parametrize("field,value", [
    ("unit_file_state", "enabled-runtime"),
    ("unit_file_state", "masked"),
    ("unit_file_state", "linked"),
    ("active_state", "failed"),
    ("load_state", "masked"),
])
def test_service_snapshot_rejects_states_it_cannot_restore_exactly(
        monkeypatch, field, value):
    helper = load_helper()
    states = {
        "wdg": {
            "target": "wdg", "service": helper.TARGET_SERVICES["wdg"],
            "load_state": "loaded", "active_state": "inactive",
            "unit_file_state": "disabled", "package_version": None,
        },
        "stock": {
            "target": "stock", "service": helper.TARGET_SERVICES["stock"],
            "load_state": "loaded", "active_state": "inactive",
            "unit_file_state": "disabled", "package_version": None,
        },
    }
    states["wdg"][field] = value
    monkeypatch.setattr(helper, "_service_status", lambda target: states[target])

    with pytest.raises(helper.HelperError, match="Cannot preserve"):
        helper._service_snapshot()


def test_restore_rejects_unrestorable_snapshot_before_systemctl(monkeypatch):
    helper = load_helper()
    run = Mock()
    monkeypatch.setattr(helper, "_run", run)
    snapshot = {
        "wdg": {
            "load_state": "loaded", "active_state": "inactive",
            "unit_file_state": "enabled-runtime",
        },
        "stock": {
            "load_state": "loaded", "active_state": "inactive",
            "unit_file_state": "disabled",
        },
    }

    with pytest.raises(helper.HelperError, match="Cannot preserve"):
        helper._restore_services(snapshot)

    run.assert_not_called()


def test_source_helper_dispatches_only_a_strict_adoption_tag(
        monkeypatch, capsys):
    helper = load_helper()
    adopted = []
    monkeypatch.setattr(
        helper, "_adopt_installed",
        lambda tag: adopted.append(tag) or {
            "action": "adopt-installed",
            "tag": tag,
            "package_version": "2.8.1+wdg1",
            "health": "ready",
        })

    assert helper.main(["adopt-installed", "v2.8.1-wdg.1"]) == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply["action"] == "adopt-installed"
    assert adopted == ["v2.8.1-wdg.1"]


def test_select_service_commits_only_after_target_is_active(monkeypatch):
    helper = load_helper()
    services = {
        "wdg": {"load_state": "loaded", "active_state": "active",
                "unit_file_state": "enabled"},
        "stock": {"load_state": "loaded", "active_state": "inactive",
                  "unit_file_state": "disabled"},
    }
    active = {"meshtasticd-wdg.service": "active",
              "meshtasticd.service": "inactive"}
    enabled = []
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_lock_transaction", nullcontext)
    monkeypatch.setattr(helper, "_service_snapshot", lambda: services)
    monkeypatch.setattr(helper, "_restore_services", Mock())

    def run(command, **_kwargs):
        if command[1] == "start":
            active[command[2]] = "active"
            other = ("meshtasticd-wdg.service"
                     if command[2] == "meshtasticd.service" else
                     "meshtasticd.service")
            active[other] = "inactive"
        return NS(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(helper, "_run", run)
    monkeypatch.setattr(
        helper, "_systemd_property",
        lambda service, prop: active[service] if prop == "ActiveState" else "loaded")
    monkeypatch.setattr(
        helper, "_set_enabled",
        lambda service, value: enabled.append((service, value)))
    monkeypatch.setattr(helper, "_service_status", lambda target: {
        "target": target, "service": helper.TARGET_SERVICES[target],
        "load_state": "loaded", "active_state": active[
            helper.TARGET_SERVICES[target]], "unit_file_state": "enabled",
        "package_version": None,
    })

    selected = helper._select_service("stock")

    assert selected["selected"] == "stock"
    assert selected["active_state"] == "active"
    assert enabled == [
        ("meshtasticd.service", True),
        ("meshtasticd-wdg.service", False),
    ]
    helper._restore_services.assert_not_called()


@pytest.mark.parametrize(
    ("target", "other"), (("wdg", "stock"), ("stock", "wdg")))
def test_select_service_does_not_probe_missing_alternate_unit(
        target, other, monkeypatch):
    helper = load_helper()
    services = {
        target: {"load_state": "loaded", "active_state": "inactive",
                 "unit_file_state": "disabled"},
        other: {"load_state": "not-found", "active_state": "inactive",
                "unit_file_state": "not-found"},
    }
    property_calls = []
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_lock_transaction", nullcontext)
    monkeypatch.setattr(helper, "_service_snapshot", lambda: services)
    monkeypatch.setattr(helper, "_restore_services", Mock())
    monkeypatch.setattr(
        helper, "_run",
        Mock(return_value=NS(returncode=0, stdout="", stderr="")))

    def prop(service_name, property_name):
        property_calls.append((service_name, property_name))
        assert service_name == helper.TARGET_SERVICES[target]
        assert property_name == "ActiveState"
        return "active"

    monkeypatch.setattr(helper, "_systemd_property", prop)
    monkeypatch.setattr(helper, "_set_enabled", Mock())
    monkeypatch.setattr(helper, "_service_status", lambda selected: {
        "target": selected, "service": helper.TARGET_SERVICES[selected],
        "load_state": "loaded", "active_state": "active",
        "unit_file_state": "enabled", "package_version": None,
    })

    selected = helper._select_service(target)

    assert selected["selected"] == target
    assert property_calls == [
        (helper.TARGET_SERVICES[target], "ActiveState")]


@pytest.mark.parametrize("failure", ["start", "enable"])
def test_select_service_restores_snapshot_on_failure(monkeypatch, failure):
    helper = load_helper()
    services = {
        "wdg": {"load_state": "loaded", "active_state": "active",
                "unit_file_state": "enabled"},
        "stock": {"load_state": "loaded", "active_state": "inactive",
                  "unit_file_state": "disabled"},
    }
    restore = Mock()
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_lock_transaction", nullcontext)
    monkeypatch.setattr(helper, "_service_snapshot", lambda: services)
    monkeypatch.setattr(helper, "_restore_services", restore)
    if failure == "start":
        monkeypatch.setattr(
            helper, "_run",
            Mock(side_effect=helper.HelperError("start failed")))
        monkeypatch.setattr(helper, "_systemd_property", Mock())
        monkeypatch.setattr(helper, "_set_enabled", Mock())
    else:
        monkeypatch.setattr(
            helper, "_run",
            Mock(return_value=NS(returncode=0, stdout="", stderr="")))
        states = iter(("active", "inactive"))
        monkeypatch.setattr(
            helper, "_systemd_property",
            lambda _service, _prop: next(states))
        monkeypatch.setattr(
            helper, "_set_enabled",
            Mock(side_effect=helper.HelperError("enable failed")))

    with pytest.raises(helper.HelperError, match="previous state restored"):
        helper._select_service("stock")
    restore.assert_called_once_with(services)


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


def test_first_install_requires_manual_migration_baseline(
        tmp_path, monkeypatch):
    helper = load_helper()
    backup_root = tmp_path / "backups"
    backup_root.mkdir(mode=0o700)
    monkeypatch.setattr(helper, "BACKUP_ROOT", backup_root)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(helper, "_installed_version", lambda: None)
    dry_run = Mock()
    monkeypatch.setattr(helper, "_candidate_dry_run", dry_run)

    with pytest.raises(
            helper.HelperError,
            match="first stock-to-WDG migration.*manually"):
        helper._create_backup(
            "v2.8.1-wdg.1", NS(package_version="2.8.1+wdg1"), NS(), {})

    dry_run.assert_not_called()
    assert list(backup_root.iterdir()) == []


def test_update_baseline_restores_live_state_changed_by_failing_candidate(
        tmp_path, monkeypatch):
    helper = load_helper()
    live_config = tmp_path / "live-etc-meshtasticd"
    live_state = tmp_path / "live-var-lib-meshtasticd"
    live_config.mkdir()
    live_state.mkdir()
    (live_config / "config.yaml").write_text("General: {}\n", encoding="utf-8")
    (live_state / "identity.bin").write_bytes(b"original identity")
    monkeypatch.setattr(helper, "MESHTASTIC_CONFIG_DIR", live_config)
    monkeypatch.setattr(helper, "MESHTASTIC_STATE_DIR", live_state)
    monkeypatch.setattr(helper, "STATE_PATHS", (live_config, live_state))

    backup_root = tmp_path / "backups"
    backup_root.mkdir(mode=0o700)
    rollback_release = tmp_path / "rollback-release"
    rollback_release.mkdir()
    (rollback_release / "release-marker").write_text("verified", encoding="utf-8")
    monkeypatch.setattr(helper, "BACKUP_ROOT", backup_root)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(helper, "_installed_version", lambda: "2.8.0+wdg1")
    monkeypatch.setattr(
        helper, "_find_cached_release",
        lambda *_args: NS(
            tag="v2.8.0-wdg.1", directory=rollback_release))

    def corrupt_then_fail(_backup):
        (live_config / "config.yaml").write_text(
            "General:\n  MACAddress: FF:FF:FF:FF:FF:FF\n",
            encoding="utf-8")
        (live_state / "identity.bin").write_bytes(b"candidate corruption")
        raise helper.HelperError("candidate health failed")

    monkeypatch.setattr(helper, "_candidate_dry_run", corrupt_then_fail)

    with pytest.raises(helper.HelperError, match="restored exactly.*health failed"):
        helper._create_backup(
            "v2.8.1-wdg.1", NS(package_version="2.8.1+wdg1"), NS(), {})

    assert (live_config / "config.yaml").read_text(encoding="utf-8") == \
        "General: {}\n"
    assert (live_state / "identity.bin").read_bytes() == b"original identity"
    assert list(backup_root.iterdir()) == []


def test_update_baseline_rejects_and_restores_successful_candidate_live_write(
        tmp_path, monkeypatch):
    helper = load_helper()
    (live_config, live_state, _snapshot, _presence,
     before) = candidate_live_state(helper, tmp_path, monkeypatch)
    backup_root = tmp_path / "backups"
    backup_root.mkdir(mode=0o700)
    rollback_release = tmp_path / "rollback-release"
    rollback_release.mkdir()
    (rollback_release / "release-marker").write_text(
        "verified", encoding="utf-8")
    monkeypatch.setattr(helper, "BACKUP_ROOT", backup_root)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(helper, "_installed_version", lambda: "2.8.0+wdg1")
    monkeypatch.setattr(
        helper, "_find_cached_release",
        lambda *_args: NS(
            tag="v2.8.0-wdg.1", directory=rollback_release))
    semantic = helper._semantic_status_snapshot(semantic_status())

    def corrupt_then_report_ready(_backup):
        (live_config / "config.yaml").write_text(
            "General:\n  MACAddress: FF:FF:FF:FF:FF:FF\n",
            encoding="utf-8")
        (live_state / "identity.bin").write_bytes(b"candidate corruption")
        return {
            "semantic": semantic,
            "effective_mac": "02:00:A1:B2:C3:D4",
            "mac_pin_required": False,
        }

    monkeypatch.setattr(helper, "_candidate_dry_run", corrupt_then_report_ready)

    with pytest.raises(
            helper.HelperError,
            match="restored exactly.*modified or obscured live state"):
        helper._create_backup(
            "v2.8.1-wdg.1", NS(package_version="2.8.1+wdg1"), NS(), {})

    assert helper._current_state_fingerprints() == before
    assert (live_state / "identity.bin").read_bytes() == b"original identity"
    assert list(backup_root.iterdir()) == []


def test_update_baseline_retains_backup_when_candidate_restore_fails(
        tmp_path, monkeypatch):
    helper = load_helper()
    live_config = tmp_path / "live-etc-meshtasticd"
    live_state = tmp_path / "live-var-lib-meshtasticd"
    live_config.mkdir()
    live_state.mkdir()
    (live_config / "config.yaml").write_text("General: {}\n", encoding="utf-8")
    (live_state / "identity.bin").write_bytes(b"original identity")
    monkeypatch.setattr(helper, "MESHTASTIC_CONFIG_DIR", live_config)
    monkeypatch.setattr(helper, "MESHTASTIC_STATE_DIR", live_state)
    monkeypatch.setattr(helper, "STATE_PATHS", (live_config, live_state))

    backup_root = tmp_path / "backups"
    backup_root.mkdir(mode=0o700)
    rollback_release = tmp_path / "rollback-release"
    rollback_release.mkdir()
    (rollback_release / "release-marker").write_text("verified", encoding="utf-8")
    monkeypatch.setattr(helper, "BACKUP_ROOT", backup_root)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(helper, "_installed_version", lambda: "2.8.0+wdg1")
    monkeypatch.setattr(
        helper, "_find_cached_release",
        lambda *_args: NS(
            tag="v2.8.0-wdg.1", directory=rollback_release))

    def corrupt_then_fail(_backup):
        (live_state / "identity.bin").write_bytes(b"candidate corruption")
        raise helper.HelperError("candidate health failed")

    monkeypatch.setattr(helper, "_candidate_dry_run", corrupt_then_fail)
    monkeypatch.setattr(
        helper, "_restore_state_from_backup",
        Mock(side_effect=helper.HelperError("restore unavailable")))

    with pytest.raises(
            helper.CandidateStateRestoreError,
            match="restoration failed.*Protected evidence retained") as caught:
        helper._create_backup(
            "v2.8.1-wdg.1", NS(package_version="2.8.1+wdg1"), NS(), {})

    assert caught.value.evidence_path.exists()
    assert caught.value.evidence_path.parent == backup_root
    assert list(backup_root.iterdir()) == [caught.value.evidence_path]


def test_adopt_installed_rejects_package_version_mismatch_before_state_access(
        tmp_path, monkeypatch):
    helper = load_helper()
    tag = "v2.8.1-wdg.1"
    prepared = NS(
        tag=tag, package_version="2.8.1+wdg1",
        directory=tmp_path / tag, package_path=tmp_path / "release.deb",
        manifest={"validated": True})
    quiescent = Mock()
    snapshot = Mock()
    cached = Mock()
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    validator = NS(validate_installed_package_payload=Mock())
    monkeypatch.setattr(helper, "_load_validator", lambda: validator)
    monkeypatch.setattr(
        helper, "_prepare_exact_release", lambda *_args: prepared)
    monkeypatch.setattr(helper, "_lock_transaction", nullcontext)
    monkeypatch.setattr(
        helper, "_installed_version", lambda: "2.8.0+wdg.9")
    monkeypatch.setattr(helper, "_require_quiescent_services", quiescent)
    monkeypatch.setattr(helper, "_snapshot_live_state_for_adoption", snapshot)
    monkeypatch.setattr(helper, "_cache_installed_release", cached)

    with pytest.raises(helper.HelperError, match="does not match"):
        helper._adopt_installed(tag)

    quiescent.assert_not_called()
    snapshot.assert_not_called()
    cached.assert_not_called()


def test_adopt_installed_refuses_a_running_service(monkeypatch):
    helper = load_helper()
    monkeypatch.setattr(helper, "_service_snapshot", lambda: {
        "wdg": {
            "service": helper.TARGET_SERVICES["wdg"],
            "load_state": "loaded", "active_state": "active",
        },
        "stock": {
            "service": helper.TARGET_SERVICES["stock"],
            "load_state": "loaded", "active_state": "inactive",
        },
    })

    with pytest.raises(helper.HelperError, match="Stop both.*still running"):
        helper._require_quiescent_services()


def test_adopt_installed_cleans_snapshot_after_unhealthy_binary(
        tmp_path, monkeypatch):
    helper = load_helper()
    tag = "v2.8.1-wdg.1"
    prepared = NS(
        tag=tag, package_version="2.8.1+wdg1",
        directory=tmp_path / tag, package_path=tmp_path / "release.deb",
        manifest={"validated": True})
    snapshot = tmp_path / "adopt-state-test"
    snapshot.mkdir()
    (snapshot / "private-copy").write_text("copied state", encoding="utf-8")
    cached = Mock()
    validator = NS(validate_installed_package_payload=Mock())
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(helper, "_load_validator", lambda: validator)
    monkeypatch.setattr(
        helper, "_prepare_exact_release", lambda *_args: prepared)
    monkeypatch.setattr(helper, "_lock_transaction", nullcontext)
    monkeypatch.setattr(
        helper, "_installed_version", lambda: prepared.package_version)
    monkeypatch.setattr(helper, "_require_quiescent_services", lambda: None)
    monkeypatch.setattr(
        helper, "_snapshot_live_state_for_adoption",
        lambda: (snapshot, {"live": "unchanged"}))
    monkeypatch.setattr(
        helper, "_candidate_dry_run",
        Mock(side_effect=helper.HelperError("candidate health failed")))
    monkeypatch.setattr(
        helper, "_current_state_fingerprints",
        lambda: {"live": "unchanged"})
    monkeypatch.setattr(helper, "_cache_installed_release", cached)

    with pytest.raises(helper.HelperError, match="health failed"):
        helper._adopt_installed(tag)

    assert not snapshot.exists()
    cached.assert_not_called()


def test_adoption_restores_live_state_changed_by_failing_candidate(
        tmp_path, monkeypatch):
    helper = load_helper()
    (live_config, live_state, snapshot, _presence,
     before) = candidate_live_state(helper, tmp_path, monkeypatch)
    tag = "v2.8.1-wdg.1"
    prepared = NS(
        tag=tag, package_version="2.8.1+wdg1",
        directory=tmp_path / tag, package_path=tmp_path / "release.deb",
        manifest={"validated": True})
    validator = NS(validate_installed_package_payload=Mock())
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(helper, "_load_validator", lambda: validator)
    monkeypatch.setattr(
        helper, "_prepare_exact_release", lambda *_args: prepared)
    monkeypatch.setattr(helper, "_lock_transaction", nullcontext)
    monkeypatch.setattr(
        helper, "_installed_version", lambda: prepared.package_version)
    monkeypatch.setattr(helper, "_require_quiescent_services", lambda: None)
    monkeypatch.setattr(
        helper, "_snapshot_live_state_for_adoption",
        lambda: (snapshot, before))

    def corrupt_then_fail(_snapshot):
        (live_config / "config.yaml").write_text(
            "General:\n  MACAddress: FF:FF:FF:FF:FF:FF\n",
            encoding="utf-8")
        (live_state / "identity.bin").write_bytes(b"candidate corruption")
        raise helper.HelperError("candidate health failed")

    monkeypatch.setattr(helper, "_candidate_dry_run", corrupt_then_fail)
    monkeypatch.setattr(helper, "_cache_installed_release", Mock())

    with pytest.raises(helper.HelperError, match="restored exactly.*health failed"):
        helper._adopt_installed(tag)

    assert helper._current_state_fingerprints() == before
    assert (live_state / "identity.bin").read_bytes() == b"original identity"
    assert not snapshot.exists()


def test_adoption_rejects_and_restores_successful_candidate_live_write(
        tmp_path, monkeypatch):
    helper = load_helper()
    (live_config, live_state, snapshot, _presence,
     before) = candidate_live_state(helper, tmp_path, monkeypatch)
    tag = "v2.8.1-wdg.1"
    prepared = NS(
        tag=tag, package_version="2.8.1+wdg1",
        directory=tmp_path / tag, package_path=tmp_path / "release.deb",
        manifest={"validated": True})
    validator = NS(validate_installed_package_payload=Mock())
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(helper, "_load_validator", lambda: validator)
    monkeypatch.setattr(
        helper, "_prepare_exact_release", lambda *_args: prepared)
    monkeypatch.setattr(helper, "_lock_transaction", nullcontext)
    monkeypatch.setattr(
        helper, "_installed_version", lambda: prepared.package_version)
    monkeypatch.setattr(helper, "_require_quiescent_services", lambda: None)
    monkeypatch.setattr(
        helper, "_snapshot_live_state_for_adoption",
        lambda: (snapshot, before))
    semantic = helper._semantic_status_snapshot(semantic_status())

    def corrupt_then_report_ready(_snapshot):
        (live_config / "config.yaml").write_text(
            "General:\n  MACAddress: FF:FF:FF:FF:FF:FF\n",
            encoding="utf-8")
        (live_state / "identity.bin").write_bytes(b"candidate corruption")
        return {
            "semantic": semantic,
            "effective_mac": "02:00:A1:B2:C3:D4",
            "mac_pin_required": False,
        }

    monkeypatch.setattr(helper, "_candidate_dry_run", corrupt_then_report_ready)
    monkeypatch.setattr(helper, "_cache_installed_release", Mock())

    with pytest.raises(
            helper.HelperError,
            match="restored exactly.*modified or obscured live state"):
        helper._adopt_installed(tag)

    assert helper._current_state_fingerprints() == before
    assert (live_state / "identity.bin").read_bytes() == b"original identity"
    assert not snapshot.exists()


def test_adoption_retains_snapshot_when_candidate_state_restore_fails(
        tmp_path, monkeypatch):
    helper = load_helper()
    (_live_config, live_state, snapshot, _presence,
     before) = candidate_live_state(helper, tmp_path, monkeypatch)
    tag = "v2.8.1-wdg.1"
    prepared = NS(
        tag=tag, package_version="2.8.1+wdg1",
        directory=tmp_path / tag, package_path=tmp_path / "release.deb",
        manifest={"validated": True})
    validator = NS(validate_installed_package_payload=Mock())
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(helper, "_load_validator", lambda: validator)
    monkeypatch.setattr(
        helper, "_prepare_exact_release", lambda *_args: prepared)
    monkeypatch.setattr(helper, "_lock_transaction", nullcontext)
    monkeypatch.setattr(
        helper, "_installed_version", lambda: prepared.package_version)
    monkeypatch.setattr(helper, "_require_quiescent_services", lambda: None)
    monkeypatch.setattr(
        helper, "_snapshot_live_state_for_adoption",
        lambda: (snapshot, before))

    def corrupt_then_fail(_snapshot):
        (live_state / "identity.bin").write_bytes(b"candidate corruption")
        raise helper.HelperError("candidate health failed")

    monkeypatch.setattr(helper, "_candidate_dry_run", corrupt_then_fail)
    monkeypatch.setattr(
        helper, "_restore_state_from_backup",
        Mock(side_effect=helper.HelperError("restore unavailable")))
    monkeypatch.setattr(helper, "_cache_installed_release", Mock())

    with pytest.raises(
            helper.CandidateStateRestoreError,
            match="restoration failed.*Protected evidence retained") as caught:
        helper._adopt_installed(tag)

    assert caught.value.evidence_path == snapshot
    assert snapshot.exists()


def test_adopt_installed_validates_copy_then_seeds_only_rollback_cache(
        tmp_path, monkeypatch):
    helper = load_helper()
    tag = "v2.8.1-wdg.1"
    prepared = NS(
        tag=tag, package_version="2.8.1+wdg1",
        directory=tmp_path / tag, package_path=tmp_path / "release.deb",
        manifest={"validated": True})
    snapshot = tmp_path / "adopt-state-test"
    snapshot.mkdir()
    live_fingerprints = {"/etc/meshtasticd": [{"sha256": "same"}]}
    semantic = helper._semantic_status_snapshot(semantic_status())
    baseline = {
        "semantic": semantic,
        "effective_mac": "02:00:A1:B2:C3:D4",
        "mac_pin_required": False,
    }
    quiescent = Mock()
    candidate = Mock(return_value=baseline)
    cached = Mock()
    forbidden_run = Mock(side_effect=AssertionError(
        "adoption must not execute package or service commands"))
    validator = NS(validate_installed_package_payload=Mock())
    prepare = Mock(return_value=prepared)
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(helper, "_load_validator", lambda: validator)
    monkeypatch.setattr(helper, "_prepare_exact_release", prepare)
    monkeypatch.setattr(helper, "_lock_transaction", nullcontext)
    monkeypatch.setattr(
        helper, "_installed_version", lambda: prepared.package_version)
    monkeypatch.setattr(helper, "_require_quiescent_services", quiescent)
    monkeypatch.setattr(
        helper, "_snapshot_live_state_for_adoption",
        lambda: (snapshot, live_fingerprints))
    monkeypatch.setattr(
        helper, "_current_state_fingerprints", lambda: live_fingerprints)
    monkeypatch.setattr(helper, "_candidate_dry_run", candidate)
    monkeypatch.setattr(helper, "_cache_installed_release", cached)
    monkeypatch.setattr(helper, "_run", forbidden_run)

    reply = helper._adopt_installed(tag)

    assert reply == {
        "action": "adopt-installed",
        "tag": tag,
        "package_version": prepared.package_version,
        "health": "ready",
    }
    prepare.assert_called_once_with(tag, validator)
    validator.validate_installed_package_payload.assert_called_once_with(
        prepared.package_path, prepared.manifest)
    candidate.assert_called_once_with(snapshot)
    cached.assert_called_once_with(prepared)
    assert quiescent.call_count == 2
    forbidden_run.assert_not_called()
    assert not snapshot.exists()


def test_adopt_installed_rejects_payload_mismatch_before_execution_or_cache(
        tmp_path, monkeypatch):
    helper = load_helper()
    tag = "v2.8.1-wdg.1"
    prepared = NS(
        tag=tag, package_version="2.8.1+wdg1",
        directory=tmp_path / tag, package_path=tmp_path / "release.deb",
        manifest={"validated": True})
    validator = NS(validate_installed_package_payload=Mock(
        side_effect=ValueError("installed payload differs")))
    snapshot = Mock()
    candidate = Mock()
    cached = Mock()
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(helper, "_load_validator", lambda: validator)
    monkeypatch.setattr(
        helper, "_prepare_exact_release", lambda *_args: prepared)
    monkeypatch.setattr(helper, "_lock_transaction", nullcontext)
    monkeypatch.setattr(
        helper, "_installed_version", lambda: prepared.package_version)
    monkeypatch.setattr(helper, "_require_quiescent_services", lambda: None)
    monkeypatch.setattr(helper, "_snapshot_live_state_for_adoption", snapshot)
    monkeypatch.setattr(helper, "_candidate_dry_run", candidate)
    monkeypatch.setattr(helper, "_cache_installed_release", cached)

    with pytest.raises(ValueError, match="payload differs"):
        helper._adopt_installed(tag)

    validator.validate_installed_package_payload.assert_called_once_with(
        prepared.package_path, prepared.manifest)
    snapshot.assert_not_called()
    candidate.assert_not_called()
    cached.assert_not_called()


def test_adopt_installed_persists_verified_missing_mac_before_caching(
        tmp_path, monkeypatch):
    helper = load_helper()
    tag = "v2.8.1-wdg.1"
    prepared = NS(
        tag=tag, package_version="2.8.1+wdg1",
        directory=tmp_path / tag, package_path=tmp_path / "release.deb",
        manifest={"validated": True})
    snapshot = tmp_path / "adopt-state-test"
    snapshot.mkdir()
    semantic = helper._semantic_status_snapshot(semantic_status())
    before = {
        str(helper.MESHTASTIC_CONFIG_DIR): [{
            "path": "config.yaml", "type": "file", "mode": 0o640,
            "uid": 0, "gid": 900, "sha256": "old",
        }],
        str(helper.MESHTASTIC_STATE_DIR): [],
    }
    after = {
        str(helper.MESHTASTIC_CONFIG_DIR): [{
            "path": "config.yaml", "type": "file", "mode": 0o640,
            "uid": 0, "gid": 900, "sha256": "new",
        }],
        str(helper.MESHTASTIC_STATE_DIR): [],
    }
    validator = NS(validate_installed_package_payload=Mock())
    pin = Mock()
    cache = Mock()
    candidate = Mock(side_effect=[
        {
            "semantic": semantic,
            "effective_mac": "02:00:A1:B2:C3:D4",
            "mac_pin_required": True,
        },
        {
            "semantic": semantic,
            "effective_mac": "02:00:A1:B2:C3:D4",
            "mac_pin_required": False,
        },
    ])
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(helper, "_load_validator", lambda: validator)
    monkeypatch.setattr(
        helper, "_prepare_exact_release", lambda *_args: prepared)
    monkeypatch.setattr(helper, "_lock_transaction", nullcontext)
    monkeypatch.setattr(
        helper, "_installed_version", lambda: prepared.package_version)
    monkeypatch.setattr(helper, "_require_quiescent_services", lambda: None)
    monkeypatch.setattr(
        helper, "_snapshot_live_state_for_adoption", lambda: (snapshot, before))
    monkeypatch.setattr(
        helper, "_adoption_config_rollback_copy",
        lambda _snapshot: (
            b"General:\n  MACAddressSource: eth0\n", 0, 900, 0o640))
    monkeypatch.setattr(helper, "_live_config_matches", lambda _copy: True)
    monkeypatch.setattr(
        helper, "_current_state_fingerprints",
        Mock(side_effect=[before, before, before, after, after]))
    monkeypatch.setattr(helper, "_candidate_dry_run", candidate)
    monkeypatch.setattr(helper, "_pin_live_mac", pin)
    monkeypatch.setattr(helper, "_cache_installed_release", cache)

    helper._adopt_installed(tag)

    assert candidate.call_args_list == [
        call(snapshot),
        call(snapshot, pinned_mac="02:00:A1:B2:C3:D4"),
    ]
    pin.assert_called_once_with("02:00:A1:B2:C3:D4")
    cache.assert_called_once_with(prepared)
    assert not snapshot.exists()


def test_failed_adoption_rolls_back_verified_mac_pin_before_returning(
        tmp_path, monkeypatch):
    helper = load_helper()
    tag = "v2.8.1-wdg.1"
    prepared = NS(
        tag=tag, package_version="2.8.1+wdg1",
        directory=tmp_path / tag, package_path=tmp_path / "release.deb",
        manifest={"validated": True})
    snapshot = tmp_path / "adopt-state-test"
    snapshot.mkdir()
    semantic = helper._semantic_status_snapshot(semantic_status())
    config_key = str(helper.MESHTASTIC_CONFIG_DIR)
    state_key = str(helper.MESHTASTIC_STATE_DIR)
    before = {
        config_key: [{
            "path": "config.yaml", "type": "file", "mode": 0o640,
            "uid": 0, "gid": 900, "sha256": "old",
        }],
        state_key: [],
    }
    after = {
        config_key: [{
            "path": "config.yaml", "type": "file", "mode": 0o640,
            "uid": 0, "gid": 900, "sha256": "new",
        }],
        state_key: [],
    }
    rollback_copy = (
        b"General:\n  MACAddressSource: eth0\n", 0, 900, 0o640)
    validator = NS(validate_installed_package_payload=Mock())
    restore = Mock()
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(helper, "_load_validator", lambda: validator)
    monkeypatch.setattr(
        helper, "_prepare_exact_release", lambda *_args: prepared)
    monkeypatch.setattr(helper, "_lock_transaction", nullcontext)
    monkeypatch.setattr(
        helper, "_installed_version", lambda: prepared.package_version)
    monkeypatch.setattr(helper, "_require_quiescent_services", lambda: None)
    monkeypatch.setattr(
        helper, "_snapshot_live_state_for_adoption", lambda: (snapshot, before))
    monkeypatch.setattr(
        helper, "_adoption_config_rollback_copy", lambda _snapshot: rollback_copy)
    monkeypatch.setattr(
        helper, "_candidate_dry_run",
        Mock(side_effect=[
            {
                "semantic": semantic,
                "effective_mac": "02:00:A1:B2:C3:D4",
                "mac_pin_required": True,
            },
            {
                "semantic": semantic,
                "effective_mac": "02:00:A1:B2:C3:D4",
                "mac_pin_required": False,
            },
        ]))
    monkeypatch.setattr(helper, "_pin_live_mac", Mock())
    monkeypatch.setattr(helper, "_live_config_matches", lambda _copy: True)
    monkeypatch.setattr(helper, "_restore_adoption_config", restore)
    monkeypatch.setattr(
        helper, "_current_state_fingerprints",
        Mock(side_effect=[
            before, before, before, after, after, after, before]))
    monkeypatch.setattr(
        helper, "_cache_installed_release",
        Mock(side_effect=helper.HelperError("cache failed")))

    with pytest.raises(helper.HelperError, match="cache failed"):
        helper._adopt_installed(tag)

    restore.assert_called_once_with(rollback_copy)
    assert not snapshot.exists()


def test_adoption_rolls_back_when_pin_replacement_raises_after_write(
        tmp_path, monkeypatch):
    helper = load_helper()
    tag = "v2.8.1-wdg.1"
    prepared = NS(
        tag=tag, package_version="2.8.1+wdg1",
        directory=tmp_path / tag, package_path=tmp_path / "release.deb",
        manifest={"validated": True})
    snapshot = tmp_path / "adopt-state-test"
    snapshot.mkdir()
    semantic = helper._semantic_status_snapshot(semantic_status())
    config_key = str(helper.MESHTASTIC_CONFIG_DIR)
    state_key = str(helper.MESHTASTIC_STATE_DIR)
    before = {
        config_key: [{
            "path": "config.yaml", "type": "file", "mode": 0o640,
            "uid": 0, "gid": 900, "sha256": "old",
        }],
        state_key: [],
    }
    after = {
        config_key: [{
            "path": "config.yaml", "type": "file", "mode": 0o640,
            "uid": 0, "gid": 900, "sha256": "new",
        }],
        state_key: [],
    }
    rollback_copy = (
        b"General:\n  MACAddressSource: eth0\n", 0, 900, 0o640)
    restore = Mock()
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(
        helper, "_load_validator",
        lambda: NS(validate_installed_package_payload=Mock()))
    monkeypatch.setattr(
        helper, "_prepare_exact_release", lambda *_args: prepared)
    monkeypatch.setattr(helper, "_lock_transaction", nullcontext)
    monkeypatch.setattr(
        helper, "_installed_version", lambda: prepared.package_version)
    monkeypatch.setattr(helper, "_require_quiescent_services", lambda: None)
    monkeypatch.setattr(
        helper, "_snapshot_live_state_for_adoption", lambda: (snapshot, before))
    monkeypatch.setattr(
        helper, "_adoption_config_rollback_copy", lambda _snapshot: rollback_copy)
    monkeypatch.setattr(
        helper, "_candidate_dry_run",
        Mock(side_effect=[
            {
                "semantic": semantic,
                "effective_mac": "02:00:A1:B2:C3:D4",
                "mac_pin_required": True,
            },
            {
                "semantic": semantic,
                "effective_mac": "02:00:A1:B2:C3:D4",
                "mac_pin_required": False,
            },
        ]))
    monkeypatch.setattr(
        helper, "_pin_live_mac",
        Mock(side_effect=helper.HelperError("directory fsync failed")))
    monkeypatch.setattr(helper, "_live_config_matches", lambda _copy: True)
    monkeypatch.setattr(helper, "_restore_adoption_config", restore)
    monkeypatch.setattr(
        helper, "_current_state_fingerprints",
        Mock(side_effect=[before, before, before, after, before]))
    cache = Mock()
    monkeypatch.setattr(helper, "_cache_installed_release", cache)

    with pytest.raises(helper.HelperError, match="directory fsync failed"):
        helper._adopt_installed(tag)

    restore.assert_called_once_with(rollback_copy)
    cache.assert_not_called()
    assert not snapshot.exists()


def test_existing_rollback_cache_must_match_the_verified_release(
        tmp_path, monkeypatch):
    helper = load_helper()
    installed_cache = tmp_path / "installed"
    target = installed_cache / "v2.8.1-wdg.1"
    target.mkdir(parents=True)
    prepared = NS(
        tag=target.name, package_version="2.8.1+wdg1",
        manifest={"source_commit": "a" * 40},
        directory=tmp_path / "prepared")
    cached = NS(
        package_version=prepared.package_version,
        manifest={"source_commit": "b" * 40})
    monkeypatch.setattr(helper, "INSTALLED_CACHE", installed_cache)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(
        helper, "_load_validator",
        lambda: NS(validate_prepared_release=Mock(return_value=cached)))

    with pytest.raises(helper.HelperError, match="differs from the verified"):
        helper._cache_installed_release(prepared)


def test_backup_persists_private_sanitized_preinstall_baseline(
        tmp_path, monkeypatch):
    helper = load_helper()
    backup_root = tmp_path / "backups"
    backup_root.mkdir(mode=0o700)
    cached = tmp_path / "v2.8.0-wdg.1"
    cached.mkdir()
    (cached / "old.deb").write_bytes(b"old")
    rollback = fake_prepared_release(cached.name, cached)
    prepared_dir = tmp_path / "v2.8.1-wdg.1"
    prepared_dir.mkdir()
    prepared = fake_prepared_release(prepared_dir.name, prepared_dir)
    raw = semantic_status()
    raw["identity"]["private_key"] = "PRIVATE-KEY-BYTES"
    raw["channels"][0]["psk"] = "SECRET-PSK-BYTES"
    semantic = helper._semantic_status_snapshot(raw)
    baseline = {
        "semantic": semantic,
        "effective_mac": "02:00:A1:B2:C3:D4",
        "mac_pin_required": True,
    }
    monkeypatch.setattr(helper, "BACKUP_ROOT", backup_root)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(helper, "_installed_version", lambda: "2.8.0+wdg1")
    monkeypatch.setattr(helper, "_find_cached_release", lambda *a: rollback)
    monkeypatch.setattr(helper, "_tree_fingerprint", lambda _path: [])
    monkeypatch.setattr(helper, "_copy_state_to_backup", lambda _backup: {})
    monkeypatch.setattr(helper, "_candidate_dry_run", Mock(return_value=baseline))
    monkeypatch.setattr(helper.os, "fchown", lambda *a: None)

    backup, metadata = helper._create_backup(
        "v2.8.1-wdg.1", prepared, NS(), {})

    transaction = backup / "transaction.json"
    serialized = transaction.read_text(encoding="utf-8")
    persisted = json.loads(serialized)
    assert metadata == persisted
    assert persisted["semantic_baseline"] == semantic
    assert persisted["effective_mac"] == "02:00:A1:B2:C3:D4"
    assert persisted["mac_pin_required"] is True
    assert persisted["format"] == 2
    assert persisted["new_release"]["package_sha256"] == "a" * 64
    assert persisted["rollback_release"]["tag"] == "v2.8.0-wdg.1"
    assert "PRIVATE-KEY-BYTES" not in serialized
    assert "SECRET-PSK-BYTES" not in serialized
    assert "private_key" not in persisted["semantic_baseline"]
    assert "psk" not in persisted["semantic_baseline"]["channels"][0]
    assert stat.S_IMODE(transaction.stat().st_mode) == 0o600


def test_install_failure_runs_automatic_rollback(tmp_path, monkeypatch):
    helper = load_helper()
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    stage = cache / "v2.8.1-wdg.1"
    stage.mkdir(mode=0o700)
    package = stage / "meshtasticd-wdg_2.8.1+wdg1_arm64.deb"
    package.write_bytes(b"deb")
    package.chmod(0o600)
    prepared = fake_prepared_release(stage.name, stage)
    validator = NS(
        validate_prepared_release=lambda *a, **k: prepared,
        validate_installed_package_payload=Mock())
    backup = tmp_path / "backup"
    backup.mkdir(mode=0o700)
    metadata = baseline_metadata(helper)
    metadata["new_release"] = helper._release_identity(prepared)
    rolled_back = []
    monkeypatch.setattr(helper, "CACHE_ROOT", cache)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda path: None)
    monkeypatch.setattr(helper, "_load_validator", lambda: validator)
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_lock_transaction", lambda: _Context())
    monkeypatch.setattr(helper, "_create_backup", lambda *a: (backup, metadata))
    monkeypatch.setattr(helper, "_read_private_json", lambda _path: metadata)
    monkeypatch.setattr(
        helper, "_validate_transaction_metadata",
        lambda value, *_args: (helper._validation_baseline(value), {}, {}))
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


def test_malformed_persisted_baseline_aborts_before_apt(
        tmp_path, monkeypatch):
    helper = load_helper()
    cache = tmp_path / "cache"
    stage = cache / "v2.8.1-wdg.1"
    stage.mkdir(parents=True, mode=0o700)
    package = stage / "meshtasticd-wdg_2.8.1+wdg1_arm64.deb"
    package.write_bytes(b"deb")
    prepared = fake_prepared_release(stage.name, stage)
    validator = NS(
        validate_prepared_release=lambda *a, **k: prepared,
        validate_installed_package_payload=Mock())
    backup = tmp_path / "backup"
    backup.mkdir()
    metadata = baseline_metadata(helper)
    metadata["semantic_baseline"]["node_id"] = "not-a-node"
    calls = []
    transaction_restore = Mock()
    services_restore = Mock()
    monkeypatch.setattr(helper, "CACHE_ROOT", cache)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(helper, "_load_validator", lambda: validator)
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_lock_transaction", lambda: _Context())
    monkeypatch.setattr(helper, "_service_snapshot", lambda: {
        "wdg": {"load_state": "loaded"},
        "stock": {"load_state": "loaded"},
    })
    monkeypatch.setattr(
        helper, "_create_backup", lambda *a: (backup, metadata))
    monkeypatch.setattr(helper, "_read_private_json", lambda _path: metadata)
    monkeypatch.setattr(
        helper, "_validate_transaction_metadata",
        lambda value, *_args: (helper._validation_baseline(value), {}, {}))
    monkeypatch.setattr(
        helper, "_run",
        lambda command, **_kwargs: (
            calls.append(command) or NS(returncode=0, stdout="", stderr="")))
    monkeypatch.setattr(
        helper, "_restore_transaction",
        transaction_restore)
    monkeypatch.setattr(helper, "_restore_services", services_restore)

    with pytest.raises(
            helper.HelperError,
            match="aborted before package changes.*malformed"):
        helper._install_tag(prepared.tag)

    transaction_restore.assert_not_called()
    services_restore.assert_called_once()
    assert not any(command[0] == "apt-get" for command in calls)


def restore_transaction_fixture(tmp_path, monkeypatch, *, with_package=True):
    helper = load_helper()
    live_config = tmp_path / "etc" / "meshtasticd"
    live_state = tmp_path / "var" / "lib" / "meshtasticd"
    live_config.mkdir(parents=True)
    live_state.mkdir(parents=True)
    (live_config / "config.yaml").write_text("candidate", encoding="utf-8")
    (live_state / "identity.bin").write_bytes(b"candidate")
    monkeypatch.setattr(helper, "STATE_PATHS", (live_config, live_state))

    backup = tmp_path / "backup"
    state_root = backup / "state"
    state_root.mkdir(parents=True)
    backup.chmod(0o700)
    for target, payload in (
            (live_config, b"verified config"),
            (live_state, b"verified identity")):
        source = state_root / helper._state_backup_key(target)
        source.mkdir()
        (source / "value.bin").write_bytes(payload)

    new_dir = tmp_path / "new" / "v2.8.1-wdg.1"
    new_dir.mkdir(parents=True)
    new_release = fake_prepared_release(new_dir.name, new_dir, digest="c" * 64)
    previous_version = "2.8.0+wdg1" if with_package else None
    rollback_release = None
    if with_package:
        rollback_dir = backup / "rollback-release" / "v2.8.0-wdg.1"
        rollback_dir.mkdir(parents=True)
        rollback_release = fake_prepared_release(
            rollback_dir.name, rollback_dir, digest="d" * 64)
    services = {
        "wdg": {
            "load_state": "loaded", "active_state": "inactive",
            "unit_file_state": "enabled",
        },
        "stock": {
            "load_state": "loaded", "active_state": "active",
            "unit_file_state": "disabled",
        },
    }
    presence = {
        helper._state_backup_key(path): True for path in helper.STATE_PATHS}
    fingerprints = {
        str(path): helper._tree_fingerprint(
            state_root / helper._state_backup_key(path))
        for path in helper.STATE_PATHS
    }
    metadata = {
        "format": 2,
        "new_release": helper._release_identity(new_release),
        "previous_version": previous_version,
        "rollback_release": (
            helper._release_identity(rollback_release)
            if rollback_release is not None else None),
        "services": services,
        "state_presence": presence,
        "state_fingerprints": fingerprints,
        "semantic_baseline": helper._semantic_status_snapshot(semantic_status()),
        "effective_mac": "02:00:A1:B2:C3:D4",
        "mac_pin_required": False,
    }
    validator = NS(
        validate_prepared_release=Mock(return_value=rollback_release),
        validate_installed_package_payload=Mock())
    commands = []

    def run(command, **kwargs):
        if command[0] == "cp":
            shutil.copytree(command[-2], command[-1], copy_function=shutil.copy2)
        else:
            commands.append((command, kwargs))
        return NS(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(helper, "_run", run)
    monkeypatch.setattr(helper, "_installed_version", lambda: previous_version)
    monkeypatch.setattr(helper, "_restore_services", Mock())
    return (helper, backup, metadata, validator, commands,
            live_config, live_state, rollback_release)


@pytest.mark.parametrize("with_package", [False, True])
def test_restore_transaction_verifies_package_state_and_files(
        tmp_path, monkeypatch, with_package):
    (helper, backup, metadata, validator, calls,
     live_config, live_state, rollback_release) = restore_transaction_fixture(
         tmp_path, monkeypatch, with_package=with_package)

    helper._restore_transaction(backup, metadata, validator)

    assert (live_config / "value.bin").read_bytes() == b"verified config"
    assert (live_state / "value.bin").read_bytes() == b"verified identity"
    helper._restore_services.assert_called_once_with(metadata["services"])
    if not with_package:
        assert any(command[:2] == ["dpkg", "--purge"]
                   for command, _kwargs in calls)
        validator.validate_installed_package_payload.assert_not_called()
    else:
        apt_commands = [
            command for command, _kwargs in calls if command[0] == "apt-get"]
        assert apt_commands == [[
            "apt-get", "-y", "--allow-downgrades",
            "--no-install-recommends", "install",
            str(rollback_release.package_path),
        ]]
        validator.validate_installed_package_payload.assert_called_once_with(
            rollback_release.package_path, rollback_release.manifest)


def test_restore_preflight_rejects_corrupt_backup_before_live_mutation(
        tmp_path, monkeypatch):
    (helper, backup, metadata, validator, calls,
     live_config, _live_state, _release) = restore_transaction_fixture(
         tmp_path, monkeypatch)
    before = (live_config / "config.yaml").read_bytes()
    source = backup / "state" / helper._state_backup_key(live_config) / "value.bin"
    source.write_bytes(b"tampered")

    with pytest.raises(helper.HelperError, match="fingerprint"):
        helper._restore_transaction(backup, metadata, validator)

    assert (live_config / "config.yaml").read_bytes() == before
    assert calls == []
    helper._restore_services.assert_not_called()


def test_restore_preflight_rejects_missing_backup_before_live_mutation(
        tmp_path, monkeypatch):
    (helper, backup, metadata, validator, calls,
     live_config, live_state, _release) = restore_transaction_fixture(
         tmp_path, monkeypatch)
    before_config = helper._tree_fingerprint(live_config)
    before_state = helper._tree_fingerprint(live_state)
    shutil.rmtree(
        backup / "state" / helper._state_backup_key(live_state))

    with pytest.raises(helper.HelperError, match="contents do not match"):
        helper._restore_transaction(backup, metadata, validator)

    assert helper._tree_fingerprint(live_config) == before_config
    assert helper._tree_fingerprint(live_state) == before_state
    assert calls == []
    helper._restore_services.assert_not_called()


def test_restore_preflight_rejects_release_digest_mismatch_before_mutation(
        tmp_path, monkeypatch):
    (helper, backup, metadata, validator, calls,
     live_config, live_state, _release) = restore_transaction_fixture(
         tmp_path, monkeypatch)
    before_config = helper._tree_fingerprint(live_config)
    before_state = helper._tree_fingerprint(live_state)
    metadata["rollback_release"]["manifest_sha256"] = "e" * 64

    with pytest.raises(
            helper.HelperError,
            match="differs from transaction metadata"):
        helper._restore_transaction(backup, metadata, validator)

    assert helper._tree_fingerprint(live_config) == before_config
    assert helper._tree_fingerprint(live_state) == before_state
    assert calls == []
    helper._restore_services.assert_not_called()


def test_state_swap_recovers_earlier_target_when_later_swap_fails(
        tmp_path, monkeypatch):
    (helper, backup, metadata, _validator, _calls,
     live_config, live_state, _release) = restore_transaction_fixture(
         tmp_path, monkeypatch, with_package=False)
    entries = helper._prepare_state_restore(
        backup, metadata["state_presence"], metadata["state_fingerprints"])
    original_rename = helper.os.rename
    failed = False

    def fail_second_staging(source, target):
        nonlocal failed
        if (not failed and Path(target) == live_state
                and Path(source) == entries[1].staging):
            failed = True
            raise OSError("simulated second swap failure")
        return original_rename(source, target)

    monkeypatch.setattr(helper.os, "rename", fail_second_staging)
    with pytest.raises(OSError, match="second swap"):
        helper._apply_state_restore(entries)

    assert (live_config / "config.yaml").read_text() == "candidate"
    assert (live_state / "identity.bin").read_bytes() == b"candidate"
    helper._cleanup_restore_entries(entries)


def test_candidate_restore_rejects_changed_backup_before_live_mutation(
        tmp_path, monkeypatch):
    helper = load_helper()
    (live_config, live_state, snapshot, presence,
     before) = candidate_live_state(helper, tmp_path, monkeypatch)
    backup_file = (
        snapshot / "state" / helper._state_backup_key(live_config)
        / "config.yaml")
    backup_file.write_text("tampered backup\n", encoding="utf-8")
    live_config_fingerprint = helper._tree_fingerprint(live_config)
    live_state_fingerprint = helper._tree_fingerprint(live_state)

    with pytest.raises(helper.HelperError, match="fingerprint"):
        helper._restore_state_from_backup(
            snapshot, presence, before)

    assert helper._tree_fingerprint(live_config) == live_config_fingerprint
    assert helper._tree_fingerprint(live_state) == live_state_fingerprint


def test_install_tag_downloads_only_the_matching_release_into_root_cache(
        tmp_path, monkeypatch):
    helper = load_helper()
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    requested = "v2.8.1-wdg.1"
    release = {"tag_name": requested}
    prepared = NS(
        tag=requested, package_version="2.8.1+wdg1",
        package_path=cache / "unused.deb", directory=cache / requested)
    calls = []
    validator = NS(
        meshtastic_releases=lambda: [
            {"tag_name": "v2.8.0-wdg.9"}, release],
        prepare_meshtastic_release=lambda **kwargs: (
            calls.append(kwargs) or prepared),
    )
    monkeypatch.setattr(helper, "CACHE_ROOT", cache)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda path: None)
    monkeypatch.setattr(helper, "_load_validator", lambda: validator)
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(
        helper, "_lock_transaction",
        Mock(side_effect=RuntimeError("stop after preparation")))

    with pytest.raises(RuntimeError, match="stop after preparation"):
        helper._install_tag(requested)

    assert calls == [{
        "cache_root": cache, "release": release, "require_root": True,
        "check_host": True,
    }]


def test_prepare_first_tag_seals_root_inbox_before_returning_package(
        tmp_path, monkeypatch):
    helper = load_helper()
    tag = "v2.8.1-wdg.1"
    cache = tmp_path / "cache"
    inbox_root = cache / "first-install-inbox"
    inbox = inbox_root / tag
    inbox.mkdir(parents=True, mode=0o700)
    for name in (
            "compatibility.json", "SHA256SUMS", "SOURCE.txt", "copyright",
            "meshtasticd-wdg_2.8.1+wdg1_arm64.deb"):
        (inbox / name).write_bytes(b"abc")
        (inbox / name).chmod(0o600)

    validations = []

    def validate(directory, **kwargs):
        validations.append((Path(directory), kwargs))
        return fake_prepared_release(tag, directory)

    validator = NS(validate_prepared_release=validate)
    monkeypatch.setattr(helper, "CACHE_ROOT", cache)
    monkeypatch.setattr(helper, "FIRST_INSTALL_INBOX", inbox_root)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(helper, "_load_validator", lambda: validator)
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_installed_version", lambda: None)
    monkeypatch.setattr(helper, "_lock_transaction", nullcontext)

    outcome = helper._prepare_first_tag(tag)

    protected = cache / tag
    package = protected / "meshtasticd-wdg_2.8.1+wdg1_arm64.deb"
    assert package.read_bytes() == b"abc"
    assert outcome == {
        "action": "prepare-first-tag",
        "tag": tag,
        "package_version": "2.8.1+wdg1",
        "package_path": str(package),
    }
    assert validations[0][0] == inbox
    assert validations[-1][0] == protected
    assert all(call_kwargs == {
        "expected_tag": tag,
        "require_secure": True,
        "check_host": True,
    } for _path, call_kwargs in validations)


def semantic_status(*, node_id="!a1b2c3d4", long_name="uConsole",
                    short_name="UC", public=True, private=True,
                    channels=None):
    if channels is None:
        channels = [{
            "index": 0, "name": "LongFast", "role": 1, "has_psk": True,
        }]
    return {
        "state": "ready", "radio_status": "ready",
        "identity": {
            "node_id": node_id, "name": long_name,
            "short_name": short_name, "has_public_key": public,
            "has_private_key": private,
        },
        "channels": channels,
    }


def fake_prepared_release(tag, directory, *, digest="a" * 64):
    version = tag.removeprefix("v").replace("-wdg.", "+wdg")
    asset = f"meshtasticd-wdg_{version}_arm64.deb"
    manifest = {
        "source_commit": "b" * 40,
        "package": {
            "asset": asset,
            "version": version,
            "size": 3,
            "sha256": digest,
        },
    }
    return NS(
        tag=tag,
        package_version=version,
        package_path=Path(directory) / asset,
        directory=Path(directory),
        manifest=manifest,
    )


def baseline_metadata(helper, *, pin_required=False, status=None):
    return {
        "format": 1,
        "semantic_baseline": helper._semantic_status_snapshot(
            status or semantic_status()),
        "effective_mac": "02:00:A1:B2:C3:D4",
        "mac_pin_required": pin_required,
    }


def candidate_backup(tmp_path):
    backup = tmp_path / "20260924T120000Z-test"
    state = backup / "state"
    config = state / "etc-meshtasticd"
    fsdir = state / "var-lib-meshtasticd"
    (fsdir / "prefs").mkdir(parents=True)
    (fsdir / "backups").mkdir()
    config.mkdir(parents=True)
    (config / "config.yaml").write_text("Lora:\n  Module: sx1262\n")
    (config / "wdg-portduino.yaml").write_text("wdg_api:\n  enabled: true\n")
    for relative in (
            "prefs/device.proto", "prefs/config.proto",
            "prefs/channels.proto", "backups/backup.proto"):
        path = fsdir / relative
        path.write_bytes(relative.encode())
    backup.chmod(0o700)
    return backup


def nested_candidate_backup(tmp_path):
    backup = candidate_backup(tmp_path)
    state_root = backup / "state/var-lib-meshtasticd"
    nested = state_root / ".portduino/default"
    nested.mkdir(parents=True)
    (state_root / "prefs").rename(nested / "prefs")
    (state_root / "backups").rename(nested / "backups")
    return backup


def test_state_fsdir_prefers_stock_portduino_layout(tmp_path):
    helper = load_helper()
    state_root = tmp_path / "var-lib-meshtasticd"
    flat = state_root / "prefs"
    nested = state_root / ".portduino/default"
    flat.mkdir(parents=True)
    nested.mkdir(parents=True)

    assert helper._resolve_state_fsdir(state_root) == nested


def test_state_fsdir_accepts_legacy_flat_fixture(tmp_path):
    helper = load_helper()
    state_root = tmp_path / "var-lib-meshtasticd"
    (state_root / "prefs").mkdir(parents=True)

    assert helper._resolve_state_fsdir(state_root) == state_root


def test_state_fsdir_rejects_nested_symlink(tmp_path):
    helper = load_helper()
    state_root = tmp_path / "var-lib-meshtasticd"
    target = tmp_path / "outside"
    target.mkdir()
    (state_root / ".portduino").mkdir(parents=True)
    (state_root / ".portduino/default").symlink_to(target)

    with pytest.raises(helper.HelperError, match="unsafe"):
        helper._resolve_state_fsdir(state_root)


def test_candidate_copy_uses_stock_nested_state(tmp_path, monkeypatch):
    helper = load_helper()
    backup = nested_candidate_backup(tmp_path)
    runtime_root = install_candidate_workspace_fakes(
        helper, monkeypatch, tmp_path)
    candidate = None
    try:
        (candidate, fsdir, _config, _socket,
         _fragment_dir) = helper._copy_candidate_state(backup, 612, 613)
        assert fsdir == candidate / "var-lib-meshtasticd/.portduino/default"
        assert (fsdir / "prefs/device.proto").is_file()
    finally:
        if candidate is not None:
            shutil.rmtree(candidate)
    assert list(runtime_root.iterdir()) == []


class FakeCandidateProcess:
    def __init__(self, *, require_kill=False,
                 output=b"MAC ADDRESS: 02:00:A1:B2:C3:D4\n"):
        self.pid = 424242
        self.require_kill = require_kill
        self.returncode = None
        self.signals = []
        self.wait_calls = []
        self.stdout = io.BytesIO(output)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.require_kill and signal.SIGKILL not in self.signals:
            raise __import__("subprocess").TimeoutExpired("candidate", timeout)
        self.returncode = (
            -signal.SIGKILL if signal.SIGKILL in self.signals else 0)
        return self.returncode


def install_candidate_workspace_fakes(helper, monkeypatch, tmp_path):
    runtime_root = tmp_path / "validation-root"
    runtime_root.mkdir(mode=0o711)
    monkeypatch.setattr(
        helper, "_meshtasticd_credentials",
        lambda: (612, 613, [614, 615, 616]))
    monkeypatch.setattr(helper, "_secure_validation_root", lambda: runtime_root)
    monkeypatch.setattr(helper.os, "chown", lambda *a, **k: None)
    return runtime_root


def test_candidate_credentials_match_service_supplementary_groups(monkeypatch):
    helper = load_helper()
    groups = {
        "meshtasticd": NS(gr_gid=613),
        "spi": NS(gr_gid=614),
        "gpio": NS(gr_gid=615),
        "watchdogs": NS(gr_gid=616),
    }
    monkeypatch.setattr(
        helper.pwd, "getpwnam",
        lambda name: NS(pw_uid=612, pw_gid=613) if name == "meshtasticd"
        else (_ for _ in ()).throw(KeyError(name)))
    monkeypatch.setattr(helper.grp, "getgrnam", lambda name: groups[name])

    assert helper._meshtasticd_credentials() == (
        612, 613, [614, 615, 616])


def install_candidate_fakes(helper, monkeypatch, process, tmp_path):
    popen_calls = []
    workspaces = []
    runtime_root = install_candidate_workspace_fakes(
        helper, monkeypatch, tmp_path)
    monkeypatch.setattr(
        helper, "_validate_installed_binary",
        lambda: helper.WDG_BINARY_PATH)

    def popen(command, **kwargs):
        popen_calls.append((command, kwargs))
        workspaces.append(Path(kwargs["cwd"]))
        return process

    monkeypatch.setattr(helper.subprocess, "Popen", popen)
    def killpg(pid, sig):
        if pid != process.pid:
            pytest.fail("candidate cleanup targeted the wrong process group")
        if sig == 0:
            if process.returncode is not None:
                raise ProcessLookupError
            return
        process.signals.append(sig)

    monkeypatch.setattr(helper.os, "killpg", killpg)
    return popen_calls, workspaces, runtime_root


def test_candidate_dry_run_rejects_critical_state_mutation(
        tmp_path, monkeypatch):
    helper = load_helper()
    backup = candidate_backup(tmp_path)
    process = FakeCandidateProcess()
    _calls, workspaces, runtime_root = install_candidate_fakes(
        helper, monkeypatch, process, tmp_path)

    def health(socket_path, **_kwargs):
        critical = socket_path.parent / "var-lib-meshtasticd/prefs/device.proto"
        critical.write_bytes(b"candidate changed identity")
        return semantic_status()

    monkeypatch.setattr(helper, "_health_check", health)

    with pytest.raises(helper.HelperError, match="modified critical"):
        helper._candidate_dry_run(backup)
    assert process.signals == [signal.SIGTERM]
    assert process.returncode == 0
    assert all(not path.exists() for path in workspaces)
    assert list(runtime_root.iterdir()) == []


def test_candidate_dry_run_allows_volatile_node_history_and_sanitizes_env(
        tmp_path, monkeypatch):
    helper = load_helper()
    backup = candidate_backup(tmp_path)
    process = FakeCandidateProcess()
    calls, workspaces, runtime_root = install_candidate_fakes(
        helper, monkeypatch, process, tmp_path)

    def health(socket_path, **_kwargs):
        assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(socket_path.parent.parent.stat().st_mode) == 0o711
        fsdir = socket_path.parent / "var-lib-meshtasticd"
        (fsdir / "prefs/nodes.proto").write_bytes(b"volatile node cache")
        (fsdir / "history.log").write_text("volatile history")
        return semantic_status()

    monkeypatch.setattr(helper, "_health_check", health)

    snapshot = helper._candidate_dry_run(backup)

    assert snapshot["semantic"]["node_id"] == "!a1b2c3d4"
    assert snapshot["semantic"]["channels"] == [{
        "index": 0, "name": "LongFast", "role": 1, "has_psk": True,
    }]
    assert snapshot["effective_mac"] == "02:00:A1:B2:C3:D4"
    assert snapshot["mac_pin_required"] is True
    command, kwargs = calls[0]
    assert command[:6] == [
        str(helper.FLOCK_PATH), "-n", "-E", "75",
        str(helper.RADIO_LOCK_PATH), str(helper.WDG_BINARY_PATH),
    ]
    assert command[6] == "--port=0"
    assert any(value.startswith("--fsdir=") for value in command)
    assert any(value.startswith("--config=") for value in command)
    assert kwargs["env"]["MESHTASTIC_WDG_ALLOWED_UID"] == "0"
    assert kwargs["env"]["MESHTASTIC_WDG_DISABLE_BLUETOOTH"] == "1"
    assert kwargs["env"]["MESHTASTIC_WDG_SOCKET"].endswith("/wdg.sock")
    assert kwargs["env"]["MESHTASTIC_WDG_SOCKET"] != str(
        helper.WDG_SOCKET_PATH)
    assert "MESHTASTIC_WDG_POLICY" not in kwargs["env"]
    assert "LD_PRELOAD" not in kwargs["env"]
    assert kwargs["stdin"] is helper.subprocess.DEVNULL
    assert kwargs["stdout"] is helper.subprocess.PIPE
    assert kwargs["stderr"] is helper.subprocess.STDOUT
    assert kwargs["user"] == 612
    assert kwargs["group"] == 613
    assert kwargs["extra_groups"] == [614, 615, 616]
    assert kwargs["umask"] == 0o077
    assert kwargs["env"]["HOME"] == kwargs["cwd"]
    assert kwargs["env"]["TMPDIR"] == kwargs["cwd"]
    assert all(not path.exists() for path in workspaces)
    assert list(runtime_root.iterdir()) == []


def test_candidate_config_directory_is_redirected_into_private_copy(
        tmp_path, monkeypatch):
    helper = load_helper()
    backup = candidate_backup(tmp_path)
    source_config = backup / "state/etc-meshtasticd"
    (source_config / "config.d").mkdir()
    (source_config / "config.d/radio.yaml").write_text(
        "Lora:\n  Module: sx1262\n", encoding="utf-8")
    (source_config / "config.yaml").write_text(
        "General:\n"
        "  ConfigDirectory: /etc/meshtasticd/config.d/\n"
        "  AvailableDirectory: /etc/meshtasticd/available.d/\n",
        encoding="utf-8",
    )

    runtime_root = install_candidate_workspace_fakes(
        helper, monkeypatch, tmp_path)
    candidate = None
    try:
        (candidate, _fsdir, config, _socket,
         fragment_dir) = helper._copy_candidate_state(backup, 612, 613)

        text = config.read_text(encoding="utf-8")
        expected = candidate / "etc-meshtasticd/config.d"
        assert fragment_dir == expected
        assert f"ConfigDirectory: {json.dumps(str(expected))}" in text
        assert "/etc/meshtasticd/config.d/" not in text
        assert (expected / "radio.yaml").is_file()
    finally:
        if candidate is not None:
            shutil.rmtree(candidate)
    assert list(runtime_root.iterdir()) == []


@pytest.mark.parametrize("value", [
    "/tmp/external-config.d",
    "../../live-config.d",
])
def test_candidate_config_directory_rejects_paths_outside_copy(
        tmp_path, value, monkeypatch):
    helper = load_helper()
    backup = candidate_backup(tmp_path)
    config = backup / "state/etc-meshtasticd/config.yaml"
    config.write_text(
        f"General:\n  ConfigDirectory: {value}\n", encoding="utf-8")

    runtime_root = install_candidate_workspace_fakes(
        helper, monkeypatch, tmp_path)
    with pytest.raises(helper.HelperError, match="ConfigDirectory"):
        helper._copy_candidate_state(backup, 612, 613)
    assert list(runtime_root.iterdir()) == []


@pytest.mark.parametrize("text", [
    "General: {ConfigDirectory: /etc/meshtasticd/config.d/}\n",
    '"General":\n  ConfigDirectory: /etc/meshtasticd/config.d/\n',
    ("defaults: &general\n  ConfigDirectory: /etc/meshtasticd/config.d/\n"
     "General:\n  <<: *general\n"),
    ("defaults: &root\n"
     "  General:\n"
     "    ConfigDirectory: /etc/meshtasticd/config.d/\n"
     "<<: *root\n"),
])
def test_candidate_config_rejects_indirect_config_directory(
        tmp_path, text, monkeypatch):
    helper = load_helper()
    backup = candidate_backup(tmp_path)
    config = backup / "state/etc-meshtasticd/config.yaml"
    config.write_text(text, encoding="utf-8")

    runtime_root = install_candidate_workspace_fakes(
        helper, monkeypatch, tmp_path)
    with pytest.raises(helper.HelperError, match="General"):
        helper._copy_candidate_state(backup, 612, 613)
    assert list(runtime_root.iterdir()) == []


def test_candidate_config_rejects_non_plain_config_directory_key(
        tmp_path, monkeypatch):
    helper = load_helper()
    backup = candidate_backup(tmp_path)
    config = backup / "state/etc-meshtasticd/config.yaml"
    config.write_text(
        "General:\n  ConfigDirectory : /etc/meshtasticd/config.d/\n",
        encoding="utf-8")
    runtime_root = install_candidate_workspace_fakes(
        helper, monkeypatch, tmp_path)

    with pytest.raises(helper.HelperError, match="ConfigDirectory"):
        helper._copy_candidate_state(backup, 612, 613)

    assert list(runtime_root.iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_candidate_copy_rejects_recursive_symlinks_and_special_files(
        tmp_path, monkeypatch, kind):
    helper = load_helper()
    backup = candidate_backup(tmp_path)
    unsafe = backup / "state/var-lib-meshtasticd/prefs/unsafe"
    if kind == "symlink":
        unsafe.symlink_to("device.proto")
        match = "symlinks"
    else:
        os.mkfifo(unsafe)
        match = "special files"
    runtime_root = install_candidate_workspace_fakes(
        helper, monkeypatch, tmp_path)

    with pytest.raises(helper.HelperError, match=match):
        helper._copy_candidate_state(backup, 612, 613)

    assert list(runtime_root.iterdir()) == []


def test_candidate_process_is_killed_and_reaped_after_health_failure(
        tmp_path, monkeypatch):
    helper = load_helper()
    backup = candidate_backup(tmp_path)
    process = FakeCandidateProcess(require_kill=True)
    _calls, workspaces, runtime_root = install_candidate_fakes(
        helper, monkeypatch, process, tmp_path)
    monkeypatch.setattr(
        helper, "_health_check",
        Mock(side_effect=helper.HelperError("candidate health failed")))

    with pytest.raises(helper.HelperError, match="candidate health failed"):
        helper._candidate_dry_run(backup)

    assert process.signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.returncode == -9
    assert process.wait_calls == [5.0, 5.0]
    assert all(not path.exists() for path in workspaces)
    assert list(runtime_root.iterdir()) == []


def test_candidate_health_reports_shared_radio_lock_contention():
    helper = load_helper()
    process = NS(returncode=75, poll=lambda: 75)

    with pytest.raises(
            helper.HelperError, match="AIO SX1262 is already owned"):
        helper._health_check(
            Path("/definitely/not/created.sock"), timeout=0.1,
            process=process)


class _HealthSocket:
    def __init__(self, *packets):
        self.packets = [json.dumps(packet).encode("utf-8")
                        for packet in packets]
        self.sent = []

    def sendall(self, payload):
        self.sent.append(json.loads(payload))

    def recv(self, _size):
        return self.packets.pop(0)


def test_health_socket_requires_versioned_reply_envelope():
    helper = load_helper()
    valid_body = {"protocol_version": 1}

    for invalid in (
        {"type": "reply", "request_id": "health", "ok": True,
         "body": valid_body},
        {"v": 2, "type": "reply", "request_id": "health", "ok": True,
         "body": valid_body},
        {"v": True, "type": "reply", "request_id": "health", "ok": True,
         "body": valid_body},
        {"v": 1, "type": "reply", "request_id": "other", "ok": True,
         "body": valid_body},
        {"v": 1, "type": "unknown", "request_id": "health", "ok": True,
         "body": valid_body},
    ):
        with pytest.raises(helper.HelperError, match="health (envelope|reply)"):
            helper._socket_request(
                _HealthSocket(invalid), "health", "get_status")


def test_health_socket_accepts_only_well_formed_events_before_reply():
    helper = load_helper()
    client = _HealthSocket(
        {"v": 1, "type": "event", "event_id": 1, "name": "ready",
         "body": {}},
        {"v": 1, "type": "reply", "request_id": "health", "ok": True,
         "body": {"protocol_version": 1}},
    )

    assert helper._socket_request(client, "health", "get_status") == {
        "protocol_version": 1}
    assert client.sent == [{
        "v": 1, "type": "command", "request_id": "health",
        "name": "get_status", "body": {},
    }]

    with pytest.raises(helper.HelperError, match="event envelope"):
        helper._socket_request(
            _HealthSocket({"v": 1, "type": "event", "name": "ready"}),
            "health", "get_status")

    for event_id in (True, -1, "1", None):
        with pytest.raises(helper.HelperError, match="event envelope"):
            helper._socket_request(
                _HealthSocket({
                    "v": 1, "type": "event", "event_id": event_id,
                    "name": "ready", "body": {},
                }),
                "health", "get_status")


@pytest.mark.parametrize("protocol_version", [True, 1.0, "1", 2, None])
def test_health_requires_plain_api_version_one(protocol_version):
    helper = load_helper()
    with pytest.raises(helper.HelperError, match="incompatible WDG API"):
        helper._validate_health_bodies(
            {"protocol_version": protocol_version}, semantic_status())


@pytest.mark.parametrize("status", [
    {**semantic_status(), "state": "starting"},
    {**semantic_status(), "radio_status": "starting"},
    {key: value for key, value in semantic_status().items() if key != "state"},
    {key: value for key, value in semantic_status().items()
     if key != "radio_status"},
])
def test_health_requires_both_ready_states(status):
    helper = load_helper()
    with pytest.raises(helper.HelperError, match="radio is not ready"):
        helper._validate_health_bodies({"protocol_version": 1}, status)


def test_candidate_process_group_is_killed_after_leader_already_exited(
        monkeypatch):
    helper = load_helper()
    process = FakeCandidateProcess()
    process.returncode = 23
    signals = []
    group_wait = Mock(side_effect=(False, True))
    monkeypatch.setattr(
        helper.os, "killpg",
        lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(helper, "_wait_for_process_group_exit", group_wait)

    helper._terminate_candidate(process)

    assert signals == [
        (process.pid, signal.SIGTERM),
        (process.pid, signal.SIGKILL),
    ]
    assert process.wait_calls == [1.0]
    assert group_wait.call_args_list == [
        call(process.pid, 5.0),
        call(process.pid, 5.0),
    ]


def test_candidate_output_capture_is_bounded_and_workspace_is_cleaned(
        tmp_path, monkeypatch):
    helper = load_helper()
    backup = candidate_backup(tmp_path)
    process = FakeCandidateProcess(
        output=(b"x" * (helper.STARTUP_OUTPUT_LIMIT + 1)
                + b"\nMAC ADDRESS: 02:00:A1:B2:C3:D4\n"))
    _calls, workspaces, runtime_root = install_candidate_fakes(
        helper, monkeypatch, process, tmp_path)
    monkeypatch.setattr(
        helper, "_health_check", lambda *_args, **_kwargs: semantic_status())

    with pytest.raises(helper.HelperError, match="exceeded"):
        helper._candidate_dry_run(backup)

    assert all(not path.exists() for path in workspaces)
    assert list(runtime_root.iterdir()) == []


@pytest.mark.parametrize("status", [
    {},
    {"identity": {}, "channels": []},
    semantic_status(public="yes"),
    semantic_status(channels=[{
        "index": 0, "name": "one", "role": 1, "has_psk": False,
    }, {
        "index": 0, "name": "duplicate", "role": 2, "has_psk": True,
    }]),
])
def test_semantic_status_rejects_missing_wrong_or_duplicate_fields(status):
    helper = load_helper()
    with pytest.raises(helper.HelperError, match="malformed"):
        helper._semantic_status_snapshot(status)


def test_effective_mac_parser_is_strict_and_cross_checks_node_identity():
    helper = load_helper()
    semantic = helper._semantic_status_snapshot(semantic_status())

    assert helper._parse_effective_mac(
        b"startup\nMAC ADDRESS: 02:00:A1:B2:C3:D4\n",
        truncated=False, semantic=semantic) == "02:00:A1:B2:C3:D4"

    for output in (
            b"MAC ADDRESS: 02:00:a1:b2:c3:d4\n",
            b"MAC ADDRESS: 02:00:A1:B2:C3\n",
            (b"MAC ADDRESS: 02:00:A1:B2:C3:D4\n"
             b"MAC ADDRESS: 02:00:A1:B2:C3:D4\n")):
        with pytest.raises(helper.HelperError, match="exactly one"):
            helper._parse_effective_mac(
                output, truncated=False, semantic=semantic)
    with pytest.raises(helper.HelperError, match="does not map"):
        helper._parse_effective_mac(
            b"MAC ADDRESS: 02:00:11:22:33:44\n",
            truncated=False, semantic=semantic)
    with pytest.raises(helper.HelperError, match="exceeded"):
        helper._parse_effective_mac(
            b"MAC ADDRESS: 02:00:A1:B2:C3:D4\n",
            truncated=True, semantic=semantic)


def test_mac_pin_rewrites_only_supported_general_mappings():
    helper = load_helper()
    mac = "02:00:A1:B2:C3:D4"

    rendered, changed = helper._rewrite_general_mac_text(
        "Lora:\n  Module: sx1262\n", mac)
    assert changed is True
    assert rendered.endswith(
        'General:\n  MACAddress: "02:00:A1:B2:C3:D4"\n')

    rendered, changed = helper._rewrite_general_mac_text(
        "General:\n  MACAddressSource: eth0\n  MaxNodes: 100\n", mac)
    assert changed is True
    assert "MACAddressSource" not in rendered
    assert 'MACAddress: "02:00:A1:B2:C3:D4"' in rendered

    explicit = 'General:\n  MACAddress: "02:00:A1:B2:C3:D4"\n'
    assert helper._rewrite_general_mac_text(explicit, mac) == (explicit, False)

    for unsafe in (
            "defaults: &defaults\n  MaxNodes: 100\nGeneral:\n  <<: *defaults\n",
            "General:\n  MACAddressSource: eth0\n  MACAddressSource: wlan0\n",
            "General: {MACAddressSource: eth0}\n",
            "General:\n  MACAddressSource : eth0\n",
            "General:\n  - MACAddressSource: eth0\n"):
        with pytest.raises(
                helper.HelperError,
                match="MACAddress|MAC pinning|duplicate|block"):
            helper._rewrite_general_mac_text(unsafe, mac)


def test_mac_pin_rejects_indirect_general_in_config_fragment(tmp_path):
    helper = load_helper()
    config = tmp_path / "config.yaml"
    fragments = tmp_path / "config.d"
    fragments.mkdir()
    config.write_text("Lora:\n  Module: sx1262\n", encoding="utf-8")
    (fragments / "indirect.yaml").write_text(
        "defaults: &defaults\n"
        "  General:\n"
        "    MaxNodes: 100\n"
        "<<: *defaults\n",
        encoding="utf-8")

    with pytest.raises(helper.HelperError, match="aliases"):
        helper._render_pinned_config(
            config, "02:00:A1:B2:C3:D4", fragments)


def test_candidate_applies_validated_mac_pin_in_disposable_config(
        tmp_path, monkeypatch):
    helper = load_helper()
    backup = candidate_backup(tmp_path)
    process = FakeCandidateProcess()
    _calls, workspaces, runtime_root = install_candidate_fakes(
        helper, monkeypatch, process, tmp_path)

    def health(socket_path, **_kwargs):
        config = socket_path.parent / "etc-meshtasticd/config.yaml"
        assert 'MACAddress: "02:00:A1:B2:C3:D4"' in config.read_text()
        return semantic_status()

    monkeypatch.setattr(helper, "_health_check", health)
    snapshot = helper._candidate_dry_run(
        backup, pinned_mac="02:00:A1:B2:C3:D4")

    assert snapshot["semantic"]["node_id"] == "!a1b2c3d4"
    assert snapshot["effective_mac"] == "02:00:A1:B2:C3:D4"
    assert snapshot["mac_pin_required"] is False
    assert all(not path.exists() for path in workspaces)
    assert list(runtime_root.iterdir()) == []


def test_atomic_live_mac_pin_is_restored_from_transaction_backup(
        tmp_path, monkeypatch):
    helper = load_helper()
    config_dir = tmp_path / "etc-meshtasticd"
    state_dir = tmp_path / "var-lib-meshtasticd"
    config_dir.mkdir()
    state_dir.mkdir()
    config = config_dir / "config.yaml"
    original = "Lora:\n  Module: sx1262\n"
    config.write_text(original, encoding="utf-8")
    config.chmod(0o640)

    backup = tmp_path / "backup"
    backup_state = backup / "state"
    backup_state.mkdir(parents=True)
    shutil.copytree(
        config_dir, backup_state / helper._state_backup_key(config_dir))
    shutil.copytree(
        state_dir, backup_state / helper._state_backup_key(state_dir))
    presence = {
        helper._state_backup_key(config_dir): True,
        helper._state_backup_key(state_dir): True,
    }
    monkeypatch.setattr(helper, "MESHTASTIC_CONFIG_DIR", config_dir)
    monkeypatch.setattr(helper, "MESHTASTIC_STATE_DIR", state_dir)
    monkeypatch.setattr(helper, "STATE_PATHS", (config_dir, state_dir))
    monkeypatch.setattr(
        helper, "_validated_live_config", lambda path: path.lstat())
    monkeypatch.setattr(helper.os, "fchown", lambda *a: None)

    helper._pin_live_mac("02:00:A1:B2:C3:D4")
    assert 'MACAddress: "02:00:A1:B2:C3:D4"' in config.read_text()
    assert stat.S_IMODE(config.stat().st_mode) == 0o640

    helper._restore_state_from_backup(backup, presence)
    assert config.read_text(encoding="utf-8") == original


def test_candidate_process_is_reaped_before_malformed_status_is_rejected(
        tmp_path, monkeypatch):
    helper = load_helper()
    backup = candidate_backup(tmp_path)
    process = FakeCandidateProcess()
    _calls, workspaces, runtime_root = install_candidate_fakes(
        helper, monkeypatch, process, tmp_path)
    monkeypatch.setattr(
        helper, "_health_check", lambda *_args, **_kwargs: {"identity": {}})

    with pytest.raises(helper.HelperError, match="malformed"):
        helper._candidate_dry_run(backup)

    assert process.signals == [signal.SIGTERM]
    assert process.returncode == 0
    assert all(not path.exists() for path in workspaces)
    assert list(runtime_root.iterdir()) == []


def test_live_validation_rejects_semantic_change(monkeypatch):
    helper = load_helper()
    expected = helper._semantic_status_snapshot(semantic_status())
    monkeypatch.setattr(
        helper, "_health_check",
        lambda: semantic_status(long_name="Regenerated"))
    critical = Mock()
    monkeypatch.setattr(helper, "_critical_state_snapshot", critical)

    with pytest.raises(helper.HelperError, match="semantics differ"):
        helper._verify_live_state(expected, {"before": True})
    critical.assert_not_called()


def test_live_validation_rejects_critical_change(monkeypatch):
    helper = load_helper()
    status = semantic_status()
    expected = helper._semantic_status_snapshot(status)
    monkeypatch.setattr(helper, "_health_check", lambda: status)
    monkeypatch.setattr(
        helper, "_critical_state_snapshot",
        lambda *_args: {"after": True})
    monkeypatch.setattr(helper, "_resolve_state_fsdir", lambda path: path)

    with pytest.raises(helper.HelperError, match="modified critical"):
        helper._verify_live_state(expected, {"before": True})


def test_live_validation_allows_volatile_node_history_changes(
        tmp_path, monkeypatch):
    helper = load_helper()
    config = tmp_path / "etc-meshtasticd"
    fsdir = tmp_path / "var-lib-meshtasticd"
    config.mkdir()
    (config / "config.yaml").write_text("Lora: {}\n")
    (fsdir / "prefs").mkdir(parents=True)
    (fsdir / "backups").mkdir()
    (fsdir / "prefs/device.proto").write_bytes(b"identity")
    before = helper._critical_state_snapshot(fsdir, config)
    status = semantic_status()
    expected = helper._semantic_status_snapshot(status)
    monkeypatch.setattr(helper, "MESHTASTIC_CONFIG_DIR", config)
    monkeypatch.setattr(helper, "MESHTASTIC_STATE_DIR", fsdir)

    def health():
        (fsdir / "prefs/nodes.proto").write_bytes(b"updated nodes")
        (fsdir / "prefs/transmit_history.dat").write_bytes(b"updated history")
        return status

    monkeypatch.setattr(helper, "_health_check", health)

    assert helper._verify_live_state(expected, before) == status


@pytest.mark.parametrize(
    ("mode", "uid", "gid", "accepted"), [
        (stat.S_IFREG | 0o755, 0, 0, True),
        (stat.S_IFREG | 0o755, 1000, 0, False),
        (stat.S_IFREG | 0o775, 0, 0, False),
        (stat.S_IFREG | 0o644, 0, 0, False),
        (stat.S_IFLNK | 0o777, 0, 0, False),
    ])
def test_candidate_binary_validation_requires_fixed_root_owned_executable(
        monkeypatch, mode, uid, gid, accepted):
    helper = load_helper()

    class FakeFixedPath:
        def lstat(self):
            return NS(st_mode=mode, st_uid=uid, st_gid=gid)

    fixed = FakeFixedPath()
    monkeypatch.setattr(helper, "WDG_BINARY_PATH", fixed)
    if accepted:
        assert helper._validate_installed_binary() is fixed
    else:
        with pytest.raises(helper.HelperError, match="unsafe ownership or mode"):
            helper._validate_installed_binary()


def test_candidate_baseline_mismatch_triggers_automatic_rollback(
        tmp_path, monkeypatch):
    helper = load_helper()
    cache = tmp_path / "cache"
    stage = cache / "v2.8.1-wdg.1"
    stage.mkdir(parents=True, mode=0o700)
    package = stage / "meshtasticd-wdg_2.8.1+wdg1_arm64.deb"
    package.write_bytes(b"deb")
    prepared = fake_prepared_release(stage.name, stage)
    validator = NS(
        validate_prepared_release=lambda *a, **k: prepared,
        validate_installed_package_payload=Mock())
    backup = candidate_backup(tmp_path)
    metadata = baseline_metadata(helper)
    metadata["new_release"] = helper._release_identity(prepared)
    policy = tmp_path / "wdg-portduino.yaml"
    policy.write_text("wdg_api:\n  enabled: true\n")
    rolled_back = []
    monkeypatch.setattr(helper, "CACHE_ROOT", cache)
    monkeypatch.setattr(helper, "WDG_POLICY_PATH", policy)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(helper, "_load_validator", lambda: validator)
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_lock_transaction", lambda: _Context())
    monkeypatch.setattr(helper, "_service_snapshot", lambda: {
        "wdg": {"load_state": "loaded"},
        "stock": {"load_state": "loaded"},
    })
    monkeypatch.setattr(
        helper, "_create_backup", lambda *a: (backup, metadata))
    monkeypatch.setattr(helper, "_read_private_json", lambda _path: metadata)
    monkeypatch.setattr(
        helper, "_validate_transaction_metadata",
        lambda *_args: (None, {}, {}))
    monkeypatch.setattr(
        helper, "_run",
        lambda *a, **k: NS(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(
        helper, "_installed_version", lambda: prepared.package_version)
    monkeypatch.setattr(helper, "_state_unchanged", lambda _metadata: True)
    monkeypatch.setattr(helper.grp, "getgrnam", lambda _name: NS(gr_gid=995))
    monkeypatch.setattr(helper.os, "chown", lambda *a: None)
    mismatch = helper._semantic_status_snapshot(
        semantic_status(long_name="Regenerated"))
    monkeypatch.setattr(helper, "_candidate_dry_run", lambda *_a, **_k: {
        "semantic": mismatch,
        "effective_mac": "02:00:A1:B2:C3:D4",
        "mac_pin_required": False,
    })
    verify_live = Mock()
    monkeypatch.setattr(helper, "_verify_live_state", verify_live)
    monkeypatch.setattr(
        helper, "_restore_transaction",
        lambda backup_arg, *_args: rolled_back.append(backup_arg))

    with pytest.raises(helper.HelperError, match="was rolled back.*pre-install"):
        helper._install_tag(prepared.tag)

    assert rolled_back == [backup]
    verify_live.assert_not_called()


def test_live_integrity_failure_uses_existing_automatic_rollback(
        tmp_path, monkeypatch):
    helper = load_helper()
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    stage = cache / "v2.8.1-wdg.1"
    stage.mkdir(mode=0o700)
    package = stage / "meshtasticd-wdg_2.8.1+wdg1_arm64.deb"
    package.write_bytes(b"deb")
    prepared = fake_prepared_release(stage.name, stage)
    validator = NS(
        validate_prepared_release=lambda *a, **k: prepared,
        validate_installed_package_payload=Mock())
    backup = candidate_backup(tmp_path)
    metadata = baseline_metadata(helper)
    metadata["new_release"] = helper._release_identity(prepared)
    live_config = tmp_path / "live-etc"
    live_state = tmp_path / "live-state"
    live_config.mkdir()
    live_state.mkdir()
    policy = live_config / "wdg-portduino.yaml"
    policy.write_text("wdg_api:\n  enabled: true\n")
    rolled_back = []
    monkeypatch.setattr(helper, "CACHE_ROOT", cache)
    monkeypatch.setattr(helper, "WDG_POLICY_PATH", policy)
    monkeypatch.setattr(helper, "MESHTASTIC_CONFIG_DIR", live_config)
    monkeypatch.setattr(helper, "MESHTASTIC_STATE_DIR", live_state)
    monkeypatch.setattr(helper, "_secure_root_directory", lambda _path: None)
    monkeypatch.setattr(helper, "_load_validator", lambda: validator)
    monkeypatch.setattr(helper, "_require_root", lambda: None)
    monkeypatch.setattr(helper, "_lock_transaction", lambda: _Context())
    monkeypatch.setattr(helper, "_service_snapshot", lambda: {
        "wdg": {"load_state": "loaded"},
        "stock": {"load_state": "loaded"},
    })
    monkeypatch.setattr(
        helper, "_create_backup", lambda *a: (backup, metadata))
    monkeypatch.setattr(helper, "_read_private_json", lambda _path: metadata)
    monkeypatch.setattr(
        helper, "_validate_transaction_metadata",
        lambda *_args: (None, {}, {}))
    monkeypatch.setattr(
        helper, "_run",
        lambda *a, **k: NS(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(
        helper, "_installed_version", lambda: prepared.package_version)
    monkeypatch.setattr(helper, "_state_unchanged", lambda _metadata: True)
    monkeypatch.setattr(helper.grp, "getgrnam", lambda _name: NS(gr_gid=995))
    monkeypatch.setattr(helper.os, "chown", lambda *a: None)
    expected = helper._semantic_status_snapshot(semantic_status())
    monkeypatch.setattr(helper, "_candidate_dry_run", lambda _backup, **_kwargs: {
        "semantic": expected,
        "effective_mac": "02:00:A1:B2:C3:D4",
        "mac_pin_required": False,
    })
    monkeypatch.setattr(
        helper, "_critical_state_snapshot", lambda *a: {"critical": True})
    monkeypatch.setattr(
        helper, "_verify_live_state",
        Mock(side_effect=helper.HelperError("live semantics differ")))
    monkeypatch.setattr(
        helper, "_restore_transaction",
        lambda backup_arg, metadata_arg, validator_arg: rolled_back.append(
            backup_arg))
    monkeypatch.setattr(helper, "_cache_installed_release", Mock())
    monkeypatch.setattr(helper, "_record_last_backup", Mock())

    with pytest.raises(helper.HelperError, match="was rolled back"):
        helper._install_tag(prepared.tag)

    assert rolled_back == [backup]
    helper._cache_installed_release.assert_not_called()
    helper._record_last_backup.assert_not_called()


class _Context:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
