"""Strict structured Packet Sniffer result transport."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .app_state import ProbeEntry, SnifferAP
from .wardrive_protocol import MAC, TOKEN, display_bytes, integer

MAX_APS = 100
MAX_PROBES = 200


def parse_sniffer_record(line: str):
    """Parse one bounded ``SNIFF:`` record, rejecting partial/unsafe data."""
    try:
        if not line.startswith("SNIFF:") or len(line.encode()) > 512:
            return None
        record = json.loads(line[6:])
        if not isinstance(record, dict) or type(record.get("v")) is not int:
            return None
        if record["v"] != 1 or record.get("kind") not in {
                "summary", "ap", "probe", "done"}:
            return None
        session = record.get("session")
        if not isinstance(session, str) or not TOKEN.fullmatch(session):
            return None
        kind = record["kind"]
        if kind == "summary":
            if type(record.get("available")) is not bool:
                return None
            integer(record, "packets", 0, 0xFFFFFFFF)
            integer(record, "aps", 0, MAX_APS)
            integer(record, "probes", 0, MAX_PROBES)
            integer(record, "age_ms", 0, 0xFFFFFFFF)
        elif kind == "ap":
            integer(record, "seq", 1, MAX_APS)
            integer(record, "channel", 1, 196)
            integer(record, "clients", 0, 50)
            integer(record, "packets", 0, 0xFFFFFFFF)
            integer(record, "probes", 0, MAX_PROBES)
            if not isinstance(record.get("bssid"), str) or not MAC.fullmatch(record["bssid"]):
                return None
            raw = bytes.fromhex(record["bssid"].replace(":", ""))
            if raw[0] & 1 or not any(raw):
                return None
            if not isinstance(record.get("ssid_hex"), str) or not re.fullmatch(
                    r"(?:[0-9a-fA-F]{2}){0,32}", record["ssid_hex"]):
                return None
            record["bssid"] = record["bssid"].upper()
            record["name"] = display_bytes(record["ssid_hex"]) or "<hidden>"
        elif kind == "probe":
            integer(record, "seq", 1, MAX_PROBES)
            integer(record, "rssi", -127, 20)
            if not isinstance(record.get("mac"), str) or not MAC.fullmatch(record["mac"]):
                return None
            raw = bytes.fromhex(record["mac"].replace(":", ""))
            if raw[0] & 1 or not any(raw):
                return None
            if not isinstance(record.get("ssid_hex"), str) or not re.fullmatch(
                    r"(?:[0-9a-fA-F]{2}){0,32}", record["ssid_hex"]):
                return None
            record["mac"] = record["mac"].upper()
            record["name"] = display_bytes(record["ssid_hex"]) or "<hidden>"
        else:
            integer(record, "aps", 0, MAX_APS)
            integer(record, "probes", 0, MAX_PROBES)
        return record
    except (ValueError, TypeError, KeyError, RecursionError):
        return None


@dataclass
class SnifferIntelCollector:
    """Atomically assemble a token-bound export before replacing UI state."""

    session: str = ""
    state: str = "idle"
    available: bool = False
    packets: int = 0
    age_ms: int = 0
    expected_aps: int = 0
    expected_probes: int = 0
    aps: list[SnifferAP] = field(default_factory=list)
    probes: list[ProbeEntry] = field(default_factory=list)
    error: str = ""

    def start(self, session: str) -> str:
        if not TOKEN.fullmatch(session):
            raise ValueError("Invalid sniffer result session token")
        self.session = session
        self.state = "waiting"
        self.available = False
        self.packets = self.age_ms = 0
        self.expected_aps = self.expected_probes = 0
        self.aps.clear()
        self.probes.clear()
        self.error = ""
        return "show_sniffer_intel " + session

    def accept(self, record) -> bool:
        """Accept a record. Return True only for a complete valid export."""
        if not record or record.get("session") != self.session:
            return False
        kind = record["kind"]
        if kind == "summary":
            if self.state != "waiting":
                self._fail("duplicate or out-of-order summary")
                return False
            self.available = record["available"]
            self.packets = record["packets"]
            self.age_ms = record["age_ms"]
            self.expected_aps = record["aps"]
            self.expected_probes = record["probes"]
            if (not self.available and
                    (self.packets or self.expected_aps or
                     self.expected_probes or self.age_ms)):
                self._fail("unavailable result carried observations")
                return False
            self.state = "receiving"
            return False
        if self.state != "receiving":
            self._fail("result arrived before summary")
            return False
        if kind == "ap":
            if record["seq"] != len(self.aps) + 1 or len(self.aps) >= self.expected_aps:
                self._fail("AP sequence mismatch")
                return False
            if (record["packets"] > self.packets or
                    record["probes"] > self.expected_probes):
                self._fail("AP metrics exceed session totals")
                return False
            self.aps.append(SnifferAP(
                bssid=record["bssid"], ssid=record["name"],
                channel=record["channel"], client_count=record["clients"],
                packet_count=record["packets"], probe_count=record["probes"],
            ))
            return False
        if kind == "probe":
            if (len(self.aps) != self.expected_aps or
                    record["seq"] != len(self.probes) + 1 or
                    len(self.probes) >= self.expected_probes):
                self._fail("probe sequence mismatch")
                return False
            self.probes.append(ProbeEntry(
                ssid=record["name"], mac=record["mac"], rssi=record["rssi"],
            ))
            return False
        if (record["aps"] != self.expected_aps or
                record["probes"] != self.expected_probes or
                len(self.aps) != self.expected_aps or
                len(self.probes) != self.expected_probes):
            self._fail("sniffer result count mismatch")
            return False
        self.state = "complete"
        return True

    def _fail(self, message: str) -> None:
        self.state = "error"
        self.error = message
