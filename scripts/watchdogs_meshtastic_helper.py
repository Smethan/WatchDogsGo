#!/usr/bin/python3
"""Privileged, argument-allowlisted Meshtastic package/service helper.

Installed as ``/usr/local/libexec/watchdogs-meshtastic`` by setup.sh.  It never
accepts a URL, package path, command, service name, executable path, backup
path, or cache path from its caller.
"""

from __future__ import annotations

import fcntl
import grp
import hashlib
import importlib.util
import json
import os
import pwd
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

HELPER_VERSION = 6
TAG_RE = re.compile(
    r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)-wdg\.(0|[1-9][0-9]*)$")
TARGET_SERVICES = {
    "wdg": "meshtasticd-wdg.service",
    "stock": "meshtasticd.service",
}
SERVICE_ACTIONS = frozenset({"start", "stop", "enable", "disable"})
PACKAGE_NAME = "meshtasticd-wdg"
CACHE_ROOT = Path("/var/cache/watchdogs/meshtasticd-wdg")
INSTALLED_CACHE = CACHE_ROOT / "installed"
FIRST_INSTALL_INBOX = CACHE_ROOT / "first-install-inbox"
BACKUP_ROOT = Path("/var/backups/meshtasticd-wdg")
LAST_BACKUP = BACKUP_ROOT / "LAST_TRANSACTION"
LOCK_DIRECTORY = Path("/run/lock/watchdogs")
LOCK_PATH = LOCK_DIRECTORY / "meshtastic-update.lock"
PROTECTED_VALIDATOR = Path(
    "/usr/local/libexec/watchdogs-meshtastic-lib/meshtastic_updates.py")
WDG_SOCKET_PATH = Path("/run/meshtasticd/wdg.sock")
WDG_BINARY_PATH = Path("/usr/lib/meshtasticd-wdg/meshtasticd")
RADIO_LOCK_PATH = Path("/run/lock/watchdogs/aio-sx1262.lock")
FLOCK_PATH = Path("/usr/bin/flock")
VALIDATION_ROOT = Path("/run/watchdogs-meshtastic-validation")
MESHTASTIC_CONFIG_DIR = Path("/etc/meshtasticd")
MESHTASTIC_STATE_DIR = Path("/var/lib/meshtasticd")
WDG_POLICY_PATH = MESHTASTIC_CONFIG_DIR / "wdg-portduino.yaml"
STARTUP_OUTPUT_LIMIT = 64 * 1024
MAC_LINE_RE = re.compile(
    rb"(?m)^MAC ADDRESS: ([0-9A-F]{2}(?::[0-9A-F]{2}){5})\r?$")
EXECUTABLES = {
    "systemctl": "/usr/bin/systemctl",
    "dpkg-query": "/usr/bin/dpkg-query",
    "dpkg": "/usr/bin/dpkg",
    "apt-get": "/usr/bin/apt-get",
    "cp": "/usr/bin/cp",
    "systemd-sysusers": "/usr/bin/systemd-sysusers",
    "systemd-tmpfiles": "/usr/bin/systemd-tmpfiles",
}
STATE_PATHS = (
    MESHTASTIC_CONFIG_DIR,
    MESHTASTIC_STATE_DIR,
)
CRITICAL_STATE_FILES = (
    Path("prefs/device.proto"),
    Path("prefs/config.proto"),
    Path("prefs/channels.proto"),
    Path("prefs/event-config.proto"),
    Path("prefs/event-channels.proto"),
    Path("backups/backup.proto"),
    Path("backups/event-backup.proto"),
)
SERVICE_SUPPLEMENTARY_GROUPS = ("spi", "gpio", "watchdogs")
RESTORABLE_LOAD_STATES = frozenset({"loaded", "not-found"})
RESTORABLE_ACTIVE_STATES = frozenset({"active", "inactive"})
RESTORABLE_UNIT_FILE_STATES = frozenset({"enabled", "disabled", "not-found"})
PACKAGE_VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\+wdg(0|[1-9][0-9]*)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class HelperError(RuntimeError):
    pass


class CandidateStateRestoreError(HelperError):
    """A candidate changed live state and its protected copy must be retained."""

    def __init__(self, message: str, evidence_path: Path) -> None:
        super().__init__(message)
        self.evidence_path = evidence_path


class InstallTransactionError(HelperError):
    """One install failed after recording whether rollback was complete."""

    def __init__(
        self,
        message: str,
        *,
        rollback_restored: bool,
        backup: Path | None,
    ) -> None:
        super().__init__(message)
        self.rollback_restored = rollback_restored
        self.backup = backup


class _StateRestoreEntry:
    def __init__(
        self,
        *,
        target: Path,
        expected: list[dict[str, Any]] | None,
        staging: Path | None,
        recovery: Path | None,
        staged_identity: tuple[int, int] | None = None,
    ) -> None:
        self.target = target
        self.expected = expected
        self.staging = staging
        self.recovery = recovery
        self.staged_identity = staged_identity
        self.moved_old = False
        self.moved_new = False


def _json_output(**values: Any) -> None:
    print(json.dumps({"ok": True, **values}, sort_keys=True))


def _json_install_error(error: InstallTransactionError) -> None:
    print(json.dumps({
        "ok": False,
        "action": "install-tag",
        "error_type": "install_failed",
        "error": str(error),
        "rollback_restored": error.rollback_restored,
        "backup": str(error.backup) if error.backup is not None else None,
    }, sort_keys=True))


def _require_root() -> None:
    if os.geteuid() != 0:
        raise HelperError("This Meshtastic operation must run through sudo")


def _run(command: list[str], *, timeout: int = 60,
         check: bool = True) -> subprocess.CompletedProcess[str]:
    if not command or command[0] not in EXECUTABLES:
        raise HelperError("Helper attempted a non-allowlisted executable")
    command = [EXECUTABLES[command[0]], *command[1:]]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=False)
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise HelperError(f"{command[0]} failed: {detail[:600]}")
    return result


def _secure_root_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise HelperError(str(path) + " must be a real directory")
    info = path.stat()
    if info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o077:
        raise HelperError(str(path) + " must be root:root mode 0700")


def _load_validator():
    """Import only the root-owned validation module installed by setup."""
    path = PROTECTED_VALIDATOR
    try:
        info = path.stat()
    except OSError as exc:
        raise HelperError("Protected Meshtastic validator is not installed") from exc
    if (path.is_symlink() or not path.is_file() or info.st_uid != 0
            or info.st_gid != 0 or info.st_mode & 0o022):
        raise HelperError("Protected Meshtastic validator has unsafe ownership or mode")
    spec = importlib.util.spec_from_file_location(
        "_watchdogs_protected_meshtastic_updates", path)
    if spec is None or spec.loader is None:
        raise HelperError("Could not load the protected Meshtastic validator")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules while the module is
    # executing, so register this private fixed-name import first.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _systemd_property(service: str, prop: str) -> str:
    result = _run(
        ["systemctl", "show", "--property=" + prop, "--value", service],
        timeout=15, check=False)
    value = (result.stdout or "").strip()
    if result.returncode:
        detail = (result.stderr or result.stdout or "systemctl show failed").strip()
        lowered = detail.lower()
        if (prop == "LoadState"
                and ("could not be found" in lowered
                     or "not found" in lowered)):
            return "not-found"
        raise HelperError("Could not inspect " + service + ": " + detail[:300])
    return value


def _installed_version() -> str | None:
    result = _run(
        ["dpkg-query", "--show", "--showformat=${db:Status-Abbrev}\t${Version}",
         PACKAGE_NAME], timeout=15, check=False)
    if result.returncode:
        return None
    parts = (result.stdout or "").strip().split("\t", 1)
    if len(parts) != 2 or not parts[0].startswith("ii"):
        return None
    return parts[1]


def _service_status(target: str) -> dict[str, Any]:
    service = TARGET_SERVICES[target]
    load_state = _systemd_property(service, "LoadState")
    if load_state == "not-found":
        # systemctl may itself fail when asked for properties of an absent
        # unit. LoadState is authoritative; do not turn an optional missing
        # stock/fork service into a helper failure by probing it further.
        return {
            "target": target,
            "service": service,
            "load_state": load_state,
            "active_state": "inactive",
            "unit_file_state": "not-found",
            "package_version": (
                _installed_version() if target == "wdg" else None),
        }
    return {
        "target": target,
        "service": service,
        "load_state": load_state,
        "active_state": _systemd_property(service, "ActiveState"),
        "unit_file_state": _systemd_property(service, "UnitFileState"),
        "package_version": _installed_version() if target == "wdg" else None,
    }


def _set_service(action: str, target: str) -> dict[str, Any]:
    _require_root()
    if action not in SERVICE_ACTIONS or target not in TARGET_SERVICES:
        raise HelperError("Unsupported service operation")
    with _lock_transaction():
        _run(["systemctl", action, TARGET_SERVICES[target]], timeout=30)
        return {"action": action, **_service_status(target)}


def _select_service(target: str) -> dict[str, Any]:
    """Atomically select one boot-time/live daemon and restore on failure."""
    _require_root()
    if target not in TARGET_SERVICES:
        raise HelperError("Unsupported Meshtastic service target")
    other = "stock" if target == "wdg" else "wdg"
    with _lock_transaction():
        services = _service_snapshot()
        try:
            if services[target].get("load_state") == "not-found":
                raise HelperError("Selected Meshtastic service is not installed")
            # Starting the chosen unit first lets systemd enforce Conflicts and
            # avoids disabling the last bootable unit before readiness is
            # known. Every following mutation is covered by snapshot restore.
            _run(["systemctl", "start", TARGET_SERVICES[target]], timeout=45)
            if _systemd_property(
                    TARGET_SERVICES[target], "ActiveState") != "active":
                raise HelperError("Selected Meshtastic service did not become active")
            other_state = (
                "inactive"
                if services[other].get("load_state") == "not-found"
                else _systemd_property(
                    TARGET_SERVICES[other], "ActiveState"))
            if other_state not in ("", "inactive", "failed"):
                raise HelperError(
                    "Conflicting Meshtastic service remained " + other_state)
            _set_enabled(TARGET_SERVICES[target], True)
            _set_enabled(TARGET_SERVICES[other], False)
        except Exception as select_error:
            try:
                _restore_services(services)
            except Exception as restore_error:  # noqa: BLE001
                raise HelperError(
                    f"Service selection failed ({select_error}); previous "
                    f"service state could not be restored ({restore_error})"
                ) from select_error
            raise HelperError(
                "Service selection failed; previous state restored: "
                + str(select_error)) from select_error
    return {
        "action": "select-service",
        "selected": target,
        **_service_status(target),
    }


