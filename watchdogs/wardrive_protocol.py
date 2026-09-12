"""Strict, bounded WDG v1 records. Firmware uptime is not a GPS clock."""
import json
import re
MAC = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\Z")
TOKEN = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
KINDS = {"started", "stopped", "error", "stats", "wifi", "wifi_mgmt", "ble"}

def integer(d, key, low, high):
    v = d.get(key)
    if type(v) is not int or not low <= v <= high:
        raise ValueError(key)

def parse_record(line):
    if not line.startswith("WDG:"):
        return None
    try:
        if len(line.encode("utf-8")) > 1024:
            raise ValueError("line")
        d = json.loads(line[4:])
        if not isinstance(d, dict) or type(d.get("v")) is not int or d["v"] != 1:
            raise ValueError("version")
        kind = d.get("kind")
        if kind == "capabilities":
            if type(d.get("wardrive_serial_v1")) is not bool:
                raise ValueError("capabilities")
            return d
        if kind not in KINDS or not isinstance(d.get("session"), str) or not TOKEN.fullmatch(d["session"]):
            raise ValueError("session")
        integer(d, "seq", 1, 2**32-1)
        if kind in {"wifi", "wifi_mgmt", "ble"}:
            integer(d, "capture_ms", 0, 2**63-1)
            integer(d, "age_ms", 0, 2000)
            integer(d, "rssi", -127, 20)
            if not isinstance(d.get("mac"), str) or not MAC.fullmatch(d["mac"]):
                raise ValueError("mac")
            d["mac"] = d["mac"].upper()
            key, size = ("data_hex", 62) if kind == "ble" else ("ssid_hex", 32)
            value = d.get(key)
            if not isinstance(value, str) or len(value) > size * 2 or not re.fullmatch(r"(?:[0-9a-fA-F]{2})*", value):
                raise ValueError(key)
            if kind == "ble":
                integer(d, "addr_type", 0, 3)
                integer(d, "event", 0, 4)
                if d.get("truncated") is not False:
                    raise ValueError("truncated")
            else:
                if kind == "wifi" and (not isinstance(d.get("auth", "UNKNOWN"), str) or len(d.get("auth", "UNKNOWN")) > 32):
                    raise ValueError("auth")
                integer(d, "channel", 1, 196)
                if kind == "wifi_mgmt":
                    if d.get("subtype") not in (4, 5, 8) or type(d.get("ssid_present")) is not bool:
                        raise ValueError("management")
                    if not MAC.fullmatch(d.get("receiver", "")):
                        raise ValueError("receiver")
        if kind in {"stats", "started", "stopped"}:
            for key in ("wifi_count", "ble_count", "drops"):
                integer(d, key, 0, 2**32-1)
        return d
    except (ValueError, TypeError, KeyError, RecursionError):
        return None

def display_bytes(value):
    text = bytes.fromhex(value).decode("utf-8", "replace")
    return "".join(c if c.isprintable() else " " for c in text)
