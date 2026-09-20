from types import SimpleNamespace as NS

import pytest

from watchdogs.trail_layer import TrailLayer, _heat_color


class FakeImage:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.calls = []

    def cls(self, *args):
        self.calls.append(("cls", args))

    def line(self, *args):
        self.calls.append(("line", args))


class FakePyxel:
    Image = FakeImage

    def __init__(self):
        self.blit_calls = []
        self.clip_calls = []
        self.line_calls = []

    def blt(self, *args):
        self.blit_calls.append(args)

    def clip(self, *args):
        self.clip_calls.append(args)

    def line(self, *args):
        self.line_calls.append(args)


def projection(**changes):
    values = dict(zoom=13, center_lat=40.0, center_lon=-90.0,
                  lon_span=0.08)
    values.update(changes)
    return NS(**values)


def points(*rows):
    return [dict(lat=lat, lon=lon, segment=segment, density=density)
            for lat, lon, segment, density in rows]


def finish(layer):
    while layer._job is not None:
        layer.step(max_ms=1000, max_records=100_000)


def publish(layer, records, *, style="solid", revision=1, proj=None):
    proj = proj or projection()
    px = FakePyxel()
    layer.request(records, revision, style, proj)
    finish(layer)
    layer.draw(px, proj)
    return px, proj


def image_lines(layer):
    return [args for name, args in layer.image.calls if name == "line"]


def test_solid_trail_is_three_pixels_thick_and_stable_frame_is_one_blit():
    layer = TrailLayer(640, 16, 218)
    records = points((40, -90.001, 1, 0), (40, -90, 1, 0))
    px, proj = publish(layer, records)

    lines = image_lines(layer)
    assert len(lines) == 3
    assert {line[-1] for line in lines} == {3}
    assert len({(line[1], line[3]) for line in lines}) == 3
    assert px.line_calls == []

    px.blit_calls.clear()
    layer.request(records, 1, "solid", proj)
    layer.step()
    layer.draw(px, proj)
    assert len(px.blit_calls) == 1
    assert px.blit_calls[0][-1] == 15
    assert px.line_calls == []


@pytest.mark.parametrize("density,color", [
    (0, 12), (2, 12), (3, 3), (5, 3), (6, 11), (11, 11),
    (12, 10), (23, 10), (24, 9), (47, 9), (48, 8), (999, 8),
])
def test_heat_color_bands(density, color):
    assert _heat_color(density) == color


def test_heat_trail_uses_each_segment_endpoint_density():
    layer = TrailLayer(640, 16, 218)
    records = points(
        (40, -90.002, 1, 0),
        (40, -90.001, 1, 7),
        (40, -90.000, 1, 50),
    )
    publish(layer, records, style="heat")
    colors = [line[-1] for line in image_lines(layer)]
    assert colors == [11, 11, 11, 8, 8, 8]


def test_segment_breaks_and_zero_length_points_are_not_connected():
    layer = TrailLayer(640, 16, 218)
    records = points(
        (40, -90.003, 1, 0),
        (40, -90.002, 1, 0),
        (40, -90.001, 2, 0),
        (40, -90.001, 2, 0),
        (40, -90.000, 2, 0),
    )
    publish(layer, records)
    assert len(image_lines(layer)) == 6


def test_build_is_incremental_and_image_is_only_created_during_draw():
    layer = TrailLayer(640, 16, 218)
    records = [dict(lat=40, lon=-90 + index / 100_000,
                    segment=1, density=0) for index in range(600)]
    proj = projection()
    layer.request(records, 1, "solid", proj)
    layer.step(max_ms=1000, max_records=4)
    assert layer._job is not None
    assert layer.image is None

    finish(layer)
    assert layer._ready is not None
    assert layer.image is None
    px = FakePyxel()
    layer.draw(px, proj)
    assert layer.image is not None
    assert layer.image.calls[0] == ("cls", (15,))


