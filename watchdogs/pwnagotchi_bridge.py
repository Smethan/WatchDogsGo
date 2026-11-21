"""Background client for the local Pwnagotchi control socket and CLI.

The Pyxel thread reads :meth:`state` and queues actions with :meth:`submit`;
all socket and subprocess work stays on this bridge's daemon thread. Activity
entries are newest-first and intentionally bounded for the small device UI.
"""

from __future__ import annotations

import json
import queue
import select
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from copy import deepcopy
from typing import Any


DEFAULT_SOCKET = "/run/pwnagotchi/control.sock"
DEFAULT_CTL = "pwnagotchi-ctl"
MAX_LINE = 1 << 20
EVENT_LIMIT = 80


class PwnagotchiBridge:
    """Keep a bounded live view of Pwnagotchi without blocking the UI."""

    def __init__(
        self,
        socket_path: str = DEFAULT_SOCKET,
        ctl_command: str | tuple[str, ...] = DEFAULT_CTL,
        *,
        poll_interval: float = 8.0,
        reconnect_delay: float = 2.0,
        event_limit: int = EVENT_LIMIT,
        runner=subprocess.run,
    ):
        self.socket_path = socket_path
        self.ctl_command = (
            (ctl_command,) if isinstance(ctl_command, str) else tuple(ctl_command)
        )
        self.poll_interval = max(1.0, float(poll_interval))
        self.reconnect_delay = max(0.1, float(reconnect_delay))
        self.event_limit = max(1, int(event_limit))
        self._runner = runner
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._actions: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        self._events: deque[dict[str, Any]] = deque(maxlen=self.event_limit)
        self._last_activity: dict[str, tuple[str, float]] = {}
        self._last_mood_signature: tuple[str, str] | None = None
        self._data: dict[str, Any] = {
            "connected": False,
            "snapshot": None,
            "unread": 0,
            "error": "",
            "busy": False,
            "last_result": "",
            "service_active": False,
            "service_enabled": False,
            "backend": "",
            "interface": "",
            "adapters": [],
        }
        self._thread = threading.Thread(
            target=self._run, name="pwnagotchi-bridge-control", daemon=True
        )
        self._subscription_thread = threading.Thread(
            target=self._run_subscription,
            name="pwnagotchi-bridge-subscription",
            daemon=True,
        )
        self._subscription_thread.start()
        self._thread.start()

    def state(self) -> dict[str, Any]:
        """Return a detached UI-safe copy; activity entries are newest-first."""
        with self._lock:
            result = deepcopy(self._data)
            result["events"] = list(deepcopy(self._events))
            return result

    def mark_read(self) -> None:
        with self._lock:
            self._data["unread"] = 0

    def submit(self, action: str, value: Any = None) -> bool:
        """Queue one action for the worker; return False while another runs."""
        if action not in {
            "start",
            "stop",
            "activate",
            "deactivate",
            "deauth",
            "adapter",
        }:
            return False
        if action == "deauth" and type(value) is not bool:
            return False
        if action == "adapter" and (
            not isinstance(value, str)
            or not value
            or len(value) > 15
            or not all(c.isalnum() or c in "_.-" for c in value)
        ):
            return False
        with self._lock:
            if self._data["busy"] or self._stop.is_set():
                return False
            try:
                self._actions.put_nowait((action, value))
            except queue.Full:
                return False
            self._data["busy"] = True
            self._data["last_result"] = "Working…"
            return True

    def close(self) -> None:
        self._stop.set()
        # Drop accepted work that has not started. An action already dequeued
        # runs to its bounded command timeout before this join returns.
        try:
            self._actions.get_nowait()
        except queue.Empty:
            pass
        else:
            with self._lock:
                self._data["busy"] = False
                self._data["last_result"] = "Cancelled during shutdown"
        if self._subscription_thread.is_alive():
            self._subscription_thread.join(timeout=1.0)
        if self._thread.is_alive():
            # Service commands use a 15 second subprocess timeout. Leave a
            # small margin so a started mutation cannot outlive WDG shutdown.
            self._thread.join(timeout=16.0)

    def _run(self) -> None:
        next_poll = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_poll:
                self._poll_cli()
                next_poll = now + self.poll_interval
            timeout = min(0.25, max(0.01, next_poll - time.monotonic()))
            try:
                action = self._actions.get(timeout=timeout)
            except queue.Empty:
                continue
            # submit() represents an accepted request. If close() raced with
            # dequeue, finish that bounded action before returning.
            self._run_action(*action)

    def _run_subscription(self) -> None:
        while not self._stop.is_set():
            try:
                self._subscribe_once()
            except (OSError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
                with self._lock:
                    self._data["connected"] = False
                    if not self._data["error"] or self._data["error"].startswith(
                        "Socket"
                    ):
                        self._data["error"] = f"Socket unavailable: {exc}"
            if not self._stop.is_set():
                self._stop.wait(self.reconnect_delay)

    def _command(self, *args: str, sudo: bool = False) -> list[str]:
        command = list(self.ctl_command)
        if sudo:
            command = [shutil.which("sudo") or "sudo", "-n", *command]
        return [*command, *args]

    def _run_cli(self, *args: str, sudo: bool = False, timeout: float = 4.0):
        return self._runner(
            self._command(*args, sudo=sudo),
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    def _poll_cli(self) -> None:
        try:
            result = self._run_cli("status", "--json", timeout=4.0)
            if result.returncode == 0:
                body = json.loads(result.stdout or "{}")
                if not isinstance(body, dict):
                    raise ValueError("status returned an invalid JSON object")
                services = [
                    row for row in (body.get("services") or []) if isinstance(row, dict)
                ]
                live = body.get("live") or {}
                if not isinstance(live, dict):
                    live = {}
                with self._lock:
                    self._data["service_active"] = any(
                        s.get("active")
                        for s in services
                        if s.get("unit") == "pwnagotchi-uconsole.service"
                    )
                    self._data["service_enabled"] = any(
                        s.get("enabled")
                        for s in services
                        if s.get("unit") == "pwnagotchi-uconsole.service"
                    )
                    self._data["backend"] = (
                        body.get("backend") or live.get("backend") or ""
                    )
                    self._data["interface"] = (live.get("interface") or "") or (
                        (live.get("snapshot") or {}).get("interface") or ""
                    )
                    if not self._data["connected"] and body.get("live"):
                        self._apply_snapshot(body["live"])
                    if not self._data["connected"] and body.get("live_error"):
                        self._data["error"] = str(body["live_error"])
        except (
            OSError,
            subprocess.SubprocessError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ) as exc:
            with self._lock:
                self._data["error"] = f"Status unavailable: {exc}"

        try:
            result = self._run_cli("adapter", "list", "--json", timeout=4.0)
            if result.returncode == 0:
                body = json.loads(result.stdout or "[]")
                records = (
                    body.get("interfaces", body.get("adapters", body))
                    if isinstance(body, dict)
                    else body
                )
                if isinstance(records, list):
                    normalized = []
                    for row in records:
                        if isinstance(row, str):
                            normalized.append({"name": row, "mode": ""})
                        elif isinstance(row, dict) and row.get("name"):
                            normalized.append(
                                dict(
                                    row,
                                    name=str(row["name"]),
                                    mode=str(row.get("mode", "")),
                                )
                            )
                    with self._lock:
                        self._data["adapters"] = normalized
        except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
            pass

        try:
            result = self._run_cli("adapter", "status", "--json", timeout=4.0)
            if result.returncode == 0:
                body = json.loads(result.stdout or "{}")
                if isinstance(body, dict):
                    with self._lock:
                        self._data["backend"] = (
                            body.get("backend") or self._data["backend"]
                        )
                        self._data["interface"] = (
                            body.get("interface")
                            or body.get("configured")
                            or self._data["interface"]
                        )
        except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
            pass

    def _run_action(self, action: str, value: Any) -> None:
        try:
            if action == "deauth":
                self._request(
                    {"v": 1, "op": "set", "setting": "deauth", "enabled": value}
                )
                result_text = "Deauth enabled" if value else "Deauth disabled"
            elif action == "adapter":
                result = self._run_cli("adapter", "set", value, sudo=True, timeout=15.0)
                self._check_command(result, "Adapter change")
                result_text = f"Adapter set to {value}; service restarted"
            else:
                result = self._run_cli(action, timeout=15.0)
                self._check_command(result, f"Pwnagotchi {action}")
                result_text = f"Pwnagotchi {action} requested"
            with self._lock:
                self._data["last_result"] = result_text
                self._data["error"] = ""
        except (
            OSError,
            subprocess.SubprocessError,
            ValueError,
            RuntimeError,
            TimeoutError,
        ) as exc:
            with self._lock:
                self._data["last_result"] = str(exc)
                self._data["error"] = str(exc)
        finally:
            with self._lock:
                self._data["busy"] = False
            # Refresh status after a change unless shutdown is waiting for this
            # action to finish; the service mutation is the required boundary.
            if not self._stop.is_set():
                self._poll_cli()

    @staticmethod
    def _check_command(result, label: str) -> None:
        if result.returncode != 0:
            detail = (
                getattr(result, "stderr", "") or getattr(result, "stdout", "") or ""
            ).strip()
            raise RuntimeError(f"{label} failed{': ' + detail if detail else ''}")

    def _request(self, message: dict[str, Any], timeout: float = 2.0) -> dict[str, Any]:
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(payload) > 8192:
            raise ValueError("control request is too large")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(self.socket_path)
            client.sendall(payload)
            data = bytearray()
            while b"\n" not in data:
                chunk = client.recv(4096)
                if not chunk:
                    raise ConnectionError("control socket closed before response")
                data.extend(chunk)
                if len(data) > MAX_LINE:
                    raise ValueError("control response exceeds limit")
        response = json.loads(bytes(data).split(b"\n", 1)[0].decode("utf-8"))
        if response.get("type") == "error":
            raise RuntimeError(
                f"{response.get('code', 'control_error')}: {response.get('message', 'request failed')}"
            )
        return response

    def _fetch_snapshot(self) -> None:
        try:
            response = self._request({"v": 1, "op": "snapshot"})
            with self._lock:
                self._apply_snapshot(response.get("data") or {})
        except (
            OSError,
            ValueError,
            RuntimeError,
            TimeoutError,
            json.JSONDecodeError,
        ) as exc:
            with self._lock:
                self._data["error"] = f"Snapshot refresh failed: {exc}"

    def _subscribe_once(self) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2.0)
            client.connect(self.socket_path)
            client.sendall(b'{"v":1,"op":"subscribe"}\n')
            client.setblocking(False)
            with self._lock:
                self._data["connected"] = True
                self._data["error"] = ""
            buffer = bytearray()
            while not self._stop.is_set():
                ready, _, _ = select.select([client], [], [], 0.5)
                if not ready:
                    continue
                chunk = client.recv(16384)
                if not chunk:
                    raise ConnectionError("subscription closed")
                buffer.extend(chunk)
                if len(buffer) > MAX_LINE and b"\n" not in buffer:
                    raise ValueError("control event exceeds limit")
                while b"\n" in buffer:
                    raw, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    if len(raw) > MAX_LINE:
                        raise ValueError("control event exceeds limit")
                    if not raw:
                        continue
                    message = json.loads(raw.decode("utf-8"))
                    self._handle_message(message)

    def _handle_message(self, message: dict[str, Any]) -> None:
        kind = message.get("event")
        data = message.get("data") or {}
        if message.get("type") == "snapshot":
            with self._lock:
                self._apply_snapshot(data)
            return
        if message.get("type") != "event":
            return
        if kind == "resync_required":
            self._fetch_snapshot()
            self._append_event(
                "sync", "Activity stream refreshed", data, message.get("ts")
            )
            return
        if kind == "mood":
            mood = dict(data) if isinstance(data, dict) else {}
            with self._lock:
                current = self._data.get("snapshot") or {}
                current["mood"] = mood
                self._data["snapshot"] = current
                self._record_mood(mood, message.get("ts"))
            return
        if kind == "ui":
            with self._lock:
                current = self._data.get("snapshot") or {}
                current["ui"] = dict(current.get("ui") or {}, **(data.get("ui") or {}))
                self._data["snapshot"] = current
                self._data["interface"] = (
                    current.get("interface") or self._data["interface"]
                )
            self._append_event("status", self._ui_text(data), data, message.get("ts"))
            return
        if kind == "ap_snapshot":
            with self._lock:
                current = self._data.get("snapshot") or {}
                current["radio"] = dict(current.get("radio") or {}, **data)
                self._data["snapshot"] = current
            count = data.get(
                "eligible_ap_count", data.get("ap_count", len(data.get("aps") or []))
            )
            self._append_event(
                "networks",
                f"Network scan: {count} eligible APs",
                data,
                message.get("ts"),
            )
            return
        if kind == "action":
            action = data.get("action", "radio action")
            outcome = data.get("result", "submitted")
            bssid = data.get("bssid") or "unknown AP"
            station = data.get("station")
            text = f"{action.title()} {outcome}: {bssid}"
            if station:
                text += f" / {station}"
            with self._lock:
                current = self._data.get("snapshot") or {}
                radio = dict(current.get("radio") or {})
                actions = dict(radio.get("actions") or {})
                if outcome == "submitted":
                    counter = f"{action}_submitted"
                    actions[counter] = int(actions.get(counter, 0) or 0) + 1
                radio["actions"] = actions
                current["radio"] = radio
                self._data["snapshot"] = current
            self._append_event("decision", text, data, message.get("ts"))
            return
        if kind == "handshake":
            with self._lock:
                current = self._data.get("snapshot") or {}
                capture = dict(current.get("capture") or {})
                capture["handshakes_run"] = (
                    int(capture.get("handshakes_run", 0) or 0) + 1
                )
                capture["last_handshake"] = dict(data)
                current["capture"] = capture
                self._data["snapshot"] = current
            self._append_event(
                "capture",
                f"Handshake saved: {data.get('bssid') or 'network'}",
                data,
                message.get("ts"),
            )
            return
        if kind == "epoch":
            with self._lock:
                current = self._data.get("snapshot") or {}
                current["epoch"] = dict(data)
                self._data["snapshot"] = current
            number = data.get("number", "?")
            stats = data.get("stats") or {}
            parts = []
            for key in ("reward", "num_handshakes", "num_deauths", "num_associations"):
                if key in stats:
                    parts.append(f"{key.replace('num_', '')} {stats[key]}")
            suffix = " · ".join(parts)
            self._append_event(
                "brain",
                f"AI epoch {number}" + (f": {suffix}" if suffix else ""),
                data,
                message.get("ts"),
            )
            return
        if kind == "control":
            setting = data.get("setting", "control")
            enabled = data.get("enabled")
            with self._lock:
                current = self._data.get("snapshot") or {}
                controls = dict(current.get("controls") or {})
                if setting in ("deauth", "associate") and type(enabled) is bool:
                    controls[setting] = enabled
                if "persistent" in data:
                    controls["persistent"] = bool(data["persistent"])
                current["controls"] = controls
                self._data["snapshot"] = current
            self._append_event(
                "control",
                f"{setting.title()} {'on' if enabled else 'off'}",
                data,
                message.get("ts"),
            )
            return
        if kind == "c5":
            with self._lock:
                current = self._data.get("snapshot") or {}
                current["radio"] = dict(current.get("radio") or {}, **data)
                current["c5"] = dict(data)
                self._data["snapshot"] = current
            self._append_event(
                "networks",
                f"C5 {data.get('mode', 'radio')}: {data.get('ap_count', 0)} APs, {data.get('frames', 0)} frames",
                data,
                message.get("ts"),
            )

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._data["snapshot"] = deepcopy(snapshot)
        self._data["backend"] = snapshot.get("backend") or self._data["backend"]
        self._data["interface"] = snapshot.get("interface") or self._data["interface"]
        mood = snapshot.get("mood")
        if isinstance(mood, dict):
            self._record_mood(mood, mood.get("since"))

    def _record_mood(self, mood: dict[str, Any], ts: Any = None) -> None:
        """Log each meaningful mood/reason change once, including resync state."""
        name = str(mood.get("name") or "unknown").strip().lower()
        reason = str(mood.get("reason") or "").strip()
        signature = (name, reason)
        if signature == self._last_mood_signature:
            return
        self._last_mood_signature = signature
        summary = name.upper()
        if reason:
            summary += ": " + reason
        self._append_event("mood", summary, mood, ts or mood.get("since"))

    @staticmethod
    def _ui_text(data: dict[str, Any]) -> str:
        ui = data.get("ui") or {}
        face = ui.get("face")
        status = ui.get("status")
        if face and status:
            return f"Pwnagotchi {face}: {status}"
        if status:
            return f"Pwnagotchi: {status}"
        return "Pwnagotchi status updated"

    def _append_event(self, kind: str, text: str, detail: Any, ts: Any = None) -> None:
        try:
            timestamp = float(ts) if ts is not None else time.time()
        except (TypeError, ValueError):
            timestamp = time.time()
        if kind in ("status", "networks"):
            cooldown = 10.0 if kind == "networks" else 5.0
            with self._lock:
                previous = self._last_activity.get(kind)
                self._last_activity[kind] = (str(text), timestamp)
                if previous and (
                    previous[0] == str(text) or timestamp - previous[1] < cooldown
                ):
                    return
        entry = {"ts": timestamp, "kind": kind, "text": str(text)[:180]}
        if isinstance(detail, dict) and detail:
            fields = {
                "decision": (
                    "action",
                    "result",
                    "bssid",
                    "station",
                    "channel",
                    "error",
                    "at",
                ),
                "capture": ("bssid", "station", "file", "at"),
                "brain": ("number", "stats"),
                "networks": (
                    "eligible_ap_count",
                    "ap_count",
                    "aps_truncated",
                    "mode",
                    "channel",
                    "frames",
                    "eapol",
                    "pmkids",
                    "wifi_records",
                ),
                "status": ("ui",),
                "control": ("setting", "enabled", "persistent"),
                "sync": ("dropped",),
                "mood": ("name", "reason", "since", "inactive_epochs", "active_epochs"),
            }.get(kind, ())
            compact = {key: detail[key] for key in fields if key in detail}
            if kind == "brain" and isinstance(compact.get("stats"), dict):
                stat_keys = (
                    "reward",
                    "num_handshakes",
                    "num_deauths",
                    "num_associations",
                    "epoch",
                )
                compact["stats"] = {
                    key: compact["stats"][key]
                    for key in stat_keys
                    if key in compact["stats"]
                    and isinstance(compact["stats"][key], (str, int, float, bool))
                }
            if kind == "status" and isinstance(compact.get("ui"), dict):
                compact["ui"] = {
                    key: value
                    for key, value in compact["ui"].items()
                    if key in ("face", "status", "mode")
                    and isinstance(value, (str, int, float, bool))
                }
            if compact:
                entry["detail"] = compact
        with self._lock:
            self._events.appendleft(entry)
            self._data["unread"] += 1
