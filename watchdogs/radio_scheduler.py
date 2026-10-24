"""Deterministic multi-radio allocation with explicit coverage and restoration."""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from .c5_jobs import validate_job_request, limits_for_operation

CHANNEL_COVERAGE_LABELS = {
    "fast": "Fast 2.4 GHz (channels 1/6/11)",
    "full": "Full supported 2.4 GHz (channels 1-14)",
}


class CapacityError(ValueError):
    """Raised when the requested targets/channels cannot be fully allocated."""


def _field(device: Any, name: str, default: Any = None) -> Any:
    if isinstance(device, Mapping):
        return device.get(name, default)
    return getattr(device, name, default)


def _info(device: Any) -> dict[str, Any]:
    info = _field(device, "device_info", {}) or {}
    if not info and isinstance(device, Mapping) and "device_id" in device:
        info = device
    return dict(info)


def _identity(device: Any) -> tuple[str, str, list[int], dict[str, int], set[str]]:
    info = _info(device)
    device_id = str(_field(device, "device_id", info.get("device_id", ""))).upper()
    boot_id = str(_field(device, "boot_id", info.get("boot_id", ""))).upper()
    channels = list(info.get("channels", _field(device, "channels", [])) or [])
    limits = dict(info.get("limits", _field(device, "limits", {})) or {})
    operations = set(info.get("operations", _field(device, "operations", [])) or [])
    if not device_id or not boot_id:
        raise ValueError("each device needs device_id and boot_id")
    return device_id, boot_id, channels, limits, operations


def _supports_handshake_channel_144(device: Any) -> bool:
    version = _info(device).get("firmware_version", _field(device, "firmware_version"))
    if not isinstance(version, str):
        return False
    parts = version.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        return False
    return tuple(int(part) for part in parts) >= (1, 8, 6)


def _without_unsupported_handshake_144(device: Any, channels: list[int],
                                         operation: str) -> list[int]:
    if operation == "handshake" and not _supports_handshake_channel_144(device):
        return [channel for channel in channels if channel != 144]
    return channels


def _target_channel(target: Mapping[str, Any]) -> int:
    channel = target.get("channel")
    if type(channel) is not int or not 1 <= channel <= 196:
        raise ValueError("target channel")
    return channel


