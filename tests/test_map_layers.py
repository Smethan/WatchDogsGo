from types import SimpleNamespace as NS

from watchdogs.app import HUD_TOP, MAP_H, MapProjection, W
from watchdogs.map_layers import HistoricalNodeLayer, LiveNodeLayer, RadarNodeLayer


class FakeImage:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.calls = []

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, args))
        return record


class FakePyxel:
    Image = FakeImage
    frame_count = 0

    def __init__(self):
        self.blit_calls = []
        self.clip_calls = []
        self.rings = []

    def blt(self, *args):
        self.blit_calls.append(args)

    def clip(self, *args):
        self.clip_calls.append(args)

    def circb(self, *args):
        self.rings.append(args)


def layer():
    return HistoricalNodeLayer(
        W, HUD_TOP, MAP_H, wifi_color=11, bt_color=3, cell_color=10)


def projection():
    proj = MapProjection()
    proj.zoom = 13
    proj.center_lat = proj._target_lat = 40.0
    proj.center_lon = proj._target_lon = -90.0
    return proj


def finish(layer, records=100_000):
    while layer._job is not None:
        layer.step(max_ms=1000, max_records=records)


def test_history_build_is_incremental_and_published_only_during_draw():
    points = [
        {"lat": 40 + i / 1_000_000, "lon": -90, "type": "wifi"}
        for i in range(300)
    ]
    subject, proj, px = layer(), projection(), FakePyxel()
    subject.request(points, proj)
    subject.step(max_ms=1000, max_records=10)
    assert subject._job is not None
    assert subject.image is None

    finish(subject)
    assert subject._ready is not None
    assert subject.image is None
    subject.draw(px, proj)
    assert subject.image is not None
    assert px.blit_calls


def test_history_layer_reuses_overscan_until_camera_crosses_threshold():
    points = [{"lat": 40, "lon": -90, "type": "wifi", "label": "AP"}]
    subject, proj, px = layer(), projection(), FakePyxel()
    subject.request(points, proj)
    finish(subject)
    subject.draw(px, proj)
    first_image = subject.image

    # Ten screen pixels of movement translates the cached image without work.
    proj.center_lon += 10 * proj.lon_span / W
    subject.request(points, proj)
    assert subject._job is None
    subject.draw(px, proj)
    assert subject.image is first_image
    assert px.blit_calls[-1][0] == -subject.overscan - 10

    # Crossing the configured half-overscan begins a bounded replacement.
    proj.center_lon += 60 * proj.lon_span / W
    subject.request(points, proj)
    assert subject._job is not None
    assert subject.image is first_image


def test_history_layer_queues_latest_view_while_build_is_running():
    points = [{"lat": 40, "lon": -90, "type": "wifi"}] * 500
    subject, proj = layer(), projection()
    subject.request(points, proj)
    subject.step(max_ms=1000, max_records=1)
    proj.center_lon += 0.01
    subject.request(points, proj)
    assert subject._queued is not None
    assert subject._queued.center_lon == proj.center_lon


def test_history_layer_never_publishes_a_completed_stale_request():
    subject, proj, px = layer(), projection(), FakePyxel()
    old = [{"lat": 40, "lon": -90, "type": "wifi"}]
    new = [{"lat": 40, "lon": -89.999, "type": "wifi"}]
    subject.request(old, proj, revision=1)
    finish(subject)
    assert subject._ready is not None

    subject.request(new, proj, revision=2)
    assert subject._ready is None and subject._job is not None
    finish(subject)
    subject.draw(px, proj)

    assert subject.data_token[-1] == 2


def test_history_layer_finishes_during_continuous_small_camera_motion():
    points = [
        {"lat": 40, "lon": -90 + index / 1_000_000, "type": "wifi"}
        for index in range(512)
    ]
    subject, proj, px = layer(), projection(), FakePyxel()
    for _frame in range(120):
        proj.center_lon += 0.25 * proj.lon_span / W
        subject.request(points, proj, revision=1)
        subject.step(max_ms=1000, max_records=32)
        subject.draw(px, proj)
        if subject.image is not None:
            break
    assert subject.image is not None
    assert subject._job is None and subject._queued is None


