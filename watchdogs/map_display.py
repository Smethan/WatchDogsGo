"""Display policy and bounded lifecycle state for map observations.

This module deliberately has no Pyxel dependency.  It turns the map layer
settings into small, testable decisions that the renderer can cache:

* each layer is hidden, recent (discrete palette fade), or persistent;
* recent observations stay fully bright for most of a 30 second lifetime,
  then step through two dimmer palette colors before disappearing;
* the high-volume Wi-Fi/BLE working set is deduplicated and globally bounded.

Pyxel has no per-primitive alpha channel, so ``fade_color`` uses palette
substitution rather than pretending that alpha blending is available.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


LAYER_WIFI = "wifi"
LAYER_BLE = "ble"
LAYER_CELL = "cell"
LAYER_FLOCK = "flock"
LAYER_AXON = "axon"
LAYER_MESHCORE = "meshcore"
LAYER_ADSB = "adsb"
LAYER_SENSOR433 = "sensor433"
LAYER_HANDSHAKE = "handshake"

MAP_LAYERS = (
    LAYER_WIFI,
    LAYER_BLE,
    LAYER_CELL,
    LAYER_FLOCK,
    LAYER_AXON,
    LAYER_MESHCORE,
    LAYER_ADSB,
    LAYER_SENSOR433,
    LAYER_HANDSHAKE,
)

MODE_OFF = "off"
MODE_RECENT = "recent"
MODE_KEEP = "keep"
DISPLAY_MODES = (MODE_OFF, MODE_RECENT, MODE_KEEP)

DEFAULT_RECENT_SECONDS = 30.0
MAX_RECENT_OBSERVATIONS = 512

DEFAULT_LAYER_MODES = {
    LAYER_WIFI: MODE_RECENT,
    LAYER_BLE: MODE_RECENT,
    LAYER_CELL: MODE_RECENT,
    LAYER_FLOCK: MODE_KEEP,
    LAYER_AXON: MODE_KEEP,
    LAYER_MESHCORE: MODE_KEEP,
    LAYER_ADSB: MODE_RECENT,
    LAYER_SENSOR433: MODE_RECENT,
    LAYER_HANDSHAKE: MODE_KEEP,
}

LAYER_LABELS = {
    LAYER_WIFI: "WiFi",
    LAYER_BLE: "BLE",
    LAYER_CELL: "Cell",
    LAYER_FLOCK: "Flock",
    LAYER_AXON: "Axon",
    LAYER_MESHCORE: "MeshCore",
    LAYER_ADSB: "ADS-B",
    LAYER_SENSOR433: "433 MHz",
    LAYER_HANDSHAKE: "Handshake",
}

_LAYER_ALIASES = {
    "wi-fi": LAYER_WIFI,
    "wlan": LAYER_WIFI,
    "bt": LAYER_BLE,
    "bluetooth": LAYER_BLE,
    "lte": LAYER_CELL,
    "mast": LAYER_CELL,
    "masts": LAYER_CELL,
    "lora": LAYER_MESHCORE,
    "mesh": LAYER_MESHCORE,
    "aircraft": LAYER_ADSB,
    "ads-b": LAYER_ADSB,
    "ads": LAYER_ADSB,
    "433": LAYER_SENSOR433,
    "rf433": LAYER_SENSOR433,
    "sensor": LAYER_SENSOR433,
    "sensors": LAYER_SENSOR433,
    "hs": LAYER_HANDSHAKE,
    "handshakes": LAYER_HANDSHAKE,
}

_MODE_ALIASES = {
    "hidden": MODE_OFF,
    "hide": MODE_OFF,
    "disabled": MODE_OFF,
    "false": MODE_OFF,
    "0": MODE_OFF,
    "fade": MODE_RECENT,
    "fading": MODE_RECENT,
    "temporary": MODE_RECENT,
    "temp": MODE_RECENT,
    "30s": MODE_RECENT,
    "30 sec": MODE_RECENT,
    "persistent": MODE_KEEP,
    "persist": MODE_KEEP,
    "always": MODE_KEEP,
    "on": MODE_KEEP,
    "true": MODE_KEEP,
    "1": MODE_KEEP,
}


def normalize_layer_name(value: Any, default: str | None = None) -> str | None:
    """Return a canonical layer name, or *default* for an unknown value."""
    if not isinstance(value, str):
        return default
    name = value.strip().lower().replace("_", "-")
    canonical = _LAYER_ALIASES.get(name, name.replace("-", ""))
    return canonical if canonical in MAP_LAYERS else default


def layer_label(layer: Any) -> str:
    """Human-readable label for a canonical name or supported alias."""
    canonical = normalize_layer_name(layer)
    return LAYER_LABELS.get(canonical, str(layer))


def normalize_mode(value: Any, default: str = MODE_RECENT) -> str:
    """Normalize a display mode while remaining friendly to old booleans."""
    if isinstance(value, bool):
        return MODE_KEEP if value else MODE_OFF
    if isinstance(value, str):
        mode = value.strip().lower().replace("_", " ")
        mode = _MODE_ALIASES.get(mode, mode)
        if mode in DISPLAY_MODES:
            return mode
    return default if default in DISPLAY_MODES else MODE_RECENT


def normalize_layer_modes(values: Mapping[str, Any] | None) -> dict[str, str]:
    """Return a complete layer-mode dictionary with safe defaults."""
    result = dict(DEFAULT_LAYER_MODES)
    if not isinstance(values, Mapping):
        return result
    for raw_layer, raw_mode in values.items():
        layer = normalize_layer_name(raw_layer)
        if layer is not None:
            result[layer] = normalize_mode(raw_mode, result[layer])
    return result


def cycle_mode(mode: Any, direction: int = 1) -> str:
    """Cycle OFF -> FADE -> KEEP (or backwards for a negative direction)."""
    current = normalize_mode(mode)
    index = DISPLAY_MODES.index(current)
    step = -1 if direction < 0 else 1
    return DISPLAY_MODES[(index + step) % len(DISPLAY_MODES)]


def mode_label(mode: Any, lifetime: float = DEFAULT_RECENT_SECONDS) -> str:
    """Short menu label for a display mode."""
    normalized = normalize_mode(mode)
    if normalized == MODE_OFF:
        return "OFF"
    if normalized == MODE_KEEP:
        return "KEEP"
    seconds = max(1, round(float(lifetime)))
    return f"FADE {seconds}s"


def _timestamp(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def record_last_seen(record: Any) -> float | None:
    """Read a monotonic timestamp from a number, mapping, or object."""
    if isinstance(record, (int, float)) and not isinstance(record, bool):
        return _timestamp(record)
    if isinstance(record, Mapping):
        for key in ("last_seen", "seen_at", "seen"):
            if key in record:
                return _timestamp(record[key])
        return None
    for name in ("last_seen", "seen_at", "seen"):
        if hasattr(record, name):
            return _timestamp(getattr(record, name))
    return None


def fade_stage(last_seen: Any, now: float | None = None,
               lifetime: float = DEFAULT_RECENT_SECONDS) -> int | None:
    """Return palette stage 0..2, or ``None`` after expiry.

    The dot is fully bright for the first two thirds of its lifetime, muted
    for the next sixth, dark for the final sixth, and then absent.  A future
    timestamp is treated as freshly observed, which tolerates small clock
    ordering mistakes without making a dot immortal.
    """
    seen = record_last_seen(last_seen)
    current = _timestamp(time.monotonic() if now is None else now)
    duration = _timestamp(lifetime)
    if seen is None or current is None or duration is None or duration <= 0:
        return None
    age = max(0.0, current - seen)
    if age >= duration:
        return None
    fraction = age / duration
    if fraction < 2.0 / 3.0:
        return 0
    if fraction < 5.0 / 6.0:
        return 1
    return 2


@dataclass(frozen=True, slots=True)
class DisplayState:
    visible: bool
    stage: int | None
    age: float | None
    expires_in: float | None


def display_state(mode: Any, record: Any, *, now: float | None = None,
                  lifetime: float = DEFAULT_RECENT_SECONDS) -> DisplayState:
    """Resolve visibility and fade state for a timestamped record."""
    normalized = normalize_mode(mode)
    seen = record_last_seen(record)
    current = _timestamp(time.monotonic() if now is None else now)
    age = None if seen is None or current is None else max(0.0, current - seen)
    if normalized == MODE_OFF:
        return DisplayState(False, None, age, None)
    if normalized == MODE_KEEP:
        return DisplayState(True, 0, age, None)
    stage = fade_stage(seen, current, lifetime)
    remaining = None if age is None else max(0.0, float(lifetime) - age)
    return DisplayState(stage is not None, stage, age, remaining)


def is_visible(mode: Any, record: Any, *, now: float | None = None,
               lifetime: float = DEFAULT_RECENT_SECONDS) -> bool:
    return display_state(mode, record, now=now, lifetime=lifetime).visible


# Each tuple is (full, muted, dark).  These are substitutions within Pyxel's
# fixed 16-color palette; callers can provide a project-specific table.
DEFAULT_FADE_PALETTE: dict[int, tuple[int, int, int]] = {
    0: (0, 0, 0),
    1: (1, 1, 0),
    2: (2, 1, 1),
    3: (3, 5, 1),
    4: (4, 4, 1),
    5: (5, 1, 1),
    6: (6, 12, 5),
    7: (7, 13, 5),
    8: (8, 4, 1),
    9: (9, 4, 1),
    10: (10, 9, 4),
    11: (11, 3, 1),
    12: (12, 5, 1),
    13: (13, 5, 1),
    14: (14, 8, 4),
    15: (15, 13, 5),
}


def fade_color(base_color: int, stage: int | None,
               palette: Mapping[int, Sequence[int]] | None = None) -> int | None:
    """Return a discrete Pyxel palette color; ``None`` means do not draw."""
    if stage is None:
        return None
    table = DEFAULT_FADE_PALETTE if palette is None else palette
    colors = table.get(int(base_color), (int(base_color),) * 3)
    if not colors:
        return int(base_color)
    index = min(max(0, int(stage)), len(colors) - 1)
    return int(colors[index])


def color_for_record(base_color: int, mode: Any, record: Any, *,
                     now: float | None = None,
                     lifetime: float = DEFAULT_RECENT_SECONDS,
                     palette: Mapping[int, Sequence[int]] | None = None
                     ) -> int | None:
    """Resolve a record's display mode and return its current draw color."""
    state = display_state(mode, record, now=now, lifetime=lifetime)
    return fade_color(base_color, state.stage, palette) if state.visible else None


