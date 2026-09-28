"""Private controller-keyed Bluetooth phone bond metadata.

BlueZ remains the authority for cryptographic bond material.  This store only
records which already authenticated phone WDG permits on each controller.  It
never stores a passkey, long-term key, or other Bluetooth secret.
"""

from __future__ import annotations

import json
import math
import os
import pwd
import re
import secrets
import stat
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path

SCHEMA_VERSION = 1
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
MAX_STORE_BYTES = 64 * 1024
AUTHENTICATION_RANDOM_PIN = "random_pin"
STATE_ACTIVE = "active"
STATE_CLEANUP_PENDING = "cleanup_pending"

_MAC = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_SOURCE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class BluetoothBondStoreError(RuntimeError):
    """Base class for bond-store failures."""


class UnsafeBluetoothBondStore(BluetoothBondStoreError):
    """The store path, ownership, or permissions are unsafe."""


class CorruptBluetoothBondStore(BluetoothBondStoreError):
    """The persisted schema or one of its entries is invalid."""


@dataclass(frozen=True)
class BluetoothBond:
    """Non-secret metadata for one authenticated phone/controller pair."""

    controller: str
    phone_address: str
    phone_name: str
    authentication: str
    source: str
    state: str
    timestamp: float

    def as_dict(self, *, include_controller: bool = True) -> dict[str, object]:
        result = {
            "address": self.phone_address,
            "name": self.phone_name,
            "authentication": self.authentication,
            "source": self.source,
            "state": self.state,
            "timestamp": self.timestamp,
        }
        if include_controller:
            result["controller"] = self.controller
        return result


@dataclass(frozen=True)
class _Owner:
    home: Path
    uid: int
    gid: int


def _real_invoking_user() -> _Owner:
    """Resolve the desktop account, validating sudo's identity metadata."""
    effective_uid = os.geteuid()
    if effective_uid == 0 and os.environ.get("SUDO_USER"):
        user = os.environ["SUDO_USER"].strip()
        raw_uid = os.environ.get("SUDO_UID", "")
        if not user or not raw_uid.isdecimal():
            raise UnsafeBluetoothBondStore(
                "SUDO_USER/SUDO_UID do not identify the invoking user")
        try:
            account = pwd.getpwnam(user)
        except KeyError as exc:
            raise UnsafeBluetoothBondStore(
                "the invoking sudo user does not exist") from exc
        if account.pw_uid != int(raw_uid):
            raise UnsafeBluetoothBondStore(
                "SUDO_UID does not match the invoking sudo user")
        return _Owner(Path(account.pw_dir), account.pw_uid, account.pw_gid)
    try:
        account = pwd.getpwuid(effective_uid)
    except KeyError as exc:
        raise UnsafeBluetoothBondStore(
            "the effective user has no account entry") from exc
    return _Owner(Path(account.pw_dir), account.pw_uid, account.pw_gid)


def default_bond_store_path() -> Path:
    """Return the real invoking user's bond-store path."""
    return _real_invoking_user().home / ".watchdogs" / "bluetooth_bonds.json"


def canonical_controller(value: object) -> str:
    """Validate and normalize a stable Bluetooth controller MAC address."""
    return _mac(value, "controller")


def canonical_address(value: object) -> str:
    """Validate and normalize a Bluetooth phone MAC address."""
    return _mac(value, "phone")


def _verified_flag(value: object) -> bool:
    # dbus.Boolean is an integer-like wrapper rather than the True singleton.
    return (value is True
            or (not isinstance(value, (str, bytes, bytearray)) and value == 1))


def _name_prefix(value: object) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.strip().encode("utf-8")[:80]
    return raw.decode("utf-8", errors="ignore")


