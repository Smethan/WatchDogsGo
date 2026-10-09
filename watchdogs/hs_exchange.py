"""Bounded host exchange validation matching projectZero's hs_exchange.c.

Each instance belongs to one device/boot/job. A complete M1/M2 or M2/M3
exchange is required; counts from other instances never complete it.
"""
from collections import OrderedDict
from dataclasses import dataclass
import struct


@dataclass(frozen=True)
class KeyFrame:
    message: int
    ap: bytes
    client: bytes
    replay: int
    nonce: bytes
    keyver: int
    mic: bytes
    eapol: bytes


def parse_key_frame(frame):
    if len(frame) < 24 or frame[0] & 3:
        return None
    kind, subtype, ds = (frame[0] >> 2) & 3, frame[0] >> 4, frame[1] & 3
    if kind != 2 or ds not in (1, 2) or frame[1] & 0x40:
        return None
    header = 24
    if subtype & 8:
        if len(frame) < 26 or frame[24] & 0x80:
            return None
        header += 2 + (4 if frame[1] & 0x80 else 0)
    if frame[header:header+8] != b'\xaa\xaa\x03\x00\x00\x00\x88\x8e':
        return None
    e = frame[header+8:]
    if len(e) < 99 or e[0] not in (1, 2, 3) or e[1] != 3:
        return None
    body = int.from_bytes(e[2:4], 'big')
    if not 95 <= body <= 252 or 4+body > len(e):
        return None
    e = e[:4+body]
    key, bits = e[4:], int.from_bytes(e[5:7], 'big')
    if (key[0] not in (2, 254) or not bits & 8 or bits & 0xc00
            or int.from_bytes(key[93:95], 'big') != body-95):
        return None
    ack, install, mic = bool(bits & 0x80), bool(bits & 0x40), bool(bits & 0x100)
    nonce = key[13:45]
    message = (1 if ack and not install and not mic else
               2 if not ack and not install and mic and any(nonce) else
               3 if ack and install and mic else
               4 if not ack and not install and mic and not any(nonce) else 0)
    if not message or (mic and not any(key[77:93])):
        return None
    if (message in (1, 3)) != (ds == 2):
        return None
    ap = frame[10:16] if ds == 2 else frame[4:10]
    client = frame[4:10] if ds == 2 else frame[10:16]
    if not any(ap) or not any(client) or ap[0] & 1 or client[0] & 1:
        return None
    replay = int.from_bytes(key[5:13], 'big')
    if message in (3, 4):
        if replay == 0:
            return None
        replay -= 1
    keyver = bits & 7
    if not 1 <= keyver <= 3:
        return None
    return KeyFrame(message, ap, client, replay, nonce, keyver, key[77:93], e)


class ExchangeValidator:
    def __init__(self, limit=32):
        if not 1 <= limit <= 128:
            raise ValueError('Exchange capacity must be bounded')
        self.limit = limit
        self.entries = OrderedDict()
        self.ssids = OrderedDict()
        self.saturated = 0
        self.complete = 0

    def set_ssid(self, ap, ssid):
        if isinstance(ap, str):
            ap = bytes.fromhex(ap.replace(':', ''))
        if len(ap) == 6 and 0 < len(ssid) <= 32:
            self.ssids[ap] = bytes(ssid)
            if len(self.ssids) > 128:
                self.ssids.popitem(last=False)

    def ingest(self, frame):
        parsed = parse_key_frame(frame)
        if parsed is None or len(frame) > 512:
            return None
        key = (parsed.ap, parsed.client, parsed.replay)
        entry = self.entries.get(key)
        if entry is None:
            if len(self.entries) >= self.limit:
                victim = next((k for k, v in self.entries.items() if not v['complete']), None)
                if victim is None:
                    self.saturated += 1
                    return None
                del self.entries[victim]
            entry = {'frames': {}, 'complete': False}
            self.entries[key] = entry
        self.entries.move_to_end(key)
        if entry['complete']:
            return None
        entry['frames'][parsed.message] = (parsed, bytes(frame))
        ssid = self.ssids.get(parsed.ap)
        if not ssid or 2 not in entry['frames']:
            return None
        station = entry['frames'][2][0]
        for ap_message, pair in ((1, 0), (3, 2)):
            if ap_message not in entry['frames']:
                continue
            ap = entry['frames'][ap_message][0]
            if ap.keyver != station.keyver or not any(ap.nonce) or not any(station.nonce) or not any(station.mic):
                continue
            eapol = bytearray(station.eapol)
            eapol[81:97] = bytes(16)
            artifact = struct.pack('<IIBB32sB16s6s32s6s32sH256s',
                0x58504348, 4, pair, len(ssid), ssid, station.keyver, station.mic,
                ap.ap, ap.nonce, station.client, station.nonce, len(eapol), eapol)
            entry['complete'] = True
            self.complete += 1
            return {'bssid': ap.ap.hex(':').upper(), 'station': station.client.hex(':').upper(),
                    'replay': parsed.replay, 'message_pair': pair, 'hccapx': artifact,
                    'frames': [saved[1] for _, saved in sorted(entry['frames'].items())]}
        return None