def test_history_layer_keeps_cluster_members_for_popup_and_translates_nav():
    points = [
        {"lat": 40, "lon": -90, "type": "wifi", "label": "one"},
        {"lat": 40.000001, "lon": -90.000001, "type": "bt", "label": "two"},
    ]
    subject, proj, px = layer(), projection(), FakePyxel()
    subject.request(points, proj)
    finish(subject)
    subject.draw(px, proj)
    assert len(subject.clusters) == 1
    assert subject.clusters[0]["points"] == points

    original = subject.screen_position(subject.clusters[0], proj)
    proj.center_lon += 7 * proj.lon_span / W
    shifted = subject.screen_position(subject.clusters[0], proj)
    assert shifted == (original[0] - 7, original[1])


def test_live_layer_builds_two_cached_blink_frames_and_reuses_them():
    wifi = [NS(lat=40, lon=-90, bssid="00:11:22:33:44:55",
               color=10, hacked=False)]
    ble = [NS(lat=40.0001, lon=-90, mac="AA:BB:CC:DD:EE:FF",
              color=9, hacked=False, blink_phase=1.2)]
    subject = LiveNodeLayer(W, HUD_TOP, MAP_H)
    proj, px = projection(), FakePyxel()
    subject.request(wifi, ble, 1, frozenset(), proj)
    while subject._job is not None:
        subject.step(max_ms=1000)
    assert subject.images == (None, None)
    subject.draw(px, proj)
    first_images = subject.images
    assert all(image is not None for image in first_images)
    assert first_images[0] is not first_images[1]

    proj.center_lon += 10 * proj.lon_span / W
    subject.request(wifi, ble, 1, frozenset(), proj)
    assert subject._job is None
    subject.draw(px, proj)
    assert subject.images == first_images
    assert px.blit_calls[-1][0] == -subject.overscan - 10


def test_live_layer_excludes_notable_devices_from_cached_dots():
    wifi = [NS(lat=40, lon=-90, bssid="00:11:22:33:44:55",
               color=10, hacked=False)]
    subject = LiveNodeLayer(W, HUD_TOP, MAP_H)
    proj, px = projection(), FakePyxel()
    notable = frozenset({"wifi:00:11:22:33:44:55"})
    subject.request(wifi, [], 1, notable, proj)
    while subject._job is not None:
        subject.step(max_ms=1000)
    subject.draw(px, proj)
    primitive_calls = {
        name for image in subject.images for name, _args in image.calls
        if name != "cls"
    }
    assert primitive_calls == set()


def test_live_layer_coalesces_rapid_data_changes_between_publishes():
    wifi = [NS(lat=40, lon=-90, bssid="00:11:22:33:44:55",
               color=10, hacked=False)]
    subject = LiveNodeLayer(
        W, HUD_TOP, MAP_H, min_rebuild_interval=60)
    proj, px = projection(), FakePyxel()
    subject.request(wifi, [], 1, frozenset(), proj)
    while subject._job is not None:
        subject.step(max_ms=1000)
    subject.draw(px, proj)

    wifi.append(NS(lat=40, lon=-90.001, bssid="00:11:22:33:44:66",
                   color=9, hacked=False))
    subject.request(wifi, [], 2, frozenset(), proj)
    assert subject._job is None
    assert subject._queued is not None

    subject._last_publish_at -= 61
    subject.step(max_ms=1000)
    assert subject._ready is not None
    subject.draw(px, proj)
    assert subject.data_token[1] == 2


def test_live_layer_discards_completed_stale_snapshot_before_draw():
    old = [NS(lat=40, lon=-90, bssid="00:11:22:33:44:55",
              color=10, hacked=False)]
    new = [NS(lat=40, lon=-89.999, bssid="00:11:22:33:44:66",
              color=9, hacked=False)]
    subject, proj, px = LiveNodeLayer(W, HUD_TOP, MAP_H), projection(), FakePyxel()
    subject.request(old, [], 1, frozenset(), proj)
    finish(subject)
    assert subject._ready is not None

    subject.request(new, [], 2, frozenset(), proj)
    assert subject._ready is None and subject._job is not None
    finish(subject)
    subject.draw(px, proj)

    assert subject.data_token[4] == 2


