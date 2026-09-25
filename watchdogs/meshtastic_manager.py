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
from collections import deque
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
LEASE_INACTIVE = "inactive"
LEASE_ACTIVE = "active"
LEASE_POSSIBLY_ACTIVE = "possibly-active"
_SERVICE_TRANSITIONAL_STATES = {
    "activating", "deactivating", "reloading", "refreshing",
}
_SERVICE_SNAPSHOT_ATTEMPTS = 20
_SERVICE_SNAPSHOT_POLL_SECONDS = 0.05


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
    deadline: float = 0.0
    sent: bool = False
    completed: bool = False
    cancelled: bool = False
    indeterminate: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def begin_send(self, now: float) -> bool:
        """Claim the right to send unless the caller already timed out."""
        with self._lock:
            if self.cancelled or self.completed or now >= self.deadline:
                self.cancelled = True
                self.reason = self.reason or "Control command expired before send"
                self.event.set()
                return False
            self.sent = True
            return True

    def resolve(self, ok: bool, reason: str = "",
                body: dict | None = None) -> bool:
        """Publish one result unless the caller already gave up.

        A late reply is consumed silently.  In particular, it must not turn an
        already reported indeterminate timeout into a later definitive error.
        """
        with self._lock:
            if self.cancelled or self.completed:
                return False
            self.ok = bool(ok)
            self.reason = str(reason or "")
            self.body = dict(body or {})
            self.completed = True
            self.event.set()
            return True

    def timeout(self, name: str) -> tuple[bool, str, dict] | None:
        """Cancel an unsent command or mark a sent result indeterminate.

        ``None`` means a reply won the deadline race and the caller should
        consume the resolved values instead.
        """
        with self._lock:
            if self.completed:
                return None
            self.cancelled = True
            if self.sent:
                self.indeterminate = True
                self.reason = (
                    f"Timed out waiting for Meshtastic {name} reply; "
                    "the daemon may have completed the request")
                body = {"_indeterminate": True, "_sent": True}
            else:
                self.reason = (
                    f"Timed out before Meshtastic {name} was sent; "
                    "the request was cancelled")
                body = {"_indeterminate": False, "_sent": False}
            self.event.set()
            return False, self.reason, body

    def result(self) -> tuple[bool, str, dict]:
        with self._lock:
            return self.ok, self.reason, dict(self.body)