class RadioScheduler:
    """Plan background radios, target coverage, helper capture and clean restore.

    Plans are plain dictionaries suitable for ``JobController.start_group``.
    Every request is boot-bound and validated before it is returned. Targets
    are assigned exactly once; capacity failures are explicit exceptions.
    """

    def plan_wardrive(self, devices: Iterable[Any], *, targets: Iterable[Mapping[str, Any]] = (),
                      helper_targets: Iterable[Mapping[str, Any]] = (),
                      helper_device_id: str | None = None,
                      dwell_ms: int = 160) -> dict[str, Any]:
        supplied = list(devices)
        candidates = []
        seen = set()
        for device in supplied:
            device_id, boot_id, channels, limits, operations = _identity(device)
            if device_id in seen:
                raise ValueError("duplicate device_id")
            seen.add(device_id)
            if _field(device, "connected", True) is False:
                continue
            if "wardrive" in operations:
                candidates.append({"source": device, "id": device_id, "boot": boot_id,
                                   "channels": channels, "limits": limits,
                                   "board": _field(device, "board", _info(device).get("board", "")),
                                   "ops": operations})
        if not candidates:
            raise CapacityError("no connected wardrive-capable device")
        xiao = next((d for d in candidates if d["board"] == "xiao"), None)
        zero = next((d for d in candidates if d["board"] == "waveshare-zero"), None)
        preferred = [d for d in (xiao, zero) if d]
        for candidate in candidates:
            if candidate not in preferred and len(preferred) < 2:
                preferred.append(candidate)
        single_device = len(preferred) == 1
        target_list = [dict(target) for target in targets]
        helper_list = [dict(target) for target in helper_targets]
        if helper_list and any("hs_sniff" not in d["ops"] for d in candidates):
            # Capability may be present only on a non-wardrive helper; inspect all
            # supplied devices below before reporting unavailable.
            pass
        if target_list:
            if len(target_list) > sum(d["limits"].get("targets", 64) for d in preferred):
                raise CapacityError("target count exceeds aggregate device capacity")
            assigned = self._assign_targets(target_list, preferred)
        else:
            assigned = {d["id"]: [] for d in preferred}

        requests: dict[str, dict[str, Any]] = {}
        coverage: dict[str, list[int]] = {}
        background_restore: dict[str, dict[str, Any]] = {}
        for item in preferred:
            own_targets = assigned.get(item["id"], [])
            target_channels = sorted({_target_channel(target) for _, target in own_targets})
            if target_channels:
                if len(target_channels) > 40:
                    raise CapacityError(f"channel list exceeds protocol limit for {item['id']}")
                channels = target_channels
            else:
                # Use common 2.4/5 GHz channels, not an arithmetic approximation
                # that accidentally skips 149/153/157/161 and other valid 5 GHz.
                wifi24 = [1, 6, 11]
                wifi5 = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112,
                         116, 120, 124, 128, 132, 136, 140, 144, 149, 153, 157,
                         161, 165]
                if single_device:
                    default_channels = wifi24 + wifi5
                elif item["board"] == "xiao":
                    default_channels = list(range(1, 14))
                elif item["board"] == "waveshare-zero":
                    default_channels = wifi5
                else:
                    default_channels = wifi24 + wifi5
                channels = [channel for channel in default_channels if channel in item["channels"]]
                if not channels:
                    channels = item["channels"]
                if len(channels) > 40:
                    # A static background scan uses a bounded, explicit common
                    # subset spanning both bands rather than silently clipping.
                    channels = [c for c in wifi24 + wifi5 if c in channels][:40]
            band = self._band(channels)
            request = {
                "boot_id": item["boot"], "op": "wardrive", "band": band,
                "channels": channels, "dwell_ms": dwell_ms,
                "ble": "scan" if item["board"] == "xiao" else "off",
                "targets": [target for _index, target in own_targets],
                "exclude": [], "options": {"storage": "serial", "scope": "selected"},
            }
            request = validate_job_request(request, boot_id=item["boot"], limits=item["limits"])
            requests[item["id"]] = request
            coverage[item["id"]] = [index for index, _target in own_targets]
            # This is the exact background plan to restore after a clean helper
            # completion. A caller must not auto-restore after disconnect/error.
            background_restore[item["id"]] = dict(request)

        by_id = {str(_field(d, "device_id", _info(d).get("device_id", ""))).upper(): d
                 for d in supplied}
        helper = None
        helper_mode = "none"
        if helper_list:
            helper_candidates = []
            for d in supplied:
                info = _info(d)
                device_id = str(_field(d, "device_id", info.get("device_id", ""))).upper()
                operations = set(info.get("operations", _field(d, "operations", [])) or [])
                connected = _field(d, "connected", True)
                if connected is not False and "hs_sniff" in operations:
                    helper_candidates.append((device_id, d))
            requested = helper_device_id.upper() if helper_device_id else None
            selected = next(((did, d) for did, d in helper_candidates if did == requested), None)
            if requested and selected is None:
                raise CapacityError(f"requested helper unavailable or lacks hs_sniff: {requested}")
            if selected is None:
                selected = next(((did, d) for did, d in helper_candidates if did not in requests), None)
            if selected and selected[0] not in requests:
                did, device = selected
                info = _info(device)
                chans = sorted({_target_channel(target) for target in helper_list})
                if len(chans) > 40:
                    raise CapacityError("helper channel list exceeds protocol limit")
                boot = str(info.get("boot_id", _field(device, "boot_id", ""))).upper()
                limits = dict(info.get("limits", _field(device, "limits", {})) or {})
                helper = validate_job_request({
                    "boot_id": boot, "op": "hs_sniff", "band": self._band(chans),
                    "channels": chans, "dwell_ms": dwell_ms, "ble": "off",
                    "targets": helper_list, "exclude": [],
                    "options": {"storage": "serial", "scope": "selected"},
                }, boot_id=boot, limits=limits)
                requests[did] = helper
                helper_mode = "dedicated-hs_sniff"
            else:
                if target_list:
                    # Both assigned foreground target jobs stay intact. The
                    # scheduler reports helper unavailability instead of stealing
                    # either radio from explicit target coverage.
                    helper_mode = "unavailable-both-foreground"
                else:
                    # With only background work, one radio can drop BLE duty for
                    # the requested receive focus and restore it after clean end.
                    fallback_id = requested if requested in requests else next(iter(requests))
                    requests[fallback_id]["ble"] = "off"
                    helper_mode = "wardrive-wifi-no-ble"

        assigned_indices = [index for indices in coverage.values() for index in indices]
        if sorted(assigned_indices) != list(range(len(target_list))):
            raise AssertionError("scheduler did not preserve complete target coverage")
        return {
            "requests": requests,
            "coverage": coverage,
            "helper": {"mode": helper_mode, "device_id": next(
                (did for did, req in requests.items() if req.get("op") == "hs_sniff"), None),
                "targets": helper_list},
            "restore_on_clean": background_restore,
            "requires_user_start_after_error": True,
        }

    @staticmethod
    def _band(channels: list[int]) -> str:
        if not channels:
            return "wifi24"
        low, high = any(channel <= 14 for channel in channels), any(channel > 14 for channel in channels)
        return "both" if low and high else "wifi24" if low else "wifi5"

    @staticmethod
    def _assign_targets(targets: list[dict[str, Any]], devices: list[dict[str, Any]]) -> dict[str, list[tuple[int, dict[str, Any]]]]:
        """Boundedly balance small target sets; use explicit first-fit at larger sizes."""
        ordered = sorted(enumerate(targets), key=lambda pair: (
            -sum(1 for dev in devices if _target_channel(pair[1]) in dev["channels"]),
            _target_channel(pair[1]), pair[0],
        ))
        assigned = {device["id"]: [] for device in devices}
        target_cap = {d["id"]: d["limits"].get("targets", 64) for d in devices}

        def can_add(device: dict[str, Any], target: dict[str, Any]) -> bool:
            channels = { _target_channel(t) for _i, t in assigned[device["id"]] }
            channel = _target_channel(target)
            return (channel in device["channels"] and len(assigned[device["id"]]) < target_cap[device["id"]]
                    and (channel in channels or len(channels) < 40))

        visits = 0

        def place(index: int) -> bool:
            nonlocal visits
            visits += 1
            if visits > 10000:
                return False
            if index == len(ordered):
                return True
            target_index, target = ordered[index]
            channel = _target_channel(target)
            options = [d for d in devices if can_add(d, target)]
            options.sort(key=lambda d: (
                0 if (d.get("board") == "xiao" and channel <= 14)
                or (d.get("board") == "waveshare-zero" and channel > 14) else 1,
                len(assigned[d["id"]]), d["id"],
            ))
            for device in options:
                assigned[device["id"]].append((target_index, target))
                if place(index + 1):
                    return True
                assigned[device["id"]].pop()
            return False

        if len(ordered) <= 16:
            placed = place(0)
        else:
            placed = True
            for target_index, target in ordered:
                options = [d for d in devices if can_add(d, target)]
                channel = _target_channel(target)
                options.sort(key=lambda d: (
                    0 if (d.get("board") == "xiao" and channel <= 14)
                    or (d.get("board") == "waveshare-zero" and channel > 14) else 1,
                    len(assigned[d["id"]]), d["id"],
                ))
                if not options:
                    placed = False
                    break
                assigned[options[0]["id"]].append((target_index, target))
        if not placed:
            raise CapacityError("targets cannot be fully covered by available channel/capacity limits")
        return assigned

    def plan(self, devices: Iterable[Any], operation: str, *,
             targets: Iterable[Mapping[str, Any]] = (), channels: Iterable[int] | None = None,
             band: str | None = None, dwell_ms: int = 160, ble: str = "off",
             options: Mapping[str, Any] | None = None,
             device_id: str | None = None,
             channel_coverage: str = "fast") -> dict[str, Any]:
        """Plan any protocol-approved operation without reducing target coverage."""
        if channel_coverage not in CHANNEL_COVERAGE_LABELS:
            raise ValueError("channel_coverage must be 'fast' or 'full'")
        if channel_coverage == "full" and operation != "sniffer":
            raise ValueError("full channel coverage is available for Packet Sniffer only")
        if operation == "wardrive":
            return self.plan_wardrive(devices, targets=targets, dwell_ms=dwell_ms)
        supplied = list(devices)
        eligible = []
        wifi5 = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112,
                 116, 120, 124, 128, 132, 136, 140, 144, 149, 153, 157, 161, 165]
        wifi24 = ([1, 6, 11] if channel_coverage == "fast"
                  else list(range(1, 15)))
        single = False
        seen = set()
        for device in supplied:
            current_id, boot, supported_channels, limits, operations = _identity(device)
            limits = limits_for_operation(_info(device), operation)
            if current_id in seen:
                raise ValueError("duplicate device_id")
            seen.add(current_id)
            if _field(device, "connected", True) is False:
                continue
            if operation not in operations:
                continue
            eligible.append({"id": current_id, "boot": boot,
                             "channels": supported_channels, "limits": limits,
                             "board": str(_field(device, "board", _info(device).get("board", ""))),
                             "roles": set(_field(device, "roles", ()) or ()),
                             "firmware_version": _info(device).get("firmware_version")})
        if not eligible:
            raise CapacityError(f"no connected device supports {operation}")
        if device_id:
            eligible = [d for d in eligible if d["id"] == device_id.upper()]
            if not eligible:
                raise CapacityError(f"requested device unavailable or unsupported: {device_id}")
        singleton_ops = {"ble_scan", "bt_track", "airtag_scan", "portal", "evil_twin"}
        if operation in singleton_ops:
            role_pref = "wifi24" if operation in {"ble_scan", "bt_track", "airtag_scan"} else "foreground"
            eligible.sort(key=lambda d: (0 if role_pref in d["roles"] else 1,
                                         0 if d["board"] == "xiao" else 1, d["id"]))
            eligible = eligible[:1]
        elif not device_id:
            eligible.sort(key=lambda d: (0 if d["board"] == "xiao" else
                                         1 if d["board"] == "waveshare-zero" else 2,
                                         0 if "foreground" in d["roles"] else 1, d["id"]))
        single = len(eligible) == 1
        target_list = [dict(target) for target in targets]
        ble_ops = {"ble_scan", "bt_track", "airtag_scan"}
        if operation in ble_ops and target_list:
            raise ValueError("BLE-only operations do not accept WiFi targets")
        requested_channels = list(channels) if channels is not None else sorted({
            _target_channel(target) for target in target_list})
        if operation == "handshake":
            stale_devices = [device for device in eligible
                             if not _supports_handshake_channel_144(device)]
            for device in stale_devices:
                device["channels"] = [channel for channel in device["channels"]
                                      if channel != 144]
            needs_144 = (144 in requested_channels or
                         any(_target_channel(target) == 144 for target in target_list))
            if needs_144 and not any(144 in device["channels"] for device in eligible):
                raise CapacityError(
                    "Handshake channel 144 requires firmware 1.8.6 or newer; "
                    "update a capable C5 before selecting that network")
        if operation in {"portal", "evil_twin"}:
            if target_list and len(target_list) > eligible[0]["limits"].get("targets", 64):
                raise CapacityError(f"{operation} targets exceed single-device capacity")
            assigned = {eligible[0]["id"]: list(enumerate(target_list))}
        else:
            assigned = self._assign_targets(target_list, eligible) if target_list else {
                device["id"]: [] for device in eligible}
        if operation in ble_ops:
            if channels is not None and requested_channels:
                raise ValueError("BLE-only operations do not accept WiFi channels")
            requested_channels = []
        if not requested_channels and not target_list and operation not in ble_ops:
            if single:
                requested_channels = [c for c in wifi24 + wifi5 if c in eligible[0]["channels"]]
            else:
                # Split 2.4/5 GHz receive work by the paired board roles.
                requested_channels = []
        if len(requested_channels) > 40:
            raise CapacityError("channel list exceeds protocol limit")
        plans: dict[str, dict[str, Any]] = {}
        coverage = {}
        for device in eligible:
            own = assigned[device["id"]]
            if target_list and not own:
                continue
            own_channels = sorted({_target_channel(target) for _, target in own}) or requested_channels
            if operation == "handshake" and 144 in own_channels and 144 not in device["channels"]:
                own_channels = [channel for channel in own_channels if channel != 144]
            if not own_channels and channels is None and not target_list and operation not in ble_ops:
                defaults = wifi24 if device["board"] == "xiao" else wifi5 if device["board"] == "waveshare-zero" else wifi24 + wifi5
                own_channels = [c for c in defaults if c in device["channels"]]
                if (operation == "handshake" and device["board"] == "xiao"
                        and 144 in device["channels"]):
                    own_channels = sorted(set(own_channels + [144]))
            if not own_channels and operation not in ble_ops:
                continue
            if own_channels and any(channel not in device["channels"] for channel in own_channels):
                raise CapacityError(f"requested channel unsupported by {device['id']}")
            request = {
                "boot_id": device["boot"], "op": operation,
                "band": band or ("none" if operation in ble_ops else self._band(own_channels)),
                "channels": own_channels,
                "dwell_ms": dwell_ms, "ble": "scan" if operation in ble_ops else ble,
                "targets": [target for _, target in own], "exclude": [],
                "options": dict(options or {"storage": "serial", "scope": "selected"}),
            }
            plans[device["id"]] = validate_job_request(
                request, boot_id=device["boot"], limits=device["limits"])
            coverage[device["id"]] = [index for index, _target in own]
        indices = [index for members in coverage.values() for index in members]
        if sorted(indices) != list(range(len(target_list))):
            raise AssertionError("scheduler did not preserve complete target coverage")
        return {"requests": plans, "coverage": coverage, "helper": {"mode": "none"},
                "restore_on_clean": {}, "requires_user_start_after_error": True}
