"""The fork socket and legacy TCP client share one stable manager facade."""

import json
import socket
import threading
import time
from queue import Empty, Queue
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from watchdogs.meshtastic_manager import (
    MeshtasticManager,
    WDG_MAX_PACKET,
    WDG_PROTOCOL_MAJOR,
)


class FakePub:
    def __init__(self):
        self.listeners = {}

    def subscribe(self, callback, topic):
        self.listeners.setdefault(topic, []).append(callback)

    def unsubscribe(self, callback, topic):
        self.listeners.get(topic, []).remove(callback)


class FakeInterface:
    instances = []

    def __init__(self, hostname, portNumber, timeout):
        self.hostname = hostname
        self.portNumber = portNumber
        self.timeout = timeout
        self.myInfo = NS(my_node_num=1)
        self.nodesByNum = {
            1: {"num": 1, "user": {"id": "!00000001",
                                      "longName": "WDG"}},
            2: {"num": 2, "user": {"id": "!00000002",
                                      "longName": "Nearby"},
                "position": {"latitude": 40.1, "longitude": -90.2},
                "hopsAway": 1},
        }
        self.nodes = {"!00000002": self.nodesByNum[2]}
        self.localNode = NS(channels=[
            NS(role=1, settings=NS(name="LongFast")),
            NS(role=0, settings=NS(name="")),
        ])
        self.sendText = Mock(return_value=NS(id=123))
        self.sendData = Mock(return_value=NS(id=456))
        self.close = Mock()
        self.instances.append(self)


class PortNums:
    class PortNum:
        NODEINFO_APP = 4


def wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def manager(pub=None, probe=lambda _host, _port: True, runner=None, **kwargs):
    pub = pub or FakePub()
    runner = runner or Mock(
        return_value=NS(returncode=0, stdout="", stderr=""))
    return MeshtasticManager(
        dependency_loader=lambda: (FakeInterface, pub, PortNums, "^all"),
        port_probe=probe, service_runner=runner, sleep=lambda _seconds: None,
        backend_mode="legacy_tcp", **kwargs), pub


class FakeWdgDaemon:
    """Packet-preserving peer for transport and reconnect tests.

    The CI sandbox denies creation of Unix ``SOCK_SEQPACKET`` pairs.  This
    double retains send/recv packet boundaries and timeout/disconnect behavior
    while the production connector still uses a real AF_UNIX socket.
    """

    def __init__(self, path, *, protocol_version=WDG_PROTOCOL_MAJOR,
                 minimal=False):
        self.path = str(path)
        self.protocol_version = protocol_version
        self.minimal = minimal
        self.commands = []
        self.connections = 0
        self.fail_commands = {}
        self.lease_granted = True
        self._client = None
        self._client_lock = threading.Lock()

    def connect(self, path):
        assert path == self.path
        caller = FakePacketSocket(self)
        with self._client_lock:
            self._client = caller
        self.connections += 1
        return caller

    @staticmethod
    def _packet(message):
        return json.dumps(message, separators=(",", ":")).encode()

    def _send(self, client, message):
        client.push(self._packet(message))

    def _reply(self, client, request, body=None, *, ok=True,
               message="", error_code=""):
        value = {
            "v": 1, "type": "reply",
            "request_id": request["request_id"], "ok": ok,
        }
        if ok:
            value["body"] = body or {}
        else:
            value.update(message=message, error_code=error_code)
        self._send(client, value)

    def emit(self, name, body=None):
        with self._client_lock:
            client = self._client
        if client is None:
            raise AssertionError("daemon has no connected client")
        self._send(client, {
            "v": 1, "type": "event", "name": name, "body": body or {},
        })

    def disconnect(self):
        with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()

    def close(self):
        self.disconnect()

    def handle(self, client, payload):
        request = json.loads(payload)
        self.commands.append(request)
        name = request["name"]
        if name in self.fail_commands:
            self._reply(client, request, ok=False,
                        error_code="rejected",
                        message=self.fail_commands[name])
            return
        if name == "hello":
            capabilities = ["hello", "get_status"] if self.minimal else [
                "hello", "get_status", "snapshot_nodes",
                "send_text", "request_node_info", "set_phone_ble",
                "open_pairing", "forget_phone", "ble_scan_lease_acquire",
                "ble_scan_lease_release", "retry_shared_adapter"]
            self._reply(client, request, {
                "protocol_version": self.protocol_version,
                "max_packet_bytes": WDG_MAX_PACKET,
                "capabilities": capabilities,
            })
        elif name == "get_status":
            if self.minimal:
                self._reply(client, request, {
                    "state": "ready", "client_connected": True,
                    "pending_replies": 0,
                })
                return
            self._reply(client, request, {
                "state": "ready",
                "identity": {"node_id": "!00000001", "name": "WDG"},
                "channels": [{"index": 0, "name": "LongFast", "role": 1}],
                "packets_received": 7,
                "ble_status": "advertising",
                "phone_connected": False,
                "radio_status": "ready",
            })
        elif name == "snapshot_nodes" and not self.minimal:
            self._reply(client, request)
            self._send(client, {"v": 1, "type": "event",
                                "name": "snapshot_begin", "body": {}})
            self._send(client, {
                "v": 1, "type": "event", "name": "node", "body": {
                    "id": "!00000002", "name": "Nearby",
                    "lat": 40.1, "lon": -90.2, "hops": 1,
                    "rssi": -91, "snr": 4.5,
                }})
            self._send(client, {"v": 1, "type": "event",
                                "name": "snapshot_complete", "body": {}})
        elif name == "send_text":
            self._reply(client, request, {"packet_id": 123})
        elif name == "request_node_info":
            self._reply(client, request, {
                "message": "Zero-hop NodeInfo request sent"})
        elif name == "ble_scan_lease_acquire":
            self._reply(client, request, {
                "granted": self.lease_granted,
                "reason": ("adapter yielded" if self.lease_granted
                           else "phone has priority")})
        elif name in ("set_phone_ble", "open_pairing", "forget_phone",
                      "ble_scan_lease_release", "retry_shared_adapter"):
            self._reply(client, request)
        else:
            self._reply(client, request, ok=False,
                        error_code="unsupported_command",
                        message="unsupported command")


