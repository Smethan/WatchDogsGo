"""Parent-process supervisor for the isolated Reticulum/LXMF sidecar."""

from __future__ import annotations

import json
import logging
import os
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .radio_ownership import RadioOwnership, RadioOwnershipBusy
from .reticulum_config import (
    ReticulumConfigError,
    ReticulumProfile,
    append_history,
    commit_pending_profile,
    ensure_private_dir,
    load_profile,
    save_profile,
    validate_destination_hash,
    validate_message_text,
)

log = logging.getLogger(__name__)
IPC_VERSION = 1
IPC_MAX_PACKET = 64 * 1024
EVENT_LIMIT = 512
REQUEST_NAMES = {
    "hello", "status", "announce", "send_text", "sync_propagation",
    "cancel_propagation", "shutdown",
}
EVENT_NAMES = {
    "ready", "contact", "message", "outbound_status", "radio_status",
    "propagation_status", "error", "stopped",
}


class ReticulumManager:
    """Supervise one Reticulum sidecar without blocking the Pyxel thread."""

    STATES = ("stopped", "starting", "ready", "stopping", "error")

    def __init__(self, app_dir: str | Path, *, service_handoff=None,
                 operation_guard: Callable[[], str] | None = None,
                 runtime_dir: str | Path | None = None,
                 process_factory=None, thread_factory=None,
                 ownership_factory=None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.app_dir = Path(app_dir).resolve()
        self._service_handoff = service_handoff
        self._operation_guard = operation_guard
        self._runtime_dir_override = (
            Path(runtime_dir).resolve() if runtime_dir is not None else None)
        self._process_factory = process_factory or subprocess.Popen
        self._thread_factory = thread_factory or threading.Thread
        self._ownership_factory = ownership_factory or RadioOwnership
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._events_lock = threading.Lock()
        self._events: deque[tuple[str, dict[str, Any]]] = deque()
        self._thread: threading.Thread | None = None
        self._process = None
        self._listener: socket.socket | None = None
        self._socket: socket.socket | None = None
        self._socket_path: Path | None = None
        self._stop_requested = threading.Event()
        self._shutdown_requested = threading.Event()
        self._starting = False
        self._reuse_snapshot_once = False
        self._session_reused_snapshot = False
        self._session_created_snapshot = False
        self._profile_file_override: Path | None = None
        self._profile: ReticulumProfile | None = None
        self._action = ""
        self._state = "stopped"
        self._radio_owned = False
        self._resource_close_uncertain = False
        self._teardown_verified = False
        self._local_hash = ""
        self._local_name = ""
        self._last_error = ""
        self._event_drops = 0
        self._propagation_state = "disabled"
        self._propagation_progress = 0.0
        self._propagation_node = ""
        self._pending_requests: dict[str, dict[str, Any]] = {}
        self._outbound_pending: set[str] = set()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def running(self) -> bool:
        with self._lock:
            return self._state in ("starting", "ready", "stopping")

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._state == "ready"

    @property
    def worker_active(self) -> bool:
        with self._lock:
            thread = self._thread
            return bool(self._starting or (thread and thread.is_alive()))

    @property
    def radio_owned(self) -> bool:
        with self._lock:
            return self._radio_owned

    @property
    def local_hash(self) -> str:
        with self._lock:
            return self._local_hash

    @property
    def local_name(self) -> str:
        with self._lock:
            return self._local_name

    @property
    def profile(self) -> ReticulumProfile | None:
        """Return the exact validated profile used for this sidecar session."""
        with self._lock:
            return self._profile

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    @property
    def event_drops(self) -> int:
        with self._events_lock:
            return self._event_drops

    @property
    def propagation_state(self) -> str:
        with self._lock:
            return self._propagation_state

    @property
    def propagation_progress(self) -> float:
        with self._lock:
            return self._propagation_progress

    @property
    def propagation_node(self) -> str:
        with self._lock:
            return self._propagation_node

    def _service_restore_pending(self) -> bool:
        handoff = self._service_handoff
        if handoff is None:
            return False
        pending = getattr(handoff, "service_restore_pending", False)
        try:
            return bool(pending() if callable(pending) else pending)
        except Exception:
            return False

    def reuse_retained_service_snapshot_once(self) -> None:
        """Allow the next direct-to-direct start to reuse the exact token."""
        with self._lock:
            self._reuse_snapshot_once = True

    def _prepare_service_handoff(self) -> bool:
        handoff = self._service_handoff
        if handoff is None:
            return True
        self._session_created_snapshot = False
        self._session_reused_snapshot = False
        reuse = False
        with self._lock:
            reuse = self._reuse_snapshot_once
            self._reuse_snapshot_once = False
        try:
            if self._service_restore_pending():
                if reuse:
                    self._session_reused_snapshot = True
                    return True
                if not handoff.resume_service(timeout=15.0, connect=False):
                    self._emit("error", {
                        "code": "service_restore_failed",
                        "detail": "Could not restore the retained Meshtastic service state",
                        "fatal": True,
                    })
                    return False
                if self._service_restore_pending():
                    self._emit("error", {
                        "code": "service_restore_unresolved",
                        "detail": "Meshtastic service restore token remained unresolved",
                        "fatal": True,
                    })
                    return False
            if not handoff.suspend_service(timeout=10.0):
                self._emit("error", {
                    "code": "service_suspend_failed",
                    "detail": "Meshtastic services did not release the SX1262",
                    "fatal": True,
                })
                return False
            if not self._service_restore_pending():
                self._emit("error", {
                    "code": "service_snapshot_missing",
                    "detail": "Meshtastic suspension did not retain an exact restore snapshot",
                    "fatal": True,
                })
                return False
            self._session_created_snapshot = True
            return True
        except Exception as exc:
            self._emit("error", {
                "code": "service_handoff_failed", "detail": str(exc)[:240],
                "fatal": True,
            })
            return False

    def _restore_service_after_failed_start(self) -> None:
        if (not self._session_created_snapshot
                or self._service_handoff is None
                or not self._service_restore_pending()):
            return
        try:
            restored = bool(self._service_handoff.resume_service(
                timeout=15.0, connect=True))
        except Exception as exc:
            restored = False
            self._emit("error", {
                "code": "service_start_rollback_failed",
                "detail": str(exc)[:240], "fatal": True,
            })
        if not restored:
            self._emit("error", {
                "code": "service_start_rollback_failed",
                "detail": "Reticulum failed and the exact Meshtastic state could not be restored",
                "fatal": True,
            })

    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        with self._events_lock:
            if len(self._events) >= EVENT_LIMIT:
                removable = next((i for i, item in enumerate(self._events)
                                  if item[0] in (
                                      "radio_status", "status",
                                      "propagation_status")), None)
                if removable is not None:
                    del self._events[removable]
                else:
                    self._events.popleft()
                self._event_drops += 1
                if not any(item[0] == "error"
                           and item[1].get("code") == "event_queue_overflow"
                           for item in self._events):
                    # Reserve one slot for the visible overflow diagnostic and
                    # one for the event that triggered it. Critical message
                    # records are also appended to durable history below.
                    if len(self._events) >= EVENT_LIMIT - 1:
                        removable = next((
                            i for i, item in enumerate(self._events)
                            if item[0] in (
                                "radio_status", "status",
                                "propagation_status")), None)
                        if removable is not None:
                            del self._events[removable]
                        else:
                            self._events.popleft()
                        self._event_drops += 1
                    self._events.append(("error", {
                        "code": "event_queue_overflow",
                        "detail": "Reticulum event queue overflowed",
                        "fatal": False,
                    }))
            if len(self._events) >= EVENT_LIMIT:
                removable = next((i for i, item in enumerate(self._events)
                                  if item[0] in (
                                      "radio_status", "status",
                                      "propagation_status")), None)
                if removable is not None:
                    del self._events[removable]
                else:
                    self._events.popleft()
                self._event_drops += 1
            self._events.append((name, payload))
        if name in ("message", "outbound_status"):
            try:
                append_history(self.app_dir, {
                    "event": name, "recorded_at": time.time(), **payload})
            except Exception:
                log.exception("Could not append Reticulum message history")

    def poll_events(self, limit: int = 100) -> list[tuple[str, dict]]:
        limit = max(0, min(int(limit), EVENT_LIMIT))
        result = []
        with self._events_lock:
            while self._events and len(result) < limit:
                result.append(self._events.popleft())
        return result

    def _validated_runtime_root(self) -> Path:
        candidates: list[Path] = []
        if self._runtime_dir_override is not None:
            candidates.append(self._runtime_dir_override)
        else:
            value = os.environ.get("XDG_RUNTIME_DIR", "").strip()
            if value:
                candidates.append(Path(value))
            candidates.append(Path("/run/user") / str(os.getuid()))
        for candidate in candidates:
            try:
                if not candidate.is_absolute():
                    continue
                info = candidate.stat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                    continue
                if info.st_mode & 0o022:
                    continue
                return ensure_private_dir(candidate / "watchdogs")
            except (OSError, ReticulumConfigError):
                continue
        raise RuntimeError(
            "No private user runtime directory is available for Reticulum IPC")

    def _open_listener(self) -> socket.socket:
        root = self._validated_runtime_root()
        path = root / "reticulum-v1.sock"
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None:
            if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError("refusing unsafe existing Reticulum IPC path")
            if not stat.S_ISSOCK(info.st_mode):
                raise RuntimeError("Reticulum IPC path exists and is not a socket")
            path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            listener.bind(str(path))
            os.chmod(path, 0o600)
            listener.listen(1)
            listener.settimeout(0.25)
        except Exception:
            listener.close()
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise
        self._socket_path = path
        self._listener = listener
        return listener

    @staticmethod
    def _decode_packet(packet: bytes) -> dict[str, Any]:
        if not packet or len(packet) > IPC_MAX_PACKET:
            raise ValueError("invalid Reticulum IPC packet size")
        value = json.loads(packet.decode("utf-8"))
        if not isinstance(value, dict) or value.get("v") != IPC_VERSION:
            raise ValueError("unsupported Reticulum IPC envelope")
        if value.get("type") not in ("reply", "event"):
            raise ValueError("unexpected Reticulum IPC message type")
        if not isinstance(value.get("name"), str) \
                or not isinstance(value.get("payload"), dict):
            raise ValueError("invalid Reticulum IPC fields")
        if value["type"] == "reply":
            if (value["name"] not in REQUEST_NAMES
                    or not isinstance(value.get("request_id"), str)
                    or not value["request_id"]):
                raise ValueError("invalid Reticulum IPC reply")
        elif value["name"] not in EVENT_NAMES:
            raise ValueError("invalid Reticulum IPC event")
        return value

    def _send_request(self, name: str, payload: dict[str, Any], *,
                      request_id: str | None = None,
                      context: dict[str, Any] | None = None) -> str | None:
        sock = self._socket
        if sock is None:
            return None
        request_id = request_id or str(uuid.uuid4())
        packet = json.dumps({
            "v": IPC_VERSION, "type": "request", "request_id": request_id,
            "name": name, "payload": payload,
        }, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(packet) > IPC_MAX_PACKET:
            return None
        with self._lock:
            self._pending_requests[request_id] = {
                "name": name, **dict(context or {}),
            }
        try:
            with self._send_lock:
                sock.sendall(packet)
        except OSError as exc:
            with self._lock:
                self._last_error = str(exc)
                self._pending_requests.pop(request_id, None)
            return None
        return request_id

    def _handle_message(self, value: dict[str, Any]) -> None:
        name = value["name"]
        payload = value["payload"]
        if value["type"] == "reply":
            request_id = value.get("request_id", "")
            with self._lock:
                request = self._pending_requests.pop(request_id, {})
            if payload.get("error"):
                correlation_id = str(request.get("correlation_id") or "")
                if name == "send_text" and correlation_id:
                    with self._lock:
                        self._outbound_pending.discard(correlation_id)
                    self._emit("outbound_status", {
                        "correlation_id": correlation_id,
                        "message_hash": "",
                        "state": "failed",
                        "detail": str(payload["error"])[:240],
                    })
                self._emit("error", {
                    "code": f"{name}_failed",
                    "detail": str(payload["error"])[:240], "fatal": False,
                })
            return
        if name == "outbound_status":
            correlation_id = str(payload.get("correlation_id") or "")
            if correlation_id:
                with self._lock:
                    if str(payload.get("state") or "") in (
                            "stored", "delivered", "failed"):
                        self._outbound_pending.discard(correlation_id)
                    else:
                        self._outbound_pending.add(correlation_id)
        if name == "ready":
            identity_hash = str(payload.get("identity_hash") or "")
            display_name = str(payload.get("display_name") or "")
            validate_destination_hash(identity_hash)
            if payload.get("radio_owned") is not True:
                raise ValueError(
                    "Reticulum ready event did not confirm radio ownership")
            with self._lock:
                self._local_hash = identity_hash
                self._local_name = display_name[:64]
                self._radio_owned = bool(payload.get("radio_owned"))
                profile = payload.get("profile")
                if isinstance(profile, dict):
                    node = str(profile.get(
                        "propagation_node_hash") or "")
                    self._propagation_node = node
                    self._propagation_state = (
                        "idle" if node else "disabled")
                    self._propagation_progress = 0.0
                self._state = "ready"
        elif name == "propagation_status":
            state = str(payload.get("state") or "unknown")[:40]
            progress = payload.get("progress", 0.0)
            if isinstance(progress, bool) or not isinstance(
                    progress, (int, float)):
                raise ValueError("invalid propagation progress")
            node = str(payload.get("node_hash") or "")
            if node:
                validate_destination_hash(node)
            with self._lock:
                self._propagation_state = state
                self._propagation_progress = max(
                    0.0, min(1.0, float(progress)))
                self._propagation_node = node
        elif name == "stopped":
            with self._lock:
                self._radio_owned = bool(payload.get("radio_owned"))
                self._teardown_verified = bool(
                    payload.get("clean", False)
                    and not self._radio_owned)
                if not self._teardown_verified:
                    self._resource_close_uncertain = True
        elif name == "error":
            detail = str(payload.get("detail") or "Reticulum error")[:240]
            with self._lock:
                self._last_error = detail
                if payload.get("fatal"):
                    self._state = "error"
                    if payload.get("code") == "radio_close_uncertain":
                        self._resource_close_uncertain = True
        self._emit(name, payload)

    def _fail_pending_outbound(self, detail: str) -> None:
        with self._lock:
            correlations = list(self._outbound_pending)
            self._outbound_pending.clear()
            self._pending_requests.clear()
        for correlation_id in correlations:
            self._emit("outbound_status", {
                "correlation_id": correlation_id,
                "message_hash": "",
                "state": "failed",
                "detail": detail[:240],
            })

    def _spawn(self, profile_file: Path):
        command = [
            sys.executable, "-m", "watchdogs.reticulum_sidecar",
            "--profile", str(profile_file),
            "--socket", str(self._socket_path),
        ]
        return self._process_factory(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
            cwd=str(self.app_dir),
        )

    def _validated_profile_file(self, profile_file: Path) -> Path:
        """Accept only a private regular profile in this app's state dir."""
        raw = Path(profile_file)
        if not raw.is_absolute() or raw.is_symlink():
            raise ReticulumConfigError(
                "Reticulum sidecar profile path is unsafe")
        try:
            resolved = raw.resolve(strict=True)
            expected_parent = (self.app_dir / "reticulum").resolve(strict=True)
            info = resolved.stat()
        except OSError as exc:
            raise ReticulumConfigError(
                f"Reticulum sidecar profile is unavailable: {exc}") from exc
        if (resolved.parent != expected_parent
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ReticulumConfigError(
                "Reticulum sidecar profile must be a private owned file")
        return resolved

    def _accept_child(self, listener: socket.socket, process,
                      deadline: float) -> socket.socket:
        while self._monotonic() < deadline:
            if self._stop_requested.is_set():
                raise RuntimeError("Reticulum startup was cancelled")
            if process.poll() is not None:
                raise RuntimeError(
                    f"Reticulum sidecar exited during startup ({process.returncode})")
            try:
                conn, _address = listener.accept()
            except socket.timeout:
                continue
            credentials = conn.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED,
                struct.calcsize("3i"))
            pid, uid, _gid = struct.unpack("3i", credentials)
            if uid != os.getuid() or pid != process.pid:
                conn.close()
                continue
            conn.settimeout(0.25)
            return conn
        raise TimeoutError("Reticulum sidecar did not connect within 15 seconds")

    def _worker(self, profile_file: Path) -> None:
        startup_ready = False
        try:
            if not self._prepare_service_handoff():
                raise RuntimeError("Reticulum service handoff was rejected")
            if self._stop_requested.is_set():
                raise RuntimeError("Reticulum startup was cancelled")
            listener = self._open_listener()
            process = self._spawn(profile_file)
            with self._lock:
                self._process = process
            conn = self._accept_child(
                listener, process, self._monotonic() + 15.0)
            self._socket = conn
            self._send_request("hello", {})
            while not self._stop_requested.is_set():
                if process.poll() is not None:
                    break
                try:
                    packet = conn.recv(IPC_MAX_PACKET + 1)
                except socket.timeout:
                    continue
                if not packet:
                    break
                try:
                    value = self._decode_packet(packet)
                    self._handle_message(value)
                    if value["type"] == "event" and value["name"] == "ready":
                        startup_ready = True
                except Exception as exc:
                    self._emit("error", {
                        "code": "ipc_protocol", "detail": str(exc)[:240],
                        "fatal": False,
                    })
            returncode = process.poll()
            expected_stop = (self._shutdown_requested.is_set()
                             or self._stop_requested.is_set())
            if returncode is None:
                try:
                    returncode = process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    if not expected_stop:
                        process.terminate()
                        try:
                            returncode = process.wait(timeout=2.0)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            returncode = process.wait(timeout=1.0)
            if (not expected_stop and not self._last_error):
                detail = (
                    "Reticulum IPC connection ended unexpectedly"
                    if returncode in (None, 0) else
                    f"Reticulum sidecar exited with status {returncode}")
                with self._lock:
                    self._last_error = detail
                    self._state = "error"
                self._emit("error", {
                    "code": "sidecar_exited",
                    "detail": detail,
                    "fatal": True,
                })
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)[:240]
                self._state = "error"
            self._emit("error", {
                "code": "start_failed", "detail": str(exc)[:240],
                "fatal": True,
            })
        finally:
            if not startup_ready and not self._stop_requested.is_set():
                self._restore_service_after_failed_start()
            self._fail_pending_outbound(
                "Reticulum sidecar stopped before terminal delivery state")
            self._cleanup_worker_resources()
            with self._lock:
                self._radio_owned = False
                if self._state != "error" or self._stop_requested.is_set():
                    self._state = "stopped"
                self._thread = None

    def _cleanup_worker_resources(self) -> None:
        sock, listener = self._socket, self._listener
        self._socket = None
        self._listener = None
        for value in (sock, listener):
            if value is not None:
                try:
                    value.close()
                except OSError:
                    pass
        path = self._socket_path
        self._socket_path = None
        if path is not None:
            try:
                info = path.lstat()
                if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
                    path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                log.exception("Could not remove Reticulum IPC socket")

    def start(self, profile: ReticulumProfile | None = None,
              action: str = "handoff", *, profile_file: Path | None = None) \
            -> bool:
        if self._operation_guard is not None:
            reason = str(self._operation_guard() or "").strip()
            if reason:
                self._emit("error", {
                    "code": "operation_blocked", "detail": reason,
                    "fatal": False,
                })
                return False
        try:
            profile = (profile or load_profile(self.app_dir)).validate()
        except ReticulumConfigError as exc:
            self._emit("error", {
                "code": "invalid_profile", "detail": str(exc),
                "fatal": False,
            })
            return False
        if not profile.confirmed:
            self._emit("error", {
                "code": "profile_unconfirmed",
                "detail": "Review and confirm Reticulum RF settings first",
                "fatal": False,
            })
            return False
        with self._lock:
            thread = self._thread
            if self._starting or (thread is not None and thread.is_alive()):
                return False
            self._starting = True
            self._state = "starting"
            self._last_error = ""
            self._resource_close_uncertain = False
            self._teardown_verified = False
            self._propagation_state = (
                "idle" if profile.propagation_node_hash else "disabled")
            self._propagation_progress = 0.0
            self._propagation_node = profile.propagation_node_hash
            self._profile = profile
            self._action = str(action)[:40]
            self._stop_requested.clear()
            self._shutdown_requested.clear()
            try:
                if profile_file is None:
                    profile_file = save_profile(self.app_dir, profile)
                else:
                    profile_file = Path(profile_file)
                profile_file = self._validated_profile_file(profile_file)
                thread = self._thread_factory(
                    target=self._worker, args=(profile_file,),
                    name="wdg-reticulum-manager", daemon=True)
                self._thread = thread
                thread.start()
            except Exception as exc:
                self._thread = None
                self._state = "error"
                self._last_error = str(exc)[:240]
                self._reuse_snapshot_once = False
                self._emit("error", {
                    "code": "thread_start_failed", "detail": str(exc)[:240],
                    "fatal": True,
                })
                return False
            finally:
                self._starting = False
        return True

    def wait_ready(self, timeout: float = 15.0) -> bool:
        deadline = self._monotonic() + max(0.0, timeout)
        while self._monotonic() < deadline:
            if self.ready:
                return True
            if self.state in ("stopped", "error"):
                return False
            time.sleep(0.05)
        return self.ready

    def send_text(self, destination_hash: str, text: str) -> str | None:
        try:
            destination_hash = validate_destination_hash(destination_hash)
            text = validate_message_text(text)
        except ReticulumConfigError as exc:
            self._emit("error", {
                "code": "invalid_message", "detail": str(exc),
                "fatal": False,
            })
            return None
        if not self.ready:
            return None
        correlation_id = str(uuid.uuid4())
        with self._lock:
            self._outbound_pending.add(correlation_id)
        request = self._send_request("send_text", {
            "destination_hash": destination_hash, "text": text,
            "correlation_id": correlation_id,
        }, context={"correlation_id": correlation_id})
        if not request:
            with self._lock:
                self._outbound_pending.discard(correlation_id)
            return None
        self._emit("message", {
            "correlation_id": correlation_id,
            "message_hash": "",
            "direction": "out",
            "peer_hash": destination_hash,
            "peer_name": destination_hash[:8],
            "text": text,
            "state": "queued",
            "delivery_method": (
                "propagated" if self._profile is not None
                and self._profile.propagated_outbound else "direct"),
            "timestamp": time.time(),
        })
        return correlation_id

    def announce(self) -> bool:
        return bool(self.ready and self._send_request("announce", {}))

    def sync_propagation(self, max_messages: int = 100) -> bool:
        """Request stored LXMF messages through the current RNS path."""
        if type(max_messages) is not int or not 0 <= max_messages <= 200:
            return False
        with self._lock:
            if (self._state != "ready" or not self._profile
                    or not self._profile.propagation_node_hash):
                return False
        return bool(self._send_request(
            "sync_propagation", {"max_messages": max_messages}))

    def cancel_propagation_sync(self) -> bool:
        with self._lock:
            if self._state != "ready" or not self._propagation_node:
                return False
        return bool(self._send_request("cancel_propagation", {}))

    def _probe_lock_free(self) -> bool:
        probe = self._ownership_factory()
        try:
            probe.acquire("WatchDogsGo Reticulum shutdown probe")
            probe.release()
            return True
        except RadioOwnershipBusy:
            return False
        except Exception:
            return False

    def stop(self, timeout: float = 8.0) -> bool:
        with self._lock:
            thread = self._thread
            process = self._process
            if thread is None and process is None:
                self._state = "stopped"
                return not self._resource_close_uncertain
            self._state = "stopping"
            self._shutdown_requested.set()
        self._send_request("shutdown", {})
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=min(5.0, max(0.0, timeout)))
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
            except Exception:
                log.exception("Could not terminate Reticulum sidecar")
        self._stop_requested.set()
        if thread is not None and thread is not threading.current_thread():
            remaining = max(0.0, timeout - 7.0)
            thread.join(timeout=max(1.0, remaining))
        with self._lock:
            process = self._process
            process_stopped = process is None or process.poll() is not None
            worker_stopped = self._thread is None or not self._thread.is_alive()
            self._process = None if process_stopped else process
        lock_free = self._probe_lock_free() if process_stopped else False
        clean = (process_stopped and worker_stopped and lock_free
                 and self._teardown_verified
                 and not self._resource_close_uncertain)
        with self._lock:
            self._radio_owned = not lock_free
            self._state = "stopped" if clean else "error"
            if not clean:
                self._last_error = (
                    "Reticulum sidecar or SX1262 cleanup could not be verified")
        if not clean:
            self._emit("error", {
                "code": "shutdown_uncertain", "detail": self._last_error,
                "fatal": True,
            })
        return clean

    close = stop

    def restart_with_profile(self, profile: ReticulumProfile,
                             timeout: float = 15.0) -> bool:
        """Apply a profile transactionally; restore the old profile on error."""
        profile = profile.validate()
        old = load_profile(self.app_dir)
        pending_path = save_profile(self.app_dir, profile, pending=True)
        was_running = self.running
        if was_running and not self.stop(timeout=8.0):
            return False
        self.reuse_retained_service_snapshot_once()
        if self.start(profile, action="profile", profile_file=pending_path) \
                and self.wait_ready(timeout):
            commit_pending_profile(self.app_dir)
            return True
        candidate_clean = self.stop(timeout=8.0)
        try:
            pending_path.unlink()
        except FileNotFoundError:
            pass
        if was_running and candidate_clean:
            self.reuse_retained_service_snapshot_once()
            rollback_ready = bool(
                self.start(old, action="profile_rollback")
                and self.wait_ready(timeout))
            if not rollback_ready:
                self.stop(timeout=8.0)
                self._emit("error", {
                    "code": "profile_rollback_failed",
                    "detail": (
                        "Neither the proposed nor previous Reticulum profile "
                        "could be started; restart WatchDogsGo before using "
                        "the SX1262"),
                    "fatal": True,
                })
        return False
