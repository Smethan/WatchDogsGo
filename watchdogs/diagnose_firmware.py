"""Query firmware identity/capabilities without starting a scan or flashing."""

import argparse
import json
import time

import serial

from .serial_manager import SerialManager
from .wardrive_protocol import parse_record


def parse_diagnostic_capabilities(line):
    """Accept the bounded v1 degraded reply omitted by the legacy parser."""
    if not line.startswith("WDG:") or len(line.encode("utf-8", "replace")) > 1024:
        return None
    try:
        record = json.loads(line[4:])
    except (TypeError, ValueError):
        return None
    if (
        isinstance(record, dict)
        and type(record.get("v")) is int
        and record.get("v") == 1
        and record.get("kind") == "capabilities"
        and record.get("c5_jobs_v1") is False
        and type(record.get("max_line")) is int
        and record.get("max_line") == 1024
        and "wardrive_serial_v1" not in record
    ):
        return record
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", required=True, help="ESP32 serial port, e.g. /dev/ttyACM0"
    )
    args = parser.parse_args()
    print("Close WDG and serial monitors before running this diagnostic.", flush=True)
    print(
        "Only version/get_capabilities queries are sent; no scan or flash commands.",
        flush=True,
    )
    replies = []
    manager = SerialManager(args.port)
    try:
        manager.setup()
        start = time.monotonic()
        commands = [
            (1, "version"),
            (3, "get_capabilities"),
            (6, "get_capabilities"),
            (9, "get_capabilities"),
        ]
        while time.monotonic() - start < 13:
            elapsed = time.monotonic() - start
            if commands and elapsed >= commands[0][0]:
                _, cmd = commands.pop(0)
                print("TX:", cmd, flush=True)
                manager.send_command(cmd)
            for line in manager.read_available(max_bytes=4096, max_lines=64):
                # repr preserves control sequences without letting firmware
                # output issue terminal control commands.
                print("RX:", repr(line), flush=True)
                d = parse_record(line)
                if d and d["kind"] == "capabilities":
                    replies.append(d["wardrive_serial_v1"])
                elif parse_diagnostic_capabilities(line) is not None:
                    replies.append(None)
            time.sleep(0.02)
    except (serial.SerialException, OSError) as exc:
        print("Serial error:", exc)
        return 2
    finally:
        manager.close()
    if replies:
        if replies[-1] is None:
            print(
                "RESULT: Firmware replied; C5 jobs are unavailable (c5_jobs_v1=false)."
            )
            return 1
        print("RESULT: All Wardrive", "SUPPORTED" if replies[-1] else "UNSUPPORTED")
        return 0 if replies[-1] else 1
    print(
        "RESULT: No valid capability reply. This alone does not identify the firmware."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
