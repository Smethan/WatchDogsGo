"""Bounded host-side validation and encoding for the C5 jobs v1 protocol."""
from __future__ import annotations

import base64
import json
import re
from typing import Any, Mapping

from .wardrive_protocol import parse_record

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
DEVICE_RE = re.compile(r"[0-9A-Fa-f]{12}\Z")
BOOT_RE = re.compile(r"[0-9A-Fa-f]{16}\Z")
BUILD_ID_RE = re.compile(r"[0-9A-Fa-f]{16}\Z")
FIRMWARE_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}\Z")
MAC_RE = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\Z")
HEX_RE = re.compile(r"(?:[0-9A-Fa-f]{2})*\Z")
OPERATIONS = frozenset((
    "wardrive", "wifi_scan", "ble_scan", "bt_track", "airtag_scan",
    "sniffer", "hs_sniff", "handshake", "deauth", "sae", "blackout",
    "evil_twin", "portal",
))
BOARDS = frozenset(("wroom", "xiao", "waveshare-zero"))
MAX_JOB_JSON = 4096
LOSS_KEYS = ("queue", "age", "output", "saturation")
CHANNELS = frozenset((*range(1, 15), 36, 40, 44, 48, 52, 56, 60, 64,
    100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
    149, 153, 157, 161, 165))
V3_DATA_KINDS = frozenset((
    "started", "stopped", "error", "heartbeat", "status", "batch_start",
    "batch_results", "batch_done", "wifi", "wifi_mgmt", "ble", "live_oui",
    "hs_packet",
    "legacy",
))


def _json_object(value: str, max_bytes: int) -> dict[str, Any]:
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError("record too large")
    try:
        decoded = json.loads(value)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("record must be an object")
    return decoded


def parse_device_info(line: str) -> dict[str, Any] | None:
    """Return validated v3 device metadata, or None for any malformed line."""
    if not isinstance(line, str) or not line.startswith("WDG:"):
        return None
    try:
        d = _json_object(line[4:], 1020)
        if d.get("v") != 3 or type(d.get("v")) is not int or d.get("kind") != "device_info":
            raise ValueError("version/kind")
        device_id, boot_id, board = d.get("device_id"), d.get("boot_id"), d.get("board")
        if not isinstance(device_id, str) or not DEVICE_RE.fullmatch(device_id):
            raise ValueError("device_id")
        if not isinstance(boot_id, str) or not BOOT_RE.fullmatch(boot_id):
            raise ValueError("boot_id")
        if board not in BOARDS:
            raise ValueError("board")
        # These fields were added after the v3 protocol shipped. Keep them
        # optional so earlier firmware remains fully negotiable, but validate
        # every supplied value before exposing it to UI or diagnostics.
        if "firmware_version" in d and (
                not isinstance(d["firmware_version"], str)
                or not FIRMWARE_VERSION_RE.fullmatch(d["firmware_version"])):
            raise ValueError("firmware_version")
        if "build_id" in d and (
                not isinstance(d["build_id"], str)
                or not BUILD_ID_RE.fullmatch(d["build_id"])):
            raise ValueError("build_id")
        if "boot_state" in d and d["boot_state"] not in ("ready", "degraded"):
            raise ValueError("boot_state")
        if "boot_reason" in d and (
                not isinstance(d["boot_reason"], str)
                or len(d["boot_reason"].encode("utf-8")) > 64
                or any(ord(char) < 0x20 or ord(char) > 0x7e
                       for char in d["boot_reason"])):
            raise ValueError("boot_reason")
        if 'legacy_busy' in d and type(d['legacy_busy']) is not bool:
            raise ValueError('legacy_busy')
        for key in ("flash_bytes", "psram_bytes"):
            if type(d.get(key)) is not int or d[key] < 0:
                raise ValueError(key)
        operations = d.get("operations")
        channels = d.get("channels")
        limits = d.get("limits")
        updates = d.get("updates")
        if not isinstance(operations, list) or any(x not in OPERATIONS for x in operations):
            raise ValueError("operations")
        if len(set(operations)) != len(operations):
            raise ValueError("operations")
        if not isinstance(channels, list) or len(channels) > 196 or any(
                type(c) is not int or not 1 <= c <= 196 for c in channels):
            raise ValueError("channels")
        if len(set(channels)) != len(channels):
            raise ValueError("channels")
        if not isinstance(limits, dict):
            raise ValueError("limits")
        for key, maximum in (("targets", 64), ("exclusions", 32), ("batch", 4096),
                             ("aps", 4096), ("clients", 4096), ("probes", 4096)):
            if type(limits.get(key)) is not int or not 0 <= limits[key] <= maximum:
                raise ValueError("limits." + key)
        if not isinstance(updates, list) or not updates or any(
                x not in ("usb-ota", "rom") for x in updates):
            raise ValueError("updates")
        if len(set(updates)) != len(updates):
            raise ValueError("updates")
        operation_limits = d.get('operation_limits', {})
        if not isinstance(operation_limits, dict) or set(operation_limits) - set(operations):
            raise ValueError('operation_limits')
        for operation, values in operation_limits.items():
            if not isinstance(values, dict) or set(values) - {'targets', 'aps', 'clients', 'probes', 'exchanges'}:
                raise ValueError('operation_limits.' + operation)
            if any(type(value) is not int or not 0 <= value <= 4096 for value in values.values()):
                raise ValueError('operation_limits.' + operation)
        if 'memory_limits' in d:
            memory_limits = d['memory_limits']
            if (not isinstance(memory_limits, dict) or any(
                    type(value) is not int or not 0 <= value <= 2**32-1
                    for value in memory_limits.values())):
                raise ValueError('memory_limits')
        d["device_id"] = device_id.upper()
        d["boot_id"] = boot_id.upper()
        return d
    except (ValueError, TypeError, KeyError, RecursionError):
        return None