def _lock_transaction():
    parent_descriptor = -1
    descriptor = -1
    try:
        try:
            watchdogs_gid = grp.getgrnam("watchdogs").gr_gid
        except KeyError as exc:
            raise HelperError(
                "The watchdogs radio-lock group is missing; rerun setup.sh") from exc
        parent_flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0))
        parent_descriptor = os.open(LOCK_DIRECTORY, parent_flags)
        parent_info = os.fstat(parent_descriptor)
        if (not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_uid != 0
                or parent_info.st_gid != watchdogs_gid
                or stat.S_IMODE(parent_info.st_mode) != 0o2750):
            raise HelperError(
                "Meshtastic lock directory is unsafe; rerun setup.sh")

        flags = (os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(
            LOCK_PATH.name, flags, 0o600, dir_fd=parent_descriptor)
        opened_info = os.fstat(descriptor)
        if (not stat.S_ISREG(opened_info.st_mode)
                or opened_info.st_nlink != 1):
            raise HelperError("Meshtastic transaction lock is unsafe")
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HelperError(
                "Another Meshtastic install, adoption, or rollback is in progress"
            ) from exc

        path_info = os.stat(
            LOCK_PATH.name, dir_fd=parent_descriptor, follow_symlinks=False)
        locked_info = os.fstat(descriptor)
        if (not stat.S_ISREG(path_info.st_mode)
                or locked_info.st_nlink != 1
                or path_info.st_dev != locked_info.st_dev
                or path_info.st_ino != locked_info.st_ino
                or locked_info.st_uid != 0 or locked_info.st_gid != 0
                or stat.S_IMODE(locked_info.st_mode) != 0o600):
            raise HelperError(
                "Meshtastic transaction lock changed while being acquired")
        stream = os.fdopen(descriptor, "a+")
        descriptor = -1
        return stream
    except HelperError:
        raise
    except OSError as exc:
        raise HelperError(
            "Could not acquire the protected Meshtastic transaction lock") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _tree_fingerprint(path: Path) -> list[dict[str, Any]] | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_dir():
        raise HelperError(str(path) + " must be a real directory")
    root_info = path.lstat()
    # Include the root itself.  A recursive child-only fingerprint could let a
    # damaged backup restore a directory with the wrong owner or mode while
    # every file below it still appeared valid.
    result: list[dict[str, Any]] = [{
        "path": ".",
        "mode": stat.S_IMODE(root_info.st_mode),
        "uid": root_info.st_uid,
        "gid": root_info.st_gid,
        "type": "directory",
    }]
    for entry in sorted(path.rglob("*"), key=lambda item: item.as_posix()):
        relative = entry.relative_to(path).as_posix()
        info = entry.lstat()
        record: dict[str, Any] = {
            "path": relative,
            "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "gid": info.st_gid,
        }
        if entry.is_symlink():
            record["type"] = "symlink"
            record["target"] = os.readlink(entry)
        elif entry.is_dir():
            record["type"] = "directory"
        elif entry.is_file():
            record["type"] = "file"
            digest = hashlib.sha256()
            with entry.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            record["sha256"] = digest.hexdigest()
        else:
            raise HelperError("Unsupported special file in " + str(path))
        result.append(record)
    return result


def _file_fingerprint(path: Path) -> dict[str, Any] | None:
    """Hash one critical file without following a replacement symlink."""
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    record: dict[str, Any] = {
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
    }
    if stat.S_ISLNK(info.st_mode):
        record.update(type="symlink", target=os.readlink(path))
        return record
    if not stat.S_ISREG(info.st_mode):
        raise HelperError("Critical Meshtastic path is not a regular file: "
                          + str(path))
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    record.update(type="file", sha256=digest.hexdigest())
    return record


def _critical_state_snapshot(
        fsdir: Path, config_dir: Path) -> dict[str, Any]:
    """Capture identity/channel state while excluding volatile node history."""
    if fsdir.is_symlink() or not fsdir.is_dir():
        raise HelperError("Meshtastic state directory is missing or unsafe: "
                          + str(fsdir))
    if config_dir.is_symlink() or not config_dir.is_dir():
        raise HelperError("Meshtastic config directory is missing or unsafe: "
                          + str(config_dir))
    return {
        "config": _tree_fingerprint(config_dir),
        "state": {
            path.as_posix(): _file_fingerprint(fsdir / path)
            for path in CRITICAL_STATE_FILES
        },
    }


def _resolve_state_fsdir(state_root: Path) -> Path:
    """Resolve stock Portduino state while retaining flat test compatibility."""
    if state_root.is_symlink() or not state_root.is_dir():
        raise HelperError("Meshtastic state directory is missing or unsafe: "
                          + str(state_root))

    portduino = state_root / ".portduino"
    nested = portduino / "default"
    if nested.exists() or nested.is_symlink():
        if (portduino.is_symlink() or not portduino.is_dir()
                or nested.is_symlink() or not nested.is_dir()):
            raise HelperError("Meshtastic Portduino state is unsafe: "
                              + str(nested))
        return nested

    prefs = state_root / "prefs"
    if prefs.is_symlink() or not prefs.is_dir():
        raise HelperError(
            "Meshtastic state contains neither stock .portduino/default "
            "nor a compatible flat prefs directory: " + str(state_root))
    return state_root


def _semantic_status_snapshot(status: dict[str, Any]) -> dict[str, Any]:
    """Extract only identity and channel semantics without copying secrets."""
    if not isinstance(status, dict):
        raise HelperError("meshtasticd-wdg returned malformed semantic status")
    identity = status.get("identity")
    channels = status.get("channels")
    if not isinstance(identity, dict) or not isinstance(channels, list):
        raise HelperError("meshtasticd-wdg returned malformed semantic status")

    node_id = identity.get("node_id")
    long_name = identity.get("long_name", identity.get("name"))
    short_name = identity.get("short_name")
    has_public = identity.get("has_public_key")
    has_private = identity.get("has_private_key")
    if (not isinstance(node_id, str)
            or re.fullmatch(r"![0-9A-Fa-f]{8}", node_id) is None
            or not isinstance(long_name, str)
            or not isinstance(short_name, str)
            or type(has_public) is not bool
            or type(has_private) is not bool):
        raise HelperError("meshtasticd-wdg returned malformed semantic identity")

    normalized_channels: list[dict[str, Any]] = []
    seen_indexes: set[int] = set()
    for channel in channels:
        if not isinstance(channel, dict):
            raise HelperError("meshtasticd-wdg returned malformed channel status")
        index = channel.get("index")
        name = channel.get("name")
        role = channel.get("role")
        has_psk = channel.get("has_psk")
        if (type(index) is not int or not 0 <= index <= 255
                or index in seen_indexes
                or not isinstance(name, str)
                or type(role) is not int or role not in (1, 2)
                or type(has_psk) is not bool):
            raise HelperError("meshtasticd-wdg returned malformed channel status")
        seen_indexes.add(index)
        normalized_channels.append({
            "index": index,
            "name": name,
            "role": role,
            "has_psk": has_psk,
        })
    return {
        "node_id": node_id.lower(),
        "long_name": long_name,
        "short_name": short_name,
        "has_public_key": has_public,
        "has_private_key": has_private,
        # A list deliberately preserves the firmware's channel order.
        "channels": normalized_channels,
    }


def _validated_semantic_baseline(value: Any) -> dict[str, Any]:
    """Validate persisted semantics without accepting secret-bearing fields."""
    expected_keys = {
        "node_id", "long_name", "short_name", "has_public_key",
        "has_private_key", "channels",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise HelperError("Meshtastic semantic baseline metadata is malformed")
    raw = {
        "identity": {
            "node_id": value.get("node_id"),
            "long_name": value.get("long_name"),
            "short_name": value.get("short_name"),
            "has_public_key": value.get("has_public_key"),
            "has_private_key": value.get("has_private_key"),
        },
        "channels": value.get("channels"),
    }
    try:
        normalized = _semantic_status_snapshot(raw)
    except HelperError as exc:
        raise HelperError(
            "Meshtastic semantic baseline metadata is malformed") from exc
    if normalized != value:
        raise HelperError("Meshtastic semantic baseline metadata is malformed")
    return normalized


def _parse_effective_mac(
        output: bytes, *, truncated: bool,
        semantic: dict[str, Any]) -> str:
    """Parse the daemon's own effective MAC and bind it to its node ID."""
    if truncated:
        raise HelperError(
            "meshtasticd-wdg startup output exceeded the validation limit")
    matches = MAC_LINE_RE.findall(output)
    if len(matches) != 1:
        raise HelperError(
            "meshtasticd-wdg did not report exactly one valid effective MAC")
    mac = matches[0].decode("ascii")
    octets = mac.split(":")
    if len(octets) != 6:
        raise HelperError("meshtasticd-wdg reported an invalid effective MAC")
    node_id = semantic.get("node_id")
    expected_node_id = "!" + "".join(octets[2:]).lower()
    if node_id != expected_node_id:
        raise HelperError(
            "meshtasticd-wdg effective MAC does not map to its reported node ID; "
            "set a stable General.MACAddress and retry")
    return mac


def _validation_baseline(
        metadata: dict[str, Any]) -> tuple[dict[str, Any], str, bool]:
    semantic = _validated_semantic_baseline(
        metadata.get("semantic_baseline"))
    mac = metadata.get("effective_mac")
    pin_required = metadata.get("mac_pin_required")
    if (not isinstance(mac, str)
            or re.fullmatch(r"[0-9A-F]{2}(?::[0-9A-F]{2}){5}", mac) is None
            or type(pin_required) is not bool):
        raise HelperError("Meshtastic semantic baseline metadata is malformed")
    expected_node_id = "!" + "".join(mac.split(":")[2:]).lower()
    if semantic["node_id"] != expected_node_id:
        raise HelperError("Meshtastic semantic baseline metadata is malformed")
    return semantic, mac, pin_required


def _copy_state_to_backup(backup: Path) -> dict[str, bool]:
    presence: dict[str, bool] = {}
    state_dir = backup / "state"
    state_dir.mkdir(mode=0o700)
    for source in STATE_PATHS:
        key = _state_backup_key(source)
        exists = source.exists()
        presence[key] = exists
        if exists:
            if source.is_symlink() or not source.is_dir():
                raise HelperError(str(source) + " must be a real directory")
            _run(["cp", "-a", "--", str(source), str(state_dir / key)], timeout=120)
    return presence


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_manifest_digest(manifest: dict[str, Any]) -> str:
    payload = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _package_version_for_tag(tag: str) -> str:
    match = TAG_RE.fullmatch(tag)
    if match is None:
        raise HelperError("Expected Meshtastic tag vX.Y.Z-wdg.N")
    major, minor, patch, revision = match.groups()
    return f"{major}.{minor}.{patch}+wdg{revision}"


def _release_identity(prepared: Any) -> dict[str, Any]:
    manifest = getattr(prepared, "manifest", None)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("package"), dict):
        raise HelperError("Verified Meshtastic release metadata is incomplete")
    package = manifest["package"]
    value = {
        "tag": getattr(prepared, "tag", None),
        "package_version": getattr(prepared, "package_version", None),
        "package_asset": package.get("asset"),
        "package_size": package.get("size"),
        "package_sha256": package.get("sha256"),
        "source_commit": manifest.get("source_commit"),
        "manifest_sha256": _canonical_manifest_digest(manifest),
    }
    return _validate_release_identity(value)


def _validate_release_identity(value: Any) -> dict[str, Any]:
    expected_keys = {
        "tag", "package_version", "package_asset", "package_size",
        "package_sha256", "source_commit", "manifest_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise HelperError("Meshtastic transaction release identity is malformed")
    tag = value.get("tag")
    package_version = value.get("package_version")
    if (not isinstance(tag, str) or TAG_RE.fullmatch(tag) is None
            or package_version != _package_version_for_tag(tag)
            or PACKAGE_VERSION_RE.fullmatch(str(package_version)) is None):
        raise HelperError("Meshtastic transaction release identity is malformed")
    expected_asset = f"meshtasticd-wdg_{package_version}_arm64.deb"
    if (value.get("package_asset") != expected_asset
            or type(value.get("package_size")) is not int
            or value["package_size"] <= 0
            or not isinstance(value.get("package_sha256"), str)
            or SHA256_RE.fullmatch(value["package_sha256"]) is None
            or not isinstance(value.get("source_commit"), str)
            or re.fullmatch(r"[0-9a-f]{40}", value["source_commit"]) is None
            or not isinstance(value.get("manifest_sha256"), str)
            or SHA256_RE.fullmatch(value["manifest_sha256"]) is None):
        raise HelperError("Meshtastic transaction release identity is malformed")
    return value


def _validate_transaction_metadata(
        metadata: Any, backup: Path, validator) -> tuple[Any | None, dict[str, bool],
                                                         dict[str, Any]]:
    """Validate every rollback input before any live service or path changes."""
    if backup.is_symlink() or not backup.is_dir():
        raise HelperError("Meshtastic rollback backup is missing or unsafe")
    backup_info = backup.stat()
    if (os.geteuid() == 0
            and (backup_info.st_uid != 0 or backup_info.st_gid != 0
                 or stat.S_IMODE(backup_info.st_mode) != 0o700)):
        raise HelperError("Meshtastic rollback backup is not root-private")
    expected_keys = {
        "format", "new_release", "previous_version", "rollback_release",
        "services", "state_presence", "state_fingerprints",
        "semantic_baseline", "effective_mac", "mac_pin_required",
    }
    if (not isinstance(metadata, dict) or set(metadata) != expected_keys
            or type(metadata.get("format")) is not int
            or metadata["format"] != 2):
        raise HelperError("Meshtastic transaction metadata is unsupported")

    _validate_release_identity(metadata.get("new_release"))
    _validation_baseline(metadata)
    _validate_service_snapshot(metadata.get("services"))
    presence = metadata.get("state_presence")
    fingerprints = metadata.get("state_fingerprints")
    expected_paths = {str(path) for path in STATE_PATHS}
    expected_keys_by_path = {_state_backup_key(path) for path in STATE_PATHS}
    if (not isinstance(presence, dict)
            or set(presence) != expected_keys_by_path
            or any(type(value) is not bool for value in presence.values())
            or not isinstance(fingerprints, dict)
            or set(fingerprints) != expected_paths):
        raise HelperError("Meshtastic transaction state metadata is malformed")

    previous_version = metadata.get("previous_version")
    rollback_identity = metadata.get("rollback_release")
    prepared = None
    if previous_version is None:
        if rollback_identity is not None:
            raise HelperError("Meshtastic rollback package metadata is inconsistent")
    else:
        if (not isinstance(previous_version, str)
                or PACKAGE_VERSION_RE.fullmatch(previous_version) is None):
            raise HelperError("Meshtastic rollback package metadata is invalid")
        rollback_identity = _validate_release_identity(rollback_identity)
        if rollback_identity["package_version"] != previous_version:
            raise HelperError("Meshtastic rollback package metadata is inconsistent")
        rollback_dir = backup / "rollback-release" / rollback_identity["tag"]
        prepared = validator.validate_prepared_release(
            rollback_dir, expected_tag=rollback_identity["tag"],
            require_secure=True, check_host=False)
        if _release_identity(prepared) != rollback_identity:
            raise HelperError(
                "Verified rollback release differs from transaction metadata")

    state_dir = backup / "state"
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise HelperError("Meshtastic backup state directory is missing or unsafe")
    actual_backup_keys = {entry.name for entry in state_dir.iterdir()}
    expected_present_keys = {
        key for key, value in presence.items() if value}
    if actual_backup_keys != expected_present_keys:
        raise HelperError("Meshtastic backup state contents do not match metadata")
    for target in STATE_PATHS:
        key = _state_backup_key(target)
        expected = fingerprints[str(target)]
        source = state_dir / key
        if presence[key]:
            if (source.is_symlink() or not source.is_dir()
                    or not isinstance(expected, list)
                    or _tree_fingerprint(source) != expected):
                raise HelperError(
                    "Meshtastic backup state fingerprint does not match: " + key)
        elif expected is not None:
            raise HelperError("Meshtastic absent-state fingerprint is malformed")
    return prepared, presence, fingerprints


def _prepare_state_restore(
        backup: Path, presence: dict[str, bool],
        fingerprints: dict[str, Any]) -> list[_StateRestoreEntry]:
    """Copy validated backup trees beside targets before mutating live state."""
    entries: list[_StateRestoreEntry] = []
    try:
        for target in STATE_PATHS:
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise HelperError(
                    "Refusing to replace unsafe state path " + str(target))
            if target.parent.is_symlink() or not target.parent.is_dir():
                raise HelperError(
                    "Meshtastic state parent is missing or unsafe: "
                    + str(target.parent))
            key = _state_backup_key(target)
            expected = fingerprints[str(target)]
            staging = None
            staged_identity = None
            entry = _StateRestoreEntry(
                target=target, expected=expected, staging=None,
                recovery=None)
            entries.append(entry)
            if presence[key]:
                source = backup / "state" / key
                reserved = Path(tempfile.mkdtemp(
                    prefix="." + target.name + ".wdg-restore-",
                    dir=target.parent))
                reserved.rmdir()
                staging = reserved
                entry.staging = staging
                _run(["cp", "-a", "--", str(source), str(staging)], timeout=120)
                if (staging.is_symlink() or not staging.is_dir()
                        or _tree_fingerprint(staging) != expected):
                    raise HelperError(
                        "Prepared Meshtastic restore copy failed verification: " + key)
                staged_info = staging.lstat()
                staged_identity = (staged_info.st_dev, staged_info.st_ino)
            recovery = Path(tempfile.mkdtemp(
                prefix="." + target.name + ".wdg-recovery-",
                dir=target.parent))
            recovery.rmdir()
            entry.recovery = recovery
            entry.staged_identity = staged_identity
        return entries
    except Exception:
        _cleanup_restore_entries(entries)
        raise


def _cleanup_restore_entries(entries: list[_StateRestoreEntry]) -> None:
    for entry in entries:
        for path in (entry.staging, entry.recovery):
            if path is not None and (path.exists() or path.is_symlink()):
                if path.is_symlink() or not path.is_dir():
                    raise HelperError(
                        "Meshtastic restore staging path changed unexpectedly")
                shutil.rmtree(path)


def _recover_state_swaps(entries: list[_StateRestoreEntry]) -> None:
    errors: list[str] = []
    for entry in reversed(entries):
        try:
            if entry.moved_new:
                if entry.target.is_symlink() or not entry.target.is_dir():
                    raise HelperError(
                        "replacement path changed during state recovery")
                info = entry.target.lstat()
                if (info.st_dev, info.st_ino) != entry.staged_identity:
                    raise HelperError(
                        "replacement identity changed during state recovery")
                shutil.rmtree(entry.target)
                entry.moved_new = False
            if entry.moved_old:
                if entry.target.exists() or entry.target.is_symlink():
                    raise HelperError(
                        "target reappeared during state recovery")
                if entry.recovery is None:
                    raise HelperError("state recovery path was not allocated")
                os.rename(entry.recovery, entry.target)
                entry.moved_old = False
            _fsync_directory(entry.target.parent)
        except Exception as exc:  # noqa: BLE001 - report every recovery failure
            errors.append(str(entry.target) + ": " + str(exc))
    if errors:
        raise HelperError(
            "Meshtastic live state recovery was incomplete: " + "; ".join(errors))


def _apply_state_restore(entries: list[_StateRestoreEntry]) -> None:
    """Swap all prepared trees in; recover earlier swaps if any later one fails."""
    try:
        # Revalidate every path first so a bad later entry cannot be discovered
        # only after an earlier live directory has already moved.
        for entry in entries:
            if (entry.target.is_symlink()
                    or (entry.target.exists() and not entry.target.is_dir())
                    or entry.recovery is None
                    or entry.recovery.exists()
                    or entry.recovery.is_symlink()):
                raise HelperError(
                    "Meshtastic restore target changed after preflight: "
                    + str(entry.target))
            if entry.staging is None:
                if entry.expected is not None:
                    raise HelperError("Meshtastic restore staging is incomplete")
            else:
                if entry.staging.is_symlink() or not entry.staging.is_dir():
                    raise HelperError("Meshtastic restore staging is missing")
                info = entry.staging.lstat()
                if ((info.st_dev, info.st_ino) != entry.staged_identity
                        or _tree_fingerprint(entry.staging) != entry.expected):
                    raise HelperError(
                        "Meshtastic restore staging changed after preflight")
        for entry in entries:
            if entry.target.exists():
                if entry.recovery is None:
                    raise HelperError("state recovery path was not allocated")
                os.rename(entry.target, entry.recovery)
                entry.moved_old = True
            if entry.staging is not None:
                os.rename(entry.staging, entry.target)
                entry.moved_new = True
            _fsync_directory(entry.target.parent)
        for entry in entries:
            actual = _tree_fingerprint(entry.target)
            if actual != entry.expected:
                raise HelperError(
                    "Meshtastic restored state failed verification: "
                    + str(entry.target))
    except Exception as restore_error:
        try:
            _recover_state_swaps(entries)
        except Exception as recovery_error:
            raise HelperError(
                f"Meshtastic state restore failed ({restore_error}); recovery "
                f"also failed ({recovery_error})") from restore_error
        raise


def _finalize_state_restore(entries: list[_StateRestoreEntry]) -> None:
    for entry in entries:
        if entry.recovery is not None and entry.recovery.exists():
            if entry.recovery.is_symlink() or not entry.recovery.is_dir():
                raise HelperError(
                    "Meshtastic recovery copy changed before cleanup")
            shutil.rmtree(entry.recovery)
        entry.moved_old = False
        entry.moved_new = False
        _fsync_directory(entry.target.parent)


def _restore_state_from_backup(
        backup: Path, presence: dict[str, bool],
        expected_fingerprints: dict[str, Any] | None = None) -> None:
    """Restore a private state copy only after proving every source tree.

    ``expected_fingerprints`` is supplied by candidate validation, where the
    pre-candidate snapshot is the trust anchor.  The optional form remains
    useful for the narrowly scoped MAC-pin rollback, whose root-private copy
    is created and consumed inside one helper invocation.
    """
    expected_keys = {_state_backup_key(path) for path in STATE_PATHS}
    if (not isinstance(presence, dict) or set(presence) != expected_keys
            or any(type(value) is not bool for value in presence.values())):
        raise HelperError("Meshtastic backup presence metadata is malformed")
    state_dir = backup / "state"
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise HelperError("Meshtastic backup state directory is missing or unsafe")
    if expected_fingerprints is not None:
        if (not isinstance(expected_fingerprints, dict)
                or set(expected_fingerprints) != {
                    str(path) for path in STATE_PATHS}):
            raise HelperError(
                "Meshtastic backup fingerprint metadata is malformed")
        fingerprints = dict(expected_fingerprints)
    else:
        fingerprints = {}
    for target in STATE_PATHS:
        key = _state_backup_key(target)
        if presence.get(key):
            source = state_dir / key
            if source.is_symlink() or not source.is_dir():
                raise HelperError("Backup state is missing or unsafe: " + key)
            actual = _tree_fingerprint(source)
            if expected_fingerprints is None:
                fingerprints[str(target)] = actual
            elif actual != fingerprints[str(target)]:
                raise HelperError(
                    "Meshtastic backup state fingerprint does not match: " + key)
        else:
            if (state_dir / key).exists() or (state_dir / key).is_symlink():
                raise HelperError("Unexpected backup state exists: " + key)
            if expected_fingerprints is None:
                fingerprints[str(target)] = None
            elif fingerprints[str(target)] is not None:
                raise HelperError(
                    "Meshtastic absent-state fingerprint is malformed")
    entries = _prepare_state_restore(backup, presence, fingerprints)
    _apply_state_restore(entries)
    _finalize_state_restore(entries)


def _state_backup_key(path: Path) -> str:
    return path.as_posix().lstrip("/").replace("/", "-")


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchown(descriptor, 0, 0)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(
        path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_private_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise HelperError("Meshtastic transaction metadata is missing")
    info = path.stat()
    if info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o077:
        raise HelperError("Meshtastic transaction metadata is not private")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HelperError("Meshtastic transaction metadata is invalid") from exc
    if (not isinstance(value, dict)
            or type(value.get("format")) is not int
            or value.get("format") != 2):
        raise HelperError("Meshtastic transaction metadata is unsupported")
    return value


def _validate_service_snapshot(
        snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Accept only service states the helper can restore exactly."""
    if not isinstance(snapshot, dict) or set(snapshot) != set(TARGET_SERVICES):
        raise HelperError("Meshtastic service snapshot is malformed")
    active_targets: list[str] = []
    for target in TARGET_SERVICES:
        state = snapshot.get(target)
        if not isinstance(state, dict):
            raise HelperError("Meshtastic service snapshot is malformed")
        load_state = state.get("load_state")
        active_state = state.get("active_state")
        unit_file_state = state.get("unit_file_state")
        if (load_state not in RESTORABLE_LOAD_STATES
                or active_state not in RESTORABLE_ACTIVE_STATES
                or unit_file_state not in RESTORABLE_UNIT_FILE_STATES):
            raise HelperError(
                f"Cannot preserve {TARGET_SERVICES[target]} state exactly: "
                f"load={load_state or 'unknown'}, "
                f"active={active_state or 'unknown'}, "
                f"unit={unit_file_state or 'unknown'}; normalize the service "
                "to loaded plus enabled/disabled and active/inactive")
        if load_state == "not-found":
            if active_state != "inactive" or unit_file_state != "not-found":
                raise HelperError("Meshtastic service snapshot is inconsistent")
        elif unit_file_state == "not-found":
            raise HelperError("Meshtastic service snapshot is inconsistent")
        if active_state == "active":
            active_targets.append(target)
    if len(active_targets) > 1:
        raise HelperError(
            "Both Meshtastic services are active; stop one before continuing")
    return snapshot


def _service_snapshot() -> dict[str, dict[str, Any]]:
    return _validate_service_snapshot({
        target: _service_status(target) for target in TARGET_SERVICES})


def _find_cached_release(version: str, validator) -> Any | None:
    if not INSTALLED_CACHE.exists():
        return None
    if INSTALLED_CACHE.is_symlink() or not INSTALLED_CACHE.is_dir():
        raise HelperError("Installed-package cache is unsafe")
    for candidate in sorted(INSTALLED_CACHE.iterdir()):
        if not candidate.is_dir() or candidate.is_symlink() or not TAG_RE.fullmatch(candidate.name):
            continue
        try:
            prepared = validator.validate_prepared_release(
                candidate, expected_tag=candidate.name, require_secure=True,
                check_host=False)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            continue
        if prepared.package_version == version:
            return prepared
    return None


def _prepare_exact_release(tag: str, validator) -> Any:
    """Load or download one exact, protected-validator-approved release."""
    release_dir = CACHE_ROOT / tag
    if release_dir.exists():
        prepared = validator.validate_prepared_release(
            release_dir, expected_tag=tag, require_secure=True,
            check_host=True)
    else:
        releases = validator.meshtastic_releases()
        release = next(
            (candidate for candidate in releases
             if candidate.get("tag_name") == tag), None)
        if release is None:
            raise HelperError(
                "The requested Smethan Meshtastic release was not found")
        prepared = validator.prepare_meshtastic_release(
            cache_root=CACHE_ROOT, release=release, require_root=True,
            check_host=True)
    if (prepared.tag != tag
            or prepared.directory != release_dir
            or not isinstance(prepared.package_version, str)):
        raise HelperError(
            "Protected validator returned the wrong Meshtastic release")
    return prepared


def _create_backup(tag: str, prepared: Any, validator,
                   services: dict[str, dict[str, Any]]) -> tuple[Path, dict[str, Any]]:
    _secure_root_directory(BACKUP_ROOT)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = Path(tempfile.mkdtemp(prefix=stamp + "-", dir=BACKUP_ROOT))
    os.chmod(backup, 0o700)
    try:
        previous_version = _installed_version()
        if previous_version is None:
            raise HelperError(
                "No previously installed meshtasticd-wdg package exists from "
                "which to establish an authoritative semantic baseline, and "
                "the stock service exposes no trustworthy read-only WDG "
                "semantic baseline. The first stock-to-WDG migration must be "
                "performed manually: install and verify " + tag
                + ", stop both Meshtastic services, then run `sudo "
                "/usr/local/libexec/watchdogs-meshtastic adopt-installed "
                + tag + "`; the update helper will not validate a new package "
                "against itself")
        rollback_release = _find_cached_release(previous_version, validator)
        if rollback_release is None:
            raise HelperError(
                "The installed meshtasticd-wdg package has no verified rollback "
                "package in the root cache; refusing to update")
        rollback_tag = rollback_release.tag
        rollback_root = backup / "rollback-release"
        rollback_root.mkdir(mode=0o700)
        shutil.copytree(
            rollback_release.directory, rollback_root / rollback_tag,
            copy_function=shutil.copy2)
        os.chmod(rollback_root / rollback_tag, 0o700)
        for child in (rollback_root / rollback_tag).iterdir():
            os.chmod(child, 0o600)

        fingerprints = {
            str(path): _tree_fingerprint(path) for path in STATE_PATHS}
        presence = _copy_state_to_backup(backup)
        baseline = _candidate_dry_run_preserving_live_state(
            backup, fingerprints, presence)
        metadata = {
            "format": 2,
            "new_release": _release_identity(prepared),
            "previous_version": previous_version,
            "rollback_release": _release_identity(rollback_release),
            "services": services,
            "state_presence": presence,
            "state_fingerprints": fingerprints,
            "semantic_baseline": baseline["semantic"],
            "effective_mac": baseline["effective_mac"],
            "mac_pin_required": baseline["mac_pin_required"],
        }
        _validation_baseline(metadata)
        _write_private_json(backup / "transaction.json", metadata)
        return backup, metadata
    except Exception as exc:
        # A failed candidate that also defeated exact state restoration leaves
        # the private backup in place as the only recovery/evidence copy.
        if not isinstance(exc, CandidateStateRestoreError):
            shutil.rmtree(backup, ignore_errors=True)
        raise


def _state_unchanged(metadata: dict[str, Any]) -> bool:
    expected = metadata.get("state_fingerprints")
    if not isinstance(expected, dict):
        return False
    return all(_tree_fingerprint(Path(path)) == fingerprint
               for path, fingerprint in expected.items())


def _set_enabled(service: str, enabled: bool) -> None:
    if not enabled and _systemd_property(service, "LoadState") == "not-found":
        return
    _run(["systemctl", "enable" if enabled else "disable", service], timeout=30)


def _restore_services(snapshot: dict[str, Any]) -> None:
    snapshot = _validate_service_snapshot(snapshot)
    _run(["systemctl", "daemon-reload"], timeout=30)
    for target, service in TARGET_SERVICES.items():
        state = snapshot[target]
        if state["load_state"] != "not-found":
            _set_enabled(service, state["unit_file_state"] == "enabled")
    # Conflicting daemons cannot be active simultaneously.  The validated
    # snapshot identifies the sole prior radio owner, if there was one.
    active_target = None
    for target in ("stock", "wdg"):
        state = snapshot.get(target, {}) if isinstance(snapshot, dict) else {}
        if state.get("active_state") == "active":
            active_target = target
    for service in TARGET_SERVICES.values():
        _run(["systemctl", "stop", service], timeout=30, check=False)
    if active_target is not None:
        _run(["systemctl", "start", TARGET_SERVICES[active_target]], timeout=45)
    restored = _service_snapshot()
    for target in TARGET_SERVICES:
        expected = snapshot[target]
        actual = restored[target]
        for field in ("load_state", "active_state", "unit_file_state"):
            if actual[field] != expected[field]:
                raise HelperError(
                    "Could not restore " + TARGET_SERVICES[target]
                    + " " + field + " exactly")


def _restore_transaction(backup: Path, metadata: dict[str, Any], validator) -> None:
    """Restore one transaction only after complete, immutable preflight."""
    prepared, presence, fingerprints = _validate_transaction_metadata(
        metadata, backup, validator)
    entries = _prepare_state_restore(backup, presence, fingerprints)
    state_applied = False
    try:
        # Revalidate the exact package bytes immediately before the first live
        # mutation. The root-only cache is trusted, but this also detects disk
        # damage between transaction creation and rollback.
        if prepared is not None:
            rollback_identity = metadata["rollback_release"]
            prepared = validator.validate_prepared_release(
                prepared.directory, expected_tag=rollback_identity["tag"],
                require_secure=True, check_host=False)
            if _release_identity(prepared) != rollback_identity:
                raise HelperError(
                    "Rollback release changed after restore preflight")

        for service in TARGET_SERVICES.values():
            _run(["systemctl", "stop", service], timeout=30, check=False)
        previous_version = metadata["previous_version"]
        if previous_version is None:
            _run(["dpkg", "--purge", PACKAGE_NAME], timeout=120, check=False)
        else:
            assert prepared is not None
            _run([
                "apt-get", "-y", "--allow-downgrades",
                "--no-install-recommends", "install",
                str(prepared.package_path),
            ], timeout=300)
        if _installed_version() != previous_version:
            raise HelperError(
                "Meshtastic package rollback did not restore the previous version")
        if prepared is not None:
            # Prove both the cached release and every installed payload byte
            # still match the exact identity recorded by the transaction.
            checked = validator.validate_prepared_release(
                prepared.directory,
                expected_tag=metadata["rollback_release"]["tag"],
                require_secure=True, check_host=False)
            if _release_identity(checked) != metadata["rollback_release"]:
                raise HelperError(
                    "Rollback release identity changed during installation")
            validator.validate_installed_package_payload(
                checked.package_path, checked.manifest)

        _apply_state_restore(entries)
        state_applied = True
        if not _state_unchanged(metadata):
            raise HelperError(
                "Meshtastic state rollback did not restore the verified backup")
        _restore_services(metadata["services"])
        _finalize_state_restore(entries)
    except Exception:
        if not state_applied:
            _cleanup_restore_entries(entries)
        # Once the verified old package and state are restored, retain them if
        # service restoration fails. Replacing them with candidate-era state
        # would make recovery less safe; the private backup remains available.
        raise


def _socket_request(client: socket.socket, request_id: str, name: str) -> dict[str, Any]:
    packet = json.dumps({
        "v": 1,
        "type": "command",
        "request_id": request_id,
        "name": name,
        "body": {},
    }, separators=(",", ":")).encode("utf-8")
    client.sendall(packet)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        payload = client.recv(65537)
        if len(payload) > 65536:
            raise HelperError("meshtasticd-wdg returned an oversized health reply")
        try:
            reply = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HelperError("meshtasticd-wdg returned invalid health JSON") from exc
        if (not isinstance(reply, dict)
                or type(reply.get("v")) is not int
                or reply.get("v") != 1):
            raise HelperError(
                "meshtasticd-wdg returned an invalid health envelope")
        packet_type = reply.get("type")
        if packet_type == "event":
            if (type(reply.get("event_id")) is not int
                    or reply["event_id"] < 0
                    or not isinstance(reply.get("name"), str)
                    or not reply["name"]
                    or not isinstance(reply.get("body"), dict)):
                raise HelperError(
                    "meshtasticd-wdg returned an invalid event envelope")
            continue
        if (packet_type != "reply"
                or reply.get("request_id") != request_id):
            raise HelperError(
                "meshtasticd-wdg returned an unexpected health reply")
        if reply.get("ok") is not True or not isinstance(reply.get("body"), dict):
            raise HelperError("meshtasticd-wdg rejected the health request")
        return reply["body"]
    raise HelperError("meshtasticd-wdg health request timed out")


def _validate_health_bodies(
        hello: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
    if (type(hello.get("protocol_version")) is not int
            or hello["protocol_version"] != 1):
        raise HelperError("meshtasticd-wdg exposes an incompatible WDG API")
    if (status.get("state") != "ready"
            or status.get("radio_status") != "ready"):
        raise HelperError("meshtasticd-wdg radio is not ready")
    return status


def _health_check(
        socket_path: Path = WDG_SOCKET_PATH, *, timeout: float = 20.0,
        process: subprocess.Popen | None = None) -> dict[str, Any]:
    """Verify one fixed WDG socket and return its complete status body."""
    socket_path = Path(socket_path)
    deadline = time.monotonic() + max(0.1, float(timeout))
    last_error = "socket did not appear"
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            if getattr(process, "returncode", None) == 75:
                raise HelperError(
                    "AIO SX1262 is already owned by another process; stop "
                    "WatchDogsGo direct LoRa/MeshCore use and retry")
            raise HelperError(
                "meshtasticd-wdg candidate exited before its health check "
                f"(status {getattr(process, 'returncode', 'unknown')})")
        if not socket_path.exists():
            time.sleep(0.25)
            continue
        try:
            if not stat.S_ISSOCK(socket_path.lstat().st_mode):
                raise HelperError(
                    "meshtasticd-wdg health path is not a Unix socket")
        except OSError as exc:
            last_error = str(exc)
            time.sleep(0.25)
            continue
        client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        client.settimeout(3.0)
        try:
            client.connect(str(socket_path))
            hello = _socket_request(client, "install-health-hello", "hello")
            status = _socket_request(client, "install-health-status", "get_status")
            return _validate_health_bodies(hello, status)
        except (OSError, HelperError) as exc:
            last_error = str(exc)
            time.sleep(0.25)
        finally:
            client.close()
    raise HelperError("meshtasticd-wdg failed its health check: " + last_error)


def _validate_installed_binary() -> Path:
    """Return only the fixed package binary after root-ownership checks."""
    path = WDG_BINARY_PATH
    try:
        info = path.lstat()
    except OSError as exc:
        raise HelperError("Installed meshtasticd-wdg binary is missing") from exc
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0 or info.st_gid != 0
            or info.st_mode & 0o022
            or not info.st_mode & stat.S_IXUSR):
        raise HelperError(
            "Installed meshtasticd-wdg binary has unsafe ownership or mode")
    return path


def _validate_regular_tree(path: Path, description: str) -> None:
    """Reject every symlink and non-file/non-directory below ``path``."""
    try:
        info = path.lstat()
    except OSError as exc:
        raise HelperError(description + " is missing or unreadable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise HelperError(description + " must be a real directory")

    pending = [path]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                children = list(entries)
        except OSError as exc:
            raise HelperError(description + " is unreadable") from exc
        for entry in children:
            try:
                child_info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise HelperError(description + " changed during validation") from exc
            child = Path(entry.path)
            if stat.S_ISLNK(child_info.st_mode):
                raise HelperError(
                    "Candidate validation rejects symlinks: " + str(child))
            if stat.S_ISDIR(child_info.st_mode):
                pending.append(child)
            elif not stat.S_ISREG(child_info.st_mode):
                raise HelperError(
                    "Candidate validation rejects special files: " + str(child))


def _secure_validation_root() -> Path:
    """Return the fixed root-owned, execute-only-listing validation parent."""
    path = VALIDATION_ROOT
    created = False
    try:
        path.mkdir(mode=0o711)
        created = True
    except FileExistsError:
        pass
    if created:
        os.chown(path, 0, 0)
        os.chmod(path, 0o711)
    try:
        info = path.lstat()
    except OSError as exc:
        raise HelperError("Meshtastic validation root is unavailable") from exc
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o711):
        raise HelperError(
            "Meshtastic validation root must be root:root mode 0711")
    return path


def _meshtasticd_credentials() -> tuple[int, int, list[int]]:
    try:
        account = pwd.getpwnam("meshtasticd")
        group = grp.getgrnam("meshtasticd")
    except KeyError as exc:
        raise HelperError("meshtasticd system account is not installed") from exc
    uid = account.pw_uid
    gid = group.gr_gid
    if uid <= 0 or gid <= 0 or account.pw_gid != gid:
        raise HelperError("meshtasticd system account has unsafe credentials")
    supplementary: list[int] = []
    for name in SERVICE_SUPPLEMENTARY_GROUPS:
        try:
            supplement = grp.getgrnam(name).gr_gid
        except KeyError as exc:
            raise HelperError(
                "Meshtastic service group is not installed: " + name) from exc
        if supplement <= 0 or supplement == gid or supplement in supplementary:
            raise HelperError(
                "Meshtastic service supplementary groups are unsafe")
        supplementary.append(supplement)
    return uid, gid, supplementary


def _chown_validation_tree(path: Path, uid: int, gid: int) -> None:
    _validate_regular_tree(path, "Meshtastic candidate copy")
    # Keep the random child root-owned and non-traversable until every copied
    # descendant has been validated and assigned to the daemon.
    for directory, names, files in os.walk(
            path, topdown=False, followlinks=False):
        for name in [*names, *files]:
            os.chown(Path(directory) / name, uid, gid, follow_symlinks=False)
        os.chown(directory, uid, gid)
    os.chmod(path, 0o700)


def _copy_candidate_state(
        backup: Path, uid: int, gid: int,
        ) -> tuple[Path, Path, Path, Path, Path | None]:
    """Clone immutable rollback data for an isolated candidate boot."""
    source_root = backup / "state"
    source_config = source_root / _state_backup_key(MESHTASTIC_CONFIG_DIR)
    source_fsdir = source_root / _state_backup_key(MESHTASTIC_STATE_DIR)
    _validate_regular_tree(source_root, "Meshtastic backup state root")
    _validate_regular_tree(
        source_config, "Meshtastic backup configuration")
    _validate_regular_tree(source_fsdir, "Meshtastic backup state")

    parent = _secure_validation_root()
    candidate = Path(tempfile.mkdtemp(prefix="candidate-", dir=parent))
    try:
        os.chmod(candidate, 0o700)
        config_dir = candidate / "etc-meshtasticd"
        state_root = candidate / "var-lib-meshtasticd"
        shutil.copytree(
            source_config, config_dir, symlinks=False,
            copy_function=shutil.copy2)
        shutil.copytree(
            source_fsdir, state_root, symlinks=False,
            copy_function=shutil.copy2)
        _chown_validation_tree(candidate, uid, gid)
        fsdir = _resolve_state_fsdir(state_root)
        config = config_dir / "config.yaml"
        try:
            config_info = config.lstat()
        except OSError as exc:
            raise HelperError(
                "Copied Meshtastic config.yaml is missing or unsafe") from exc
        if not stat.S_ISREG(config_info.st_mode):
            raise HelperError(
                "Copied Meshtastic config.yaml is missing or unsafe")
        fragment_dir = _isolate_candidate_config_directory(config, config_dir)
        socket_path = candidate / "wdg.sock"
        return candidate, fsdir, config, socket_path, fragment_dir
    except Exception:
        try:
            shutil.rmtree(candidate)
        except OSError as cleanup_error:
            raise HelperError(
                "Candidate preparation failed and its validation directory "
                "could not be cleaned") from cleanup_error
        raise


def _yaml_uses_indirection_or_documents(content: str) -> bool:
    stripped = content.strip()
    if not stripped or stripped.startswith("#"):
        return False
    without_comment = content.split("#", 1)[0]
    return bool(
        re.search(
            r"(?:^|[\s:\-\[,])[*&][A-Za-z0-9_-]+", without_comment)
        or re.search(r"(?:^|\s)<<[ \t]*:", without_comment)
        or stripped in ("---", "...")
        or stripped.startswith("%")
    )


def _isolate_candidate_config_directory(
        config: Path, config_dir: Path) -> Path | None:
    """Redirect ConfigDirectory fragments into the disposable config copy."""
    try:
        text = config.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise HelperError("Copied Meshtastic config.yaml is unreadable") from exc

    lines = text.splitlines(keepends=True)
    general_rows: list[int] = []
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        stripped = content.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _yaml_uses_indirection_or_documents(content):
            raise HelperError(
                "Copied Meshtastic config uses YAML aliases or document "
                "features that prevent safe General.ConfigDirectory isolation")
        if re.match(r"^[\"']General[\"'][ \t]*:", content):
            raise HelperError(
                "Copied Meshtastic config uses a quoted General key")
        if (re.match(r"^General[ \t]*:", content)
                and not re.fullmatch(r"General:[ \t]*(?:#.*)?", content)):
            raise HelperError(
                "Copied Meshtastic config uses an unsupported inline General mapping")
        if re.fullmatch(r"General:[ \t]*(?:#.*)?", content):
            general_rows.append(index)
        if (content and not content[0].isspace()
                and re.match(r"^[A-Za-z0-9_.-]+[ \t]*:", content) is None):
            raise HelperError(
                "Copied Meshtastic config root must be a plain mapping")

    if len(general_rows) > 1:
        raise HelperError(
            "Copied Meshtastic config has duplicate General mappings")
    if not general_rows:
        return None

    start = general_rows[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        content = lines[index].rstrip("\r\n")
        if content and not content[0].isspace() and not content.startswith("#"):
            end = index
            break

    child_indents: list[int] = []
    for line in lines[start + 1:end]:
        content = line.rstrip("\r\n")
        if not content.strip() or content.lstrip().startswith("#"):
            continue
        indentation = content[:len(content) - len(content.lstrip())]
        if "\t" in indentation:
            raise HelperError(
                "Copied Meshtastic General mapping may not use tab indentation")
        child_indents.append(len(indentation))
    direct_indent = min(child_indents, default=2)
    direct_prefix = " " * direct_indent
    config_directory_rows: list[tuple[int, str, str]] = []
    for index in range(start + 1, end):
        content = lines[index].rstrip("\r\n")
        if not content.strip() or content.lstrip().startswith("#"):
            continue
        if not content.startswith(direct_prefix):
            continue
        remainder = content[direct_indent:]
        if remainder.startswith((" ", "\t")):
            continue
        if re.match(r"[\"']ConfigDirectory[\"'][ \t]*:", remainder):
            raise HelperError(
                "Copied Meshtastic config uses a quoted ConfigDirectory key")
        match = re.fullmatch(
            r"ConfigDirectory:[ \t]*(.*?)[ \t]*(?:#.*)?", remainder)
        if re.match(r"ConfigDirectory[ \t]*:", remainder):
            if match is None:
                raise HelperError(
                    "Copied Meshtastic config uses an unsupported "
                    "General.ConfigDirectory key")
            config_directory_rows.append(
                (index, direct_prefix, match.group(1)))
        elif re.match(r"[A-Za-z0-9_.-]+[ \t]*:", remainder) is None:
            raise HelperError(
                "Copied Meshtastic General must be a plain mapping")

    if len(config_directory_rows) > 1:
        raise HelperError(
            "Copied Meshtastic config has duplicate General.ConfigDirectory entries")
    if not config_directory_rows:
        return None

    index, indent, scalar = config_directory_rows[0]
    scalar = scalar.strip()
    if ((scalar.startswith('"') and scalar.endswith('"'))
            or (scalar.startswith("'") and scalar.endswith("'"))):
        scalar = scalar[1:-1]
    if not scalar:
        return None

    configured = Path(scalar)
    if configured.is_absolute():
        try:
            relative = configured.relative_to(MESHTASTIC_CONFIG_DIR)
        except ValueError as exc:
            raise HelperError(
                "General.ConfigDirectory points outside /etc/meshtasticd; "
                "candidate validation cannot safely follow it") from exc
    else:
        relative = configured
    if relative.is_absolute() or ".." in relative.parts:
        raise HelperError(
            "General.ConfigDirectory escapes the copied configuration")

    target = config_dir / relative
    try:
        resolved_root = config_dir.resolve(strict=True)
        resolved_target = target.resolve(strict=True)
        resolved_target.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise HelperError(
            "General.ConfigDirectory is missing or escapes the copied configuration") from exc
    if not resolved_target.is_dir():
        raise HelperError(
            "General.ConfigDirectory is not a directory in the copied configuration")

    newline = "\r\n" if lines[index].endswith("\r\n") else "\n"
    lines[index] = (
        indent + "ConfigDirectory: " + json.dumps(str(resolved_target)) + newline)
    try:
        config.write_text("".join(lines), encoding="utf-8")
    except OSError as exc:
        raise HelperError(
            "Could not isolate the copied Meshtastic ConfigDirectory") from exc
    return resolved_target


def _normalize_config_mac(value: str) -> str | None:
    scalar = value.strip()
    if ((scalar.startswith('"') and scalar.endswith('"'))
            or (scalar.startswith("'") and scalar.endswith("'"))):
        scalar = scalar[1:-1]
    digits = scalar.replace(":", "")
    if re.fullmatch(r"[0-9A-Fa-f]{12}", digits) is None:
        return None
    return ":".join(
        digits[index:index + 2].upper() for index in range(0, 12, 2))


def _rewrite_general_mac_text(text: str, mac: str) -> tuple[str, bool]:
    """Render one conservative, semantics-preserving General.MACAddress pin."""
    if _normalize_config_mac(mac) != mac:
        raise HelperError("Validated Meshtastic MAC is malformed")
    lines = text.splitlines(keepends=True)
    general_rows: list[int] = []
    risky_yaml = False
    root_mapping = True
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        stripped = content.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _yaml_uses_indirection_or_documents(content):
            risky_yaml = True
        if re.match(r"^[\"']General[\"'][ \t]*:", content):
            raise HelperError(
                "Automatic MAC pinning does not support quoted General keys")
        if re.match(r"^General[ \t]*:", content):
            if not re.fullmatch(r"General:[ \t]*(?:#.*)?", content):
                raise HelperError(
                    "Automatic MAC pinning requires General to be a block mapping")
            general_rows.append(index)
        if (content and not content[0].isspace()
                and (stripped.startswith(("-", "{", "["))
                     or re.match(
                         r"^[A-Za-z0-9_.-]+[ \t]*:", content) is None)):
            root_mapping = False

    if len(general_rows) > 1:
        raise HelperError(
            "Automatic MAC pinning refuses duplicate General mappings")

    if not general_rows:
        if risky_yaml or not root_mapping:
            raise HelperError(
                "General.MACAddress is absent and config.yaml uses YAML features "
                "the helper cannot edit safely; set General.MACAddress manually")
        newline = "\r\n" if "\r\n" in text and "\n" in text else "\n"
        prefix = text
        if prefix and not prefix.endswith(("\n", "\r")):
            prefix += newline
        if prefix and not prefix.endswith(newline * 2):
            prefix += newline
        return (
            prefix + "General:" + newline
            + "  MACAddress: \"" + mac + "\"" + newline,
            True,
        )

    start = general_rows[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        content = lines[index].rstrip("\r\n")
        if content and not content[0].isspace() and not content.startswith("#"):
            end = index
            break

    child_indents: list[int] = []
    for line in lines[start + 1:end]:
        content = line.rstrip("\r\n")
        if not content.strip() or content.lstrip().startswith("#"):
            continue
        leading = content[:len(content) - len(content.lstrip(" "))]
        if "\t" in content[:len(content) - len(content.lstrip())]:
            raise HelperError(
                "Automatic MAC pinning does not support tab-indented General settings")
        if leading:
            child_indents.append(len(leading))
    direct_indent = min(child_indents, default=2)
    direct_prefix = " " * direct_indent
    address_rows: list[tuple[int, str]] = []
    source_rows: list[int] = []
    for index in range(start + 1, end):
        content = lines[index].rstrip("\r\n")
        if not content.startswith(direct_prefix):
            continue
        remainder = content[direct_indent:]
        if remainder.startswith((" ", "\t")):
            continue
        if not remainder.strip() or remainder.lstrip().startswith("#"):
            continue
        if re.match(r"[\"'](?:MACAddress|MACAddressSource)[\"'][ \t]*:", remainder):
            raise HelperError(
                "Automatic MAC pinning does not support quoted MAC keys")
        if re.match(r"<<[ \t]*:", remainder):
            risky_yaml = True
        match = re.fullmatch(
            r"MACAddress:[ \t]*(.*?)[ \t]*(?:#.*)?", remainder)
        if re.match(r"MACAddress[ \t]*:", remainder) and match is None:
            raise HelperError(
                "Automatic MAC pinning requires a plain MACAddress key")
        if match:
            address_rows.append((index, match.group(1)))
        source_match = re.fullmatch(
            r"MACAddressSource:[ \t]*.*?[ \t]*(?:#.*)?", remainder)
        if (re.match(r"MACAddressSource[ \t]*:", remainder)
                and source_match is None):
            raise HelperError(
                "Automatic MAC pinning requires a plain MACAddressSource key")
        if source_match:
            source_rows.append(index)
        if re.match(r"[A-Za-z0-9_.-]+[ \t]*:", remainder) is None:
            raise HelperError(
                "Automatic MAC pinning requires General to be a plain mapping")

    if len(address_rows) > 1 or len(source_rows) > 1:
        raise HelperError(
            "Automatic MAC pinning refuses duplicate MAC settings")
    if address_rows and source_rows:
        raise HelperError(
            "General.MACAddress and General.MACAddressSource are both set")
    if address_rows:
        configured = _normalize_config_mac(address_rows[0][1])
        if configured is None or configured != mac:
            raise HelperError(
                "Configured General.MACAddress does not match the daemon's "
                "effective MAC")
        return text, False
    if risky_yaml:
        raise HelperError(
            "General.MACAddress is absent and config.yaml uses YAML aliases or "
            "document features the helper cannot edit safely; set it manually")

    newline = "\r\n" if lines[start].endswith("\r\n") else "\n"
    replacement = direct_prefix + "MACAddress: \"" + mac + "\"" + newline
    if source_rows:
        lines[source_rows[0]] = replacement
    else:
        lines.insert(start + 1, replacement)
    return "".join(lines), True


def _fragment_contains_general(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise HelperError("Meshtastic config fragment is unreadable: " + str(path)) from exc
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _yaml_uses_indirection_or_documents(line):
            raise HelperError(
                "Automatic MAC pinning does not support YAML aliases or "
                "document features in " + str(path))
        if re.match(r"^[\"']General[\"'][ \t]*:", line):
            raise HelperError(
                "Automatic MAC pinning does not support quoted General keys in "
                + str(path))
        if re.match(r"^General[ \t]*:", line):
            if not re.fullmatch(r"General:[ \t]*(?:#.*)?", line):
                raise HelperError(
                    "Automatic MAC pinning requires block General mappings in "
                    + str(path))
            return True
        if (line and not line[0].isspace()
                and re.match(r"^[A-Za-z0-9_.-]+[ \t]*:", line) is None):
            raise HelperError(
                "Automatic MAC pinning requires config fragments to use plain "
                "root mappings: " + str(path))
    return False


def _render_pinned_config(
        config: Path, mac: str, fragment_dir: Path | None,
        ) -> tuple[str, bool]:
    try:
        text = config.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise HelperError("Meshtastic config.yaml is unreadable") from exc
    rendered, changed = _rewrite_general_mac_text(text, mac)
    if changed and fragment_dir is not None:
        try:
            fragments = list(fragment_dir.iterdir())
        except OSError as exc:
            raise HelperError("Meshtastic ConfigDirectory is unreadable") from exc
        for fragment in fragments:
            if fragment.suffix == ".yaml" and _fragment_contains_general(fragment):
                raise HelperError(
                    "General.MACAddress is absent from config.yaml, but a "
                    "ConfigDirectory fragment also defines General; move the "
                    "General settings into config.yaml before updating")
    return rendered, changed


def _validated_live_config(config: Path):
    try:
        info = config.lstat()
        parent_info = config.parent.lstat()
    except OSError as exc:
        raise HelperError("Live Meshtastic config.yaml is missing") from exc
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(parent_info.st_mode)
            or not stat.S_ISDIR(parent_info.st_mode)
            or info.st_uid != 0 or info.st_mode & 0o022
            or parent_info.st_uid != 0 or parent_info.st_mode & 0o022):
        raise HelperError("Live Meshtastic config.yaml is unsafe")
    return info


def _atomic_replace_live_config(
        payload: bytes, *, uid: int, gid: int, mode: int,
        failure_message: str) -> None:
    config = MESHTASTIC_CONFIG_DIR / "config.yaml"
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=".config.yaml.wdg-", dir=config.parent)
        temporary = Path(name)
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config)
        temporary = None
        directory_fd = os.open(
            config.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise HelperError(failure_message) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _pin_live_mac(mac: str) -> None:
    """Atomically pin the pre-install MAC in the live primary config."""
    config = MESHTASTIC_CONFIG_DIR / "config.yaml"
    info = _validated_live_config(config)
    rendered, changed = _render_pinned_config(config, mac, None)
    if not changed:
        raise HelperError(
            "Meshtastic MAC pin preflight no longer matches live config.yaml")
    _atomic_replace_live_config(
        rendered.encode("utf-8"), uid=info.st_uid, gid=info.st_gid,
        mode=stat.S_IMODE(info.st_mode),
        failure_message="Could not atomically pin General.MACAddress")


def _adoption_config_rollback_copy(
        snapshot: Path) -> tuple[bytes, int, int, int]:
    """Load the exact primary config copied before an adoption MAC pin."""
    source = (
        snapshot / "state" / _state_backup_key(MESHTASTIC_CONFIG_DIR)
        / "config.yaml")
    try:
        source_info = source.lstat()
        payload = source.read_bytes()
    except OSError as exc:
        raise HelperError("Adoption config rollback copy is unavailable") from exc
    if stat.S_ISLNK(source_info.st_mode) or not stat.S_ISREG(source_info.st_mode):
        raise HelperError("Adoption config rollback copy is unsafe")
    return (
        payload, source_info.st_uid, source_info.st_gid,
        stat.S_IMODE(source_info.st_mode))


def _restore_adoption_config(backup: tuple[bytes, int, int, int]) -> None:
    """Atomically restore the saved primary config after failed adoption."""
    payload, uid, gid, mode = backup
    _validated_live_config(MESHTASTIC_CONFIG_DIR / "config.yaml")
    _atomic_replace_live_config(
        payload, uid=uid, gid=gid, mode=mode,
        failure_message="Could not roll back the adoption MAC pin")


def _live_config_matches(backup: tuple[bytes, int, int, int]) -> bool:
    payload, uid, gid, mode = backup
    config = MESHTASTIC_CONFIG_DIR / "config.yaml"
    info = _validated_live_config(config)
    try:
        actual = config.read_bytes()
    except OSError as exc:
        raise HelperError("Live Meshtastic config.yaml is unreadable") from exc
    return (
        actual == payload and info.st_uid == uid and info.st_gid == gid
        and stat.S_IMODE(info.st_mode) == mode)


def _start_output_capture(process: subprocess.Popen):
    if process.stdout is None:
        raise HelperError("meshtasticd-wdg startup output was not captured")
    captured = bytearray()
    state: dict[str, Any] = {"truncated": False, "error": None}

    def drain() -> None:
        try:
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    return
                remaining = STARTUP_OUTPUT_LIMIT - len(captured)
                if remaining > 0:
                    captured.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    state["truncated"] = True
        except Exception as exc:  # noqa: BLE001 - reported after process reap
            state["error"] = exc

    thread = threading.Thread(
        target=drain, name="meshtastic-validation-output", daemon=True)
    thread.start()
    return thread, captured, state


def _finish_output_capture(capture) -> tuple[bytes, bool]:
    thread, captured, state = capture
    thread.join(timeout=5.0)
    if thread.is_alive():
        raise HelperError("meshtasticd-wdg startup output did not close")
    if state["error"] is not None:
        raise HelperError("Could not read meshtasticd-wdg startup output") from state[
            "error"]
    return bytes(captured), bool(state["truncated"])


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group_exit(
        process_group: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _process_group_exists(process_group):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))
    return True


def _terminate_candidate(process: subprocess.Popen) -> None:
    """Terminate the whole candidate group even if its leader exited first."""
    leader_running = process.poll() is None
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    leader_reaped = False
    term_expired = False
    try:
        process.wait(timeout=5.0 if leader_running else 1.0)
        leader_reaped = True
    except subprocess.TimeoutExpired:
        term_expired = True
    group_exited = (
        False if term_expired
        else _wait_for_process_group_exit(process.pid, 5.0))
    if not group_exited:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if not leader_reaped:
            try:
                process.wait(timeout=5.0)
                leader_reaped = True
            except subprocess.TimeoutExpired as exc:
                raise HelperError(
                    "meshtasticd-wdg candidate leader did not exit after "
                    "SIGKILL") from exc
        if not _wait_for_process_group_exit(process.pid, 5.0):
            raise HelperError(
                "meshtasticd-wdg candidate process group survived SIGKILL")
    if not leader_reaped:
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired as exc:
            raise HelperError(
                "meshtasticd-wdg candidate leader could not be reaped") from exc


def _candidate_dry_run(
        backup: Path, *, pinned_mac: str | None = None) -> dict[str, Any]:
    """Boot the installed candidate against a disposable state copy."""
    binary = _validate_installed_binary()
    uid, gid, supplementary_groups = _meshtasticd_credentials()
    candidate = None
    try:
        (candidate, fsdir, config, socket_path,
         fragment_dir) = _copy_candidate_state(backup, uid, gid)
        config_dir = config.parent
        if pinned_mac is not None:
            rendered, changed = _render_pinned_config(
                config, pinned_mac, fragment_dir)
            if not changed:
                raise HelperError(
                    "Meshtastic MAC pin preflight no longer matches the backup")
            config.write_text(rendered, encoding="utf-8")
        critical_before = _critical_state_snapshot(fsdir, config_dir)
        environment = {
            "HOME": str(candidate),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "TMPDIR": str(candidate),
            "MESHTASTIC_WDG_SOCKET": str(socket_path),
            "MESHTASTIC_WDG_ALLOWED_UID": "0",
            "MESHTASTIC_WDG_DISABLE_BLUETOOTH": "1",
        }
        command = [
            str(FLOCK_PATH), "-n", "-E", "75", str(RADIO_LOCK_PATH),
            str(binary), "--port=0",
            "--fsdir=" + str(fsdir),
            "--config=" + str(config),
        ]
        process = None
        capture = None
        status = None
        health_error = None
        output = b""
        truncated = False
        try:
            process = subprocess.Popen(
                command,
                cwd=str(candidate),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
                user=uid,
                group=gid,
                extra_groups=supplementary_groups,
                umask=0o077,
            )
            capture = _start_output_capture(process)
            try:
                status = _health_check(socket_path, process=process)
            except Exception as exc:  # noqa: BLE001 - inspect state after shutdown
                health_error = exc
        finally:
            termination_error = None
            if process is not None:
                try:
                    _terminate_candidate(process)
                except Exception as exc:  # noqa: BLE001 - finish pipe cleanup
                    termination_error = exc
            if capture is not None:
                output, truncated = _finish_output_capture(capture)
            if termination_error is not None:
                raise termination_error

        critical_after = _critical_state_snapshot(fsdir, config_dir)
        if critical_after != critical_before:
            raise HelperError(
                "meshtasticd-wdg candidate modified critical identity or channel "
                "state during its isolated dry-run") from health_error
        if health_error is not None:
            raise health_error
        if status is None:
            raise HelperError("meshtasticd-wdg candidate returned no health status")
        semantic = _semantic_status_snapshot(status)
        effective_mac = _parse_effective_mac(
            output, truncated=truncated, semantic=semantic)
        if pinned_mac is not None and effective_mac != pinned_mac:
            raise HelperError(
                "meshtasticd-wdg candidate ignored the validated MAC pin")
        _rendered, pin_required = _render_pinned_config(
            config, effective_mac, fragment_dir)
        return {
            "semantic": semantic,
            "effective_mac": effective_mac,
            "mac_pin_required": pin_required,
        }
    finally:
        if candidate is not None:
            try:
                shutil.rmtree(candidate)
            except OSError as exc:
                raise HelperError(
                    "Could not clean the Meshtastic validation directory") from exc


def _verify_live_state(
        expected_semantic: dict[str, Any],
        critical_before: dict[str, Any]) -> dict[str, Any]:
    """Require the live daemon to preserve the pre-install baseline."""
    status = _health_check()
    actual_semantic = _semantic_status_snapshot(status)
    if actual_semantic != expected_semantic:
        raise HelperError(
            "Live meshtasticd-wdg identity or channel semantics differ from "
            "the authoritative pre-install baseline")
    critical_after = _critical_state_snapshot(
        _resolve_state_fsdir(MESHTASTIC_STATE_DIR), MESHTASTIC_CONFIG_DIR)
    if critical_after != critical_before:
        raise HelperError(
            "Live meshtasticd-wdg modified critical identity or channel state")
    return status


def _cache_installed_release(prepared: Any) -> bool:
    """Atomically seed the rollback cache; return whether a directory was added."""
    _secure_root_directory(INSTALLED_CACHE)
    target = INSTALLED_CACHE / prepared.tag
    if target.exists():
        validator = _load_validator()
        cached = validator.validate_prepared_release(
            target, expected_tag=prepared.tag, require_secure=True,
            check_host=False)
        if (cached.package_version != prepared.package_version
                or cached.manifest != prepared.manifest):
            raise HelperError(
                "Installed-package cache differs from the verified release")
        return False

    staging_root = Path(tempfile.mkdtemp(prefix=".installed-", dir=INSTALLED_CACHE))
    staging = staging_root / prepared.tag
    try:
        shutil.copytree(
            prepared.directory, staging, copy_function=shutil.copy2)
        os.chmod(staging, 0o700)
        for child in staging.iterdir():
            os.chmod(child, 0o600)
        validator = _load_validator()
        cached = validator.validate_prepared_release(
            staging, expected_tag=prepared.tag, require_secure=True,
            check_host=False)
        if (cached.package_version != prepared.package_version
                or cached.manifest != prepared.manifest):
            raise HelperError(
                "Copied installed-package cache differs from the verified release")
        os.rename(staging, target)
        return True
    except FileExistsError as exc:
        raise HelperError(
            "Installed-package cache changed during adoption") from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _current_state_fingerprints() -> dict[str, Any]:
    return {str(path): _tree_fingerprint(path) for path in STATE_PATHS}


def _candidate_dry_run_preserving_live_state(
        backup: Path, expected_fingerprints: dict[str, Any],
        presence: dict[str, bool], *, pinned_mac: str | None = None,
        ) -> dict[str, Any]:
    """Run a candidate and reject or restore any writes to live state.

    The candidate runs as the real ``meshtasticd`` account so it can exercise
    SPI/GPIO exactly like the service.  That account can also write the live
    state directory.  A buggy candidate must therefore be treated as capable
    of ignoring ``--fsdir`` until its isolated boot succeeds.
    """
    candidate_error: Exception | None = None
    candidate_result: dict[str, Any] | None = None
    try:
        if pinned_mac is None:
            candidate_result = _candidate_dry_run(backup)
        else:
            candidate_result = _candidate_dry_run(
                backup, pinned_mac=pinned_mac)
    except Exception as exc:  # noqa: BLE001 - inspect live state before propagating
        candidate_error = exc

    inspection_error: Exception | None = None
    try:
        changed = _current_state_fingerprints() != expected_fingerprints
    except Exception as exc:  # noqa: BLE001 - restore conservatively
        inspection_error = exc
        changed = True

    if not changed:
        if candidate_error is not None:
            raise candidate_error
        if candidate_result is None:
            raise HelperError("Meshtastic candidate returned no validation result")
        return candidate_result

    if candidate_error is None:
        candidate_error = HelperError(
            "Meshtastic candidate modified or obscured live state during "
            "isolated validation")

    try:
        _restore_state_from_backup(
            backup, presence, expected_fingerprints)
        restored = _current_state_fingerprints()
        if restored != expected_fingerprints:
            raise HelperError(
                "restored state does not match the pre-candidate snapshot")
    except Exception as restore_error:  # noqa: BLE001 - preserve both errors
        inspection_detail = (
            "; live-state inspection also failed: " + str(inspection_error)
            if inspection_error is not None else "")
        raise CandidateStateRestoreError(
            "Meshtastic candidate validation failed ("
            + str(candidate_error)
            + "); it changed or obscured live state and automatic restoration "
            "failed (" + str(restore_error) + ")"
            + inspection_detail + ". Protected evidence retained at "
            + str(backup),
            backup,
        ) from candidate_error

    raise HelperError(
        "Meshtastic candidate validation failed and its live-state changes "
        "were restored exactly: " + str(candidate_error)
    ) from candidate_error


def _require_only_mac_pin_changed(
        before: dict[str, Any], after: dict[str, Any]) -> None:
    """Prove an adoption pin changed only config.yaml file contents."""
    state_key = str(MESHTASTIC_STATE_DIR)
    config_key = str(MESHTASTIC_CONFIG_DIR)
    if set(before) != {config_key, state_key} or set(after) != set(before):
        raise HelperError("Meshtastic state changed unexpectedly while pinning the MAC")
    if before.get(state_key) != after.get(state_key):
        raise HelperError("Meshtastic state changed unexpectedly while pinning the MAC")
    if not isinstance(before.get(config_key), list) or not isinstance(
            after.get(config_key), list):
        raise HelperError("Meshtastic configuration changed unexpectedly while pinning the MAC")

    old_records = {
        record.get("path"): record for record in before[config_key]
        if isinstance(record, dict) and isinstance(record.get("path"), str)
    }
    new_records = {
        record.get("path"): record for record in after[config_key]
        if isinstance(record, dict) and isinstance(record.get("path"), str)
    }
    if (len(old_records) != len(before[config_key])
            or len(new_records) != len(after[config_key])
            or set(old_records) != set(new_records)
            or "config.yaml" not in old_records):
        raise HelperError("Meshtastic configuration changed unexpectedly while pinning the MAC")
    old_config = dict(old_records.pop("config.yaml"))
    new_config = dict(new_records.pop("config.yaml"))
    old_digest = old_config.pop("sha256", None)
    new_digest = new_config.pop("sha256", None)
    if (old_records != new_records or old_config != new_config
            or not isinstance(old_digest, str)
            or not isinstance(new_digest, str) or old_digest == new_digest):
        raise HelperError("Meshtastic configuration changed unexpectedly while pinning the MAC")


def _snapshot_live_state_for_adoption() -> tuple[Path, dict[str, Any]]:
    """Make a stable, disposable copy without exposing or changing live state."""
    parent = _secure_validation_root()
    snapshot = Path(tempfile.mkdtemp(prefix="adopt-state-", dir=parent))
    try:
        os.chown(snapshot, 0, 0)
        os.chmod(snapshot, 0o700)
        before = _current_state_fingerprints()
        presence = _copy_state_to_backup(snapshot)
        if not all(presence.get(_state_backup_key(path)) for path in STATE_PATHS):
            raise HelperError(
                "Meshtastic config and state must both exist before adoption")
        if _current_state_fingerprints() != before:
            raise HelperError(
                "Meshtastic config or state changed while preparing adoption; "
                "keep both services stopped and retry")
        return snapshot, before
    except Exception:
        shutil.rmtree(snapshot, ignore_errors=True)
        raise


def _require_quiescent_services() -> None:
    services = _service_snapshot()
    running = [
        state.get("service", TARGET_SERVICES[target])
        for target, state in services.items()
        if state.get("load_state") != "not-found"
        and state.get("active_state") not in ("inactive", "failed")
    ]
    if running:
        raise HelperError(
            "Stop both Meshtastic services before adopting the installed "
            "package; still running: " + ", ".join(running))


def _adopt_installed(tag: str) -> dict[str, Any]:
    """Validate an already-installed fork and seed its rollback package."""
    _require_root()
    if not TAG_RE.fullmatch(tag):
        raise HelperError("Expected Meshtastic tag vX.Y.Z-wdg.N")
    _secure_root_directory(CACHE_ROOT)
    validator = _load_validator()
    prepared = _prepare_exact_release(tag, validator)
    with _lock_transaction():
        installed_version = _installed_version()
        if installed_version != prepared.package_version:
            raise HelperError(
                "Installed meshtasticd-wdg version does not match "
                f"{tag}: expected {prepared.package_version}, found "
                f"{installed_version or 'no installed package'}")
        _require_quiescent_services()
        validator.validate_installed_package_payload(
            prepared.package_path, prepared.manifest)
        snapshot: Path | None = None
        pre_pin_fingerprints: dict[str, Any] | None = None
        expected_pinned_fingerprints: dict[str, Any] | None = None
        rollback_config: tuple[bytes, int, int, int] | None = None
        expected_pinned_config: tuple[bytes, int, int, int] | None = None
        pin_attempted = False
        cache_created = False
        try:
            snapshot, live_fingerprints = _snapshot_live_state_for_adoption()
            pre_pin_fingerprints = live_fingerprints
            snapshot_presence = {
                _state_backup_key(path): True for path in STATE_PATHS}
            baseline = _candidate_dry_run_preserving_live_state(
                snapshot, live_fingerprints, snapshot_presence)
            semantic, effective_mac, pin_required = _validation_baseline({
                "semantic_baseline": baseline.get("semantic"),
                "effective_mac": baseline.get("effective_mac"),
                "mac_pin_required": baseline.get("mac_pin_required"),
            })
            if _current_state_fingerprints() != live_fingerprints:
                raise HelperError(
                    "Meshtastic config or state changed during adoption; "
                    "nothing was cached")
            if pin_required:
                rollback_config = _adoption_config_rollback_copy(snapshot)
                pinned = _candidate_dry_run_preserving_live_state(
                    snapshot, live_fingerprints, snapshot_presence,
                    pinned_mac=effective_mac)
                if (pinned.get("semantic") != semantic
                        or pinned.get("effective_mac") != effective_mac
                        or pinned.get("mac_pin_required") is not False):
                    raise HelperError(
                        "The verified General.MACAddress pin changed Meshtastic "
                        "identity or channel semantics; set the MAC manually")
                try:
                    original_text = rollback_config[0].decode("utf-8", "strict")
                except UnicodeDecodeError as exc:
                    raise HelperError(
                        "Meshtastic config.yaml is not valid UTF-8") from exc
                rendered, changed = _rewrite_general_mac_text(
                    original_text, effective_mac)
                if not changed:
                    raise HelperError(
                        "Meshtastic MAC pin preflight no longer matches the "
                        "adoption snapshot")
                expected_pinned_config = (
                    rendered.encode("utf-8"), *rollback_config[1:])
                pin_attempted = True
                _pin_live_mac(effective_mac)
                after_pin = _current_state_fingerprints()
                _require_only_mac_pin_changed(live_fingerprints, after_pin)
                if not _live_config_matches(expected_pinned_config):
                    raise HelperError(
                        "Meshtastic config changed while applying the verified "
                        "MAC pin")
                live_fingerprints = after_pin
                expected_pinned_fingerprints = after_pin
            if _current_state_fingerprints() != live_fingerprints:
                raise HelperError(
                    "Meshtastic config or state changed during adoption; "
                    "nothing was cached")
            _require_quiescent_services()
            cache_created = _cache_installed_release(prepared)
            if snapshot is not None:
                try:
                    shutil.rmtree(snapshot)
                    snapshot = None
                except OSError as exc:
                    raise HelperError(
                        "Could not clean the Meshtastic adoption snapshot"
                    ) from exc
        except Exception as adoption_error:
            rollback_errors: list[str] = []
            if cache_created:
                try:
                    shutil.rmtree(INSTALLED_CACHE / prepared.tag)
                except OSError as exc:
                    rollback_errors.append("cache cleanup failed: " + str(exc))
            if pin_attempted and rollback_config is not None:
                try:
                    current = _current_state_fingerprints()
                    if current != pre_pin_fingerprints:
                        if (expected_pinned_fingerprints is not None
                                and current != expected_pinned_fingerprints):
                            raise HelperError(
                                "live state changed after the MAC pin")
                        if (expected_pinned_config is None
                                or not _live_config_matches(
                                    expected_pinned_config)):
                            raise HelperError(
                                "live config no longer matches the MAC pin")
                        _restore_adoption_config(rollback_config)
                        if _current_state_fingerprints() != pre_pin_fingerprints:
                            raise HelperError(
                                "restored config does not match the pre-pin state")
                except Exception as exc:  # noqa: BLE001
                    rollback_errors.append("MAC pin rollback failed: " + str(exc))
            if (snapshot is not None
                    and not isinstance(adoption_error,
                                       CandidateStateRestoreError)):
                try:
                    shutil.rmtree(snapshot)
                except OSError as exc:
                    rollback_errors.append("snapshot cleanup failed: " + str(exc))
            if rollback_errors:
                raise HelperError(
                    f"Adoption failed ({adoption_error}); rollback was "
                    "incomplete: " + "; ".join(rollback_errors)
                ) from adoption_error
            raise
    return {
        "action": "adopt-installed",
        "tag": tag,
        "package_version": prepared.package_version,
        "health": "ready",
    }


def _record_last_backup(backup: Path) -> None:
    if not re.fullmatch(r"\d{8}T\d{6}Z-[A-Za-z0-9_-]+", backup.name):
        raise HelperError("Generated backup name is invalid")
    temp = BACKUP_ROOT / (".last-" + str(os.getpid()))
    descriptor = os.open(
        temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(backup.name + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, LAST_BACKUP)


def _prepare_first_tag(tag: str) -> dict[str, Any]:
    """Validate a fixed root-only inbox, then seal it in the release cache."""
    _require_root()
    if not TAG_RE.fullmatch(tag):
        raise HelperError("Expected Meshtastic tag vX.Y.Z-wdg.N")
    _secure_root_directory(CACHE_ROOT)
    _secure_root_directory(FIRST_INSTALL_INBOX)
    validator = _load_validator()
    with _lock_transaction():
        if _installed_version() is not None:
            raise HelperError(
                "meshtasticd-wdg is already installed; use the in-game updater")
        inbox = FIRST_INSTALL_INBOX / tag
        if inbox.exists() or inbox.is_symlink():
            prepared = validator.validate_prepared_release(
                inbox, expected_tag=tag, require_secure=True, check_host=True)
            target = CACHE_ROOT / tag
            if target.exists() or target.is_symlink():
                cached = validator.validate_prepared_release(
                    target, expected_tag=tag, require_secure=True,
                    check_host=True)
                if _release_identity(cached) != _release_identity(prepared):
                    raise HelperError(
                        "Protected release cache differs from first-install inbox")
                prepared = cached
            else:
                staging_root = Path(tempfile.mkdtemp(
                    prefix=".first-install-", dir=CACHE_ROOT))
                staging = staging_root / tag
                try:
                    shutil.copytree(
                        inbox, staging, copy_function=shutil.copy2)
                    os.chmod(staging, 0o700)
                    for child in staging.iterdir():
                        os.chmod(child, 0o600)
                    checked = validator.validate_prepared_release(
                        staging, expected_tag=tag, require_secure=True,
                        check_host=True)
                    if _release_identity(checked) != _release_identity(prepared):
                        raise HelperError(
                            "Copied first-install release failed verification")
                    os.rename(staging, target)
                    _fsync_directory(CACHE_ROOT)
                finally:
                    shutil.rmtree(staging_root, ignore_errors=True)
                prepared = validator.validate_prepared_release(
                    target, expected_tag=tag, require_secure=True,
                    check_host=True)
        else:
            # Once a tag is published, first installs can resolve it directly.
            # Pre-publication hardware testing uses the fixed inbox above.
            prepared = _prepare_exact_release(tag, validator)
        checked = validator.validate_prepared_release(
            prepared.directory, expected_tag=tag, require_secure=True,
            check_host=True)
        if _release_identity(checked) != _release_identity(prepared):
            raise HelperError("First-install release changed during validation")
    return {
        "action": "prepare-first-tag",
        "tag": tag,
        "package_version": prepared.package_version,
        "package_path": str(prepared.package_path),
    }


def _install_tag(tag: str) -> dict[str, Any]:
    _require_root()
    if not TAG_RE.fullmatch(tag):
        raise HelperError("Expected Meshtastic tag vX.Y.Z-wdg.N")
    _secure_root_directory(CACHE_ROOT)
    validator = _load_validator()
    # The unprivileged UI passes only an immutable, strictly validated release
    # tag. No caller-controlled path or package bytes cross the sudo boundary.
    prepared = _prepare_exact_release(tag, validator)
    with _lock_transaction():
        services = _service_snapshot()
        backup = None
        metadata = None
        package_mutation_started = False
        try:
            for target, service in TARGET_SERVICES.items():
                if services[target].get("load_state") != "not-found":
                    _run(["systemctl", "stop", service], timeout=30)
            backup, metadata = _create_backup(
                tag, prepared, validator, services)
            # All later comparisons use the private on-disk record, rather
            # than a value that only existed in this process before apt.
            metadata = _read_private_json(backup / "transaction.json")
            _validate_transaction_metadata(metadata, backup, validator)
            if metadata["new_release"] != _release_identity(prepared):
                raise HelperError(
                    "Candidate release differs from transaction metadata")
            baseline_semantic, effective_mac, pin_required = (
                _validation_baseline(metadata))
            package_mutation_started = True
            _run([
                "apt-get", "-y", "--no-install-recommends", "install",
                str(prepared.package_path),
            ], timeout=300)
            if _installed_version() != prepared.package_version:
                raise HelperError("Installed package version does not match the release")
            prepared = validator.validate_prepared_release(
                prepared.directory, expected_tag=tag, require_secure=True,
                check_host=True)
            if metadata["new_release"] != _release_identity(prepared):
                raise HelperError(
                    "Candidate release identity changed during installation")
            validator.validate_installed_package_payload(
                prepared.package_path, prepared.manifest)
            if not _state_unchanged(metadata):
                raise HelperError("Package installation modified Meshtastic identity/state")
            _run([
                "systemd-sysusers",
                "/usr/lib/sysusers.d/meshtasticd-wdg.conf",
            ], timeout=30)
            _run([
                "systemd-tmpfiles", "--create",
                "/usr/lib/tmpfiles.d/meshtasticd-wdg.conf",
            ], timeout=30)
            config = WDG_POLICY_PATH
            if config.is_symlink() or not config.is_file():
                raise HelperError("Meshtastic WDG policy is missing or unsafe; rerun setup.sh")
            try:
                meshtastic_gid = grp.getgrnam("meshtasticd").gr_gid
            except KeyError as exc:
                raise HelperError("meshtasticd system group was not created") from exc
            os.chown(config, 0, meshtastic_gid)
            os.chmod(config, 0o640)
            candidate = _candidate_dry_run(
                backup, pinned_mac=effective_mac if pin_required else None)
            if (candidate["semantic"] != baseline_semantic
                    or candidate["effective_mac"] != effective_mac):
                raise HelperError(
                    "New meshtasticd-wdg candidate identity or channel semantics "
                    "differ from the authoritative pre-install baseline")
            if pin_required:
                _pin_live_mac(effective_mac)
            _run(["systemctl", "daemon-reload"], timeout=30)
            live_critical = _critical_state_snapshot(
                _resolve_state_fsdir(MESHTASTIC_STATE_DIR),
                MESHTASTIC_CONFIG_DIR)
            _run(["systemctl", "start", TARGET_SERVICES["wdg"]], timeout=45)
            _verify_live_state(baseline_semantic, live_critical)
            _set_enabled(TARGET_SERVICES["wdg"], True)
            _set_enabled(TARGET_SERVICES["stock"], False)
            _cache_installed_release(prepared)
            _record_last_backup(backup)
        except Exception as install_error:
            try:
                if (package_mutation_started
                        and backup is not None and metadata is not None):
                    _restore_transaction(backup, metadata, validator)
                    outcome = "was rolled back"
                else:
                    _restore_services(services)
                    outcome = "was aborted before package changes; services were restored"
            except Exception as rollback_error:  # noqa: BLE001 - preserve both failures
                raise InstallTransactionError(
                    f"Meshtastic install failed ({install_error}); automatic rollback "
                    f"also failed ({rollback_error}). Backup: {backup or 'not created'}",
                    rollback_restored=False,
                    backup=backup,
                ) from install_error
            raise InstallTransactionError(
                f"Meshtastic install failed and {outcome}: {install_error}",
                rollback_restored=True,
                backup=backup,
            ) from install_error
    return {
        "action": "install-tag",
        "tag": tag,
        "package_version": prepared.package_version,
        "backup": str(backup),
        "service": TARGET_SERVICES["wdg"],
        "health": "ready",
    }


def _last_backup_directory() -> Path:
    _secure_root_directory(BACKUP_ROOT)
    if LAST_BACKUP.is_symlink() or not LAST_BACKUP.is_file():
        raise HelperError("No completed Meshtastic transaction is available to roll back")
    info = LAST_BACKUP.stat()
    if info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o077:
        raise HelperError("Meshtastic rollback pointer is not private")
    name = LAST_BACKUP.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"\d{8}T\d{6}Z-[A-Za-z0-9_-]+", name):
        raise HelperError("Meshtastic rollback pointer is invalid")
    backup = BACKUP_ROOT / name
    if backup.parent != BACKUP_ROOT or backup.is_symlink() or not backup.is_dir():
        raise HelperError("Meshtastic rollback backup is missing or unsafe")
    return backup


def _rollback() -> dict[str, Any]:
    _require_root()
    validator = _load_validator()
    with _lock_transaction():
        backup = _last_backup_directory()
        metadata = _read_private_json(backup / "transaction.json")
        _restore_transaction(backup, metadata, validator)
    return {
        "action": "rollback",
        "restored_version": metadata.get("previous_version"),
        "backup": str(backup),
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args == ["version"]:
            _json_output(helper_version=HELPER_VERSION)
            return 0
        if len(args) == 2 and args[0] == "status" and args[1] in TARGET_SERVICES:
            _json_output(**_service_status(args[1]))
            return 0
        if (len(args) == 2 and args[0] in SERVICE_ACTIONS
                and args[1] in TARGET_SERVICES):
            _json_output(**_set_service(args[0], args[1]))
            return 0
        if (len(args) == 2 and args[0] == "select-service"
                and args[1] in TARGET_SERVICES):
            _json_output(**_select_service(args[1]))
            return 0
        if len(args) == 2 and args[0] == "install-tag" and TAG_RE.fullmatch(args[1]):
            _json_output(**_install_tag(args[1]))
            return 0
        if (len(args) == 2 and args[0] == "prepare-first-tag"
                and TAG_RE.fullmatch(args[1])):
            _json_output(**_prepare_first_tag(args[1]))
            return 0
        if (len(args) == 2 and args[0] == "adopt-installed"
                and TAG_RE.fullmatch(args[1])):
            _json_output(**_adopt_installed(args[1]))
            return 0
        if args == ["rollback"]:
            _json_output(**_rollback())
            return 0
        raise HelperError(
            "Usage: watchdogs-meshtastic version | status/start/stop/enable/disable/"
            "select-service wdg|stock | install-tag/prepare-first-tag/"
            "adopt-installed "
            "vX.Y.Z-wdg.N | rollback")
    except InstallTransactionError as exc:
        _json_install_error(exc)
        return 2
    except (HelperError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
