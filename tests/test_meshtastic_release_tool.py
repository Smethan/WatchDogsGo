"""Tests for the operator-facing Meshtastic release workflow."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "meshtastic_release.py"


def load_tool():
    spec = importlib.util.spec_from_file_location(
        "wdg_test_meshtastic_release", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves the importing module through sys.modules.
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def result(payload=None, *, returncode=0, stderr=""):
    return NS(
        returncode=returncode,
        stdout=json.dumps(payload if payload is not None else {"ok": True}),
        stderr=stderr,
    )


def service_payload(target, *, active=False, installed=True):
    return {
        "ok": True,
        "target": target,
        "service": (
            "meshtasticd-wdg.service" if target == "wdg"
            else "meshtasticd.service"),
        "load_state": "loaded",
        "active_state": "active" if active else "inactive",
        "unit_file_state": "enabled" if active else "disabled",
        "package_version": "2.8.0+wdg6" if target == "wdg" and installed else None,
    }


def test_tag_conversion_and_candidate_file_set(tmp_path):
    tool = load_tool()
    assert tool.package_version_for_tag("v2.8.0-wdg.7") == "2.8.0+wdg7"
    assert tool.package_name_for_tag("v2.8.0-wdg.7") == (
        "meshtasticd-wdg_2.8.0+wdg7_arm64.deb")
    with pytest.raises(tool.ReleaseToolError):
        tool.package_version_for_tag("v02.8.0-wdg.7")

    for name in tool.expected_candidate_names("v2.8.0-wdg.7"):
        (tmp_path / name).write_bytes(b"candidate")
    files = tool.validate_candidate_directory(tmp_path, "v2.8.0-wdg.7")
    assert {path.name for path in files} == tool.expected_candidate_names(
        "v2.8.0-wdg.7")

    (tmp_path / "unexpected").write_bytes(b"no")
    with pytest.raises(tool.ReleaseToolError, match="file set is not exact"):
        tool.validate_candidate_directory(tmp_path, "v2.8.0-wdg.7")


def test_workflow_and_artifact_provenance_are_exact():
    tool = load_tool()
    run = {
        "id": 123,
        "run_attempt": 2,
        "event": "push",
        "head_branch": "v2.8.0-wdg.7",
        "head_sha": "a" * 40,
        "status": "completed",
        "conclusion": "success",
        "path": ".github/workflows/wdg-native.yml",
        "repository": {"full_name": "Smethan/meshtastic-firmware"},
    }
    assert tool.validate_workflow_run(
        run, tag="v2.8.0-wdg.7", run_id=123, attempt=2) is run

    artifact = {
        "id": 456,
        "name": "meshtasticd-wdg-arm64-123-2",
        "expired": False,
        "workflow_run": {"id": 123},
    }
    listing = {"artifacts": [artifact]}
    assert tool.validate_artifact_listing(
        listing, run_id=123, attempt=2) is artifact

    for field, value in (
        ("event", "workflow_dispatch"),
        ("head_branch", "main"),
        ("conclusion", "failure"),
        ("run_attempt", 1),
    ):
        changed = dict(run, **{field: value})
        with pytest.raises(tool.ReleaseToolError):
            tool.validate_workflow_run(
                changed, tag="v2.8.0-wdg.7", run_id=123, attempt=2)

    with pytest.raises(tool.ReleaseToolError, match="expired"):
        tool.validate_artifact_listing(
            {"artifacts": [dict(artifact, expired=True)]},
            run_id=123, attempt=2)


def test_adoption_stops_and_restores_only_previously_active_service():
    tool = load_tool()
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        operation = command[3]
        if operation == "status":
            target = command[4]
            return result(service_payload(
                target, active=target == "wdg", installed=True))
        if operation == "adopt-installed":
            return result({
                "ok": True,
                "action": "adopt-installed",
                "tag": "v2.8.0-wdg.6",
                "package_version": "2.8.0+wdg6",
                "health": "ready",
            })
        return result({"ok": True, "action": operation})

    manager = tool.ReleaseManager(runner=runner, gh="/usr/bin/gh")
    reply = manager.adopt("v2.8.0-wdg.6")

    assert reply["health"] == "ready"
    mutations = [call[3:] for call in calls if call[3] != "status"]
    assert mutations == [
        ["stop", "wdg"],
        ["adopt-installed", "v2.8.0-wdg.6"],
        ["start", "wdg"],
    ]
    assert not any(call[3] in {"enable", "disable"} for call in calls)


def test_failed_adoption_restores_service_and_preserves_primary_error():
    tool = load_tool()
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        operation = command[3]
        if operation == "status":
            target = command[4]
            return result(service_payload(
                target, active=target == "wdg", installed=True))
        if operation == "adopt-installed":
            return result(returncode=1, stderr="release is still a draft")
        return result({"ok": True, "action": operation})

    manager = tool.ReleaseManager(runner=runner, gh="/usr/bin/gh")
    with pytest.raises(tool.ReleaseToolError, match="still a draft"):
        manager.adopt("v2.8.0-wdg.6")

    assert ["start", "wdg"] in [call[3:] for call in calls]


def test_adoption_refuses_overlapping_daemons_before_mutation():
    tool = load_tool()
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        assert command[3] == "status"
        return result(service_payload(
            command[4], active=True, installed=True))

    manager = tool.ReleaseManager(runner=runner, gh="/usr/bin/gh")
    with pytest.raises(tool.ReleaseToolError, match="Both.*active"):
        manager.adopt("v2.8.0-wdg.6")

    assert all(call[3] == "status" for call in calls)


def test_publishing_requires_explicit_confirmation_before_any_command():
    tool = load_tool()
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        return result()

    manager = tool.ReleaseManager(runner=runner, gh="/usr/bin/gh")
    with pytest.raises(tool.ReleaseToolError, match="--yes"):
        manager.publish_adopt_draft(
            tag="v2.8.0-wdg.6", run_id=123, attempt=1,
            confirmed=False)
    assert calls == []


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_positive_integer_rejects_invalid_cli_values(value):
    tool = load_tool()
    with pytest.raises(argparse.ArgumentTypeError):
        tool.positive_integer(value)
