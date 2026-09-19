#!/usr/bin/env python3
"""Repeatable host benchmark for map indexing and cached node layers.

The fake image measures Python projection, clustering, raster dispatch and
cached-blit dispatch. It deliberately excludes GPU/display-driver time.
"""

import argparse
import json
import random
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

# Direct execution puts scripts/ on sys.path, so add the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from watchdogs.map_index import GeoPointIndex
from watchdogs.map_layers import HistoricalNodeLayer, LiveNodeLayer


WIDTH, MAP_TOP, MAP_HEIGHT = 640, 16, 218


class Image:
    def __init__(self, width, height):
        self.width, self.height = width, height

    def __getattr__(self, _name):
        return lambda *_args: None


class Pyxel:
    Image = Image
    frame_count = 0

    def __getattr__(self, _name):
        return lambda *_args: None


def elapsed(call):
    start = time.perf_counter()
    call()
    return round((time.perf_counter() - start) * 1000, 3)


def finish(layer):
    while layer._job is not None:
        layer.step(max_ms=100_000)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--points", type=int, default=50_000)
    args = parser.parse_args()
    rng = random.Random(42)
    proj = NS(zoom=13, center_lat=40.0, center_lon=-90.0,
              lon_span=0.12, lat_span=0.12 * MAP_HEIGHT / WIDTH)
    points = [
        {"lat": 40 + rng.uniform(-0.05, 0.05),
         "lon": -90 + rng.uniform(-0.08, 0.08),
         "type": ("wifi", "bt", "cell")[index % 3],
         "label": f"node-{index}"}
        for index in range(args.points)
    ]
    wifi = [
        NS(lat=point["lat"], lon=point["lon"],
           bssid=f"00:11:{index // 65536:02X}:{index // 256 % 256:02X}:{index % 256:02X}:01",
           color=10, hacked=False)
        for index, point in enumerate(points[::2])
    ]
    ble = [
        NS(lat=point["lat"], lon=point["lon"],
           mac=f"AA:BB:{index // 65536:02X}:{index // 256 % 256:02X}:{index % 256:02X}:02",
           color=9, hacked=False, blink_phase=index / 17)
        for index, point in enumerate(points[1::2])
    ]

    index = GeoPointIndex()
    index_build = elapsed(lambda: index.ensure(points))
    index_query = elapsed(lambda: index.query(
        proj.center_lat, proj.center_lon, proj.lat_span, proj.lon_span))

    history = HistoricalNodeLayer(
        WIDTH, MAP_TOP, MAP_HEIGHT,
        wifi_color=11, bt_color=3, cell_color=10)
    history.request(points, proj)
    history_build = elapsed(lambda: finish(history))
    px = Pyxel()
    history_publish = elapsed(lambda: history.draw(px, proj))
    history_cached = elapsed(lambda: history.draw(px, proj))

    live = LiveNodeLayer(WIDTH, MAP_TOP, MAP_HEIGHT)
    live.request(wifi, ble, 1, frozenset(), proj)
    live_build = elapsed(lambda: finish(live))
    live_publish = elapsed(lambda: live.draw(px, proj))
    live_cached = elapsed(lambda: live.draw(px, proj))

    print(json.dumps({
        "points": args.points,
        "visible_index_candidates": index._last_query_candidates,
        "milliseconds": {
            "adaptive_index_build": index_build,
            "adaptive_index_query": index_query,
            "historical_model_build": history_build,
            "historical_fake_raster_publish": history_publish,
            "historical_cached_dispatch": history_cached,
            "live_model_build": live_build,
            "live_two_frame_fake_raster_publish": live_publish,
            "live_cached_dispatch": live_cached,
        },
    }, indent=2))


if __name__ == "__main__":
    main()
