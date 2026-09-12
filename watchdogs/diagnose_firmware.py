"""Query firmware identity/capabilities without starting a scan or flashing."""
import argparse
import time

import serial

from .config import BAUD_RATE
from .serial_manager import SerialLineBuffer
from .wardrive_protocol import parse_record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="ESP32 serial port, e.g. /dev/ttyACM0")
    args = parser.parse_args()
    print("Close WDG and serial monitors before running this diagnostic.", flush=True)
    print("Only version/get_capabilities queries are sent; no scan or flash commands.", flush=True)
    replies = []
    buf = SerialLineBuffer()
    try:
        with serial.Serial(args.port, BAUD_RATE, timeout=0.2, write_timeout=2) as conn:
            start = time.monotonic()
            commands = [(1, "version"), (3, "get_capabilities"),
                        (6, "get_capabilities"), (9, "get_capabilities")]
            while time.monotonic() - start < 13:
                elapsed = time.monotonic() - start
                if commands and elapsed >= commands[0][0]:
                    _, cmd = commands.pop(0)
                    print("TX:", cmd, flush=True)
                    conn.write((cmd + "\r\n").encode())
                    conn.flush()
                raw = conn.read(min(max(conn.in_waiting, 1), 4096))
                for line in buf.feed(raw):
                    # repr preserves control sequences without letting firmware
                    # output issue terminal control commands.
                    print("RX:", repr(line), flush=True)
                    d = parse_record(line)
                    if d and d["kind"] == "capabilities":
                        replies.append(d["wardrive_serial_v1"])
    except (serial.SerialException, OSError) as exc:
        print("Serial error:", exc)
        return 2
    if replies:
        print("RESULT: All Wardrive", "SUPPORTED" if replies[-1] else "UNSUPPORTED")
        return 0 if replies[-1] else 1
    print("RESULT: No valid capability reply. This alone does not identify the firmware.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
