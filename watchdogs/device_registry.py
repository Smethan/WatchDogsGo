"""USB endpoint and factory-identity registry for C5 job-capable devices."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .c5_jobs import parse_device_info

_SUPPORTED_VID_PID = {
    (0x10C4, 0xEA60), (0x1A86, 0x7523), (0x1A86, 0x55D4),
    (0x0403, 0x6001), (0x0403, 0x6015), (0x303A, 0x1001), (0x303A, 0x4001),
}


def _attr(port: Any, name: str, default: Any = None) -> Any:
    if isinstance(port, Mapping):
        return port.get(name, default)
    return getattr(port, name, default)


def supported_candidates(ports: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    """Return stable USB candidate metadata, without guessing from tty numbering."""
    if ports is None:
        try:
            from serial.tools import list_ports
            ports = list_ports.comports()
        except Exception:
            return []
    result = []
    for port in ports:
        vid, pid = _attr(port, "vid"), _attr(port, "pid")
        if (vid, pid) not in _SUPPORTED_VID_PID:
            continue
        endpoint = _attr(port, "device")
        if not isinstance(endpoint, str) or not endpoint:
            continue
        serial_number = _attr(port, "serial_number") or ""
        location = _attr(port, "location") or ""
        result.append({
            "endpoint": endpoint,
            "usb_identity": {
                "vid": vid, "pid": pid, "serial_number": str(serial_number),
                "location": str(location),
            },
            "description": str(_attr(port, "description", "") or ""),
            "manufacturer": str(_attr(port, "manufacturer", "") or ""),
            "product": str(_attr(port, "product", "") or ""),
        })
    return sorted(result, key=lambda item: item["endpoint"])


def _usb_key(identity: Mapping[str, Any] | str | None, endpoint: str) -> str:
    if isinstance(identity, str):
        return "serial:" + identity if identity else "endpoint:" + endpoint
    identity = identity or {}
    serial_number = str(identity.get("serial_number", "") or "").strip()
    if serial_number:
        return "serial:" + serial_number
    location = str(identity.get("location", "") or "").strip()
    if location:
        return "usb:" + location
    vid, pid = identity.get("vid"), identity.get("pid")
    if vid is not None and pid is not None:
        return f"usb:{int(vid):04x}:{int(pid):04x}:{endpoint}"
    return "endpoint:" + endpoint


@dataclass
class DeviceEntry:
    device_id: str
    endpoint: str
    usb_identity: dict[str, Any]
    device_info: dict[str, Any]
    aliases: set[str] = field(default_factory=set)
    roles: set[str] = field(default_factory=set)
    connected: bool = True
    session: Any = None

    @property
    def boot_id(self) -> str:
        return self.device_info["boot_id"]

    @property
    def board(self) -> str:
        return self.device_info["board"]

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(self.device_info.get("operations", ()))


class DeviceRegistry:
    """Indexes attached endpoints separately from firmware factory identities."""

    def __init__(self) -> None:
        self._devices: dict[str, DeviceEntry] = {}
        self._endpoint_to_id: dict[str, str] = {}
        self._usb_to_id: dict[str, str] = {}
        self._aliases: dict[str, str] = {}

    def upsert(self, endpoint: str, usb_identity: Mapping[str, Any] | str | None,
               device_info: Mapping[str, Any] | str, *, session: Any = None) -> DeviceEntry:
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError("endpoint")
        if isinstance(device_info, str):
            parsed = parse_device_info(device_info)
        else:
            try:
                parsed = parse_device_info("WDG:" + json.dumps(device_info, separators=(",", ":")))
            except (TypeError, ValueError):
                parsed = None
        if not parsed or parsed.get("device_id") is None:
            raise ValueError("invalid device_info")
        device_id = str(parsed["device_id"]).upper()
        usb_identity = dict(usb_identity) if isinstance(usb_identity, Mapping) else {
            "serial_number": usb_identity or ""}
        key = _usb_key(usb_identity, endpoint)
        prior = self._devices.get(device_id)
        old_usb_id = self._usb_to_id.get(key)
        if old_usb_id and old_usb_id != device_id:
            displaced = self._devices.get(old_usb_id)
            if displaced is not None:
                displaced.connected = False
                displaced.session = None
                self._endpoint_to_id.pop(displaced.endpoint, None)
        # Factory identity remains authoritative, while a changed boot ID updates
        # the entry in place so aliases and user roles survive a reboot.
        entry = prior or DeviceEntry(device_id, endpoint, usb_identity, parsed)
        if entry.endpoint != endpoint:
            self._endpoint_to_id.pop(entry.endpoint, None)
        entry.endpoint = endpoint
        entry.usb_identity = usb_identity
        entry.device_info = parsed
        entry.connected = True
        if session is not None:
            entry.session = session
        self._devices[device_id] = entry
        self._endpoint_to_id[endpoint] = device_id
        self._usb_to_id[key] = device_id
        return entry

    def identify_usb(self, endpoint: str, usb_identity: Mapping[str, Any] | str | None) -> DeviceEntry | None:
        """Find an already-known physical board before firmware identity arrives."""
        key = _usb_key(usb_identity, endpoint)
        device_id = self._usb_to_id.get(key) or self._endpoint_to_id.get(endpoint)
        return self._devices.get(device_id) if device_id else None

    def by_endpoint(self, endpoint: str) -> DeviceEntry | None:
        device_id = self._endpoint_to_id.get(endpoint)
        return self._devices.get(device_id) if device_id else None

    def get(self, device_id_or_alias: str) -> DeviceEntry | None:
        key = str(device_id_or_alias).upper()
        device_id = self._aliases.get(device_id_or_alias, key)
        return self._devices.get(device_id)

    def assign_alias(self, device_id: str, alias: str) -> None:
        entry = self.get(device_id)
        if entry is None or not isinstance(alias, str) or not alias.strip():
            raise ValueError("device or alias")
        alias = alias.strip()
        owner = self._aliases.get(alias)
        if owner and owner != entry.device_id:
            raise ValueError("alias already assigned")
        self._aliases[alias] = entry.device_id
        entry.aliases.add(alias)

    def assign_role(self, device_id: str, role: str, enabled: bool = True) -> None:
        entry = self.get(device_id)
        if entry is None or not isinstance(role, str) or not role.strip():
            raise ValueError("device or role")
        if enabled:
            entry.roles.add(role)
        else:
            entry.roles.discard(role)

    def disconnect(self, endpoint: str) -> DeviceEntry | None:
        entry = self.by_endpoint(endpoint)
        if entry is not None:
            entry.connected = False
            entry.session = None
            self._endpoint_to_id.pop(endpoint, None)
        return entry

    def list(self, *, connected_only: bool = False) -> list[DeviceEntry]:
        entries = self._devices.values()
        if connected_only:
            entries = (entry for entry in entries if entry.connected)
        return sorted(entries, key=lambda entry: entry.device_id)
