from types import SimpleNamespace

import pytest

from watchdogs.map_display import (
    DEFAULT_LAYER_MODES,
    DEFAULT_RECENT_SECONDS,
    DISPLAY_MODES,
    MAP_LAYERS,
    MAX_RECENT_OBSERVATIONS,
    MODE_KEEP,
    MODE_OFF,
    MODE_RECENT,
    RecentObservationRegistry,
    color_for_record,
    cycle_mode,
    display_state,
    fade_color,
    fade_stage,
    layer_label,
    mode_label,
    normalize_layer_modes,
    normalize_layer_name,
    normalize_mode,
    record_last_seen,
)


def test_layer_catalog_and_defaults_are_complete():
    assert MAP_LAYERS == (
        "wifi", "ble", "cell", "flock", "axon", "meshcore", "adsb",
        "sensor433", "handshake",
    )
    assert set(DEFAULT_LAYER_MODES) == set(MAP_LAYERS)
    assert DEFAULT_LAYER_MODES == {
        "wifi": "recent", "ble": "recent", "cell": "recent",
        "flock": "keep", "axon": "keep", "meshcore": "keep",
        "adsb": "recent", "sensor433": "recent", "handshake": "keep",
    }
    assert DEFAULT_RECENT_SECONDS == 30.0
    assert MAX_RECENT_OBSERVATIONS == 512


@pytest.mark.parametrize(("raw", "expected"), [
    ("Wi-Fi", "wifi"), ("WLAN", "wifi"), ("BT", "ble"),
    ("Bluetooth", "ble"), ("LTE", "cell"), ("LoRa", "meshcore"),
    ("ADS-B", "adsb"), ("aircraft", "adsb"), ("433", "sensor433"),
    ("handshakes", "handshake"),
])
def test_layer_names_accept_ui_and_legacy_aliases(raw, expected):
    assert normalize_layer_name(raw) == expected


def test_unknown_layer_uses_requested_default_and_labels_are_human_readable():
    assert normalize_layer_name("nope") is None
    assert normalize_layer_name(None, "wifi") == "wifi"
    assert layer_label("lora") == "MeshCore"
    assert layer_label("adsb") == "ADS-B"


@pytest.mark.parametrize(("raw", "expected"), [
    ("off", MODE_OFF), (False, MODE_OFF), ("hide", MODE_OFF),
    ("fade", MODE_RECENT), ("30s", MODE_RECENT), ("recent", MODE_RECENT),
    ("keep", MODE_KEEP), (True, MODE_KEEP), ("persistent", MODE_KEEP),
])
def test_mode_normalization_supports_saved_and_ui_values(raw, expected):
    assert normalize_mode(raw) == expected


def test_invalid_modes_fall_back_without_mutating_defaults():
    result = normalize_layer_modes({"wifi": "nonsense", "bt": "off", "bad": "keep"})
    assert result["wifi"] == MODE_RECENT
    assert result["ble"] == MODE_OFF
    assert set(result) == set(MAP_LAYERS)
    result["wifi"] = MODE_KEEP
    assert DEFAULT_LAYER_MODES["wifi"] == MODE_RECENT


def test_modes_cycle_in_both_directions_and_have_short_labels():
    assert DISPLAY_MODES == (MODE_OFF, MODE_RECENT, MODE_KEEP)
    assert cycle_mode(MODE_OFF) == MODE_RECENT
    assert cycle_mode(MODE_RECENT) == MODE_KEEP
    assert cycle_mode(MODE_KEEP) == MODE_OFF
    assert cycle_mode(MODE_OFF, -1) == MODE_KEEP
    assert mode_label(MODE_OFF) == "OFF"
    assert mode_label(MODE_RECENT) == "FADE 30s"
    assert mode_label(MODE_KEEP) == "KEEP"


def test_fade_stages_spend_most_of_lifetime_bright_then_disappear():
    assert fade_stage(100, now=100) == 0
    assert fade_stage(100, now=119.999) == 0
    assert fade_stage(100, now=120) == 1
    assert fade_stage(100, now=124.999) == 1
    assert fade_stage(100, now=125) == 2
    assert fade_stage(100, now=129.999) == 2
    assert fade_stage(100, now=130) is None
    assert fade_stage(101, now=100) == 0
    assert fade_stage(None, now=100) is None