class FakePacketSocket:
    def __init__(self, daemon):
        self.daemon = daemon
        self.timeout = 0.25
        self.incoming = Queue()
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def send(self, payload):
        if self.closed:
            raise OSError("socket closed")
        self.daemon.handle(self, payload)
        return len(payload)

    def recv(self, _maximum):
        try:
            return self.incoming.get(timeout=self.timeout)
        except Empty as exc:
            raise socket.timeout() from exc

    def push(self, payload):
        if not self.closed:
            self.incoming.put(payload)

    def shutdown(self, _how):
        self.disconnect()

    def close(self):
        self.disconnect()

    def disconnect(self):
        if not self.closed:
            self.closed = True
            self.incoming.put(b"")


def test_client_connects_to_existing_daemon_and_normalizes_nodes():
    mt, pub = manager()
    mt.start()
    wait_for(lambda: mt.connected)

    assert mt.local_node_id == "!00000001"
    assert mt.local_name == "WDG"
    assert mt.channels == [{"index": 0, "name": "LongFast"}]
    assert mt.nodes["!00000002"]["name"] == "Nearby"
    assert mt.nodes["!00000002"]["cached"] is True

    iface = FakeInterface.instances[-1]
    for callback in pub.listeners["meshtastic.receive"]:
        callback({
            "from": 2, "channel": 0, "rxRssi": -91, "rxSnr": 4.5,
            "hopStart": 3, "hopLimit": 2,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
        }, iface)
    events = mt.poll_events()
    message = next(value for kind, value in events if kind == "message")
    assert message == {
        "text": "hello", "sender": "Nearby", "sender_id": "!00000002",
        "channel": 0, "rssi": -91.0, "snr": 4.5, "hops": 1}
    assert mt.nodes["!00000002"]["cached"] is False
    mt.close()
    assert not mt.running and not mt.connected


def test_send_and_zero_hop_discovery_use_official_client_api():
    mt, _pub = manager()
    mt.start()
    wait_for(lambda: mt.connected)
    iface = FakeInterface.instances[-1]

    assert mt.send_text("direct", "!00000002", channel=1)
    assert mt.request_discovery()
    wait_for(lambda: iface.sendText.called and iface.sendData.called)

    iface.sendText.assert_called_once_with(
        "direct", channelIndex=1, wantAck=True,
        destinationId="!00000002")
    iface.sendData.assert_called_once_with(
        b"", destinationId="^all", portNum=4,
        wantAck=False, wantResponse=True, hopLimit=0)
    mt.close()


