"""Bounded passive OpenDroneID Basic ID and Location message decoder.

Implements only the two 25-byte messages needed for a map position.  Other
message types, operator coordinates and authentication data are ignored.
The byte layout follows opendroneid-core-c's packed structures.
"""
from __future__ import annotations

from collections import OrderedDict
import math
import time

from .notable_detector import ad_fields

MESSAGE_SIZE = 25
ASTM_SERVICE = b"\xfa\xff\x0d"


def messages(payload):
    """Return complete ODID messages; never decode a truncated message pack."""
    if len(payload) == MESSAGE_SIZE and payload[0] >> 4 in (0, 1):
        return [payload]
    if len(payload) < 3 or payload[0] >> 4 != 15:
        return []
    size, count = payload[1], payload[2]
    if size != MESSAGE_SIZE or not 1 <= count <= 9 or len(payload) != 3 + size*count:
        return []
    return [payload[3+i*size:3+(i+1)*size] for i in range(count)]


def basic_id(msg):
    if len(msg) != MESSAGE_SIZE or msg[0] >> 4 != 0:
        return None
    id_type = msg[1] >> 4
    if id_type not in (1, 2, 3, 4):
        return None
    raw = msg[2:22].split(b"\x00", 1)[0].strip()
    if not 3 <= len(raw) <= 20 or any(not 32 <= b <= 126 for b in raw):
        return None
    return raw.decode("ascii")


def location(msg):
    if len(msg) != MESSAGE_SIZE or msg[0] >> 4 != 1:
        return None
    lat = int.from_bytes(msg[5:9], "little", signed=True) / 1e7
    lon = int.from_bytes(msg[9:13], "little", signed=True) / 1e7
    if (not math.isfinite(lat) or not math.isfinite(lon)
            or not -90 <= lat <= 90 or not -180 <= lon <= 180
            or (lat == 0 and lon == 0)):
        return None
    return {"latitude": lat, "longitude": lon}


def ble_messages(data_hex):
    try:
        data = bytes.fromhex(data_hex)
    except (ValueError, TypeError):
        return []
    output = []
    for kind, value in ad_fields(data):
        if kind == 0x16 and value.startswith(ASTM_SERVICE) and len(value) == 4+MESSAGE_SIZE:
            output.extend(messages(value[4:]))
    return output


class RemoteIdDecoder:
    def __init__(self):
        self.cache = OrderedDict()

    def clear(self):
        self.cache.clear()

    def accept(self, mac, payload, now=None):
        """Return a position and advertised broadcast ID when both are fresh.

        A location-only advertisement may be displayed using its address as a
        provisional identity.  It must never match an agency ID allowlist.
        """
        if not mac or not isinstance(payload, (list, tuple)):
            return None
        now = time.monotonic() if now is None else now
        old = self.cache.pop(mac, {"seen": now, "id": None, "position": None})
        if now-old["seen"] > 30:
            old = {"seen": now, "id": None, "position": None}
        if now-old.get("id_seen", -float("inf")) > 30:
            old["id"] = None
        if now-old.get("position_seen", -float("inf")) > 30:
            old["position"] = None
        old["seen"] = now
        changed = False
        for msg in payload:
            ident = basic_id(msg)
            pos = location(msg)
            if ident:
                if old["id"] is not None and old["id"] != ident:
                    # A recycled address must not lend the previous aircraft's
                    # last position to a newly advertised identifier.
                    old["position"] = None
                old["id"] = ident
                old["id_seen"] = now
                changed = True
            if pos:
                old["position"] = pos
                old["position_seen"] = now
                changed = True
        self.cache[mac] = old
        while len(self.cache) > 256:
            self.cache.popitem(last=False)
        if not changed or old["position"] is None:
            return None
        return {"identity": old["id"] or mac.upper(),
                "broadcast_id": old["id"],
                "position": old["position"],
                "stable": bool(old["id"]),
                "label": "Remote ID " + (old["id"] or "unlinked")}