def test_live_layer_finishes_during_continuous_small_camera_motion():
    wifi = [
        NS(lat=40, lon=-90 + index / 1_000_000,
           bssid=f"00:11:22:33:{index // 256:02X}:{index % 256:02X}",
           color=10, hacked=False)
        for index in range(512)
    ]
    subject, proj, px = LiveNodeLayer(W, HUD_TOP, MAP_H), projection(), FakePyxel()
    ble = []
    for _frame in range(120):
        proj.center_lon += 0.25 * proj.lon_span / W
        subject.request(wifi, ble, 1, frozenset(), proj)
        subject.step(max_ms=1000, max_records=32)
        subject.draw(px, proj)
        if subject.images[0] is not None:
            break
    assert subject.images[0] is not None
    assert subject._job is None and subject._queued is None


def test_radar_layer_deduplicates_pixels_and_translates_small_gps_moves():
    wifi = [
        NS(lat=40, lon=-90, bssid=f"00:11:22:33:44:{index:02X}",
           color=10, hacked=False)
        for index in range(20)
    ]
    subject, px, ble, loot = RadarNodeLayer(20), FakePyxel(), [], []
    subject.request(
        wifi, ble, loot, 1, frozenset(), 40, -90, scale=1000)
    while subject._job is not None:
        subject.step(max_ms=1000)
    subject.draw(px, 610, 40, 40, -90, 1000)
    point_calls = [call for call in subject.image.calls if call[0] == "pset"]
    assert len(point_calls) == 1

    first_image = subject.image
    subject.request(
        wifi, ble, loot, 1, frozenset(), 40, -89.998, scale=1000)
    assert subject._job is None
    subject.draw(px, 610, 40, 40, -89.998, 1000)
    assert subject.image is first_image
    assert px.blit_calls[-1][0] == 610 - 28 - 2


def test_radar_layer_coalesces_rapid_node_updates():
    wifi = [NS(lat=40, lon=-90, bssid="00:11:22:33:44:55",
               color=10, hacked=False)]
    ble, loot = [], []
    subject = RadarNodeLayer(20, min_rebuild_interval=60)
    px = FakePyxel()
    subject.request(wifi, ble, loot, 1, frozenset(), 40, -90, 1000)
    while subject._job is not None:
        subject.step(max_ms=1000)
    subject.draw(px, 610, 40, 40, -90, 1000)

    wifi.append(NS(lat=40, lon=-90.001, bssid="00:11:22:33:44:66",
                   color=9, hacked=False))
    subject.request(wifi, ble, loot, 2, frozenset(), 40, -90, 1000)
    assert subject._job is None and subject._queued is not None
    subject._last_publish_at -= 61
    subject.step(max_ms=1000)
    assert subject._ready is not None


def test_radar_layer_discards_completed_stale_snapshot_before_draw():
    old = [NS(lat=40, lon=-90, bssid="00:11:22:33:44:55",
              color=10, hacked=False)]
    new = [NS(lat=40, lon=-89.999, bssid="00:11:22:33:44:66",
              color=9, hacked=False)]
    subject, px = RadarNodeLayer(20), FakePyxel()
    subject.request(old, [], [], 1, frozenset(), 40, -90, 1000)
    finish(subject)
    assert subject._ready is not None

    subject.request(new, [], [], 2, frozenset(), 40, -90, 1000)
    assert subject._ready is None and subject._job is not None
    finish(subject)
    subject.draw(px, 610, 40, 40, -90, 1000)

    assert subject.data_token[6] == 2


def test_radar_layer_finishes_during_continuous_small_gps_motion():
    wifi = [
        NS(lat=40, lon=-90 + index / 1_000_000,
           bssid=f"00:11:22:33:{index // 256:02X}:{index % 256:02X}",
           color=10, hacked=False)
        for index in range(512)
    ]
    subject, px, center_lon = RadarNodeLayer(20), FakePyxel(), -90.0
    ble, loot = [], []
    scale = 1000
    for _frame in range(120):
        center_lon += 0.25 / scale
        subject.request(
            wifi, ble, loot, 1, frozenset(), 40, center_lon, scale)
        subject.step(max_ms=1000, max_records=32)
        subject.draw(px, 610, 40, 40, center_lon, scale)
        if subject.image is not None:
            break
    assert subject.image is not None
    assert subject._job is None and subject._queued is None
