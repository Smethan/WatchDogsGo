"""Private configuration and durable history for the Reticulum add-on.

Reticulum identities and IFAC passphrases must not live in the ordinary
``wardrive_settings.json`` file.  This module keeps all Reticulum state below a
mode-0700 directory and performs versioned, fail-closed profile validation.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROFILE_VERSION = 1
RETICULUM_DIRNAME = "reticulum"
PROFILE_FILENAME = "profile.json"
HISTORY_FILENAME = "history.jsonl"

FREQUENCY_MIN_HZ = 150_000_000
FREQUENCY_MAX_HZ = 960_000_000
BANDWIDTH_CHOICES = (
    7_800, 10_400, 15_600, 20_800, 31_250,
    41_700, 62_500, 125_000, 250_000, 500_000,
)
DISPLAY_NAME_MAX_BYTES = 64
NETWORK_NAME_MAX_BYTES = 64
PASSPHRASE_MAX_BYTES = 128
MESSAGE_MAX_BYTES = 120
DESTINATION_RE = re.compile(r"^[0-9a-f]{32}$")


class ReticulumConfigError(ValueError):
    """The saved or proposed Reticulum profile is unsafe or invalid."""


def _utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


def _clean_single_line(value: Any, *, field: str, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise ReticulumConfigError(f"{field} must be text")
    value = value.strip()
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ReticulumConfigError(f"{field} must be a single line")
    if _utf8_len(value) > max_bytes:
        raise ReticulumConfigError(
            f"{field} must be at most {max_bytes} UTF-8 bytes")
    return value


@dataclass(frozen=True)
class ReticulumProfile:
    """Validated version-one Reticulum radio and identity preferences."""

    version: int = PROFILE_VERSION
    confirmed: bool = False
    display_name: str = ""
    frequency_hz: int = 869_618_000
    bandwidth_hz: int = 62_500
    spreading_factor: int = 8
    coding_rate: int = 5
    tx_power_dbm: int = 14
    airtime_short_percent: float = 10.0
    airtime_long_percent: float = 2.0
    network_name: str = ""
    network_passphrase: str = ""
    propagation_node_hash: str = ""
    propagated_outbound: bool = False

    def validate(self) -> "ReticulumProfile":
        if type(self.version) is not int or self.version != PROFILE_VERSION:
            raise ReticulumConfigError(
                f"unsupported Reticulum profile version: {self.version!r}")
        if type(self.confirmed) is not bool:
            raise ReticulumConfigError("confirmed must be true or false")
        display_name = _clean_single_line(
            self.display_name, field="display_name",
            max_bytes=DISPLAY_NAME_MAX_BYTES)
        network_name = _clean_single_line(
            self.network_name, field="network_name",
            max_bytes=NETWORK_NAME_MAX_BYTES)
        passphrase = _clean_single_line(
            self.network_passphrase, field="network_passphrase",
            max_bytes=PASSPHRASE_MAX_BYTES)
        propagation_node_hash = _clean_single_line(
            self.propagation_node_hash, field="propagation_node_hash",
            max_bytes=32)
        if bool(network_name) != bool(passphrase):
            raise ReticulumConfigError(
                "network_name and network_passphrase must both be blank or "
                "both be populated")
        if (propagation_node_hash
                and not DESTINATION_RE.fullmatch(propagation_node_hash)):
            raise ReticulumConfigError(
                "propagation_node_hash must be blank or 32 lowercase hex "
                "characters")
        if type(self.propagated_outbound) is not bool:
            raise ReticulumConfigError(
                "propagated_outbound must be true or false")
        if self.propagated_outbound and not propagation_node_hash:
            raise ReticulumConfigError(
                "propagated_outbound requires a propagation node hash")
        if type(self.frequency_hz) is not int or not (
                FREQUENCY_MIN_HZ <= self.frequency_hz <= FREQUENCY_MAX_HZ):
            raise ReticulumConfigError(
                "frequency_hz must be between 150000000 and 960000000")
        if type(self.bandwidth_hz) is not int \
                or self.bandwidth_hz not in BANDWIDTH_CHOICES:
            raise ReticulumConfigError(
                "bandwidth_hz is not supported by the SX1262 profile")
        if type(self.spreading_factor) is not int \
                or not 5 <= self.spreading_factor <= 12:
            raise ReticulumConfigError("spreading_factor must be 5 through 12")
        if type(self.coding_rate) is not int \
                or not 5 <= self.coding_rate <= 8:
            raise ReticulumConfigError("coding_rate must be 5 through 8")
        if type(self.tx_power_dbm) is not int \
                or not -9 <= self.tx_power_dbm <= 22:
            raise ReticulumConfigError("tx_power_dbm must be -9 through 22")
        if isinstance(self.airtime_short_percent, bool) or not isinstance(
                self.airtime_short_percent, (int, float)):
            raise ReticulumConfigError("airtime_short_percent must be numeric")
        if isinstance(self.airtime_long_percent, bool) or not isinstance(
                self.airtime_long_percent, (int, float)):
            raise ReticulumConfigError("airtime_long_percent must be numeric")
        short = float(self.airtime_short_percent)
        long = float(self.airtime_long_percent)
        if not 0.0 < short <= 100.0:
            raise ReticulumConfigError(
                "airtime_short_percent must be greater than 0 and at most 100")
        if not 0.0 < long <= short:
            raise ReticulumConfigError(
                "airtime_long_percent must be greater than 0 and no greater "
                "than airtime_short_percent")
        return ReticulumProfile(
            version=PROFILE_VERSION,
            confirmed=self.confirmed,
            display_name=display_name,
            frequency_hz=self.frequency_hz,
            bandwidth_hz=self.bandwidth_hz,
            spreading_factor=self.spreading_factor,
            coding_rate=self.coding_rate,
            tx_power_dbm=self.tx_power_dbm,
            airtime_short_percent=short,
            airtime_long_percent=long,
            network_name=network_name,
            network_passphrase=passphrase,
            propagation_node_hash=propagation_node_hash,
            propagated_outbound=self.propagated_outbound,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ReticulumProfile":
        if not isinstance(value, Mapping):
            raise ReticulumConfigError("Reticulum profile must be an object")
        allowed = set(cls.__dataclass_fields__)
        unknown = set(value) - allowed
        if unknown:
            raise ReticulumConfigError(
                "unknown Reticulum profile field(s): "
                + ", ".join(sorted(map(str, unknown))))
        try:
            return cls(**dict(value)).validate()
        except TypeError as exc:
            raise ReticulumConfigError(str(exc)) from exc

    @classmethod
    def seed_from_meshcore(cls, preset: tuple[int, int, int, int, str]) \
            -> "ReticulumProfile":
        frequency, sf, coding_rate, bandwidth, _label = preset
        return cls(
            frequency_hz=int(frequency),
            bandwidth_hz=int(bandwidth),
            spreading_factor=int(sf),
            coding_rate=int(coding_rate),
        ).validate()

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self.validate())

    def with_updates(self, **updates: Any) -> "ReticulumProfile":
        value = self.to_mapping()
        value.update(updates)
        return ReticulumProfile.from_mapping(value)


def state_dir(app_dir: str | Path) -> Path:
    return Path(app_dir) / RETICULUM_DIRNAME


def profile_path(app_dir: str | Path) -> Path:
    return state_dir(app_dir) / PROFILE_FILENAME


def history_path(app_dir: str | Path) -> Path:
    return state_dir(app_dir) / HISTORY_FILENAME


def identity_path(app_dir: str | Path) -> Path:
    return state_dir(app_dir) / "identity"


def rns_dir(app_dir: str | Path) -> Path:
    return state_dir(app_dir) / "rns"


def lxmf_dir(app_dir: str | Path) -> Path:
    return state_dir(app_dir) / "lxmf"


def _reject_symlink(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise ReticulumConfigError(f"refusing symlinked Reticulum path: {path}")


def ensure_private_dir(path: Path) -> Path:
    """Create a private directory without following an existing symlink."""
    _reject_symlink(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_symlink(path)
    if not path.is_dir():
        raise ReticulumConfigError(f"Reticulum state path is not a directory: {path}")
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        raise ReticulumConfigError(
            f"could not secure Reticulum directory {path}: {exc}") from exc
    return path


def ensure_state_layout(app_dir: str | Path) -> Path:
    root = ensure_private_dir(state_dir(app_dir))
    ensure_private_dir(root / "rns")
    ensure_private_dir(root / "rns" / "interfaces")
    ensure_private_dir(root / "lxmf")
    return root


def _atomic_private_write(path: Path, data: bytes) -> None:
    ensure_private_dir(path.parent)
    _reject_symlink(path)
    fd, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _reject_symlink(path)
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def write_private_file(path: str | Path, data: bytes | str) -> Path:
    """Atomically write a mode-0600 file below a secured parent directory."""
    path = Path(path)
    if isinstance(data, str):
        data = data.encode("utf-8")
    _atomic_private_write(path, bytes(data))
    return path


def load_profile(app_dir: str | Path, *, seed: ReticulumProfile | None = None) \
        -> ReticulumProfile:
    path = profile_path(app_dir)
    _reject_symlink(path)
    if not path.exists():
        return (seed or ReticulumProfile()).validate()
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReticulumConfigError(
            f"could not load Reticulum profile: {exc}") from exc
    return ReticulumProfile.from_mapping(value)


def save_profile(app_dir: str | Path, profile: ReticulumProfile, *,
                 pending: bool = False) -> Path:
    profile = profile.validate()
    path = profile_path(app_dir)
    if pending:
        path = path.with_suffix(".pending")
    payload = (json.dumps(profile.to_mapping(), indent=2, sort_keys=True)
               + "\n").encode("utf-8")
    _atomic_private_write(path, payload)
    return path


def commit_pending_profile(app_dir: str | Path) -> ReticulumProfile:
    active = profile_path(app_dir)
    pending = active.with_suffix(".pending")
    _reject_symlink(pending)
    profile = ReticulumProfile.from_mapping(
        json.loads(pending.read_text(encoding="utf-8")))
    _reject_symlink(active)
    os.replace(pending, active)
    os.chmod(active, 0o600)
    directory_fd = os.open(active.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return profile


def validate_destination_hash(value: str) -> str:
    if not isinstance(value, str) or not DESTINATION_RE.fullmatch(value):
        raise ReticulumConfigError(
            "Reticulum destination must be 32 lowercase hexadecimal characters")
    return value


def validate_message_text(value: str) -> str:
    value = _clean_single_line(
        value, field="message", max_bytes=MESSAGE_MAX_BYTES)
    if not value:
        raise ReticulumConfigError("message must not be blank")
    return value


def append_history(app_dir: str | Path, event: Mapping[str, Any]) -> None:
    """Append one bounded JSON history event to the private state file."""
    if not isinstance(event, Mapping):
        raise ReticulumConfigError("history event must be an object")
    encoded = json.dumps(dict(event), sort_keys=True, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > 16_384:
        raise ReticulumConfigError("history event exceeds 16 KiB")
    root = ensure_state_layout(app_dir)
    path = root / HISTORY_FILENAME
    _reject_symlink(path)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (encoded + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def load_history(app_dir: str | Path, *, limit: int = 200,
                 diagnostics: list[str] | None = None) -> list[dict]:
    path = history_path(app_dir)
    _reject_symlink(path)
    if not path.is_file():
        return []
    limit = max(0, min(int(limit), 1_000))
    result: deque[dict] = deque(maxlen=limit)
    malformed = 0
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    malformed += 1
                    continue
                if isinstance(value, dict):
                    result.append(value)
                else:
                    malformed += 1
    except (OSError, UnicodeError) as exc:
        if diagnostics is not None:
            diagnostics.append(
                "Could not read Reticulum history: " + str(exc)[:160])
        return []
    if malformed and diagnostics is not None:
        diagnostics.append(
            f"Ignored {malformed} malformed Reticulum history record(s)")
    return list(result)
