"""Persistent wardrive settings shared by startup and the wardrive UI.

The LTE integration flag must be available before :class:`GpsManager` starts,
so settings loading cannot live only in ``WardriveUI``.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from .p25_catalog import BUILTIN_SITES, find_builtin_site_for_frequency
from .map_display import (
    DEFAULT_LAYER_MODES,
    MAP_LAYERS,
    MODE_KEEP,
    MODE_OFF,
    normalize_mode,
)


DOT_FADE_CHOICES = (15, 30, 60, 120)
TRAIL_MODES = ("off", "solid", "heat")
LORA_PROTOCOLS = ("meshcore", "meshtastic", "reticulum")
MESHTASTIC_BACKENDS = ("fork_socket",)

_BLUETOOTH_MAC = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


DEFAULTS = {
    "flock": True,
    "axon": True,
    # Pig Detector is started explicitly from SNIFF.  These settings alone
    # never claim radios or enable SDR power during an upgrade.
    "pig_product_alerts": True,
    "pig_estimates": True,
    "pig_sdr_profile": "auto",
    "pig_watchlist": [],
    "pig_p25_control_hz": 0,
    "pig_p25_system": "",
    "p25_custom_sites": [],
    "p25_selected_site_id": "",
    "pig_gain_db": 0,
    "pig_ppm": 0,
    "pig_agency_icao": [],
    "pig_agency_rid": [],
    "pig_talkgroups": {},
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
    "meshtastic_backend": "fork_socket",
    "meshtastic_phone_ble_enabled": True,
    "meshtastic_phone_adapter": "auto",
    # Opt-in: upgrades must not unexpectedly expose a new BLE peripheral.
    "meshcore_ble_enabled": False,
    "meshcore_ble_adapter": "auto",
    # BlueZ retains the cryptographic bond. WDG stores only the one permitted
    # MeshMapper device identity so unrelated paired devices stay isolated.
    "meshcore_ble_paired_address": "",
    "meshcore_ble_paired_name": "",
    "host_ble_adapter": "auto",
    "c5_preset": "split-wifi-ble",
    "c5_devices": {},
    "realert_seconds": 60,
    "suppressed_rules": [],
    "suppressed_devices": [],
    **{f"dot_{layer}_mode": mode
       for layer, mode in DEFAULT_LAYER_MODES.items()},
}


def settings_path(app_dir: str | Path) -> Path:
    return Path(app_dir) / "wardrive_settings.json"


def legacy_meshcore_bond(
        settings: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return the normalized one-release MeshCore bond mirror."""
    address = str(settings.get(
        "meshcore_ble_paired_address", "") or "").strip()
    if not _BLUETOOTH_MAC.fullmatch(address):
        return None
    name = str(settings.get("meshcore_ble_paired_name", "") or "").strip()
    raw = name.encode("utf-8")[:80]
    return address.upper(), raw.decode("utf-8", errors="ignore")


def mirror_meshcore_bond_settings(
        settings: MutableMapping[str, Any], bond=None) -> None:
    """Mirror a bond record into legacy settings for one release.

    Passing ``None`` clears the compatibility fields. Cryptographic material
    and transient passkeys are never represented in wardrive settings.
    """
    if bond is None:
        address = ""
        name = ""
    else:
        address = str(getattr(bond, "phone_address", "") or "")
        name = str(getattr(bond, "phone_name", "") or "")
    settings["meshcore_ble_paired_address"] = address
    settings["meshcore_ble_paired_name"] = name


def migrate_legacy_meshcore_bond(
        settings: MutableMapping[str, Any], store, controller: str,
        verifier):
    """Verifier-gated migration into the shared controller bond store."""
    legacy = legacy_meshcore_bond(settings)
    if legacy is None:
        return store.get(controller)
    address, name = legacy
    record = store.migrate_legacy(
        controller, address, name, verifier, source="meshcore")
    if record is not None:
        # Keep these compatibility fields synchronized for this release.
        mirror_meshcore_bond_settings(settings, record)
    return record


