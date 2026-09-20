"""Fresh host fixes and segmented local route storage. No map/pan coordinates."""
from collections import OrderedDict, deque
from dataclasses import asdict
import json
import math
import time

def distance(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 12742000 * math.asin(min(1, math.sqrt(h)))

class FixHistory:
    def __init__(self):
        self.fixes = deque(maxlen=600)
        self.stamp = None

    def update(self, fix, now):
        stamp = getattr(fix, "received_at", 0)
        if stamp and stamp != self.stamp:
            self.stamp = stamp
            valid = fix.valid and -90 <= fix.latitude <= 90 and -180 <= fix.longitude <= 180
            self.fixes.append((stamp, asdict(fix) if valid else None))
            while self.fixes and stamp-self.fixes[0][0] > 30:
                self.fixes.popleft()

    def at(self, when):
        if not self.fixes:
            return None
        stamp, fix = min(self.fixes, key=lambda item: abs(item[0]-when))
        return fix if abs(when-stamp) <= 3 else None

class WardriveTrail:
    def __init__(self):
        self.path = None
        self.points = deque(maxlen=4096)  # display window; full route remains on disk
        self.revision = 0
        self.segment = 0
        self.last = None
        self.last_stamp = None
        self.last_input = None
        self._recent_radios = OrderedDict()

    def note_observation(self, identity, now):
        """Record one Wi-Fi/BLE identity for route-density coloring."""
        key = str(identity).upper()
        if not key:
            return
        self._recent_radios[key] = float(now)
        self._recent_radios.move_to_end(key)
        self.recent_unique_count(now)

    def recent_unique_count(self, now, window=10.0):
        """Return unique radio identities heard in the rolling window."""
        cutoff = float(now) - float(window)
        while self._recent_radios:
            first = next(iter(self._recent_radios))
            if self._recent_radios[first] >= cutoff:
                break
            self._recent_radios.popitem(last=False)
        return len(self._recent_radios)

    def set_path(self, path, read_only=False):
        if path == self.path:
            return
        self.path = path
        self.points.clear()
        if path and path.exists():
            with path.open("rb" if read_only else "rb+") as f:
                while True:
                    offset = f.tell()
                    line = f.readline()
                    if not line:
                        break
                    try:
                        p = json.loads(line)
                        if all(k in p for k in ("lat", "lon", "segment", "time")):
                            self.points.append(p)
                    except (ValueError, TypeError):
                        if not line.endswith(b"\n"):
                            if not read_only:
                                f.seek(offset)
                                f.truncate()  # discard only incomplete final record before resuming
                            break
        self.segment = max((p["segment"] for p in self.points), default=0)
        self.revision += 1
        self.break_segment()

    def break_segment(self):
        if self.last is not None:
            self.last = None
        self.segment += 1

    def sample(self, fix, now, enabled, density=None):
        if not enabled or fix is None:
            if self.last is not None:
                self.break_segment()
            return
        stamp = fix["received_at"]
        if stamp == self.last_stamp:
            return
        if self.last_input is not None and now-self.last_input > 3:
            self.break_segment()
        self.last_input = now
        self.last_stamp = stamp
        if density is None:
            density = self.recent_unique_count(now)
        p = {"lat": fix["latitude"], "lon": fix["longitude"], "alt": fix["altitude"],
             "hdop": fix["hdop"], "time": time.time(), "segment": self.segment,
             "density": max(0, int(density))}
        if self.last:
            old, old_now = self.last
            dt = now-old_now
            meters = distance((old["lat"], old["lon"]), (p["lat"], p["lon"]))
            if meters > max(100, dt*70):
                self.break_segment()
                p["segment"] = self.segment
            elif dt < 1 or (meters < max(3, min(fix["hdop"]*5, 20)) and dt < 10):
                return
        self.last_stamp = stamp
        self.last = (p, now)
        self.points.append(p)
        self.revision += 1
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(p, separators=(",", ":")) + "\n")