def test_display_state_distinguishes_off_recent_and_keep():
    record = {"last_seen": 100}
    off = display_state(MODE_OFF, record, now=110)
    recent = display_state(MODE_RECENT, record, now=125)
    expired = display_state(MODE_RECENT, record, now=130)
    keep = display_state(MODE_KEEP, record, now=1000)
    assert not off.visible and off.stage is None
    assert recent.visible and recent.stage == 2 and recent.expires_in == 5
    assert not expired.visible and expired.expires_in == 0
    assert keep.visible and keep.stage == 0 and keep.expires_in is None


def test_timestamp_helpers_accept_mapping_object_and_numeric_records():
    assert record_last_seen(12.5) == 12.5
    assert record_last_seen({"seen_at": "13"}) == 13
    assert record_last_seen(SimpleNamespace(last_seen=14)) == 14
    assert record_last_seen({}) is None


def test_palette_fade_is_discrete_and_expired_record_has_no_color():
    assert fade_color(11, 0) == 11
    assert fade_color(11, 1) == 3
    assert fade_color(11, 2) == 1
    assert fade_color(11, None) is None
    assert fade_color(99, 2) == 99
    assert fade_color(8, 1, {8: (8, 2, 1)}) == 2
    record = SimpleNamespace(last_seen=100)
    assert color_for_record(8, MODE_RECENT, record, now=125) == 1
    assert color_for_record(8, MODE_RECENT, record, now=130) is None
    assert color_for_record(8, MODE_KEEP, record, now=1000) == 8


def test_registry_deduplicates_and_keeps_latest_position_metadata_and_peak_rssi():
    registry = RecentObservationRegistry(capacity=4)
    first = registry.observe(
        "wifi", "aa:bb", seen_at=10, lat=40, lon=-90, rssi="-65",
        metadata={"name": "old", "channel": 1})
    second = registry.observe(
        "wifi", "AA:BB", seen_at=11, lat=41, lon=-91, rssi=-80,
        metadata={"name": "new", "channel": 6})
    assert len(registry) == 1
    assert first.identity == "AA:BB"
    assert second.lat == 41 and second.lon == -91
    assert second.rssi == -65
    assert second.latest_rssi == -80
    assert second.metadata == {"name": "new", "channel": 6}
    assert second.last_seen == 11


def test_duplicate_time_refresh_does_not_invalidate_unchanged_bright_content():
    registry = RecentObservationRegistry()
    registry.observe("ble", "aa", seen_at=0, lat=1, lon=2, rssi=-50,
                     metadata={"name": "tag"})
    revision = registry.revision
    registry.observe("ble", "aa", seen_at=5, lat=1, lon=2, rssi=-60,
                     metadata={"name": "tag"})
    assert registry.revision == revision
    current = registry.snapshot(layer="ble", now=5)[0]
    assert current.latest_rssi == -60
    assert current.rssi == -50


def test_refreshing_a_dim_record_makes_it_bright_and_invalidates_cache():
    registry = RecentObservationRegistry()
    registry.observe("wifi", "aa", seen_at=0)
    before_tick = registry.revision
    result = registry.tick(now=25)
    assert result.changed == 1 and result.expired == 0
    assert registry.revision == before_tick + 1
    dim_revision = registry.revision
    refreshed = registry.observe("wifi", "aa", seen_at=26)
    assert refreshed.stage == 0
    assert registry.revision == dim_revision + 1


def test_refresh_after_expiry_starts_a_new_peak_rssi_window():
    registry = RecentObservationRegistry()
    registry.observe("wifi", "aa", seen_at=0, rssi=-40)
    refreshed = registry.observe("wifi", "aa", seen_at=31, rssi=-85)
    assert refreshed.rssi == -85
    assert refreshed.latest_rssi == -85