def verify_bluez_bond(
    controller: str,
    address: str,
    *,
    managed_objects: Mapping | None = None,
    object_manager=None,
    bus_factory=None,
) -> dict[str, object] | None:
    """Read and verify one exact BlueZ ``Device1`` bond.

    This helper never pairs, trusts, connects, removes, or otherwise mutates a
    device.  Production callers use a private system-bus connection.  Tests
    can inject either a managed-object mapping or an ObjectManager-compatible
    object.
    """
    try:
        controller_key = canonical_controller(controller)
        phone_address = canonical_address(address)
    except CorruptBluetoothBondStore:
        return None

    private_bus = None
    try:
        if managed_objects is None:
            if object_manager is None:
                import dbus

                if bus_factory is None:
                    def bus_factory():
                        try:
                            return dbus.SystemBus(private=True)
                        except TypeError:  # older dbus-python
                            return dbus.bus.BusConnection(
                                dbus.bus.BUS_SYSTEM)

                private_bus = bus_factory()
                object_manager = dbus.Interface(
                    private_bus.get_object("org.bluez", "/"),
                    "org.freedesktop.DBus.ObjectManager")
            managed_objects = object_manager.GetManagedObjects()
        if not isinstance(managed_objects, Mapping):
            return None

        adapter_path = ""
        for path, interfaces in managed_objects.items():
            if not isinstance(interfaces, Mapping):
                continue
            adapter = interfaces.get("org.bluez.Adapter1")
            if not isinstance(adapter, Mapping):
                continue
            try:
                adapter_address = canonical_controller(
                    adapter.get("Address", ""))
            except CorruptBluetoothBondStore:
                continue
            if adapter_address == controller_key:
                adapter_path = str(path).rstrip("/")
                break
        if not adapter_path:
            return None

        for path, interfaces in managed_objects.items():
            device_path = str(path)
            if (not device_path.startswith(adapter_path + "/")
                    or not isinstance(interfaces, Mapping)):
                continue
            device = interfaces.get("org.bluez.Device1")
            if not isinstance(device, Mapping):
                continue
            if (device.get("Adapter") is not None
                    and str(device.get("Adapter")) != adapter_path):
                continue
            try:
                candidate = canonical_address(device.get("Address", ""))
            except CorruptBluetoothBondStore:
                continue
            if candidate != phone_address:
                continue
            if not all(_verified_flag(device.get(flag)) for flag in (
                    "Paired", "Bonded", "Trusted")):
                return None
            name = _name_prefix(
                device.get("Alias", device.get("Name", "")))
            return {
                "controller": controller_key,
                "address": phone_address,
                "name": name,
                "paired": True,
                "bonded": True,
                "trusted": True,
                "path": device_path,
            }
        return None
    except Exception:  # noqa: BLE001 - D-Bus failures must fail closed
        # D-Bus absence, restart, or malformed data fails closed.
        return None
    finally:
        close = getattr(private_bus, "close", None)
        if close is not None:
            with suppress(Exception):
                close()