def test_manager_starts_service_only_when_tcp_is_unavailable():
    probes = iter([False, True])
    runner = Mock(return_value=NS(returncode=0, stdout="", stderr=""))
    mt, _pub = manager(probe=lambda _host, _port: next(probes),
                       runner=runner)
    mt.start()
    wait_for(lambda: mt.connected)
    runner.assert_called_once_with(
        ["systemctl", "start", "meshtasticd.service"],
        capture_output=True, text=True, timeout=12)
    assert mt.started_daemon
    mt.close()


def test_explicit_radio_release_stops_service():
    runner = Mock(return_value=NS(returncode=0, stdout="", stderr=""))
    mt, _pub = manager(runner=runner)
    mt.start()
    wait_for(lambda: mt.connected)
    mt.close(stop_daemon=True)
    runner.assert_called_once_with(
        ["systemctl", "stop", "meshtasticd.service"],
        capture_output=True, text=True, timeout=12)


def test_close_before_first_start_does_not_poison_next_connection():
    runner = Mock(return_value=NS(returncode=0, stdout="", stderr=""))
    mt, _pub = manager(runner=runner)

    mt.close(stop_daemon=True)
    mt.start()
    wait_for(lambda: mt.connected)

    assert mt.running
    mt.close()


def test_fork_socket_negotiates_snapshots_and_normalizes_commands(tmp_path):
    daemon = FakeWdgDaemon(tmp_path / "wdg.sock")
    tcp_loader = Mock(side_effect=AssertionError("TCP backend must not open"))
    service = Mock(side_effect=AssertionError("existing socket needs no service"))
    mt = MeshtasticManager(
        backend_mode="auto", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect,
        dependency_loader=tcp_loader, service_runner=service)
    try:
        mt.start()
        wait_for(lambda: mt.connected and "!00000002" in mt.nodes)

        assert mt.active_backend == "fork_socket"
        assert mt.local_node_id == "!00000001"
        assert mt.local_name == "WDG"
        assert mt.channels == [{"index": 0, "name": "LongFast"}]
        assert mt.nodes["!00000002"]["name"] == "Nearby"
        assert mt.nodes["!00000002"]["cached"] is True
        assert mt.packets_received == 7
        tcp_loader.assert_not_called()
        service.assert_not_called()

        daemon.emit("message", {
            "text": "hello", "sender_id": "!00000002", "channel": 0,
            "rssi": -90, "snr": 3.5, "hops": 1,
        })
        assert mt.send_text("direct", "!00000002", channel=1)
        assert mt.request_discovery()
        wait_for(lambda: any(c["name"] == "request_node_info"
                             for c in daemon.commands))
        wait_for(lambda: any(kind == "message" for kind, _ in mt.queue.queue))

        send = next(c for c in daemon.commands if c["name"] == "send_text")
        assert send["body"] == {
            "text": "direct", "destination": "!00000002",
            "channel": 1, "want_ack": True,
        }
        discovery = next(
            c for c in daemon.commands if c["name"] == "request_node_info")
        assert discovery["body"] == {"hop_limit": 0}

        events = mt.poll_events()
        message = next(value for kind, value in events if kind == "message")
        assert message == {
            "text": "hello", "sender": "Nearby",
            "sender_id": "!00000002", "channel": 0,
            "rssi": -90.0, "snr": 3.5, "hops": 1,
        }
        assert any(kind == "sent" and value["packet_id"] == 123
                   for kind, value in events)
        assert any(kind == "discovery" for kind, _value in events)
    finally:
        mt.close()
        daemon.close()


def test_socket_overflow_requests_one_fresh_snapshot(tmp_path):
    daemon = FakeWdgDaemon(tmp_path / "wdg.sock")
    mt = MeshtasticManager(
        backend_mode="fork_socket", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect)
    try:
        mt.start()
        wait_for(lambda: mt.connected and sum(
            c["name"] == "snapshot_nodes" for c in daemon.commands) == 1)
        daemon.emit("overflow", {"dropped": 4, "resync_required": True})
        wait_for(lambda: sum(c["name"] == "snapshot_nodes"
                             for c in daemon.commands) == 2)
        assert any("overflow (4)" in str(value)
                   for kind, value in mt.poll_events() if kind == "status")
    finally:
        mt.close()
        daemon.close()


