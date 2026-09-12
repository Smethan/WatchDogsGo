"""Bounded passive serial capture, raw PCAP and observed PMKID metadata.

EAPOL frame counts are not claims of complete or crackable handshakes.
"""
from collections import OrderedDict
import json
import os
from pathlib import Path
import struct
import time


def tagged_fields(data):
    pos = 0
    while pos + 2 <= len(data):
        kind, size = data[pos:pos+2]
        pos += 2
        if pos + size > len(data):
            return
        yield kind, data[pos:pos+size]
        pos += size


def rsn_pmkids(data):
    """Return only complete PMKID lists from an RSN information element."""
    if len(data) < 8 or data[:2] != b"\x01\x00":
        return []
    pos = 6
    for _ in range(2):  # pairwise cipher and authentication suite lists
        if pos + 2 > len(data):
            return []
        count = int.from_bytes(data[pos:pos+2], "little")
        pos += 2 + count * 4
        if pos > len(data):
            return []
    pos += 2  # RSN capabilities
    if pos + 2 > len(data):
        return []
    count = int.from_bytes(data[pos:pos+2], "little")
    pos += 2
    if pos + count * 16 > len(data):
        return []
    return [data[i:i+16] for i in range(pos, pos+count*16, 16)]


def inspect_frame(frame):
    """Extract identity/context and PMKIDs without guessing missing fields."""
    info = {"eapol": False, "message": 0, "replay": None, "pmkids": [], "bssid": "", "station": "", "ssid_hex": None}
    if len(frame) < 24:
        return info
    mac = lambda p: p.hex(":").upper()
    kind, subtype, flags = (frame[0] >> 2) & 3, frame[0] >> 4, frame[1]
    if kind == 0:
        fixed = {0:4, 1:6, 2:10, 3:6, 5:12, 8:12}.get(subtype)
        if fixed is None or len(frame) < 24 + fixed:
            return info
        info["bssid"] = mac(frame[16:22])
        if subtype in (0, 2):
            info["station"] = mac(frame[10:16])
        elif subtype in (1, 3):
            info["station"] = mac(frame[4:10])
        for tag, value in tagged_fields(frame[24+fixed:]):
            if tag == 0 and 0 < len(value) <= 32:
                info["ssid_hex"] = value.hex()
            elif tag == 48:
                info["pmkids"].extend(rsn_pmkids(value))
    elif kind == 2 and not flags & 0x40:
        ds = flags & 3
        hdr = 30 if ds == 3 else 24
        if subtype & 8:
            if len(frame) < hdr+2 or frame[hdr] & 0x80:
                return info
            hdr += 2 + (4 if flags & 0x80 else 0)
        if frame[hdr:hdr+8] != b"\xaa\xaa\x03\x00\x00\x00\x88\x8e":
            return info
        eapol = frame[hdr+8:]
        if len(eapol) < 4:
            return info
        end = 4 + int.from_bytes(eapol[2:4], "big")
        if end > len(eapol):
            return info
        eapol = eapol[:end]
        info["eapol"] = True
        if ds == 1:
            info.update(bssid=mac(frame[4:10]), station=mac(frame[10:16]))
        elif ds == 2:
            info.update(bssid=mac(frame[10:16]), station=mac(frame[4:10]))
        # EAPOL-Key fixed header is 99 bytes, including the EAPOL header.
        if len(eapol) < 99 or eapol[1] != 3 or eapol[4] not in (2, 254):
            return info
        key_info = int.from_bytes(eapol[5:7], "big")
        size = int.from_bytes(eapol[97:99], "big")
        if 99+size > len(eapol):
            return info
        # Pairwise keys only; group rekeys/error/request frames aren't M1-M4.
        if key_info & 8 and not key_info & 0xc00:
            ack, mic, install = bool(key_info & 0x80), bool(key_info & 0x100), bool(key_info & 0x40)
            if ack and not mic and not install:
                info["message"] = 1
            elif ack and mic and install:
                info["message"] = 3
            elif not ack and mic and not install:
                info["message"] = 2 if any(eapol[17:49]) else 4
            info["replay"] = int.from_bytes(eapol[9:17], "big")
        if key_info & 0x1000 or eapol[4] != 2:
            return info  # encrypted key data cannot be inspected
        for tag, value in tagged_fields(eapol[99:99+size]):
            if tag == 221 and len(value) == 20 and value[:4] == b"\x00\x0f\xac\x04":
                info["pmkids"].append(value[4:])
    info["pmkids"] = [p.hex() for p in info["pmkids"] if any(p)]
    return info


