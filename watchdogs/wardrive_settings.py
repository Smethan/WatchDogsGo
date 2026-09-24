"""Persistent wardrive settings shared by startup and the wardrive UI.

The LTE integration flag must be available before :class:`GpsManager` starts,
so settings loading cannot live only in ``WardriveUI``.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from .map_display import (
    DEFAULT_LAYER_MODES,
    MAP_LAYERS,
    MODE_KEEP,
    MODE_OFF,
    normalize_mode,
)


DOT_FADE_CHOICES = (15, 30, 60, 120)
TRAIL_MODES = ("off", "solid", "heat")
LORA_PROTOCOLS = ("meshcore", "meshtastic")


DEFAULTS = {
    "flock": True,
    "axon": True,
    "precise": True,
    "network_dots": True,
    "trail": False,
    "trail_mode": "off",
    "dot_fade_seconds": 30,
    "lte_modem": True,
    "cell_tracking": True,
    "cell_neighbors": False,
    # Automatic host collectors used by All Wardrive. These do not power the
    # hardware; they decide whether WDG may claim it for a collector. Defaults
    # preserve the behavior from before these controls existed.
    "wardrive_lora": True,
    "lora_protocol": "meshcore",
    "wardrive_adsb": True,
    "wardrive_433": False,
    "realert_seconds": 60,
    "suppressed_rules": [],
    "suppressed_devices": [],
    **{f"dot_{layer}_mode": mode
       for layer, mode in DEFAULT_LAYER_MODES.items()},
}


def settings_path(app_dir: str | Path) -> Path:
    return Path(app_dir) / "wardrive_settings.json"


def normalize_settings(saved: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate settings and migrate the old all-dots/trail booleans."""
    result = deepcopy(DEFAULTS)
    if not isinstance(saved, Mapping):
        return result
    for key, default in DEFAULTS.items():
        value = saved.get(key)
        if type(value) is type(default):
            result[key] = deepcopy(value)

    has_legacy_dots = type(saved.get("network_dots")) is bool
    legacy_dots = saved.get("network_dots")
    for layer in MAP_LAYERS:
        key = f"dot_{layer}_mode"
        if key in saved:
            result[key] = normalize_mode(saved.get(key), DEFAULT_LAYER_MODES[layer])
        elif layer in ("wifi", "ble", "cell") and has_legacy_dots:
            # Preserve the old toggle's behavior for existing installations:
            # ON retained bounded ordinary dots until eviction. OFF hid WiFi
            # and BLE, but its separate live cell indicators stayed visible.
            if legacy_dots is True:
                result[key] = MODE_KEEP
            elif layer in ("wifi", "ble"):
                result[key] = MODE_OFF
            else:
                result[key] = DEFAULT_LAYER_MODES[layer]
        else:
            result[key] = DEFAULT_LAYER_MODES[layer]

    seconds = saved.get("dot_fade_seconds", DEFAULTS["dot_fade_seconds"])
    result["dot_fade_seconds"] = (
        seconds if type(seconds) is int and seconds in DOT_FADE_CHOICES
        else DEFAULTS["dot_fade_seconds"])

    if "trail_mode" in saved and saved.get("trail_mode") in TRAIL_MODES:
        result["trail_mode"] = saved["trail_mode"]
    else:
        result["trail_mode"] = "solid" if saved.get("trail") is True else "off"
    result["trail"] = result["trail_mode"] != "off"
    result["network_dots"] = any(
        result[f"dot_{layer}_mode"] != MODE_OFF
        for layer in ("wifi", "ble"))
    # One RTL-SDR cannot run dump1090 and rtl_433 at the same time. A hand-
    # edited invalid file resolves to the historical ADS-B default; the UI
    # toggle records the user's most recent choice explicitly.
    if result["wardrive_adsb"] and result["wardrive_433"]:
        result["wardrive_433"] = False
    if result["lora_protocol"] not in LORA_PROTOCOLS:
        result["lora_protocol"] = DEFAULTS["lora_protocol"]
    return result


def load_settings(app_dir: str | Path) -> dict[str, Any]:
    """Load known, correctly typed settings and fill missing defaults.

    Older files do not contain ``lte_modem``; defaulting it to ``True`` keeps
    the existing SIM7600 behavior until the user explicitly disables it.
    Invalid or partially written files are treated like a first launch.
    """
    try:
        saved = json.loads(settings_path(app_dir).read_text(encoding="utf-8"))
        return normalize_settings(saved)
    except (OSError, ValueError, TypeError):
        return deepcopy(DEFAULTS)


def save_settings(app_dir: str | Path, settings: Mapping[str, Any]) -> None:
    """Atomically persist known settings."""
    path = settings_path(app_dir)
    value = normalize_settings(settings)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
