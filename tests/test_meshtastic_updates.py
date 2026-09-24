"""Release and package validation for the isolated Meshtastic fork."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

from watchdogs import meshtastic_updates as updates

TAG = "v2.8.1-wdg.1"
VERSION = "2.8.1+wdg1"
ASSET = f"meshtasticd-wdg_{VERSION}_arm64.deb"


def _core_manifest(tag=TAG):
    return {
        "format": 1,
        "repository": "Smethan/meshtastic-firmware",
        "tag": tag,
        "upstream_repository": "meshtastic/firmware",
        "upstream_tag": "v2.8.1",
        "upstream_commit": "a" * 40,
        "wdg_api": {"major": 1, "minor": 0},
        "package": {
            "name": "meshtasticd-wdg",
            "version": updates.package_version_for_tag(tag),
            "architecture": "arm64",
            "asset": updates.package_asset_for_tag(tag),
        },
        "minimum_glibc": "2.36",
    }


def _write(path: Path, data: bytes | str, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    path.chmod(mode)


def build_package(tmp_path: Path, *, extra_path: str | None = None,
                  control_extra: str = "", maintainer_script: str | None = None):
    root = tmp_path / "package-root"
    control = (
        "Package: meshtasticd-wdg\n"
        f"Version: {VERSION}\n"
        "Architecture: arm64\n"
        "Maintainer: WatchDogsGo tests <nobody@example.invalid>\n"
        "Description: isolated test package\n"
        + control_extra
    )
    _write(root / "DEBIAN/control", control)
    _write(root / "DEBIAN/postinst", updates.SAFE_POSTINST, 0o755)
    if maintainer_script:
        _write(root / f"DEBIAN/{maintainer_script}", "#!/bin/sh\nexit 0\n", 0o755)

    # Minimal ELF64 little-endian AArch64 header.  dpkg does not execute it;
    # the validator only needs enough header bytes to prove the architecture.
    elf = bytearray(64)
    elf[:4] = b"\x7fELF"
    elf[4] = 2
    elf[5] = 1
    elf[18:20] = (183).to_bytes(2, "little")
    _write(root / "usr/lib/meshtasticd-wdg/meshtasticd", bytes(elf), 0o755)
    _write(
        root / "usr/lib/systemd/system/meshtasticd-wdg.service",
        "[Unit]\nConflicts=meshtasticd.service\n"
        "[Service]\nUser=meshtasticd\nGroup=meshtasticd\n"
        "ExecStart=/usr/bin/flock -n -E 75 "
        "/run/lock/watchdogs/aio-sx1262.lock "
        "/usr/lib/meshtasticd-wdg/meshtasticd "
        "--config=/etc/meshtasticd/config.yaml "
        "--fsdir=/var/lib/meshtasticd\n",
    )
    _write(root / "usr/lib/tmpfiles.d/meshtasticd-wdg.conf",
           "d /run/meshtasticd 0750 meshtasticd meshtasticd -\n")
    _write(root / "usr/lib/sysusers.d/meshtasticd-wdg.conf",
           "u meshtasticd - 'Meshtastic daemon' /var/lib/meshtasticd\n")
    _write(
        root / "usr/share/dbus-1/system.d/meshtasticd-wdg.conf",
        "<!DOCTYPE busconfig PUBLIC '-//freedesktop//DTD D-BUS Bus "
        "Configuration 1.0//EN' "
        "'http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd'>\n"
        "<busconfig><policy user='meshtasticd'>"
        "<allow send_destination='org.bluez'/></policy></busconfig>\n",
    )
    _write(root / "usr/share/meshtasticd-wdg/wdg-portduino.example.yaml",
           "wdg_api:\n  enabled: true\n")
    _write(root / "usr/share/doc/meshtasticd-wdg/copyright", "GPL-3.0\n")
    _write(root / "usr/share/doc/meshtasticd-wdg/UPSTREAM_BASE",
           "upstream_tag=v2.8.1\nupstream_commit=" + "a" * 40 + "\n")
    embedded = _core_manifest()
    embedded["artifact_digest_source"] = "GitHub release compatibility.json"
    _write(root / "usr/share/doc/meshtasticd-wdg/compatibility.json",
           json.dumps(embedded, sort_keys=True))
    if extra_path:
        _write(root / extra_path.lstrip("/"), "forbidden\n", 0o755)

    package = tmp_path / ASSET
    subprocess.run(
        ["dpkg-deb", "--build", "--root-owner-group", str(root), str(package)],
        check=True, capture_output=True)
    manifest = _core_manifest()
    blob = package.read_bytes()
    manifest["package"]["size"] = len(blob)
    manifest["package"]["sha256"] = hashlib.sha256(blob).hexdigest()
    return package, manifest


def stage_release(tmp_path: Path, **package_options):
    package, manifest = build_package(tmp_path, **package_options)
    stage = tmp_path / TAG
    stage.mkdir(mode=0o700)
    manifest_blob = json.dumps(manifest, sort_keys=True).encode()
    (stage / "compatibility.json").write_bytes(manifest_blob)
    (stage / ASSET).write_bytes(package.read_bytes())
    sums = (
        hashlib.sha256(manifest_blob).hexdigest() + "  compatibility.json\n"
        + manifest["package"]["sha256"] + "  " + ASSET + "\n")
    (stage / "SHA256SUMS").write_text(sums, encoding="ascii")
    for child in stage.iterdir():
        child.chmod(0o600)
    return stage, manifest


def release_entry(tag=TAG, *, repo="Smethan/meshtastic-firmware"):
    asset = updates.package_asset_for_tag(tag)
    base = f"https://github.com/{repo}/releases/download/{quote(tag, safe='.-')}/"
    names = (asset, "compatibility.json", "SHA256SUMS")
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": name, "browser_download_url": base + quote(name, safe="._+-")}
            for name in names
        ],
    }


def test_tag_and_package_names_are_a_separate_version_contract():
    assert updates.parse_meshtastic_tag(TAG) == (2, 8, 1, 1)
    assert updates.package_version_for_tag(TAG) == VERSION
    assert updates.package_asset_for_tag(TAG) == ASSET
    for value in ("v2.8.1", "2.8.1-wdg.1", "v2.8.1-wdg.0/../../x"):
        with pytest.raises(ValueError):
            updates.parse_meshtastic_tag(value)


def test_release_list_accepts_only_exact_smethan_assets(monkeypatch):
    good = release_entry()
    older = release_entry("v2.8.0-wdg.9")
    wrong_repo = release_entry(repo="Smethan/firmware")
    extra = release_entry()
    extra["assets"].append({
        "name": "unexpected.deb",
        "browser_download_url": (
            "https://github.com/Smethan/meshtastic-firmware/releases/download/"
            + TAG + "/unexpected.deb"),
    })
    payload = json.dumps([
        older, good, dict(good, draft=True), dict(good, prerelease=True),
        wrong_repo, extra, good,
    ]).encode()
    monkeypatch.setattr(updates, "download", lambda url, limit: payload)
    # Default arguments bind at definition time, so inject the downloader.
    found = updates.meshtastic_releases(downloader=lambda url, limit: payload)
    assert [item["tag_name"] for item in found] == [TAG, "v2.8.0-wdg.9"]


def test_valid_package_and_staged_release_are_accepted(tmp_path):
    stage, manifest = stage_release(tmp_path)
    prepared = updates.validate_prepared_release(
        stage, expected_tag=TAG, require_secure=False,
        machine="aarch64", glibc_version="2.36")
    assert prepared.package_version == VERSION
    assert prepared.package_path.name == ASSET
    assert prepared.manifest == manifest


@pytest.mark.parametrize("extra_path", [
    "usr/bin/meshtasticd",
    "usr/lib/systemd/system/meshtasticd.service",
    "var/lib/meshtasticd/identity",
    "etc/meshtasticd/config.yaml",
])
def test_package_rejects_forbidden_paths(tmp_path, extra_path):
    stage, _ = stage_release(tmp_path, extra_path=extra_path)
    with pytest.raises(ValueError, match="forbidden path"):
        updates.validate_prepared_release(
            stage, expected_tag=TAG, require_secure=False, check_host=False)


@pytest.mark.parametrize("control_extra", [
    "Conflicts: meshtasticd\n",
    "Replaces: meshtasticd\n",
    "Breaks: meshtasticd\n",
])
def test_package_rejects_debian_takeover_fields(tmp_path, control_extra):
    stage, _ = stage_release(tmp_path, control_extra=control_extra)
    with pytest.raises(ValueError, match="must not declare"):
        updates.validate_prepared_release(
            stage, expected_tag=TAG, require_secure=False, check_host=False)


def test_package_rejects_maintainer_scripts(tmp_path):
    stage, _ = stage_release(tmp_path, maintainer_script="prerm")
    with pytest.raises(ValueError, match="forbidden path|maintainer"):
        updates.validate_prepared_release(
            stage, expected_tag=TAG, require_secure=False, check_host=False)


def test_manifest_rejects_wrong_repo_api_arch_and_host(tmp_path):
    _, manifest = stage_release(tmp_path)
    for mutate, message in (
        (lambda item: item.update(repository="Smethan/firmware"), "Smethan fork"),
        (lambda item: item["wdg_api"].update(major=2), "API major"),
        (lambda item: item["package"].update(architecture="amd64"), "ARM64"),
    ):
        candidate = copy.deepcopy(manifest)
        mutate(candidate)
        with pytest.raises(ValueError, match=message):
            updates.validate_compatibility_manifest(candidate, expected_tag=TAG)
    with pytest.raises(RuntimeError, match="ARM64 hosts only"):
        updates.check_host_compatibility(manifest, machine="x86_64", glibc_version="2.36")
    with pytest.raises(RuntimeError, match="requires glibc"):
        updates.check_host_compatibility(manifest, machine="aarch64", glibc_version="2.35")


def test_prepare_downloads_three_verified_assets_into_private_cache(tmp_path):
    built = tmp_path / "built"
    built.mkdir()
    package, manifest = build_package(built)
    manifest_blob = json.dumps(manifest, sort_keys=True).encode()
    sums = (
        hashlib.sha256(manifest_blob).hexdigest() + "  compatibility.json\n"
        + manifest["package"]["sha256"] + "  " + ASSET + "\n").encode()
    release = release_entry()
    base = f"https://github.com/Smethan/meshtastic-firmware/releases/download/{TAG}/"
    payloads = {
        base + "compatibility.json": manifest_blob,
        base + "SHA256SUMS": sums,
        base + ASSET: package.read_bytes(),
    }
    cache = tmp_path / "cache"
    prepared = updates.prepare_meshtastic_release(
        cache_root=cache, release=release,
        downloader=lambda url, limit: payloads[url], require_root=False,
        machine="aarch64", glibc_version="2.36")
    assert prepared.directory == cache / TAG
    assert stat_mode(cache) == 0o700
    assert stat_mode(prepared.directory) == 0o700
    assert all(stat_mode(path) == 0o600 for path in prepared.directory.iterdir())


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
