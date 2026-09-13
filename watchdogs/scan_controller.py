"""Acknowledged scan lifecycle with monotonic deadlines and liveness diagnostics."""
import secrets
from collections import Counter

class ScanController:
    def __init__(self, send, clock):
        self.send, self.clock = send, clock
        self.reset()

    def reset(self):
        self.supported = None
        self.hs_supported = None
        self.capture_targets_supported = None
        self.wifi_supported = None
        self.wifi_only = False
        self.diagnostic = False
        self.record_counts = Counter()
        self.seq_gaps = 0
        self.false_timeouts = 0
        self._old_timeout = False
        self.last_stats = self.clock()
        self.mode = "wardrive"
        self.probing = False
        self.probe_deadline = 0
        self.next_probe = 0
        self.probe_attempts = 0
        self.probe_error = ""
        self.session = ""
        self.state = "idle"
        self.seq = 0
        self.last_seen = {}
        self.stats = {}
        self.last_heartbeat = self.clock()
        self.next_keepalive = 0
        self.deadline = 0
        self.error = ""

    @property
    def active(self):
        return self.state in ("starting", "running", "stopping")

    @property
    def gap_percent(self):
        """Missing serial sequence positions, not over-the-air loss or devices."""
        return 100 * self.seq_gaps / self.seq if self.seq else 0.0

    def probe(self):
        if self.active or self.probing:
            return False
        self.supported = None
        self.hs_supported = None
        self.capture_targets_supported = None
        self.wifi_supported = None
        self.probing = True
        self.probe_error = ""
        self.probe_attempts = 1
        self.probe_deadline = self.clock() + 8
        self.next_probe = self.clock() + 2
        self.send("get_capabilities")
        return True

    def start(self, mode="wardrive", wifi_only=False, diagnostic=False):
        if mode == "hs_sniff":
            supported = self.hs_supported
        else:
            supported = self.wifi_supported if wifi_only else self.supported
        if not supported or self.active:
            return False
        self.mode = mode
        self.wifi_only = wifi_only and mode == "wardrive"
        self.diagnostic = diagnostic
        self.record_counts.clear()
        self.seq_gaps = self.false_timeouts = 0
        self._old_timeout = False
        self.last_stats = self.clock()
        self.session = secrets.token_hex(8)
        self.seq = 0
        self.last_seen.clear()
        self.stats.clear()
        self.error = ""
        self.state = "starting"
        self.deadline = self.clock() + 8
        self.last_heartbeat = self.clock()
        self.next_keepalive = self.clock() + 5
        command = "start_hs_sniff_serial" if mode == "hs_sniff" else "start_wardrive_serial"
        if self.wifi_only:
            command = "start_wardrive_wifi_serial"
        self.send(command + " " + self.session)
        return True

    def stop(self):
        if self.active:
            self.state = "stopping"
            self.deadline = self.clock() + 6

    def handle(self, d):
        if d["kind"] == "capabilities":
            self.supported = d["wardrive_serial_v1"]
            self.hs_supported = d.get("hs_sniff_serial_v1", False) is True
            self.capture_targets_supported = d.get("hs_capture_targets_v1", False) is True
            self.wifi_supported = d.get("wardrive_wifi_serial_v1", False) is True
            self.probing = False
            self.probe_error = ""
            return False
        if not self.active or d["session"] != self.session or d["seq"] <= self.seq:
            return False
        self.seq_gaps += max(0, d["seq"] - self.seq - 1)
        self.seq = d["seq"]
        self.record_counts[d["kind"]] += 1
        # Any validated, current-session record proves the firmware is alive.
        # A dropped stats line must not terminate an otherwise flowing capture.
        self.last_heartbeat = self.clock()
        kind = d["kind"]
        if kind == "started" and self.state == "starting":
            self.state = "running"
        elif kind == "stopped":
            self.state = "idle"
        elif kind == "error":
            self.error = str(d.get("message", "Firmware error"))[:80]
            self.state = "stopping"
            self.deadline = self.clock() + 6
        if kind in ("started", "stats"):
            self.last_stats = self.clock()
            self._old_timeout = False
            self.last_heartbeat = self.clock()
            self.stats = d
        if kind in ("wifi", "ble"):
            self.last_seen[kind] = self.clock()
        if self.mode == "hs_sniff":
            return self.state in ("running", "stopping") and kind == "hs_packet"
        return self.state == "running" and kind in ("wifi", "wifi_mgmt", "ble")

    def tick(self):
        now = self.clock()
        if (self.state == "running" and now - self.last_stats > 7
                and now - self.last_heartbeat <= 7 and not self._old_timeout):
            self.false_timeouts += 1
            self._old_timeout = True
        if self.probing:
            if now >= self.probe_deadline:
                self.probing = False
                self.probe_error = "No capability reply; check serial connection or retry All Wardrive"
            elif now >= self.next_probe and self.probe_attempts < 3:
                self.probe_attempts += 1
                self.next_probe = now + 2
                self.send("get_capabilities")
        if self.state in ("starting", "stopping") and now > self.deadline:
            self.state = "error"
            self.error = "Scan acknowledgement timed out; STOP before retry"
            self.send("stop")
        elif self.state == "running" and now - self.last_heartbeat > 7:
            self.error = "No firmware records for 7s; stopping"
            self.stop()
            self.send("stop")
        if self.state in ("starting", "running") and now >= self.next_keepalive:
            self.next_keepalive = now + 5
            self.send("wardrive_keepalive " + self.session)
