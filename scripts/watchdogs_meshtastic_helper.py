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
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HELPER_VERSION = 1
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)-wdg\.(\d+)$")
TARGET_SERVICES = {
    "wdg": "meshtasticd-wdg.service",
    "stock": "meshtasticd.service",
}
SERVICE_ACTIONS = frozenset({"start", "stop", "enable", "disable"})
PACKAGE_NAME = "meshtasticd-wdg"
CACHE_ROOT = Path("/var/cache/watchdogs/meshtasticd-wdg")
INSTALLED_CACHE = CACHE_ROOT / "installed"
BACKUP_ROOT = Path("/var/backups/meshtasticd-wdg")
LAST_BACKUP = BACKUP_ROOT / "LAST_TRANSACTION"
LOCK_PATH = Path("/run/lock/watchdogs-meshtastic-update.lock")
PROTECTED_VALIDATOR = Path(
    "/usr/local/libexec/watchdogs-meshtastic-lib/meshtastic_updates.py")
WDG_SOCKET_PATH = Path("/run/meshtasticd/wdg.sock")
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
    Path("/etc/meshtasticd"),
    Path("/var/lib/meshtasticd"),
)


class HelperError(RuntimeError):
    pass


def _json_output(**values: Any) -> None:
    print(json.dumps({"ok": True, **values}, sort_keys=True))


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
    if result.returncode and not value:
        return "not-found" if prop == "LoadState" else ""
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
    return {
        "target": target,
        "service": service,
        "load_state": _systemd_property(service, "LoadState"),
        "active_state": _systemd_property(service, "ActiveState"),
        "unit_file_state": _systemd_property(service, "UnitFileState"),
        "package_version": _installed_version() if target == "wdg" else None,
    }


def _set_service(action: str, target: str) -> dict[str, Any]:
    _require_root()
    if action not in SERVICE_ACTIONS or target not in TARGET_SERVICES:
        raise HelperError("Unsupported service operation")
    _run(["systemctl", action, TARGET_SERVICES[target]], timeout=30)
    return {"action": action, **_service_status(target)}


