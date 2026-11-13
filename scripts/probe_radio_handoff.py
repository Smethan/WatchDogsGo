#!/usr/bin/env python3
"""Exercise WDG MeshCore/Meshtastic SX1262 handoffs without Pyxel.

This probe uses the same LoRaManager, MeshtasticManager, and SX1262Controller
entry points as WatchDogsGo. It listens only; it does not enqueue a MeshCore
advert or other transmission. Meshtastic service start/stop/enable/disable
commands are blocked by a read-only systemctl runner.

Run from the WatchDogsGo checkout, for example:
    sudo -E .venv/bin/python3 scripts/probe_radio_handoff.py --cycles 2
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


STEP_TIMEOUT = 20.0
MESHCORE_STOP_TIMEOUT = 5.0  # Preserve LoRaManager.stop()'s production default.
MESHTASTIC_CONNECT_TIMEOUT = 15.0


def stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S%z")


def log(message: str) -> None:
    print(f"{stamp()} {message}", flush=True)


def refuse_if_wdg_ui_running() -> None:
    """Avoid sharing the broker with an interactive WDG process."""
    self_pid = os.getpid()
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == self_pid:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (OSError, PermissionError):
            continue
        if not raw:
            continue
        args = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        joined = " ".join(args)
        is_wdg = ("-m watchdogs" in joined) or any(
            arg.endswith("/watchdogs/__main__.py") for arg in args
        )
        if is_wdg:
            raise RuntimeError(
                f"WDG appears to be running (pid {entry.name}); close it first"
            )


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read settings {path}: {exc}") from exc
    return value if isinstance(value, dict) else {}


def user_home() -> Path:
    sudo_user = os.environ.get("SUDO_USER", "").strip()
    if sudo_user:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


def read_meshcore_region(home: Path) -> str | None:
    """Read the optional MeshCore preset without invoking migration code."""
    config = read_json(home / ".watchdogs_meshcore.json")
    region = config.get("region")
    return region if isinstance(region, str) and region else None


def read_meshtastic_backend(project_root: Path) -> str:
    settings = read_json(project_root / "wardrive_settings.json")
    backend = settings.get("meshtastic_backend", "auto")
    return backend if backend in {"auto", "fork_socket", "legacy_tcp"} else "auto"


def read_only_systemctl(args, **kwargs):
    """Permit service inspection but reject every systemd mutation request."""
    argv = [str(part) for part in args]
    if argv and Path(argv[0]).name == "systemctl":
        operation = argv[1] if len(argv) > 1 else ""
        if operation not in {"show", "is-active", "is-enabled"}:
            raise PermissionError(
                "probe blocked systemd mutation: " + " ".join(argv[:3])
            )
    return subprocess.run(argv, **kwargs)


def compact_status(status: dict[str, Any]) -> str:
    metrics = status.get("metrics")
    lease_owner = ""
    if isinstance(metrics, dict):
        lease_owner = str(metrics.get("lease_owner", "") or "")
    return (
        f"state={status.get('state', '?')} "
        f"active_mode={status.get('active_mode', '?')} "
        f"ready={bool(status.get('protocol_ready'))} "
        f"power={status.get('power', '?')} "
        f"lease_owner={lease_owner or status.get('lease_owner', '?')}"
    )


def wait_broker(controller, mode: str, timeout: float = STEP_TIMEOUT):
    return controller.wait_for_mode_ready(mode, timeout=timeout)


def wait_meshcore(lora, controller, timeout: float = STEP_TIMEOUT) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lora.radio_owned and lora.meshcore_ready:
            return controller.get_status()
        thread = lora._thread
        if thread is None or not thread.is_alive():
            break
        time.sleep(0.05)
    raise TimeoutError("MeshCore worker did not reach configured RX readiness")


def start_meshcore(lora, controller, region: str | None) -> dict[str, Any]:
    if lora.running or lora.worker_active:
        raise RuntimeError("LoRaManager still has an active worker")
    if not lora.start_meshcore(region):
        raise RuntimeError("LoRaManager rejected start_meshcore")
    return wait_meshcore(lora, controller)


def stop_meshcore(lora, controller) -> dict[str, Any]:
    started = time.monotonic()
    if not lora.stop(timeout=MESHCORE_STOP_TIMEOUT):
        detail = []
        while not lora.queue.empty():
            try:
                detail.append(str(lora.queue.get_nowait()[0]))
            except Exception:
                break
        raise RuntimeError("LoRaManager.stop() failed: " + "; ".join(detail[-4:]))
    elapsed = time.monotonic() - started
    log(f"MeshCore stop returned in {elapsed:.2f}s")
    return wait_broker(controller, "meshtastic")


def start_meshtastic(manager, controller) -> dict[str, Any]:
    if not manager.start():
        raise RuntimeError(
            "MeshtasticManager.start() rejected: "
            + str(manager.last_error or "no detail")
        )
    if not manager.wait_connected(timeout=MESHTASTIC_CONNECT_TIMEOUT):
        raise TimeoutError(
            "MeshtasticManager did not connect: "
            + str(manager.last_error or "no detail")
        )
    status = wait_broker(controller, "meshtastic")
    return status


def close_meshtastic(manager) -> None:
    if manager.running or manager.connected:
        if not manager.close(stop_daemon=False):
            raise RuntimeError(
                "MeshtasticManager.close() could not stop its client worker: "
                + str(manager.last_error or "no detail")
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cycles",
        type=int,
        choices=(1, 2),
        default=1,
        help="number of Meshtastic -> MeshCore -> Meshtastic cycles (1 or 2)",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="WatchDogsGo checkout (default: current directory)",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="show the bounded test sequence without connecting",
    )
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    if not (project_root / "watchdogs" / "lora_manager.py").is_file():
        parser.error("run from WatchDogsGo or pass --project-root")
    sys.path.insert(0, str(project_root))

    if args.plan:
        print(f"cycles={args.cycles}; max_cycles=2; step_timeout={STEP_TIMEOUT:.0f}s")
        print(
            "sequence per cycle: Meshtastic connected -> MeshCore RX ready -> Meshtastic connected"
        )
        print("transmit: none; settings: read-only; service mutations: blocked")
        return 0

    refuse_if_wdg_ui_running()

    # Require the already-running WDG daemon socket before constructing the
    # Meshtastic client. Its read-only systemctl runner blocks any fallback
    # service start if the socket disappears after this check.
    from watchdogs.meshtastic_manager import MeshtasticManager
    from watchdogs.sx1262_client import SX1262Controller

    manager_probe = MeshtasticManager(
        backend_mode=read_meshtastic_backend(project_root),
        service_runner=read_only_systemctl,
        phone_ble_enabled=False,
    )
    socket_path = Path(manager_probe.socket_path)
    if not socket_path.exists():
        raise RuntimeError(f"Meshtastic daemon socket is absent: {socket_path}")

    controller = SX1262Controller()
    lora = None
    manager = manager_probe
    initial_mode = ""
    region = read_meshcore_region(user_home())
    if region:
        log(f"read MeshCore region {region!r} from user config")
    else:
        log("MeshCore region config absent; using WDG default")

    try:
        controller.connect()
        initial_status = controller.get_status()
        initial_mode = str(
            initial_status.get("active_mode") or initial_status.get("state") or ""
        ).lower()
        if initial_mode != "meshtastic":
            raise RuntimeError(
                "safe probe requires Meshtastic to be the initial broker "
                "owner; refusing to replace a direct MeshCore controller; got "
                + compact_status(initial_status)
            )
        log("initial broker: " + compact_status(initial_status))

        from watchdogs.lora_manager import LoRaManager

        lora = LoRaManager(sx1262_controller=controller)

        status = start_meshtastic(manager, controller)
        log("confirmed existing Meshtastic mode: " + compact_status(status))

        for cycle in range(1, args.cycles + 1):
            log(f"cycle {cycle}/{args.cycles}: starting MeshCore RX")
            if manager.running or manager.connected:
                close_meshtastic(manager)
            status = start_meshcore(lora, controller, region)
            log("MeshCore ready: " + compact_status(status))

            log(f"cycle {cycle}/{args.cycles}: returning to Meshtastic")
            status = stop_meshcore(lora, controller)
            log("Meshtastic mode ready: " + compact_status(status))
            status = start_meshtastic(manager, controller)
            log("Meshtastic client connected; broker: " + compact_status(status))

        # Each cycle ends on Meshtastic. Leave its client connected, matching
        # the exact initial radio owner and the normal WDG mode.
        log("test cycles complete; restoring the initial Meshtastic owner")
        if lora.running or lora.worker_active:
            stop_meshcore(lora, controller)
        if not (manager.running and manager.connected):
            status = start_meshtastic(manager, controller)
        final_status = controller.get_status()
        final_mode = str(
            final_status.get("active_mode") or final_status.get("state") or ""
        ).lower()
        if final_mode != initial_mode:
            raise RuntimeError(
                "restoration did not return to initial mode: "
                + compact_status(final_status)
            )
        log("PASS: final broker restored to " + compact_status(final_status))
        return 0
    except Exception as exc:
        log(f"FAIL: {type(exc).__name__}: {exc}")
        return 1
    finally:
        # Try to return ownership to its original mode even after a failed
        # transition. These calls remain bounded and never change systemd.
        stopped = True
        try:
            if lora is not None and (lora.running or lora.worker_active):
                stopped = bool(lora.stop(timeout=MESHCORE_STOP_TIMEOUT))
                if not stopped:
                    log("cleanup: LoRa worker did not release its broker lease")
        except Exception as exc:
            log(f"cleanup: MeshCore stop failed: {type(exc).__name__}: {exc}")
        try:
            if manager.running or manager.connected:
                manager.close(stop_daemon=False)
        except Exception as exc:
            log(f"cleanup: Meshtastic close failed: {type(exc).__name__}: {exc}")
        try:
            if initial_mode == "meshtastic" and controller.connected and stopped:
                status = controller.get_status()
                current = str(
                    status.get("active_mode") or status.get("state") or ""
                ).lower()
                if current != "meshtastic":
                    status = controller.activate_mode("meshtastic")
                    status = controller.wait_for_mode_ready(
                        "meshtastic", timeout=STEP_TIMEOUT
                    )
                    log("cleanup reactivated Meshtastic: " + compact_status(status))
                if not (manager.running and manager.connected):
                    if manager.start():
                        if not manager.wait_connected(
                            timeout=MESHTASTIC_CONNECT_TIMEOUT
                        ):
                            log("cleanup: Meshtastic client did not reconnect")
                    else:
                        log("cleanup: Meshtastic client restart was rejected")
        except Exception as exc:
            log(f"cleanup: could not restore initial mode: {type(exc).__name__}: {exc}")
        try:
            controller.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"FATAL: {type(exc).__name__}: {exc}")
        raise SystemExit(2)
