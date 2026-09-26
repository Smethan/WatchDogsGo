"""Strict release preparation for the Smethan Meshtastic daemon fork.

This module intentionally has no generic repository, URL, package-name, or
service-name inputs.  The unprivileged application may download and inspect a
release, while the installed root helper imports a protected copy of this same
module and repeats every validation before invoking dpkg.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import selectors
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

MESHTASTIC_RELEASE_REPO = "Smethan/meshtastic-firmware"
MESHTASTIC_UPSTREAM_REPO = "meshtastic/firmware"
MESHTASTIC_RELEASES_URL = (
    "https://api.github.com/repos/Smethan/meshtastic-firmware/releases"
)
MESHTASTIC_CACHE_ROOT = Path("/var/cache/watchdogs/meshtasticd-wdg")
MESHTASTIC_PACKAGE_NAME = "meshtasticd-wdg"
MESHTASTIC_ARCHITECTURE = "arm64"
MESHTASTIC_WDG_API_MAJOR = 1
MESHTASTIC_WDG_API_MINOR = 0
DPKG_DEB = "/usr/bin/dpkg-deb"
GETCONF = "/usr/bin/getconf"

_CANONICAL_UINT = r"(?:0|[1-9][0-9]*)"
TAG_RE = re.compile(
    rf"^v({_CANONICAL_UINT})\.({_CANONICAL_UINT})\."
    rf"({_CANONICAL_UINT})-wdg\.({_CANONICAL_UINT})$")
PACKAGE_VERSION_RE = re.compile(
    rf"^({_CANONICAL_UINT})\.({_CANONICAL_UINT})\."
    rf"({_CANONICAL_UINT})\+wdg({_CANONICAL_UINT})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UPSTREAM_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,79}$")
GLIBC_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?$")

COMPATIBILITY_ASSET = "compatibility.json"
CHECKSUM_ASSET = "SHA256SUMS"
SOURCE_ASSET = "SOURCE.txt"
COPYRIGHT_ASSET = "copyright"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_CHECKSUM_BYTES = 64 * 1024
MAX_NOTICE_BYTES = 64 * 1024
MAX_PACKAGE_BYTES = 96 * 1024 * 1024
MAX_TAR_BYTES = 160 * 1024 * 1024
MAX_TAR_MEMBERS = 128
MAX_DPKG_STDERR_BYTES = 16 * 1024
DPKG_INSPECTION_TIMEOUT_SECONDS = 30.0

PACKAGE_FILES = frozenset({
    "usr/lib/meshtasticd-wdg/meshtasticd",
    "usr/lib/systemd/system/meshtasticd-wdg.service",
    "usr/lib/tmpfiles.d/meshtasticd-wdg.conf",
    "usr/lib/sysusers.d/meshtasticd-wdg.conf",
    "usr/share/dbus-1/system.d/meshtasticd-wdg.conf",
    "usr/share/meshtasticd-wdg/wdg-portduino.example.yaml",
    "usr/share/doc/meshtasticd-wdg/copyright",
    "usr/share/doc/meshtasticd-wdg/UPSTREAM_BASE",
    "usr/share/doc/meshtasticd-wdg/compatibility.json",
})
PACKAGE_DIRECTORIES = frozenset(
    PurePosixPath(*parts[:index]).as_posix()
    for filename in PACKAGE_FILES
    for parts in (PurePosixPath(filename).parts,)
    for index in range(1, len(parts))
)
CONTROL_FILES = frozenset({"control", "md5sums", "postinst"})
MAINTAINER_SCRIPTS = frozenset({
    "preinst", "postinst", "prerm", "postrm", "config", "triggers",
})
SAFE_CONTROL_FIELDS = frozenset({
    "Package", "Version", "Architecture", "Maintainer", "Section",
    "Priority", "Depends", "Homepage", "Description",
})
SAFE_STATIC_DEPENDENCIES = frozenset({
    "adduser", "bluez", "dbus", "util-linux",
})
SAFE_REQUIRED_DEPENDENCIES = frozenset({
    *SAFE_STATIC_DEPENDENCIES,
    "libc6", "libgcc-s1", "liborcania2.3", "libstdc++6",
    "libulfius2.7t64",
})
# ``dpkg-shlibdeps`` emits only direct ELF dependencies.  Keep this closed to
# the runtime packages supplied by the pinned Debian Trixie build image so a
# checksummed .deb cannot make apt install an unrelated package (and thereby
# execute that package's maintainer scripts) as a side effect of an update.
SAFE_DEPENDENCY_PACKAGES = frozenset({
    *SAFE_REQUIRED_DEPENDENCIES,
    "libacl1", "libbluetooth3", "libbsd0", "libgpiod3", "libi2c0",
    "libjsoncpp26", "liborcania2.3", "libsdbus-c++2",
    "libsdl2-2.0-0", "libssl3t64", "libsystemd0", "libulfius2.7t64",
    "libusb-1.0-0", "libuv1t64", "libyaml-cpp0.8",
})
DEPENDENCY_RE = re.compile(
    r"^([a-z0-9][a-z0-9+.-]*)(?: \((>=) "
    r"([0-9A-Za-z.+:~_-]+)\))?$")
SAFE_CONTROL_STATIC = {
    "Maintainer": "Smethan <noreply@github.com>",
    "Section": "net",
    "Priority": "optional",
    "Homepage": "https://github.com/Smethan/meshtastic-firmware",
    "Description": (
        "Meshtastic daemon with WatchDogsGo local API and BlueZ phone transport\n"
        "Side-by-side Meshtastic Portduino build for the uConsole AIO v2 radio."
    ),
}
SAFE_POSTINST = b'''#!/bin/sh
set -e

case "${1-}" in
configure | reconfigure)
\tif command -v systemd-sysusers >/dev/null 2>&1; then
\t\tsystemd-sysusers /usr/lib/sysusers.d/meshtasticd-wdg.conf
\tfi
\tif command -v systemd-tmpfiles >/dev/null 2>&1; then
\t\tsystemd-tmpfiles --create /usr/lib/tmpfiles.d/meshtasticd-wdg.conf
\tfi
\tpolicy=/etc/meshtasticd/wdg-portduino.yaml
\tif [ -e "$policy" ] || [ -L "$policy" ]; then
\t\tif [ ! -f "$policy" ] || [ -L "$policy" ]; then
\t\t\techo "Unsafe Meshtastic WDG policy: $policy" >&2
\t\t\texit 1
\t\tfi
\t\tchown root:meshtasticd "$policy"
\t\tchmod 0640 "$policy"
\tfi
\tif command -v systemctl >/dev/null 2>&1; then
\t\tsystemctl daemon-reload >/dev/null 2>&1 || true
\tfi
\t;;
abort-upgrade | abort-remove | abort-deconfigure) ;;
*) ;;
esac

exit 0
'''

# These byte-for-byte package interface constants are duplicated in the GPL
# firmware repository and are available under MIT OR GPL-3.0-only. Keeping the
# privileged unit/account/path policy exact prevents a checksummed fork release
# from silently adding another root-capable command or host account.
SAFE_SERVICE = b'''[Unit]
Description=Meshtastic daemon for WatchDogsGo
Wants=bluetooth.service
After=bluetooth.service meshtasticd.service
Conflicts=meshtasticd.service
StartLimitIntervalSec=200
StartLimitBurst=5

[Service]
Type=simple
User=meshtasticd
Group=meshtasticd
SupplementaryGroups=spi gpio watchdogs
RuntimeDirectory=meshtasticd
RuntimeDirectoryMode=0770
UMask=0007
Environment=MESHTASTIC_WDG_POLICY=/etc/meshtasticd/wdg-portduino.yaml
ExecStartPre=/usr/bin/test -r /etc/meshtasticd/config.yaml
ExecStart=/usr/bin/flock -n -E 75 /run/lock/watchdogs/aio-sx1262.lock /usr/lib/meshtasticd-wdg/meshtasticd --config=/etc/meshtasticd/config.yaml --fsdir=/var/lib/meshtasticd
Restart=on-failure
RestartSec=3
RestartPreventExitStatus=75
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
'''
SAFE_TMPFILES = b'''d /run/lock/watchdogs 2750 root watchdogs -
f /run/lock/watchdogs/aio-sx1262.lock 0660 root watchdogs -
d /run/meshtasticd 0770 meshtasticd meshtasticd -
'''
SAFE_SYSUSERS = b'''g watchdogs -
g spi -
g gpio -
u meshtasticd - "Meshtastic daemon" /var/lib/meshtasticd /usr/sbin/nologin
m meshtasticd watchdogs
m meshtasticd spi
m meshtasticd gpio
'''


@dataclass(frozen=True)
class PreparedMeshtasticRelease:
    """A fully validated release staged under the closed root cache."""

    tag: str
    package_version: str
    directory: Path
    package_path: Path
    manifest: dict[str, Any]


def parse_meshtastic_tag(tag: str) -> tuple[int, int, int, int]:
    match = TAG_RE.fullmatch(str(tag))
    if not match:
        raise ValueError("Expected Meshtastic tag vX.Y.Z-wdg.N")
    return tuple(int(value) for value in match.groups())  # type: ignore[return-value]


def package_version_for_tag(tag: str) -> str:
    major, minor, patch, revision = parse_meshtastic_tag(tag)
    return f"{major}.{minor}.{patch}+wdg{revision}"


def package_asset_for_tag(tag: str) -> str:
    version = package_version_for_tag(tag)
    return f"meshtasticd-wdg_{version}_arm64.deb"


def _version_tuple(value: str, pattern: re.Pattern[str]) -> tuple[int, ...]:
    match = pattern.fullmatch(value)
    if not match:
        raise ValueError("Invalid version: " + value)
    return tuple(int(item or 0) for item in match.groups())


def _require_plain_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(label + " must be an integer")
    return value


def validate_compatibility_manifest(
    manifest: Any,
    *,
    expected_tag: str | None = None,
) -> dict[str, Any]:
    """Validate the signed-by-checksum compatibility contract.

    Unknown top-level keys are tolerated so a release can add descriptive
    metadata without making an older WDG unsafe.  Every field that controls
    installation is exact and closed.
    """
    if not isinstance(manifest, dict):
        raise ValueError("Meshtastic compatibility manifest must be an object")
    if _require_plain_int(manifest.get("format"), "manifest format") != 1:
        raise ValueError("Unsupported Meshtastic compatibility format")
    if manifest.get("repository") != MESHTASTIC_RELEASE_REPO:
        raise ValueError("Compatibility manifest repository is not the Smethan fork")
    tag = manifest.get("tag")
    if not isinstance(tag, str):
        raise ValueError("Compatibility manifest tag is missing")
    parse_meshtastic_tag(tag)
    if expected_tag is not None and tag != expected_tag:
        raise ValueError("Compatibility manifest tag does not match the release")
    if manifest.get("upstream_repository") != MESHTASTIC_UPSTREAM_REPO:
        raise ValueError("Compatibility manifest has the wrong upstream repository")
    upstream_tag = manifest.get("upstream_tag")
    if not isinstance(upstream_tag, str) or not UPSTREAM_TAG_RE.fullmatch(upstream_tag):
        raise ValueError("Compatibility manifest has an invalid upstream tag")
    upstream_commit = manifest.get("upstream_commit")
    if (not isinstance(upstream_commit, str)
            or not re.fullmatch(r"[0-9a-f]{40}", upstream_commit)):
        raise ValueError("Compatibility manifest has an invalid upstream commit")
    source_commit = manifest.get("source_commit")
    if (not isinstance(source_commit, str)
            or not re.fullmatch(r"[0-9a-f]{40}", source_commit)):
        raise ValueError("Compatibility manifest has an invalid source commit")
    if manifest.get("source_url") != (
            "https://github.com/Smethan/meshtastic-firmware/tree/"
            + source_commit):
        raise ValueError("Compatibility manifest has an invalid source URL")
    if manifest.get("source_ref") != tag:
        raise ValueError("Compatibility manifest source reference is not its tag")
    if manifest.get("license") != "GPL-3.0-only":
        raise ValueError("Compatibility manifest has the wrong source license")

    api = manifest.get("wdg_api")
    if not isinstance(api, dict):
        raise ValueError("Compatibility manifest is missing the WDG API version")
    if (_require_plain_int(api.get("major"), "WDG API major")
            != MESHTASTIC_WDG_API_MAJOR):
        raise ValueError("Release requires an incompatible WDG API major version")
    minor = _require_plain_int(api.get("minor"), "WDG API minor")
    if minor < MESHTASTIC_WDG_API_MINOR:
        raise ValueError("Release provides an older WDG API revision")

    package = manifest.get("package")
    if not isinstance(package, dict):
        raise ValueError("Compatibility manifest is missing package metadata")
    expected_version = package_version_for_tag(tag)
    expected_asset = package_asset_for_tag(tag)
    if package.get("name") != MESHTASTIC_PACKAGE_NAME:
        raise ValueError("Compatibility manifest has the wrong package name")
    version = package.get("version")
    if version != expected_version or not PACKAGE_VERSION_RE.fullmatch(str(version)):
        raise ValueError("Package version does not match the release tag")
    if package.get("architecture") != MESHTASTIC_ARCHITECTURE:
        raise ValueError("Meshtastic release is not the ARM64 package")
    if package.get("asset") != expected_asset:
        raise ValueError("Package asset name does not match the release tag")
    size = _require_plain_int(package.get("size"), "package size")
    if size <= 0 or size > MAX_PACKAGE_BYTES:
        raise ValueError("Meshtastic package size is outside the safe limit")
    digest = package.get("sha256")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise ValueError("Meshtastic package checksum is invalid")

    minimum_glibc = manifest.get("minimum_glibc")
    if not isinstance(minimum_glibc, str) or not GLIBC_RE.fullmatch(minimum_glibc):
        raise ValueError("Compatibility manifest has an invalid minimum glibc")
    built_glibc = manifest.get("built_glibc_requirement")
    if built_glibc is not None:
        if not isinstance(built_glibc, str) or not GLIBC_RE.fullmatch(built_glibc):
            raise ValueError("Compatibility manifest has an invalid built glibc requirement")
        if _glibc_tuple(built_glibc) > _glibc_tuple(minimum_glibc):
            raise ValueError("Built daemon requires a newer glibc than the release declares")
    return manifest


def _glibc_tuple(version: str) -> tuple[int, int, int]:
    values = _version_tuple(version, GLIBC_RE)
    return (values + (0, 0, 0))[:3]  # type: ignore[return-value]


def host_glibc_version() -> str:
    libc, version = platform.libc_ver()
    if libc.lower() == "glibc" and GLIBC_RE.fullmatch(version):
        return version
    result = subprocess.run(
        [GETCONF, "GNU_LIBC_VERSION"], capture_output=True, text=True,
        timeout=5, check=False)
    match = re.search(r"glibc\s+(\d+\.\d+(?:\.\d+)?)", result.stdout or "")
    if result.returncode or not match:
        raise RuntimeError("Could not determine the host glibc version")
    return match.group(1)


def check_host_compatibility(
    manifest: dict[str, Any],
    *,
    machine: str | None = None,
    glibc_version: str | None = None,
) -> None:
    validate_compatibility_manifest(manifest)
    machine = (machine or platform.machine()).lower()
    if machine not in {"aarch64", "arm64"}:
        raise RuntimeError("meshtasticd-wdg releases support ARM64 hosts only")
    current = glibc_version or host_glibc_version()
    if _glibc_tuple(current) < _glibc_tuple(manifest["minimum_glibc"]):
        raise RuntimeError(
            f"Release requires glibc {manifest['minimum_glibc']} or newer; "
            f"this host has {current}")


def _asset_base(tag: str) -> str:
    # Tags and asset names have already passed strict ASCII regexes.  Quoting
    # still makes the URL construction unambiguous for the '+' in deb versions.
    return (
        "https://github.com/Smethan/meshtastic-firmware/releases/download/"
        + quote(tag, safe=".-") + "/")


def _asset_map(release: dict[str, Any]) -> dict[str, str]:
    tag = release.get("tag_name")
    if not isinstance(tag, str):
        raise ValueError("Release has no tag")
    parse_meshtastic_tag(tag)
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ValueError("Release has no assets")
    result: dict[str, str] = {}
    base = _asset_base(tag)
    for entry in assets:
        if not isinstance(entry, dict):
            raise ValueError("Release contains an invalid asset entry")
        name = entry.get("name")
        url = entry.get("browser_download_url")
        if not isinstance(name, str) or not isinstance(url, str):
            raise ValueError("Release asset is missing its name or URL")
        if name in result:
            raise ValueError("Release contains a duplicate asset: " + name)
        if url != base + quote(name, safe="._+-"):
            raise ValueError("Release asset is not hosted by the configured Smethan fork")
        result[name] = url
    return result


def validate_release_entry(release: Any) -> dict[str, Any]:
    if not isinstance(release, dict):
        raise ValueError("Invalid GitHub release entry")
    if release.get("draft") or release.get("prerelease"):
        raise ValueError("Expected a published stable Meshtastic release")
    tag = release.get("tag_name")
    if not isinstance(tag, str):
        raise ValueError("Release has no tag")
    parse_meshtastic_tag(tag)
    assets = _asset_map(release)
    package_assets = [name for name in assets
                      if re.fullmatch(r"meshtasticd-wdg_.+_arm64\.deb", name)]
    required = {
        COMPATIBILITY_ASSET, CHECKSUM_ASSET, SOURCE_ASSET, COPYRIGHT_ASSET,
    }
    if set(assets) != required | set(package_assets) or len(package_assets) != 1:
        raise ValueError(
            "Release must contain one package, compatibility.json, "
            "SHA256SUMS, SOURCE.txt, and copyright")
    if package_assets[0] != package_asset_for_tag(tag):
        raise ValueError("Release package asset does not match its tag")
    return release


def download(url: str, limit: int) -> bytes:
    request = Request(url, headers={"User-Agent": "WatchDogsGo-Meshtastic-Updater"})
    with urlopen(request, timeout=30) as response:
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > limit:
            raise ValueError("Meshtastic release asset is too large")
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("Meshtastic release asset is too large")
    return payload


def meshtastic_releases(
    *, downloader: Callable[[str, int], bytes] = download,
) -> list[dict[str, Any]]:
    raw = downloader(MESHTASTIC_RELEASES_URL + "?per_page=50", 2 * 1024 * 1024)
    try:
        values = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("GitHub returned invalid Meshtastic release metadata") from exc
    if not isinstance(values, list):
        raise ValueError("GitHub returned an invalid Meshtastic release list")
    accepted: dict[str, dict[str, Any]] = {}
    for value in values:
        try:
            release = validate_release_entry(value)
        except (TypeError, ValueError):
            continue
        accepted.setdefault(release["tag_name"], release)
    return sorted(
        accepted.values(), key=lambda item: parse_meshtastic_tag(item["tag_name"]),
        reverse=True)


def _parse_checksums(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("SHA256SUMS must be ASCII") from exc
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._+-]+)", line)
        if not match:
            raise ValueError("SHA256SUMS contains an invalid line")
        digest, name = match.groups()
        if name in result:
            raise ValueError("SHA256SUMS contains a duplicate filename")
        result[name] = digest
    return result


def _safe_tar_name(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts:
        raise ValueError("Debian package contains an unsafe archive path")
    return path.as_posix().rstrip("/")


def _dpkg_tar(
    package_path: Path,
    option: str,
) -> bytes:
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    payload = bytearray()
    stderr = bytearray()
    completed = False
    deadline = time.monotonic() + DPKG_INSPECTION_TIMEOUT_SECONDS
    try:
        process = subprocess.Popen(
            [DPKG_DEB, option, str(package_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True)
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, 30)
            events = selector.select(min(0.25, remaining))
            if not events:
                continue
            for key, _mask in events:
                chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    room = MAX_TAR_BYTES + 1 - len(payload)
                    if room > 0:
                        payload.extend(chunk[:room])
                    if len(payload) > MAX_TAR_BYTES:
                        raise ValueError("Debian package archive is too large")
                else:
                    room = MAX_DPKG_STDERR_BYTES - len(stderr)
                    if room > 0:
                        stderr.extend(chunk[:room])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, 30)
        returncode = process.wait(timeout=remaining)
        if returncode:
            detail = bytes(stderr).decode("utf-8", "replace").strip()
            raise ValueError(
                "Could not inspect Debian package: "
                + (detail[:200] or f"dpkg-deb exited {returncode}"))
        completed = True
        return bytes(payload)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Debian package inspection timed out") from exc
    finally:
        selector.close()
        if process is not None:
            # dpkg-deb can delegate decompression to children.  Keep the
            # inspector in a private process group and tear down that entire
            # group on failure, even if the leader exited while a child kept
            # an output pipe open.
            if not completed:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def _read_tar_files(
    payload: bytes,
    *,
    allowed_files: frozenset[str],
    required_files: frozenset[str],
    required_directories: frozenset[str] = frozenset(),
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    directories: set[str] = set()
    total = 0
    allowed_directories = {""}
    for filename in allowed_files:
        parts = PurePosixPath(filename).parts
        allowed_directories.update(
            PurePosixPath(*parts[:index]).as_posix()
            for index in range(1, len(parts)))
    try:
        archive = tarfile.open(  # noqa: SIM115 - TarError is translated below
            fileobj=io.BytesIO(payload), mode="r:")
    except tarfile.TarError as exc:
        raise ValueError("Debian package contains an invalid tar archive") from exc
    with archive:
        for member_count, member in enumerate(archive, start=1):
            if member_count > MAX_TAR_MEMBERS:
                raise ValueError("Debian package archive has too many members")
            raw_name = member.name
            if raw_name in {".", "./"} and member.isdir():
                if (member.uid != 0 or member.gid != 0
                        or member.mode & 0o7777 != 0o755):
                    raise ValueError(
                        "Debian package directories must be root-owned mode 0755")
                continue
            name = _safe_tar_name(raw_name)
            if member.isdir():
                if name not in allowed_directories:
                    raise ValueError(
                        "Debian package contains a forbidden path: /" + name)
                if name in directories:
                    raise ValueError(
                        "Debian package contains a duplicate path: /" + name)
                if (member.uid != 0 or member.gid != 0
                        or member.mode & 0o7777 != 0o755):
                    raise ValueError(
                        "Debian package directory /" + name
                        + " must be root-owned mode 0755")
                directories.add(name)
                continue
            if (not member.isfile() or member.issym() or member.islnk()
                    or name not in allowed_files):
                raise ValueError("Debian package contains a forbidden path: /" + name)
            if name in files:
                raise ValueError("Debian package contains a duplicate path: /" + name)
            if member.uid != 0 or member.gid != 0:
                raise ValueError("Debian package files must be owned by root")
            total += member.size
            if total > MAX_TAR_BYTES:
                raise ValueError("Debian package contents are too large")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("Could not read Debian package member: /" + name)
            files[name] = stream.read(member.size + 1)
            if len(files[name]) != member.size:
                raise ValueError("Debian package member is truncated: /" + name)
            mode = member.mode & 0o7777
            expected_mode = (
                0o755 if name.endswith("/meshtasticd") or name == "postinst"
                else 0o644)
            if mode != expected_mode:
                raise ValueError(
                    f"Debian package member /{name} has mode {mode:o}; "
                    f"expected {expected_mode:o}")
    missing = required_files - set(files)
    if missing:
        raise ValueError("Debian package is missing: /" + min(missing))
    missing_directories = required_directories - directories
    if missing_directories:
        raise ValueError(
            "Debian package is missing directory metadata: /"
            + min(missing_directories))
    return files


def _dpkg_field(
    package_path: Path,
    field: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    result = runner(
        [DPKG_DEB, "--field", str(package_path), field],
        capture_output=True, text=True, timeout=10, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout or "dpkg-deb failed").strip()
        raise ValueError("Could not inspect Debian package metadata: " + detail[:200])
    return (result.stdout or "").strip()


def _parse_control_stanza(payload: bytes) -> dict[str, str]:
    """Parse one closed Debian binary-package control stanza.

    Continuations are accepted only for Description.  Relationship fields
    must stay on one line so alternatives, architecture restrictions, build
    profiles, and hidden continuation entries cannot bypass dependency review.
    """
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ValueError("Debian control metadata must be UTF-8") from exc
    if "\x00" in text:
        raise ValueError("Debian control metadata contains NUL bytes")

    fields: dict[str, str] = {}
    current: str | None = None
    saw_blank = False
    for raw_line in text.splitlines():
        if not raw_line:
            saw_blank = True
            current = None
            continue
        if saw_blank:
            raise ValueError("Debian control metadata must contain one stanza")
        if raw_line[0].isspace():
            if current != "Description" or not raw_line.startswith(" "):
                raise ValueError(
                    "Debian control metadata has an unsupported continuation")
            fields[current] += "\n" + raw_line[1:]
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9-]*):[ \t]*(.*)", raw_line)
        if match is None:
            raise ValueError("Debian control metadata contains an invalid field")
        name, value = match.groups()
        if name not in SAFE_CONTROL_FIELDS:
            raise ValueError("meshtasticd-wdg contains an unexpected Debian field: " + name)
        if name in fields:
            raise ValueError("Debian control metadata contains a duplicate field: " + name)
        fields[name] = value
        current = name

    if set(fields) != SAFE_CONTROL_FIELDS:
        missing = SAFE_CONTROL_FIELDS - set(fields)
        raise ValueError(
            "Debian control metadata is missing the reviewed field: "
            + min(missing))
    return fields


def _validate_dependencies(value: str) -> None:
    dependencies = [item.strip() for item in value.split(",")]
    if not dependencies or any(not item for item in dependencies):
        raise ValueError("Debian Depends metadata is malformed")
    seen: set[str] = set()
    for dependency in dependencies:
        # The strict expression deliberately rejects alternatives (``|``),
        # architecture qualifiers/restrictions, build profiles, substvars,
        # and package-name qualifiers such as ``:any``.
        match = DEPENDENCY_RE.fullmatch(dependency)
        if match is None:
            raise ValueError("Debian Depends contains unsupported syntax")
        name = match.group(1)
        if name not in SAFE_DEPENDENCY_PACKAGES:
            raise ValueError("Debian Depends contains an unreviewed package: " + name)
        if name in seen:
            raise ValueError("Debian Depends contains a duplicate package: " + name)
        relation = match.group(2)
        if ((name in SAFE_STATIC_DEPENDENCIES and relation is not None)
                or (name not in SAFE_STATIC_DEPENDENCIES and relation != ">=")):
            raise ValueError(
                "Debian Depends does not match the reviewed version contract: "
                + name)
        seen.add(name)
    missing = SAFE_REQUIRED_DEPENDENCIES - seen
    if missing:
        raise ValueError(
            "Debian Depends is missing the required package: " + min(missing))


def _package_data(
    package_path: Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, bytes]:
    return _read_tar_files(
        _dpkg_tar(package_path, "--fsys-tarfile"),
        allowed_files=PACKAGE_FILES,
        required_files=PACKAGE_FILES,
        required_directories=PACKAGE_DIRECTORIES,
    )


def validate_debian_package(
    package_path: Path,
    manifest: dict[str, Any],
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Inspect a .deb without executing or extracting any package content."""
    package_path = Path(package_path)
    package = manifest["package"]
    try:
        info = package_path.stat()
    except OSError as exc:
        raise ValueError("Meshtastic package is missing") from exc
    if not package_path.is_file() or package_path.is_symlink():
        raise ValueError("Meshtastic package must be a regular file")
    if info.st_size != package["size"]:
        raise ValueError("Meshtastic package size does not match compatibility.json")
    digest = hashlib.sha256(package_path.read_bytes()).hexdigest()
    if digest != package["sha256"]:
        raise ValueError("Meshtastic package checksum does not match compatibility.json")

    control = _read_tar_files(
        _dpkg_tar(package_path, "--ctrl-tarfile"),
        allowed_files=CONTROL_FILES,
        required_files=frozenset({"control"}),
    )
    fields = _parse_control_stanza(control["control"])
    expected_fields = {
        "Package": MESHTASTIC_PACKAGE_NAME,
        "Version": package["version"],
        "Architecture": MESHTASTIC_ARCHITECTURE,
        **SAFE_CONTROL_STATIC,
    }
    for field, expected in expected_fields.items():
        if fields[field] != expected:
            raise ValueError(
                f"Debian {field} metadata does not match the reviewed contract")
    _validate_dependencies(fields["Depends"])

    scripts = MAINTAINER_SCRIPTS & set(control)
    if scripts - {"postinst"}:
        raise ValueError("meshtasticd-wdg contains a forbidden maintainer script")
    if control.get("postinst") != SAFE_POSTINST:
        raise ValueError("meshtasticd-wdg postinst is not the reviewed no-start script")
    data = _package_data(package_path, runner=runner)

    exact_policy_files = {
        "usr/lib/systemd/system/meshtasticd-wdg.service": SAFE_SERVICE,
        "usr/lib/tmpfiles.d/meshtasticd-wdg.conf": SAFE_TMPFILES,
        "usr/lib/sysusers.d/meshtasticd-wdg.conf": SAFE_SYSUSERS,
    }
    for name, expected in exact_policy_files.items():
        if data[name] != expected:
            raise ValueError(
                "meshtasticd-wdg contains an unreviewed privileged policy: /"
                + name)

    binary = data["usr/lib/meshtasticd-wdg/meshtasticd"]
    if (len(binary) < 20 or binary[:4] != b"\x7fELF" or binary[4] != 2
            or binary[5] != 1
            or int.from_bytes(binary[18:20], "little") != 183):
        raise ValueError("meshtasticd-wdg binary is not a 64-bit ARM ELF")

    try:
        embedded = json.loads(data["usr/share/doc/meshtasticd-wdg/compatibility.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Package contains invalid embedded compatibility.json") from exc
    # The external, checksummed manifest is authoritative for the .deb size
    # and digest.  Its embedded copy cannot contain those two values without a
    # cryptographic self-reference, so it must match after omitting exactly
    # those fields from both sides.
    if not isinstance(embedded, dict) or not isinstance(embedded.get("package"), dict):
        raise ValueError("Package contains invalid embedded compatibility metadata")
    if ({"size", "sha256"} & set(embedded["package"])):
        raise ValueError("Embedded compatibility metadata contains self-referential fields")
    embedded = json.loads(json.dumps(embedded))
    if embedded.pop("artifact_digest_source", None) != "GitHub release compatibility.json":
        raise ValueError("Embedded compatibility metadata has no authoritative digest source")
    comparable = json.loads(json.dumps(manifest))
    comparable["package"].pop("size", None)
    comparable["package"].pop("sha256", None)
    comparable.pop("built_glibc_requirement", None)
    if embedded != comparable:
        raise ValueError("Package compatibility.json differs from the release manifest")

    upstream = data["usr/share/doc/meshtasticd-wdg/UPSTREAM_BASE"].decode(
        "utf-8", "strict")
    if (manifest["upstream_tag"] not in upstream
            or manifest["upstream_commit"] not in upstream):
        raise ValueError("Package UPSTREAM_BASE does not match compatibility.json")

    try:
        policy = ET.fromstring(
            data["usr/share/dbus-1/system.d/meshtasticd-wdg.conf"])
    except (ET.ParseError, UnicodeDecodeError) as exc:
        raise ValueError("Package contains an invalid BlueZ D-Bus policy") from exc
    children = list(policy)
    if (policy.tag != "busconfig" or len(children) != 1
            or children[0].tag != "policy"
            or children[0].attrib != {"user": "meshtasticd"}):
        raise ValueError("BlueZ D-Bus policy grants permissions beyond meshtasticd")
    grants = list(children[0])
    if (len(grants) != 1 or grants[0].tag != "allow"
            or grants[0].attrib != {"send_destination": "org.bluez"}):
        raise ValueError("BlueZ D-Bus policy must grant meshtasticd access to org.bluez")


def validate_installed_package_payload(
    package_path: Path,
    manifest: dict[str, Any],
    *,
    root: Path = Path("/"),
    runner: Callable[..., Any] = subprocess.run,
    require_root_ownership: bool = True,
) -> None:
    """Prove installed package files are byte-identical to one validated .deb.

    This is the first-migration adoption boundary.  Matching the dpkg version
    is insufficient because a locally rebuilt package can reuse that version.
    Open every fixed payload path without following symlinks, verify metadata,
    and compare the bytes that were already accepted by the package validator.
    """
    validate_debian_package(package_path, manifest, runner=runner)
    data = _package_data(Path(package_path), runner=runner)
    root = Path(root)
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise ValueError("Installed package root is unavailable") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("Installed package root must be a real directory")

    for name in sorted(
            PACKAGE_DIRECTORIES,
            key=lambda value: (len(PurePosixPath(value).parts), value)):
        target = root / name
        flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_DIRECTORY", 0))
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(target, flags)
        except OSError as exc:
            raise ValueError(
                "Installed package directory is missing or unsafe: /" + name
            ) from exc
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISDIR(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != 0o755
                    or (require_root_ownership
                        and (info.st_uid != 0 or info.st_gid != 0))):
                raise ValueError(
                    "Installed package directory has unsafe metadata: /" + name)
        finally:
            os.close(descriptor)

    for name, expected in data.items():
        target = root / name
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(target, flags)
        except OSError as exc:
            raise ValueError("Installed package payload is missing or unsafe: /" + name) from exc
        try:
            info = os.fstat(descriptor)
            expected_mode = 0o755 if name.endswith("/meshtasticd") else 0o644
            if (not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != expected_mode
                    or (require_root_ownership
                        and (info.st_uid != 0 or info.st_gid != 0))):
                raise ValueError(
                    "Installed package payload has unsafe metadata: /" + name)
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                actual = stream.read(len(expected) + 1)
            if actual != expected:
                raise ValueError(
                    "Installed package payload differs from the validated release: /"
                    + name)
        finally:
            os.close(descriptor)


def _load_json(payload: bytes, label: str) -> Any:
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(label + " is not valid JSON") from exc


def _secure_cache_directory(path: Path, *, require_root: bool) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("Meshtastic cache root must be a real directory")
    info = path.stat()
    if require_root and (info.st_uid != 0 or info.st_gid != 0):
        raise RuntimeError("Meshtastic cache must be owned by root")
    if info.st_mode & 0o077:
        raise RuntimeError("Meshtastic cache must be accessible only by root")


def validate_prepared_release(
    directory: Path,
    *,
    expected_tag: str,
    require_secure: bool = True,
    check_host: bool = True,
    machine: str | None = None,
    glibc_version: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> PreparedMeshtasticRelease:
    directory = Path(directory)
    parse_meshtastic_tag(expected_tag)
    if directory.name != expected_tag or directory.is_symlink() or not directory.is_dir():
        raise ValueError("Prepared Meshtastic release has an invalid cache directory")
    expected_files = {
        COMPATIBILITY_ASSET,
        CHECKSUM_ASSET,
        SOURCE_ASSET,
        COPYRIGHT_ASSET,
        package_asset_for_tag(expected_tag),
    }
    actual = {path.name for path in directory.iterdir()}
    if actual != expected_files or any(path.is_symlink() or not path.is_file()
                                       for path in directory.iterdir()):
        raise ValueError("Prepared Meshtastic cache contains unexpected files")
    if require_secure:
        for path in (directory, *directory.iterdir()):
            info = path.stat()
            if info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o077:
                raise RuntimeError("Prepared Meshtastic cache must be root-owned and private")

    manifest_blob = (directory / COMPATIBILITY_ASSET).read_bytes()
    if len(manifest_blob) > MAX_MANIFEST_BYTES:
        raise ValueError("compatibility.json is too large")
    manifest = validate_compatibility_manifest(
        _load_json(manifest_blob, COMPATIBILITY_ASSET), expected_tag=expected_tag)
    if check_host:
        check_host_compatibility(
            manifest, machine=machine, glibc_version=glibc_version)
    checksums_blob = (directory / CHECKSUM_ASSET).read_bytes()
    if len(checksums_blob) > MAX_CHECKSUM_BYTES:
        raise ValueError("SHA256SUMS is too large")
    for notice in (SOURCE_ASSET, COPYRIGHT_ASSET):
        size = (directory / notice).stat().st_size
        if size <= 0 or size > MAX_NOTICE_BYTES:
            raise ValueError(notice + " is empty or too large")
    checksums = _parse_checksums(checksums_blob)
    package_name = manifest["package"]["asset"]
    package_path = directory / package_name
    checksummed_files = {
        COMPATIBILITY_ASSET: directory / COMPATIBILITY_ASSET,
        package_name: package_path,
        SOURCE_ASSET: directory / SOURCE_ASSET,
        COPYRIGHT_ASSET: directory / COPYRIGHT_ASSET,
    }
    if set(checksums) != set(checksummed_files):
        raise ValueError(
            "SHA256SUMS must cover exactly the manifest, package, and source notices")
    for name, path in checksummed_files.items():
        if checksums[name] != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError(name + " checksum mismatch")
    validate_debian_package(package_path, manifest, runner=runner)
    return PreparedMeshtasticRelease(
        tag=expected_tag,
        package_version=manifest["package"]["version"],
        directory=directory,
        package_path=package_path,
        manifest=manifest,
    )


def prepare_meshtastic_release(
    *,
    cache_root: Path = MESHTASTIC_CACHE_ROOT,
    release: dict[str, Any] | None = None,
    downloader: Callable[[str, int], bytes] = download,
    require_root: bool = True,
    check_host: bool = True,
    machine: str | None = None,
    glibc_version: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> PreparedMeshtasticRelease:
    """Download every required immutable asset and atomically stage a release."""
    if release is None:
        releases = meshtastic_releases(downloader=downloader)
        if not releases:
            raise RuntimeError("No compatible Smethan Meshtastic release was found")
        release = releases[0]
    release = validate_release_entry(release)
    tag = release["tag_name"]
    assets = _asset_map(release)
    cache_root = Path(cache_root)
    _secure_cache_directory(cache_root, require_root=require_root)

    manifest_blob = downloader(assets[COMPATIBILITY_ASSET], MAX_MANIFEST_BYTES)
    manifest = validate_compatibility_manifest(
        _load_json(manifest_blob, COMPATIBILITY_ASSET), expected_tag=tag)
    if check_host:
        check_host_compatibility(
            manifest, machine=machine, glibc_version=glibc_version)
    package_name = manifest["package"]["asset"]
    if package_name not in assets:
        raise ValueError("Release is missing the package named by compatibility.json")
    sums_blob = downloader(assets[CHECKSUM_ASSET], MAX_CHECKSUM_BYTES)
    package_blob = downloader(assets[package_name], MAX_PACKAGE_BYTES)
    source_blob = downloader(assets[SOURCE_ASSET], MAX_NOTICE_BYTES)
    copyright_blob = downloader(assets[COPYRIGHT_ASSET], MAX_NOTICE_BYTES)
    if len(package_blob) != manifest["package"]["size"]:
        raise ValueError("Downloaded package size does not match compatibility.json")
    if hashlib.sha256(package_blob).hexdigest() != manifest["package"]["sha256"]:
        raise ValueError("Downloaded package checksum does not match compatibility.json")
    sums = _parse_checksums(sums_blob)
    payloads = {
        COMPATIBILITY_ASSET: manifest_blob,
        package_name: package_blob,
        SOURCE_ASSET: source_blob,
        COPYRIGHT_ASSET: copyright_blob,
    }
    if set(sums) != set(payloads):
        raise ValueError(
            "SHA256SUMS must cover exactly the manifest, package, and source notices")
    for name, payload in payloads.items():
        if sums[name] != hashlib.sha256(payload).hexdigest():
            raise ValueError("Downloaded " + name + " checksum mismatch")

    temp_dir = Path(tempfile.mkdtemp(prefix=".prepare-", dir=cache_root))
    os.chmod(temp_dir, 0o700)
    try:
        payloads[CHECKSUM_ASSET] = sums_blob
        for name, payload in payloads.items():
            path = temp_dir / name
            path.write_bytes(payload)
            os.chmod(path, 0o600)
        # The final directory name is part of the helper's closed interface.
        candidate = cache_root / tag
        if candidate.exists():
            cached = validate_prepared_release(
                candidate, expected_tag=tag, require_secure=require_root,
                check_host=check_host, machine=machine,
                glibc_version=glibc_version, runner=runner)
            if (cached.manifest != manifest
                    or hashlib.sha256(cached.package_path.read_bytes()).hexdigest()
                    != sums[package_name]):
                raise ValueError(
                    "Cached Meshtastic release differs from the published assets")
            return cached
        os.replace(temp_dir, candidate)
        temp_dir = candidate
        return validate_prepared_release(
            candidate, expected_tag=tag, require_secure=require_root,
            check_host=check_host, machine=machine,
            glibc_version=glibc_version, runner=runner)
    finally:
        if temp_dir.exists() and temp_dir.name.startswith(".prepare-"):
            for child in temp_dir.iterdir():
                child.unlink()
            temp_dir.rmdir()
