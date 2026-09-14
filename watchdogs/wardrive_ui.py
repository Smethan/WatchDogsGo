"""Wardrive integration kept outside the main game's rendering and menu code."""
from collections import OrderedDict, deque
import json
from pathlib import Path
import time

from .app_state import Network
from .wardrive_protocol import parse_record, display_bytes
from .scan_controller import ScanController
from .notable_detector import NotableDetector, ble_name
from .wardrive_trail import FixHistory, WardriveTrail
from .passive_capture import PassiveCapture
from .passive_screen import PassiveScreen
from .handshake_capture import HandshakeCapture, COMMANDS, capture_storage
from .handshake_targets import HandshakeTargets, parse_target_record, ERRORS
from .handshake_screen import HandshakeScreen
from .host_ble import HostBleScanner
from .cell_monitor import HostCellScanner

PURPLE, ORANGE, CYAN = 2, 9, 3
DEFAULTS = {"flock": True, "axon": True, "precise": True, "trail": False,
            "realert_seconds": 60, "suppressed_rules": [], "suppressed_devices": []}

class WardriveUI:
    def __init__(self, app):
        self.app = app
        self.scan = ScanController(app._send, time.monotonic)
        self.host_ble = HostBleScanner()
        self.cell = HostCellScanner()
        self.cell_unique = set()
        self.cell_observations = 0
        self.cell_serving = 0
        self.cell_neighbors = 0
        self.cell_latest = None
        self.cell_retry_at = 0
        self.passive = PassiveCapture()
        self.hs_screen = PassiveScreen(app, self)
        self.capture = HandshakeCapture()
        self.targets = HandshakeTargets(time.monotonic)
        self.capture_screen = HandshakeScreen(app, self)
        self.capture_stop_ack = False
        self.detector = NotableDetector()
        self.fixes = FixHistory()
        self.trail = WardriveTrail()
        self.history_trail = None
        self.history_notables = OrderedDict()
        self.history_index = -1
        self.settings_path = Path(app._app_dir) / "wardrive_settings.json"
        self.settings = dict(DEFAULTS)
        try:
            saved = json.loads(self.settings_path.read_text())
            for key, value in DEFAULTS.items():
                if type(saved.get(key)) is type(value):
                    self.settings[key] = saved[key]
        except (OSError, ValueError, TypeError):
            pass
        self.settings_open = False
        self.selection = 0
        self.details = False
        self.detail_selection = 0
        self.notables = OrderedDict()
        self.alerts = deque(maxlen=16)
        self.alert_until = 0
        self.connection = None
        self.store_path = None
        self.invalid_records = 0
        self.last_error = ""
        self.last_probe_error = ""
        self.diagnostic_next = 0
        self.diagnostic_state = ""
        self.reported_false_timeouts = 0
        self.host_ble_batch_lines = deque(maxlen=256)

    def on_stop(self):
        self.targets.cancel()
        if self.app._pending_cmd and self.app._pending_cmd.startswith("hs_scan "):
            self.app._pending_cmd = None
        self.capture.stop()
        self.host_ble.stop()
        self.host_ble_batch_lines.clear()
        self.cell.stop()
        self.scan.stop()
        self.app._clear_scan_state()
        self.app._gps_wait = False
        self.app._gps_wait_cmd = ""
        self.app._gps_wait_dialog = False
        self.trail.break_segment()
        self.detector.clear()

    def tick(self):
        app = self.app
        connection = app.serial if app.serial and app.serial.is_open else None
        if connection is not self.connection:
            if self.scan.diagnostic and self.scan.active:
                self.scan.state = "error"
                self.scan.error = "Serial connection lost or changed"
                self.write_diagnostics(time.monotonic())
            self.host_ble.stop()
            self.host_ble_batch_lines.clear()
            self.cell.stop()
            self.connection = connection
            self.close_passive()
            self.capture.finish("disconnected")
            self.capture_stop_ack = False
            app.capturing_hs = False
            self.scan.reset()
            self.targets.disconnect()
            app._clear_scan_state()
            self.detector.clear()
            self.trail.break_segment()
            if connection:
                self.scan.probe()
        # Legacy stop acknowledgement may precede the no-SD base64 dump.
        # Resume transitions only after its cleanup line was fully processed.
        if self.capture_stop_ack and self.capture.current and not self.capture.current.active:
            self.capture_stop_ack = False
            self.handle_line("All operations stopped.")
        now = time.monotonic()
        self.fixes.update(app.gps.fix, now)
        self.scan.tick()
        if self.targets.tick():
            if app._pending_cmd and app._pending_cmd.startswith("hs_scan "):
                app._pending_cmd = None
            app._send("stop")
        run = self.capture.current
        if run and run.state == "starting" and time.monotonic()-run.started_at > 15:
            run.note = "No capture acknowledgement; stopped. Check firmware and retry."
            self.capture.finish("error")
            app.capturing_hs = False
            app._send("stop")
        self.poll_host_ble(now)
        self.poll_cell(now)
        self.write_diagnostics(now)
        if self.scan.false_timeouts > self.reported_false_timeouts:
            self.reported_false_timeouts = self.scan.false_timeouts
            app._term_add("[TEST] Old heartbeat rule would stop, but valid ESP32 records are still arriving.", raw=True)
            app.msg("[TEST] Stats late; ESP32 data still arriving. Old timeout avoided.", ORANGE)
        if not self.scan.active and self.passive.file:
            self.close_passive()
        if self.scan.probe_error != self.last_probe_error:
            self.last_probe_error = self.scan.probe_error
            if self.last_probe_error:
                app.msg("[WDG] " + self.last_probe_error, 8)
                app._term_add("[WDG] " + self.last_probe_error, raw=True)
        if self.scan.error and self.scan.error != self.last_error:
            self.last_error = self.scan.error
            app.msg("[WDG] " + self.last_error, 8)
            app._term_add("[WDG] " + self.last_error, raw=True)
        session = Path(app.loot.session_path) if app.loot and app.loot.active else None
        path = session / "notable_detections.jsonl" if session else None
        if path != self.store_path:
            self.store_path = path
            self.notables.clear()
            self.alerts.clear()
            if path and path.exists():
                with path.open(encoding="utf-8") as f:
                    for line in f:
                        try:
                            item = json.loads(line)
                            item["fix_seen"] = 0
                            item["seen"] = 0  # restored observations are historical
                            self.notables[item["key"]] = item
                            if len(self.notables) > 1024:
                                self.notables.popitem(last=False)
                        except (ValueError, KeyError, TypeError):
                            pass
            self.trail.set_path(session / "wardrive_trail.jsonl" if session else None)
        active = app.wifi_scanning or app.ble_scanning or (self.scan.state == "running" and self.scan.mode == "wardrive")
        fix = self.fixes.at(now) if app.gps.available else None
        try:
            self.trail.sample(fix, now, self.settings["trail"] and active)
        except OSError as exc:
            app.msg("[TRAIL] " + str(exc)[:70], 8)
        if self.alerts and now >= self.alert_until:
            if self.alert_until:
                self.alerts.popleft()
            self.alert_until = now + 5 if self.alerts else 0

    def handle_line(self, line):
        s = line.strip()
        if s.startswith("HST:"):
            d = parse_target_record(s)
            if d is None:
                self.targets.cancel("Malformed scan result. Press R to rescan.")
            elif d["kind"] == "capture_error":
                run = self.capture.current
                if run and run.state == "starting" and d["storage"] == self.capture.storage:
                    run.note = ERRORS[d["error"]]
                    self.capture.finish("error")
                    self.app.capturing_hs = False
                    self.app.msg(run.note, 8)
            else:
                self.targets.accept(d)
            return True
        if self.capture.handle(s):
            return True
        if self.capture.current and self.capture.current.state in ("stopped", "error", "disconnected"):
            self.app.capturing_hs = False
        if s.startswith("WDG:"):
            d = parse_record(s)
            if d is None:
                self.invalid_records += 1
                return True
            if self.app.loot:
                self.app.loot.log_serial(s)
            if d["kind"] == "capabilities":
                support = "supported" if d.get("wardrive_wifi_serial_v1") is True else "needs firmware 1.7.3+"
                self.app._term_add("[WDG] WiFi-only serial for host BLE: " + support, raw=True)
                protocol = "batched v2" if d.get("wardrive_batch_serial_v2") is True else "legacy stream"
                self.app._term_add("[WDG] All Wardrive transport: " + protocol, raw=True)
            previous = self.scan.state
            if self.scan.handle(d):
                if d["kind"] == "hs_packet":
                    try:
                        self.passive.accept(d)
                    except OSError as exc:
                        self.app.msg("[HS SNIFF] Storage error: " + str(exc)[:60], 8)
                        self.app._send("stop")
                        self.close_passive()
                else:
                    self.observation(d)
            if d["kind"] == "batch_start":
                self.app._term_add(f"[ALL] Background scan #{d['batch']} started (10s)", raw=True)
            elif d["kind"] == "batch_results":
                self.app._term_add(
                    f"[ALL] Results #{d['batch']}: WiFi {d['batch_wifi']} | BLE {d['batch_ble']}", raw=True)
                while self.host_ble_batch_lines:
                    self.app._term_add(self.host_ble_batch_lines.popleft(), raw=True)
            elif d["kind"] == "batch_done":
                self.app._term_add(f"[ALL] Batch #{d['batch']} complete; starting next scan", raw=True)
            if previous == "starting" and self.scan.state == "running" and self.scan.wifi_only:
                if not self.host_ble.start(self.scan.session):
                    self.host_ble_error("Previous Bluetooth scan is still closing; retry shortly")
                else:
                    self.app._term_add("[ALL] Wi-Fi: ESP32 | BLE: uConsole (starting)", raw=True)
            if d["kind"] == "stopped" and not self.scan.active:
                self.host_ble.stop()
                self.cell.stop()
                self.close_passive()
            return True
        # Completion, not the early 'stop command received' message.
        if "all operations stopped" in s.lower() or "all stopped" in s.lower():
            app = self.app
            if self.capture.current and self.capture.current.active and self.capture.current.cleanup_pending:
                self.capture_stop_ack = True
                return True
            self.capture.finish()
            # v2 emits a structured stop first, but this global cleanup line is
            # an independent acknowledgement if USB congestion lost that frame.
            self.scan.accept_plain_stop()
            if self.scan.state != "running":
                app.sniffing = app.capturing_hs = False
                app._bt_tracking = app._bt_airtag = False
                app.state.portal_running = app.state.evil_twin_running = False
            if app._pending_cmd and self.scan.active:
                return True  # wait for this session's structured stopped event first
            if app._pending_cmd:
                cmd, state, name = app._pending_cmd, app._pending_state, app._pending_cmd_name
                app._pending_cmd = None
                if state in ("all_wardrive", "all_wardrive_host", "all_wardrive_test", "hs_sniff"):
                    self.detector.clear()
                    if state == "hs_sniff":
                        if not self.app.loot or not self.app.loot.active:
                            app.msg("[HS SNIFF] No writable loot session; capture cancelled.", 8)
                            return True
                        try:
                            self.passive.open(Path(app.loot.session_path) / "handshakes")
                        except OSError as exc:
                            app.msg("[HS SNIFF] Cannot save capture: " + str(exc)[:60], 8)
                            return True
                    if not self.scan.start("hs_sniff" if state == "hs_sniff" else "wardrive", wifi_only=state == "all_wardrive_host", diagnostic=state == "all_wardrive_test"):
                        self.close_passive()
                        app.msg("[WDG] Scan unavailable; check firmware/connection", 8)
                        return True
                    if state != "hs_sniff":
                        self.host_ble_batch_lines.clear()
                        self.cell_unique.clear()
                        self.cell_observations = self.cell_serving = self.cell_neighbors = 0
                        self.cell_latest = None
                        self.cell.start(self.scan.session)
                    self.last_error = ""
                    self.reported_false_timeouts = 0
                    self.diagnostic_next = 0
                    if self.scan.diagnostic:
                        app._term_add("[TEST] ESP32 WiFi + BLE; comparing stats heartbeat with all valid records.", raw=True)
                        if app.loot and app.loot.active:
                            app._term_add("[TEST] Timing log: " + str(Path(app.loot.session_path) / "wardrive_diagnostics.jsonl"), raw=True)
                else:
                    if capture_storage(cmd):
                        self.capture.start(cmd)
                    elif state == "hs_target_scan" and not self.targets.dispatched(cmd):
                        return True
                    app._send(cmd)
                    app._set_running(state, True)
                    if state in ("bt_scanning", "ble_scan"):
                        app._bt_scan_start_time = time.time()
                app.msg("[START] " + name, CYAN)
                return True
            if self.scan.state == "running":
                return True  # delayed legacy text cannot stop a confirmed new session
        return False

    def host_ble_error(self, message):
        self.scan.error = "uConsole BLE: " + message[:150]
        self.app._term_add("[ALL] " + self.scan.error, raw=True)
        self.app._term_add("[ALL] Enable Bluetooth in the OS and check that bleak is installed.", raw=True)
        self.app._send("stop")

    def write_diagnostics(self, now):
        scan = self.scan
        if not scan.diagnostic or not self.app.loot or not self.app.loot.active:
            return
        if now < self.diagnostic_next and scan.state == self.diagnostic_state:
            return
        if not scan.active and scan.state == self.diagnostic_state:
            return
        self.diagnostic_next = now + 1
        self.diagnostic_state = scan.state
        entry = dict(time=time.time(), session=scan.session, state=scan.state,
                     stats_age=round(now-scan.last_stats, 3),
                     record_age=round(now-scan.last_heartbeat, 3),
                     control_age=round(now-scan.last_control, 3),
                     data_age=round(now-scan.last_data, 3),
                     status_probes=scan.status_attempts,
                     batch=scan.batch_number, batch_phase=scan.batch_phase,
                     received=dict(scan.record_counts), sequence_gaps=scan.seq_gaps,
                     sequence_gap_percent=round(scan.gap_percent, 3),
                     false_timeouts=scan.false_timeouts, firmware_stats=scan.stats,
                     invalid_records=self.invalid_records, error=scan.error)
        try:
            path = Path(self.app.loot.session_path) / "wardrive_diagnostics.jsonl"
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError as exc:
            self.app._term_add("[TEST] Cannot save timing log: " + str(exc), raw=True)

    def poll_host_ble(self, now):
        active = self.scan.state == "running" and self.scan.wifi_only
        if not active:
            self.host_ble.stop()
        for session, kind, data in self.host_ble.poll():
            if not active or session != self.scan.session:
                continue
            if kind == "error":
                self.host_ble_error(data)
                active = False
            elif kind == "started":
                self.host_ble.state = "running"
                self.app._term_add("[ALL] uConsole BLE scanning", raw=True)
            elif kind == "ble":
                received, record = data
                age = max(0, now - received)
                if age > 2:
                    self.host_ble.drops += 1
                    continue
                record["age_ms"] = int(age * 1000)
                self.scan.last_seen["ble"] = received
                # Host BLE must never renew the ESP32 heartbeat.
                if self.scan.batch:
                    name = ble_name(bytes.fromhex(record["data_hex"])) or "?"
                    self.host_ble_batch_lines.append(
                        f"[BLE]  {name[:16]:<16} {record['mac']} {record['rssi']}dBm [host]")
                self.observation(record, terminal=not self.scan.batch)

    def poll_cell(self, now):
        active = self.scan.state in ("starting", "running") and self.scan.mode == "wardrive"
        if not active:
            self.cell.stop()
        elif (self.cell.session != self.scan.session or self.cell.state == "idle"
              or self.cell.state == "error" and now >= self.cell_retry_at):
            if self.cell.state == "error":
                self.cell.stop()
            self.cell.start(self.scan.session)
        for session, kind, data in self.cell.poll():
            if session != self.scan.session or not active:
                continue
            if kind == "started":
                self.cell.state = "starting"
                self.app._term_add("[CELL] Provider starting: " + str(data), raw=True)
                continue
            if kind == "error":
                self.cell.state = "error"
                self.cell.error = str(data)
                self.cell_retry_at = now + 15
                self.app._term_add("[CELL] " + self.cell.error, raw=True)
                self.app.msg("[CELL] Unavailable; WiFi/BLE still running", 8)
                continue
            measured, cells = data
            serving = [cell for cell in cells if cell.serving]
            neighbors = [cell for cell in cells if not cell.serving]
            self.cell_serving, self.cell_neighbors = len(serving), len(neighbors)
            self.cell.state = "full" if neighbors else "serving_only"
            fix = self.fixes.at(now)
            if fix is None:
                self.cell.state = "no_gps"
                continue
            for cell in cells:
                record = cell.record()
                self.cell_unique.add(cell.identity)
                self.cell_latest = record if cell.serving or self.cell_latest is None else self.cell_latest
                if self.app.loot and self.app.loot.active:
                    try:
                        if self.app.loot.save_wardriving_cell(
                                record, observation_fix=fix, observed_at=measured):
                            self.cell_observations += 1
                    except Exception as exc:
                        self.app._term_add("[CELL] Save failed: " + str(exc), raw=True)

    def close_passive(self):
        had_file = self.passive.file is not None
        try:
            self.passive.close()
        except OSError as exc:
            self.app.msg("[HS SNIFF] Save error: " + str(exc)[:60], 8)
        if had_file:
            self.app._term_add("[HS SNIFF] Capture file: " + str(self.passive.path), raw=True)
            self.app.msg(f"[HS SNIFF] EAPOL:{self.passive.eapol} PMKID:{self.passive.pmkids}", CYAN)

    def observation(self, d, terminal=True):
        now = time.monotonic()
        d = dict(d)
        d["fix"] = self.fixes.at(now - d["age_ms"] / 1000) if self.app.gps.available else None
        d["observed_at"] = time.time() - d["age_ms"] / 1000
        if d["kind"] == "wifi":
            net = Network(index="0", bssid=d["mac"], ssid=display_bytes(d["ssid_hex"]),
                          channel=str(d["channel"]), rssi=str(d["rssi"]),
                          auth=d.get("auth", "UNKNOWN"), band="5G" if d["channel"] > 14 else "2.4G")
            self.app._ingest_wifi(net, observation=d, terminal=terminal)
        elif d["kind"] == "ble":
            name = ble_name(bytes.fromhex(d["data_hex"]))
            self.app._ingest_ble(d["mac"], d["rssi"], name or "?", observation=d, terminal=terminal)
        else:
            self.detect(d)

    @staticmethod
    def save_args(observation):
        if observation is None:
            return {}
        return {"observation_fix": observation["fix"], "observed_at": observation["observed_at"]}

    def observe_legacy(self, kind, mac, name, rssi, observation):
        if observation is not None:
            self.detect(observation)
            return
        if not (self.app.wifi_scanning or self.app.ble_scanning):
            return
        d = {"kind": kind, "mac": mac.upper(), "rssi": rssi, "name": name,
             "ssid_hex": name.encode().hex() if kind == "wifi" else "", "data_hex": "",
             "addr_type": None, "fix": self.fixes.at(time.monotonic()), "observed_at": time.time()}
        self.detect(d)

    def detect(self, d):
        now = time.monotonic()
        kind = "wifi" if d["kind"] == "wifi_mgmt" else d["kind"]
        identity = kind + ":" + d["mac"]
        if identity in self.settings["suppressed_devices"]:
            return
        hits = self.detector.classify(d, now, self.app._whitelist.is_blocked, self.settings["suppressed_rules"])
        for hit in hits:
            if not self.settings[hit["category"]]:
                continue
            hit["evidence"] = [h for h in hit["evidence"] if h["id"] not in self.settings["suppressed_rules"]]
            if not hit["evidence"]:
                continue
            hit["strength"] = max(h["strength"] for h in hit["evidence"])
            key = identity + ":" + hit["category"]
            old = self.notables.pop(key, None)
            # Preserve evidence during intermittent sparse advertisements.
            if old and now-old["seen"] < 3 and old["strength"] > hit["strength"]:
                hit = {k:old[k] for k in ("category", "label", "strength", "evidence")}
            item = dict(hit, key=key, identity=identity, mac=d["mac"], kind=kind,
                        name=d.get("name") or (display_bytes(d.get("ssid_hex", "")) if kind=="wifi" else ble_name(bytes.fromhex(d.get("data_hex", "")))),
                        rssi=d["rssi"], first=old["first"] if old else d["observed_at"],
                        last=d["observed_at"], seen=now, fix=d["fix"] or (old["fix"] if old else None),
                        fix_seen=now if d["fix"] else old.get("fix_seen",0) if old else 0)
            # Stored current observation fix stays distinct from last known marker fix.
            item["observation_fix"] = d["fix"]
            self.notables[key] = item
            while len(self.notables) > 1024:
                self.notables.popitem(last=False)
            if self.store_path and (not old or now-old.get("saved",0)>=1):
                item["saved"] = now
                try:
                    with self.store_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(item, separators=(",", ":")) + "\n")
                except OSError as exc:
                    self.app.msg("[DETECT] Save failed: " + str(exc)[:50], 8)
            elif old:
                item["saved"] = old.get("saved",0)
            absence = max(10, self.settings["realert_seconds"])
            if hit["strength"] > 0 and (not old or now-old["seen"] >= absence or hit["strength"] > old["strength"]):
                # One queued alert per identity/category; evidence updates replace it.
                for i, queued in enumerate(self.alerts):
                    if queued["key"] == key:
                        self.alerts[i] = item
                        break
                else:
                    self.alerts.append(item)
                self.app._term_add("[DETECT] " + item["label"] + " " + d["mac"] + " " + ",".join(h["id"] for h in hit["evidence"]), raw=True)

    def is_notable(self, kind, mac):
        return any(kind+":"+mac.upper()+":"+cat in self.notables and self.settings[cat]
                   and self.notables[kind+":"+mac.upper()+":"+cat]["strength"] > 0 for cat in ("flock", "axon"))

    def position(self, item):
        if not item["fix"]:
            return None
        if self.settings["precise"]:
            return item["fix"]["latitude"], item["fix"]["longitude"]
        objects = self.app.wifi_networks if item["kind"] == "wifi" else self.app.ble_devices
        for obj in objects:
            if getattr(obj, "bssid", getattr(obj, "mac", "")).upper() == item["mac"]:
                return obj.lat, obj.lon
        # Probe-only detections have no decorative inventory position.
        return item["fix"]["latitude"], item["fix"]["longitude"]

    def visible_notables(self):
        for item in list(self.history_notables.values()) + list(self.notables.values()):
            if self.settings[item["category"]] and item["strength"] > 0:
                pos = self.position(item)
                if pos:
                    yield item, pos

    def draw_markers(self):
        import pyxel as px
        now = time.monotonic()
        for item, pos in self.visible_notables():
            x, y = self.app.proj.geo_to_screen(*pos)
            if self.app.proj.screen_visible(x,y):
                px.circb(x,y,5,PURPLE)
                if now-item.get("fix_seen",0) < 60:
                    px.circ(x,y,2,PURPLE)
                px.text(x+6,y-3,item["category"][0].upper(),PURPLE)

    def draw_trail(self):
        import pyxel as px
        if not self.settings["trail"]:
            return
        px.clip(0,16,640,218)
        previous = None
        for p in (self.history_trail or self.trail).points:
            xy = self.app.proj.geo_to_screen(p["lat"],p["lon"])
            if previous and previous[0]["segment"] == p["segment"]:
                px.line(*previous[1],*xy,CYAN)
            previous = p,xy
        px.clip()

    def draw_radar(self, rx, ry, rr, scale):
        import pyxel as px
        def xy(lat,lon):
            return rx+(lon-self.app.player_lon)*scale, ry+(self.app.player_lat-lat)*scale
        if self.settings["trail"]:
            previous = None
            for p in (self.history_trail or self.trail).points:
                point = xy(p["lat"],p["lon"])
                if previous and previous[0]["segment"] == p["segment"]:
                    # Clip a segment to the circle analytically before drawing.
                    a,b = previous[1],point
                    dx,dy = b[0]-a[0],b[1]-a[1]
                    qa = dx*dx+dy*dy
                    if qa:
                        qb = 2*((a[0]-rx)*dx+(a[1]-ry)*dy)
                        qc = (a[0]-rx)**2+(a[1]-ry)**2-(rr-1)**2
                        disc = qb*qb-4*qa*qc
                        if disc >= 0:
                            lo,hi = max(0,(-qb-disc**0.5)/(2*qa)),min(1,(-qb+disc**0.5)/(2*qa))
                            if lo<=hi:
                                px.line(a[0]+lo*dx,a[1]+lo*dy,a[0]+hi*dx,a[1]+hi*dy,CYAN)
                previous = p,point
        for item,pos in self.visible_notables():
            x,y = xy(*pos)
            if (x-rx)**2+(y-ry)**2 < (rr-3)**2:
                px.circb(x,y,3,PURPLE)  # ring stays visible under the observer dot
                if time.monotonic()-item.get("fix_seen",0) < 60:
                    px.pset(x,y,PURPLE)

    def cycle_history(self):
        if not self.app.loot:
            return
        root = Path(self.app.loot.session_path).parent
        paths = sorted(root.glob("*/wardrive_trail.jsonl"), reverse=True)
        paths = [p for p in paths if p != self.trail.path]
        self.history_index += 1
        if self.history_index >= len(paths):
            self.history_index = -1
            self.history_trail = None
            self.history_notables.clear()
            return
        path = paths[self.history_index]
        trail = WardriveTrail()
        trail.set_path(path, read_only=True)
        self.history_trail = trail
        self.history_notables.clear()
        notable_path = path.parent / "notable_detections.jsonl"
        if notable_path.exists():
            with notable_path.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        item = json.loads(line)
                        item["seen"] = item["fix_seen"] = 0
                        if item["kind"] not in ("wifi","ble") or item["category"] not in ("flock","axon"):
                            continue
                        self.history_notables[item["key"]] = item
                        if len(self.history_notables)>1024:
                            self.history_notables.popitem(last=False)
                    except (ValueError, KeyError, TypeError):
                        pass

    def update_settings(self):
        import pyxel as px
        if px.btnp(px.KEY_S):
            self.app._send("stop")
            return
        if px.btnp(px.KEY_ESCAPE) or px.btnp(px.KEY_TAB):
            self.settings_open = False
            return
        if px.btnp(px.KEY_H):
            try:
                self.cycle_history()
            except OSError as exc:
                self.app.msg("[TRAIL] Cannot load history: " + str(exc)[:50],8)
        if px.btnp(px.KEY_D):
            self.details = not self.details
        if self.details:
            items = list(self.notables.values())
            if px.btnp(px.KEY_UP): self.detail_selection = max(0,self.detail_selection-1)
            if px.btnp(px.KEY_DOWN): self.detail_selection = min(max(0,len(items)-1),self.detail_selection+1)
            if px.btnp(px.KEY_M) and items:
                identity = items[self.detail_selection]["identity"]
                if identity not in self.settings["suppressed_devices"]:
                    self.settings["suppressed_devices"].append(identity)
                self.alerts = deque((a for a in self.alerts if a["identity"] != identity),maxlen=16)
                self.persist_settings()
            if px.btnp(px.KEY_R):
                self.settings["suppressed_devices"] = []
                self.persist_settings()
            return
        if px.btnp(px.KEY_UP): self.selection = max(0,self.selection-1)
        if px.btnp(px.KEY_DOWN): self.selection = min(3,self.selection+1)
        if px.btnp(px.KEY_RETURN):
            key = ("flock","axon","precise","trail")[self.selection]
            self.settings[key] = not self.settings[key]
            if key == "trail": self.trail.break_segment()
            self.persist_settings()

    def persist_settings(self):
        try:
            temp = self.settings_path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.settings,indent=2)+"\n")
            temp.replace(self.settings_path)
        except OSError as exc:
            self.app.msg("[WDG] Settings save failed: " + str(exc)[:50],8)

    def draw_overlay(self):
        import pyxel as px
        if self.settings_open:
            px.camera(0,-24)
            px.rect(40,35,560,285,0)
            px.rectb(40,35,560,285,PURPLE)
            px.text(55,47,"WARDRIVE SETTINGS   arrows / ENTER / ESC",7)
            labels = ("Flock detection", "Axon detection", "Precise Flock/Axon markers", "Wardrive trail")
            for i,(key,label) in enumerate(zip(("flock","axon","precise","trail"),labels)):
                px.text(55,70+i*15,("> " if i==self.selection else "  ")+label+": "+("ON" if self.settings[key] else "OFF"),11 if i==self.selection else 7)
            px.text(55,137,"Precise = where YOU heard it, not the camera location.",13)
            route = self.history_trail.path.parent.name if self.history_trail else "current session"
            px.text(55,149,"[H] Route history: " + route,13)
            px.text(55,165,"[D] DETECTIONS   [M] mute selected   [R] reset mutes",7)
            items = list(self.notables.values())
            offset = max(0,self.detail_selection-5) if self.details else max(0,len(items)-6)
            for i,item in enumerate(items[offset:offset+6]):
                selected = self.details and i+offset == self.detail_selection
                label = ("> " if selected else "  ")+item["label"]+" "+item["mac"]+" "+str(item["rssi"])+"dBm"
                if item["identity"] in self.settings["suppressed_devices"]: label += " MUTED"
                px.text(55,180+i*15,label[:104],7 if selected else ORANGE if item["category"]=="axon" else 14)
            if self.details and items:
                item = items[min(self.detail_selection,len(items)-1)]
                px.text(55,280,("Rules: "+", ".join(h["id"] for h in item["evidence"]))[:104],13)
                px.text(55,293,("Heard: "+time.strftime("%H:%M:%S",time.localtime(item["last"]))+"  "+("GPS recorded" if item["observation_fix"] else "GPS unavailable")),13)
            px.camera()
            self.app._draw_mc_toast()
        if self.scan.active and not self.settings_open and not self.hs_screen.open and not self.capture_screen.open:
            if self.scan.mode == "hs_sniff":
                text = f"Passive -> uConsole | EAPOL:{self.passive.eapol} PMKID:{self.passive.pmkids}"
                text += f" drops:{self.scan.stats.get('drops',0)} incomplete:{self.passive.lost}"
            else:
                now = time.monotonic()
                ages = " ".join(k+":"+(str(int(now-self.scan.last_seen[k]))+"s" if k in self.scan.last_seen else "--") for k in ("wifi","ble"))
                text = "Last heard "+ages+" drops:"+str(self.scan.stats.get("drops",0))+" bad:"+str(self.invalid_records)
                if self.scan.wifi_only:
                    text = "ESP WiFi / host BLE:"+self.host_ble.state+" "+ages+" drops:"+str(self.scan.stats.get("drops",0))+"/"+str(self.host_ble.drops)
                elif self.scan.diagnostic:
                    text = f"TEST ctl:{now-self.scan.last_control:.1f}s data:{now-self.scan.last_data:.1f}s probes:{self.scan.status_attempts} gaps:{self.scan.seq_gaps} ({self.scan.gap_percent:.1f}%)"
                if self.scan.batch:
                    if self.scan.batch_phase == "scanning":
                        elapsed = min(10, max(0, now-self.scan.batch_started))
                        text = f"Background scan #{self.scan.batch_number} {elapsed:.0f}/10s | " + text
                    elif self.scan.batch_phase == "results":
                        counts = self.scan.batch_counts
                        text = f"Results #{self.scan.batch_number} WiFi:{counts['wifi']} BLE:{counts['ble']} | " + text
                    elif self.scan.batch_phase == "next":
                        text = f"Batch #{self.scan.batch_number} complete; next scan | " + text
                elif self.scan.legacy:
                    text = "LEGACY STREAM | " + text
                if not self.app.gps_fix: text += " GPS unavailable"
                cell = " CELL:" + self.cell.state.upper()
                cell += f" {len(self.cell_unique)}/{self.cell_observations}"
                if self.cell.provider:
                    cell += " " + self.cell.provider.replace("modemmanager", "MM").replace("sim7600_at", "AT")
                if self.cell_latest:
                    cell += " " + self.cell_latest["technology"] + " " + str(round(self.cell_latest.get("signal_dbm") or 0)) + "dBm"
                text += cell
            px.rect(4,218,632,12,0)
            px.text(6,220,text,9 if self.scan.stats.get("drops",0) else 13)
        if self.alerts:
            item = self.alerts[0]
            color = ORANGE if item["category"]=="axon" else PURPLE
            # Separate from MeshCore's y=20..52 toast, above every menu.
            px.rect(55,56,530,39,0)
            px.rectb(55,56,530,39,color)
            px.rect(56,57,528,14,color)
            px.text(62,61,item["label"].upper()+"  "+item["mac"],7 if color==PURPLE else 0)
            px.text(62,74,(item["name"][:22]+"  "+str(item["rssi"])+"dBm  "+("heard here" if item["observation_fix"] else "GPS unavailable")),7)
            px.text(62,84,(", ".join(h["method"] for h in item["evidence"]))[:95],13)