class PassiveCapture:
    def __init__(self):
        self.file = self.events = None
        self.path = None
        self.frames = self.eapol = self.pmkids = self.lost = 0
        self.partial = None
        self.ssids = OrderedDict()
        self.seen = OrderedDict()
        self.rows = OrderedDict()

    def open(self, directory):
        self.close()
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.time_ns()
        self.path = directory / f"passive_{stamp}.pcap"
        try:
            self.file = self.path.open("xb")
            self.events = self.path.with_suffix(".jsonl").open("x", encoding="utf-8")
            self.file.write(struct.pack("<IHHIIII", 0xa1b2c3d4, 2, 4, 0, 0, 2304, 105))
            self.file.flush()
        except OSError:
            self.close()
            raise
        self.frames = self.eapol = self.pmkids = self.lost = 0
        self.partial = None
        self.ssids.clear()
        self.seen.clear()
        self.rows.clear()
        self.last_flush = time.monotonic()

    def close(self):
        if self.partial:
            self.lost += 1
            self.partial = None
        error = None
        for name in ("file", "events"):
            fh = getattr(self, name)
            if fh:
                try:
                    fh.flush()
                    os.fsync(fh.fileno())
                except OSError as exc:
                    error = exc
                finally:
                    try:
                        fh.close()
                    except OSError as exc:
                        error = exc
                    setattr(self, name, None)
        if error:
            raise error

    def accept(self, d):
        if not self.file:
            return
        if d["offset"] == 0:
            if self.partial:
                self.lost += 1
            self.partial = dict(meta=d, data=bytearray(), seq=d["seq"]-1,
                                at=time.time()-d["age_ms"]/1000)
        p = self.partial
        if not p:
            return
        same = ("packet", "total", "capture_ms", "channel", "rssi", "session")
        if (any(p["meta"][k] != d[k] for k in same)
                or d["seq"] != p["seq"]+1 or d["offset"] != len(p["data"])):
            self.lost += 1
            self.partial = None
            return
        p["data"].extend(bytes.fromhex(d["data_hex"]))
        p["seq"] = d["seq"]
        if len(p["data"]) != d["total"]:
            return
        frame = bytes(p["data"])
        self.partial = None
        at = p["at"]
        self.file.write(struct.pack("<IIII", int(at), int((at % 1)*1000000), len(frame), len(frame)))
        self.file.write(frame)
        self.frames += 1
        info = inspect_frame(frame)
        self.eapol += int(info["eapol"])
        if info["ssid_hex"]:
            self.ssids[info["bssid"]] = info["ssid_hex"]
            if len(self.ssids) > 1024:
                self.ssids.popitem(last=False)
        info["ssid_hex"] = info["ssid_hex"] or self.ssids.get(info["bssid"])
        row = None
        if info["bssid"] and (info["eapol"] or info["pmkids"]):
            identity = (info["bssid"], info["station"])
            row = self.rows.setdefault(identity, dict(bssid=info["bssid"], station=info["station"],
                messages=[0,0,0,0], pmkids=0, first=at, last=at, channel=d["channel"], rssi=d["rssi"]))
            row.update(last=at, channel=d["channel"], rssi=d["rssi"])
            if len(self.rows) > 512:
                self.rows.popitem(last=False)
        if info["message"]:
            if row:
                row["messages"][info["message"]-1] += 1
            self.events.write(json.dumps(dict(at=at, kind="eapol", message=info["message"],
                replay=info["replay"], bssid=info["bssid"], station=info["station"],
                ssid_hex=info["ssid_hex"], channel=d["channel"], rssi=d["rssi"])) + "\n")
        for pmkid in info["pmkids"]:
            key = (info["bssid"], info["station"], pmkid)
            if key in self.seen:
                continue
            self.seen[key] = True
            if len(self.seen) > 4096:
                self.seen.popitem(last=False)
            self.pmkids += 1
            if row:
                row["pmkids"] += 1
            self.events.write(json.dumps(dict(at=at, kind="pmkid", pmkid=pmkid,
                bssid=info["bssid"], station=info["station"], ssid_hex=info["ssid_hex"],
                channel=d["channel"], rssi=d["rssi"])) + "\n")
        self.file.flush()  # preserve complete records even if the app crashes
        self.events.flush()
        if time.monotonic() - self.last_flush >= 1:
            os.fsync(self.file.fileno())
            os.fsync(self.events.fileno())
            self.last_flush = time.monotonic()
