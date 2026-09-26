#!/usr/bin/env python3
"""Configure gpsd as the single owner of a uConsole GPS UART.

The helper deliberately edits only the small set of gpsd keys WDG owns and
only migrates Meshtastic direct UART paths used by the CM4/CM5 platform GPS.
Every changed pre-existing file receives one immutable pre-migration backup.
"""

import argparse
import os
import re
import shlex
import shutil
import tempfile
from pathlib import Path

GPSD_KEYS = ("START_DAEMON", "USBAUTO", "DEVICES", "GPSD_OPTIONS")
PLATFORM_UARTS = {"serial0", "ttyS0", "ttyAMA0"}


def configure_gpsd_defaults(text: str, device: str) -> str:
    values: dict[str, str] = {}
    lines = text.splitlines()
    for line in lines:
        match = re.match(r"^\s*([A-Z_]+)\s*=\s*(.*)$", line)
        if match and match.group(1) in GPSD_KEYS:
            values[match.group(1)] = match.group(2).strip()

    raw_options = values.get("GPSD_OPTIONS", "").strip()
    if (len(raw_options) >= 2 and raw_options[0] == raw_options[-1]
            and raw_options[0] in {'"', "'"}):
        raw_options = raw_options[1:-1]
    try:
        options = shlex.split(raw_options)
    except ValueError:
        options = []
    if "-n" not in options:
        options.append("-n")
    replacements = {
        "START_DAEMON": '"true"',
        "USBAUTO": '"false"',
        "DEVICES": f'"{device}"',
        "GPSD_OPTIONS": '"' + " ".join(options) + '"',
    }

    output: list[str] = []
    replaced: set[str] = set()
    for line in lines:
        match = re.match(r"^(\s*)([A-Z_]+)\s*=.*$", line)
        if match and match.group(2) in replacements:
            key = match.group(2)
            if key not in replaced:
                output.append(f"{key}={replacements[key]}")
                replaced.add(key)
            continue
        output.append(line)
    for key in GPSD_KEYS:
        if key not in replaced:
            output.append(f"{key}={replacements[key]}")
    return "\n".join(output).rstrip() + "\n"


def _gps_block(lines: list[str]) -> tuple[int, int] | None:
    start = next((index for index, line in enumerate(lines)
                  if re.match(r"^GPS:\s*(?:#.*)?$", line)), None)
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^[A-Za-z][A-Za-z0-9_]*:\s*(?:#.*)?$", lines[index]):
            end = index
            break
    return start, end


def configure_meshtastic(text: str, device: str,
                         host: str = "127.0.0.1", port: int = 2947) -> tuple[str, bool]:
    """Return migrated YAML and whether it was safe to manage this GPS block."""
    lines = text.splitlines()
    bounds = _gps_block(lines)
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["GPS:", f"  GpsdHost: {host}", f"  GpsdPort: {port}"])
        return "\n".join(lines).rstrip() + "\n", True

    start, end = bounds
    active_serial: tuple[int, str] | None = None
    for index in range(start + 1, end):
        match = re.match(r"^\s+SerialPath:\s*([^#\s]+)", lines[index])
        if match:
            active_serial = (index, match.group(1))
            break
    if active_serial is not None:
        configured_name = Path(active_serial[1]).name
        requested_name = Path(device).name
        if configured_name not in PLATFORM_UARTS or requested_name not in PLATFORM_UARTS:
            return text if text.endswith("\n") else text + "\n", False

    body: list[str] = []
    for index in range(start + 1, end):
        line = lines[index]
        if re.match(r"^\s+(?:GpsdHost|GpsdPort):", line):
            continue
        if active_serial is not None and index == active_serial[0]:
            body.append("  # WDG gpsd owns raw UART: " + line.strip())
        else:
            body.append(line)
    canonical = [f"  GpsdHost: {host}", f"  GpsdPort: {port}"]
    lines[start:end] = [lines[start], *canonical, *body]
    return "\n".join(lines).rstrip() + "\n", True


def _atomic_write(path: Path, content: str, mode: int = 0o644) -> bool:
    encoded = content.encode("utf-8")
    try:
        if path.read_bytes() == encoded:
            return False
    except OSError:
        pass

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = None
    try:
        existing = path.stat()
    except OSError:
        pass
    if existing is not None:
        backup = path.with_name(path.name + ".wdg-before-shared-gps")
        if not backup.exists():
            shutil.copy2(path, backup)

    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, existing.st_mode & 0o7777 if existing else mode)
        if existing is not None:
            os.chown(temporary, existing.st_uid, existing.st_gid)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpsd-default", type=Path, required=True)
    parser.add_argument("--meshtastic-config", type=Path, required=True)
    parser.add_argument("--marker", type=Path, required=True)
    parser.add_argument("--device", default="/dev/serial0")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2947)
    args = parser.parse_args()

    try:
        defaults = args.gpsd_default.read_text(encoding="utf-8")
    except OSError:
        defaults = ""
    defaults_changed = _atomic_write(
        args.gpsd_default, configure_gpsd_defaults(defaults, args.device))

    meshtastic_changed = False
    managed = True
    if args.meshtastic_config.exists():
        original = args.meshtastic_config.read_text(encoding="utf-8")
        migrated, managed = configure_meshtastic(
            original, args.device, args.host, args.port)
        if managed:
            meshtastic_changed = _atomic_write(args.meshtastic_config, migrated)
    if not managed:
        print("meshtastic=skipped-nonplatform-uart")
        return 2

    marker = (
        "# Managed by WatchDogsGo setup.sh; gpsd is the sole raw-UART owner.\n"
        f"HOST={args.host}\nPORT={args.port}\nDEVICE={args.device}\n")
    marker_changed = _atomic_write(args.marker, marker)
    print("gpsd=" + ("changed" if defaults_changed else "unchanged"))
    print("meshtastic=" + ("changed" if meshtastic_changed else "unchanged"))
    print("marker=" + ("changed" if marker_changed else "unchanged"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
