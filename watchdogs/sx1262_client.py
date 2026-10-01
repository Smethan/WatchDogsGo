"""Client for the local ``watchdogs-sx1262d`` radio broker.

The broker is the only process allowed to own the AIO SX1262, its GPIOs, or
its power rail.  This module deliberately has no direct-radio fallback: a
missing or faulted broker must remain visible to the operator.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


API_MAJOR = 1
API_MINOR = 1
DEFAULT_SOCKET = "/run/watchdogs/sx1262d.sock"
MAX_PACKET = 4096
MAX_LORA_PAYLOAD = 255
VALID_ROLES = frozenset({"controller", "meshtastic", "meshcore", "reticulum"})
VALID_MODES = frozenset({"meshtastic", "meshcore", "reticulum"})


class BrokerError(RuntimeError):
    """The broker rejected an operation or returned an invalid response."""


class BrokerUnavailable(BrokerError):
    """The broker socket is unavailable or the connection was lost."""


class BrokerProtocolError(BrokerError):
    """A peer sent data outside the bounded broker protocol."""


@dataclass(frozen=True)
class BrokerSession:
    connection_id: int
    generation: int
    api_major: int
    api_minor: int


@dataclass(frozen=True)
class RadioPacket:
    payload: bytes
    rssi: float
    snr: float
    frequency_error: float
    monotonic_ns: int


def _encode(message: dict[str, Any]) -> bytes:
    raw = json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(raw) > MAX_PACKET:
        raise BrokerProtocolError(
            f"SX1262 broker message is {len(raw)} bytes; limit is {MAX_PACKET}")
    return raw


def _decode(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_PACKET:
        raise BrokerProtocolError("invalid SX1262 broker packet length")
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerProtocolError("invalid SX1262 broker JSON") from exc
    if not isinstance(message, dict):
        raise BrokerProtocolError("SX1262 broker envelope must be an object")
    return message


class SX1262Client:
    """Synchronous requests plus a bounded asynchronous event stream."""

    def __init__(
        self,
        role: str,
        *,
        socket_path: str = DEFAULT_SOCKET,
        timeout: float = 5.0,
        event_limit: int = 128,
        socket_factory: Callable[..., socket.socket] = socket.socket,
    ) -> None:
        if role not in VALID_ROLES:
            raise ValueError(f"invalid SX1262 broker role: {role}")
        self.role = role
        self.socket_path = socket_path
        self.timeout = max(0.1, float(timeout))
        self._event_limit = max(1, int(event_limit))
        self._socket_factory = socket_factory
        self._sock: socket.socket | None = None
        self._session: BrokerSession | None = None
        self._request_id = 0
        self._lock = threading.RLock()
        self._events: queue.Queue[dict[str, Any]] = queue.Queue(self._event_limit)
        self.dropped_events = 0

    @property
    def connected(self) -> bool:
        return self._sock is not None and self._session is not None

    @property
    def generation(self) -> int:
        if self._session is None:
            return 0
        return self._session.generation

    @property
    def session(self) -> BrokerSession | None:
        return self._session

    def connect(self) -> BrokerSession:
        with self._lock:
            self.close()
            try:
                sock = self._socket_factory(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                sock.settimeout(self.timeout)
                sock.connect(self.socket_path)
            except OSError as exc:
                try:
                    sock.close()  # type: ignore[possibly-undefined]
                except (OSError, UnboundLocalError):
                    pass
                raise BrokerUnavailable(
                    f"SX1262 manager unavailable at {self.socket_path}: {exc}") from exc
            self._sock = sock
            try:
                response = self._exchange({
                    "type": "hello",
                    "api": {"major": API_MAJOR, "minor": API_MINOR},
                    "role": self.role,
                    "pid": os.getpid(),
                }, include_generation=False)
                api = response.get("api", {})
                if int(api.get("major", -1)) != API_MAJOR:
                    raise BrokerProtocolError("SX1262 broker API major mismatch")
                self._session = BrokerSession(
                    connection_id=int(response["connection_id"]),
                    generation=int(response["generation"]),
                    api_major=int(api["major"]),
                    api_minor=int(api.get("minor", 0)),
                )
                return self._session
            except Exception:
                self.close()
                raise

    def close(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
            self._session = None
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def request(self, operation: str, **arguments: Any) -> dict[str, Any]:
        with self._lock:
            if not self.connected:
                self.connect()
            try:
                return self._exchange(
                    {"type": "request", "op": operation, **arguments})
            except BrokerError as exc:
                # Ownership transitions increment the generation between a
                # request being sent and its response. The manager rejects the
                # old request before executing it and includes the current
                # generation, so one replay is safe and prevents the heartbeat
                # thread from abandoning a freshly granted WDG lease.
                if not str(exc).startswith("stale_generation:"):
                    raise
                return self._exchange(
                    {"type": "request", "op": operation, **arguments})

    def _exchange(
        self, envelope: dict[str, Any], *, include_generation: bool = True,
    ) -> dict[str, Any]:
        sock = self._sock
        if sock is None:
            raise BrokerUnavailable("SX1262 manager is not connected")
        self._request_id += 1
        request_id = self._request_id
        envelope["request_id"] = request_id
        if include_generation:
            envelope["generation"] = self.generation
        try:
            sock.sendall(_encode(envelope))
            while True:
                raw = sock.recv(MAX_PACKET + 1)
                message = _decode(raw)
                if "generation" in message and self._session is not None:
                    self._session = BrokerSession(
                        self._session.connection_id,
                        int(message["generation"]),
                        self._session.api_major,
                        self._session.api_minor,
                    )
                if message.get("type") == "event":
                    self._queue_event(message)
                    continue
                if int(message.get("request_id", -1)) != request_id:
                    raise BrokerProtocolError("SX1262 broker response ID mismatch")
                if message.get("type") != "response":
                    raise BrokerProtocolError("unexpected SX1262 broker envelope")
                if not bool(message.get("ok")):
                    error = message.get("error", {})
                    code = str(error.get("code", "rejected"))
                    detail = str(error.get("message", "broker rejected request"))
                    raise BrokerError(f"{code}: {detail}")
                result = message.get("result", {})
                if not isinstance(result, dict):
                    raise BrokerProtocolError("SX1262 broker result must be an object")
                return result
        except (OSError, socket.timeout) as exc:
            self.close()
            raise BrokerUnavailable(f"SX1262 manager connection failed: {exc}") from exc

    def _queue_event(self, event: dict[str, Any]) -> None:
        try:
            self._events.put_nowait(event)
            return
        except queue.Full:
            pass
        # RX events are passive and may be discarded before lease, fault, power,
        # or TX-completion events.  If a control event arrives, evict the oldest
        # passive event when possible.
        if event.get("event") == "rx_packet":
            self.dropped_events += 1
            return
        retained: list[dict[str, Any]] = []
        removed = False
        while not self._events.empty():
            queued = self._events.get_nowait()
            if not removed and queued.get("event") == "rx_packet":
                removed = True
                self.dropped_events += 1
                continue
            retained.append(queued)
        for queued in retained[-(self._event_limit - 1):]:
            self._events.put_nowait(queued)
        if not removed and self._events.full():
            self._events.get_nowait()
            self.dropped_events += 1
        self._events.put_nowait(event)

    def next_event(self, timeout: float | None = None) -> dict[str, Any] | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def configure_phy(self, **configuration: Any) -> dict[str, Any]:
        return self.request("configure_phy", phy=configuration)

    def start_rx(self) -> dict[str, Any]:
        return self.request("start_rx")

    def standby(self) -> dict[str, Any]:
        return self.request("standby")

    def sleep(self) -> dict[str, Any]:
        return self.request("sleep")

    def cad(self) -> dict[str, Any]:
        return self.request("cad")

    def transmit(self, payload: bytes) -> dict[str, Any]:
        payload = bytes(payload)
        if not 1 <= len(payload) <= MAX_LORA_PAYLOAD:
            raise ValueError("LoRa payload must contain 1 through 255 bytes")
        return self.request(
            "transmit", payload=base64.b64encode(payload).decode("ascii"))

    def get_metrics(self) -> dict[str, Any]:
        return self.request("get_metrics")

    def quiesced(self) -> dict[str, Any]:
        return self.request("quiesced")

    @staticmethod
    def radio_packet(event: dict[str, Any]) -> RadioPacket:
        if event.get("event") != "rx_packet":
            raise BrokerProtocolError("event is not an RX packet")
        try:
            payload = base64.b64decode(event["payload"], validate=True)
        except (KeyError, ValueError) as exc:
            raise BrokerProtocolError("invalid RX payload") from exc
        if len(payload) > MAX_LORA_PAYLOAD:
            raise BrokerProtocolError("oversized RX payload")
        return RadioPacket(
            payload=payload,
            rssi=float(event["rssi"]),
            snr=float(event["snr"]),
            frequency_error=float(event.get("frequency_error", 0.0)),
            monotonic_ns=int(event["monotonic_ns"]),
        )


class SX1262Controller(SX1262Client):
    """WDG-only administrative client with a renewable mode lease."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("controller", **kwargs)
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._active_mode: str | None = None
        self.last_heartbeat_error: str = ""

    def get_status(self) -> dict[str, Any]:
        return self.request("get_status")

    def wait_for_mode_ready(
        self, mode: str, *, timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Wait for confirmed mode ownership, PHY configuration, and RX."""
        if mode not in VALID_MODES:
            raise ValueError(f"invalid SX1262 mode: {mode}")
        deadline = time.monotonic() + max(0.1, float(timeout))
        last_status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last_status = self.get_status()
            state = str(last_status.get("state") or "").upper()
            if (state == mode.upper()
                    and str(last_status.get("active_mode") or mode) == mode
                    and bool(last_status.get("protocol_ready"))):
                return last_status
            if state in {"FAULT", "OFF"}:
                raise BrokerError(
                    "radio_fault: " + str(last_status.get("fault") or state))
            time.sleep(0.05)
        raise BrokerError(
            "mode_not_ready: SX1262 manager did not confirm "
            f"{mode} PHY/RX readiness; last state="
            f"{last_status.get('state', 'unknown')}")

    def activate_mode(self, mode: str) -> dict[str, Any]:
        if mode not in VALID_MODES:
            raise ValueError(f"invalid SX1262 mode: {mode}")
        # Refresh controller liveness before requesting a non-default mode.
        # This also protects upgrades that temporarily retain a pre-v16
        # manager, whose activation request did not itself refresh the lease.
        self.heartbeat()
        result = self.request("activate_mode", mode=mode)
        self._active_mode = mode
        if mode in {"meshcore", "reticulum"}:
            self._start_heartbeat()
        else:
            self._stop_heartbeat()
        return result

    def release_mode(self) -> dict[str, Any]:
        self._stop_heartbeat()
        self._active_mode = None
        return self.request("release_mode")

    def admin_power_off(self) -> dict[str, Any]:
        self._stop_heartbeat()
        self._active_mode = None
        result = self.request("admin_power_off")
        if not bool(result.get("pending")):
            return result
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            status = self.get_status()
            if (str(status.get("state")) == "OFF"
                    and bool(status.get("forced_off"))
                    and not bool(status.get("power"))):
                return status
            if str(status.get("state")) == "FAULT":
                raise BrokerError(
                    "radio_fault: " + str(status.get("fault") or
                                           "power-off failed"))
            time.sleep(0.05)
        raise BrokerError(
            "power_transition: SX1262 manager did not confirm OFF state")

    def admin_power_on(self, preferred_mode: str | None = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {}
        if preferred_mode is not None:
            if preferred_mode not in VALID_MODES:
                raise ValueError(f"invalid SX1262 mode: {preferred_mode}")
            arguments["mode"] = preferred_mode
        result = self.request("admin_power_on", **arguments)
        self._active_mode = str(result.get("active_mode") or preferred_mode or "meshtastic")
        if self._active_mode in {"meshcore", "reticulum"}:
            self._start_heartbeat()
        return result

    def clear_fault(self) -> dict[str, Any]:
        return self.request("clear_fault")

    def heartbeat(self) -> dict[str, Any]:
        return self.request("heartbeat")

    def close(self) -> None:
        self._stop_heartbeat()
        super().close()

    def _start_heartbeat(self) -> None:
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="wdg-sx1262-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        thread, self._heartbeat_thread = self._heartbeat_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(1.0):
            try:
                self.heartbeat()
                self.last_heartbeat_error = ""
            except BrokerError as exc:
                self.last_heartbeat_error = str(exc)
                # Reconnects are attempted by the next heartbeat/request, but
                # never reclaim a non-Meshtastic lease implicitly.
                self.close()
                return


__all__ = [
    "API_MAJOR", "API_MINOR", "BrokerError", "BrokerProtocolError",
    "BrokerSession", "BrokerUnavailable", "DEFAULT_SOCKET", "MAX_PACKET",
    "RadioPacket", "SX1262Client", "SX1262Controller",
]
