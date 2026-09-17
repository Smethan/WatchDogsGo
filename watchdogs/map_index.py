"""Geographic indexes that keep map work proportional to the viewport."""

from __future__ import annotations

import math
from collections import OrderedDict


class GeoPointIndex:
    """Multi-resolution buckets for GPS-tagged records."""

    _MAX_QUERY_CELLS = 256
    _MAX_QUERY_CACHE = 8

    def __init__(self, bucket_degrees: float = 0.1):
        base = max(float(bucket_degrees), 1e-6)
        self.bucket_degrees = base
        self._levels = tuple(sorted({base / 10.0, base, base * 10.0,
                                     base * 100.0}))
        self._points: list = []
        self._coords: list[tuple[float, float] | None] = []
        self._buckets: dict[float, dict[tuple[int, int], list[int]]] = {
            level: {} for level in self._levels
        }
        self._query_cache: OrderedDict[tuple, list] = OrderedDict()
        self._last_query_candidates = 0
        self.token: tuple[int, int] | None = None

    @staticmethod
    def _normal_lon(lon: float) -> float:
        return (lon + 180.0) % 360.0 - 180.0

    @staticmethod
    def _bucket(value: float, level: float) -> int:
        return math.floor(value / level)

    @staticmethod
    def _coordinates(point) -> tuple[float, float]:
        return float(point["lat"]), float(point["lon"])

    def ensure(self, points: list) -> tuple[int, int]:
        """Index only appended objects; reset when the public list is replaced."""
        token = (id(points), len(points))
        if token == self.token:
            return token

        previous_length = (
            self.token[1]
            if self.token and self.token[0] == id(points)
            and len(points) >= self.token[1]
            else 0
        )
        if previous_length == 0:
            self._coords = []
            self._buckets = {level: {} for level in self._levels}

        self._points = points
        for index in range(previous_length, len(points)):
            point = points[index]
            coord = None
            try:
                lat, lon = self._coordinates(point)
                lon = self._normal_lon(lon)
                if -90.0 <= lat <= 90.0:
                    coord = (lat, lon)
            except (AttributeError, KeyError, TypeError, ValueError):
                pass
            self._coords.append(coord)
            if coord is None:
                continue
            lat, lon = coord
            for level in self._levels:
                key = (self._bucket(lat, level), self._bucket(lon, level))
                self._buckets[level].setdefault(key, []).append(index)

        self.token = token
        self._query_cache.clear()
        return token

    def _choose_level(self, south: float, north: float,
                      lon_ranges: tuple[tuple[float, float], ...]) -> float:
        for level in self._levels:
            lat_cells = self._bucket(north, level) - self._bucket(south, level) + 1
            lon_cells = sum(
                self._bucket(min(hi, 180.0 - 1e-9), level)
                - self._bucket(lo, level) + 1
                for lo, hi in lon_ranges
            )
            if lat_cells * lon_cells <= self._MAX_QUERY_CELLS:
                return level
        return self._levels[-1]

    def query(self, center_lat: float, center_lon: float,
              lat_span: float, lon_span: float,
              x_margin: float = 0.04, y_margin: float = 0.06) -> list:
        """Return points within the viewport plus its marker visibility margin."""
        if not self._points:
            return []

        cache_key = (self.token, center_lat, center_lon, lat_span, lon_span,
                     x_margin, y_margin)
        cached = self._query_cache.get(cache_key)
        if cached is not None:
            self._query_cache.move_to_end(cache_key)
            return cached

        if lon_span >= 20.0:
            result = self._points
            self._last_query_candidates = len(result)
        else:
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

            level = self._choose_level(south, north, lon_ranges)
            buckets = self._buckets[level]
            candidate_indices: list[int] = []
            lat_start = self._bucket(south, level)
            lat_end = self._bucket(north, level)
            for lon_start, lon_end in lon_ranges:
                bx_start = self._bucket(lon_start, level)
                bx_end = self._bucket(min(lon_end, 180.0 - 1e-9), level)
                for by in range(lat_start, lat_end + 1):
                    for bx in range(bx_start, bx_end + 1):
                        candidate_indices.extend(buckets.get((by, bx), ()))

            self._last_query_candidates = len(candidate_indices)
            found = []
            for index in candidate_indices:
                coord = self._coords[index]
                if coord is None:
                    continue
                lat, lon = coord
                if south <= lat <= north and any(
                        lo <= lon <= hi for lo, hi in lon_ranges):
                    found.append(index)
            found.sort()
            result = [self._points[index] for index in found]

        self._query_cache[cache_key] = result
        self._query_cache.move_to_end(cache_key)
        while len(self._query_cache) > self._MAX_QUERY_CACHE:
            self._query_cache.popitem(last=False)
        return result


class GeoObjectIndex(GeoPointIndex):
    """Geographic index for live objects exposing ``lat`` and ``lon``."""

    @staticmethod
    def _coordinates(point) -> tuple[float, float]:
        return float(point.lat), float(point.lon)
