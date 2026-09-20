from types import SimpleNamespace as NS
from unittest.mock import Mock
from queue import Queue
import zlib

from watchdogs.app import MAP_NODE_LIMIT, MapMarker, MapProjection, WatchDogsGame
from watchdogs.map_index import GeoObjectIndex, GeoPointIndex
from watchdogs.wardrive_trail import WardriveTrail
from watchdogs.wardrive_ui import RADAR_TRAIL_POINT_LIMIT, WardriveUI
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


def test_geo_index_reuses_identical_query_until_source_changes():
    points = [
        {"lat": 40, "lon": -90},
        {"lat": 40.001, "lon": -90.001},
    ]
    index = GeoPointIndex()
    index.ensure(points)
    first = index.query(40, -90, 0.02, 0.02)
    second = index.query(40, -90, 0.02, 0.02)
    assert second is first

    points.append({"lat": 40.002, "lon": -90.002})
    index.ensure(points)
    third = index.query(40, -90, 0.02, 0.02)
    assert third is not first and len(third) == 3


def test_geo_index_uses_fine_bucket_for_dense_close_view():
    points = [
        {"lat": 40 + (i % 100) / 1000,
         "lon": -90 + (i // 100) / 1000}
        for i in range(10_000)
    ]
    index = GeoPointIndex()
    index.ensure(points)
    result = index.query(40.005, -89.995, 0.01, 0.01)

    assert result
    assert index._last_query_candidates < len(points) // 10


def test_projection_settles_when_camera_is_within_half_a_pixel():
    projection = MapProjection()
    projection.zoom = 13
    projection.center_lat = 40.0
    projection.center_lon = -90.0
    subpixel = 0.49 * projection.lon_span / 640
    projection.smooth_move(40.0 + subpixel, -90.0 - subpixel)

    projection.update()

    assert projection.center_lat == projection._target_lat
    assert projection.center_lon == projection._target_lon


def test_projection_keeps_smoothing_visible_camera_motion():
    projection = MapProjection()
    projection.zoom = 13
    projection.center_lat = 40.0
    projection.center_lon = -90.0
    projection.smooth_move(40.01, -90.01)

    projection.update()

    assert projection.center_lat == 40.0008
    assert projection.center_lon == -90.0008


def test_map_history_view_keeps_only_newest_points_without_truncating_loot():
    game = WatchDogsGame.__new__(WatchDogsGame)
    game.loot_points = [{"id": index} for index in range(MAP_NODE_LIMIT + 12)]

    view = game._get_map_loot_points()

    assert len(view) == MAP_NODE_LIMIT
    assert view[0]["id"] == 12
    assert view[-1]["id"] == MAP_NODE_LIMIT + 11
    assert len(game.loot_points) == MAP_NODE_LIMIT + 12
    assert game._get_map_loot_points() is view

    game.wifi_networks = [object()] * 130
    reduced = game._get_map_loot_points()
    assert len(reduced) == MAP_NODE_LIMIT - 256
    assert len(reduced) + len(game.wifi_networks) <= MAP_NODE_LIMIT


def test_live_scan_updates_do_not_rescan_complete_historical_loot():
    class CountingPoints(list):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    points = CountingPoints(
        {"lat": 40, "lon": -90, "type": "wifi",
         "bssid": f"00:11:22:33:{index // 256:02X}:{index % 256:02X}"}
        for index in range(5000))
    game = WatchDogsGame.__new__(WatchDogsGame)
    game.loot_points = points
    game.wifi_networks = []
    game.ble_devices = []
    game.wardrive = NS(
        layer_mode=lambda _layer: "keep", cell_candidates=[])

    game._get_map_loot_points()
    assert points.iterations == 1

    game._map_registry().observe(
        "wifi", "AA:BB:CC:DD:EE:FF", seen_at=1,
        lat=40, lon=-90, rssi=-50)
    game._get_map_loot_points()

    assert points.iterations == 1
    assert len(game._map_history_candidates) <= MAP_NODE_LIMIT * 2


def test_trail_draw_dispatches_to_cached_layer(monkeypatch):
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
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = {"trail": True, "trail_mode": "solid"}
    ui.trail = trail
    ui.history_trail = None
    ui.app = NS(proj=projection)
    ui._trail_layer = NS(
        image=None, request=Mock(), step=Mock(), draw=Mock(),
        invalidate=Mock())
    px = NS()
    monkeypatch.setitem(__import__("sys").modules, "pyxel", px)

    ui.draw_trail()
    ui.draw_trail()
    assert ui._trail_layer.request.call_count == 2
    assert ui._trail_layer.step.call_count == 2
    assert ui._trail_layer.draw.call_count == 2
    args = ui._trail_layer.request.call_args.args
    assert args == (trail.points, trail.revision, "solid", projection)

    trail.points.append({"lat": 40.0002, "lon": -90.0002, "segment": 2})
    trail.revision += 1
    ui.draw_trail()
    assert ui._trail_layer.request.call_args.args[1] == trail.revision


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


def test_radar_trail_uses_only_a_bounded_recent_tail(monkeypatch):
    trail = WardriveTrail()
    trail.points.extend(
        {"lat": 40, "lon": -90 + index / 10_000_000,
         "segment": 1, "density": 0}
        for index in range(4096))
    trail.revision = 4096
    ui = WardriveUI.__new__(WardriveUI)
    ui.settings = {"trail": True, "trail_mode": "solid"}
    ui.trail = trail
    ui.history_trail = None
    ui.history_notables = {}
    ui.notables = {}
    ui.cell_candidates = []
    ui._radar_trail_key = None
    ui._radar_trail_segments = []
    ui.app = NS(player_lat=40, player_lon=-90)
    px = NS(line=Mock(), circb=Mock(), pset=Mock())
    monkeypatch.setitem(__import__("sys").modules, "pyxel", px)

    ui.draw_radar(20, 20, 20, 1000)

    assert len(ui._radar_trail_segments) <= RADAR_TRAIL_POINT_LIMIT - 1
    assert px.line.call_count <= (RADAR_TRAIL_POINT_LIMIT - 1) * 2


def test_periodic_loot_snapshot_is_applied_on_game_thread(monkeypatch):
    import watchdogs.app as appmod
    game = WatchDogsGame.__new__(WatchDogsGame)
    game.loot = NS(loot_totals={"wardriving_wifi": 7})
    game._app_dir = "/tmp"
    game.loot_points = []
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


def test_empty_loot_snapshot_clears_published_map_points():
    game = WatchDogsGame.__new__(WatchDogsGame)
    game.loot_points = [{"lat": 40, "lon": -90}]
    game._loot_points_revision = 3
    game._map_loot_view_key = ("old",)

    game._apply_loot_snapshot({"points": []})

    assert game.loot_points == []
    assert game._loot_points_revision == 4
    assert game._map_loot_view_key is None


def test_map_policy_change_invalidates_every_cached_node_layer():
    game = WatchDogsGame.__new__(WatchDogsGame)
    game.wardrive = NS(fade_seconds=lambda: 60)
    game._history_node_layer = NS(invalidate=Mock())
    game._live_node_layer = NS(invalidate=Mock())
    game._radar_node_layer = NS(invalidate=Mock())
    game._map_live_view_key = ("old",)
    game._map_loot_view_key = ("old",)
    game._map_display_next_tick = 10

    game._on_map_policy_changed()

    assert game._map_live_view_key is None
    assert game._map_loot_view_key is None
    assert game._map_display_next_tick == 0
    assert game._map_observations.lifetime == 60
    for layer in (game._history_node_layer, game._live_node_layer,
                  game._radar_node_layer):
        layer.invalidate.assert_called_once_with()


def test_restored_meshcore_marker_uses_historical_display_policy(monkeypatch):
    import watchdogs.app as appmod
    game = WatchDogsGame.__new__(WatchDogsGame)
    game.markers = [MapMarker(
        40, -90, "saved", "meshcore", key="meshcore:1", last_seen=0)]
    game.wardrive = NS(layer_color=Mock(return_value=None))
    game.proj = NS(zoom=0)
    monkeypatch.setattr(appmod, "pyxel", NS(frame_count=0))

    game._draw_markers()

    assert game.wardrive.layer_color.call_args.kwargs["historical"] is True


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