def test_overscan_cache_reuses_image_and_translates_until_threshold():
    layer = TrailLayer(640, 16, 218, overscan=128, rebuild_shift=64)
    records = points((40, -90.001, 1, 0), (40, -90, 1, 0))
    px, proj = publish(layer, records)
    first_image = layer.image

    proj.center_lon += 10 * proj.lon_span / 640
    layer.request(records, 1, "solid", proj)
    assert layer._job is None
    layer.draw(px, proj)
    assert layer.image is first_image
    assert px.blit_calls[-1][0] == -layer.overscan - 10

    proj.center_lon += 60 * proj.lon_span / 640
    layer.request(records, 1, "solid", proj)
    assert layer._job is not None
    assert layer.image is first_image


def test_invalidation_removes_published_and_pending_work():
    layer = TrailLayer(640, 16, 218)
    records = points((40, -90.001, 1, 0), (40, -90, 1, 0))
    publish(layer, records)
    records.append(dict(lat=40, lon=-89.999, segment=1, density=0))
    layer.request(records, 2, "solid", projection())
    assert layer._job is not None

    layer.invalidate()
    assert layer.image is None
    assert layer.data_token is None
    assert layer._job is layer._queued is layer._ready is None


def test_latest_queued_request_replaces_stale_build_before_publication():
    layer = TrailLayer(640, 16, 218)
    records = [dict(lat=40, lon=-90 + index / 100_000,
                    segment=1, density=0) for index in range(600)]
    proj = projection()
    layer.request(records, 1, "solid", proj)
    layer.step(max_ms=1000, max_records=1)

    records.append(dict(lat=40, lon=-89.99, segment=1, density=50))
    layer.request(records, 2, "heat", proj)
    assert layer._queued is not None
    finish(layer)
    # The helper runs through both jobs.  Only the latest may become ready.
    assert layer._ready[0].token[2:] == (2, "heat")
    px = FakePyxel()
    layer.draw(px, proj)
    assert layer.data_token[2:] == (2, "heat")


def test_long_trail_publishes_during_continuous_small_camera_motion():
    layer = TrailLayer(640, 16, 218, rebuild_shift=64)
    records = [
        dict(lat=40, lon=-90 + index / 1_000_000,
             segment=1, density=index % 64)
        for index in range(4096)
    ]
    proj = projection()
    px = FakePyxel()

    for _frame in range(120):
        # A quarter pixel of camera movement per frame models the map easing
        # while GPS is moving, without crossing the overscan rebuild margin.
        proj.center_lon += 0.25 * proj.lon_span / 640
        layer.request(records, 1, "heat", proj)
        layer.step(max_ms=1000, max_records=256)
        layer.draw(px, proj)
        if layer.image is not None:
            break

    assert layer.image is not None
    assert layer._job is None
    assert layer._queued is None
    assert px.blit_calls


def test_completed_stale_request_is_discarded_before_draw():
    layer = TrailLayer(640, 16, 218)
    first = points((40, -90.001, 1, 0), (40, -90, 1, 0))
    second = points((40, -90.002, 1, 0), (40, -90, 1, 50))
    proj = projection()
    layer.request(first, 1, "solid", proj)
    finish(layer)
    assert layer._ready is not None

    layer.request(second, 2, "heat", proj)
    assert layer._ready is None and layer._job is not None
    finish(layer)
    px = FakePyxel()
    layer.draw(px, proj)
    assert layer.style == "heat"
    assert {line[-1] for line in image_lines(layer)} == {8}


def test_decimation_caps_rendered_segments():
    layer = TrailLayer(640, 16, 218, max_segments=32)
    records = [dict(lat=40, lon=-90 + index / 1_000_000,
                    segment=1, density=0) for index in range(1000)]
    publish(layer, records)
    assert len(image_lines(layer)) <= 32 * 3


def test_decimation_keeps_short_segment_endpoints_without_joining_breaks():
    layer = TrailLayer(640, 16, 218, max_segments=4)
    records = points(
        (40, -90.004, 1, 0),
        (40, -90.003, 1, 0),
        (40, -90.002, 2, 0),
        (40, -90.001, 2, 0),
        (40, -90.000, 3, 0),
        (40, -89.999, 3, 0),
    )
    publish(layer, records)
    # Three separate two-point routes, each rendered three pixels thick.
    assert len(image_lines(layer)) == 9


def test_invalid_style_is_rejected():
    with pytest.raises(ValueError, match="trail style"):
        TrailLayer(640, 16, 218).request([], 0, "sparkles", projection())
