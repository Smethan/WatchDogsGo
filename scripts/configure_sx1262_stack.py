#!/usr/bin/env python3
"""Configure the AIO SX1262 manager and broker-backed Meshtastic client."""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path


LORA_HEADER = re.compile(r"^Lora:\s*(?:#.*)?$")
TOP_LEVEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*:\s*(?:#.*)?$")


def broker_meshtastic_config(text: str) -> str:
    """Replace only the top-level hardware Lora mapping, idempotently."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines)
                  if LORA_HEADER.match(line.rstrip("\r\n"))), None)
    replacement = [
        "Lora:\n",
        "  # WDG unified manager is the sole SPI/GPIO/power owner.\n",
        "  Module: broker\n",
        "  BrokerSocket: /run/watchdogs/sx1262d.sock\n",
    ]
    if start is None:
        prefix = "" if not text or text.endswith("\n") else "\n"
        return text + prefix + "".join(replacement)
    end = start + 1
    while end < len(lines):
        raw = lines[end].rstrip("\r\n")
        if raw and not raw[0].isspace() and TOP_LEVEL.match(raw):
            break
        end += 1
    return "".join(lines[:start] + replacement + lines[end:])


def manager_config(gpiochip: int) -> str:
    return f"""# Managed by WatchDogsGo setup.sh; hardware facts only.
Lora:
  Module: sx1262
  spidev: spidev1.0
  spiSpeed: 7800000
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


def atomic_write(path: Path, content: str, mode: int = 0o640) -> bool:
    encoded = content.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--meshtastic-config", type=Path, required=True)
    parser.add_argument("--manager-config", type=Path, required=True)
    parser.add_argument("--gpiochip", type=int, required=True)
    args = parser.parse_args()
    source = (args.meshtastic_config.read_text(encoding="utf-8")
              if args.meshtastic_config.exists() else "---\n")
    changed = atomic_write(
        args.meshtastic_config, broker_meshtastic_config(source))
    manager_changed = atomic_write(
        args.manager_config, manager_config(args.gpiochip))
    print("meshtastic=" + ("changed" if changed else "unchanged"))
    print("manager=" + ("changed" if manager_changed else "unchanged"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
