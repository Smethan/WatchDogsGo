"""Background client for a local ``meshtasticd`` instance.

The daemon owns the SX1262 hardware.  WatchDogsGo only talks to its official
Meshtastic Client API over localhost TCP port 4403, which keeps SPI/GPIO access
in one process and lets the daemon continue serving other clients after WDG
closes.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from queue import Empty, Queue
from typing import Any, Callable


MESHTASTIC_HOST = "127.0.0.1"
MESHTASTIC_PORT = 4403
MT_DISCOVERY_INTERVAL = 60.0
MT_DISCOVERY_MIN_DISTANCE_M = 50.0


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


class MeshtasticManager:
    """Own one nonblocking Meshtastic TCP client and normalize its events."""

    def __init__(
        self,
        host: str = MESHTASTIC_HOST,
        port: int = MESHTASTIC_PORT,
        *,
        dependency_loader: Callable = _default_dependencies,
        port_probe: Callable[[str, int], bool] = _port_open,
        service_runner: Callable[..., Any] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.host = host
        self.port = port
        self.queue: Queue = Queue()
        self.running = False
        self.connected = False
        self.nodes: dict[str, dict] = {}
        self.channels: list[dict] = [{"index": 0, "name": "Primary"}]
        self.local_node_id = ""
        self.local_name = "Meshtastic"
        self.packets_received = 0

        self._dependency_loader = dependency_loader
        self._port_probe = port_probe
        self._service_runner = service_runner
        self._sleep = sleep
        self._interface = None
        self._pub = None
        self._portnums = None
        self._broadcast_addr = "^all"
        self._commands: Queue = Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._subscriptions: list[tuple[Callable, str]] = []
        self.started_daemon = False

    def _emit(self, kind: str, value: Any) -> None:
        self.queue.put((kind, value))

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

    def poll_events(self) -> list[tuple[str, Any]]:
        events = []
        while True:
            try:
                events.append(self.queue.get_nowait())
            except Empty:
                return events

    def _ensure_service(self) -> bool:
        if self._port_probe(self.host, self.port):
            return True
        try:
            result = self._service_runner(
                ["systemctl", "start", "meshtasticd"],
                capture_output=True, text=True, timeout=12)
        except Exception as exc:
            self._emit("error", f"Could not start meshtasticd: {exc}")
            return False
        if result.returncode != 0:
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

    def _stop_service(self) -> None:
        try:
            result = self._service_runner(
                ["systemctl", "stop", "meshtasticd"],
                capture_output=True, text=True, timeout=12)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "systemctl failed").strip()
                self._emit("error", "Could not stop meshtasticd: " + detail[:160])
            else:
                self._emit("status", "meshtasticd stopped; SX1262 released")
        except Exception as exc:
            self._emit("error", f"Could not stop meshtasticd: {exc}")
        self.started_daemon = False

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
            if not self._ensure_service() or self._stop_event.is_set():
                return
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
            with self._lock:
                self.running = False
                if self._thread is threading.current_thread():
                    self._thread = None

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