@dataclass(frozen=True)
class _ServiceSnapshot:
    """Exact systemd state needed to reverse a radio-owner transition."""

    target: str
    load_state: str
    active_state: str
    unit_file_state: str

    @property
    def installed(self) -> bool:
        return self.load_state == "loaded"

    @property
    def active(self) -> bool:
        return self.active_state == "active"

    @property
    def enabled(self) -> bool:
        # ``enabled-runtime`` is just as capable of starting the daemon for
        # the current boot.  Treating it as disabled can let WDG hand the
        # SX1262 to a direct client while systemd still owns an activation
        # path for the daemon.
        return self.unit_file_state in ("enabled", "enabled-runtime")

    @property
    def exactly_restorable(self) -> bool:
        """Whether WDG's narrow helper can reproduce this boot-time state.

        The privileged helper intentionally exposes only enable/disable.  It
        cannot safely reconstruct runtime enables, masks, or links.  Those
        states must therefore stop a transition before either service is
        mutated instead of being flattened to ``disabled``.
        """
        return (
            (self.load_state == "loaded"
             and self.active_state in ("active", "inactive")
             and self.unit_file_state in ("enabled", "disabled"))
            or (self.load_state == "not-found"
                and self.active_state == "inactive"
                and self.unit_file_state == "not-found")
        )

    @property
    def transitional(self) -> bool:
        return self.active_state in _SERVICE_TRANSITIONAL_STATES

    def describe(self) -> str:
        return (f"{self.load_state}/{self.active_state}/"
                f"{self.unit_file_state}")


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
        operation_guard: Callable[[], str] | None = None,
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
        self.pairing_pin = ""
        self.host_ble_degraded = False
        self.host_ble_pause_reason = ""
        self.radio_status = "unknown"
        self.full_client_owner = "unknown"
        # Service lifetime and client transport lifetime are deliberately
        # distinct.  A daemon can remain active while its local socket is
        # restarting, so the UI must not infer systemd state from connected.
        self._service_states = {"wdg": "unknown", "stock": "unknown"}
        self.last_error = ""

        self._dependency_loader = dependency_loader
        self._port_probe = port_probe
        self._service_runner = service_runner
        self._service_controller = service_controller
        self._sleep = sleep
        self._socket_probe = socket_probe
        self._socket_connector = socket_connector
        self._monotonic = monotonic
        self._operation_guard = operation_guard
        self._interface = None
        self._socket: socket.socket | None = None
        self._pub = None
        self._portnums = None
        self._broadcast_addr = "^all"
        self._commands: Queue = Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._fork_connected_event = threading.Event()
        self._connected_event = threading.Event()
        self._lock = threading.Lock()
        self._closing = False
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
        self.phone_ble_enabled_applied: bool | None = None
        self.phone_ble_adapter_applied = ""
        self._ble_scan_lease_state = LEASE_INACTIVE
        self._ble_scan_lease_owner = ""
        self._ble_scan_lease_lock = threading.Lock()
        self._pairing_agent_lease_state = LEASE_INACTIVE
        self._control_timeout = 4.0
        self._phone_scan_disconnects = deque(maxlen=8)
        # A direct-radio handoff must restore the unit that was actually
        # active, even when the saved backend preference points elsewhere.
        self._suspended_service_target: str | None = None
        self._suspended_service_snapshot: dict[str, _ServiceSnapshot] | None = None
        self._backend_service_rollback: dict[str, _ServiceSnapshot] | None = None
        self._resume_backend_once = ""
        self.started_daemon = False

    def _emit(self, kind: str, value: Any) -> None:
        if kind == "error":
            self.last_error = str(value)
        self.queue.put((kind, value))

    def _operation_blocked(self) -> bool:
        if self._operation_guard is None:
            return False
        reason = str(self._operation_guard() or "").strip()
        if not reason:
            return False
        self._emit("error", reason)
        return True

    @property
    def backend(self) -> str:
        """The active backend, or configured backend before startup."""
        return self.active_backend or self.backend_mode

    @property
    def ble_scan_lease_active(self) -> bool:
        """Whether a daemon scan lease is active or may be active."""
        return self._ble_scan_lease_state != LEASE_INACTIVE

    @property
    def ble_scan_lease_state(self) -> str:
        """Confirmed or conservative state of the daemon scan lease."""
        return self._ble_scan_lease_state

    @property
    def ble_scan_lease_owner(self) -> str:
        return self._ble_scan_lease_owner

    @property
    def _ble_scan_lease_active(self) -> bool:
        """Compatibility alias for older tests and UI integrations."""
        return self._ble_scan_lease_state != LEASE_INACTIVE

    @_ble_scan_lease_active.setter
    def _ble_scan_lease_active(self, value: bool) -> None:
        self._ble_scan_lease_state = (
            LEASE_ACTIVE if value else LEASE_INACTIVE)

    @property
    def pairing_agent_lease_active(self) -> bool:
        """Whether the daemon agent lease is active or may be active."""
        return self._pairing_agent_lease_state != LEASE_INACTIVE

    @property
    def pairing_agent_lease_state(self) -> str:
        """Confirmed or conservative state of the daemon agent lease."""
        return self._pairing_agent_lease_state

    @property
    def _pairing_agent_lease_active(self) -> bool:
        """Compatibility alias for older tests and UI integrations."""
        return self._pairing_agent_lease_state != LEASE_INACTIVE

    @_pairing_agent_lease_active.setter
    def _pairing_agent_lease_active(self, value: bool) -> None:
        self._pairing_agent_lease_state = (
            LEASE_ACTIVE if value else LEASE_INACTIVE)

    @property
    def service_restore_pending(self) -> bool:
        """Whether an exact pre-handoff systemd state still needs restoring.

        This is an ownership barrier, not merely cached status.  Callers must
        retry :meth:`resume_service` and must not select or start another
        daemon while the snapshot is present.
        """
        return self._suspended_service_snapshot is not None

    def _service_restore_blocks(self, operation: str) -> bool:
        """Fail closed while an earlier handoff restore is unresolved."""
        if not self.service_restore_pending:
            return False
        self._emit(
            "error",
            "Previous Meshtastic service state is still unresolved; retry "
            "resume_service before " + operation,
        )
        return True

    def set_backend_mode(self, mode: str) -> bool:
        """Select the transport for the next connection.

        A live transport is never switched underneath callbacks.  Callers may
        close the manager first, then apply a setting and start it again.
        """
        if mode not in BACKEND_MODES:
            raise ValueError("Unsupported Meshtastic backend: " + str(mode))
        if self._operation_blocked():
            return False
        with self._lock:
            if self.running or self._closing:
                return False
            self.backend_mode = mode
            self.active_backend = ""
            self._fork_identified = False
        return True

    def activate_backend_service(self, mode: str,
                                 *, timeout: float = 15.0) -> bool:
        """Make the selected daemon the exact live and boot-time radio owner.

        This is intentionally separate from ``set_backend_mode`` so ordinary
        client construction remains read-only. The settings UI calls it only
        during an explicit backend transition after disconnecting WDG.
        """
        if mode not in BACKEND_MODES:
            raise ValueError("Unsupported Meshtastic backend: " + str(mode))
        if self._operation_blocked():
            return False
        if self._service_restore_blocks("selecting another service"):
            return False
        if self._backend_service_rollback is not None:
            self._emit(
                "error",
                "A previous Meshtastic service selection is still pending; "
                "commit or roll it back before selecting another service",
            )
            return False
        previous: dict[str, _ServiceSnapshot] | None = None
        previous_target: str | None = None
        restored = False
        try:
            if self._service_controller is not None:
                self._service_controller.require_current()
            previous = self._capture_service_snapshot()
            if not self._service_snapshot_is_restorable(
                    previous, operation="switch"):
                return False
            previous_target = self._snapshot_selected_target(previous)
            fork_load = previous["wdg"].load_state
            if mode == "fork_socket" and fork_load == "not-found":
                self._emit("error", "meshtasticd-wdg is not installed")
                return False
            target = (
                "stock" if mode == "legacy_tcp"
                or (mode == "auto" and fork_load == "not-found")
                else "wdg")
            if not previous[target].installed:
                self._emit("error", "Selected Meshtastic service is not installed")
                return False
            self._stop_event.clear()
            if not self._select_service_target(target):
                raise RuntimeError("selected Meshtastic service did not activate")
            deadline = self._monotonic() + max(0.0, float(timeout))
            while (not self._service_target_ready(target)
                   and self._monotonic() < deadline):
                self._sleep(0.1)
            if not self._service_target_ready(target):
                raise RuntimeError(
                    "selected Meshtastic service endpoint did not become ready")
            if target == "stock":
                self.host_ble_degraded = False
                self.host_ble_pause_reason = ""
                self._phone_scan_disconnects.clear()
            self._resume_backend_once = ""
            self._backend_service_rollback = previous
            self._emit("status", "Selected Meshtastic service: " + target)
            return True
        except Exception as exc:
            detail = "Could not switch Meshtastic service: " + str(exc)[:160]
            if previous is not None:
                restored = self._restore_service_snapshot(
                    previous, timeout=max(1.0, float(timeout)))
                if restored:
                    suffix = ("previous " + previous_target + " service restored"
                              if previous_target else
                              "previous service state restored")
                    detail += "; " + suffix
                else:
                    detail += "; previous service state could not be restored"
            self._backend_service_rollback = (
                previous if previous is not None and not restored else None)
            self._emit("error", detail)
            return False

    def commit_backend_service_activation(self) -> None:
        """Discard rollback state after transport negotiation succeeds."""
        self._backend_service_rollback = None

    def rollback_backend_service_activation(self,
                                            *, timeout: float = 15.0) -> bool:
        """Restore the exact state captured before the latest selection."""
        if self._service_restore_blocks("rolling back another service change"):
            return False
        snapshot = self._backend_service_rollback
        if snapshot is None:
            return True
        restored = self._restore_service_snapshot(snapshot, timeout=timeout)
        if restored:
            self._backend_service_rollback = None
        return restored

    def start(self) -> bool:
        """Start the local daemon if needed and connect in a worker thread."""
        if self._operation_blocked():
            return False
        if self._service_restore_blocks("starting a daemon"):
            return False
        with self._lock:
            if self._closing:
                self._emit(
                    "error",
                    "Meshtastic client close is still in progress; start was "
                    "not accepted",
                )
                return False
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
            self._fork_connected_event.clear()
            self._connected_event.clear()
            self.active_backend = ""
            self._pending_requests.clear()
            self._completed_requests.clear()
            self._snapshot_pending = False
            self._snapshot_active = False
            self._snapshot_seen.clear()
            self._reset_applied_phone_ble()
            self._stop_event.clear()
            try:
                thread = threading.Thread(
                    target=self._run, name="wdg-meshtastic", daemon=True)
                self._thread = thread
                thread.start()
            except Exception as exc:
                self._thread = None
                self.running = False
                self._stop_event.set()
                self._emit(
                    "error", "Could not start Meshtastic client worker: "
                    + str(exc)[:160])
                return False
        return True

    def close(self, *, stop_daemon: bool = False) -> bool:
        """Disconnect WDG and report whether its worker actually stopped.

        Service ownership must never change while the old client worker can
        still issue requests.  Callers that intend to switch or update a
        daemon therefore use this return value as a hard lifecycle barrier.
        """
        with self._lock:
            if self._closing:
                self._emit(
                    "error", "Meshtastic client close is already in progress")
                return False
            self._closing = True
        try:
            lease_owner = ""
            with self._ble_scan_lease_lock:
                if self._ble_scan_lease_active:
                    lease_owner = self._ble_scan_lease_owner
            if lease_owner:
                # Release while the socket worker can still service the command.
                # A failed release deliberately preserves the owner locally.
                self.release_ble_scan_lease(owner=lease_owner)
            if self.pairing_agent_lease_active:
                # A timed-out acquisition is still possibly active. Attempt
                # the idempotent release before ending the socket session;
                # closing the session below is the final cleanup barrier.
                self.release_pairing_agent_lease()
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
            stopped = not thread or not thread.is_alive()
            if stopped:
                self.running = False
                self._thread = None
            else:
                self._emit(
                    "error",
                    "Meshtastic client worker did not stop; service operation "
                    "was aborted",
                )
            if not stopped:
                self.connected = False
                self._fork_connected_event.clear()
                self._connected_event.clear()
                return False
            self._reset_socket_session_state()
            if stop_daemon:
                if not self._stop_service():
                    return False
                # Stopping both possible owners definitively invalidates any
                # daemon-side bounded lease whose release reply was lost.
                self._reset_local_leases()
            return True
        finally:
            with self._lock:
                self._closing = False

    stop = close

    def wait_connected(self, timeout: float = 10.0,
                       *, backend: str | None = None) -> bool:
        """Wait for complete transport negotiation, not just an open endpoint."""
        expected = backend
        if expected == "auto":
            expected = None
        deadline = self._monotonic() + max(0.0, float(timeout))
        while True:
            if self.connected:
                return expected is None or self.active_backend == expected
            thread = self._thread
            if (not self.running
                    and (thread is None or not thread.is_alive())):
                return False
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return False
            self._connected_event.wait(min(0.1, remaining))

    def _reset_local_leases(self) -> None:
        """Forget daemon-scoped leases whenever their socket session ends."""
        with self._ble_scan_lease_lock:
            self._ble_scan_lease_state = LEASE_INACTIVE
            self._ble_scan_lease_owner = ""
        self._pairing_agent_lease_state = LEASE_INACTIVE

    def _reset_socket_session_state(self) -> None:
        """Clear state that is authoritative only for one socket session."""
        self.connected = False
        self._fork_connected_event.clear()
        self._connected_event.clear()
        self.ble_status = "unknown"
        self.phone_connected = False
        self.pairing_pin = ""
        self.radio_status = "unknown"
        self.full_client_owner = "unknown"
        self.host_ble_degraded = False
        self.host_ble_pause_reason = ""
        self._phone_scan_disconnects.clear()
        self._socket_capabilities.clear()
        self._reset_applied_phone_ble()
        self._reset_local_leases()
        self._snapshot_pending = False
        self._snapshot_active = False
        self._snapshot_seen.clear()

    @property
    def service_state(self) -> str:
        """Cached systemd state for the daemon selected by this manager."""
        backend = self.active_backend or self.backend_mode
        target = "stock" if backend == "legacy_tcp" else "wdg"
        return self._service_states.get(target, "unknown")

    def _remember_service_state(
            self, target: str, load_state: str, active_state: str) -> None:
        if target not in self._service_states:
            return
        load = str(load_state or "unknown").lower()
        active = str(active_state or "unknown").lower()
        if load == "not-found":
            state = "not installed"
        elif active in ("active", "activating", "reloading"):
            state = active
        elif active in ("inactive", "failed", "deactivating"):
            state = active
        else:
            state = "unknown"
        self._service_states[target] = state

    def _control_backend(self) -> str:
        if self.active_backend:
            return self.active_backend
        return self._select_backend()

    def service_ready(self) -> bool:
        """Read readiness for the selected exact daemon without changing it."""
        if self._suspended_service_target:
            return self._service_target_ready(self._suspended_service_target)
        backend = self._control_backend()
        target = "wdg" if backend == "fork_socket" else "stock"
        return self._service_target_ready(target)

    def _retry_suspended_restore(
            self, snapshot: dict[str, _ServiceSnapshot],
            *, timeout: float) -> bool:
        """Restore and clear only the exact retained handoff snapshot."""
        if self._suspended_service_snapshot is not snapshot:
            self._emit(
                "error",
                "Meshtastic restore token changed unexpectedly; service "
                "ownership remains unresolved",
            )
            return False
        previous_error = self.last_error
        try:
            restored = bool(self._restore_service_snapshot(
                snapshot, timeout=timeout))
        except Exception as exc:
            self._emit(
                "error", "Could not restore Meshtastic service state: "
                + str(exc)[:160])
            return False
        if not restored and self.last_error == previous_error:
            self._emit(
                "error", "Previous Meshtastic service state did not restore")
        if restored:
            self._suspended_service_target = None
            self._suspended_service_snapshot = None
        return restored

    def suspend_service(self, timeout: float = 10.0) -> bool:
        """Stop and disable both daemons while preserving exact prior state."""
        # A retained snapshot is the only trustworthy rollback token after a
        # partial handoff.  Never replace it with a snapshot of the already
        # mutated state.
        if self._service_restore_blocks("suspending services again"):
            return False
        try:
            snapshot = self._capture_service_snapshot()
        except Exception as exc:
            self._emit("error", "Could not snapshot Meshtastic services: "
                       + str(exc)[:160])
            return False
        if not self._service_snapshot_is_restorable(
                snapshot, operation="suspend"):
            return False
        actual_target = self._snapshot_selected_target(snapshot)
        if not self.close():
            self._emit(
                "error", "Meshtastic client worker is still running; "
                "radio handoff aborted")
            return False
        self._suspended_service_target = actual_target
        self._suspended_service_snapshot = snapshot
        try:
            stopped = self._stop_service()
        except Exception as exc:
            stopped = False
            self._emit(
                "error", "Could not stop Meshtastic services: "
                + str(exc)[:160])
        if not stopped:
            restored = self._retry_suspended_restore(
                snapshot, timeout=timeout)
            if not restored:
                self._emit(
                    "error",
                    "Meshtastic service stop failed and the previous state "
                    "is still unresolved; retry resume_service",
                )
            return False
        try:
            for target in ("wdg", "stock"):
                state = snapshot[target]
                if (state.installed and state.unit_file_state == "enabled"
                        and not self._set_service_enabled(target, False)):
                    raise RuntimeError(
                        "could not disable " + target + " service")
        except Exception as exc:
            restored = self._retry_suspended_restore(
                snapshot, timeout=timeout)
            detail = "Could not suspend Meshtastic persistently: " + str(exc)
            if not restored:
                detail += "; previous service state could not be restored"
            self._emit("error", detail[:240])
            return False
        deadline = self._monotonic() + max(0.0, timeout)
        while (not self._services_persistently_suspended()
               and self._monotonic() < deadline):
            self._sleep(0.1)
        if not self._services_persistently_suspended():
            self._emit(
                "error", "Meshtastic service remained active or enabled")
            restored = self._retry_suspended_restore(
                snapshot, timeout=timeout)
            if not restored:
                self._emit(
                    "error",
                    "Previous Meshtastic service state is still unresolved; "
                    "retry resume_service",
                )
            return False
        return True

    def resume_service(self, timeout: float = 15.0, *,
                       connect: bool = True,
                       target: str | None = None) -> bool:
        """Restore the suspended unit, wait for readiness, then reconnect."""
        if self._operation_blocked():
            return False
        snapshot = self._suspended_service_snapshot
        target = target or self._suspended_service_target
        if target not in (None, "wdg", "stock"):
            raise ValueError("Unsupported Meshtastic service target: "
                             + str(target))
        backend = (
            "fork_socket" if target == "wdg" else
            "legacy_tcp" if target == "stock" else
            self._control_backend()
        )
        target = target or ("wdg" if backend == "fork_socket" else "stock")
        # suspend_service() intentionally closes the client and sets this
        # lifecycle flag.  Service startup has its own bounded readiness wait,
        # so clear the old client cancellation before polling.  start() will
        # still drain the previous stop command before creating a new worker.
        self._stop_event.clear()
        if snapshot is not None:
            if not self._retry_suspended_restore(
                    snapshot, timeout=timeout):
                return False
            active_target = next(
                (candidate for candidate in ("wdg", "stock")
                 if snapshot[candidate].active), None)
            # A previously enabled but inactive daemon must stay inactive.
            should_connect = bool(connect and active_target)
            if active_target:
                target = active_target
                backend = "fork_socket" if target == "wdg" else "legacy_tcp"
            # Exact service ownership is restored and the helper cleared the
            # recovery token.  A later transport failure must not masquerade
            # as an unresolved systemd rollback.
        else:
            ready = (self._ensure_fork_service() if backend == "fork_socket"
                     else self._ensure_legacy_service())
            if not ready:
                return False
            deadline = self._monotonic() + max(0.0, timeout)
            while (not self._service_target_ready(target)
                   and self._monotonic() < deadline):
                self._sleep(0.1)
            if not self._service_target_ready(target):
                self._emit("error", "Meshtastic daemon did not become ready")
                return False
            should_connect = connect
        if should_connect:
            self._resume_backend_once = backend
            if not self.start():
                return False
            if not self.wait_connected(timeout=timeout, backend=backend):
                self._emit(
                    "error", "Meshtastic daemon endpoint opened but protocol "
                    "negotiation did not complete")
                self.close()
                return False
        return True

    def _services_persistently_suspended(self) -> bool:
        for target in ("wdg", "stock"):
            state = self._service_snapshot(target)
            if not self._service_is_stopped(
                    state.load_state, state.active_state):
                return False
            # Only an exact disabled state is safe for the direct-radio
            # lifetime.  Runtime enables, links, masks, aliases and unknown
            # states are not interchangeable with disabled.
            if state.installed and state.unit_file_state != "disabled":
                return False
        return True

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
                         *, timeout: float | None = None,
                         ) -> tuple[bool, str, dict]:
        """Run one restricted fork command and wait for its correlated reply."""
        if self.active_backend != "fork_socket" or not self.connected:
            reason = "Meshtastic WDG socket is not connected"
            self._emit("error", reason)
            return False, reason, {}
        if threading.current_thread() is self._thread:
            reason = "Meshtastic control command cannot block its worker"
            self._emit("error", reason)
            return False, reason, {}
        timeout = max(
            0.01,
            float(self._control_timeout if timeout is None else timeout),
        )
        waiter = _ControlWaiter(deadline=self._monotonic() + timeout)
        self._commands.put(("control", (name, dict(body or {}), waiter)))
        if not waiter.event.wait(timeout):
            timed_out = waiter.timeout(name)
            if timed_out is not None:
                self._emit("error", timed_out[1])
                return timed_out
        ok, reason, result = waiter.result()
        if not ok:
            self._emit("error", reason or f"Meshtastic {name} failed")
        return ok, reason, result

    def configure_phone_ble(self, enabled: bool, adapter: str = "auto") -> None:
        """Store desired phone BLE policy for this and future connections."""
        self._phone_ble_enabled = bool(enabled)
        self._phone_ble_adapter = str(adapter or "auto")
        if not self._phone_ble_enabled:
            self.host_ble_degraded = False
            self.host_ble_pause_reason = ""
            self._phone_scan_disconnects.clear()

    def apply_phone_ble(self) -> bool:
        """Apply the latest desired policy when the fork socket is connected."""
        with self._ble_scan_lease_lock:
            if self._ble_scan_lease_active:
                self._emit(
                    "error",
                    "Finish the active host BLE scan before changing phone BLE")
                return False
            if self.active_backend != "fork_socket" or not self.connected:
                return True
            ok, _reason, body = self._control_command("set_phone_ble", {
                "enabled": bool(self._phone_ble_enabled),
                "adapter": self._phone_ble_adapter,
            })
            if ok:
                self._record_applied_phone_ble(body)
            if ok and not self._phone_ble_enabled:
                self.host_ble_degraded = False
                self.host_ble_pause_reason = ""
                self._phone_scan_disconnects.clear()
            return ok

    def _record_applied_phone_ble(self, body: dict) -> None:
        if "phone_ble_enabled" in body:
            self.phone_ble_enabled_applied = bool(body["phone_ble_enabled"])
        elif "enabled" in body:
            self.phone_ble_enabled_applied = bool(body["enabled"])
        if "phone_ble_adapter_address" in body:
            address = body.get("phone_ble_adapter_address")
            self.phone_ble_adapter_applied = str(address or "").upper()
        elif "adapter_address" in body:
            address = body.get("adapter_address")
            self.phone_ble_adapter_applied = str(address or "").upper()

    def _reset_applied_phone_ble(self) -> None:
        """Forget live policy until the current daemon reports it again."""
        self.phone_ble_enabled_applied = None
        self.phone_ble_adapter_applied = ""
        self.full_client_owner = "unknown"

    def _record_full_client_owner(self, body: dict) -> None:
        value = body.get("full_client_owner")
        nested = body.get("full_client")
        if value is None and isinstance(nested, dict):
            value = nested.get("owner")
        if value is None:
            value = body.get("owner")
        owner = str(value or "").strip().lower()
        if owner in {"none", "bluetooth", "bluetooth_pending", "tcp"}:
            self.full_client_owner = owner

    def host_ble_requires_lease(self, host_adapter: str = "auto") -> bool:
        """Fail closed unless the live daemon confirms a separate adapter."""
        if self.active_backend != "fork_socket" or not self.connected:
            return True
        if self.phone_ble_enabled_applied is False:
            return False
        host = str(host_adapter or "auto").upper()
        phone = str(self.phone_ble_adapter_applied or "").upper()
        if (self.phone_ble_enabled_applied is True
                and host != "AUTO" and phone and host != phone):
            return False
        return True

    def set_phone_ble(self, enabled: bool, adapter: str = "auto") -> bool:
        """Persist desired phone BLE policy and apply it when possible."""
        with self._ble_scan_lease_lock:
            if self._ble_scan_lease_active:
                self._emit(
                    "error",
                    "Finish the active host BLE scan before changing phone BLE")
                return False
            self.configure_phone_ble(enabled, adapter)
            # Keep the lease lock through the daemon command so a scanner
            # cannot acquire the adapter between policy validation and apply.
            if self.active_backend != "fork_socket" or not self.connected:
                return True
            ok, _reason, body = self._control_command("set_phone_ble", {
                "enabled": bool(self._phone_ble_enabled),
                "adapter": self._phone_ble_adapter,
            })
            if ok:
                self._record_applied_phone_ble(body)
            if ok and not self._phone_ble_enabled:
                self.host_ble_degraded = False
                self.host_ble_pause_reason = ""
                self._phone_scan_disconnects.clear()
            return ok

    def open_pairing(self, seconds: int = 120) -> bool:
        """Open a bounded Meshtastic phone pairing window."""
        seconds = max(1, min(120, int(seconds)))
        # A passkey event may arrive before the correlated command reply.
        # Clear the previous PIN first so the new asynchronous value survives.
        self.pairing_pin = ""
        ok, _reason, _body = self._control_command(
            "open_pairing", {"seconds": seconds})
        return ok

    def forget_phone(self) -> bool:
        ok, _reason, _body = self._control_command("forget_phone")
        return ok

    def acquire_ble_scan_lease(
            self, seconds: int = 20, *, owner: str = "default",
    ) -> tuple[bool, str]:
        """Ask the daemon to yield one adapter for a bounded host BLE scan."""
        owner = str(owner or "default")[:64]
        seconds = max(1, min(20, int(seconds)))

        # Connecting the restricted socket can start the manager and its
        # worker.  That worker retires stale local leases while establishing a
        # new session, so waiting for it while holding the lease mutex can
        # deadlock a stopped manager.  Establish coordination first, then
        # serialize and revalidate every fact used to issue the command.
        if (self._fork_may_own_resources()
                and not self.ensure_ble_coordination(timeout=5.0)):
            return False, "Meshtastic WDG socket is not connected"

        with self._ble_scan_lease_lock:
            held_by_owner = bool(
                self._ble_scan_lease_state == LEASE_ACTIVE
                and self._ble_scan_lease_owner == owner)
            if (self._ble_scan_lease_state != LEASE_INACTIVE
                    and self._ble_scan_lease_owner != owner):
                return False, (
                    "Bluetooth scanning is already leased to "
                    + self._ble_scan_lease_owner)
            if self._ble_scan_lease_state == LEASE_POSSIBLY_ACTIVE:
                # A prior acquire/release reached the daemon but its reply was
                # lost.  Retire that exact token before another acquisition.
                if not self._release_ble_scan_lease_locked():
                    return False, (
                        "A previous Bluetooth scan lease may still be active "
                        "for " + self._ble_scan_lease_owner)
                held_by_owner = False
            fork_active = self._fork_may_own_resources()
            if not fork_active:
                # A renewal can observe the daemon disappearing before the
                # scanner reaches its finally block. Preserve that scanner's
                # token; only its matching release may retire local ownership.
                if not held_by_owner:
                    self._ble_scan_lease_state = LEASE_INACTIVE
                    self._ble_scan_lease_owner = ""
                return True, "meshtasticd-wdg is not active"
            if self.host_ble_degraded:
                return False, (self.host_ble_pause_reason
                               or "Meshtastic phone has Bluetooth priority")
            # Coordination can disappear after the preflight wait.  Never
            # send against a replacement or half-closed session.
            if self.active_backend != "fork_socket" or not self.connected:
                return False, "Meshtastic WDG socket is not connected"
            ok, reason, body = self._control_command(
                "ble_scan_lease_acquire", {"seconds": seconds})
            indeterminate = bool(body.get("_indeterminate"))
            if indeterminate:
                self._ble_scan_lease_state = LEASE_POSSIBLY_ACTIVE
                self._ble_scan_lease_owner = owner
                # The command was sent, so a false return is not proof that
                # the daemon kept its adapter. Send an ordered compensating
                # release; preserve possibly-active if that reply is lost too.
                cleanup_ok = self._release_ble_scan_lease_locked()
                if not cleanup_ok:
                    reason = (str(reason or "scan lease reply timed out")
                              + "; lease may still be active because the "
                              "compensating release is unconfirmed")
                return False, str(reason)
            granted = bool(body.get("granted", ok)) if ok else False
            reason = str(body.get("reason") or reason or
                         ("granted" if granted else "scan lease denied"))
            if granted:
                self._ble_scan_lease_state = LEASE_ACTIVE
                self._ble_scan_lease_owner = owner
                # A phone-priority event can arrive while the command reply is
                # in flight. Do not let the scanner start from that stale
                # grant; return the lease immediately. If the release reply is
                # lost, preserve its owner until an explicit later release.
                if self.host_ble_degraded:
                    degraded_reason = (
                        self.host_ble_pause_reason
                        or "Meshtastic phone has Bluetooth priority")
                    self._release_ble_scan_lease_locked()
                    return False, degraded_reason
            elif not held_by_owner:
                self._ble_scan_lease_state = LEASE_INACTIVE
                self._ble_scan_lease_owner = ""
            return granted, reason

    def release_ble_scan_lease(self, *, owner: str = "default") -> bool:
        owner = str(owner or "default")[:64]
        with self._ble_scan_lease_lock:
            if (self._ble_scan_lease_state != LEASE_INACTIVE
                    and self._ble_scan_lease_owner != owner):
                # A second scanner must never release the first scanner's
                # daemon lease.
                return True
            return self._release_ble_scan_lease_locked()

    def _release_ble_scan_lease_locked(self) -> bool:
        """Release the current daemon lease with ``_ble_scan_lease_lock`` held."""
        if self._ble_scan_lease_state == LEASE_INACTIVE:
            return True
        if not self._fork_may_own_resources():
            self._ble_scan_lease_state = LEASE_INACTIVE
            self._ble_scan_lease_owner = ""
            return True
        if self.active_backend != "fork_socket" or not self.connected:
            return False
        ok, _reason, body = self._control_command(
            "ble_scan_lease_release")
        if ok:
            self._ble_scan_lease_state = LEASE_INACTIVE
            self._ble_scan_lease_owner = ""
        elif body.get("_indeterminate"):
            self._ble_scan_lease_state = LEASE_POSSIBLY_ACTIVE
        return ok

    def acquire_pairing_agent_lease(self, seconds: int = 120) -> bool:
        """Yield the daemon's default BlueZ agent for another bounded flow."""
        if not self._fork_may_own_resources():
            self._pairing_agent_lease_state = LEASE_INACTIVE
            return True
        if not self.ensure_ble_coordination(timeout=5.0):
            return False
        if (self._pairing_agent_lease_state == LEASE_POSSIBLY_ACTIVE
                and not self.release_pairing_agent_lease()):
            return False
        seconds = max(1, min(120, int(seconds)))
        was_active = self._pairing_agent_lease_state == LEASE_ACTIVE
        ok, _reason, body = self._control_command(
            "pairing_agent_lease_acquire", {"seconds": seconds})
        if ok:
            self._pairing_agent_lease_state = LEASE_ACTIVE
        elif body.get("_indeterminate"):
            self._pairing_agent_lease_state = LEASE_POSSIBLY_ACTIVE
            # A caller must not register a competing agent without a grant,
            # but the daemon may have yielded its agent. Compensate now; an
            # unconfirmed release remains visible and retryable.
            self.release_pairing_agent_lease()
        elif not was_active:
            self._pairing_agent_lease_state = LEASE_INACTIVE
        return ok

    def release_pairing_agent_lease(self) -> bool:
        if self._pairing_agent_lease_state == LEASE_INACTIVE:
            return True
        if not self._fork_may_own_resources():
            self._pairing_agent_lease_state = LEASE_INACTIVE
            return True
        if self.active_backend != "fork_socket" or not self.connected:
            return False
        ok, _reason, body = self._control_command(
            "pairing_agent_lease_release")
        if ok:
            self._pairing_agent_lease_state = LEASE_INACTIVE
        elif body.get("_indeterminate"):
            self._pairing_agent_lease_state = LEASE_POSSIBLY_ACTIVE
        return ok

    def retry_shared_adapter(self) -> bool:
        with self._ble_scan_lease_lock:
            if self._ble_scan_lease_active:
                self._emit(
                    "error",
                    "Finish the active host BLE scan before retrying the adapter")
                return False
            if not self._fork_may_own_resources():
                ok = True
            elif self.active_backend != "fork_socket" or not self.connected:
                self._emit("error", "Meshtastic WDG socket is not connected")
                return False
            elif "retry_shared_adapter" in self._socket_capabilities:
                ok, _reason, _body = self._control_command(
                    "retry_shared_adapter")
            else:
                ok = True
            if ok:
                self.host_ble_degraded = False
                self.host_ble_pause_reason = ""
                self._phone_scan_disconnects.clear()
            return ok

    def note_host_ble_failure(self, reason: str) -> None:
        """Preserve a connected phone after BlueZ rejects shared discovery."""
        if not self.phone_connected:
            return
        self.host_ble_degraded = True
        self.host_ble_pause_reason = str(
            reason or "BlueZ rejected scanning beside the connected phone")

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
        return self._fork_service_state()[0]

    def _fork_service_state(self) -> tuple[str, str]:
        """Return exact load/active state for the fork service."""
        if self._service_controller is not None:
            try:
                status = self._service_controller.status("wdg")
                value = (
                    str(status.load_state).lower(),
                    str(status.active_state).lower(),
                )
                self._remember_service_state("wdg", *value)
                return value
            except Exception:
                self._remember_service_state("wdg", "unknown", "unknown")
                return "unknown", "unknown"
        try:
            result = self._service_runner(
                ["systemctl", "show", WDG_SERVICE,
                 "--property=LoadState", "--property=ActiveState"],
                capture_output=True, text=True, timeout=12)
        except Exception:
            self._remember_service_state("wdg", "unknown", "unknown")
            return "unknown", "unknown"
        if result.returncode:
            self._remember_service_state("wdg", "unknown", "unknown")
            return "unknown", "unknown"
        properties = {}
        for line in (result.stdout or "").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                properties[key.strip()] = value.strip().lower()
        value = (
            properties.get("LoadState", "unknown"),
            properties.get("ActiveState", "unknown"),
        )
        self._remember_service_state("wdg", *value)
        return value

    def _service_snapshot(self, target: str) -> _ServiceSnapshot:
        if target not in ("wdg", "stock"):
            raise ValueError("Unsupported Meshtastic service target: " + target)
        if self._service_controller is not None:
            status = self._service_controller.status(target)
            snapshot = _ServiceSnapshot(
                target=target,
                load_state=str(
                    getattr(status, "load_state", "unknown")).lower(),
                active_state=str(
                    getattr(status, "active_state", "unknown")).lower(),
                unit_file_state=str(
                    getattr(status, "unit_file_state", "unknown")).lower(),
            )
            self._remember_service_state(
                target, snapshot.load_state, snapshot.active_state)
            return snapshot
        service = WDG_SERVICE if target == "wdg" else LEGACY_SERVICE
        result = self._service_runner(
            ["systemctl", "show", service,
             "--property=LoadState", "--property=ActiveState",
             "--property=UnitFileState"],
            capture_output=True, text=True, timeout=12)
        if result.returncode:
            self._remember_service_state(target, "unknown", "unknown")
            return _ServiceSnapshot(target, "unknown", "unknown", "unknown")
        properties = {}
        for line in (result.stdout or "").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                properties[key.strip()] = value.strip().lower()
        snapshot = _ServiceSnapshot(
            target=target,
            load_state=properties.get("LoadState", "unknown"),
            active_state=properties.get("ActiveState", "unknown"),
            unit_file_state=properties.get("UnitFileState", "unknown"),
        )
        self._remember_service_state(
            target, snapshot.load_state, snapshot.active_state)
        return snapshot

    def _capture_service_snapshot(self) -> dict[str, _ServiceSnapshot]:
        """Capture both units only after any bounded transition settles.

        ``systemctl show`` is not an atomic two-unit query.  Retrying the full
        pair avoids preserving one side of a service handoff as if it were a
        stable rollback point.  Unknown, failed and otherwise inconsistent
        states are returned immediately and rejected by the strict validator;
        only documented transitional ActiveState values are polled.
        """
        snapshot: dict[str, _ServiceSnapshot] = {}
        for attempt in range(_SERVICE_SNAPSHOT_ATTEMPTS):
            snapshot = {
                target: self._service_snapshot(target)
                for target in ("wdg", "stock")
            }
            if not any(state.transitional for state in snapshot.values()):
                return snapshot
            if attempt + 1 < _SERVICE_SNAPSHOT_ATTEMPTS:
                self._sleep(_SERVICE_SNAPSHOT_POLL_SECONDS)
        return snapshot

    def _service_snapshot_is_restorable(
            self, snapshot: dict[str, _ServiceSnapshot],
            *, operation: str) -> bool:
        """Accept only complete, stable states the helper can restore exactly."""
        for target in ("wdg", "stock"):
            state = snapshot.get(target)
            if state is None:
                self._emit(
                    "error",
                    "Cannot " + operation + " Meshtastic services because "
                    + target + " service state is missing",
                )
                return False
            if state.exactly_restorable:
                continue
            self._emit(
                "error",
                "Cannot " + operation + " Meshtastic services from unstable "
                + target + " state " + state.describe()
                + "; wait for loaded/active or loaded/inactive with an "
                "enabled/disabled unit, or not-found/inactive/not-found",
            )
            return False
        active = [target for target in ("wdg", "stock")
                  if snapshot[target].active]
        if len(active) > 1:
            self._emit(
                "error",
                "Cannot " + operation + " Meshtastic services while both "
                "meshtasticd-wdg and stock meshtasticd are active",
            )
            return False
        return True

    @staticmethod
    def _snapshot_selected_target(
            snapshot: dict[str, _ServiceSnapshot]) -> str | None:
        active = [target for target in ("wdg", "stock")
                  if snapshot[target].installed and snapshot[target].active]
        if len(active) == 1:
            return active[0]
        if len(active) > 1:
            return None
        enabled = [target for target in ("wdg", "stock")
                   if snapshot[target].installed and snapshot[target].enabled]
        return enabled[0] if enabled else None

    def _set_service_enabled(self, target: str, enabled: bool) -> bool:
        if self._service_controller is not None:
            status = (self._service_controller.enable(target) if enabled
                      else self._service_controller.disable(target))
            actual = str(getattr(status, "unit_file_state", "")).lower()
            return actual == ("enabled" if enabled else "disabled")
        service = WDG_SERVICE if target == "wdg" else LEGACY_SERVICE
        result = self._run_service("enable" if enabled else "disable", service)
        if result.returncode:
            detail = (result.stderr or result.stdout
                      or "systemctl failed").strip()
            self._emit(
                "error", f"Could not {'enable' if enabled else 'disable'} "
                + service + ": " + detail[:160])
            return False
        try:
            actual = self._service_snapshot(target).unit_file_state
        except Exception:
            return False
        return actual == ("enabled" if enabled else "disabled")

    def _select_service_target(self, target: str) -> bool:
        """Persist and start one exact daemon while stopping its peer."""
        other = "stock" if target == "wdg" else "wdg"
        if self._service_controller is not None:
            status = self._service_controller.select(target)
            return bool(getattr(status, "active", False))
        if not self._stop_exact_service(other):
            return False
        try:
            other_installed = self._service_snapshot(other).installed
        except Exception:
            other_installed = True
        if other_installed and not self._set_service_enabled(other, False):
            return False
        if not self._set_service_enabled(target, True):
            return False
        ready = (self._ensure_fork_service() if target == "wdg"
                 else self._ensure_legacy_service())
        return bool(ready)

    def _restore_service_snapshot(
            self, snapshot: dict[str, _ServiceSnapshot],
            *, timeout: float = 15.0) -> bool:
        """Restore active and enabled state for both mutually exclusive units."""
        if not self._service_snapshot_is_restorable(
                snapshot, operation="restore"):
            return False
        try:
            # Stop unwanted owners before changing persistent selection.
            for target in ("wdg", "stock"):
                desired = snapshot[target]
                if not desired.active and not self._stop_exact_service(target):
                    return False
            for target in ("wdg", "stock"):
                desired = snapshot[target]
                if not desired.installed:
                    continue
                current = self._service_snapshot(target)
                if current.unit_file_state != desired.unit_file_state:
                    if not self._set_service_enabled(target, desired.enabled):
                        return False
            for target in ("wdg", "stock"):
                desired = snapshot[target]
                if not desired.active:
                    continue
                if self._service_controller is not None:
                    status = self._service_controller.start(target)
                    if not bool(getattr(status, "active", False)):
                        return False
                else:
                    result = self._run_service(
                        "start", WDG_SERVICE if target == "wdg"
                        else LEGACY_SERVICE)
                    if result.returncode:
                        return False

            deadline = self._monotonic() + max(0.0, float(timeout))
            while self._monotonic() < deadline:
                if self._service_snapshot_matches(snapshot):
                    return True
                self._sleep(0.1)
            return self._service_snapshot_matches(snapshot)
        except Exception as exc:
            self._emit("error", "Could not restore Meshtastic service state: "
                       + str(exc)[:160])
            return False

    def _service_snapshot_matches(
            self, snapshot: dict[str, _ServiceSnapshot]) -> bool:
        for target in ("wdg", "stock"):
            desired = snapshot[target]
            actual = self._service_snapshot(target)
            if desired.installed and not actual.installed:
                return False
            if actual.unit_file_state != desired.unit_file_state:
                return False
            if desired.active:
                if not self._service_target_ready(target):
                    return False
            elif not self._service_is_stopped(
                    actual.load_state, actual.active_state):
                return False
        return True

    def _fork_may_own_resources(self) -> bool:
        """Fail closed for an active or transitional fork without a socket."""
        if self._socket_probe(self.socket_path):
            return True
        load_state, active_state = self._fork_service_state()
        return not (
            load_state == "not-found"
            or active_state in ("inactive", "failed"))

    def ble_coordination_ready(self) -> bool:
        """Whether a host BLE user can safely request its bounded lease."""
        if not self._fork_may_own_resources():
            return True
        return self.ensure_ble_coordination(timeout=5.0)

    def ensure_ble_coordination(self, timeout: float = 5.0) -> bool:
        """Maintain the restricted socket whenever the live fork owns BlueZ."""
        if not self._fork_may_own_resources():
            return True
        if self.active_backend == "fork_socket" and self.connected:
            return True
        if self.backend_mode == "legacy_tcp":
            self._emit(
                "error", "The WDG daemon is active while legacy TCP is "
                "selected; switch the Meshtastic backend first")
            return False
        if not self.running:
            self.start()
        self._fork_connected_event.wait(max(0.1, float(timeout)))
        return self.active_backend == "fork_socket" and self.connected

    def _ensure_fork_service(self) -> bool:
        if self._socket_probe(self.socket_path):
            if self._service_controller is None:
                self._fork_identified = True
                self._remember_service_state("wdg", "loaded", "active")
                return True
            try:
                status = self._service_controller.status("wdg")
                self._remember_service_state(
                    "wdg", getattr(status, "load_state", "loaded"),
                    getattr(status, "active_state", "active"))
                if status.active:
                    self._fork_identified = True
                    return True
            except Exception:
                pass
        self._fork_identified = True
        try:
            if self._service_controller is not None:
                status = self._service_controller.start("wdg")
                self._remember_service_state(
                    "wdg", getattr(status, "load_state", "loaded"),
                    getattr(status, "active_state", "active"))
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
                self._remember_service_state("wdg", "loaded", "active")
                return True
            self._sleep(0.25)
        self._emit(
            "error", "meshtasticd-wdg started but its local socket never opened")
        return False

    def _ensure_legacy_service(self) -> bool:
        if self._port_probe(self.host, self.port):
            if self._service_controller is None:
                self._remember_service_state("stock", "loaded", "active")
                return True
            try:
                status = self._service_controller.status("stock")
                self._remember_service_state(
                    "stock", getattr(status, "load_state", "loaded"),
                    getattr(status, "active_state", "active"))
                if status.active:
                    return True
            except Exception:
                pass
        try:
            if self._service_controller is not None:
                status = self._service_controller.start("stock")
                self._remember_service_state(
                    "stock", getattr(status, "load_state", "loaded"),
                    getattr(status, "active_state", "active"))
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
                self._remember_service_state("stock", "loaded", "active")
                return True
            self._sleep(0.25)
        self._emit("error", "meshtasticd started but TCP port 4403 never opened")
        return False

    def _select_backend(self) -> str:
        if self._resume_backend_once:
            backend = self._resume_backend_once
            self._resume_backend_once = ""
            return backend
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

    @staticmethod
    def _service_is_stopped(load_state: str, active_state: str) -> bool:
        return (load_state == "not-found"
                or active_state in ("inactive", "failed"))

    def active_service_target(self) -> str | None:
        """Return the active unit, or the sole persistently enabled unit.

        Saved UI preference and client transport are intentionally ignored:
        systemd state is the ownership authority for a direct-radio handoff.
        Returning an enabled but inactive unit lets a failed direct-radio
        startup restore its exact boot-time selection without starting it.
        """
        candidates = []
        enabled = []
        for target in ("wdg", "stock"):
            try:
                state = self._service_snapshot(target)
            except Exception:
                continue
            if state.installed and state.active:
                candidates.append(target)
            if state.installed and state.enabled:
                enabled.append(target)
        if not candidates:
            if len(enabled) == 1:
                return enabled[0]
            return None
        if len(candidates) == 1:
            return candidates[0]
        preferred = (
            "wdg" if self.active_backend == "fork_socket" else
            "stock" if self.active_backend == "legacy_tcp" else "")
        return preferred if preferred in candidates else candidates[0]

    def _service_target_ready(self, target: str) -> bool:
        try:
            if self._service_controller is not None:
                status = self._service_controller.status(target)
                if not bool(getattr(status, "active", False)):
                    return False
            else:
                _load_state, active_state = self._service_state(target)
                if active_state != "active":
                    return False
        except Exception as exc:
            self._emit("error", "Could not read Meshtastic service status: "
                       + str(exc)[:160])
            return False
        if target == "wdg":
            return bool(self._socket_probe(self.socket_path))
        return bool(self._port_probe(self.host, self.port))

    def _service_state(self, target: str) -> tuple[str, str]:
        if self._service_controller is not None:
            status = self._service_controller.status(target)
            value = (
                str(getattr(status, "load_state", "unknown")).lower(),
                str(getattr(status, "active_state", "unknown")).lower(),
            )
            self._remember_service_state(target, *value)
            return value
        service = WDG_SERVICE if target == "wdg" else LEGACY_SERVICE
        result = self._service_runner(
            ["systemctl", "show", service,
             "--property=LoadState", "--property=ActiveState"],
            capture_output=True, text=True, timeout=12)
        if result.returncode:
            self._remember_service_state(target, "unknown", "unknown")
            return "unknown", "unknown"
        properties = {}
        for line in (result.stdout or "").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                properties[key.strip()] = value.strip().lower()
        value = (
            properties.get("LoadState", "unknown"),
            properties.get("ActiveState", "unknown"),
        )
        self._remember_service_state(target, *value)
        return value

    def _stop_exact_service(self, target: str) -> bool:
        service = WDG_SERVICE if target == "wdg" else LEGACY_SERVICE
        label = "meshtasticd-wdg" if target == "wdg" else "meshtasticd"
        load_state, active_state = self._service_state(target)
        if self._service_is_stopped(load_state, active_state):
            return True
        if self._service_controller is not None:
            self._service_controller.stop(target)
        else:
            result = self._run_service("stop", service)
            if result.returncode:
                detail = (result.stderr or result.stdout
                          or "systemctl failed").strip()
                self._emit("error", f"Could not stop {label}: " + detail[:160])
                return False
        deadline = self._monotonic() + 10.0
        for _ in range(100):
            load_state, active_state = self._service_state(target)
            if self._service_is_stopped(load_state, active_state):
                self._remember_service_state(target, load_state, active_state)
                self._emit("status", f"{label} stopped")
                return True
            if self._monotonic() >= deadline:
                break
            self._sleep(0.1)
        self._emit(
            "error", f"Could not stop {label}: service remained "
            f"{active_state or 'unknown'}")
        return False

    def _stop_service(self) -> bool:
        """Stop every daemon that could still own the shared SX1262."""
        # Configuration and historical client state are not ownership proof.
        # Inspect both mutually exclusive units because a failed install,
        # manual systemctl use, or an old setup can leave the unexpected unit
        # active or transitional. Unknown state is handled fail-closed by an
        # explicit stop attempt.
        try:
            for target in ("wdg", "stock"):
                if not self._stop_exact_service(target):
                    return False
        except Exception as exc:
            self._emit("error", "Could not release Meshtastic services: "
                       + str(exc)[:160])
            return False
        self.started_daemon = False
        self._emit("status", "Meshtastic services stopped; SX1262 released")
        return True

    def _legacy_phoneapi_conflict(self) -> bool:
        """Whether TCP could steal a BLE-enabled fork's full-client lease."""
        # A saved "phone BLE off" preference is not proof that an already
        # running daemon applied it.  The restricted socket is the only path
        # that acknowledges that mutation, and selecting legacy deliberately
        # avoids that socket.  Therefore any live fork blocks TCP; callers that
        # want the stock daemon must switch the exact systemd service first.
        return self._fork_may_own_resources()

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
            self._connected_event.clear()
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
            self._connected_event.set()
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
            self._connected_event.clear()
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
            # Applied policy belongs to one daemon connection. A restarted or
            # downgraded daemon may omit these fields, so never authorize a
            # lease bypass from the previous socket's acknowledgement.
            self._reset_socket_session_state()
            try:
                client = self._socket_connector(self.socket_path)
                client.settimeout(0.25)
                self._socket = client
                self._socket_negotiate(client)
                if self._stop_event.is_set():
                    break
                self.connected = True
                self._fork_connected_event.set()
                self._connected_event.set()
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
                    self._emit("disconnected", "meshtasticd-wdg connection lost")
                self._fail_control_waiters(
                    "meshtasticd-wdg connection closed")
                self._pending_requests.clear()
                self._reset_socket_session_state()
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
                applied = self._socket_request(client, "set_phone_ble", {
                    "enabled": bool(self._phone_ble_enabled),
                    "adapter": self._phone_ble_adapter,
                })
                self._record_applied_phone_ble(applied)
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
                    self._dispatch_control_command(
                        client, name, body, waiter)
            message = self._socket_receive(client)
            if message is not None:
                self._handle_socket_message(client, message)

    def _dispatch_control_command(
            self, client: socket.socket, name: str, body: dict,
            waiter: _ControlWaiter) -> bool:
        """Send one queued control only while its caller can observe it."""
        if not waiter.begin_send(self._monotonic()):
            return False
        try:
            self._socket_command(client, name, body, pending=waiter)
        except (OSError, WdgProtocolError) as exc:
            waiter.resolve(False, str(exc))
            return False
        return True

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
                      "pairing_passkey", "pairing_pin",
                      "full_client_owner", "full_client_status"):
            if name == "phone_connected":
                self.phone_connected = True
                self.pairing_pin = ""
            elif name == "phone_disconnected":
                self.phone_connected = False
                if self._ble_scan_lease_active:
                    now = self._monotonic()
                    self._phone_scan_disconnects.append(now)
                    while (self._phone_scan_disconnects
                           and now - self._phone_scan_disconnects[0] > 300):
                        self._phone_scan_disconnects.popleft()
                    if len(self._phone_scan_disconnects) >= 2:
                        self.host_ble_degraded = True
                        self.host_ble_pause_reason = (
                            "Meshtastic phone disconnected repeatedly during "
                            "host BLE scanning")
                        self._emit(
                            "status",
                            "Host BLE paused: Meshtastic phone priority")
            elif name == "ble_status":
                self.ble_status = str(body.get("state") or
                                      body.get("status") or "unknown")
                if body.get("host_ble_paused") or body.get("degraded"):
                    self.host_ble_degraded = True
                    self.host_ble_pause_reason = str(
                        body.get("reason") or
                        "Meshtastic phone has Bluetooth priority")
            elif name in ("pairing_passkey", "pairing_pin"):
                self.pairing_pin = str(
                    body.get("pin") or body.get("passkey") or "")
            elif name in ("full_client_owner", "full_client_status"):
                self._record_full_client_owner(body)
            text = body.get("message") or name.replace("_", " ")
            if self.pairing_pin and name in ("pairing_passkey", "pairing_pin"):
                text = f"Meshtastic phone PIN: {self.pairing_pin}"
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
                context.resolve(False, str(text))
            else:
                self._emit("error", str(text))
            if name == "snapshot_nodes":
                self._snapshot_pending = False
            return
        body = message.get("body", message.get("payload", {}))
        body = body if isinstance(body, dict) else {}
        if isinstance(context, _ControlWaiter):
            context.resolve(
                True, str(body.get("reason") or ""), dict(body))
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
                context.resolve(False, reason)

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
        self._record_applied_phone_ble(body)
        self._record_full_client_owner(body)
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
        self._fork_connected_event.clear()
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
