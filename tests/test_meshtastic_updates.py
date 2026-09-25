"""Release and package validation for the isolated Meshtastic fork."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import tarfile
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from watchdogs import meshtastic_updates as updates

TAG = "v2.8.1-wdg.1"
VERSION = "2.8.1+wdg1"
ASSET = f"meshtasticd-wdg_{VERSION}_arm64.deb"
DEFAULT_DEPENDS = (
    "adduser, bluez, dbus, util-linux, libc6 (>= 2.36), "
    "libgcc-s1 (>= 3.0), libstdc++6 (>= 12)"
)


def _core_manifest(tag=TAG):
    source_commit = "b" * 40
    return {
        "format": 1,
        "repository": "Smethan/meshtastic-firmware",
        "tag": tag,
        "upstream_repository": "meshtastic/firmware",
        "upstream_tag": "v2.8.1",
        "upstream_commit": "a" * 40,
        "source_commit": source_commit,
        "source_url": (
            "https://github.com/Smethan/meshtastic-firmware/tree/"
            + source_commit),
        "source_ref": tag,
        "license": "GPL-3.0-only",
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
                  control_extra: str = "", maintainer_script: str | None = None,
                  service: bytes | None = None,
                  tmpfiles: bytes | None = None,
                  sysusers: bytes | None = None,
                  depends: str = DEFAULT_DEPENDS,
                  binary_directory_mode: int = 0o755):
    root = tmp_path / "package-root"
    control = (
        "Package: meshtasticd-wdg\n"
        f"Version: {VERSION}\n"
        "Architecture: arm64\n"
        "Maintainer: Smethan <noreply@github.com>\n"
        "Section: net\n"
        "Priority: optional\n"
        f"Depends: {depends}\n"
        "Homepage: https://github.com/Smethan/meshtastic-firmware\n"
        "Description: Meshtastic daemon with WatchDogsGo local API and BlueZ phone transport\n"
        " Side-by-side Meshtastic Portduino build for the uConsole AIO v2 radio.\n"
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
    (root / "usr/lib/meshtasticd-wdg").chmod(binary_directory_mode)
    _write(
        root / "usr/lib/systemd/system/meshtasticd-wdg.service",
        service if service is not None else updates.SAFE_SERVICE,
    )
    _write(root / "usr/lib/tmpfiles.d/meshtasticd-wdg.conf",
           tmpfiles if tmpfiles is not None else updates.SAFE_TMPFILES)
    _write(root / "usr/lib/sysusers.d/meshtasticd-wdg.conf",
           sysusers if sysusers is not None else updates.SAFE_SYSUSERS)
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
    (stage / updates.SOURCE_ASSET).write_text(
        "Corresponding source: https://github.com/Smethan/meshtastic-firmware\n",
        encoding="utf-8")
    (stage / updates.COPYRIGHT_ASSET).write_text(
        "GPL-3.0-only\n", encoding="utf-8")
    sums = (
        hashlib.sha256(manifest_blob).hexdigest() + "  compatibility.json\n"
        + manifest["package"]["sha256"] + "  " + ASSET + "\n"
        + hashlib.sha256((stage / updates.SOURCE_ASSET).read_bytes()).hexdigest()
        + "  " + updates.SOURCE_ASSET + "\n"
        + hashlib.sha256((stage / updates.COPYRIGHT_ASSET).read_bytes()).hexdigest()
        + "  " + updates.COPYRIGHT_ASSET + "\n")
    (stage / "SHA256SUMS").write_text(sums, encoding="ascii")
    for child in stage.iterdir():
        child.chmod(0o600)
    return stage, manifest


def release_entry(tag=TAG, *, repo="Smethan/meshtastic-firmware"):
    asset = updates.package_asset_for_tag(tag)
    base = f"https://github.com/{repo}/releases/download/{quote(tag, safe='.-')}/"
    names = (
        asset, "compatibility.json", "SHA256SUMS", "SOURCE.txt", "copyright",
    )
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
    for value in (
            "v2.8.1", "2.8.1-wdg.1", "v2.8.1-wdg.0/../../x",
            "v02.8.1-wdg.1", "v2.08.1-wdg.1", "v2.8.01-wdg.1",
            "v2.8.1-wdg.01"):
        with pytest.raises(ValueError):
            updates.parse_meshtastic_tag(value)
    assert updates.parse_meshtastic_tag("v0.0.0-wdg.0") == (0, 0, 0, 0)


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


def test_dpkg_archive_output_is_stopped_at_the_streaming_limit(
        tmp_path, monkeypatch):
    emitter = tmp_path / "emit-large-archive"
    emitter.write_text(
        "#!/usr/bin/python3\n"
        "import sys\n"
        "while True:\n"
        "    sys.stdout.buffer.write(b'x' * 1024)\n"
        "    sys.stdout.buffer.flush()\n",
        encoding="utf-8")
    emitter.chmod(0o755)
    monkeypatch.setattr(updates, "DPKG_DEB", str(emitter))
    monkeypatch.setattr(updates, "MAX_TAR_BYTES", 4096)
    started = time.monotonic()

    with pytest.raises(ValueError, match="archive is too large"):
        updates._dpkg_tar(tmp_path / "ignored.deb", "--fsys-tarfile")

    assert time.monotonic() - started < 2.0


def test_dpkg_archive_failure_kills_decompression_process_group(
        tmp_path, monkeypatch):
    pid_file = tmp_path / "inspector-pids"
    emitter = tmp_path / "forking-archive-inspector"
    emitter.write_text(
        "#!/usr/bin/python3\n"
        "import os, signal, sys, time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    while True:\n"
        "        time.sleep(60)\n"
        f"with open({str(pid_file)!r}, 'w', encoding='ascii') as output:\n"
        "    output.write(f'{os.getpid()} {child}\\n')\n"
        "while True:\n"
        "    sys.stdout.buffer.write(b'x' * 1024)\n"
        "    sys.stdout.buffer.flush()\n",
        encoding="utf-8")
    emitter.chmod(0o755)
    monkeypatch.setattr(updates, "DPKG_DEB", str(emitter))
    monkeypatch.setattr(updates, "MAX_TAR_BYTES", 4096)

    with pytest.raises(ValueError, match="archive is too large"):
        updates._dpkg_tar(tmp_path / "ignored.deb", "--fsys-tarfile")

    parent_pid, child_pid = map(int, pid_file.read_text().split())
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not any(Path(f"/proc/{pid}").exists()
                   for pid in (parent_pid, child_pid)):
            break
        time.sleep(0.02)
    assert not Path(f"/proc/{parent_pid}").exists()
    assert not Path(f"/proc/{child_pid}").exists()


def test_tar_member_count_is_bounded_before_full_materialization():
    payload = io.BytesIO()
    names = [f"entry-{index}" for index in range(updates.MAX_TAR_MEMBERS + 1)]
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for name in names:
            member = tarfile.TarInfo(name)
            member.size = 0
            member.mode = 0o644
            member.uid = 0
            member.gid = 0
            archive.addfile(member, io.BytesIO())

    with pytest.raises(ValueError, match="too many members"):
        updates._read_tar_files(
            payload.getvalue(), allowed_files=frozenset(names),
            required_files=frozenset())


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


def test_package_rejects_writable_payload_directory(tmp_path):
    stage, _ = stage_release(tmp_path, binary_directory_mode=0o777)
    with pytest.raises(ValueError, match="directory.*0755"):
        updates.validate_prepared_release(
            stage, expected_tag=TAG, require_secure=False, check_host=False)


@pytest.mark.parametrize("control_extra", [
    "Pre-Depends: libc6\n",
    "Recommends: harmless-looking-package\n",
    "Suggests: harmless-looking-package\n",
    "Enhances: harmless-looking-package\n",
    "Conflicts: meshtasticd\n",
    "Replaces: meshtasticd\n",
    "Breaks: meshtasticd\n",
    "Provides: meshtasticd\n",
    "Built-Using: source (= 1)\n",
])
def test_package_rejects_unreviewed_control_relationship_fields(
        tmp_path, control_extra):
    stage, _ = stage_release(tmp_path, control_extra=control_extra)
    with pytest.raises(ValueError, match="unexpected Debian field"):
        updates.validate_prepared_release(
            stage, expected_tag=TAG, require_secure=False, check_host=False)


@pytest.mark.parametrize("depends", [
    DEFAULT_DEPENDS + ", curl | wget",
    DEFAULT_DEPENDS + ", curl",
    DEFAULT_DEPENDS + ", libc6:any",
    "adduser, bluez, dbus, util-linux, libc6, libgcc-s1",
    (
        "adduser (>= 1), bluez, dbus, util-linux, libc6 (>= 2.36), "
        "libgcc-s1 (>= 3.0), libstdc++6 (>= 12)"
    ),
    (
        "adduser, bluez, dbus, util-linux, libc6 (= 2.36), "
        "libgcc-s1 (>= 3.0), libstdc++6 (>= 12)"
    ),
])
def test_package_rejects_unreviewed_dependency_syntax_and_packages(
        tmp_path, depends):
    stage, _ = stage_release(tmp_path, depends=depends)
    with pytest.raises(ValueError, match="Depends"):
        updates.validate_prepared_release(
            stage, expected_tag=TAG, require_secure=False, check_host=False)


@pytest.mark.parametrize("depends", [
    DEFAULT_DEPENDS + ", libc6 [arm64]",
    DEFAULT_DEPENDS + ", ${misc:Depends}",
    DEFAULT_DEPENDS + ", libc6 <!nocheck>",
])
def test_dependency_parser_rejects_arch_profiles_and_substvars(depends):
    with pytest.raises(ValueError, match="Depends"):
        updates._validate_dependencies(depends)


def test_package_rejects_maintainer_scripts(tmp_path):
    stage, _ = stage_release(tmp_path, maintainer_script="prerm")
    with pytest.raises(ValueError, match="forbidden path|maintainer"):
        updates.validate_prepared_release(
            stage, expected_tag=TAG, require_secure=False, check_host=False)


def test_adoption_payload_must_match_every_validated_package_file(tmp_path):
    built = tmp_path / "built"
    built.mkdir()
    package, manifest = build_package(built)
    installed = tmp_path / "installed"
    subprocess.run(
        ["dpkg-deb", "--extract", str(package), str(installed)],
        check=True, capture_output=True)

    updates.validate_installed_package_payload(
        package, manifest, root=installed, require_root_ownership=False)

    service = installed / "usr/lib/systemd/system/meshtasticd-wdg.service"
    service.write_bytes(service.read_bytes() + b"# locally modified\n")
    with pytest.raises(ValueError, match="differs from the validated release"):
        updates.validate_installed_package_payload(
            package, manifest, root=installed, require_root_ownership=False)


def test_adoption_rejects_writable_installed_payload_directory(tmp_path):
    built = tmp_path / "built"
    built.mkdir()
    package, manifest = build_package(built)
    installed = tmp_path / "installed"
    subprocess.run(
        ["dpkg-deb", "--extract", str(package), str(installed)],
        check=True, capture_output=True)
    (installed / "usr/lib/meshtasticd-wdg").chmod(0o777)

    with pytest.raises(ValueError, match="directory has unsafe metadata"):
        updates.validate_installed_package_payload(
            package, manifest, root=installed, require_root_ownership=False)


@pytest.mark.parametrize(("package_options"), [
    {
        "service": updates.SAFE_SERVICE
        + b"ExecStartPre=+/bin/sh -c 'touch /root/owned'\n",
    },
    {
        "service": updates.SAFE_SERVICE.replace(
            b"User=meshtasticd\n", b"User=meshtasticd\nUser=root\n"),
    },
    {
        "tmpfiles": updates.SAFE_TMPFILES
        + b"d /etc/watchdogs-owned 0777 root root -\n",
    },
    {
        "sysusers": updates.SAFE_SYSUSERS
        + b"u watchdogs-admin 0 'Unreviewed account' /root /bin/sh\n",
    },
])
def test_package_rejects_modified_privileged_policy(tmp_path, package_options):
    stage, _ = stage_release(tmp_path, **package_options)
    with pytest.raises(ValueError, match="unreviewed privileged policy"):
        updates.validate_prepared_release(
            stage, expected_tag=TAG, require_secure=False, check_host=False)


def test_manifest_rejects_wrong_repo_api_arch_and_host(tmp_path):
    _, manifest = stage_release(tmp_path)
    for mutate, message in (
        (lambda item: item.update(repository="Smethan/firmware"), "Smethan fork"),
        (lambda item: item["wdg_api"].update(major=2), "API major"),
        (lambda item: item["package"].update(architecture="amd64"), "ARM64"),
        (lambda item: item.update(source_commit="not-a-commit"), "source commit"),
        (lambda item: item.update(source_ref="develop"), "source reference"),
        (lambda item: item.update(license="MIT"), "source license"),
    ):
        candidate = copy.deepcopy(manifest)
        mutate(candidate)
        with pytest.raises(ValueError, match=message):
            updates.validate_compatibility_manifest(candidate, expected_tag=TAG)
    with pytest.raises(RuntimeError, match="ARM64 hosts only"):
        updates.check_host_compatibility(manifest, machine="x86_64", glibc_version="2.36")
    with pytest.raises(RuntimeError, match="requires glibc"):
        updates.check_host_compatibility(manifest, machine="aarch64", glibc_version="2.35")


def test_prepare_downloads_verified_release_assets_into_private_cache(tmp_path):
    built = tmp_path / "built"
    built.mkdir()
    package, manifest = build_package(built)
    manifest_blob = json.dumps(manifest, sort_keys=True).encode()
    source_blob = (
        b"Corresponding source: "
        b"https://github.com/Smethan/meshtastic-firmware\n")
    copyright_blob = b"GPL-3.0-only\n"
    sums = (
        hashlib.sha256(manifest_blob).hexdigest() + "  compatibility.json\n"
        + manifest["package"]["sha256"] + "  " + ASSET + "\n"
        + hashlib.sha256(source_blob).hexdigest() + "  SOURCE.txt\n"
        + hashlib.sha256(copyright_blob).hexdigest() + "  copyright\n").encode()
    release = release_entry()
    base = f"https://github.com/Smethan/meshtastic-firmware/releases/download/{TAG}/"
    payloads = {
        base + "compatibility.json": manifest_blob,
        base + "SHA256SUMS": sums,
        base + ASSET: package.read_bytes(),
        base + "SOURCE.txt": source_blob,
        base + "copyright": copyright_blob,
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
