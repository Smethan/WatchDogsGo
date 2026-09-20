"""Incremental, overscanned map layer for thick wardrive trails.

The expensive projection pass is independent of Pyxel.  A native ``Image`` is
created only when :meth:`TrailLayer.draw` publishes a completed build, keeping
graphics work on the game thread and reducing a stable frame to one blit.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import time


HEAT_COLORS = (
    (2, 12),
    (5, 3),
    (11, 11),
    (23, 10),
    (47, 9),
    (float("inf"), 8),
)


def _wrapped_delta(lon: float, center_lon: float) -> float:
    delta = lon - center_lon
    if delta > 180.0:
        delta -= 360.0
    elif delta < -180.0:
        delta += 360.0
    return delta


def _heat_color(density) -> int:
    try:
        value = max(0.0, float(density))
    except (TypeError, ValueError):
        value = 0.0
    for upper, color in HEAT_COLORS:
        if value <= upper:
            return color
    return 8


def heat_color(density) -> int:
    """Public color-band helper shared by the compact radar trail."""
    return _heat_color(density)


@dataclass(frozen=True)
class _TrailRequest:
    points: object
    limit: int
    token: tuple
    style: str
    zoom: int
    center_lat: float
    center_lon: float
    lon_span: float

    @property
    def key(self):
        return (self.token, self.zoom, self.center_lat, self.center_lon)


class _TrailBuild:
    """Resumable projection and decimation with no Pyxel dependency."""

    def __init__(self, request: _TrailRequest, width: int, map_top: int,
                 map_height: int, overscan: int, max_segments: int):
        self.request = request
        # WardriveTrail uses a deque, whose indexed access becomes O(n).  A
        # rebuild-local snapshot makes the incremental pass linear and also
        # prevents an append between frames from invalidating an iterator.
        self.points = tuple(request.points)[:request.limit]
        self.limit = len(self.points)
        self.width = width
        self.map_top = map_top
        self.map_height = map_height
        self.overscan = overscan
        self.max_segments = max_segments
        self.index = 0
        self.previous = None
        self.previous_xy = None
        self.tail = None
        self.tail_xy = None
        self.edges_since_emit = 0
        # Sampling a point every ``stride`` raw edges keeps long, smooth
        # routes continuous while bounding the finished native image work.
        self.stride = max(1, math.ceil(max(0, self.limit - 1)
                                      / max_segments))
        # Keep the newest commands if a route contains many tiny segments.
        self.commands = deque(maxlen=max_segments)
        self.done = False

    def _project(self, point):
        try:
            lat = float(point["lat"])
            lon = float(point["lon"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (-90.0 <= lat <= 90.0):
            return None
        scale = self.width / self.request.lon_span
        return (
            int(self.width / 2
                + _wrapped_delta(lon, self.request.center_lon) * scale),
            int(self.map_top + self.map_height / 2
                + (self.request.center_lat - lat) * scale),
        )

    @staticmethod
    def _segment(point):
        try:
            return point.get("segment")
        except AttributeError:
            return None

    def _visible(self, a, b):
        left, right = -self.overscan, self.width + self.overscan
        top = self.map_top - self.overscan
        bottom = self.map_top + self.map_height + self.overscan
        return (max(a[0], b[0]) >= left and min(a[0], b[0]) < right
                and max(a[1], b[1]) >= top and min(a[1], b[1]) < bottom)

    def _emit(self, point, xy):
        if self.previous_xy is None or xy == self.previous_xy:
            return
        if self._visible(self.previous_xy, xy):
            color = (3 if self.request.style == "solid"
                     else _heat_color(point.get("density", 0)))
            self.commands.append((*self.previous_xy, *xy, color))
        self.previous = point
        self.previous_xy = xy
        self.tail = point
        self.tail_xy = xy
        self.edges_since_emit = 0

    def _flush_tail(self):
        if self.tail is not None and self.tail_xy is not None:
            self._emit(self.tail, self.tail_xy)

    def _consume(self, point):
        xy = self._project(point)
        if xy is None:
            # Invalid coordinates are a hard break.  A later valid point must
            # never be connected across the missing sample.
            self._flush_tail()
            self.previous = self.previous_xy = None
            self.tail = self.tail_xy = None
            self.edges_since_emit = 0
            return
        if self.previous is None:
            self.previous, self.previous_xy = point, xy
            self.tail, self.tail_xy = point, xy
            return
        if self._segment(self.previous) != self._segment(point):
            self._flush_tail()
            self.previous, self.previous_xy = point, xy
            self.tail, self.tail_xy = point, xy
            self.edges_since_emit = 0
            return
        self.edges_since_emit += 1
        self.tail, self.tail_xy = point, xy
        if self.edges_since_emit >= self.stride:
            self._emit(point, xy)

    def _finish_tail(self):
        # When decimation leaves a partial interval, connect it to the final
        # point in that same segment so the current end of the route is shown.
        self._flush_tail()

    def step(self, max_ms: float, max_records: int | None = None) -> bool:
        deadline = time.perf_counter() + max_ms / 1000.0
        processed = 0
        while self.index < self.limit:
            end = min(self.limit, self.index + 128)
            while self.index < end:
                point = self.points[self.index]
                self._consume(point)
                self.index += 1
                processed += 1
                if max_records is not None and processed >= max_records:
                    return False
            if time.perf_counter() >= deadline:
                return False
        self._finish_tail()
        self.done = True
        return True


class TrailLayer:
    """Cached three-pixel map trail with solid and density-heat styles."""

    def __init__(self, width: int, map_top: int, map_height: int,
                 overscan: int = 128, *, rebuild_shift: int | None = None,
                 transparent: int = 15, max_segments: int = 2048):
        if max_segments <= 0:
            raise ValueError("max_segments must be positive")
        self.width = width
        self.map_top = map_top
        self.map_height = map_height
        self.overscan = overscan
        self.rebuild_shift = (max(1, overscan // 2) if rebuild_shift is None
                              else rebuild_shift)
        self.transparent = transparent
        self.max_segments = max_segments
        self.image = None
        self.anchor_lat = 0.0
        self.anchor_lon = 0.0
        self.zoom = -1
        self.data_token = None
        self.style = None
        self._job: _TrailBuild | None = None
        self._queued: _TrailRequest | None = None
        self._ready: tuple[_TrailRequest, list[tuple]] | None = None

    @staticmethod
    def _make_request(points, revision, style, proj):
        if style not in ("solid", "heat"):
            raise ValueError("trail style must be 'solid' or 'heat'")
        return _TrailRequest(
            points=points,
            limit=len(points),
            token=(id(points), len(points), revision, style),
            style=style,
            zoom=proj.zoom,
            center_lat=proj.center_lat,
            center_lon=proj.center_lon,
            lon_span=proj.lon_span,
        )

    def _start(self, request: _TrailRequest):
        self._job = _TrailBuild(
            request, self.width, self.map_top, self.map_height,
            self.overscan, self.max_segments)

    def offset(self, proj):
        if proj.zoom != self.zoom:
            return None
        scale = self.width / proj.lon_span
        return (
            round(_wrapped_delta(self.anchor_lon, proj.center_lon) * scale),
            round((proj.center_lat - self.anchor_lat) * scale),
        )

    def _request_offset(self, anchor: _TrailRequest,
                        current: _TrailRequest):
        """Translate an anchored build to a newer camera request."""
        if anchor.zoom != current.zoom:
            return None
        scale = self.width / current.lon_span
        return (
            round(_wrapped_delta(
                anchor.center_lon, current.center_lon) * scale),
            round((current.center_lat - anchor.center_lat) * scale),
        )

    def _camera_compatible(self, anchor: _TrailRequest,
                           current: _TrailRequest) -> bool:
        """Whether a build can safely publish and translate to *current*."""
        if anchor.token != current.token:
            return False
        offset = self._request_offset(anchor, current)
        return (offset is not None
                and abs(offset[0]) <= self.rebuild_shift
                and abs(offset[1]) <= self.rebuild_shift)

    def invalidate(self):
        """Discard published, completed, queued, and in-flight work."""
        self.image = None
        self.data_token = None
        self.style = None
        self._job = self._queued = self._ready = None

    def request(self, points, revision, style, proj):
        request = self._make_request(points, revision, style, proj)
        has_image = self.image is not None
        offset = self.offset(proj) if has_image else None
        current = (has_image and self.data_token == request.token
                   and offset is not None
                   and abs(offset[0]) <= self.rebuild_shift
                   and abs(offset[1]) <= self.rebuild_shift)
        if current:
            return

        # A wrong-scale or wrong-style trail should disappear immediately.
        if has_image and (proj.zoom != self.zoom or request.style != self.style):
            self.image = None
            self.data_token = None

        if self._job is not None:
            # Normal GPS/camera easing changes the exact center every frame.
            # The overscanned image can translate across those small shifts,
            # so do not starve a long build by replacing it continuously.
            if not self._camera_compatible(self._job.request, request):
                self._queued = request
            return
        if self._ready is not None:
            if self._camera_compatible(self._ready[0], request):
                return
            # A newer request supersedes an unpublished build.  Never expose
            # the completed stale image for a frame.
            self._ready = None
            self._queued = None
            self._start(request)
            return
        self._queued = None
        self._start(request)

    def step(self, max_ms: float = 1.0, max_records: int = 256):
        if self._job is None:
            return
        if not self._job.step(max_ms, max_records):
            return
        request = self._job.request
        commands = list(self._job.commands)
        self._job = None
        queued, self._queued = self._queued, None
        if queued is not None and queued.key != request.key:
            # Coalesce camera/data churn to the most recent request and do not
            # publish the build that just became obsolete.
            self._start(queued)
        else:
            self._ready = request, commands

    @staticmethod
    def _offsets(x1, y1, x2, y2):
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if not length:
            return ()
        ox, oy = round(-dy / length), round(dx / length)
        if ox == 0 and oy == 0:
            ox, oy = (0, 1) if abs(dx) >= abs(dy) else (1, 0)
        return ((-ox, -oy), (0, 0), (ox, oy))

    def _render(self, px, commands):
        image_ctor = getattr(px, "Image", None)
        if not callable(image_ctor):
            return None
        image = image_ctor(
            self.width + 2 * self.overscan,
            self.map_height + 2 * self.overscan)
        image.cls(self.transparent)
        for x1, y1, x2, y2, color in commands:
            x1 += self.overscan
            x2 += self.overscan
            y1 += self.overscan - self.map_top
            y2 += self.overscan - self.map_top
            for ox, oy in self._offsets(x1, y1, x2, y2):
                image.line(x1 + ox, y1 + oy, x2 + ox, y2 + oy, color)
        return image

    def _publish(self, px):
        if self._ready is None:
            return
        request, commands = self._ready
        self._ready = None
        self.image = self._render(px, commands)
        self.anchor_lat = request.center_lat
        self.anchor_lon = request.center_lon
        self.zoom = request.zoom
        self.data_token = request.token
        self.style = request.style

    def draw(self, px, proj):
        self._publish(px)
        offset = self.offset(proj)
        if self.image is None or offset is None:
            return
        px.clip(0, self.map_top, self.width, self.map_height)
        px.blt(
            -self.overscan + offset[0],
            self.map_top - self.overscan + offset[1],
            self.image, 0, 0, self.image.width, self.image.height,
            self.transparent,
        )
        px.clip()