def limits_for_operation(info: Mapping[str, Any], operation: str) -> dict[str, int]:
    limits = dict(info.get('limits', {}))
    limits.update(info.get('operation_limits', {}).get(operation, {}))
    return limits


def validate_job_request(request: Mapping[str, Any], *, boot_id: str | None = None,
                         limits: Mapping[str, int] | None = None) -> dict[str, Any]:
    """Validate and copy a job request; rejects over-capacity input, never truncates."""
    if not isinstance(request, Mapping):
        raise ValueError("request must be an object")
    try:
        # A JSON round trip provides a bounded, detached tree and rejects custom objects.
        raw = json.dumps(request, separators=(",", ":"), ensure_ascii=True,
                         allow_nan=False).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("request is not JSON encodable") from exc
    if len(raw) > MAX_JOB_JSON:
        raise ValueError("request exceeds 4096 bytes")
    d = json.loads(raw)
    if set(d) - {"boot_id", "op", "band", "channels", "dwell_ms", "ble",
                 "targets", "exclude", "options", "live_ouis"}:
        raise ValueError("unknown request field")
    actual_boot = d.get("boot_id", boot_id)
    if not isinstance(actual_boot, str) or not BOOT_RE.fullmatch(actual_boot):
        raise ValueError("boot_id")
    if boot_id is not None and actual_boot.upper() != boot_id.upper():
        raise ValueError("stale boot_id")
    d["boot_id"] = actual_boot.upper()
    if d.get("op") not in OPERATIONS:
        raise ValueError("op")
    if d.get("band") not in ("wifi24", "wifi5", "both", "none"):
        raise ValueError("band")
    channels = d.get("channels", [])
    if not isinstance(channels, list) or len(channels) > 40 or any(
            type(c) is not int or c not in CHANNELS for c in channels):
        raise ValueError("channels")
    if len(set(channels)) != len(channels):
        raise ValueError("duplicate channel")
    if type(d.get("dwell_ms", 160)) is not int or not 40 <= d.get("dwell_ms", 160) <= 2000:
        raise ValueError("dwell_ms")
    d.setdefault("dwell_ms", 160)
    if d['band'] == 'none' and channels or d['band'] != 'none' and not channels:
        raise ValueError("band/channel plan mismatch")
    if any((c <= 14 and d['band'] == 'wifi5') or (c > 14 and d['band'] == 'wifi24') for c in channels):
        raise ValueError("channel outside assigned band")
    if d.get("ble", "off") not in ("scan", "off"):
        raise ValueError("ble")
    d.setdefault("ble", "off")
    if d['op'] in ('ble_scan', 'bt_track', 'airtag_scan'):
        if d['band'] != 'none' or d['ble'] != 'scan':
            raise ValueError('BLE operation requires a dedicated BLE assignment')
    elif d['band'] == 'none':
        raise ValueError('WiFi operation needs a channel plan')
    if d['op'] != 'wardrive' and d['op'] not in ('ble_scan', 'bt_track', 'airtag_scan') and d['ble'] != 'off':
        raise ValueError('BLE is supported only in a wardrive or BLE job')
    targets = d.get("targets", [])
    max_targets = min(16, (limits or {}).get("targets", 16))
    if not isinstance(targets, list) or len(targets) > max_targets:
        raise ValueError("targets capacity")
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("target")
        if not isinstance(target.get("bssid"), str) or not MAC_RE.fullmatch(target["bssid"]):
            raise ValueError("target bssid")
        if type(target.get("channel")) is not int or not 1 <= target["channel"] <= 196:
            raise ValueError("target channel")
        if type(target.get("auth")) is not int or not 0 <= target["auth"] <= 255:
            raise ValueError("target auth")
        ssid_hex = target.get("ssid_hex", "")
        if not isinstance(ssid_hex, str) or len(ssid_hex) > 64 or not HEX_RE.fullmatch(ssid_hex):
            raise ValueError("target ssid_hex")
        target["bssid"] = target["bssid"].upper()
        if int(target['bssid'][:2], 16) & 1 or target['bssid'] == '00:00:00:00:00:00':
            raise ValueError("target bssid must be unicast")
        if target['channel'] not in channels:
            raise ValueError("target channel outside assignment")
        target.setdefault("ssid_hex", "")
    if len({target['bssid'] for target in targets}) != len(targets):
        raise ValueError("duplicate target")
    d["targets"] = targets
    exclusions = d.get("exclude", [])
    max_exclusions = (limits or {}).get("exclusions", 32)
    if not isinstance(exclusions, list) or len(exclusions) > max_exclusions or any(
            not isinstance(mac, str) or not MAC_RE.fullmatch(mac) for mac in exclusions):
        raise ValueError("exclude")
    d["exclude"] = [mac.upper() for mac in exclusions]
    if len(set(d['exclude'])) != len(d['exclude']):
        raise ValueError("duplicate exclusion")
    if any(int(mac[:2], 16) & 1 or mac == '00:00:00:00:00:00' for mac in d['exclude']):
        raise ValueError("exclusion must be unicast")
    if {t['bssid'] for t in targets} & set(d['exclude']):
        raise ValueError("target is protected by exclusion")
    options = d.get("options", {})
    if not isinstance(options, dict) or options.get("storage", "serial") not in ("serial", "sd") \
            or options.get("scope", "selected") not in ("selected", "all"):
        raise ValueError("options")
    options.setdefault("storage", "serial")
    options.setdefault("scope", "selected")
    if set(options) - {'storage', 'scope', 'ssid_hex', 'mac'}:
        raise ValueError("unknown operation option")
    if 'ssid_hex' in options and (not isinstance(options['ssid_hex'], str)
            or not HEX_RE.fullmatch(options['ssid_hex']) or len(options['ssid_hex']) > 64):
        raise ValueError('option ssid_hex')
    if 'mac' in options and (not isinstance(options['mac'], str) or not MAC_RE.fullmatch(options['mac'])):
        raise ValueError('option mac')
    d["options"] = options
    live_ouis = d.get("live_ouis", [])
    if not isinstance(live_ouis, list) or len(live_ouis) > 8 or any(
            not isinstance(oui, str) or not re.fullmatch(r"[0-9A-Fa-f]{6}", oui)
            for oui in live_ouis):
        raise ValueError("live_ouis")
    d["live_ouis"] = [oui.upper() for oui in live_ouis]
    if len(set(d['live_ouis'])) != len(d['live_ouis']):
        raise ValueError("duplicate live OUI")
    if d['live_ouis'] and d['op'] not in ('wardrive', 'wifi_scan'):
        raise ValueError('live OUI hints require a WiFi observation job')
    if len(json.dumps(d, separators=(',', ':'), ensure_ascii=True).encode('ascii')) > MAX_JOB_JSON:
        raise ValueError("normalized request exceeds 4096 bytes")
    return d