def _number(value: Any) -> float | int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _coordinate(value: Any, *, latitude: bool) -> float | None:
    number = _number(value)
    if number is None:
        return None
    number = float(number)
    limit = 90.0 if latitude else 180.0
    return number if -limit <= number <= limit else None


@dataclass(frozen=True, slots=True)
class MapObservation:
    """Detached snapshot returned to render/cache code by the registry."""

    layer: str
    identity: str
    lat: float | None
    lon: float | None
    rssi: float | int | None
    latest_rssi: float | int | None
    last_seen: float
    metadata: dict[str, Any]
    stage: int

    @property
    def kind(self) -> str:
        return self.layer

    @property
    def key(self) -> tuple[str, str]:
        return self.layer, self.identity


@dataclass(slots=True)
class _StoredObservation:
    layer: str
    identity: str
    lat: float | None
    lon: float | None
    rssi: float | int | None
    latest_rssi: float | int | None
    last_seen: float
    metadata: dict[str, Any] = field(default_factory=dict)
    stage: int | None = 0


@dataclass(frozen=True, slots=True)
class TickResult:
    changed: int
    expired: int
    revision: int


class RecentObservationRegistry:
    """O(1) deduplicated Wi-Fi/BLE registry with deterministic LRU eviction.

    The capacity is shared by both radio types.  Refreshing an existing key
    moves it to the newest end and never creates a duplicate.  Old entries are
    retained (until LRU eviction) so ``keep`` mode can show them; ``recent``
    snapshots omit them after the configured lifetime.
    """

    _SUPPORTED = frozenset((LAYER_WIFI, LAYER_BLE))

    def __init__(self, capacity: int = MAX_RECENT_OBSERVATIONS,
                 lifetime: float = DEFAULT_RECENT_SECONDS):
        if isinstance(capacity, bool) or int(capacity) < 1:
            raise ValueError("capacity must be at least 1")
        if not math.isfinite(float(lifetime)) or float(lifetime) <= 0:
            raise ValueError("lifetime must be positive")
        self.capacity = int(capacity)
        self.lifetime = float(lifetime)
        self._items: OrderedDict[
            tuple[str, str], _StoredObservation
        ] = OrderedDict()
        self._revision = 0

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, tuple) or len(key) != 2:
            return False
        layer = normalize_layer_name(key[0])
        identity = self.normalize_identity(key[1])
        return layer is not None and identity is not None and (layer, identity) in self._items

    @property
    def revision(self) -> int:
        return self._revision

    @staticmethod
    def normalize_identity(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip().upper()
        return value or None

    def observe(self, layer: Any, identity: Any, *,
                seen_at: float | None = None, lat: Any = None,
                lon: Any = None, rssi: Any = None,
                metadata: Mapping[str, Any] | None = None,
                **fields: Any) -> MapObservation:
        """Insert or refresh one observation and return its current snapshot."""
        canonical = normalize_layer_name(layer)
        if canonical not in self._SUPPORTED:
            raise ValueError("recent registry only accepts wifi and ble observations")
        normalized_identity = self.normalize_identity(identity)
        if normalized_identity is None:
            raise ValueError("identity must be a non-empty string")
        current = _timestamp(time.monotonic() if seen_at is None else seen_at)
        if current is None:
            raise ValueError("seen_at must be a finite monotonic timestamp")

        supplied = dict(metadata or {})
        supplied.update(fields)
        key = canonical, normalized_identity
        old = self._items.get(key)
        new_lat = _coordinate(lat, latitude=True)
        new_lon = _coordinate(lon, latitude=False)
        new_rssi = _number(rssi)

        if old is None:
            stored = _StoredObservation(
                canonical, normalized_identity, new_lat, new_lon,
                new_rssi, new_rssi, current, supplied, 0)
            self._items[key] = stored
            self._revision += 1
            if len(self._items) > self.capacity:
                self._items.popitem(last=False)
            return self._snapshot(stored, MODE_RECENT, current)

        previous_actual_stage = fade_stage(old.last_seen, current, self.lifetime)
        visual_changed = previous_actual_stage != 0
        if new_lat is not None and new_lat != old.lat:
            old.lat = new_lat
            visual_changed = True
        if new_lon is not None and new_lon != old.lon:
            old.lon = new_lon
            visual_changed = True
        if new_rssi is not None:
            old.latest_rssi = new_rssi
            # Once the old sighting has expired, this is a new recent window;
            # a long-gone strong signal must not keep a newly weak dot bright.
            strongest = (
                new_rssi
                if old.rssi is None or previous_actual_stage is None
                else max(old.rssi, new_rssi)
            )
            if strongest != old.rssi:
                old.rssi = strongest
                visual_changed = True
        if supplied:
            updated = dict(old.metadata)
            updated.update(supplied)
            if updated != old.metadata:
                old.metadata = updated
                visual_changed = True
        old.last_seen = current
        old.stage = 0
        self._items.move_to_end(key)
        if visual_changed:
            self._revision += 1
        return self._snapshot(old, MODE_RECENT, current)

    def tick(self, now: float | None = None) -> TickResult:
        """Advance discrete fade stages and invalidate a cache only if needed."""
        current = _timestamp(time.monotonic() if now is None else now)
        if current is None:
            raise ValueError("now must be a finite monotonic timestamp")
        changed = expired = 0
        for item in self._items.values():
            stage = fade_stage(item.last_seen, current, self.lifetime)
            if stage != item.stage:
                if stage is None and item.stage is not None:
                    expired += 1
                item.stage = stage
                changed += 1
        if changed:
            self._revision += 1
        return TickResult(changed, expired, self._revision)

    def _snapshot(self, item: _StoredObservation, mode: str,
                  now: float) -> MapObservation:
        stage = 0 if mode == MODE_KEEP else fade_stage(
            item.last_seen, now, self.lifetime)
        # Callers only invoke this for visible records.
        return MapObservation(
            item.layer, item.identity, item.lat, item.lon, item.rssi,
            item.latest_rssi, item.last_seen, dict(item.metadata),
            0 if stage is None else stage)

    def snapshot(self, *, layer: Any = None, mode: Any = MODE_RECENT,
                 now: float | None = None) -> tuple[MapObservation, ...]:
        """Return visible records oldest-to-newest so newest dots draw last."""
        canonical = None
        if layer is not None:
            canonical = normalize_layer_name(layer)
            if canonical not in self._SUPPORTED:
                return ()
        normalized_mode = normalize_mode(mode)
        if normalized_mode == MODE_OFF:
            return ()
        current = _timestamp(time.monotonic() if now is None else now)
        if current is None:
            raise ValueError("now must be a finite monotonic timestamp")
        result = []
        for item in self._items.values():
            if canonical is not None and item.layer != canonical:
                continue
            if normalized_mode == MODE_RECENT and fade_stage(
                    item.last_seen, current, self.lifetime) is None:
                continue
            result.append(self._snapshot(item, normalized_mode, current))
        return tuple(result)

    def snapshots_by_layer(self, modes: Mapping[str, Any] | None = None, *,
                           now: float | None = None
                           ) -> dict[str, tuple[MapObservation, ...]]:
        """Return Wi-Fi and BLE snapshots using their independently set modes."""
        normalized = normalize_layer_modes(modes)
        return {
            layer: self.snapshot(layer=layer, mode=normalized[layer], now=now)
            for layer in (LAYER_WIFI, LAYER_BLE)
        }

    def get(self, layer: Any, identity: Any, *, mode: Any = MODE_KEEP,
            now: float | None = None) -> MapObservation | None:
        """Return one visible observation by identity in O(1)."""
        canonical = normalize_layer_name(layer)
        normalized_identity = self.normalize_identity(identity)
        if canonical not in self._SUPPORTED or normalized_identity is None:
            return None
        normalized_mode = normalize_mode(mode)
        if normalized_mode == MODE_OFF:
            return None
        current = _timestamp(time.monotonic() if now is None else now)
        if current is None:
            raise ValueError("now must be a finite monotonic timestamp")
        item = self._items.get((canonical, normalized_identity))
        if item is None:
            return None
        if (normalized_mode == MODE_RECENT
                and fade_stage(item.last_seen, current, self.lifetime) is None):
            return None
        return self._snapshot(item, normalized_mode, current)

    def remove(self, layer: Any, identity: Any) -> bool:
        canonical = normalize_layer_name(layer)
        normalized_identity = self.normalize_identity(identity)
        if canonical is None or normalized_identity is None:
            return False
        if self._items.pop((canonical, normalized_identity), None) is None:
            return False
        self._revision += 1
        return True

    def clear(self) -> bool:
        if not self._items:
            return False
        self._items.clear()
        self._revision += 1
        return True
