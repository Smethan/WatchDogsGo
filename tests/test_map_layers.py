from types import SimpleNamespace as NS

from watchdogs.app import HUD_TOP, MAP_H, W, MapProjection
from watchdogs.map_layers import HistoricalNodeLayer


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
    proj.center_lon += 0.001
    subject.request(points, proj)
    assert subject._queued is not None
    assert subject._queued.center_lon == proj.center_lon


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
