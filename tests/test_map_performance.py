from types import SimpleNamespace as NS
from unittest.mock import Mock
from queue import Queue
import zlib

from watchdogs.app import MapProjection, WatchDogsGame
from watchdogs.map_index import GeoObjectIndex, GeoPointIndex
from watchdogs.wardrive_trail import WardriveTrail
from watchdogs.wardrive_ui import WardriveUI
from watchdogs.tile_manager import OSM_TILE_SIZE, TileRenderer


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


def test_packed_tile_nibbles_decode_in_order(tmp_path):
    tile_dir = tmp_path / "12"
    tile_dir.mkdir()
    raw = bytes([0x01, 0x2F]) * (OSM_TILE_SIZE * OSM_TILE_SIZE // 4)
    (tile_dir / "1_2.dat").write_bytes(zlib.compress(raw))
    pixels = TileRenderer(tmp_path)._get_tile_image(12, 1, 2)
    assert pixels[:8] == bytearray([0, 1, 2, 15, 0, 1, 2, 15])
    assert len(pixels) == OSM_TILE_SIZE * OSM_TILE_SIZE


def test_geo_index_handles_dateline_view():
    points = [
        {"lat": 0, "lon": 179.99},
        {"lat": 0, "lon": -179.99},
        {"lat": 0, "lon": 170},
    ]
    index = GeoPointIndex()
    index.ensure(points)
    assert index.query(0, 179.995, 0.1, 0.1) == points[:2]


def test_geo_object_index_filters_live_devices():
    devices = [NS(lat=40, lon=-90), NS(lat=50, lon=-80)]
    index = GeoObjectIndex()
    index.ensure(devices)
    assert index.query(40, -90, 0.1, 0.1) == devices[:1]


def test_geo_index_adds_appended_objects_incrementally():
    devices = [NS(lat=40, lon=-90), NS(lat=50, lon=-80)]
    index = GeoObjectIndex()
    coordinates = index._coordinates
    index._coordinates = Mock(side_effect=coordinates)
    index.ensure(devices)
    assert index._coordinates.call_count == 2
    devices.append(NS(lat=40.001, lon=-90.001))
    index.ensure(devices)
    assert index._coordinates.call_count == 3


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


def test_trail_projection_is_cached_and_offscreen_segments_are_skipped(
        monkeypatch):
    trail = WardriveTrail()
    trail.points.extend([
        {"lat": 40, "lon": -90, "segment": 1},
        {"lat": 40.0001, "lon": -90.0001, "segment": 1},
        {"lat": 50, "lon": -80, "segment": 2},
        {"lat": 50.0001, "lon": -80.0001, "segment": 2},
    ])
    trail.revision += 1
    projection = MapProjection()
    projection.zoom = 13
    projection.center_lat = 40
    projection.center_lon = -90
    original = projection.geo_to_screen
    projection.geo_to_screen = Mock(side_effect=original)

    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = {"trail": True}
    ui.trail = trail
    ui.history_trail = None
    ui.app = NS(proj=projection)
    ui._map_trail_key = None
    ui._map_trail_segments = []
    px = NS(clip=Mock(), line=Mock())
    monkeypatch.setitem(__import__("sys").modules, "pyxel", px)

    ui.draw_trail()
    ui.draw_trail()
    assert projection.geo_to_screen.call_count == 4
    assert px.line.call_count == 2

    trail.points.append({"lat": 40.0002, "lon": -90.0002, "segment": 2})
    trail.revision += 1
    ui.draw_trail()
    assert projection.geo_to_screen.call_count == 9


def test_trail_revision_changes_only_when_display_points_change(tmp_path):
    trail = WardriveTrail()
    trail.set_path(tmp_path / "trail.jsonl")
    initial = trail.revision
    fix = {"latitude": 40, "longitude": -90, "altitude": 0,
           "hdop": 1, "received_at": 1}
    trail.sample(fix, 1, True)
    assert trail.revision == initial + 1
    trail.sample(fix, 2, True)
    assert trail.revision == initial + 1


def test_periodic_loot_snapshot_is_applied_on_game_thread(monkeypatch):
    import watchdogs.app as appmod
    game = WatchDogsGame.__new__(WatchDogsGame)
    game.loot = NS(loot_totals={"wardriving_wifi": 7})
    game._app_dir = "/tmp"
    game.loot_points = []
    game._cluster_zoom = 13
    game._cracked_ssids = {}
    game._loot_totals = {}
    game._loot_refresh_result = Queue(maxsize=1)
    game._loot_refresh_active = False
    snapshot = {
        "points": [{"lat": 40, "lon": -90}],
        "passwords": {"ssid": "secret"},
        "totals": {"wifi": 7},
    }
    game._loot_refresh_result.put(snapshot)

    game._poll_loot_refresh()
    assert game.loot_points == snapshot["points"]
    assert game._cracked_ssids == snapshot["passwords"]
    assert game._loot_totals == snapshot["totals"]
    assert game._cluster_zoom == -1


def test_periodic_loot_refresh_worker_only_publishes_snapshot(monkeypatch):
    import watchdogs.app as appmod
    game = WatchDogsGame.__new__(WatchDogsGame)
    game.loot = object()
    game._loot_refresh_result = Queue(maxsize=1)
    game._loot_refresh_active = False
    snapshot = {"points": [], "passwords": {}, "totals": {}}
    game._collect_loot_snapshot = Mock(return_value=snapshot)

    class ImmediateThread:
        def __init__(self, target, **_kwargs): self.target = target
        def start(self): self.target()

    monkeypatch.setattr(appmod.threading, "Thread", ImmediateThread)
    game._start_loot_refresh()
    assert game._loot_refresh_active
    assert game.loot is not None
    assert game._loot_refresh_result.get_nowait() == snapshot
