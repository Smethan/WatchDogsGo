"""Small geographic index for keeping map work proportional to the viewport."""

from __future__ import annotations

import math


class GeoPointIndex:
    """Bucket GPS-tagged records while preserving their original order."""

    def __init__(self, bucket_degrees: float = 0.1):
        self.bucket_degrees = bucket_degrees
        self._points: list = []
        self._buckets: dict[tuple[int, int], list[tuple[int, object]]] = {}
        self.token: tuple[int, int] | None = None

    @staticmethod
    def _normal_lon(lon: float) -> float:
        return (lon + 180.0) % 360.0 - 180.0

    def _bucket(self, value: float) -> int:
        return math.floor(value / self.bucket_degrees)

    @staticmethod
    def _coordinates(point) -> tuple[float, float]:
        return float(point["lat"]), float(point["lon"])

    def ensure(self, points: list) -> tuple[int, int]:
        """Rebuild only when the point list object or its length changes."""
        token = (id(points), len(points))
        if token == self.token:
            return token
        previous_length = self.token[1] if self.token and self.token[0] == id(points) else 0
        if not self.token or self.token[0] != id(points) or len(points) < previous_length:
            self._buckets = {}
            previous_length = 0
        self._points = points
        for index in range(previous_length, len(points)):
            point = points[index]
            try:
                lat, lon = self._coordinates(point)
                lon = self._normal_lon(lon)
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
            if not (-90.0 <= lat <= 90.0):
                continue
            key = (self._bucket(lat), self._bucket(lon))
            self._buckets.setdefault(key, []).append((index, point))
        self.token = token
        return token

    def query(self, center_lat: float, center_lon: float,
              lat_span: float, lon_span: float,
              x_margin: float = 0.04, y_margin: float = 0.06) -> list[dict]:
        """Return points within the viewport plus its marker visibility margin."""
        if not self._points:
            return []
        if lon_span >= 20.0:
            return self._points

        half_lat = lat_span * (0.5 + y_margin)
        half_lon = lon_span * (0.5 + x_margin)
        south = max(-90.0, center_lat - half_lat)
        north = min(90.0, center_lat + half_lat)
        center_lon = self._normal_lon(center_lon)
        west = center_lon - half_lon
        east = center_lon + half_lon

        if west < -180.0:
            lon_ranges = ((west + 360.0, 180.0), (-180.0, east))
        elif east >= 180.0:
            lon_ranges = ((west, 180.0), (-180.0, east - 360.0))
        else:
            lon_ranges = ((west, east),)

        found: list[tuple[int, object]] = []
        lat_start, lat_end = self._bucket(south), self._bucket(north)
        for lon_start, lon_end in lon_ranges:
            bx_start = self._bucket(lon_start)
            bx_end = self._bucket(min(lon_end, 180.0 - 1e-9))
            for by in range(lat_start, lat_end + 1):
                for bx in range(bx_start, bx_end + 1):
                    for index, point in self._buckets.get((by, bx), ()):
                        lat, lon = self._coordinates(point)
                        lon = self._normal_lon(lon)
                        if south <= lat <= north and any(
                                lo <= lon <= hi for lo, hi in lon_ranges):
                            found.append((index, point))
        found.sort(key=lambda item: item[0])
        return [point for _index, point in found]


class GeoObjectIndex(GeoPointIndex):
    """Geographic index for live objects exposing ``lat`` and ``lon``."""

    @staticmethod
    def _coordinates(point) -> tuple[float, float]:
        return float(point.lat), float(point.lon)
