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

PURPLE, ORANGE, CYAN = 2, 9, 3
DEFAULTS = {"flock": True, "axon": True, "precise": True, "trail": False,
            "realert_seconds": 60, "suppressed_rules": [], "suppressed_devices": []}

class WardriveUI:
    def __init__(self, app):
        self.app = app
        self.scan = ScanController(app._send, time.monotonic)
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

    def on_stop(self):
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
            self.connection = connection
            self.scan.reset()
            app._clear_scan_state()
            self.detector.clear()
            self.trail.break_segment()
            if connection:
                self.scan.probe()
        now = time.monotonic()
        self.fixes.update(app.gps.fix, now)
        self.scan.tick()
        if self.scan.error and self.scan.error != self.last_error:
            self.last_error = self.scan.error
            app.msg("[WDG] " + self.last_error, 8)
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
        active = app.wifi_scanning or app.ble_scanning or self.scan.state == "running"
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
        if s.startswith("WDG:"):
            d = parse_record(s)
            if d is None:
                self.invalid_records += 1
                return True
            if self.app.loot:
                self.app.loot.log_serial(s)
            if self.scan.handle(d):
                self.observation(d)
            return True
        # Completion, not the early 'stop command received' message.
        if "all operations stopped" in s.lower() or "all stopped" in s.lower():
            app = self.app
            if self.scan.state != "running":
                app.sniffing = app.capturing_hs = False
                app._bt_tracking = app._bt_airtag = False
                app.state.portal_running = app.state.evil_twin_running = False
            if app._pending_cmd and self.scan.active:
                return True  # wait for this session's structured stopped event first
            if app._pending_cmd:
                cmd, state, name = app._pending_cmd, app._pending_state, app._pending_cmd_name
                app._pending_cmd = None
                if state == "all_wardrive":
                    self.detector.clear()
                    if not self.scan.start():
                        app.msg("[WDG] Combined scan unavailable; check firmware/connection", 8)
                else:
                    app._send(cmd)
                    app._set_running(state, True)
                    if state in ("bt_scanning", "ble_scan"):
                        app._bt_scan_start_time = time.time()
                app.msg("[START] " + name, CYAN)
                return True
            if self.scan.state == "running":
                return True  # delayed legacy text cannot stop a confirmed new session
        return False

    def observation(self, d):
        now = time.monotonic()
        d = dict(d)
        d["fix"] = self.fixes.at(now - d["age_ms"] / 1000) if self.app.gps.available else None
        d["observed_at"] = time.time() - d["age_ms"] / 1000
        if d["kind"] == "wifi":
            net = Network(index="0", bssid=d["mac"], ssid=display_bytes(d["ssid_hex"]),
                          channel=str(d["channel"]), rssi=str(d["rssi"]),
                          auth=d.get("auth", "UNKNOWN"), band="5G" if d["channel"] > 14 else "2.4G")
            self.app._ingest_wifi(net, observation=d)
        elif d["kind"] == "ble":
            name = ble_name(bytes.fromhex(d["data_hex"]))
            self.app._ingest_ble(d["mac"], d["rssi"], name or "?", observation=d)
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
        if self.scan.active and not self.settings_open:
            now = time.monotonic()
            ages = " ".join(k+":"+(str(int(now-self.scan.last_seen[k]))+"s" if k in self.scan.last_seen else "--") for k in ("wifi","ble"))
            text = "Last heard "+ages+" drops:"+str(self.scan.stats.get("drops",0))+" bad:"+str(self.invalid_records)
            if not self.app.gps_fix: text += " GPS unavailable"
            px.rect(4,218,520,12,0)
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