def test_minimal_firmware_skeleton_status_keeps_connection_alive(tmp_path):
    daemon = FakeWdgDaemon(tmp_path / "wdg.sock", minimal=True)
    mt = MeshtasticManager(
        backend_mode="fork_socket", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect)
    try:
        mt.start()
        wait_for(lambda: mt.connected and any(
            command["name"] == "snapshot_nodes"
            for command in daemon.commands))
        wait_for(lambda: "unsupported command" in mt.last_error)

        assert mt.connected
        assert mt.local_name == "Meshtastic"
        assert mt.ble_status == "unknown"
        assert mt.radio_status == "unknown"
    finally:
        mt.close()
        daemon.close()


def test_socket_control_commands_have_correlated_results_and_status(tmp_path):
    daemon = FakeWdgDaemon(tmp_path / "wdg.sock")
    mt = MeshtasticManager(
        backend="fork_socket", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect,
        phone_ble_enabled=True, phone_ble_adapter="AA:BB:CC:DD:EE:FF")
    try:
        mt.start()
        wait_for(lambda: mt.connected and any(
            command["name"] == "set_phone_ble"
            for command in daemon.commands))

        assert mt.backend == "fork_socket"
        assert mt.ble_status == "advertising"
        assert mt.phone_connected is False
        assert mt.radio_status == "ready"
        configured = next(command for command in daemon.commands
                          if command["name"] == "set_phone_ble")
        assert configured["body"] == {
            "enabled": True, "adapter": "AA:BB:CC:DD:EE:FF"}

        assert mt.set_phone_ble(False, "auto")
        assert mt.open_pairing(999)
        assert mt.forget_phone()
        assert mt.acquire_ble_scan_lease(999) == (True, "adapter yielded")
        assert mt.release_ble_scan_lease()
        assert mt.retry_shared_adapter()

        pairing = next(command for command in daemon.commands
                       if command["name"] == "open_pairing")
        lease = next(command for command in daemon.commands
                     if command["name"] == "ble_scan_lease_acquire")
        assert pairing["body"] == {"seconds": 120}
        assert lease["body"] == {"seconds": 20}

        daemon.emit("phone_connected", {"message": "phone connected"})
        daemon.emit("ble_status", {"state": "connected"})
        daemon.emit("radio_status", {"state": "degraded"})
        wait_for(lambda: mt.phone_connected and
                 mt.ble_status == "connected" and
                 mt.radio_status == "degraded")
    finally:
        mt.close()
        daemon.close()


def test_socket_control_rejection_returns_reason_without_disconnect(tmp_path):
    daemon = FakeWdgDaemon(tmp_path / "wdg.sock")
    daemon.lease_granted = False
    daemon.fail_commands["forget_phone"] = "no bonded phone"
    mt = MeshtasticManager(
        backend_mode="fork_socket", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect)
    try:
        mt.start()
        wait_for(lambda: mt.connected)
        assert mt.acquire_ble_scan_lease() == (False, "phone has priority")
        assert not mt.forget_phone()
        assert mt.last_error == "no bonded phone"
        assert mt.connected
    finally:
        mt.close()
        daemon.close()


def test_suspend_then_resume_clears_client_stop_before_readiness_poll():
    state = {"starting": False, "polls": 0}
    calls = []

    def probe(_path):
        if not state["starting"]:
            return False
        state["polls"] += 1
        return state["polls"] >= 2

    def runner(args, **_kwargs):
        calls.append(args)
        if args[1] == "start":
            state["starting"] = True
        elif args[1] == "stop":
            state["starting"] = False
            state["polls"] = 0
        return NS(returncode=0, stdout="", stderr="")

    mt = MeshtasticManager(
        backend_mode="fork_socket", socket_probe=probe,
        service_runner=runner, sleep=lambda _seconds: None)

    assert mt.suspend_service()
    assert mt.resume_service(connect=False)
    assert state["polls"] >= 2
    assert [call[1] for call in calls] == ["stop", "start"]
    assert not mt._stop_event.is_set()


def test_service_handoff_uses_injected_allowlisted_controller():
    class Controller:
        def __init__(self):
            self.active = True
            self.calls = []

        def status(self, target):
            self.calls.append(("status", target))
            return NS(active=self.active, load_state="loaded")

        def stop(self, target):
            self.calls.append(("stop", target))
            self.active = False
            return NS(active=False, load_state="loaded")

        def start(self, target):
            self.calls.append(("start", target))
            self.active = True
            return NS(active=True, load_state="loaded")

    controller = Controller()
    mt = MeshtasticManager(
        backend_mode="fork_socket", service_controller=controller,
        socket_probe=lambda _path: controller.active,
        sleep=lambda _seconds: None)

    assert mt.suspend_service()
    assert mt.resume_service(connect=False)
    assert ("stop", "wdg") in controller.calls
    assert ("start", "wdg") in controller.calls
    assert all(target == "wdg" for _action, target in controller.calls)


