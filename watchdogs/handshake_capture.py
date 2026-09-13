"""Live progress for existing active captures; never owns their PCAP files."""
from collections import deque
import json
import re
import time

from .passive_capture import PassiveCapture
from .wardrive_protocol import integer, TOKEN

COMMANDS = {"sd": "start_handshake", "serial": "start_handshake_serial"}


def parse_progress(line):
    try:
        if not line.startswith("HSC:") or len(line.encode("utf-8")) > 1024:
            return None
        d = json.loads(line[4:])
        if not isinstance(d, dict) or type(d.get("v")) is not int or d["v"] != 1:
            return None
        if not isinstance(d.get("session"), str) or not TOKEN.fullmatch(d["session"]):
            return None
        integer(d, "seq", 1, 2**32-1)
        if d.get("kind") == "hs_packet":
            for key, low, high in (("packet",1,2**32-1), ("offset",0,2303),
                                   ("total",24,2304), ("capture_ms",0,2**63-1), ("age_ms",0,2000)):
                integer(d, key, low, high)
            value = d.get("data_hex")
            if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,240}", value):
                return None
            if d["offset"] % 240 or len(value)//2 != min(240, d["total"]-d["offset"]):
                return None
            # The PCAP observer does not receive radio metadata.
            d.update(channel=None, rssi=None)
        elif d.get("kind") in ("started", "stats", "stopped"):
            if d.get("storage") not in COMMANDS:
                return None
            for key in ("wifi_count", "ble_count", "drops"):
                integer(d, key, 0, 2**32-1)
        else:
            return None
        return d
    except (ValueError, TypeError, KeyError, RecursionError):
        return None


class CaptureRun(PassiveCapture):
    def __init__(self):
        super().__init__(monitor_only=True)
        self.state = "idle"
        self.session = None
        self.seq = self.gaps = self.drops = self.invalid = 0
        self.last_progress = 0
        self.cleanup_pending = False
        self.note = "Live PMKID/M1-M4 progress requires ESP firmware 1.7.4+."

    @property
    def active(self):
        return self.state in ("starting", "running", "stopping", "finishing")


class HandshakeCapture:
    def __init__(self):
        self.runs = {storage: CaptureRun() for storage in COMMANDS}
        self.storage = None
        self.retired = deque(maxlen=32)

    @property
    def current(self):
        return self.runs.get(self.storage)

    def start(self, command):
        self.finish()
        self.storage = next(storage for storage, cmd in COMMANDS.items() if cmd == command)
        run = self.runs[self.storage] = CaptureRun()
        run.state = "starting"

    def stop(self):
        if self.current and self.current.active and self.current.state != "finishing":
            self.current.state = "stopping"

    def finish(self, state="stopped"):
        run = self.current
        if run and run.active:
            run.close()  # counts/discards any incomplete progress frame
            run.state = state
            if run.session:
                self.retired.append(run.session)

    def handle(self, line):
        """Consume HSC only; legacy text must still reach the loot file parser."""
        run = self.current
        if not line.startswith("HSC:"):
            if run and run.active:
                lower = line.lower()
                if "handshake attack task started" in lower and run.state == "starting":
                    run.state = "running"
                elif "handshake attack cleanup complete" in lower or "handshake attack task finished" in lower:
                    self.finish()
                elif "handshake attack task forcefully stopped" in lower:
                    run.note = "Firmware forced capture to stop; file output may be incomplete."
                    self.finish("error")
                elif "handshake attack cleanup..." in lower:
                    run.cleanup_pending = True
                elif "failed to create handshake attack task" in lower or "failed to enable ap mode" in lower:
                    run.note = line[-110:]
                    self.finish("error")
                elif not run.session:
                    self._legacy(line, run)
            return False
        d = parse_progress(line)
        if not run or not run.active or run.state == "finishing":
            return True
        if d is None:
            run.invalid += 1
            return True
        if run.session is None:
            if d["kind"] not in ("started", "stats") or d["storage"] != self.storage or d["session"] in self.retired:
                return True
            run.session = d["session"]
            # Coarse old log sightings must not be added to packet counts.
            run.rows.clear(); run.ssids.clear(); run.eapol = 0
            run.note = "Progress copies captured frames; CH/RSSI are unavailable."
        if d["session"] != run.session or d["seq"] <= run.seq:
            return True
        if d.get("storage", self.storage) != self.storage:
            return True
        run.gaps += max(0, d["seq"] - run.seq - 1)
        run.seq = d["seq"]
        run.last_progress = time.monotonic()
        if run.state == "starting":
            run.state = "running"
        if d["kind"] == "hs_packet":
            run.accept(d)
        else:
            run.drops = d["drops"]
            if d["kind"] == "stopped":
                run.close()
                run.cleanup_pending = True
                run.state = "finishing"
                run.note = "Capture ending; waiting for file output / cleanup."
        return True

    @staticmethod
    def _legacy(line, run):
        match = re.search(r"\[HS-SNIFF\] EAPOL M([1-4]) captured for '(.*)' \(((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})\)", line)
        if not match:
            return
        message, ssid, bssid = match.groups()
        bssid = bssid.upper()
        at = time.time()
        row = run.rows.setdefault((bssid, ""), dict(bssid=bssid, station="", messages=[0,0,0,0],
            pmkids=None, first=at, last=at, channel=None, rssi=None))
        row["messages"][int(message)-1] += 1
        row["last"] = at
        run.eapol += 1
        run.ssids[bssid] = ssid[:32].encode().hex()
        if len(run.rows) > 512:
            old, _ = run.rows.popitem(last=False)
            run.ssids.pop(old[0], None)
        run.note = "Legacy M# sightings only; PMKID needs ESP firmware 1.7.4+."
