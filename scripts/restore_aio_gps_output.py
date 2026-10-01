#!/usr/bin/env python3
"""Restore standard NMEA output on an AIO GPS through gpsd.

The helper never opens the serial device.  It first identifies gpsd's active
driver and only sends the bounded u-blox ``enable NMEA`` command when gpsd is
already speaking the u-blox protocol.  Output is deliberately summarized so
receiver coordinates can never enter setup logs.
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import time
from collections.abc import Callable

WATCH = b'?WATCH={"enable":true,"json":true};\n'
ACK = "ACK to Class x06 (CFG) ID x01 (MSG)"


def detect_driver(host: str, port: int, device: str, *,
                  connector: Callable = socket.create_connection,
                  timeout: float = 4.0) -> str:
    """Return gpsd's driver for *device*, or an empty string if unavailable."""
    conn = connector((host, int(port)), 2.0)
    try:
        conn.settimeout(0.5)
        conn.sendall(WATCH)
        deadline = time.monotonic() + timeout
        buffer = b""
        while time.monotonic() < deadline:
            try:
                chunk = conn.recv(8192)
            except TimeoutError:
                continue
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                try:
                    report = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                reports = []
                if report.get("class") == "DEVICE":
                    reports = [report]
                elif report.get("class") == "DEVICES":
                    reports = report.get("devices") or []
                for candidate in reports:
                    if (isinstance(candidate, dict)
                            and candidate.get("path") == device):
                        return str(candidate.get("driver") or "")
        return ""
    finally:
        conn.close()


def restore_nmea(host: str, port: int, device: str, *,
                 driver: str, ubxtool: str | None = None,
                 runner: Callable = subprocess.run) -> tuple[bool, str]:
    """Enable standard NMEA messages through gpsd for a detected u-blox."""
    normalized = driver.strip().lower().replace("_", "-")
    if normalized not in {"u-blox", "ublox"}:
        return True, f"skipped-driver-{driver or 'unknown'}"
    tool = ubxtool or shutil.which("ubxtool")
    if not tool:
        return False, "ubxtool-missing"
    target = f"{host}:{int(port)}:{device}"
    try:
        result = runner(
            [tool, "-P", "10", "-e", "NMEA", "-w", "3", target],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "ubxtool-failed"
    acknowledgements = (result.stdout or "").count(ACK)
    if result.returncode != 0 or acknowledgements < 6:
        return False, f"nmea-acks-{acknowledgements}"
    return True, f"nmea-restored-acks-{acknowledgements}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2947)
    parser.add_argument("--device", default="/dev/serial0")
    args = parser.parse_args()

    try:
        driver = detect_driver(args.host, args.port, args.device)
    except OSError:
        print("receiver=unavailable nmea=unchanged")
        return 2
    ok, detail = restore_nmea(
        args.host, args.port, args.device, driver=driver)
    print(f"receiver={driver or 'unknown'} nmea={detail}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
