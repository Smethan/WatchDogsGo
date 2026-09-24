"""Background client for a local ``meshtasticd`` instance.

The WDG firmware fork exposes a small local ``SOCK_SEQPACKET`` API which can
coexist with the phone-facing Meshtastic PhoneAPI.  Stock ``meshtasticd`` is
still supported through its official localhost TCP interface.  The public
``MeshtasticManager`` facade deliberately keeps the same state and event names
for both transports so the Pyxel UI never has to know which daemon is running.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Any, Callable


MESHTASTIC_HOST = "127.0.0.1"
MESHTASTIC_PORT = 4403
WDG_SOCKET_PATH = "/run/meshtasticd/wdg.sock"
WDG_PROTOCOL_MAJOR = 1
WDG_PROTOCOL_MINOR = 0
WDG_MAX_PACKET = 64 * 1024
WDG_SERVICE = "meshtasticd-wdg.service"
LEGACY_SERVICE = "meshtasticd.service"
BACKEND_MODES = ("auto", "fork_socket", "legacy_tcp")
MT_DISCOVERY_INTERVAL = 60.0
MT_DISCOVERY_MIN_DISTANCE_M = 50.0


class WdgProtocolError(RuntimeError):
    """The local daemon sent a malformed or incompatible API packet."""


class WdgVersionError(WdgProtocolError):
    """The local daemon speaks a different major WDG API version."""


@dataclass
class _ControlWaiter:
    """Result channel for a public local-API control call."""

    event: threading.Event = field(default_factory=threading.Event)
    ok: bool = False
    body: dict = field(default_factory=dict)
    reason: str = ""


def _default_dependencies():
    from pubsub import pub
    from meshtastic import BROADCAST_ADDR
    from meshtastic.protobuf import portnums_pb2
    from meshtastic.tcp_interface import TCPInterface

    return TCPInterface, pub, portnums_pb2, BROADCAST_ADDR


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _socket_present(path: str) -> bool:
    """Return true only for an existing Unix-domain socket."""
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _connect_seqpacket(path: str) -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        client.settimeout(0.25)
        client.connect(path)
        return client
    except Exception:
        client.close()
        raise


class MeshtasticManager:
    """Own one nonblocking Meshtastic client and normalize its events.

    ``auto`` prefers the fork socket.  Once a fork socket or installed fork
    service is observed, this instance never falls through to TCP: doing so
    could consume the single PhoneAPI queue while a phone owns it over BLE.
    """

    def __init__(
        self,
        host: str = MESHTASTIC_HOST,
        port: int = MESHTASTIC_PORT,
        *,
        dependency_loader: Callable = _default_dependencies,
        port_probe: Callable[[str, int], bool] = _port_open,
        service_runner: Callable[..., Any] = subprocess.run,
        service_controller: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        backend_mode: str = "auto",
        backend: str | None = None,
        socket_path: str = WDG_SOCKET_PATH,
        socket_probe: Callable[[str], bool] = _socket_present,
        socket_connector: Callable[[str], socket.socket] = _connect_seqpacket,
        monotonic: Callable[[], float] = time.monotonic,
        phone_ble_enabled: bool | None = None,
        phone_ble_adapter: str = "auto",
    ) -> None:
        if backend is not None:
            if backend_mode != "auto" and backend_mode != backend:
                raise ValueError("Conflicting Meshtastic backend settings")
            backend_mode = backend
        if backend_mode not in BACKEND_MODES:
            raise ValueError("Unsupported Meshtastic backend: " + str(backend_mode))
        self.host = host
        self.port = port
        self.backend_mode = backend_mode
        self.socket_path = socket_path
        self.active_backend = ""
        self.queue: Queue = Queue()
        self.running = False
        self.connected = False
        self.nodes: dict[str, dict] = {}
        self.channels: list[dict] = [{"index": 0, "name": "Primary"}]
        self.local_node_id = ""
        self.local_name = "Meshtastic"
        self.packets_received = 0
        self.ble_status = "unknown"
        self.phone_connected = False
        self.radio_status = "unknown"
        self.last_error = ""

        self._dependency_loader = dependency_loader
        self._port_probe = port_probe
        self._service_runner = service_runner
        self._service_controller = service_controller
        self._sleep = sleep
        self._socket_probe = socket_probe
        self._socket_connector = socket_connector
        self._monotonic = monotonic
        self._interface = None
        self._socket: socket.socket | None = None
        self._pub = None
        self._portnums = None
        self._broadcast_addr = "^all"
        self._commands: Queue = Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._subscriptions: list[tuple[Callable, str]] = []
        self._request_counter = 0
        self._pending_requests: dict[str, tuple[str, Any]] = {}
        self._completed_requests: set[str] = set()
        self._snapshot_pending = False
        self._snapshot_active = False
        self._snapshot_seen: set[str] = set()
        self._fork_identified = False
        self._socket_capabilities: set[str] = set()
        self._phone_ble_enabled = phone_ble_enabled
        self._phone_ble_adapter = str(phone_ble_adapter or "auto")
        self.started_daemon = False

    def _emit(self, kind: str, value: Any) -> None:
        if kind == "error":
            self.last_error = str(value)
        self.queue.put((kind, value))

    @property
    def backend(self) -> str:
        """The active backend, or configured backend before startup."""
        return self.active_backend or self.backend_mode

    def set_backend_mode(self, mode: str) -> bool:
        """Select the transport for the next connection.

        A live transport is never switched underneath callbacks.  Callers may
        close the manager first, then apply a setting and start it again.
        """
        if mode not in BACKEND_MODES:
            raise ValueError("Unsupported Meshtastic backend: " + str(mode))
        with self._lock:
            if self.running:
                return False
            self.backend_mode = mode
            self.active_backend = ""
            self._fork_identified = False
        return True

    def start(self) -> bool:
        """Start the local daemon if needed and connect in a worker thread."""
        with self._lock:
            if self.running:
                return True
            # A close before the first start, or a previous stopped worker,
            # can leave a sentinel behind. Never let an old lifecycle command
            # terminate a fresh connection.
            while True:
                try:
                    self._commands.get_nowait()
                except Empty:
                    break
            self.running = True
            self.connected = False
            self.active_backend = ""
            self._pending_requests.clear()
            self._completed_requests.clear()
            self._snapshot_pending = False
            self._snapshot_active = False
            self._snapshot_seen.clear()
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, name="wdg-meshtastic", daemon=True)
            self._thread.start()
        return True

    def close(self, *, stop_daemon: bool = False) -> None:
        """Disconnect WDG; optionally stop the daemon to release the radio."""
        self._stop_event.set()
        if self.running or (self._thread and self._thread.is_alive()):
            self._commands.put(("stop", None))
        interface = self._interface
        if interface is not None:
            try:
                interface.close()
            except Exception:
                pass
        client = self._socket
        if client is not None:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if not thread or not thread.is_alive():
            self.running = False
            self._thread = None
        self.connected = False
        if stop_daemon:
            self._stop_service()

    stop = close

    def _control_backend(self) -> str:
        if self.active_backend:
            return self.active_backend
        return self._select_backend()

    def service_ready(self) -> bool:
        """Read readiness for the selected exact daemon without changing it."""
        backend = self._control_backend()
        if self._service_controller is not None:
            target = "wdg" if backend == "fork_socket" else "stock"
            try:
                if not self._service_controller.status(target).active:
                    return False
            except Exception as exc:
                self._emit("error", "Could not read Meshtastic service status: "
                           + str(exc)[:160])
                return False
        if backend == "fork_socket":
            return bool(self._socket_probe(self.socket_path))
        return bool(self._port_probe(self.host, self.port))

    def suspend_service(self, timeout: float = 10.0) -> bool:
        """Disconnect WDG, stop the selected daemon and wait for radio release."""
        self.close()
        if not self._stop_service():
            return False
        deadline = self._monotonic() + max(0.0, timeout)
        while self.service_ready() and self._monotonic() < deadline:
            self._sleep(0.1)
        if self.service_ready():
            self._emit("error", "Meshtastic daemon stopped but remained ready")
            return False
        return True

    def resume_service(self, timeout: float = 15.0, *,
                       connect: bool = True) -> bool:
        """Start the selected daemon, wait for readiness, and optionally connect."""
        backend = self._control_backend()
        # suspend_service() intentionally closes the client and sets this
        # lifecycle flag.  Service startup has its own bounded readiness wait,
        # so clear the old client cancellation before polling.  start() will
        # still drain the previous stop command before creating a new worker.
        self._stop_event.clear()
        ready = (self._ensure_fork_service() if backend == "fork_socket"
                 else self._ensure_legacy_service())
        if not ready:
            return False
        deadline = self._monotonic() + max(0.0, timeout)
        while not self.service_ready() and self._monotonic() < deadline:
            self._sleep(0.1)
        if not self.service_ready():
            self._emit("error", "Meshtastic daemon did not become ready")
            return False
        return self.start() if connect else True

    def send_text(self, text: str, destination: str | int | None = None,
                  channel: int = 0) -> bool:
        if not self.connected or not text.strip():
            return False
        self._commands.put(("send", {
            "text": text.strip(), "destination": destination,
            "channel": max(0, int(channel)),
        }))
        return True

    def request_discovery(self) -> bool:
        """Ask only directly heard nodes for NodeInfo (zero routed hops)."""
        if not self.connected:
            return False
        self._commands.put(("discover", None))
        return True

    def _control_command(self, name: str, body: dict | None = None,
                         *, timeout: float = 4.0) -> tuple[bool, str, dict]:
        """Run one restricted fork command and wait for its correlated reply."""
        if self.active_backend != "fork_socket" or not self.connected:
            reason = "Meshtastic WDG socket is not connected"
            self._emit("error", reason)
            return False, reason, {}
        if threading.current_thread() is self._thread:
            reason = "Meshtastic control command cannot block its worker"
            self._emit("error", reason)
            return False, reason, {}
        waiter = _ControlWaiter()
        self._commands.put(("control", (name, dict(body or {}), waiter)))
        if not waiter.event.wait(max(0.1, float(timeout))):
            reason = f"Timed out waiting for Meshtastic {name} reply"
            self._emit("error", reason)
            return False, reason, {}
        if not waiter.ok:
            self._emit("error", waiter.reason or f"Meshtastic {name} failed")
        return waiter.ok, waiter.reason, dict(waiter.body)

    def set_phone_ble(self, enabled: bool, adapter: str = "auto") -> bool:
        """Enable or disable the daemon's phone-facing BLE transport."""
        adapter = str(adapter or "auto")
        ok, _reason, _body = self._control_command("set_phone_ble", {
            "enabled": bool(enabled), "adapter": adapter,
        })
        if ok:
            self._phone_ble_enabled = bool(enabled)
            self._phone_ble_adapter = adapter
        return ok

    def open_pairing(self, seconds: int = 120) -> bool:
        """Open a bounded Meshtastic phone pairing window."""
        seconds = max(1, min(120, int(seconds)))
        ok, _reason, _body = self._control_command(
            "open_pairing", {"seconds": seconds})
        return ok

    def forget_phone(self) -> bool:
        ok, _reason, _body = self._control_command("forget_phone")
        return ok

    def acquire_ble_scan_lease(self, seconds: int = 20) -> tuple[bool, str]:
        """Ask the daemon to yield one adapter for a bounded host BLE scan."""
        seconds = max(1, min(20, int(seconds)))
        ok, reason, body = self._control_command(
            "ble_scan_lease_acquire", {"seconds": seconds})
        granted = bool(body.get("granted", ok)) if ok else False
        reason = str(body.get("reason") or reason or
                     ("granted" if granted else "scan lease denied"))
        return granted, reason

    def release_ble_scan_lease(self) -> bool:
        ok, _reason, _body = self._control_command("ble_scan_lease_release")
        return ok

    def retry_shared_adapter(self) -> bool:
        ok, _reason, _body = self._control_command("retry_shared_adapter")
        return ok

    def poll_events(self) -> list[tuple[str, Any]]:
        events = []
        while True:
            try:
                events.append(self.queue.get_nowait())
            except Empty:
                return events

    def _run_service(self, action: str, service: str):
        return self._service_runner(
            ["systemctl", action, service],
            capture_output=True, text=True, timeout=12)

    def _fork_service_load_state(self) -> str:
        """Return systemd's load state without emitting user-facing noise."""
        if self._service_controller is not None:
            try:
                return str(self._service_controller.status("wdg").load_state)
            except Exception:
                return "unknown"
        try:
            result = self._service_runner(
                ["systemctl", "show", "--property=LoadState", "--value",
                 WDG_SERVICE], capture_output=True, text=True, timeout=12)
        except Exception:
            return "unknown"
        if result.returncode:
            return "not-found"
        return (result.stdout or "").strip().lower() or "unknown"

    def _ensure_fork_service(self) -> bool:
        if self._socket_probe(self.socket_path):
            if self._service_controller is None:
                self._fork_identified = True
                return True
            try:
                if self._service_controller.status("wdg").active:
                    self._fork_identified = True
                    return True
            except Exception:
                pass
        self._fork_identified = True
        try:
            if self._service_controller is not None:
                status = self._service_controller.start("wdg")
                if not status.active:
                    self._emit("error", "Could not start meshtasticd-wdg: "
                               "service remained inactive")
                    return False
                result = None
            else:
                result = self._run_service("start", WDG_SERVICE)
        except Exception as exc:
            self._emit("error", f"Could not start meshtasticd-wdg: {exc}")
            return False
        if result is not None and result.returncode != 0:
            detail = (result.stderr or result.stdout or "systemctl failed").strip()
            self._emit("error", "Could not start meshtasticd-wdg: " + detail[:160])
            return False
        self.started_daemon = True
        for _ in range(60):
            if self._stop_event.is_set():
                return False
            if self._socket_probe(self.socket_path):
                return True
            self._sleep(0.25)
        self._emit(
            "error", "meshtasticd-wdg started but its local socket never opened")
        return False

    def _ensure_legacy_service(self) -> bool:
        if self._port_probe(self.host, self.port):
            if self._service_controller is None:
                return True
            try:
                if self._service_controller.status("stock").active:
                    return True
            except Exception:
                pass
        try:
            if self._service_controller is not None:
                status = self._service_controller.start("stock")
                if not status.active:
                    self._emit("error", "Could not start meshtasticd: "
                               "service remained inactive")
                    return False
                result = None
            else:
                result = self._run_service("start", LEGACY_SERVICE)
        except Exception as exc:
            self._emit("error", f"Could not start meshtasticd: {exc}")
            return False
        if result is not None and result.returncode != 0:
            detail = (result.stderr or result.stdout or "systemctl failed").strip()
            self._emit("error", "Could not start meshtasticd: " + detail[:160])
            return False
        self.started_daemon = True
        for _ in range(40):
            if self._stop_event.is_set():
                return False
            if self._port_probe(self.host, self.port):
                return True
            self._sleep(0.25)
        self._emit("error", "meshtasticd started but TCP port 4403 never opened")
        return False

    def _select_backend(self) -> str:
        if self.backend_mode == "fork_socket":
            self._fork_identified = True
            return "fork_socket"
        if self.backend_mode == "legacy_tcp":
            return "legacy_tcp"
        if self._socket_probe(self.socket_path):
            self._fork_identified = True
            return "fork_socket"
        # An installed fork service can be inactive or still creating its
        # socket.  Treat either case as authoritative and never consume its
        # PhoneAPI over TCP as a fallback.
        if self._fork_service_load_state() not in ("not-found", "unknown"):
            self._fork_identified = True
            return "fork_socket"
        return "legacy_tcp"

    def _stop_service(self) -> bool:
        fork = (self.active_backend == "fork_socket"
                or self.backend_mode == "fork_socket"
                or self._fork_identified)
        service = WDG_SERVICE if fork else LEGACY_SERVICE
        label = "meshtasticd-wdg" if fork else "meshtasticd"
        try:
            if self._service_controller is not None:
                target = "wdg" if fork else "stock"
                status = self._service_controller.stop(target)
                if status.active:
                    self._emit("error", f"Could not stop {label}: "
                               "service remained active")
                    return False
                result = None
            else:
                result = self._run_service("stop", service)
            if result is not None and result.returncode != 0:
                detail = (result.stderr or result.stdout or "systemctl failed").strip()
                self._emit("error", f"Could not stop {label}: " + detail[:160])
                return False
            self._emit("status", f"{label} stopped; SX1262 released")
        except Exception as exc:
            self._emit("error", f"Could not stop {label}: {exc}")
            return False
        self.started_daemon = False
        return True

    def _legacy_phoneapi_conflict(self) -> bool:
        """Whether TCP could steal a BLE-enabled fork's full-client lease."""
        if self._phone_ble_enabled is False:
            return False
        if self._service_controller is not None:
            try:
                return bool(self._service_controller.status("wdg").active)
            except Exception:
                pass
        return bool(self._socket_probe(self.socket_path))

    def _subscribe(self, callback: Callable, topic: str) -> None:
        self._pub.subscribe(callback, topic)
        self._subscriptions.append((callback, topic))

    def _unsubscribe_all(self) -> None:
        if self._pub is None:
            return
        for callback, topic in self._subscriptions:
            try:
                self._pub.unsubscribe(callback, topic)
            except Exception:
                pass
        self._subscriptions.clear()

    def _run(self) -> None:
        try:
            self.active_backend = self._select_backend()
            if self.active_backend == "fork_socket":
                if (not self._ensure_fork_service()
                        or self._stop_event.is_set()):
                    return
                self._run_socket_backend()
            else:
                if self._legacy_phoneapi_conflict():
                    self._emit(
                        "error",
                        "Legacy Meshtastic TCP is blocked while the WDG fork "
                        "may be serving a phone over BLE")
                    return
                if (not self._ensure_legacy_service()
                        or self._stop_event.is_set()):
                    return
                self._run_legacy_backend()
        finally:
            self.connected = False
            with self._lock:
                self.running = False
                if self._thread is threading.current_thread():
                    self._thread = None

    def _run_legacy_backend(self) -> None:
        """Run the stock TCP backend until stopped."""
        try:
            try:
                (interface_cls, self._pub, self._portnums,
                 self._broadcast_addr) = self._dependency_loader()
            except Exception as exc:
                self._emit(
                    "error", "Meshtastic Python support is unavailable: " + str(exc))
                return

            self._subscribe(self._on_receive, "meshtastic.receive")
            self._subscribe(self._on_node, "meshtastic.node.updated")
            self._subscribe(self._on_connection_lost,
                            "meshtastic.connection.lost")
            try:
                self._interface = interface_cls(
                    hostname=self.host, portNumber=self.port, timeout=15)
            except Exception as exc:
                self._emit("error", f"meshtasticd connection failed: {exc}")
                return
            if self._stop_event.is_set():
                return

            self.connected = True
            self._refresh_identity_and_channels()
            self._snapshot_nodes(cached=True)
            self._emit("connected", {
                "node_id": self.local_node_id,
                "name": self.local_name,
                "channels": list(self.channels),
            })

            while not self._stop_event.is_set():
                try:
                    command, data = self._commands.get(timeout=0.2)
                except Empty:
                    continue
                if command == "stop":
                    break
                if command == "send":
                    self._send_now(data)
                elif command == "discover":
                    self._discover_now()
        finally:
            interface = self._interface
            self._interface = None
            self.connected = False
            self._unsubscribe_all()
            if interface is not None:
                try:
                    interface.close()
                except Exception:
                    pass

    def _run_socket_backend(self) -> None:
        """Reconnect to the fork socket without ever falling through to TCP."""
        delay = 0.25
        while not self._stop_event.is_set():
            was_connected = False
            try:
                client = self._socket_connector(self.socket_path)
                client.settimeout(0.25)
                self._socket = client
                self._socket_negotiate(client)
                if self._stop_event.is_set():
                    break
                self.connected = True
                was_connected = True
                delay = 0.25
                self._emit("connected", {
                    "node_id": self.local_node_id,
                    "name": self.local_name,
                    "channels": list(self.channels),
                    "backend": "fork_socket",
                })
                self._request_snapshot(client)
                self._socket_event_loop(client)
            except WdgVersionError as exc:
                self._emit("error", str(exc))
                return
            except (OSError, WdgProtocolError) as exc:
                if not self._stop_event.is_set():
                    if was_connected:
                        self._emit("error", "meshtasticd-wdg socket failed: "
                                   + str(exc)[:120])
                    else:
                        self._emit("status", "Waiting for meshtasticd-wdg socket: "
                                   + str(exc)[:100])
            finally:
                client = self._socket
                self._socket = None
                if client is not None:
                    try:
                        client.close()
                    except OSError:
                        pass
                if was_connected and not self._stop_event.is_set():
                    self.connected = False
                    self._emit("disconnected", "meshtasticd-wdg connection lost")
                self._fail_control_waiters(
                    "meshtasticd-wdg connection closed")
                self._pending_requests.clear()
                self._snapshot_pending = False
                self._snapshot_active = False
                self._snapshot_seen.clear()
            if self._stop_event.is_set():
                break
            # An interruptible wait keeps close() responsive and bounds log
            # churn if the daemon is restarting.
            self._stop_event.wait(delay)
            delay = min(2.0, delay * 2.0)

    def _next_request_id(self) -> str:
        self._request_counter += 1
        return f"wdg-{self._request_counter}"

    @staticmethod
    def _encode_socket_packet(packet: dict) -> bytes:
        try:
            payload = json.dumps(
                packet, ensure_ascii=False, separators=(",", ":"),
                allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise WdgProtocolError("Could not encode WDG API request") from exc
        if len(payload) > WDG_MAX_PACKET:
            raise WdgProtocolError("WDG API request exceeds 65536 bytes")
        return payload

    @staticmethod
    def _decode_socket_packet(payload: bytes) -> dict:
        if not payload:
            raise ConnectionError("meshtasticd-wdg closed the socket")
        if len(payload) > WDG_MAX_PACKET:
            raise WdgProtocolError("WDG API packet exceeds 65536 bytes")
        try:
            message = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WdgProtocolError("Malformed WDG API JSON packet") from exc
        if not isinstance(message, dict):
            raise WdgProtocolError("WDG API packet must be a JSON object")
        version = message.get("v")
        if version != WDG_PROTOCOL_MAJOR:
            raise WdgVersionError(
                f"WDG API major {version!r} is incompatible; expected "
                f"{WDG_PROTOCOL_MAJOR}")
        if message.get("type") not in ("reply", "event"):
            raise WdgProtocolError("WDG API packet has an invalid type")
        return message

    def _socket_send(self, client: socket.socket, packet: dict) -> None:
        payload = self._encode_socket_packet(packet)
        sent = client.send(payload)
        if sent != len(payload):
            raise OSError("Incomplete WDG API packet write")

    def _socket_command(self, client: socket.socket, name: str,
                        body: dict | None = None, *, pending: Any = None) -> str:
        request_id = self._next_request_id()
        self._socket_send(client, {
            "v": WDG_PROTOCOL_MAJOR,
            "type": "command",
            "request_id": request_id,
            "name": name,
            "body": body or {},
        })
        if pending is not None:
            self._pending_requests[request_id] = (name, pending)
        return request_id

    def _socket_receive(self, client: socket.socket) -> dict | None:
        try:
            payload = client.recv(WDG_MAX_PACKET + 1)
        except socket.timeout:
            return None
        return self._decode_socket_packet(payload)

    def _socket_request(self, client: socket.socket, name: str,
                        body: dict | None = None, timeout: float = 3.0) -> dict:
        request_id = self._socket_command(client, name, body)
        deadline = self._monotonic() + timeout
        while not self._stop_event.is_set() and self._monotonic() < deadline:
            message = self._socket_receive(client)
            if message is None:
                continue
            if (message.get("type") == "reply"
                    and str(message.get("request_id")) == request_id):
                if not message.get("ok"):
                    error = (message.get("message") or message.get("error_code")
                             or f"{name} failed")
                    raise WdgProtocolError(str(error))
                result = message.get("body", message.get("payload", {}))
                if not isinstance(result, dict):
                    raise WdgProtocolError(f"{name} returned a non-object body")
                return result
            self._handle_socket_message(client, message)
        if self._stop_event.is_set():
            raise OSError("Meshtastic socket connection stopped")
        raise OSError(f"Timed out waiting for WDG API {name} reply")

    @staticmethod
    def _api_major(body: dict) -> int | None:
        value: Any = body.get("protocol_version")
        if value is None:
            value = body.get("api_version", body.get("wdg_api"))
        if isinstance(value, dict):
            value = value.get("major")
        if isinstance(value, str):
            value = value.split(".", 1)[0]
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _socket_negotiate(self, client: socket.socket) -> None:
        hello = self._socket_request(client, "hello", {
            "client": "WatchDogsGo",
            "protocol_version": WDG_PROTOCOL_MAJOR,
            "api": {"major": WDG_PROTOCOL_MAJOR,
                    "minor": WDG_PROTOCOL_MINOR},
        })
        major = self._api_major(hello)
        if major != WDG_PROTOCOL_MAJOR:
            raise WdgVersionError(
                f"meshtasticd-wdg API major {major!r} is incompatible; "
                f"expected {WDG_PROTOCOL_MAJOR}")
        maximum = hello.get("max_packet_bytes", WDG_MAX_PACKET)
        try:
            maximum = int(maximum)
        except (TypeError, ValueError):
            maximum = 0
        if maximum < 512:
            raise WdgProtocolError("meshtasticd-wdg advertised an invalid packet limit")
        capabilities = hello.get("capabilities", [])
        self._socket_capabilities = {
            str(value) for value in capabilities if isinstance(value, str)
        } if isinstance(capabilities, list) else set()
        status = self._socket_request(client, "get_status")
        self._apply_socket_status(status)
        if (self._phone_ble_enabled is not None
                and "set_phone_ble" in self._socket_capabilities):
            try:
                self._socket_request(client, "set_phone_ble", {
                    "enabled": bool(self._phone_ble_enabled),
                    "adapter": self._phone_ble_adapter,
                })
            except (OSError, WdgProtocolError) as exc:
                # A BLE policy error must not take the LoRa node or WDG event
                # stream down.  Surface it and keep the local API connected.
                self._emit("error", "Could not apply phone BLE setting: "
                           + str(exc))

    def _socket_event_loop(self, client: socket.socket) -> None:
        while not self._stop_event.is_set():
            while True:
                try:
                    command, data = self._commands.get_nowait()
                except Empty:
                    break
                if command == "stop":
                    return
                if command == "send":
                    self._socket_command(client, "send_text", {
                        "text": data["text"],
                        "destination": data["destination"],
                        "channel": data["channel"],
                        "want_ack": True,
                    }, pending=data)
                elif command == "discover":
                    # The daemon independently enforces zero-hop discovery;
                    # including the value documents and tests the client intent.
                    self._socket_command(client, "request_node_info", {
                        "hop_limit": 0,
                    }, pending={})
                elif command == "control":
                    name, body, waiter = data
                    try:
                        self._socket_command(
                            client, name, body, pending=waiter)
                    except (OSError, WdgProtocolError) as exc:
                        waiter.reason = str(exc)
                        waiter.event.set()
            message = self._socket_receive(client)
            if message is not None:
                self._handle_socket_message(client, message)

    def _handle_socket_message(self, client: socket.socket,
                               message: dict) -> None:
        if message.get("type") == "reply":
            self._handle_socket_reply(message)
            return
        name = message.get("name", message.get("event"))
        if not isinstance(name, str) or not name:
            raise WdgProtocolError("WDG API event is missing its name")
        body = message.get("body", message.get("payload", {}))
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise WdgProtocolError(f"WDG API {name} event has a non-object body")

        if name in ("ready", "status", "radio_status"):
            if name == "radio_status" and "radio_status" not in body:
                self.radio_status = str(body.get("state") or
                                        body.get("status") or "unknown")
            self._apply_socket_status(body)
            text = body.get("message") or body.get("state")
            if text:
                self._emit("status", str(text))
        elif name == "identity":
            self._apply_socket_identity(body)
        elif name == "channels":
            self._apply_socket_channels(body)
        elif name == "snapshot_begin":
            self._snapshot_active = True
            self._snapshot_seen.clear()
            for node in self.nodes.values():
                node["cached"] = True
        elif name == "node":
            cached = self._snapshot_active or bool(body.get("cached"))
            node = self._socket_node_dict(body, cached=cached)
            if node:
                self._snapshot_seen.add(node["id"])
                self._remember_node(node, "snapshot" if cached else "update")
        elif name in ("snapshot_complete", "snapshot_end"):
            self._finish_socket_snapshot()
        elif name == "message":
            self.packets_received += 1
            self._emit("message", self._socket_message_dict(body))
        elif name in ("send_accepted", "sent"):
            request_id = str(body.get("request_id") or "")
            if not request_id or request_id not in self._completed_requests:
                self._emit("sent", self._socket_sent_dict(body))
                if request_id:
                    self._mark_request_completed(request_id)
        elif name in ("send_failed", "error"):
            text = body.get("message") or body.get("error") or name
            self._emit("error", str(text))
        elif name in ("discovery_sent", "discovery"):
            self._emit("discovery", body.get("message")
                       or "Zero-hop NodeInfo request sent")
        elif name in ("overflow", "queue_dropped"):
            count = body.get("count", body.get("dropped", "?"))
            self._emit("status", f"Meshtastic event overflow ({count}); resyncing")
            self._request_snapshot(client)
        elif name in ("phone_connected", "phone_disconnected", "ble_status",
                      "pairing_passkey"):
            if name == "phone_connected":
                self.phone_connected = True
            elif name == "phone_disconnected":
                self.phone_connected = False
            elif name == "ble_status":
                self.ble_status = str(body.get("state") or
                                      body.get("status") or "unknown")
            text = body.get("message") or name.replace("_", " ")
            self._emit("status", str(text))

    def _handle_socket_reply(self, message: dict) -> None:
        request_id = str(message.get("request_id") or "")
        pending = self._pending_requests.pop(request_id, None)
        if pending is None:
            # Synchronous handshake replies are consumed by _socket_request.
            return
        name, context = pending
        if not message.get("ok"):
            text = (message.get("message") or message.get("error_code")
                    or f"{name} failed")
            if isinstance(context, _ControlWaiter):
                context.reason = str(text)
                context.event.set()
            else:
                self._emit("error", str(text))
            if name == "snapshot_nodes":
                self._snapshot_pending = False
            return
        body = message.get("body", message.get("payload", {}))
        body = body if isinstance(body, dict) else {}
        if isinstance(context, _ControlWaiter):
            context.ok = True
            context.body = dict(body)
            context.reason = str(body.get("reason") or "")
            context.event.set()
        elif name == "send_text":
            value = dict(context)
            value.update({key: body[key] for key in ("packet_id", "id")
                          if key in body})
            self._emit("sent", value)
            self._mark_request_completed(request_id)
        elif name == "request_node_info":
            self._emit("discovery", body.get("message")
                       or "Zero-hop NodeInfo request sent")
            self._mark_request_completed(request_id)
        elif name == "snapshot_nodes" and body.get("complete"):
            self._finish_socket_snapshot()

    def _mark_request_completed(self, request_id: str) -> None:
        # IDs only need short-term de-duplication against a following event.
        if len(self._completed_requests) >= 1024:
            self._completed_requests.clear()
        self._completed_requests.add(request_id)

    def _fail_control_waiters(self, reason: str) -> None:
        for _name, context in self._pending_requests.values():
            if isinstance(context, _ControlWaiter):
                context.reason = reason
                context.event.set()

    def _request_snapshot(self, client: socket.socket) -> None:
        if self._snapshot_pending:
            return
        self._snapshot_pending = True
        self._snapshot_active = True
        self._snapshot_seen.clear()
        for node in self.nodes.values():
            node["cached"] = True
        self._socket_command(client, "snapshot_nodes", {}, pending={})

    def _finish_socket_snapshot(self) -> None:
        if self._snapshot_active:
            # A complete NodeDB snapshot is authoritative.  A live update seen
            # during the snapshot is included in _snapshot_seen as well.
            for node_id in list(self.nodes):
                if node_id not in self._snapshot_seen:
                    self.nodes.pop(node_id, None)
        self._snapshot_active = False
        self._snapshot_pending = False
        self._snapshot_seen.clear()

    def _apply_socket_status(self, body: dict) -> None:
        identity = body.get("identity")
        if isinstance(identity, dict):
            self._apply_socket_identity(identity)
        else:
            self._apply_socket_identity(body)
        channels = body.get("channels")
        if isinstance(channels, list):
            self._apply_socket_channels({"channels": channels})
        try:
            if "packets_received" in body:
                self.packets_received = max(
                    0, int(body.get("packets_received") or 0))
        except (TypeError, ValueError):
            pass
        ble = body.get("ble")
        if isinstance(ble, dict):
            self.ble_status = str(ble.get("state") or
                                  ble.get("status") or self.ble_status)
            if "phone_connected" in ble:
                self.phone_connected = bool(ble["phone_connected"])
        if "ble_status" in body:
            self.ble_status = str(body.get("ble_status") or "unknown")
        if "phone_connected" in body:
            self.phone_connected = bool(body["phone_connected"])
        radio = body.get("radio")
        if isinstance(radio, dict):
            self.radio_status = str(radio.get("state") or
                                    radio.get("status") or self.radio_status)
        if "radio_status" in body:
            self.radio_status = str(body.get("radio_status") or "unknown")

    def _apply_socket_identity(self, body: dict) -> None:
        node_id = body.get("node_id", body.get("id"))
        if node_id not in (None, ""):
            self.local_node_id = self._normalize_node_id(node_id)
        name = body.get("name", body.get("long_name"))
        if name:
            self.local_name = str(name)

    def _apply_socket_channels(self, body: dict) -> None:
        values = body.get("channels", body.get("items", []))
        if not isinstance(values, list):
            return
        channels = []
        for offset, raw in enumerate(values):
            if not isinstance(raw, dict):
                continue
            try:
                index = max(0, int(raw.get("index", offset)))
            except (TypeError, ValueError):
                continue
            role = raw.get("role")
            if role in (0, "DISABLED", "disabled"):
                continue
            channels.append({
                "index": index,
                "name": str(raw.get("name") or
                            ("Primary" if index == 0 else f"Channel {index}")),
            })
        self.channels = channels or [{"index": 0, "name": "Primary"}]

    @staticmethod
    def _normalize_node_id(value: Any) -> str:
        if isinstance(value, int):
            return f"!{value:08x}"
        text = str(value or "")
        if text.startswith("!"):
            return text.lower()
        try:
            return f"!{int(text, 0):08x}"
        except (TypeError, ValueError):
            return text

    def _socket_node_dict(self, raw: dict, *, cached: bool) -> dict | None:
        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        number = raw.get("num", raw.get("node_num"))
        ident = self._normalize_node_id(
            raw.get("id", raw.get("node_id", user.get("id", number))))
        if not ident or ident == self.local_node_id:
            return None
        position = raw.get("position") if isinstance(raw.get("position"), dict) else {}
        lat = raw.get("lat", raw.get("latitude", position.get("latitude")))
        lon = raw.get("lon", raw.get("longitude", position.get("longitude")))
        return {
            "id": ident,
            "num": number,
            "name": str(raw.get("name") or raw.get("long_name")
                        or user.get("longName") or user.get("shortName") or ident),
            "short_name": str(raw.get("short_name") or user.get("shortName") or ""),
            "hardware": str(raw.get("hardware") or user.get("hwModel") or ""),
            "lat": self._number(lat, 0),
            "lon": self._number(lon, 0),
            "rssi": self._number(raw.get("rssi"), 0),
            "snr": self._number(raw.get("snr"), 0),
            "hops": max(0, self._integer(raw.get("hops", raw.get("hops_away")), 0)),
            "last_heard": self._number(
                raw.get("last_heard", raw.get("last_seen")), time.time()),
            "cached": bool(cached),
        }

    def _socket_message_dict(self, body: dict) -> dict:
        sender_id = self._normalize_node_id(
            body.get("sender_id", body.get("from", "")))
        node = self.nodes.get(sender_id, {})
        return {
            "text": str(body.get("text") or ""),
            "sender": str(body.get("sender") or node.get("name")
                          or sender_id or "?"),
            "sender_id": sender_id,
            "channel": max(0, self._integer(body.get("channel"), 0)),
            "rssi": self._number(body.get("rssi"), 0),
            "snr": self._number(body.get("snr"), 0),
            "hops": max(0, self._integer(body.get("hops"), 0)),
        }

    @staticmethod
    def _socket_sent_dict(body: dict) -> dict:
        return {
            "text": str(body.get("text") or ""),
            "destination": body.get("destination"),
            "channel": body.get("channel", 0),
            "packet_id": body.get("packet_id", body.get("id")),
        }

    def _refresh_identity_and_channels(self) -> None:
        interface = self._interface
        if interface is None:
            return
        my_num = getattr(getattr(interface, "myInfo", None),
                         "my_node_num", None)
        if my_num is not None:
            self.local_node_id = f"!{int(my_num):08x}"
        nodes = getattr(interface, "nodesByNum", None) or {}
        own = nodes.get(my_num, {}) if my_num is not None else {}
        user = own.get("user", {}) if isinstance(own, dict) else {}
        self.local_name = (user.get("longName") or user.get("shortName")
                           or self.local_node_id or "Meshtastic")

        channels = []
        for index, channel in enumerate(
                getattr(getattr(interface, "localNode", None), "channels", []) or []):
            role = getattr(channel, "role", 0)
            # Meshtastic's DISABLED enum is zero.
            if not role:
                continue
            settings = getattr(channel, "settings", None)
            name = getattr(settings, "name", "") if settings else ""
            channels.append({"index": index,
                             "name": name or ("Primary" if index == 0
                                               else f"Channel {index}")})
        self.channels = channels or [{"index": 0, "name": "Primary"}]

    def _node_dict(self, raw: Any, *, node_id: str = "",
                   rssi: Any = None, snr: Any = None,
                   cached: bool = False) -> dict | None:
        if not isinstance(raw, dict):
            try:
                raw = dict(raw)
            except Exception:
                return None
        user = raw.get("user") or {}
        num = raw.get("num")
        ident = str(user.get("id") or node_id or "")
        if not ident and num is not None:
            ident = f"!{int(num):08x}"
        if not ident or ident == self.local_node_id:
            return None
        position = raw.get("position") or {}
        lat = position.get("latitude")
        lon = position.get("longitude")
        if lat is None and position.get("latitudeI") is not None:
            lat = float(position["latitudeI"]) * 1e-7
        if lon is None and position.get("longitudeI") is not None:
            lon = float(position["longitudeI"]) * 1e-7
        try:
            lat = float(lat or 0.0)
            lon = float(lon or 0.0)
        except (TypeError, ValueError):
            lat = lon = 0.0
        return {
            "id": ident,
            "num": num,
            "name": user.get("longName") or user.get("shortName") or ident,
            "short_name": user.get("shortName") or "",
            "hardware": user.get("hwModel") or user.get("hardwareModel") or "",
            "lat": lat,
            "lon": lon,
            "rssi": self._number(rssi, self._number(raw.get("rssi"), 0)),
            "snr": self._number(snr, self._number(raw.get("snr"), 0)),
            "hops": self._integer(raw.get("hopsAway"), 0),
            "last_heard": self._number(raw.get("lastHeard"), time.time()),
            "cached": bool(cached),
        }

    @staticmethod
    def _number(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _integer(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    def _remember_node(self, node: dict, source: str) -> None:
        existing = self.nodes.get(node["id"], {})
        merged = dict(existing)
        merged.update({key: value for key, value in node.items()
                       if value not in (None, "")})
        merged["source"] = source
        self.nodes[node["id"]] = merged
        self._emit("node", dict(merged))

    def _snapshot_nodes(self, *, cached: bool) -> None:
        interface = self._interface
        if interface is None:
            return
        for node_id, raw in (getattr(interface, "nodes", None) or {}).items():
            node = self._node_dict(raw, node_id=str(node_id), cached=cached)
            if node:
                self._remember_node(node, "cached" if cached else "snapshot")

    def _lookup_node(self, node_num: Any) -> dict | None:
        interface = self._interface
        if interface is None:
            return None
        try:
            number = int(node_num)
        except (TypeError, ValueError):
            return None
        raw = (getattr(interface, "nodesByNum", None) or {}).get(number, {})
        return self._node_dict(raw, node_id=f"!{number:08x}")

    def _on_receive(self, packet=None, interface=None, **_kwargs) -> None:
        if interface is not None and interface is not self._interface:
            return
        if not isinstance(packet, dict):
            return
        self.packets_received += 1
        node = self._lookup_node(packet.get("from"))
        if node:
            node["rssi"] = self._number(packet.get("rxRssi"), node["rssi"])
            node["snr"] = self._number(packet.get("rxSnr"), node["snr"])
            node["cached"] = False
            self._remember_node(node, "packet")
        decoded = packet.get("decoded") or {}
        text = decoded.get("text")
        if not text:
            payload = decoded.get("payload")
            if isinstance(payload, bytes):
                try:
                    text = payload.decode("utf-8")
                except UnicodeDecodeError:
                    text = ""
        portnum = str(decoded.get("portnum") or "")
        if text and (not portnum or "TEXT_MESSAGE" in portnum):
            sender = node["name"] if node else str(packet.get("from") or "?")
            self._emit("message", {
                "text": str(text), "sender": sender,
                "sender_id": node["id"] if node else "",
                "channel": self._integer(packet.get("channel"), 0),
                "rssi": self._number(packet.get("rxRssi"), 0),
                "snr": self._number(packet.get("rxSnr"), 0),
                "hops": max(0, self._integer(packet.get("hopStart"), 0)
                            - self._integer(packet.get("hopLimit"), 0)),
            })

    def _on_node(self, node=None, **_kwargs) -> None:
        if not self.connected:
            return
        normalized = self._node_dict(node, cached=False)
        if normalized:
            self._remember_node(normalized, "update")

    def _on_connection_lost(self, interface=None, **_kwargs) -> None:
        if interface is not None and interface is not self._interface:
            return
        self.connected = False
        self._emit("disconnected", "meshtasticd connection lost")

    def _send_now(self, data: dict) -> None:
        interface = self._interface
        if interface is None or not self.connected:
            self._emit("error", "Meshtastic is not connected")
            return
        kwargs = {"channelIndex": data["channel"], "wantAck": True}
        if data["destination"] not in (None, ""):
            kwargs["destinationId"] = data["destination"]
        try:
            packet = interface.sendText(data["text"], **kwargs)
            self._emit("sent", {
                "text": data["text"], "destination": data["destination"],
                "channel": data["channel"],
                "packet_id": getattr(packet, "id", None),
            })
        except Exception as exc:
            self._emit("error", f"Meshtastic send failed: {exc}")

    def _discover_now(self) -> None:
        interface = self._interface
        if interface is None or not self.connected:
            self._emit("error", "Meshtastic is not connected")
            return
        try:
            interface.sendData(
                b"", destinationId=self._broadcast_addr,
                portNum=self._portnums.PortNum.NODEINFO_APP,
                wantAck=False, wantResponse=True, hopLimit=0)
            self._emit("discovery", "Zero-hop NodeInfo request sent")
        except Exception as exc:
            self._emit("error", f"Meshtastic discovery failed: {exc}")
