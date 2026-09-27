"""Isolated Reticulum/LXMF runtime for WatchDogsGo.

RNS is deliberately process-global and installs signal handlers.  WDG starts
this module as a short-lived child for every active Reticulum session so radio
protocol switches never attempt to reinitialise RNS inside the Pyxel process.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .reticulum_config import (
    ReticulumConfigError,
    ReticulumProfile,
    ensure_private_dir,
    identity_path,
    load_profile,
    validate_destination_hash,
    validate_message_text,
    write_private_file,
)
from .reticulum_interface import set_interface_event_sink

IPC_VERSION = 1
IPC_MAX_PACKET = 64 * 1024
TERMINAL_STATES = {0x08, 0xFE, 0xFD, 0xFF}
REQUEST_NAMES = {
    "hello", "status", "announce", "send_text", "sync_propagation",
    "cancel_propagation", "shutdown",
}


class SidecarProtocolError(ValueError):
    pass


def _configobj_quote(value: str) -> str:
    """Quote one validated single-line IFAC value without changing bytes."""
    if "'" not in value:
        return "'" + value + "'"
    if '"' not in value:
        return '"' + value + '"'
    if "'''" not in value:
        return "'''" + value + "'''"
    if '\"\"\"' not in value:
        return '\"\"\"' + value + '\"\"\"'
    raise ReticulumConfigError(
        "IFAC value contains both triple-quote forms and cannot be encoded "
        "safely in an RNS configuration")


def _envelope(kind: str, name: str, payload: dict[str, Any],
              request_id: str = "") -> dict[str, Any]:
    value: dict[str, Any] = {
        "v": IPC_VERSION, "type": kind, "name": name, "payload": payload,
    }
    if request_id:
        value["request_id"] = request_id
    return value


class SidecarRuntime:
    def __init__(self, profile_file: Path, socket_path: Path) -> None:
        self.profile_file = profile_file
        self.socket_path = socket_path
        self.app_dir = profile_file.parent.parent
        self.state_dir = profile_file.parent
        self.profile = load_profile(self.app_dir)
        # A transactional restart may intentionally point at profile.pending.
        if profile_file.name != "profile.json":
            self.profile = ReticulumProfile.from_mapping(
                json.loads(profile_file.read_text(encoding="utf-8")))
        if not self.profile.confirmed:
            raise ReticulumConfigError(
                "Reticulum RF settings have not been confirmed")
        self.sock: socket.socket | None = None
        self.send_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.fatal_error = ""
        self.rns = None
        self.router = None
        self.identity = None
        self.delivery_destination = None
        self.announce_handler = None
        self.announced = False
        self.outbound: dict[str, Any] = {}
        self.outbound_states: dict[str, str] = {}
        self.monitor_threads: list[threading.Thread] = []
        self.propagation_thread: threading.Thread | None = None
        self.invalid_message_last = 0.0
        self.invalid_message_suppressed = 0

    def send(self, kind: str, name: str, payload: dict[str, Any],
             request_id: str = "") -> bool:
        sock = self.sock
        if sock is None:
            return False
        packet = json.dumps(
            _envelope(kind, name, payload, request_id),
            separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(packet) > IPC_MAX_PACKET:
            return False
        try:
            with self.send_lock:
                sock.sendall(packet)
            return True
        except OSError:
            self.stop_event.set()
            return False

    def event(self, name: str, payload: dict[str, Any]) -> None:
        self.send("event", name, payload)
        if name == "error" and payload.get("fatal"):
            self.fatal_error = str(payload.get("detail") or "fatal radio error")
            self.stop_event.set()

    def _render_rns_config(self) -> None:
        rns_root = ensure_private_dir(self.state_dir / "rns")
        interfaces = ensure_private_dir(rns_root / "interfaces")
        loader = (
            "from watchdogs.reticulum_interface import AioSX1262Interface\n"
            "interface_class = AioSX1262Interface\n"
        )
        write_private_file(interfaces / "AioSX1262Interface.py", loader)
        p = self.profile
        config = [
            "[reticulum]",
            "  enable_transport = No",
            "  share_instance = No",
            "  panic_on_interface_error = No",
            "",
            "[logging]",
            "  loglevel = 3",
            "",
            "[interfaces]",
            "  [[AIO SX1262]]",
            "    type = AioSX1262Interface",
            "    enabled = Yes",
            "    interface_mode = full",
            f"    frequency = {p.frequency_hz}",
            f"    bandwidth = {p.bandwidth_hz}",
            f"    spreading_factor = {p.spreading_factor}",
            f"    coding_rate = {p.coding_rate}",
            f"    tx_power = {p.tx_power_dbm}",
            f"    airtime_short_percent = {p.airtime_short_percent}",
            f"    airtime_long_percent = {p.airtime_long_percent}",
        ]
        if p.network_name:
            config.extend([
                f"    network_name = {_configobj_quote(p.network_name)}",
                f"    passphrase = {_configobj_quote(p.network_passphrase)}",
            ])
        write_private_file(rns_root / "config", "\n".join(config) + "\n")

    def _load_identity(self, RNS):
        path = identity_path(self.app_dir)
        if path.is_symlink():
            raise ReticulumConfigError("refusing symlinked Reticulum identity")
        identity = RNS.Identity.from_file(str(path)) if path.is_file() else None
        if identity is None:
            identity = RNS.Identity()
            private_key = identity.get_private_key()
            write_private_file(path, private_key)
        os.chmod(path, 0o600)
        return identity

    def _configure_display_name(self, RNS) -> None:
        if self.profile.display_name:
            return
        destination_hash = RNS.Destination.hash_from_name_and_identity(
            "lxmf.delivery", self.identity).hex()
        self.profile = self.profile.with_updates(
            display_name="WatchDogs_" + destination_hash[:8])
        # Preserve transactional semantics by updating the exact profile path
        # used for this child, not an unrelated active profile.
        payload = json.dumps(
            self.profile.to_mapping(), indent=2, sort_keys=True) + "\n"
        write_private_file(self.profile_file, payload)

    def _install_announce_handler(self, RNS, LXMF) -> None:
        runtime = self

        class DeliveryAnnounceHandler:
            aspect_filter = "lxmf.delivery"
            receive_path_responses = True

            def received_announce(self, destination_hash, announced_identity,
                                  app_data, *_extra):
                try:
                    name = LXMF.display_name_from_app_data(app_data) or "?"
                except Exception:
                    name = "?"
                try:
                    hops = int(RNS.Transport.hops_to(destination_hash))
                except Exception:
                    hops = 0
                runtime.event("contact", {
                    "destination_hash": bytes(destination_hash).hex(),
                    "display_name": str(name)[:64],
                    "hops": max(0, hops),
                    "last_seen": time.time(),
                    "rssi": None,
                    "snr": None,
                })

        self.announce_handler = DeliveryAnnounceHandler()
        RNS.Transport.register_announce_handler(self.announce_handler)

    def _inbound_message(self, message) -> None:
        if not bool(getattr(message, "signature_validated", False)):
            now = time.monotonic()
            if now - self.invalid_message_last >= 5.0:
                detail = (
                    "Dropped an LXMF message with an invalid or unknown "
                    "signature")
                if self.invalid_message_suppressed:
                    detail += (
                        f" ({self.invalid_message_suppressed} similar "
                        "diagnostics suppressed)")
                self.event("error", {
                    "code": "unverified_message",
                    "detail": detail,
                    "fatal": False,
                })
                self.invalid_message_last = now
                self.invalid_message_suppressed = 0
            else:
                self.invalid_message_suppressed += 1
            return
        content = getattr(message, "content", "")
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        content = str(content).replace("\x00", "").replace("\r", " ").replace(
            "\n", " ").strip()
        if not content:
            return
        source_hash = bytes(getattr(message, "source_hash", b"")).hex()
        message_hash = bytes(getattr(message, "hash", b"")).hex()
        app_data = None
        try:
            app_data = sys.modules["RNS"].Identity.recall_app_data(
                bytes.fromhex(source_hash))
            peer_name = sys.modules["LXMF"].display_name_from_app_data(app_data)
        except Exception:
            peer_name = None
        self.event("message", {
            "message_hash": message_hash,
            "direction": "in",
            "peer_hash": source_hash,
            "peer_name": str(peer_name or source_hash[:8]),
            "text": content[:512],
            "state": "delivered",
            "timestamp": float(getattr(message, "timestamp", time.time())),
            "rssi": getattr(message, "rssi", None),
            "snr": getattr(message, "snr", None),
        })

    @staticmethod
    def _state_name(LXMF, value: int) -> str:
        mapping = {
            LXMF.LXMessage.GENERATING: "queued",
            LXMF.LXMessage.OUTBOUND: "queued",
            LXMF.LXMessage.SENDING: "sending",
            LXMF.LXMessage.SENT: "sent",
            LXMF.LXMessage.DELIVERED: "delivered",
            LXMF.LXMessage.REJECTED: "failed",
            LXMF.LXMessage.CANCELLED: "failed",
            LXMF.LXMessage.FAILED: "failed",
        }
        return mapping.get(value, "queued")

    def _message_status(self, LXMF, message, correlation_id: str,
                        detail: str = "") -> None:
        message_hash = getattr(message, "hash", None)
        state = self._state_name(LXMF, int(message.state))
        propagated = (
            getattr(message, "desired_method", None)
            == LXMF.LXMessage.PROPAGATED)
        if propagated and state == "sent":
            state = "stored"
            detail = detail or "stored on propagation node"
        if self.outbound_states.get(correlation_id) == state:
            return
        self.outbound_states[correlation_id] = state
        self.event("outbound_status", {
            "correlation_id": correlation_id,
            "message_hash": bytes(message_hash).hex() if message_hash else "",
            "state": state,
            "detail": detail,
            "delivery_method": "propagated" if propagated else "direct",
        })

    def _monitor_message(self, LXMF, message, correlation_id: str) -> None:
        last = None
        while not self.stop_event.is_set():
            state = int(getattr(message, "state", LXMF.LXMessage.FAILED))
            if state != last:
                self._message_status(LXMF, message, correlation_id)
                last = state
            if state in (
                    LXMF.LXMessage.DELIVERED, LXMF.LXMessage.REJECTED,
                    LXMF.LXMessage.CANCELLED, LXMF.LXMessage.FAILED):
                break
            if (getattr(message, "desired_method", None)
                    == LXMF.LXMessage.PROPAGATED
                    and state == LXMF.LXMessage.SENT):
                break
            time.sleep(0.2)
        self.outbound.pop(correlation_id, None)
        self.outbound_states.pop(correlation_id, None)

    def _announce(self) -> None:
        self.router.announce(self.delivery_destination.hash)
        self.announced = True

    @staticmethod
    def _propagation_state_name(router, value: int) -> str:
        mapping = {
            router.PR_IDLE: "idle",
            router.PR_PATH_REQUESTED: "path_requested",
            router.PR_LINK_ESTABLISHING: "link_establishing",
            router.PR_LINK_ESTABLISHED: "link_established",
            router.PR_REQUEST_SENT: "request_sent",
            router.PR_RECEIVING: "receiving",
            router.PR_RESPONSE_RECEIVED: "response_received",
            router.PR_COMPLETE: "complete",
            router.PR_NO_PATH: "no_path",
            router.PR_LINK_FAILED: "link_failed",
            router.PR_TRANSFER_FAILED: "transfer_failed",
            router.PR_NO_IDENTITY_RCVD: "identity_rejected",
            router.PR_NO_ACCESS: "access_denied",
            router.PR_FAILED: "failed",
        }
        return mapping.get(value, "unknown")

    def _emit_propagation_status(self) -> dict[str, Any]:
        router = self.router
        state_value = int(getattr(router, "propagation_transfer_state", 0))
        progress = float(getattr(
            router, "propagation_transfer_progress", 0.0) or 0.0)
        result = getattr(router, "propagation_transfer_last_result", None)
        duplicates = getattr(
            router, "propagation_transfer_last_duplicates", None)
        payload = {
            "node_hash": self.profile.propagation_node_hash,
            "state": self._propagation_state_name(router, state_value),
            "progress": max(0.0, min(1.0, progress)),
            "message_count": result if isinstance(result, int) else None,
            "duplicate_count": (
                duplicates if isinstance(duplicates, int) else None),
        }
        self.event("propagation_status", payload)
        return payload

    def _monitor_propagation(self) -> None:
        router = self.router
        last = None
        terminal = {
            router.PR_COMPLETE, router.PR_NO_PATH, router.PR_LINK_FAILED,
            router.PR_TRANSFER_FAILED, router.PR_NO_IDENTITY_RCVD,
            router.PR_NO_ACCESS, router.PR_FAILED,
        }
        while not self.stop_event.is_set():
            state = int(getattr(router, "propagation_transfer_state", 0))
            progress = round(float(getattr(
                router, "propagation_transfer_progress", 0.0) or 0.0), 3)
            snapshot = (state, progress)
            if snapshot != last:
                self._emit_propagation_status()
                last = snapshot
            if state in terminal:
                break
            time.sleep(0.2)

    def _sync_propagation(self, max_messages: int) -> dict[str, Any]:
        if not self.profile.propagation_node_hash:
            raise SidecarProtocolError(
                "no LXMF propagation node is configured")
        if type(max_messages) is not int or not 0 <= max_messages <= 200:
            raise SidecarProtocolError(
                "max_messages must be 0 (all) or an integer from 1 to 200")
        if (self.propagation_thread is not None
                and self.propagation_thread.is_alive()):
            raise SidecarProtocolError(
                "a propagation-node sync is already running")
        self.router.request_messages_from_propagation_node(
            self.identity, max_messages=max_messages)
        thread = threading.Thread(
            target=self._monitor_propagation,
            name="wdg-reticulum-propagation", daemon=True)
        self.propagation_thread = thread
        self.monitor_threads.append(thread)
        thread.start()
        return {
            "accepted": True,
            "node_hash": self.profile.propagation_node_hash,
            "max_messages": max_messages,
        }

    def _cancel_propagation(self) -> dict[str, Any]:
        self.router.cancel_propagation_node_requests()
        return self._emit_propagation_status()

    def _send_text(self, RNS, LXMF, payload: dict[str, Any]) -> dict[str, Any]:
        destination_hex = validate_destination_hash(
            payload.get("destination_hash", ""))
        text = validate_message_text(payload.get("text", ""))
        correlation_id = str(payload.get("correlation_id") or uuid.uuid4())
        if not self.announced:
            self._announce()
        destination_hash = bytes.fromhex(destination_hex)
        identity = RNS.Identity.recall(destination_hash)
        if identity is None:
            raise SidecarProtocolError(
                "destination identity is unknown; wait for its LXMF announce")
        destination = RNS.Destination(
            identity, RNS.Destination.OUT, RNS.Destination.SINGLE,
            "lxmf", "delivery")
        desired_method = (
            LXMF.LXMessage.PROPAGATED
            if self.profile.propagated_outbound
            else LXMF.LXMessage.DIRECT)
        message = LXMF.LXMessage(
            destination, self.delivery_destination, text,
            desired_method=desired_method, include_ticket=True)
        message.register_delivery_callback(
            lambda delivered: self._message_status(
                LXMF, delivered, correlation_id))
        message.register_failed_callback(
            lambda failed: self._message_status(
                LXMF, failed, correlation_id, "delivery failed"))
        self.outbound[correlation_id] = message
        self.router.handle_outbound(message)
        self._message_status(LXMF, message, correlation_id)
        monitor = threading.Thread(
            target=self._monitor_message,
            args=(LXMF, message, correlation_id),
            name="wdg-reticulum-lxmf-status", daemon=True)
        self.monitor_threads.append(monitor)
        monitor.start()
        return {"correlation_id": correlation_id}

    def initialise(self) -> tuple[Any, Any]:
        self._render_rns_config()
        try:
            import LXMF
            import RNS
        except ImportError as exc:
            raise RuntimeError(
                "Reticulum dependencies are missing; run setup.sh") from exc
        set_interface_event_sink(self.event)
        self.rns = RNS.Reticulum(
            configdir=str(self.state_dir / "rns"), loglevel=RNS.LOG_NOTICE)
        self.identity = self._load_identity(RNS)
        self._configure_display_name(RNS)
        lxmf_storage = ensure_private_dir(self.state_dir / "lxmf")
        self.router = LXMF.LXMRouter(storagepath=str(lxmf_storage))
        if self.profile.propagation_node_hash:
            self.router.set_outbound_propagation_node(bytes.fromhex(
                self.profile.propagation_node_hash))
        self.delivery_destination = self.router.register_delivery_identity(
            self.identity, display_name=self.profile.display_name)
        if self.delivery_destination is None:
            raise RuntimeError("LXMF delivery identity registration failed")
        self.router.register_delivery_callback(self._inbound_message)
        self._install_announce_handler(RNS, LXMF)
        return RNS, LXMF

    @staticmethod
    def _decode_request(packet: bytes) -> dict[str, Any]:
        if not packet or len(packet) > IPC_MAX_PACKET:
            raise SidecarProtocolError("invalid IPC packet size")
        try:
            message = json.loads(packet.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SidecarProtocolError("invalid IPC JSON") from exc
        if not isinstance(message, dict) or message.get("v") != IPC_VERSION:
            raise SidecarProtocolError("unsupported IPC envelope")
        if message.get("type") != "request":
            raise SidecarProtocolError("expected IPC request")
        if not isinstance(message.get("name"), str) \
                or not isinstance(message.get("payload"), dict):
            raise SidecarProtocolError("invalid IPC request fields")
        request_id = message.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise SidecarProtocolError("request_id is required")
        if message["name"] not in REQUEST_NAMES:
            raise SidecarProtocolError(
                f"unsupported request: {message['name']}")
        return message

    def _handle_request(self, RNS, LXMF, message: dict[str, Any]) -> bool:
        name = message["name"]
        payload = message["payload"]
        request_id = message["request_id"]
        try:
            if name == "hello":
                result = {"pid": os.getpid(), "protocol": IPC_VERSION}
            elif name == "status":
                result = {
                    "state": "ready", "announced": self.announced,
                    "identity_hash": self.delivery_destination.hash.hex(),
                }
            elif name == "announce":
                self._announce()
                result = {"announced": True}
            elif name == "send_text":
                result = self._send_text(RNS, LXMF, payload)
            elif name == "sync_propagation":
                result = self._sync_propagation(
                    payload.get("max_messages", 100))
            elif name == "cancel_propagation":
                result = self._cancel_propagation()
            elif name == "shutdown":
                self.send("reply", name, {"accepted": True}, request_id)
                self.stop_event.set()
                return False
            else:
                raise SidecarProtocolError(f"unsupported request: {name}")
            self.send("reply", name, result, request_id)
        except Exception as exc:
            self.send("reply", name, {
                "error": str(exc)[:240], "accepted": False,
            }, request_id)
        return True

    def run(self) -> int:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.sock = sock
        sock.settimeout(5.0)
        sock.connect(str(self.socket_path))
        RNS = LXMF = None
        code = 0
        try:
            RNS, LXMF = self.initialise()
            self.event("ready", {
                "identity_hash": self.delivery_destination.hash.hex(),
                "display_name": self.profile.display_name,
                "profile": {
                    "frequency_hz": self.profile.frequency_hz,
                    "bandwidth_hz": self.profile.bandwidth_hz,
                    "spreading_factor": self.profile.spreading_factor,
                    "coding_rate": self.profile.coding_rate,
                    "propagation_node_hash": (
                        self.profile.propagation_node_hash),
                    "propagated_outbound": (
                        self.profile.propagated_outbound),
                },
                "radio_owned": True,
            })
            sock.settimeout(0.25)
            while not self.stop_event.is_set():
                try:
                    packet = sock.recv(IPC_MAX_PACKET + 1)
                except socket.timeout:
                    continue
                if not packet:
                    break
                try:
                    message = self._decode_request(packet)
                    if not self._handle_request(RNS, LXMF, message):
                        break
                except SidecarProtocolError as exc:
                    self.event("error", {
                        "code": "ipc_protocol", "detail": str(exc),
                        "fatal": False,
                    })
            if self.fatal_error:
                code = 2
        except Exception as exc:
            code = 1
            self.event("error", {
                "code": "sidecar_start_failed", "detail": str(exc)[:240],
                "fatal": True,
            })
        finally:
            clean = True
            try:
                if RNS is not None and self.announce_handler is not None:
                    RNS.Transport.deregister_announce_handler(
                        self.announce_handler)
            except Exception:
                clean = False
            try:
                if self.router is not None:
                    self.router.exit_handler()
            except Exception:
                clean = False
            try:
                if RNS is not None:
                    RNS.Reticulum.exit_handler()
            except Exception:
                clean = False
            set_interface_event_sink(None)
            self.event("stopped", {
                "clean": clean, "radio_owned": False,
                "detail": self.fatal_error,
            })
            try:
                sock.close()
            except OSError:
                pass
            self.sock = None
        return code


def _parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="WatchDogsGo Reticulum sidecar")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--socket", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = _parse_args(argv)
    profile = Path(args.profile)
    socket_path = Path(args.socket)
    if not profile.is_absolute() or not socket_path.is_absolute():
        raise SystemExit("profile and socket paths must be absolute")
    runtime = SidecarRuntime(profile, socket_path)
    # RNS and LXMF install their own signal handlers during initialise().  The
    # parent normally uses the shutdown command; SIGTERM remains the bounded
    # fallback handled by those libraries.
    return runtime.run()


if __name__ == "__main__":
    raise SystemExit(main())