def test_tick_invalidates_only_on_stage_transition_or_expiry():
    registry = RecentObservationRegistry()
    registry.observe("wifi", "aa", seen_at=100)
    initial = registry.revision
    assert registry.tick(now=110).changed == 0
    assert registry.revision == initial
    assert registry.tick(now=120).changed == 1
    muted = registry.revision
    assert registry.tick(now=121).changed == 0
    assert registry.revision == muted
    assert registry.tick(now=130).expired == 1
    assert registry.revision == muted + 1
    assert registry.tick(now=131).changed == 0


def test_snapshot_reads_do_not_change_revision_and_respect_each_mode():
    registry = RecentObservationRegistry()
    registry.observe("wifi", "aa", seen_at=0)
    revision = registry.revision
    assert registry.snapshot(layer="wifi", mode=MODE_RECENT, now=31) == ()
    kept = registry.snapshot(layer="wifi", mode=MODE_KEEP, now=31)
    assert len(kept) == 1 and kept[0].stage == 0
    assert registry.snapshot(layer="wifi", mode=MODE_OFF, now=31) == ()
    assert registry.revision == revision


def test_shared_capacity_evicts_least_recently_refreshed_deterministically():
    registry = RecentObservationRegistry(capacity=3)
    registry.observe("wifi", "one", seen_at=1)
    registry.observe("ble", "two", seen_at=2)
    registry.observe("wifi", "three", seen_at=3)
    registry.observe("wifi", "one", seen_at=4)  # refresh makes ONE newest
    registry.observe("ble", "four", seen_at=5)  # TWO is now oldest
    assert len(registry) == 3
    assert ("ble", "TWO") not in registry
    assert ("wifi", "ONE") in registry
    assert [item.identity for item in registry.snapshot(mode=MODE_KEEP, now=5)] == [
        "THREE", "ONE", "FOUR",
    ]


def test_layer_snapshots_apply_independent_settings():
    registry = RecentObservationRegistry()
    registry.observe("wifi", "ap", seen_at=0)
    registry.observe("ble", "tag", seen_at=0)
    result = registry.snapshots_by_layer(
        {"wifi": MODE_KEEP, "ble": MODE_RECENT}, now=31)
    assert [item.identity for item in result["wifi"]] == ["AP"]
    assert result["ble"] == ()


def test_registry_get_is_constant_time_identity_lookup_with_policy():
    registry = RecentObservationRegistry()
    registry.observe("wifi", "aa:bb", seen_at=0, lat=40, lon=-90)
    assert registry.get("wifi", "AA:BB", mode=MODE_KEEP, now=100).lat == 40
    assert registry.get("wifi", "AA:BB", mode=MODE_RECENT, now=31) is None
    assert registry.get("wifi", "missing", mode=MODE_KEEP, now=1) is None


def test_registry_validates_input_and_ignores_invalid_coordinate_updates():
    registry = RecentObservationRegistry()
    with pytest.raises(ValueError):
        registry.observe("cell", "tower")
    with pytest.raises(ValueError):
        registry.observe("wifi", "")
    with pytest.raises(ValueError):
        registry.observe("wifi", "ap", seen_at=float("nan"))
    original = registry.observe("wifi", "ap", seen_at=1, lat=40, lon=-90)
    updated = registry.observe("wifi", "ap", seen_at=2, lat=999, lon=-999)
    assert (updated.lat, updated.lon) == (original.lat, original.lon)


def test_remove_and_clear_only_advance_revision_when_content_changes():
    registry = RecentObservationRegistry()
    registry.observe("wifi", "ap", seen_at=1)
    revision = registry.revision
    assert not registry.remove("wifi", "missing")
    assert registry.revision == revision
    assert registry.remove("wifi", "ap")
    assert registry.revision == revision + 1
    assert not registry.clear()
    registry.observe("ble", "tag", seen_at=2)
    before_clear = registry.revision
    assert registry.clear()
    assert registry.revision == before_clear + 1
    assert len(registry) == 0


def test_default_registry_capacity_constant_is_applied():
    registry = RecentObservationRegistry()
    assert registry.capacity == MAX_RECENT_OBSERVATIONS
