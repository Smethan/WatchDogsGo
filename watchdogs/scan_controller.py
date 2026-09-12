"""Acknowledged continuous scan lifecycle, independent of the UI frame rate."""
import secrets

class ScanController:
    def __init__(self, send, clock):
        self.send, self.clock = send, clock
        self.reset()

    def reset(self):
        self.supported = None
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

    def probe(self):
        if self.active or self.probing:
            return False
        self.supported = None
        self.probing = True
        self.probe_error = ""
        self.probe_attempts = 1
        self.probe_deadline = self.clock() + 8
        self.next_probe = self.clock() + 2
        self.send("get_capabilities")
        return True

    def start(self):
        if not self.supported or self.active:
            return False
        self.session = secrets.token_hex(8)
        self.seq = 0
        self.last_seen.clear()
        self.stats.clear()
        self.error = ""
        self.state = "starting"
        self.deadline = self.clock() + 8
        self.last_heartbeat = self.clock()
        self.next_keepalive = self.clock() + 5
        self.send("start_wardrive_serial " + self.session)
        return True

    def stop(self):
        if self.active:
            self.state = "stopping"
            self.deadline = self.clock() + 6

    def handle(self, d):
        if d["kind"] == "capabilities":
            self.supported = d["wardrive_serial_v1"]
            self.probing = False
            self.probe_error = ""
            return False
        if not self.active or d["session"] != self.session or d["seq"] <= self.seq:
            return False
        self.seq = d["seq"]
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
            self.last_heartbeat = self.clock()
            self.stats = d
        if kind in ("wifi", "ble"):
            self.last_seen[kind] = self.clock()
        return self.state == "running" and kind in ("wifi", "wifi_mgmt", "ble")

    def tick(self):
        now = self.clock()
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
            self.error = "Firmware heartbeat lost; stopping"
            self.stop()
            self.send("stop")
        if self.state in ("starting", "running") and now >= self.next_keepalive:
            self.next_keepalive = now + 5
            self.send("wardrive_keepalive " + self.session)
