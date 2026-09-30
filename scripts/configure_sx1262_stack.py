#!/usr/bin/env python3
"""Configure the AIO SX1262 manager and broker-backed Meshtastic client."""

from __future__ import annotations

import argparse
import grp
import os
import re
import stat
import tempfile
from pathlib import Path

LORA_HEADER = re.compile(r"^Lora:\s*(?:#.*)?$")
TOP_LEVEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*:\s*(?:#.*)?$")
BROKER_SOCKET = "/run/watchdogs/sx1262d.sock"


def lora_mapping(text: str) -> str | None:
    """Return the exact top-level Lora mapping, including its header."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines)
                  if LORA_HEADER.match(line.rstrip("\r\n"))), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines):
        raw = lines[end].rstrip("\r\n")
        if raw and not raw[0].isspace() and TOP_LEVEL.match(raw):
            break
        end += 1
    return "".join(lines[start:end])


def replace_lora_mapping(text: str, replacement: str) -> str:
    """Replace one top-level Lora mapping without touching other settings."""
    if not LORA_HEADER.match(replacement.splitlines()[0] if replacement else ""):
        raise ValueError("Replacement must be a top-level Lora mapping")
    replacement = replacement if replacement.endswith("\n") else replacement + "\n"
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines)
                  if LORA_HEADER.match(line.rstrip("\r\n"))), None)
    if start is None:
        prefix = "" if not text or text.endswith("\n") else "\n"
        return text + prefix + replacement
    end = start + 1
    while end < len(lines):
        raw = lines[end].rstrip("\r\n")
        if raw and not raw[0].isspace() and TOP_LEVEL.match(raw):
            break
        end += 1
    return "".join(lines[:start]) + replacement + "".join(lines[end:])


def is_broker_meshtastic_config(text: str) -> bool:
    mapping = lora_mapping(text)
    if mapping is None:
        return False
    significant = [
        line.strip() for line in mapping.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return significant == [
        "Lora:", "Module: broker", f"BrokerSocket: {BROKER_SOCKET}"]


def broker_meshtastic_config(text: str) -> str:
    """Replace only the top-level hardware Lora mapping, idempotently."""
    replacement = [
        "Lora:\n",
        "  # WDG unified manager is the sole SPI/GPIO/power owner.\n",
        "  Module: broker\n",
        f"  BrokerSocket: {BROKER_SOCKET}\n",
    ]
    return replace_lora_mapping(text, "".join(replacement))


def manager_config(gpiochip: int) -> str:
    return f"""# Managed by WatchDogsGo setup.sh; hardware facts only.
Lora:
  Module: sx1262
  spidev: spidev1.0
  # The uConsole AIO wiring is validated at Portduino's conservative 2 MHz
  # default.  Higher clocks can make the SX1262 probe spend its full retry
  # window waiting on an unreadable chip before the broker can answer clients.
  spiSpeed: 2000000
  gpiochip: {int(gpiochip)}
  IRQ: 26
  Busy: 24
  Reset: 25
  Enable_Pins:
    - pin: 16
      line: 16
      default_high: true
  DIO2_AS_RF_SWITCH: true
  DIO3_TCXO_VOLTAGE: 1.8
  SX126X_MAX_POWER: 22
"""


def atomic_write(
        path: Path, content: str, *, uid: int = 0, gid: int = 0,
        mode: int = 0o640) -> bool:
    encoded = content.encode("utf-8")
    if path.is_symlink():
        raise ValueError(f"Refusing symlink at protected path: {path}")
    if path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Protected path is not a regular file: {path}")
    else:
        info = None
    if (info is not None and path.read_bytes() == encoded
            and info.st_uid == uid and info.st_gid == gid
            and stat.S_IMODE(info.st_mode) == mode):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError(f"Protected parent is unsafe: {path.parent}")
    descriptor, temporary = tempfile.mkstemp(
        prefix="." + path.name + ".", dir=path.parent)
    try:
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return True


def configure_stack(
        meshtastic_config: Path, manager_config_path: Path, gpiochip: int,
        meshtastic_gid: int, manager_gid: int) -> tuple[bool, bool]:
    """Atomically apply manager hardware facts and the broker client config."""
    source = (meshtastic_config.read_text(encoding="utf-8")
              if meshtastic_config.exists() else "---\n")
    meshtastic_changed = atomic_write(
        meshtastic_config, broker_meshtastic_config(source),
        uid=0, gid=int(meshtastic_gid), mode=0o640)
    manager_changed = atomic_write(
        manager_config_path, manager_config(gpiochip),
        uid=0, gid=int(manager_gid), mode=0o640)
    return meshtastic_changed, manager_changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--meshtastic-config", type=Path, required=True)
    parser.add_argument("--manager-config", type=Path, required=True)
    parser.add_argument("--gpiochip", type=int, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("SX1262 stack configuration must run as root")
    try:
        meshtastic_gid = grp.getgrnam("meshtasticd").gr_gid
        manager_gid = grp.getgrnam("watchdogs").gr_gid
    except KeyError as exc:
        parser.error("meshtasticd/watchdogs service groups are not installed")
        raise AssertionError from exc
    changed, manager_changed = configure_stack(
        args.meshtastic_config, args.manager_config,
        args.gpiochip, meshtastic_gid, manager_gid)
    print("meshtastic=" + ("changed" if changed else "unchanged"))
    print("manager=" + ("changed" if manager_changed else "unchanged"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