def _lock_transaction():
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(LOCK_PATH, flags, 0o600)
    os.fchown(descriptor, 0, 0)
    os.fchmod(descriptor, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise HelperError("Another Meshtastic install or rollback is in progress") from exc
    return os.fdopen(descriptor, "a+")


def _tree_fingerprint(path: Path) -> list[dict[str, Any]] | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_dir():
        raise HelperError(str(path) + " must be a real directory")
    result: list[dict[str, Any]] = []
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


def _copy_state_to_backup(backup: Path) -> dict[str, bool]:
    presence: dict[str, bool] = {}
    state_dir = backup / "state"
    state_dir.mkdir(mode=0o700)
    for source in STATE_PATHS:
        key = source.as_posix().lstrip("/").replace("/", "-")
        exists = source.exists()
        presence[key] = exists
        if exists:
            if source.is_symlink() or not source.is_dir():
                raise HelperError(str(source) + " must be a real directory")
            _run(["cp", "-a", "--", str(source), str(state_dir / key)], timeout=120)
    return presence


def _restore_state_from_backup(backup: Path, presence: dict[str, bool]) -> None:
    state_dir = backup / "state"
    for target in STATE_PATHS:
        key = target.as_posix().lstrip("/").replace("/", "-")
        if target.is_symlink():
            raise HelperError("Refusing to replace symlinked state path " + str(target))
        if target.exists():
            if not target.is_dir():
                raise HelperError("Refusing to replace non-directory state path " + str(target))
            shutil.rmtree(target)
        if presence.get(key):
            source = state_dir / key
            if source.is_symlink() or not source.is_dir():
                raise HelperError("Backup state is missing or unsafe: " + key)
            _run(["cp", "-a", "--", str(source), str(target)], timeout=120)


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")


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
    if not isinstance(value, dict) or value.get("format") != 1:
        raise HelperError("Meshtastic transaction metadata is unsupported")
    return value


def _service_snapshot() -> dict[str, dict[str, Any]]:
    return {target: _service_status(target) for target in TARGET_SERVICES}


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


def _create_backup(tag: str, prepared: Any, validator,
                   services: dict[str, dict[str, Any]]) -> tuple[Path, dict[str, Any]]:
    _secure_root_directory(BACKUP_ROOT)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = Path(tempfile.mkdtemp(prefix=stamp + "-", dir=BACKUP_ROOT))
    os.chmod(backup, 0o700)
    previous_version = _installed_version()
    rollback_tag = None
    if previous_version is not None:
        rollback_release = _find_cached_release(previous_version, validator)
        if rollback_release is None:
            shutil.rmtree(backup)
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

    fingerprints = {str(path): _tree_fingerprint(path) for path in STATE_PATHS}
    presence = _copy_state_to_backup(backup)
    metadata = {
        "format": 1,
        "new_tag": tag,
        "new_version": prepared.package_version,
        "previous_version": previous_version,
        "rollback_tag": rollback_tag,
        "services": services,
        "state_presence": presence,
        "state_fingerprints": fingerprints,
    }
    _write_private_json(backup / "transaction.json", metadata)
    return backup, metadata


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
    _run(["systemctl", "daemon-reload"], timeout=30)
    for target, service in TARGET_SERVICES.items():
        state = snapshot.get(target, {}) if isinstance(snapshot, dict) else {}
        _set_enabled(service, state.get("unit_file_state") == "enabled")
    # Conflicting daemons cannot be active simultaneously.  Restore the one
    # that actually owned the radio, preferring the fork if an inconsistent
    # pre-transaction snapshot claimed both.
    active_target = None
    for target in ("stock", "wdg"):
        state = snapshot.get(target, {}) if isinstance(snapshot, dict) else {}
        if state.get("active_state") == "active":
            active_target = target
    for service in TARGET_SERVICES.values():
        _run(["systemctl", "stop", service], timeout=30, check=False)
    if active_target is not None:
        _run(["systemctl", "start", TARGET_SERVICES[active_target]], timeout=45)


def _restore_transaction(backup: Path, metadata: dict[str, Any], validator) -> None:
    for service in TARGET_SERVICES.values():
        _run(["systemctl", "stop", service], timeout=30, check=False)
    previous_version = metadata.get("previous_version")
    rollback_tag = metadata.get("rollback_tag")
    if previous_version is None:
        _run(["dpkg", "--purge", PACKAGE_NAME], timeout=120, check=False)
    else:
        rollback_dir = backup / "rollback-release" / str(rollback_tag)
        if not isinstance(rollback_tag, str) or not TAG_RE.fullmatch(rollback_tag):
            raise HelperError("Rollback package metadata is invalid")
        prepared = validator.validate_prepared_release(
            rollback_dir, expected_tag=rollback_tag, require_secure=True,
            check_host=False)
        if prepared.package_version != previous_version:
            raise HelperError("Rollback package version does not match the transaction")
        _run([
            "apt-get", "-y", "--no-install-recommends", "install",
            str(prepared.package_path),
        ], timeout=300)
    _restore_state_from_backup(backup, metadata.get("state_presence", {}))
    _restore_services(metadata.get("services", {}))


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
        if (isinstance(reply, dict) and reply.get("type") == "reply"
                and reply.get("request_id") == request_id):
            if reply.get("ok") is not True or not isinstance(reply.get("body"), dict):
                raise HelperError("meshtasticd-wdg rejected the health request")
            return reply["body"]
    raise HelperError("meshtasticd-wdg health request timed out")


def _health_check() -> None:
    deadline = time.monotonic() + 20.0
    last_error = "socket did not appear"
    while time.monotonic() < deadline:
        if not WDG_SOCKET_PATH.exists():
            time.sleep(0.25)
            continue
        client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        client.settimeout(3.0)
        try:
            client.connect(str(WDG_SOCKET_PATH))
            hello = _socket_request(client, "install-health-hello", "hello")
            if hello.get("protocol_version") != 1:
                raise HelperError("meshtasticd-wdg exposes an incompatible WDG API")
            status = _socket_request(client, "install-health-status", "get_status")
            state = status.get("radio_status", status.get("state"))
            if state != "ready":
                raise HelperError("meshtasticd-wdg radio is not ready")
            return
        except (OSError, HelperError) as exc:
            last_error = str(exc)
            time.sleep(0.25)
        finally:
            client.close()
    raise HelperError("meshtasticd-wdg failed its health check: " + last_error)


def _cache_installed_release(prepared: Any) -> None:
    _secure_root_directory(INSTALLED_CACHE)
    target = INSTALLED_CACHE / prepared.tag
    if target.exists():
        validator = _load_validator()
        cached = validator.validate_prepared_release(
            target, expected_tag=prepared.tag, require_secure=True,
            check_host=False)
        if cached.package_version != prepared.package_version:
            raise HelperError("Installed-package cache contains the wrong version")
        return
    shutil.copytree(prepared.directory, target, copy_function=shutil.copy2)
    os.chmod(target, 0o700)
    for child in target.iterdir():
        os.chmod(child, 0o600)


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


def _install_tag(tag: str) -> dict[str, Any]:
    _require_root()
    if not TAG_RE.fullmatch(tag):
        raise HelperError("Expected Meshtastic tag vX.Y.Z-wdg.N")
    _secure_root_directory(CACHE_ROOT)
    validator = _load_validator()
    prepared = validator.validate_prepared_release(
        CACHE_ROOT / tag, expected_tag=tag, require_secure=True,
        check_host=True)
    with _lock_transaction():
        services = _service_snapshot()
        backup = None
        metadata = None
        try:
            for target, service in TARGET_SERVICES.items():
                if services[target].get("load_state") != "not-found":
                    _run(["systemctl", "stop", service], timeout=30)
            backup, metadata = _create_backup(
                tag, prepared, validator, services)
            _run([
                "apt-get", "-y", "--no-install-recommends", "install",
                str(prepared.package_path),
            ], timeout=300)
            if _installed_version() != prepared.package_version:
                raise HelperError("Installed package version does not match the release")
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
            config = Path("/etc/meshtasticd/wdg-portduino.yaml")
            if config.is_symlink() or not config.is_file():
                raise HelperError("Meshtastic WDG policy is missing or unsafe; rerun setup.sh")
            try:
                meshtastic_gid = grp.getgrnam("meshtasticd").gr_gid
            except KeyError as exc:
                raise HelperError("meshtasticd system group was not created") from exc
            os.chown(config, 0, meshtastic_gid)
            os.chmod(config, 0o640)
            _run(["systemctl", "daemon-reload"], timeout=30)
            _run(["systemctl", "start", TARGET_SERVICES["wdg"]], timeout=45)
            _health_check()
            _set_enabled(TARGET_SERVICES["wdg"], True)
            _set_enabled(TARGET_SERVICES["stock"], False)
            _cache_installed_release(prepared)
            _record_last_backup(backup)
        except Exception as install_error:
            try:
                if backup is not None and metadata is not None:
                    _restore_transaction(backup, metadata, validator)
                    outcome = "was rolled back"
                else:
                    _restore_services(services)
                    outcome = "was aborted before package changes; services were restored"
            except Exception as rollback_error:  # noqa: BLE001 - preserve both failures
                raise HelperError(
                    f"Meshtastic install failed ({install_error}); automatic rollback "
                    f"also failed ({rollback_error}). Backup: {backup or 'not created'}"
                ) from install_error
            raise HelperError(
                f"Meshtastic install failed and {outcome}: {install_error}"
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
        if len(args) == 2 and args[0] == "install-tag" and TAG_RE.fullmatch(args[1]):
            _json_output(**_install_tag(args[1]))
            return 0
        if args == ["rollback"]:
            _json_output(**_rollback())
            return 0
        raise HelperError(
            "Usage: watchdogs-meshtastic version | status/start/stop/enable/disable "
            "wdg|stock | install-tag vX.Y.Z-wdg.N | rollback")
    except (HelperError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
