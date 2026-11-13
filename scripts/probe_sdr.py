#!/usr/bin/env python3
"""Run a bounded WatchDogsGo SDRManager probe without opening the Pyxel UI.

Run from the WatchDogsGo checkout, normally with sudo so SDRManager can lease
and restore the systemd readsb service:

    sudo -E .venv/bin/python3 scripts/probe_sdr.py --mode adsb --seconds 900
    sudo -E .venv/bin/python3 scripts/probe_sdr.py --mode p25 --seconds 900

This script uses receive-only ADS-B and P25 metadata profiles. It never changes
an AIO rail or edits WatchDogsGo settings.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any


SDR_PROCESS_NAMES = {
    "dump1090",
    "dump1090-fa",
    "dump1090-mutability",
    "readsb",
    "rtl_433",
    "rtl_power",
}


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def emit(event: str, **fields: Any) -> None:
    record = {"time": timestamp(), "event": event, **fields}
    print(json.dumps(record, sort_keys=True, default=str), flush=True)


def proc_entries() -> list[tuple[int, list[str], str]]:
    entries = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        pid = int(path.name)
        try:
            raw = (path / "cmdline").read_bytes()
            tokens = [
                part.decode("utf-8", "replace") for part in raw.split(b"\0") if part
            ]
            cgroup = (path / "cgroup").read_text(errors="replace")
        except (OSError, PermissionError):
            continue
        entries.append((pid, tokens, cgroup))
    return entries


def assert_no_conflicting_processes() -> None:
    own_pid = os.getpid()
    conflicts = []
    for pid, tokens, cgroup in proc_entries():
        if pid == own_pid or not tokens:
            continue
        executable = os.path.basename(tokens[0])
        joined = " ".join(tokens)
        if (
            "-m" in tokens
            and any(
                tokens[index + 1] == "watchdogs"
                for index, token in enumerate(tokens[:-1])
                if token == "-m"
            )
        ) or any(token.endswith("/watchdogs/__main__.py") for token in tokens):
            conflicts.append((pid, "WatchDogsGo UI", joined))
        elif executable in SDR_PROCESS_NAMES or any(
            os.path.basename(token) == "rx.py" for token in tokens
        ):
            # The systemd readsb service is the expected SBS source for ADS-B
            # and is safely handled by SDRManager's normal lease path for P25.
            # An unmanaged tuner process cannot be safely handed off.
            if executable == "readsb" and "readsb.service" in cgroup:
                continue
            conflicts.append((pid, executable, joined))
    if conflicts:
        emit("preflight_refused", conflicts=conflicts)
        raise RuntimeError("WDG UI or another SDR tuner process is active")


def assert_rtl_sdr_present() -> None:
    lsusb = shutil.which("lsusb")
    if not lsusb:
        raise RuntimeError("lsusb is unavailable; cannot verify RTL-SDR enumeration")
    result = subprocess.run(
        [lsusb],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("lsusb could not enumerate USB devices")
    if not any("0bda:2838" in line.lower() for line in result.stdout.splitlines()):
        emit("preflight_refused", reason="RTL2838 (0bda:2838) not present on USB")
        raise RuntimeError("RTL-SDR is not present on USB")


def read_sdr_rail() -> bool:
    from watchdogs.aio_manager import AioManager

    status = AioManager.get_status(("sdr",))
    if not isinstance(status, dict) or type(status.get("sdr")) is not bool:
        raise RuntimeError("SDR rail state could not be read")
    emit("sdr_rail", state="ON" if status["sdr"] else "OFF")
    if not status["sdr"]:
        raise RuntimeError("SDR rail is OFF; enable it before running this probe")
    return status["sdr"]


def get_readsb_status(manager) -> tuple[str, str]:
    try:
        return manager._readsb_status()
    except Exception as exc:
        return "unknown", str(exc)


def assert_no_pending_readsb_transition(manager) -> tuple[str, str]:
    state, job = manager._readsb_status()
    pending = manager._readsb_has_pending_job(job)
    emit("readsb_before", state=state, pending_job=job)
    if state in ("activating", "deactivating") or pending:
        raise RuntimeError("readsb is transitioning; retry after it settles")
    if state == "unknown":
        raise RuntimeError("readsb state is unknown")
    return state, job


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("adsb", "p25"), required=True)
    parser.add_argument("--seconds", type=int, default=900)
    parser.add_argument("--p25-hz", type=int, default=856_212_500)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="check process, rail, USB, and readsb state without starting a profile",
    )
    args = parser.parse_args()
    if not 30 <= args.seconds <= 900:
        parser.error("--seconds must be between 30 and 900")
    if not 400_000_000 <= args.p25_hz <= 1_300_000_000:
        parser.error("--p25-hz must be a valid RTL-SDR frequency in Hz")
    return args


def main() -> int:
    args = parse_args()
    project_root = Path(__file__).resolve().parents[1]
    if not (project_root / "watchdogs" / "sdr_manager.py").is_file():
        raise RuntimeError("probe must be run from a WatchDogsGo source checkout")
    sys.path.insert(0, str(project_root))
    started = False
    manager = None
    initial_readsb: tuple[str, str] | None = None
    started_at = time.monotonic()
    errors: list[str] = []
    event_counts: dict[str, int] = {}
    stopped_early = False
    exit_code = 0

    try:
        if os.geteuid() != 0:
            raise RuntimeError(
                "run with sudo so SDRManager can stop and restore readsb.service"
            )
        assert_no_conflicting_processes()
        assert_rtl_sdr_present()
        read_sdr_rail()

        from watchdogs.sdr_manager import SDRManager

        manager = SDRManager()
        initial_readsb = assert_no_pending_readsb_transition(manager)
        if args.preflight_only:
            emit("preflight_ok", mode=args.mode, readsb_state=initial_readsb[0])
        else:
            if args.mode == "adsb":
                started = manager.start_adsb()
            else:
                started = manager.start_pig_p25(
                    args.p25_hz,
                    "WDG SDR diagnostic",
                    control_channels=(args.p25_hz,),
                )
            if not started:
                for event_type, data in manager.poll_events():
                    if event_type == "error":
                        errors.append(str(data))
                        emit("profile_error", detail=str(data))
                    else:
                        emit("profile_event", kind=event_type, detail=data)
                emit("profile_start_failed", mode=args.mode, details=errors)
                exit_code = 2
            else:
                emit(
                    "profile_started",
                    mode=args.mode,
                    source=(
                        getattr(manager, "_adsb_source", "")
                        if args.mode == "adsb"
                        else "OP25"
                    ),
                    seconds=args.seconds,
                    p25_hz=args.p25_hz if args.mode == "p25" else None,
                    readsb_state=initial_readsb[0],
                )

                deadline = time.monotonic() + args.seconds
                while time.monotonic() < deadline:
                    for event_type, data in manager.poll_events():
                        event_counts[event_type] = event_counts.get(event_type, 0) + 1
                        if event_type == "error":
                            detail = str(data)
                            errors.append(detail)
                            emit("profile_error", detail=detail)
                        elif event_type == "status":
                            emit("profile_status", detail=str(data))
                    if not manager.running or manager.mode != (
                        "adsb" if args.mode == "adsb" else "pig_p25"
                    ):
                        stopped_early = True
                        emit(
                            "profile_stopped_early",
                            running=manager.running,
                            mode=manager.mode,
                        )
                        break
                    time.sleep(1.0)

                elapsed = round(time.monotonic() - started_at, 1)
                quality = dict(getattr(manager, "p25_quality", {}) or {})
                summary = {
                    "mode": args.mode,
                    "elapsed_seconds": elapsed,
                    "running_at_end": bool(manager.running),
                    "mode_at_end": manager.mode,
                    "aircraft_total": getattr(manager, "total_aircraft_seen", 0),
                    "aircraft_current": len(getattr(manager, "aircraft", {})),
                    "decoded_blocks": quality.get("decoded_blocks"),
                    "p25_tuned_frequency_hz": quality.get("tuned_frequency_hz"),
                    "event_counts": event_counts,
                    "errors": errors,
                    "stopped_early": stopped_early,
                }
                emit("profile_summary", **summary)
                if stopped_early or errors:
                    exit_code = 3
    except KeyboardInterrupt:
        emit("interrupted")
        exit_code = 130
    except Exception as exc:
        emit("probe_failed", error=f"{type(exc).__name__}: {exc}")
        exit_code = 2
    finally:
        if manager is not None:
            try:
                manager.stop()
                emit("manager_stopped", readsb_paused=bool(manager._readsb_paused))
            except Exception as exc:
                emit("manager_stop_error", error=f"{type(exc).__name__}: {exc}")
            if initial_readsb is not None:
                original_state = initial_readsb[0]
                restore_deadline = time.monotonic() + 110.0
                observed = "unknown"
                pending = ""
                while time.monotonic() < restore_deadline:
                    observed, pending = get_readsb_status(manager)
                    if original_state == "active":
                        if observed == "active" and not manager._readsb_has_pending_job(
                            pending
                        ):
                            break
                    elif original_state in ("inactive", "failed"):
                        if (
                            observed == original_state
                            and not manager._readsb_has_pending_job(pending)
                        ):
                            break
                    elif original_state in ("missing", "unsupported"):
                        if observed == original_state:
                            break
                    time.sleep(1.0)
                restored = (
                    observed == original_state
                    and not manager._readsb_has_pending_job(pending)
                )
                emit(
                    "readsb_after",
                    original_state=original_state,
                    observed_state=observed,
                    pending_job=pending,
                    restored=restored,
                    restore_timed_out=not restored,
                )
                if not restored:
                    exit_code = max(exit_code, 4)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
