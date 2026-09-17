"""Incremental, overscanned native-image layers for map observations."""

from __future__ import annotations

import time
from dataclasses import dataclass


def _wrapped_delta(lon: float, center_lon: float) -> float:
    delta = lon - center_lon
    if delta > 180.0:
        delta -= 360.0
    elif delta < -180.0:
        delta += 360.0
    return delta


@dataclass(frozen=True)
class _BuildRequest:
    points: list
    limit: int
    token: tuple[int, int]
    zoom: int
    center_lat: float
    center_lon: float
    lon_span: float
    lat_span: float

    @property
    def key(self):
        return (self.token, self.zoom, self.center_lat, self.center_lon)


class _ClusterBuild:
    """A resumable screen-cell clustering pass with no Pyxel dependencies."""

    def __init__(self, request: _BuildRequest, width: int, map_top: int,
                 map_height: int, overscan: int, cluster_px: int):
        self.request = request
        self.width = width
        self.map_top = map_top
        self.map_height = map_height
        self.overscan = overscan
        self.cluster_px = cluster_px
        self.index = 0
        self.cells: dict[tuple[int, int], dict] = {}
        self.done = False

    def _consume(self, point):
        req = self.request
        try:
            lat = float(point["lat"])
            lon = float(point["lon"])
        except (KeyError, TypeError, ValueError):
            return
        if not (-90.0 <= lat <= 90.0):
            return
        pixels_per_degree = self.width / req.lon_span
        sx = int(self.width / 2
                 + _wrapped_delta(lon, req.center_lon) * pixels_per_degree)
        sy = int(self.map_top + self.map_height / 2
                 + (req.center_lat - lat) * pixels_per_degree)
        if not (-self.overscan <= sx < self.width + self.overscan
                and self.map_top - self.overscan <= sy
                < self.map_top + self.map_height + self.overscan):
            return

        cell = (sx // self.cluster_px, sy // self.cluster_px)
        cluster = self.cells.get(cell)
        if cluster is None:
            cluster = self.cells[cell] = {
                "sum_x": 0, "sum_y": 0, "count": 0, "points": [],
                "wifi": 0, "bt": 0, "cell": 0,
            }
        cluster["sum_x"] += sx
        cluster["sum_y"] += sy
        cluster["count"] += 1
        cluster["points"].append(point)
        point_type = point.get("type")
        if point_type == "bt":
            cluster["bt"] += 1
        elif point_type == "cell":
            cluster["cell"] += 1
        else:
            cluster["wifi"] += 1

    def step(self, max_ms: float, max_records: int | None = None) -> bool:
        deadline = time.perf_counter() + max_ms / 1000.0
        processed = 0
        req = self.request
        while self.index < req.limit:
            end = min(req.limit, self.index + 128)
            while self.index < end:
                self._consume(req.points[self.index])
                self.index += 1
                processed += 1
                if max_records is not None and processed >= max_records:
                    return False
            if time.perf_counter() >= deadline:
                return False
        self.done = True
        return True

    def finish(self, wifi_color: int, bt_color: int, cell_color: int) -> list[dict]:
        req = self.request
        clusters = []
        for accumulator in self.cells.values():
            count = accumulator["count"]
            point = accumulator["points"][0]
            if count == 1:
                point_type = point.get("type")
                color = (cell_color if point_type == "cell" else
                         bt_color if point_type == "bt" else wifi_color)
                radius = 0
            else:
                wifi = accumulator["wifi"]
                bt = accumulator["bt"]
                cell = accumulator["cell"]
                color = (cell_color if cell >= max(wifi, bt) else
                         bt_color if bt > wifi else wifi_color)
                radius = min(5 + count // 3, 12)
            clusters.append({
                "x": accumulator["sum_x"] // count,
                "y": accumulator["sum_y"] // count,
                "points": accumulator["points"],
                "count": count,
                "color": color,
                "radius": radius,
                "_anchor_lat": req.center_lat,
                "_anchor_lon": req.center_lon,
                "_anchor_zoom": req.zoom,
            })
        return clusters


class HistoricalNodeLayer:
    """Cache historical dots and clusters in an overscanned Pyxel Image."""

    def __init__(self, width: int, map_top: int, map_height: int,
                 *, wifi_color: int, bt_color: int, cell_color: int,
                 overscan: int = 128, rebuild_shift: int = 64,
                 cluster_px: int = 30, transparent: int = 15):
        self.width = width
        self.map_top = map_top
        self.map_height = map_height
        self.wifi_color = wifi_color
        self.bt_color = bt_color
        self.cell_color = cell_color
        self.overscan = overscan
        self.rebuild_shift = rebuild_shift
        self.cluster_px = cluster_px
        self.transparent = transparent
        self.image = None
        self.clusters: list[dict] = []
        self.anchor_lat = 0.0
        self.anchor_lon = 0.0
        self.zoom = -1
        self.data_token = None
        self.revision = 0
        self._job: _ClusterBuild | None = None
        self._queued: _BuildRequest | None = None
        self._ready: tuple[_BuildRequest, list[dict]] | None = None

    @staticmethod
    def _request(points: list, proj) -> _BuildRequest:
        return _BuildRequest(
            points=points, limit=len(points), token=(id(points), len(points)),
            zoom=proj.zoom, center_lat=proj.center_lat,
            center_lon=proj.center_lon, lon_span=proj.lon_span,
            lat_span=proj.lat_span)

    def _start(self, request: _BuildRequest):
        self._job = _ClusterBuild(
            request, self.width, self.map_top, self.map_height,
            self.overscan, self.cluster_px)

    def offset(self, proj, *, anchor_lat=None, anchor_lon=None,
               anchor_zoom=None) -> tuple[int, int] | None:
        zoom = self.zoom if anchor_zoom is None else anchor_zoom
        if proj.zoom != zoom:
            return None
        lat = self.anchor_lat if anchor_lat is None else anchor_lat
        lon = self.anchor_lon if anchor_lon is None else anchor_lon
        pixels_per_degree = self.width / proj.lon_span
        dx = _wrapped_delta(lon, proj.center_lon) * pixels_per_degree
        dy = (proj.center_lat - lat) * pixels_per_degree
        return round(dx), round(dy)

    def request(self, points: list, proj):
        request = self._request(points, proj)
        zoom_changed = self.zoom >= 0 and proj.zoom != self.zoom
        offset = self.offset(proj) if self.image is not None else None
        current = (self.image is not None and not zoom_changed
                   and self.data_token == request.token and offset is not None
                   and abs(offset[0]) <= self.rebuild_shift
                   and abs(offset[1]) <= self.rebuild_shift)
        if current:
            return

        if zoom_changed:
            # A layer at the wrong scale is more distracting than a short,
            # bounded rebuild. Cancel obsolete model work immediately.
            self.image = None
            self.clusters = []
            self._ready = None
            self._queued = None
            self._start(request)
            return

        if self._job is not None:
            if self._job.request.key != request.key:
                self._queued = request
            return
        if self._ready is not None and self._ready[0].key == request.key:
            return
        self._start(request)

    def step(self, max_ms: float = 2.0, max_records: int | None = None):
        if self._job is None:
            return
        if not self._job.step(max_ms, max_records):
            return
        request = self._job.request
        clusters = self._job.finish(
            self.wifi_color, self.bt_color, self.cell_color)
        self._ready = request, clusters
        self._job = None
        queued, self._queued = self._queued, None
        if queued is not None and queued.key != request.key:
            self._start(queued)

    def _render(self, px, request: _BuildRequest, clusters: list[dict]):
        image_ctor = getattr(px, "Image", None)
        if not callable(image_ctor):
            return None
        image = image_ctor(
            self.width + 2 * self.overscan,
            self.map_height + 2 * self.overscan)
        image.cls(self.transparent)
        labels_left = 80
        for cluster in clusters:
            x = cluster["x"] + self.overscan
            y = cluster["y"] - self.map_top + self.overscan
            color = cluster["color"]
            if cluster["count"] == 1:
                point = cluster["points"][0]
                if request.zoom >= 8:
                    image.circ(x, y, 2, color)
                    in_view = (0 <= cluster["x"] < self.width
                               and self.map_top <= cluster["y"]
                               < self.map_top + self.map_height)
                    if request.zoom >= 10 and labels_left > 0 and in_view:
                        image.text(x + 4, y - 2,
                                   point.get("label", "")[:16], color)
                        labels_left -= 1
                elif request.zoom >= 3:
                    image.rect(x, y, 2, 2, color)
                else:
                    image.pset(x, y, color)
            else:
                radius = cluster["radius"]
                image.circ(x, y, radius, color)
                image.circb(x, y, radius, 0)
                text = str(cluster["count"])
                image.text(x - (len(text) * 5) // 2, y - 3, text, 0)
        return image

    def _publish(self, px):
        if self._ready is None:
            return
        request, clusters = self._ready
        self._ready = None
        self.image = self._render(px, request, clusters)
        self.clusters = clusters
        self.anchor_lat = request.center_lat
        self.anchor_lon = request.center_lon
        self.zoom = request.zoom
        self.data_token = request.token
        self.revision += 1

    def screen_position(self, cluster: dict, proj) -> tuple[int, int] | None:
        offset = self.offset(
            proj, anchor_lat=cluster.get("_anchor_lat", self.anchor_lat),
            anchor_lon=cluster.get("_anchor_lon", self.anchor_lon),
            anchor_zoom=cluster.get("_anchor_zoom", self.zoom))
        if offset is None:
            return None
        return cluster["x"] + offset[0], cluster["y"] + offset[1]

    def visible_clusters(self, proj) -> list[tuple[int, dict, int, int]]:
        visible = []
        for index, cluster in enumerate(self.clusters):
            position = self.screen_position(cluster, proj)
            if position is None:
                continue
            x, y = position
            if 0 <= x < self.width and self.map_top <= y < self.map_top + self.map_height:
                visible.append((index, cluster, x, y))
        return visible

    def draw(self, px, proj, selected_index: int = -1):
        self._publish(px)
        offset = self.offset(proj)
        if self.image is not None and offset is not None:
            px.clip(0, self.map_top, self.width, self.map_height)
            px.blt(-self.overscan + offset[0],
                   self.map_top - self.overscan + offset[1],
                   self.image, 0, 0, self.image.width, self.image.height,
                   self.transparent)
            px.clip()

        if 0 <= selected_index < len(self.clusters):
            cluster = self.clusters[selected_index]
            position = self.screen_position(cluster, proj)
            if position is not None:
                x, y = position
                radius = 5 if cluster["count"] == 1 else cluster["radius"] + 3
                px.circb(x, y, radius, 7)
                if px.frame_count % 20 < 14:
                    pulse = 7 if cluster["count"] == 1 else cluster["radius"] + 5
                    px.circb(x, y, pulse, 3)


@dataclass(frozen=True)
class _LiveRequest:
    wifi: list
    ble: list
    token: tuple
    notable: frozenset[str]
    zoom: int
    center_lat: float
    center_lon: float
    lon_span: float

    @property
    def key(self):
        return (self.token, self.zoom, self.center_lat, self.center_lon)


class _LiveBuild:
    """Incrementally project live devices into draw commands."""

    def __init__(self, request: _LiveRequest, width: int, map_top: int,
                 map_height: int, overscan: int):
        self.request = request
        self.width = width
        self.map_top = map_top
        self.map_height = map_height
        self.overscan = overscan
        self.kind = "wifi"
        self.index = 0
        self.commands = []

    def _objects(self):
        return self.request.wifi if self.kind == "wifi" else self.request.ble

    def _consume(self, obj):
        try:
            lat, lon = float(obj.lat), float(obj.lon)
        except (AttributeError, TypeError, ValueError):
            return
        if (lat == 0.0 and lon == 0.0) or not (-90.0 <= lat <= 90.0):
            return
        identity = ("wifi:" + obj.bssid.upper() if self.kind == "wifi"
                    else "ble:" + obj.mac.upper())
        if identity in self.request.notable:
            return
        scale = self.width / self.request.lon_span
        x = int(self.width / 2
                + _wrapped_delta(lon, self.request.center_lon) * scale)
        y = int(self.map_top + self.map_height / 2
                + (self.request.center_lat - lat) * scale)
        if not (-self.overscan <= x < self.width + self.overscan
                and self.map_top - self.overscan <= y
                < self.map_top + self.map_height + self.overscan):
            return
        phase = ((sum(ord(char) for char in obj.bssid) & 1)
                 if self.kind == "wifi"
                 else int(getattr(obj, "blink_phase", 0) * 1000) & 1)
        self.commands.append((
            self.kind, x, y, int(obj.color), bool(obj.hacked), phase))

    def step(self, max_ms: float, max_records: int | None = None) -> bool:
        deadline = time.perf_counter() + max_ms / 1000.0
        processed = 0
        while True:
            objects = self._objects()
            while self.index < len(objects):
                end = min(len(objects), self.index + 128)
                while self.index < end:
                    self._consume(objects[self.index])
                    self.index += 1
                    processed += 1
                    if max_records is not None and processed >= max_records:
                        return False
                if time.perf_counter() >= deadline:
                    return False
            if self.kind == "wifi":
                self.kind = "ble"
                self.index = 0
                continue
            return True


class LiveNodeLayer:
    """Two cached blink frames for mature Wi-Fi and BLE map nodes."""

    def __init__(self, width: int, map_top: int, map_height: int,
                 *, overscan: int = 128, rebuild_shift: int = 64,
                 transparent: int = 15):
        self.width = width
        self.map_top = map_top
        self.map_height = map_height
        self.overscan = overscan
        self.rebuild_shift = rebuild_shift
        self.transparent = transparent
        self.images = (None, None)
        self.anchor_lat = 0.0
        self.anchor_lon = 0.0
        self.zoom = -1
        self.data_token = None
        self._job: _LiveBuild | None = None
        self._queued: _LiveRequest | None = None
        self._ready: tuple[_LiveRequest, list] | None = None
        self.revision = 0

    @staticmethod
    def _request(wifi, ble, revision, notable, proj):
        token = (id(wifi), len(wifi), id(ble), len(ble), revision,
                 len(notable), hash(notable))
        return _LiveRequest(
            wifi=wifi, ble=ble, token=token, notable=notable,
            zoom=proj.zoom, center_lat=proj.center_lat,
            center_lon=proj.center_lon, lon_span=proj.lon_span)

    def offset(self, proj):
        if proj.zoom != self.zoom:
            return None
        scale = self.width / proj.lon_span
        return (round(_wrapped_delta(self.anchor_lon, proj.center_lon) * scale),
                round((proj.center_lat - self.anchor_lat) * scale))

    def request(self, wifi, ble, revision, notable, proj):
        request = self._request(wifi, ble, revision, notable, proj)
        has_image = self.images[0] is not None
        offset = self.offset(proj) if has_image else None
        current = (has_image and self.data_token == request.token
                   and offset is not None
                   and abs(offset[0]) <= self.rebuild_shift
                   and abs(offset[1]) <= self.rebuild_shift)
        if current:
            return
        if has_image and proj.zoom != self.zoom:
            self.images = (None, None)
            self._ready = self._queued = None
            self._job = _LiveBuild(
                request, self.width, self.map_top, self.map_height,
                self.overscan)
            return
        if self._job is not None:
            if self._job.request.key != request.key:
                self._queued = request
            return
        if self._ready is not None and self._ready[0].key == request.key:
            return
        self._job = _LiveBuild(
            request, self.width, self.map_top, self.map_height, self.overscan)

    def step(self, max_ms: float = 2.0, max_records: int | None = None):
        if self._job is None or not self._job.step(max_ms, max_records):
            return
        request, commands = self._job.request, self._job.commands
        self._ready = request, commands
        self._job = None
        queued, self._queued = self._queued, None
        if queued is not None and queued.key != request.key:
            self._job = _LiveBuild(
                queued, self.width, self.map_top, self.map_height,
                self.overscan)

    def _render_phase(self, px, request, commands, blink_phase):
        image = px.Image(
            self.width + 2 * self.overscan,
            self.map_height + 2 * self.overscan)
        image.cls(self.transparent)
        for kind, sx, sy, color, hacked, phase in commands:
            x = sx + self.overscan
            y = sy - self.map_top + self.overscan
            if kind == "wifi":
                if hacked:
                    image.line(x - 2, y, x, y - 3, 11)
                    image.line(x + 2, y, x, y - 3, 11)
                    image.line(x - 2, y, x, y + 2, 11)
                    image.line(x + 2, y, x, y + 2, 11)
                else:
                    draw_color = color if phase == blink_phase else 2
                    image.pset(x, y, draw_color)
                    if request.zoom >= 5:
                        image.circb(x, y, 3, draw_color)
            elif hacked:
                image.rect(x - 2, y - 2, 5, 5, 3)
                image.pset(x, y, 11)
            else:
                draw_color = color if phase == blink_phase else 2
                image.rect(x - 1, y - 1, 3, 3, draw_color)
        return image

    def _publish(self, px):
        if self._ready is None:
            return
        request, commands = self._ready
        self._ready = None
        self.images = (
            self._render_phase(px, request, commands, 0),
            self._render_phase(px, request, commands, 1))
        self.anchor_lat = request.center_lat
        self.anchor_lon = request.center_lon
        self.zoom = request.zoom
        self.data_token = request.token
        self.revision += 1

    def draw(self, px, proj):
        self._publish(px)
        offset = self.offset(proj)
        image = self.images[(px.frame_count // 10) & 1]
        if image is None or offset is None:
            return
        px.clip(0, self.map_top, self.width, self.map_height)
        px.blt(-self.overscan + offset[0],
               self.map_top - self.overscan + offset[1],
               image, 0, 0, image.width, image.height, self.transparent)
        px.clip()


@dataclass(frozen=True)
class _RadarRequest:
    wifi: list
    ble: list
    loot: list
    token: tuple
    notable: frozenset[str]
    center_lat: float
    center_lon: float
    scale: float

    @property
    def key(self):
        return (self.token, self.center_lat, self.center_lon, self.scale)


class _RadarBuild:
    def __init__(self, request: _RadarRequest, radius: int, overscan: int,
                 colors: dict[str, int]):
        self.request = request
        self.radius = radius
        self.overscan = overscan
        self.colors = colors
        self.kind = "wifi"
        self.index = 0
        self.loot_step = max(1, len(request.loot) // 100)
        self.pixels = {}

    def _objects(self):
        if self.kind == "wifi":
            return self.request.wifi
        if self.kind == "ble":
            return self.request.ble
        return self.request.loot

    def _consume(self, obj):
        if self.kind == "loot":
            try:
                lat, lon = float(obj["lat"]), float(obj["lon"])
            except (KeyError, TypeError, ValueError):
                return
            color = self.colors.get(obj.get("type", ""), self.colors["other"])
        else:
            try:
                lat, lon = float(obj.lat), float(obj.lon)
            except (AttributeError, TypeError, ValueError):
                return
            identity = ("wifi:" + obj.bssid.upper() if self.kind == "wifi"
                        else "ble:" + obj.mac.upper())
            if identity in self.request.notable:
                return
            if obj.hacked:
                color = self.colors["hacked"]
            elif self.kind == "wifi":
                color = self.colors["wifi_live"]
            else:
                color = int(obj.color)
        dx = round(_wrapped_delta(lon, self.request.center_lon)
                   * self.request.scale)
        dy = round((self.request.center_lat - lat) * self.request.scale)
        extent = self.radius + self.overscan
        if abs(dx) <= extent and abs(dy) <= extent:
            self.pixels[(dx, dy)] = color

    def step(self, max_ms: float, max_records: int | None = None):
        deadline = time.perf_counter() + max_ms / 1000.0
        processed = 0
        while True:
            objects = self._objects()
            increment = self.loot_step if self.kind == "loot" else 1
            while self.index < len(objects):
                end = min(len(objects), self.index + 128 * increment)
                while self.index < end:
                    self._consume(objects[self.index])
                    self.index += increment
                    processed += 1
                    if max_records is not None and processed >= max_records:
                        return False
                if time.perf_counter() >= deadline:
                    return False
            if self.kind == "wifi":
                self.kind = "ble"
            elif self.kind == "ble":
                self.kind = "loot"
            else:
                return True
            self.index = 0


class RadarNodeLayer:
    """Cached, deduplicated radar pixels that translate with GPS motion."""

    def __init__(self, radius: int, *, overscan: int = 8,
                 rebuild_shift: int = 4, transparent: int = 15,
                 colors: dict[str, int] | None = None):
        self.radius = radius
        self.overscan = overscan
        self.rebuild_shift = rebuild_shift
        self.transparent = transparent
        self.colors = colors or {
            "wifi": 11, "bt": 3, "cell": 10, "handshake": 8,
            "meshcore": 9, "other": 5, "wifi_live": 9, "hacked": 11,
        }
        self.image = None
        self.anchor_lat = self.anchor_lon = 0.0
        self.scale = 1.0
        self.data_token = None
        self._job: _RadarBuild | None = None
        self._queued: _RadarRequest | None = None
        self._ready: tuple[_RadarRequest, dict] | None = None

    @staticmethod
    def _request(wifi, ble, loot, revision, notable, center_lat, center_lon,
                 scale):
        token = (id(wifi), len(wifi), id(ble), len(ble),
                 id(loot), len(loot), revision, len(notable), hash(notable))
        return _RadarRequest(wifi, ble, loot, token, notable,
                             center_lat, center_lon, scale)

    def offset(self, center_lat, center_lon, scale):
        if scale != self.scale:
            return None
        return (round(_wrapped_delta(self.anchor_lon, center_lon) * scale),
                round((center_lat - self.anchor_lat) * scale))

    def request(self, wifi, ble, loot, revision, notable,
                center_lat, center_lon, scale):
        request = self._request(
            wifi, ble, loot, revision, notable, center_lat, center_lon, scale)
        offset = (self.offset(center_lat, center_lon, scale)
                  if self.image is not None else None)
        current = (self.image is not None and self.data_token == request.token
                   and offset is not None
                   and abs(offset[0]) <= self.rebuild_shift
                   and abs(offset[1]) <= self.rebuild_shift)
        if current:
            return
        if self.image is not None and scale != self.scale:
            self.image = None
            self._ready = self._queued = None
            self._job = _RadarBuild(
                request, self.radius, self.overscan, self.colors)
            return
        if self._job is not None:
            if self._job.request.key != request.key:
                self._queued = request
            return
        if self._ready is not None and self._ready[0].key == request.key:
            return
        self._job = _RadarBuild(
            request, self.radius, self.overscan, self.colors)

    def step(self, max_ms=1.0, max_records=None):
        if self._job is None or not self._job.step(max_ms, max_records):
            return
        request, pixels = self._job.request, self._job.pixels
        self._ready = request, pixels
        self._job = None
        queued, self._queued = self._queued, None
        if queued is not None and queued.key != request.key:
            self._job = _RadarBuild(
                queued, self.radius, self.overscan, self.colors)

    def _publish(self, px):
        if self._ready is None:
            return
        request, pixels = self._ready
        self._ready = None
        extent = self.radius + self.overscan
        image = px.Image(extent * 2 + 1, extent * 2 + 1)
        image.cls(self.transparent)
        for (dx, dy), color in pixels.items():
            image.pset(extent + dx, extent + dy, color)
        self.image = image
        self.anchor_lat = request.center_lat
        self.anchor_lon = request.center_lon
        self.scale = request.scale
        self.data_token = request.token

    def draw(self, px, center_x, center_y, center_lat, center_lon, scale):
        self._publish(px)
        offset = self.offset(center_lat, center_lon, scale)
        if self.image is None or offset is None:
            return
        extent = self.radius + self.overscan
        px.clip(center_x - self.radius, center_y - self.radius,
                self.radius * 2 + 1, self.radius * 2 + 1)
        px.blt(center_x - extent + offset[0],
               center_y - extent + offset[1],
               self.image, 0, 0, self.image.width, self.image.height,
               self.transparent)
        px.clip()
