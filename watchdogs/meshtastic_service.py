"""Closed client for the privileged Meshtastic service helper.

The helper is the only code allowed to mutate package or service state.  This
client exposes typed operations instead of a generic command runner so UI code
cannot pass a URL, package path, executable path, service name, or shell text
through the privilege boundary.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .meshtastic_updates import TAG_RE, parse_meshtastic_tag

MESHTASTIC_HELPER = Path("/usr/local/libexec/watchdogs-meshtastic")
REQUIRED_HELPER_VERSION = 4
TRANSACTION_TIMEOUT = 900
SERVICE_TARGETS = ("wdg", "stock")
SERVICE_ACTIONS = ("start", "stop", "enable", "disable")


@dataclass(frozen=True)
class MeshtasticServiceStatus:
    target: str
    service: str
    load_state: str
    active_state: str
    unit_file_state: str
    package_version: str | None = None

    @property
    def installed(self) -> bool:
        return self.load_state not in {"", "not-found"}

    @property
    def active(self) -> bool:
        return self.active_state == "active"

    @property
    def enabled(self) -> bool:
        return self.unit_file_state in ("enabled", "enabled-runtime")


class MeshtasticServiceController:
    """Invoke only the fixed, root-owned helper and its closed operations."""

    def __init__(
        self,
        *,
        runner: Callable[..., Any] = subprocess.run,
        geteuid: Callable[[], int] = os.geteuid,
    ) -> None:
        self._runner = runner
        self._geteuid = geteuid

    def _command(self, operation: str, argument: str | None = None) -> list[str]:
        if operation == "version" or operation == "rollback":
            if argument is not None:
                raise ValueError(operation + " does not take an argument")
        elif (operation == "status" or operation == "select-service"
              or operation in SERVICE_ACTIONS):
            if argument not in SERVICE_TARGETS:
                raise ValueError("Unknown Meshtastic service target")
        elif operation == "install-tag":
            if argument is None or not TAG_RE.fullmatch(argument):
                raise ValueError("Expected Meshtastic tag vX.Y.Z-wdg.N")
            parse_meshtastic_tag(argument)
        else:
            raise ValueError("Unsupported Meshtastic helper operation")
        command = [str(MESHTASTIC_HELPER), operation]
        if argument is not None:
            command.append(argument)
        if self._geteuid() != 0 and operation != "version":
            command = ["sudo", "-n", *command]
        return command

    def _call(self, operation: str, argument: str | None = None,
              *, timeout: int = 180) -> dict[str, Any]:
        result = self._runner(
            self._command(operation, argument), capture_output=True, text=True,
            timeout=timeout, check=False)
        if result.returncode:
            detail = (result.stderr or result.stdout or "Meshtastic helper failed").strip()
            raise RuntimeError(detail[:1200])
        try:
            payload = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Meshtastic helper returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError("Meshtastic helper returned an invalid response")
        return payload

    def version(self) -> int:
        value = self._call("version", timeout=10).get("helper_version")
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError("Meshtastic helper returned an invalid version")
        return value

    def require_current(self) -> None:
        version = self.version()
        if version < REQUIRED_HELPER_VERSION:
            raise RuntimeError(
                "Meshtastic helper is outdated; rerun sudo bash setup.sh")

    def status(self, target: str) -> MeshtasticServiceStatus:
        payload = self._call("status", target, timeout=15)
        return self._parse_status(payload, target)

    @staticmethod
    def _parse_status(
            payload: dict[str, Any], target: str) -> MeshtasticServiceStatus:
        """Validate one authoritative helper reply containing unit state."""
        if payload.get("target") != target:
            raise RuntimeError("Meshtastic helper returned the wrong service target")
        expected_service = (
            "meshtasticd-wdg.service" if target == "wdg" else
            "meshtasticd.service")
        if payload.get("service") != expected_service:
            raise RuntimeError("Meshtastic helper returned an unexpected service")
        fields = ("load_state", "active_state", "unit_file_state")
        if any(not isinstance(payload.get(field), str) for field in fields):
            raise RuntimeError("Meshtastic helper returned incomplete service status")
        package_version = payload.get("package_version")
        if package_version is not None and not isinstance(package_version, str):
            raise RuntimeError("Meshtastic helper returned an invalid package version")
        return MeshtasticServiceStatus(
            target=target,
            service=expected_service,
            load_state=payload["load_state"],
            active_state=payload["active_state"],
            unit_file_state=payload["unit_file_state"],
            package_version=package_version,
        )

    def set_state(self, action: str, target: str) -> MeshtasticServiceStatus:
        if action not in SERVICE_ACTIONS:
            raise ValueError("Unsupported Meshtastic service action")
        payload = self._call(action, target, timeout=30)
        if payload.get("action") != action:
            raise RuntimeError("Meshtastic helper returned the wrong action")
        return self._parse_status(payload, target)

    def start(self, target: str) -> MeshtasticServiceStatus:
        return self.set_state("start", target)

    def stop(self, target: str) -> MeshtasticServiceStatus:
        return self.set_state("stop", target)

    def enable(self, target: str) -> MeshtasticServiceStatus:
        return self.set_state("enable", target)

    def disable(self, target: str) -> MeshtasticServiceStatus:
        return self.set_state("disable", target)

    def select(self, target: str) -> MeshtasticServiceStatus:
        payload = self._call("select-service", target, timeout=60)
        if payload.get("action") != "select-service":
            raise RuntimeError("Meshtastic helper returned the wrong action")
        if payload.get("selected") != target:
            raise RuntimeError("Meshtastic helper selected the wrong service")
        return self._parse_status(payload, target)

    def install_tag(self, tag: str) -> dict[str, Any]:
        return self._call("install-tag", tag, timeout=TRANSACTION_TIMEOUT)

    def rollback(self) -> dict[str, Any]:
        return self._call("rollback", timeout=TRANSACTION_TIMEOUT)
