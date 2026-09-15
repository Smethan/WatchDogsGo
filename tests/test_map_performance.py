from types import SimpleNamespace as NS
from unittest.mock import Mock

from watchdogs.app import MapProjection, WatchDogsGame
from watchdogs.map_index import GeoPointIndex


def test_geo_index_filters_to_close_view_and_preserves_order():
    points = [
        {"lat": 40.0, "lon": -90.0, "label": "first"},
        {"lat": 45.0, "lon": -80.0, "label": "far"},
        {"lat": 40.001, "lon": -90.001, "label": "second"},
    ]
    index = GeoPointIndex()
    index.ensure(points)
    assert [p["label"] for p in index.query(40, -90, 0.02, 0.02)] == [
        "first", "second"]


def test_geo_index_handles_dateline_view():
    points = [
        {"lat": 0, "lon": 179.99},
        {"lat": 0, "lon": -179.99},
        {"lat": 0, "lon": 170},
    ]
    index = GeoPointIndex()
    index.ensure(points)
    assert index.query(0, 179.995, 0.1, 0.1) == points[:2]


def test_clusters_reuse_unchanged_result_and_rebuild_after_append(monkeypatch):
    import watchdogs.app as appmod
    monkeypatch.setattr(appmod, "pyxel", NS(frame_count=100))
    game = WatchDogsGame.__new__(WatchDogsGame)
    game.proj = MapProjection()
    game.proj.zoom = 13
    game.proj.center_lat = 40
    game.proj.center_lon = -90
    game.loot_points = [
        {"lat": 40, "lon": -90, "type": "wifi", "label": "near"},
        *({"lat": 45 + i / 10000, "lon": -80, "type": "wifi"}
          for i in range(1000)),
    ]
    game._clusters = []
    game._cluster_zoom = -1
    game._cluster_center = (0, 0)
    game._cluster_data_token = None
    game._loot_point_index = GeoPointIndex()
    original = game.proj.geo_to_screen
    game.proj.geo_to_screen = Mock(side_effect=original)

    game._update_clusters()
    assert game.proj.geo_to_screen.call_count == 1
    first_clusters = game._clusters
    game._update_clusters()
    assert game._clusters is first_clusters
    assert game.proj.geo_to_screen.call_count == 1

    game.loot_points.append(
        {"lat": 40.0001, "lon": -90.0001, "type": "bt", "label": "new"})
    game._update_clusters()
    assert game.proj.geo_to_screen.call_count == 3
    assert game._clusters is not first_clusters