def normalize_settings(saved: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate settings and migrate the old all-dots/trail booleans."""
    result = deepcopy(DEFAULTS)
    if not isinstance(saved, Mapping):
        return result
    for key, default in DEFAULTS.items():
        value = saved.get(key)
        if type(value) is type(default):
            result[key] = deepcopy(value)
    if result["pig_sdr_profile"] not in (
            "auto", "off", "watchlist", "p25", "433", "adsb"):
        result["pig_sdr_profile"] = "auto"
    channels = []
    for raw in result["pig_watchlist"][:16]:
        if not isinstance(raw, dict):
            continue
        hz, width, label = (raw.get("frequency_hz"),
                            raw.get("bandwidth_hz"), raw.get("label"))
        if (type(hz) is int and 24_000_000 <= hz <= 1_740_000_000
                and type(width) is int and 2_000 <= width <= 200_000
                and isinstance(label, str) and 1 <= len(label.strip()) <= 32):
            channels.append({"frequency_hz": hz, "bandwidth_hz": width,
                             "label": label.strip()})
    result["pig_watchlist"] = channels
    hz = result["pig_p25_control_hz"]
    result["pig_p25_control_hz"] = (
        hz if type(hz) is int and 24_000_000 <= hz <= 1_740_000_000 else 0)
    # User sites are deliberately small and bounded because this settings file
    # is copied and normalized on every load/save.
    custom_sites = []
    seen_site_ids = set()
    raw_custom = result.get("p25_custom_sites", [])
    if isinstance(raw_custom, list):
        for index, raw in enumerate(raw_custom[:64]):
            if not isinstance(raw, Mapping):
                continue
            fields = {}
            valid = True
            for key, limit in (("region", 32), ("county", 40), ("site", 48)):
                value = raw.get(key)
                if not isinstance(value, str):
                    valid = False
                    break
                cleaned = "".join(ch for ch in value.strip() if ch.isprintable())[
                    :limit
                ]
                if not cleaned:
                    valid = False
                    break
                fields[key] = cleaned
            if not valid:
                continue
            system = raw.get("system", "")
            if not isinstance(system, str):
                system = ""
            system = "".join(ch for ch in system.strip() if ch.isprintable())[:48]
            controls = []
            raw_controls = raw.get("control_hz", raw.get("frequencies_hz", ()))
            if isinstance(raw_controls, (list, tuple)):
                for frequency in raw_controls:
                    if (
                        type(frequency) is int
                        and 24_000_000 <= frequency <= 1_740_000_000
                        and frequency not in controls
                    ):
                        controls.append(frequency)
                    if len(controls) >= 16:
                        break
            if not controls:
                continue
            site_id = raw.get("id")
            if not isinstance(site_id, str) or not site_id.startswith("custom:"):
                site_id = f"custom:user-{index + 1}"
            site_id = site_id[:64]
            if site_id in seen_site_ids:
                continue
            seen_site_ids.add(site_id)
            normalized_site = {
                "id": site_id,
                **fields,
                "system": system,
                "control_hz": controls,
            }
            for key in ("sysid", "wacn"):
                value = raw.get(key)
                if isinstance(value, str) and re.fullmatch(
                    r"(?:0x)?[0-9A-Fa-f]{1,8}", value.strip()
                ):
                    digits = value.strip().removeprefix("0x").removeprefix("0X")
                    normalized_site[key] = "0x" + digits.upper()
            custom_sites.append(normalized_site)
    result["p25_custom_sites"] = custom_sites

    selected_site_id = result.get("p25_selected_site_id", "")
    if not isinstance(selected_site_id, str):
        selected_site_id = ""
    available_sites = [*BUILTIN_SITES, *custom_sites]
    if selected_site_id not in {site["id"] for site in available_sites}:
        selected_site_id = ""
    if selected_site_id and result["pig_p25_control_hz"]:
        selected_site = next(
            site for site in available_sites if site["id"] == selected_site_id
        )
        if result["pig_p25_control_hz"] not in selected_site["control_hz"]:
            selected_site_id = ""
    if not selected_site_id and result["pig_p25_control_hz"]:
        saved_hz = result["pig_p25_control_hz"]
        matched = find_builtin_site_for_frequency(saved_hz)
        if matched is None:
            matched = next(
                (site for site in custom_sites if saved_hz in site["control_hz"]), None
            )
        if matched is not None:
            selected_site_id = matched["id"]
        else:
            # Keep the pre-catalog user frequency visible as a custom entry.
            label = result["pig_p25_system"] or f"Saved {saved_hz / 1e6:.4f} MHz"
            legacy = {
                "id": "custom:legacy-p25",
                "region": "Custom",
                "county": "Saved channels",
                "site": label[:48],
                "system": result["pig_p25_system"],
                "control_hz": [saved_hz],
            }
            existing = next(
                (site for site in custom_sites if site["id"] == legacy["id"]), None
            )
            if existing is None and len(custom_sites) < 64:
                custom_sites.append(legacy)
                result["p25_custom_sites"] = custom_sites
            elif existing is not None and saved_hz not in existing["control_hz"]:
                existing["control_hz"].append(saved_hz)
            selected_site_id = legacy["id"]
    result["p25_selected_site_id"] = selected_site_id
    gain = result["pig_gain_db"]
    result["pig_gain_db"] = gain if type(gain) is int and 0 <= gain <= 50 else 0
    ppm = result["pig_ppm"]
    result["pig_ppm"] = ppm if type(ppm) is int and -200 <= ppm <= 200 else 0
    result["pig_p25_system"] = result["pig_p25_system"][:32].strip()
    for key, limit in (("pig_agency_icao", 6), ("pig_agency_rid", 32)):
        result[key] = list(dict.fromkeys(
            value.upper().strip() for value in result[key][:64]
            if isinstance(value, str) and 1 <= len(value.strip()) <= limit))
    result["pig_talkgroups"] = {
        str(key)[:40]: value.strip()[:40]
        for key, value in list(result["pig_talkgroups"].items())[:128]
        if isinstance(key, str) and isinstance(value, str) and value.strip()}

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
    if result['c5_preset'] not in ('split-wifi-ble', 'wifi-dedicated-ble'):
        result['c5_preset'] = DEFAULTS['c5_preset']
    devices = {}
    for identity, preferences in result['c5_devices'].items():
        if (not isinstance(identity,str) or not re.fullmatch(r'[0-9A-Fa-f]{12}',identity)
                or not isinstance(preferences,dict)):
            continue
        alias = preferences.get('alias','')
        role = preferences.get('role','auto')
        if isinstance(alias,str) and len(alias) <= 32 and role in ('auto','wifi24-ble','wifi5','ble','wifi'):
            devices[identity.upper()] = {'alias':alias, 'role':role}
    result['c5_devices'] = devices
    # Stock-versus-WDG selection was removed with unified installation.  Keep
    # the serialized key for one migration release, but force the only
    # supported restricted local API.
    result["meshtastic_backend"] = "fork_socket"
    for key in ("meshtastic_phone_adapter", "meshcore_ble_adapter",
                "host_ble_adapter"):
        value = result[key].strip() if isinstance(result[key], str) else "auto"
        result[key] = (
            "auto" if value.lower() == "auto" else value.upper()
            if _BLUETOOTH_MAC.fullmatch(value) else "auto"
        )
    paired = result["meshcore_ble_paired_address"].strip()
    result["meshcore_ble_paired_address"] = (
        paired.upper() if _BLUETOOTH_MAC.fullmatch(paired) else "")
    name = result["meshcore_ble_paired_name"].strip()
    encoded = name.encode("utf-8")[:80]
    result["meshcore_ble_paired_name"] = encoded.decode(
        "utf-8", errors="ignore")
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