def _mac(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not _MAC.fullmatch(text):
        raise CorruptBluetoothBondStore(f"invalid {label} Bluetooth address")
    return text.upper()


def _name(value: object) -> str:
    if not isinstance(value, str):
        raise CorruptBluetoothBondStore("bond name must be text")
    text = value.strip()
    raw = text.encode("utf-8")
    if len(raw) > 80:
        raise CorruptBluetoothBondStore("bond name exceeds 80 UTF-8 bytes")
    return text


def _source(value: object) -> str:
    if not isinstance(value, str) or not _SOURCE.fullmatch(value):
        raise CorruptBluetoothBondStore("invalid bond source")
    return value


def _timestamp(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CorruptBluetoothBondStore("invalid bond timestamp")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise CorruptBluetoothBondStore("invalid bond timestamp")
    return result


def _record(controller: object, value: object) -> BluetoothBond:
    controller_key = _mac(controller, "controller")
    if not isinstance(value, Mapping):
        raise CorruptBluetoothBondStore(
            f"bond entry for {controller_key} must be an object")
    expected = {
        "address", "name", "authentication", "source", "state",
        "timestamp",
    }
    if set(value) != expected:
        raise CorruptBluetoothBondStore(
            f"bond entry for {controller_key} has an invalid schema")
    authentication = value.get("authentication")
    if authentication != AUTHENTICATION_RANDOM_PIN:
        raise CorruptBluetoothBondStore(
            f"bond entry for {controller_key} has invalid authentication")
    state = value.get("state")
    if state not in (STATE_ACTIVE, STATE_CLEANUP_PENDING):
        raise CorruptBluetoothBondStore(
            f"bond entry for {controller_key} has invalid state")
    return BluetoothBond(
        controller=controller_key,
        phone_address=_mac(value.get("address"), "phone"),
        phone_name=_name(value.get("name")),
        authentication=AUTHENTICATION_RANDOM_PIN,
        source=_source(value.get("source")),
        state=str(state),
        timestamp=_timestamp(value.get("timestamp")),
    )


class BluetoothPhoneBondStore:
    """Load and atomically update the private Bluetooth bond registry."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        owner_uid: int | None = None,
        owner_gid: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        owner = (_real_invoking_user()
                 if path is None or owner_uid is None or owner_gid is None
                 else None)
        self.path = Path(path) if path is not None else (
            owner.home / ".watchdogs" / "bluetooth_bonds.json")
        self.owner_uid = (
            owner.uid if owner_uid is None else int(owner_uid))
        self.owner_gid = (
            owner.gid if owner_gid is None else int(owner_gid))
        self.clock = clock
        self._lock = threading.RLock()

    @classmethod
    def default(cls, **kwargs) -> BluetoothPhoneBondStore:
        """Construct the real invoking user's default private store."""
        return cls(None, **kwargs)

    def _open_directory(self, *, create: bool) -> int | None:
        """Open and harden the private directory without following links."""
        directory = self.path.parent
        flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                 | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_CLOEXEC", 0))
        created = False
        try:
            descriptor = os.open(directory, flags)
        except FileNotFoundError:
            if not create:
                return None
            try:
                os.mkdir(directory, DIRECTORY_MODE)
                created = True
            except FileExistsError:
                pass
            try:
                descriptor = os.open(directory, flags)
            except OSError as exc:
                raise UnsafeBluetoothBondStore(
                    "Bluetooth bond directory is not a real directory") from exc
        except OSError as exc:
            raise UnsafeBluetoothBondStore(
                "Bluetooth bond directory is not a real directory") from exc

        try:
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode):
                raise UnsafeBluetoothBondStore(
                    "Bluetooth bond directory is not a real directory")
            if created and os.geteuid() == 0:
                os.fchown(descriptor, self.owner_uid, self.owner_gid)
                info = os.fstat(descriptor)
            if info.st_uid != self.owner_uid:
                raise UnsafeBluetoothBondStore(
                    "Bluetooth bond directory has the wrong owner")
            if stat.S_IMODE(info.st_mode) != DIRECTORY_MODE:
                if os.geteuid() not in (0, self.owner_uid):
                    raise UnsafeBluetoothBondStore(
                        "Bluetooth bond directory must have mode 0700")
                os.fchmod(descriptor, DIRECTORY_MODE)
                info = os.fstat(descriptor)
                if stat.S_IMODE(info.st_mode) != DIRECTORY_MODE:
                    raise UnsafeBluetoothBondStore(
                        "Bluetooth bond directory must have mode 0700")
            if created:
                parent_fd = os.open(directory.parent, flags)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _require_file(self, directory_fd: int) -> os.stat_result | None:
        try:
            info = os.stat(
                self.path.name, dir_fd=directory_fd,
                follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise UnsafeBluetoothBondStore(
                "Bluetooth bond store is not a regular file")
        if info.st_uid != self.owner_uid:
            raise UnsafeBluetoothBondStore(
                "Bluetooth bond store has the wrong owner")
        if stat.S_IMODE(info.st_mode) != FILE_MODE:
            raise UnsafeBluetoothBondStore(
                "Bluetooth bond store must have mode 0600")
        if info.st_size > MAX_STORE_BYTES:
            raise CorruptBluetoothBondStore(
                "Bluetooth bond store exceeds its size limit")
        return info

    def _read_file(
            self, directory_fd: int, expected: os.stat_result) -> bytes:
        flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_CLOEXEC", 0))
        try:
            descriptor = os.open(
                self.path.name, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise UnsafeBluetoothBondStore(
                "Bluetooth bond store changed during validation") from exc
        try:
            actual = os.fstat(descriptor)
            if ((actual.st_dev, actual.st_ino)
                    != (expected.st_dev, expected.st_ino)
                    or not stat.S_ISREG(actual.st_mode)
                    or actual.st_uid != self.owner_uid
                    or stat.S_IMODE(actual.st_mode) != FILE_MODE
                    or actual.st_size > MAX_STORE_BYTES):
                raise UnsafeBluetoothBondStore(
                    "Bluetooth bond store changed during validation")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                return stream.read(MAX_STORE_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _load_unlocked(self) -> dict[str, BluetoothBond]:
        directory_fd = self._open_directory(create=False)
        if directory_fd is None:
            return {}
        try:
            info = self._require_file(directory_fd)
            if info is None:
                return {}
            raw = self._read_file(directory_fd, info)
            if len(raw) > MAX_STORE_BYTES:
                raise CorruptBluetoothBondStore(
                    "Bluetooth bond store exceeds its size limit")
            value = json.loads(raw.decode("utf-8"))
        except CorruptBluetoothBondStore:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CorruptBluetoothBondStore(
                "Bluetooth bond store is not valid JSON") from exc
        finally:
            os.close(directory_fd)
        if (not isinstance(value, Mapping)
                or set(value) != {"version", "controllers"}
                or value.get("version") != SCHEMA_VERSION
                or not isinstance(value.get("controllers"), Mapping)):
            raise CorruptBluetoothBondStore(
                "Bluetooth bond store has an unsupported schema")
        result: dict[str, BluetoothBond] = {}
        for controller, entry in value["controllers"].items():
            record = _record(controller, entry)
            if record.controller in result:
                raise CorruptBluetoothBondStore(
                    "Bluetooth bond store has duplicate controllers")
            result[record.controller] = record
        return result

    def load(self) -> dict[str, BluetoothBond]:
        """Return all validated controller records, or fail closed."""
        with self._lock:
            return dict(self._load_unlocked())

    def get(self, controller: str) -> BluetoothBond | None:
        """Return the exact stable-controller record when present."""
        key = _mac(controller, "controller")
        with self._lock:
            return self._load_unlocked().get(key)

    def _write_unlocked(self, values: Mapping[str, BluetoothBond]) -> None:
        directory_fd = self._open_directory(create=True)
        if directory_fd is None:  # pragma: no cover - create=True guarantees it
            raise UnsafeBluetoothBondStore(
                "Bluetooth bond directory could not be created")
        self._require_file(directory_fd)
        payload = json.dumps({
            "version": SCHEMA_VERSION,
            "controllers": {
                key: values[key].as_dict(include_controller=False)
                for key in sorted(values)
            },
        }, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        descriptor = -1
        temporary = ""
        try:
            flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_CLOEXEC", 0))
            for _attempt in range(64):
                temporary = (
                    ".bluetooth_bonds." + secrets.token_hex(12) + ".tmp")
                try:
                    descriptor = os.open(
                        temporary, flags, FILE_MODE,
                        dir_fd=directory_fd)
                    break
                except FileExistsError:
                    continue
            if descriptor < 0:
                raise OSError("could not allocate a private temporary file")
            os.fchmod(descriptor, FILE_MODE)
            if os.geteuid() == 0:
                os.fchown(descriptor, self.owner_uid, self.owner_gid)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Refuse a symlink or permission swap immediately before replace.
            self._require_file(directory_fd)
            os.replace(
                temporary, self.path.name,
                src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            temporary = ""
            os.fsync(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            os.close(directory_fd)

    def commit(
        self,
        controller: str,
        address: str,
        name: str = "",
        source: str = "meshcore",
        *,
        authentication: str = AUTHENTICATION_RANDOM_PIN,
        timestamp: float | None = None,
    ) -> BluetoothBond:
        """Commit one active phone for a stable controller address."""
        if authentication != AUTHENTICATION_RANDOM_PIN:
            raise CorruptBluetoothBondStore(
                "only random_pin authentication may be persisted")
        record = _record(_mac(controller, "controller"), {
            "address": _mac(address, "phone"),
            "name": _name(name),
            "authentication": authentication,
            "source": _source(source),
            "state": STATE_ACTIVE,
            "timestamp": self.clock() if timestamp is None else timestamp,
        })
        with self._lock:
            values = self._load_unlocked()
            values[record.controller] = record
            self._write_unlocked(values)
        return record

    def remove(self, controller: str, expected_address: str = "") -> bool:
        """Remove only the selected controller's retained phone metadata."""
        key = _mac(controller, "controller")
        expected = (_mac(expected_address, "phone")
                    if str(expected_address or "").strip() else "")
        with self._lock:
            values = self._load_unlocked()
            current = values.get(key)
            if current is None:
                return False
            if expected and current.phone_address != expected:
                return False
            del values[key]
            self._write_unlocked(values)
            return True

    def mark_cleanup_pending(
        self, controller: str, address: str = "", name: str = "",
        source: str = "meshcore", *, timestamp: float | None = None,
    ) -> BluetoothBond:
        """Retain an uncertain bond and prevent it being treated as active."""
        key = _mac(controller, "controller")
        with self._lock:
            values = self._load_unlocked()
            current = values.get(key)
            if current is None:
                updated = _record(key, {
                    "address": _mac(address, "phone"),
                    "name": _name(name),
                    "authentication": AUTHENTICATION_RANDOM_PIN,
                    "source": _source(source),
                    "state": STATE_CLEANUP_PENDING,
                    "timestamp": (
                        self.clock() if timestamp is None else timestamp),
                })
            else:
                if (str(address or "").strip()
                        and _mac(address, "phone")
                        != current.phone_address):
                    raise BluetoothBondStoreError(
                        "cleanup address does not match the retained phone")
                updated = replace(
                    current, state=STATE_CLEANUP_PENDING,
                    timestamp=_timestamp(
                        self.clock() if timestamp is None else timestamp))
            values[key] = updated
            self._write_unlocked(values)
            return updated

    def migrate_legacy(
        self,
        controller: str,
        address: str,
        name: str,
        verifier: Callable[[str, str], object],
        *,
        source: str = "meshcore",
    ) -> BluetoothBond | None:
        """Import legacy settings only after exact live BlueZ verification.

        The verifier is called with normalized controller and phone addresses.
        It must return verified BlueZ ``Device1`` properties including the
        exact address and all three security flags. Legacy settings are
        read-only here so callers can continue mirroring them for the
        one-release compatibility window.
        """
        try:
            controller_key = _mac(controller, "controller")
            phone_address = _mac(address, "phone")
        except CorruptBluetoothBondStore:
            return None
        existing = self.get(controller_key)
        if existing is not None:
            return existing
        try:
            verified = verifier(controller_key, phone_address)
        except Exception:  # noqa: BLE001 - verifier failures fail closed
            return None
        verified_name = ""
        if not isinstance(verified, Mapping):
            return None
        try:
            exact_controller = _mac(
                verified.get("controller", verified.get("Controller")),
                "controller")
            exact_address = _mac(
                verified.get("address", verified.get("Address")), "phone")
        except CorruptBluetoothBondStore:
            return None
        paired = verified.get("paired", verified.get("Paired"))
        bonded = verified.get("bonded", verified.get("Bonded"))
        trusted = verified.get("trusted", verified.get("Trusted"))
        if (exact_controller != controller_key
                or exact_address != phone_address
                or not _verified_flag(paired)
                or not _verified_flag(bonded)
                or not _verified_flag(trusted)):
            return None
        candidate = verified.get(
            "name", verified.get("Alias", verified.get("Name", "")))
        if isinstance(candidate, str):
            verified_name = candidate
        return self.commit(
            controller_key, phone_address, name or verified_name,
            source=source)


# Backwards-friendly short names for callers that do not need the distinction
# between BlueZ's cryptographic bond and WDG's retained phone metadata.
BluetoothBondStore = BluetoothPhoneBondStore
BondRecord = BluetoothBond