def encode_job_start(token: str, request: Mapping[str, Any], *, boot_id: str | None = None,
                     limits: Mapping[str, int] | None = None) -> str:
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise ValueError("token")
    normalized = validate_job_request(request, boot_id=boot_id, limits=limits)
    # Firmware through v1.8.1 compares this nonce to its lowercase boot value
    # exactly; v1.8.2 accepts either hex case. Emit lowercase for compatibility
    # with both while keeping validated host identities uppercase. This changes
    # only the detached wire copy; stale-boot checks remain case-insensitive.
    normalized["boot_id"] = normalized["boot_id"].lower()
    encoded = base64.urlsafe_b64encode(json.dumps(
        normalized, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).decode("ascii").rstrip("=")
    return f"job_start {token} {encoded}"


def parse_job_record(line: str, *, expected_device_id: str | None = None,
                     expected_boot_id: str | None = None, last_seq: int = 0) -> dict[str, Any] | None:
    """Validate a v3 job record and its v1/v2 payload using the established parser."""
    if not isinstance(line, str) or not line.startswith("WDG:"):
        return None
    try:
        d = _json_object(line[4:], 1020)
        if type(d.get("v")) is not int or d["v"] != 3 or d.get("kind") not in V3_DATA_KINDS:
            raise ValueError("version/kind")
        device_id, boot_id = d.get("device_id"), d.get("boot_id")
        if not isinstance(device_id, str) or not DEVICE_RE.fullmatch(device_id):
            raise ValueError("device_id")
        if not isinstance(boot_id, str) or not BOOT_RE.fullmatch(boot_id):
            raise ValueError("boot_id")
        if expected_device_id and device_id.upper() != expected_device_id.upper():
            raise ValueError("wrong device")
        if expected_boot_id and boot_id.upper() != expected_boot_id.upper():
            raise ValueError("wrong boot")
        if not isinstance(d.get("session"), str) or not TOKEN_RE.fullmatch(d["session"]):
            raise ValueError("session")
        seq = d.get("seq")
        if type(seq) is not int or not 1 <= seq <= 2**32-1 or seq <= last_seq:
            raise ValueError("seq")
        kind = d["kind"]
        if kind in ('status', 'started', 'heartbeat') and 'channel' in d:
            if type(d['channel']) is not int or d['channel'] not in CHANNELS:
                raise ValueError('applied channel')
        for key in ("sniffer_packets", "sniffer_aps", "sniffer_drops"):
            if key in d and (type(d[key]) is not int or not 0 <= d[key] <= 2**32-1):
                raise ValueError(key)
        if "requested_channel" in d and (
                type(d["requested_channel"]) is not int
                or d["requested_channel"] not in CHANNELS):
            raise ValueError("requested_channel")
        if "actual_channel" in d and (
                type(d["actual_channel"]) is not int
                or d["actual_channel"] != 0 and d["actual_channel"] not in CHANNELS):
            raise ValueError("actual_channel")
        if "radio_mode" in d and d["radio_mode"] not in ("null", "sta", "ap", "sta_ap"):
            raise ValueError("radio_mode")
        if "band" in d and d["band"] not in ("2g", "5g", "auto", "unknown"):
            raise ValueError("band")
        if "sdk_error" in d and (
                type(d["sdk_error"]) is not int
                or not -(2**31) <= d["sdk_error"] <= 2**31-1):
            raise ValueError("sdk_error")
        for key in ("start_reason", "failure_reason"):
            if key in d and (not isinstance(d[key], str)
                             or not 1 <= len(d[key]) <= 40
                             or re.fullmatch(r"[a-z0-9_]+", d[key]) is None):
                raise ValueError(key)
        if kind == 'legacy':
            line = d.get('line')
            if not isinstance(line,str) or len(line.encode('utf-8')) > 750 or '\n' in line or '\r' in line:
                raise ValueError('legacy line')
            d['device_id'], d['boot_id'] = device_id.upper(), boot_id.upper()
            return d
        if 'losses' in d:
            losses = d['losses']
            if (not isinstance(losses, dict) or set(losses) != set(LOSS_KEYS)
                    or any(type(value) is not int or not 0 <= value <= 2**32-1
                           for value in losses.values())):
                raise ValueError('losses')
        original_state = d.get("state")
        legacy = dict(d)
        legacy["v"] = 1 if kind == "hs_packet" else 2
        legacy.pop("device_id", None)
        legacy.pop("boot_id", None)
        if legacy["v"] == 2:
            legacy.setdefault('batch', 0)
            # V2's frozen parser predates the v3 stopping state. Validate all other
            # fields through it, while preserving stopping in the returned record.
            if legacy.get("state") in ("starting", "stopping"):
                legacy["state"] = "running"
        line2 = "WDG:" + json.dumps(legacy, separators=(",", ":"))
        validated = parse_record(line2)
        if validated is None:
            raise ValueError("payload")
        # Return original v3 envelope with canonicalized fields from the validator.
        d.update(validated)
        d["v"] = 3
        d["kind"] = kind
        d["device_id"] = device_id.upper()
        d["boot_id"] = boot_id.upper()
        if original_state is not None:
            d["state"] = original_state
        return d
    except (ValueError, TypeError, KeyError, RecursionError):
        return None
