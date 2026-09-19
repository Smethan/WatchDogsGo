"""Persistent wardrive settings shared by startup and the wardrive UI.

The LTE integration flag must be available before :class:`GpsManager` starts,
so settings loading cannot live only in ``WardriveUI``.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping


DEFAULTS = {
    "flock": True,
    "axon": True,
    "precise": True,
    "network_dots": True,
    "trail": False,
    "lte_modem": True,
    "cell_tracking": True,
    "cell_neighbors": False,
    "realert_seconds": 60,
    "suppressed_rules": [],
    "suppressed_devices": [],
}


def settings_path(app_dir: str | Path) -> Path:
    return Path(app_dir) / "wardrive_settings.json"


def load_settings(app_dir: str | Path) -> dict[str, Any]:
    """Load known, correctly typed settings and fill missing defaults.

    Older files do not contain ``lte_modem``; defaulting it to ``True`` keeps
    the existing SIM7600 behavior until the user explicitly disables it.
    Invalid or partially written files are treated like a first launch.
    """
    result = deepcopy(DEFAULTS)
    try:
        saved = json.loads(settings_path(app_dir).read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            return result
        for key, default in DEFAULTS.items():
            value = saved.get(key)
            if type(value) is type(default):
                result[key] = value
    except (OSError, ValueError, TypeError):
        pass
    return result


def save_settings(app_dir: str | Path, settings: Mapping[str, Any]) -> None:
    """Atomically persist known settings."""
    path = settings_path(app_dir)
    value = {
        key: settings.get(key, default)
        if type(settings.get(key, default)) is type(default) else default
        for key, default in DEFAULTS.items()
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
