"""Acknowledged scan lifecycle with separate control and observation clocks."""
import secrets
from collections import Counter


class ScanController:
    CONTROL_KINDS = {"started", "stopped", "error", "heartbeat", "status",
                     "batch_start", "batch_results", "batch_done", "stats"}

    def __init__(self, send, clock):
        self.send, self.clock = send, clock
        self.reset()

    def reset(self):
        self.supported = self.hs_supported = self.capture_targets_supported = None
        self.capture_exclusions_supported = self.capture_pcapng_supported = None
        self.wifi_supported = None
        self.batch_supported = self.wifi_batch_supported = False
        self.wifi_only = self.diagnostic = self.batch = self.legacy = False
        self.record_counts = Counter()
        self.seq_gaps = self.false_timeouts = 0
        self._old_timeout = self._control_warned = False
        now = self.clock()
        self.last_stats = self.last_control = self.last_data = self.last_heartbeat = now
        self.mode = "wardrive"
        self.probing = False
        self.probe_deadline = self.next_probe = 0
        self.probe_attempts = 0
        self.probe_error = ""
        self.session = ""
        self.state = "idle"
        self.seq = 0
        self.last_seen = {}
        self.stats = {}
        self.batch_number = 0
        self.batch_phase = "idle"
        self.batch_started = 0
        self.batch_counts = {"wifi": 0, "ble": 0}
        self.next_keepalive = self.deadline = self.next_status_probe = 0
        self.status_attempts = 0
        self.error = ""

    @property
    def active(self):
        return self.state in ("starting", "running", "stopping")

    @property
    def gap_percent(self):
        return 100 * self.seq_gaps / self.seq if self.seq else 0.0

    def probe(self):
        if self.active or self.probing:
            return False
        self.supported = self.hs_supported = self.capture_targets_supported = None
        self.capture_exclusions_supported = self.capture_pcapng_supported = None
        self.wifi_supported = None
        self.batch_supported = self.wifi_batch_supported = False
        self.probing = True
        self.probe_error = ""
        self.probe_attempts = 1
        self.probe_deadline = self.clock() + 8
        self.next_probe = self.clock() + 2
        self.send("get_capabilities")
        return True

    def start(self, mode="wardrive", wifi_only=False, diagnostic=False):
        supported = self.hs_supported if mode == "hs_sniff" else (
            self.wifi_supported if wifi_only else self.supported)
        if not supported or self.active:
            return False
        self.mode = mode
        self.wifi_only = wifi_only and mode == "wardrive"
        self.diagnostic = diagnostic
        self.batch = mode == "wardrive" and (
            self.wifi_batch_supported if self.wifi_only else self.batch_supported)
        self.legacy = mode == "wardrive" and not self.batch
        self.record_counts.clear()
        self.seq_gaps = self.false_timeouts = 0
        self._old_timeout = self._control_warned = False
        now = self.clock()
        self.last_stats = self.last_control = self.last_data = self.last_heartbeat = now
        self.session = secrets.token_hex(8)
        self.seq = 0
        self.last_seen.clear()
        self.stats.clear()
        self.batch_number = 0
        self.batch_phase = "starting"
        self.batch_started = now
        self.batch_counts = {"wifi": 0, "ble": 0}
        self.error = ""
        self.state = "starting"
        self.deadline = now + (12 if self.batch else 8)
        self.next_status_probe = now + 4
        self.status_attempts = 0
        self.next_keepalive = now + 5
        if mode == "hs_sniff":
            command = "start_hs_sniff_serial"
        elif self.batch:
            command = "start_wardrive_wifi_batch_serial" if self.wifi_only else "start_wardrive_batch_serial"
        else:
            command = "start_wardrive_wifi_serial" if self.wifi_only else "start_wardrive_serial"
        self.send(command + " " + self.session)
        return True

    def stop(self):
        if self.active:
            now = self.clock()
            self.state = "stopping"
            self.deadline = now + (8 if self.batch else 6)
            self.next_status_probe = now + 4
            self.status_attempts = 0

    def accept_plain_stop(self):
        """Use the firmware's global completion line as a v2 stop fallback."""
        if self.batch and self.state == "stopping":
            self.state = "idle"
            self.batch_phase = "stopped"
            return True
        return False

    def handle(self, d):
        if d["kind"] == "capabilities":
            self.supported = d["wardrive_serial_v1"]
            self.hs_supported = d.get("hs_sniff_serial_v1", False) is True
            self.capture_targets_supported = d.get("hs_capture_targets_v1", False) is True
            self.capture_exclusions_supported = d.get("hs_capture_exclusions_v1", False) is True
            self.capture_pcapng_supported = d.get("hs_capture_pcapng_v1", False) is True
            self.wifi_supported = d.get("wardrive_wifi_serial_v1", False) is True
            self.batch_supported = d.get("wardrive_batch_serial_v2", False) is True
            self.wifi_batch_supported = d.get("wardrive_wifi_batch_serial_v2", False) is True
            self.probing = False
            self.probe_error = ""
            return False
        if not self.active or d["session"] != self.session or d["seq"] <= self.seq:
            return False
        now = self.clock()
        self.seq_gaps += max(0, d["seq"] - self.seq - 1)
        self.seq = d["seq"]
        kind = d["kind"]
        self.record_counts[kind] += 1
        if kind in self.CONTROL_KINDS:
            self.last_control = now
            self._control_warned = False
            self.last_heartbeat = now
        if kind in ("wifi", "wifi_mgmt", "ble", "hs_packet"):
            self.last_data = now
            self.last_heartbeat = now
        if kind == "started" and self.state == "starting":
            self.state = "running"
        elif kind == "status":
            remote = d.get("state")
            if remote in ("running", "reporting") and self.state == "starting":
                self.state = "running"
            elif remote == "stopped" and self.active:
                self.state = "idle"
        elif kind == "stopped":
            self.state = "idle"
        elif kind == "error":
            self.error = str(d.get("message", "Firmware error"))[:80]
            self.state = "stopping"
            self.deadline = now + (8 if self.batch else 6)
        if kind in ("started", "stats", "heartbeat", "status", "batch_start",
                    "batch_results", "batch_done"):
            self.last_stats = now
            self.stats = d
        if d.get("v", 1) == 2:
            self.batch_number = d.get("batch", self.batch_number)
            if kind == "batch_start":
                self.batch_phase = "scanning"
                self.batch_started = now
                self.batch_counts = {"wifi": 0, "ble": 0}
            elif kind == "batch_results":
                self.batch_phase = "results"
                self.batch_counts = {"wifi": d["batch_wifi"], "ble": d["batch_ble"]}
            elif kind == "batch_done":
                self.batch_phase = "next"
                self.batch_counts = {"wifi": d["batch_wifi"], "ble": d["batch_ble"]}
            elif kind == "stopped":
                self.batch_phase = "stopped"
        if kind in ("wifi", "ble"):
            self.last_seen[kind] = now
        if self.mode == "hs_sniff":
            return self.state in ("running", "stopping") and kind == "hs_packet"
        return self.state == "running" and kind in ("wifi", "wifi_mgmt", "ble")

    def _status(self):
        if self.batch and self.session:
            self.status_attempts += 1
            self.send("wardrive_status " + self.session)

    def tick(self):
        now = self.clock()
        if (self.legacy and self.state == "running" and now-self.last_stats > 7
                and now-self.last_data <= 7 and not self._old_timeout):
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
        if self.state == "starting":
            if self.batch and now >= self.next_status_probe and now <= self.deadline:
                self._status()
                self.next_status_probe = now + 4
            if now > self.deadline:
                self.state = "error"
                self.error = "Scan start acknowledgement timed out; STOP before retry"
                self.send("stop")
        elif self.state == "stopping":
            if self.batch and now >= self.next_status_probe and now <= self.deadline:
                self._status()
                self.next_status_probe = now + 4
            if now > self.deadline:
                self.state = "error"
                self.error = "Scan stop acknowledgement timed out; firmware may be disconnected"
                self.send("stop")
        elif self.state == "running":
            if self.batch:
                control_age = now-self.last_control
                data_age = now-self.last_data
                if control_age > 6 and not self._control_warned:
                    self._control_warned = True
                    self._status()
                    self.next_status_probe = now + 4
                elif self._control_warned and now >= self.next_status_probe and control_age <= 15:
                    self._status()
                    self.next_status_probe = now + 5
                if control_age > 15 and data_age > 15:
                    self.error = "No ESP32 control or scan records for 15s; stopping"
                    self.stop()
                    self.send("stop")
            elif now-self.last_heartbeat > 7:
                self.error = "No firmware records for 7s; stopping"
                self.stop()
                self.send("stop")
        if self.state in ("starting", "running") and now >= self.next_keepalive:
            self.next_keepalive = now + 5
            self.send("wardrive_keepalive " + self.session)
