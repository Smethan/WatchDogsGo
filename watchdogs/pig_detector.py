"""Passive, evidence-graded Pig Detector observations and map geometry.

Coordinates are typed by provenance: a receiver fix is never a transmitter
position.  RSSI areas are experimental and deliberately withheld unless a
moving receiver supplied enough independent geometry.
"""
from __future__ import annotations

from collections import OrderedDict, deque
import json
import math
from pathlib import Path
import statistics
import time
import uuid


MAX_LIVE = 512
MAX_IDENTITIES = 128
MAX_JSONL_BYTES = 4 * 1024 * 1024
EARTH_METERS_PER_DEGREE = 111_320.0


def _lat_lon(fix):
    if not isinstance(fix, dict):
        return None
    lat = fix.get("latitude", fix.get("lat"))
    lon = fix.get("longitude", fix.get("lon"))
    if (type(lat) not in (int, float) or type(lon) not in (int, float)
            or not math.isfinite(lat) or not math.isfinite(lon)
            or not -90 <= lat <= 90 or not -180 <= lon <= 180):
        return None
    return float(lat), float(lon)


def estimate_rssi_area(samples):
    """Fit a deliberately coarse source area; return None for poor geometry.

    A log-distance fit with unknown transmit power/path-loss exponent creates
    candidate locations.  This is a model uncertainty area, not triangulation
    or a measured location.
    """
    points = [(pos[0], pos[1], float(rssi)) for pos, rssi in samples
              if pos is not None and -127 <= rssi <= 20][-64:]
    if len(points) < 6:
        return None
    lat0 = statistics.mean(p[0] for p in points)
    lon0 = statistics.mean(p[1] for p in points)
    cos_lat = max(0.1, math.cos(math.radians(lat0)))
    xy = [((lon-lon0)*EARTH_METERS_PER_DEGREE*cos_lat,
           (lat-lat0)*EARTH_METERS_PER_DEGREE, rssi)
          for lat, lon, rssi in points]
    xs, ys = [p[0] for p in xy], [p[1] for p in xy]
    span = max(max(xs)-min(xs), max(ys)-min(ys))
    if span < 30 or max(xs)-min(xs) < 10 or max(ys)-min(ys) < 10:
        return None
    # Reject almost straight routes, where either side of the road fits.
    max_area2 = max(abs((b[0]-a[0])*(c[1]-a[1])
                        -(b[1]-a[1])*(c[0]-a[0]))
                    for a in xy[::max(1, len(xy)//12)]
                    for b in xy[::max(1, len(xy)//12)]
                    for c in xy[::max(1, len(xy)//12)])
    if max_area2 < 300 or max(p[2] for p in xy)-min(p[2] for p in xy) < 6:
        return None
    margin = max(100.0, span)
    step = max(15.0, (span + 2*margin)/30)
    xlow, xhigh = min(xs)-margin, max(xs)+margin
    ylow, yhigh = min(ys)-margin, max(ys)+margin
    candidates = []
    ix_count = min(41, int((xhigh-xlow)/step)+1)
    iy_count = min(41, int((yhigh-ylow)/step)+1)
    for ix in range(ix_count):
        cx = xlow + ix*step
        for iy in range(iy_count):
            cy = ylow + iy*step
            distances = [max(3.0, math.hypot(cx-x, cy-y)) for x,y,_ in xy]
            for exponent in (2.0, 3.0, 4.0):
                intercept = statistics.median(
                    rssi + 10*exponent*math.log10(d)
                    for (_,_,rssi),d in zip(xy,distances))
                residuals = [rssi-(intercept-10*exponent*math.log10(d))
                             for (_,_,rssi),d in zip(xy,distances)]
                score = sum(min(144.0, r*r) for r in residuals)/len(residuals)
                candidates.append((score, cx, cy, ix, iy))
    if not candidates:
        return None
    best = min(item[0] for item in candidates)
    if best > 36.0:
        return None
    near = [item for item in candidates if item[0] <= best + 4.0]
    # A candidate touching the search boundary means the source is not bounded.
    if (not near or any(ix in (0, ix_count-1) or iy in (0, iy_count-1)
                        for _,_,_,ix,iy in near)):
        return None
    center_x = statistics.mean(p[1] for p in near)
    center_y = statistics.mean(p[2] for p in near)
    radius = max(30.0, max(math.hypot(x-center_x,y-center_y)
                            for _,x,y,_,_ in near) + step)
    if radius > 1000:
        return None
    return {"latitude": lat0 + center_y/EARTH_METERS_PER_DEGREE,
            "longitude": lon0 + center_x/(EARTH_METERS_PER_DEGREE*cos_lat),
            "radius_m": round(radius), "samples": len(points),
            "fit_error_db": round(math.sqrt(best), 1)}


class PigDetector:
    def __init__(self, settings):
        self.settings = settings
        self.active = False
        self.owns_scan = False
        self.owned_group = None
        self.owned_session = None
        self.owned_serial = None
        self.owns_sdr = False
        self.owned_sdr_mode = None
        self.store_path = None
        self.live = OrderedDict()
        self.routes = OrderedDict()
        self.broadcast_tracks = OrderedDict()
        self.estimates = OrderedDict()
        self.last_saved = {}
        self.last_alert = {}
        self.last_estimate = {}
        self.status = "OFF"
        self.session_id = ""
        self.sequence = 0
        self.estimate_budget_at = 0.0

    def start(self, session_dir=None):
        self.active = True
        self.owns_scan = self.owns_sdr = False
        self.owned_sdr_mode = None
        self.owned_group = self.owned_session = self.owned_serial = None
        self.status = "STARTING"
        self.session_id = uuid.uuid4().hex[:16]
        self.sequence = 0
        self.live.clear()
        self.routes.clear()
        self.broadcast_tracks.clear()
        self.estimates.clear()
        self.last_saved.clear()
        self.last_alert.clear()
        self.last_estimate.clear()
        self.store_path = Path(session_dir) / "pig_observations.jsonl" if session_dir else None

    def stop(self):
        self.active = False
        self.owns_scan = self.owns_sdr = False
        self.owned_sdr_mode = None
        self.owned_group = self.owned_session = self.owned_serial = None
        self.status = "OFF"
        self.store_path = None

    def _emit(self, key, record, *, interval=1.0):
        if not self.active:
            return False
        now = time.monotonic()
        record = dict(record, v=1)
        self.sequence += 1
        if record.get("observed_at") is None:
            record["observed_at"] = time.time()
        record["receiver"] = record.get("receiver") or "host"
        record["session"] = record.get("session") or self.session_id
        record["seq"] = record.get("seq") or self.sequence
        record["seen"] = now
        self.live[key] = record
        self.live.move_to_end(key)
        while len(self.live) > MAX_LIVE:
            self.live.popitem(last=False)
        if self.store_path and now-self.last_saved.get(key, -1e9) >= interval:
            try:
                if (not self.store_path.exists()
                        or self.store_path.stat().st_size < MAX_JSONL_BYTES):
                    durable = {k:v for k,v in record.items() if k != "seen"}
                    line = json.dumps(durable, separators=(",", ":")) + "\n"
                    if (not self.store_path.exists() or
                            self.store_path.stat().st_size + len(line.encode()) <= MAX_JSONL_BYTES):
                        with self.store_path.open("a", encoding="utf-8") as stream:
                            stream.write(line)
                        self.last_saved[key] = now
                        if len(self.last_saved) > MAX_LIVE:
                            self.last_saved.pop(next(iter(self.last_saved)))
            except OSError:
                self.status = "SAVE ERROR"
        return True

    def ingest_notable(self, d, hit):
        if not self.active:
            return False
        category = hit["category"]
        source = "wifi" if d["kind"].startswith("wifi") else "ble"
        identity = f"{source}:{d['mac'].upper()}"
        if identity in self.settings.get("suppressed_devices", ()):
            return False
        strength = hit["strength"]
        tier = "product" if strength >= 2 else "lead"
        stable = ((source == "wifi" and not (int(d["mac"][:2], 16) & 2))
                  or (source == "ble" and d.get("addr_type") == 0))
        record = {
            "source": source, "category": category, "identity": identity,
            "stable_identity": stable, "tier": tier, "label": hit["label"],
            "evidence": hit["evidence"], "receiver": d.get("device_id"),
            "session": d.get("source_session", d.get("session")),
            "seq": d.get("seq"), "channel": d.get("channel"),
            "signal_dbm": d.get("rssi"), "observer_fix": d.get("fix"),
            "broadcast_position": None, "observed_at": d.get("observed_at"),
        }
        key = f"notable:{category}:{identity}"
        self._emit(key, record, interval=2.0)
        pos = _lat_lon(d.get("fix"))
        if stable and pos is not None and type(d.get("rssi")) is int:
            route = self.routes.setdefault(key, deque(maxlen=64))
            if not route or time.monotonic()-route[-1][2] >= 1.0:
                route.append((pos, d["rssi"], time.monotonic()))
            self.routes.move_to_end(key)
            while len(self.routes) > MAX_IDENTITIES:
                self.routes.popitem(last=False)
            now = time.monotonic()
            if (self.settings.get("pig_estimates", True)
                    and now-self.last_estimate.get(key, 0) >= 20
                    and now >= self.estimate_budget_at):
                self.last_estimate[key] = now
                self.estimate_budget_at = now + 2
                if len(self.last_estimate) > MAX_IDENTITIES:
                    self.last_estimate.pop(next(iter(self.last_estimate)))
                area = estimate_rssi_area([(p,r) for p,r,t in route if now-t <= 300])
                if area:
                    self.estimates[key] = dict(area, seen=now, label=hit["label"])
                    self.estimates.move_to_end(key)
                    while len(self.estimates) > MAX_IDENTITIES:
                        self.estimates.popitem(last=False)
                else:
                    self.estimates.pop(key, None)
        if category in ("flock", "axon") or tier != "product":
            return False  # Existing WDG alerts Flock/Axon; leads stay quiet.
        now = time.monotonic()
        if (self.settings.get("pig_product_alerts", True)
                and now-self.last_alert.get(key, -1e9) >= 60):
            self.last_alert[key] = now
            if len(self.last_alert) > MAX_LIVE:
                self.last_alert.pop(next(iter(self.last_alert)))
            return True
        return False

    def ingest_rf(self, data, observer_fix):
        hz = data.get("frequency_hz")
        if type(hz) is not int:
            return
        self._emit(f"rf:{hz}", {
            "source": "sdr_rf", "category": (
                "rf_activity" if data.get("above_threshold") else "rf_coverage"),
            "tier": "lead" if data.get("above_threshold") else "neutral",
            "identity": None, "stable_identity": False,
            "label": data.get("label", f"{hz/1e6:.4f} MHz")[:40],
            "frequency_hz": hz, "bandwidth_hz": data.get("bandwidth_hz"),
            "power_db": data.get("power_db"),
            "noise_floor_db": data.get("noise_floor_db"),
            "threshold_db": data.get("threshold_db"),
            "above_threshold": data.get("above_threshold"),
            "last_visit": data.get("last_visit"),
            "revisit_seconds": data.get("revisit_seconds"),
            "observer_fix": observer_fix, "broadcast_position": None,
        }, interval=5)

    def ingest_p25(self, data, observer_fix):
        system = str(data.get("sysid") or data.get("system_id") or
                     data.get("system_label") or "")
        tgid = str(data.get("talkgroup", ""))
        mapped = self.settings.get("pig_talkgroups", {}).get(f"{system}:{tgid}")
        self._emit(f"p25:{system}:{tgid or 'site'}", {
            "source": "p25", "category": "network_activity",
            "tier": "configured" if mapped else "lead",
            "identity": None, "stable_identity": False,
            "label": ("Configured network activity: " + mapped) if mapped
                     else "P25 system/site activity",
            "frequency_hz": data.get("control_frequency_hz"),
            "system_id": system, "site": data.get("site"),
            "rfss": data.get("rfss"), "talkgroup": tgid or None,
            "decoded_blocks": data.get("decoded_blocks"),
            "decode_blocks_per_min": data.get("decode_blocks_per_min"),
            "observer_fix": observer_fix, "broadcast_position": None,
        }, interval=5)

    def ingest_sensor433(self, sensor, observer_fix):
        """Expose existing rtl_433 decodes as neutral heard-at observations."""
        if not self.active:
            return
        key = f"sensor433:{str(sensor.sid)[:80]}"
        previous = self.live.get(key)
        if previous and previous.get("source_seen") == sensor.last_seen:
            return
        self._emit(key, {
            "source": "rtl_433", "category": "sensor", "tier": "neutral",
            "identity": str(sensor.sid)[:80], "stable_identity": False,
            "label": str(sensor.model)[:40], "receiver": "aio-sdr",
            "frequency_hz": 433_920_000,
            "signal_level_db": sensor.data.get("rssi"),
            "observer_fix": observer_fix, "broadcast_position": None,
            "observed_at": sensor.last_seen, "source_seen": sensor.last_seen,
        }, interval=5)

    def ingest_broadcast(self, source, identity, position, observer_fix,
                         *, label="", signal_dbm=None, observed_at=None,
                         stable=True, receiver=None, session=None, seq=None,
                         channel=None, transport=None):
        pos = _lat_lon(position)
        if not self.active or pos is None or not isinstance(identity, str) or not identity:
            return
        identity = identity.upper()[:40]
        allowlist = self.settings.get(
            "pig_agency_icao" if source == "adsb" else "pig_agency_rid", ())
        configured = stable and identity in allowlist
        key = f"broadcast:{source}:{identity}"
        self._emit(key, {
            "source": source, "category": "user_tagged" if configured else "broadcast",
            "tier": "configured" if configured else "neutral",
            "identity": identity, "stable_identity": stable,
            "label": label or identity, "signal_dbm": signal_dbm,
            "receiver": receiver, "session": session, "seq": seq,
            "channel": channel, "transport": transport,
            "observer_fix": observer_fix,
            "broadcast_position": {"latitude": pos[0], "longitude": pos[1]},
            "observed_at": observed_at or time.time(),
        }, interval=1)
        if stable:
            trail = self.broadcast_tracks.setdefault(key, deque(maxlen=32))
            if not trail or trail[-1][:2] != pos:
                trail.append((pos[0], pos[1], time.monotonic()))
            self.broadcast_tracks.move_to_end(key)
            while len(self.broadcast_tracks) > MAX_IDENTITIES:
                self.broadcast_tracks.popitem(last=False)

    def visible(self, now=None):
        now = time.monotonic() if now is None else now
        for key, item in list(self.live.items()):
            ttl = 30 if item["source"] == "remote_id" else 60
            if now-item["seen"] <= ttl:
                yield key, item

    def draw_map(self, px, proj):
        if not self.active:
            return
        now = time.monotonic()
        for key, item in self.visible(now):
            if item["category"] in ("flock", "axon"):
                continue  # Their existing heard-here markers stay visible.
            if item["category"] == "rf_coverage":
                continue  # A noise measurement is coverage, not a device lead.
            broadcast = _lat_lon(item.get("broadcast_position"))
            if broadcast is not None:
                x,y = proj.geo_to_screen(*broadcast)
                if not proj.screen_visible(x,y):
                    continue
                color = 10 if item["tier"] == "configured" else 12
                trail = self.broadcast_tracks.get(key, ())
                recent = [(a,b) for a,b,t in trail if now-t <= 60]
                for a,b in zip(recent, recent[1:]):
                    x1,y1 = proj.geo_to_screen(*a)
                    x2,y2 = proj.geo_to_screen(*b)
                    px.line(x1,y1,x2,y2,color)
                px.line(x-4,y,x,y-4,color)
                px.line(x,y-4,x+4,y,color)
                px.line(x+4,y,x,y+4,color)
                px.line(x,y+4,x-4,y,color)
            else:
                pos = _lat_lon(item.get("observer_fix"))
                if pos is None:
                    continue
                x,y = proj.geo_to_screen(*pos)
                if not proj.screen_visible(x,y):
                    continue
                color = 9 if item["tier"] == "product" else 13
                px.circb(x,y,4,color)
                px.pset(x,y,color)
            if proj.zoom >= 5:
                px.text(x+6,y-4,str(item["label"])[:24],color)
        if self.settings.get("pig_estimates", True):
            for area in list(self.estimates.values()):
                if now-area["seen"] > 300:
                    continue
                lat,lon = area["latitude"],area["longitude"]
                x,y = proj.geo_to_screen(lat,lon)
                edge,_ = proj.geo_to_screen(
                    lat,lon+area["radius_m"]/(EARTH_METERS_PER_DEGREE*
                                             max(0.1,math.cos(math.radians(lat)))))
                radius = int(max(4,min(80,abs(edge-x))))
                if proj.screen_visible(x,y):
                    px.circb(x,y,radius,6)
                    px.text(x+5,y+5,f"RF area ~{area['radius_m']}m",6)