def test_backend_alias_is_compatible_with_existing_no_arg_constructor():
    assert MeshtasticManager(backend="fork_socket").backend == "fork_socket"
    assert MeshtasticManager().backend == "auto"
    with pytest.raises(ValueError, match="Conflicting Meshtastic backend"):
        MeshtasticManager(backend_mode="legacy_tcp", backend="fork_socket")


def test_socket_reconnects_and_resnapshots_without_tcp_fallback(tmp_path):
    daemon = FakeWdgDaemon(tmp_path / "wdg.sock")
    tcp_loader = Mock(side_effect=AssertionError("TCP backend must not open"))
    mt = MeshtasticManager(
        backend_mode="auto", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect,
        dependency_loader=tcp_loader)
    try:
        mt.start()
        wait_for(lambda: mt.connected and daemon.connections == 1)
        daemon.disconnect()
        wait_for(lambda: daemon.connections >= 2 and mt.connected, timeout=3)
        wait_for(lambda: sum(c["name"] == "snapshot_nodes"
                             for c in daemon.commands) >= 2)
        tcp_loader.assert_not_called()
        assert sum(kind == "connected" for kind, _ in mt.poll_events()) >= 2
    finally:
        mt.close()
        daemon.close()


def test_incompatible_fork_never_falls_through_to_phoneapi_tcp(tmp_path):
    daemon = FakeWdgDaemon(tmp_path / "wdg.sock", protocol_version=2)
    tcp_loader = Mock(side_effect=AssertionError("TCP backend must not open"))
    mt = MeshtasticManager(
        backend_mode="auto", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect,
        dependency_loader=tcp_loader)
    try:
        mt.start()
        wait_for(lambda: not mt.running)
        errors = [str(value) for kind, value in mt.poll_events()
                  if kind == "error"]
        assert any("API major 2" in value and "expected 1" in value
                   for value in errors)
        tcp_loader.assert_not_called()
        assert not mt.connected
    finally:
        mt.close()
        daemon.close()


def test_auto_uses_legacy_only_when_fork_socket_and_unit_are_absent():
    pub = FakePub()
    calls = []

    def runner(args, **_kwargs):
        calls.append(args)
        if args[1] == "show":
            return NS(returncode=0, stdout="not-found\n", stderr="")
        return NS(returncode=0, stdout="", stderr="")

    mt = MeshtasticManager(
        backend_mode="auto", socket_probe=lambda _path: False,
        service_runner=runner, port_probe=lambda _host, _port: True,
        dependency_loader=lambda: (FakeInterface, pub, PortNums, "^all"))
    try:
        mt.start()
        wait_for(lambda: mt.connected)
        assert mt.active_backend == "legacy_tcp"
        assert calls == [["systemctl", "show", "--property=LoadState",
                          "--value", "meshtasticd-wdg.service"]]
    finally:
        mt.close()


def test_legacy_tcp_cannot_compete_with_phone_enabled_fork():
    tcp_loader = Mock(side_effect=AssertionError("TCP backend must not open"))
    mt = MeshtasticManager(
        backend_mode="legacy_tcp", socket_probe=lambda _path: True,
        port_probe=lambda _host, _port: True,
        dependency_loader=tcp_loader)
    mt.start()
    wait_for(lambda: not mt.running)

    assert not mt.connected
    assert "blocked" in mt.last_error
    tcp_loader.assert_not_called()


def test_explicit_phone_ble_disable_allows_legacy_tcp_fallback():
    pub = FakePub()
    mt = MeshtasticManager(
        backend_mode="legacy_tcp", phone_ble_enabled=False,
        socket_probe=lambda _path: True,
        port_probe=lambda _host, _port: True,
        dependency_loader=lambda: (FakeInterface, pub, PortNums, "^all"))
    try:
        mt.start()
        wait_for(lambda: mt.connected)
        assert mt.active_backend == "legacy_tcp"
    finally:
        mt.close()


@pytest.mark.parametrize("mode", ["", "fork", "tcp", "AUTO"])
def test_rejects_unknown_backend_modes(mode):
    with pytest.raises(ValueError, match="Unsupported Meshtastic backend"):
        MeshtasticManager(backend_mode=mode)
