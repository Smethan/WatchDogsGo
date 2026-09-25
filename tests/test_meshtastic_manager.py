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
    _ControlWaiter,
    _ServiceSnapshot,
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


def no_fork_runner(args, **_kwargs):
    if args[1] == "show":
        missing = "meshtasticd-wdg.service" in args
        return NS(
            returncode=0,
            stdout=("LoadState=not-found\nActiveState=inactive\n"
                    "UnitFileState=not-found\n"
                    if missing else
                    "LoadState=loaded\nActiveState=active\n"
                    "UnitFileState=enabled\n"),
            stderr="")
    return NS(returncode=0, stdout="", stderr="")


def manager(pub=None, probe=lambda _host, _port: True, runner=None, **kwargs):
    pub = pub or FakePub()
    if runner is None:
        runner = Mock(side_effect=no_fork_runner)
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
                "ble_scan_lease_release", "pairing_agent_lease_acquire",
                "pairing_agent_lease_release", "retry_shared_adapter"]
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
                "full_client_owner": "none",
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
                      "ble_scan_lease_release", "pairing_agent_lease_acquire",
                      "pairing_agent_lease_release", "retry_shared_adapter"):
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
    runner = Mock(side_effect=no_fork_runner)
    mt, _pub = manager(probe=lambda _host, _port: next(probes),
                       runner=runner)
    mt.start()
    wait_for(lambda: mt.connected)
    runner.assert_any_call(
        ["systemctl", "start", "meshtasticd.service"],
        capture_output=True, text=True, timeout=12)
    assert sum(
        call.args[0][1:3] == ["start", "meshtasticd.service"]
        for call in runner.call_args_list) == 1
    assert mt.started_daemon
    mt.close()


def test_explicit_radio_release_stops_service():
    active = {
        "meshtasticd-wdg.service": True,
        "meshtasticd.service": True,
    }

    def runner(args, **_kwargs):
        if args[1] == "show":
            service = args[2]
            state = "active" if active[service] else "inactive"
            return NS(
                returncode=0,
                stdout=f"LoadState=loaded\nActiveState={state}\n",
                stderr="")
        if args[1] == "stop":
            active[args[2]] = False
        return NS(returncode=0, stdout="", stderr="")

    runner = Mock(side_effect=runner)
    mt, _pub = manager(runner=runner)
    mt.close(stop_daemon=True)
    runner.assert_any_call(
        ["systemctl", "stop", "meshtasticd-wdg.service"],
        capture_output=True, text=True, timeout=12)
    runner.assert_any_call(
        ["systemctl", "stop", "meshtasticd.service"],
        capture_output=True, text=True, timeout=12)
    runner.assert_any_call(
        ["systemctl", "show", "meshtasticd-wdg.service",
         "--property=LoadState", "--property=ActiveState"],
        capture_output=True, text=True, timeout=12)
    runner.assert_any_call(
        ["systemctl", "show", "meshtasticd.service",
         "--property=LoadState", "--property=ActiveState"],
        capture_output=True, text=True, timeout=12)


def test_close_before_first_start_does_not_poison_next_connection():
    runner = Mock(side_effect=no_fork_runner)
    mt, _pub = manager(runner=runner)

    mt.close(stop_daemon=True)
    mt.start()
    wait_for(lambda: mt.connected)

    assert mt.running
    mt.close()


def test_close_reports_stuck_worker_and_does_not_stop_services():
    class StuckThread:
        def __init__(self):
            self.join_timeout = None

        def is_alive(self):
            return True

        def join(self, timeout):
            self.join_timeout = timeout

    runner = Mock(side_effect=no_fork_runner)
    mt = MeshtasticManager(
        backend_mode="fork_socket", service_runner=runner)
    stuck = StuckThread()
    mt._thread = stuck
    mt.running = True
    mt.connected = True

    assert not mt.close(stop_daemon=True)
    assert mt.running
    assert mt._thread is stuck
    assert stuck.join_timeout == 2.0
    assert not any(
        call.args[0][1] == "stop" for call in runner.call_args_list)
    assert "worker did not stop" in mt.last_error


def test_close_is_a_hard_barrier_against_concurrent_start():
    starts = []
    mt = MeshtasticManager(service_runner=Mock(side_effect=no_fork_runner))

    class StuckThread:
        def is_alive(self):
            return True

        def join(self, timeout):
            assert timeout == 2.0
            starts.append(mt.start())

    mt._thread = StuckThread()
    mt.running = True

    assert not mt.close()
    assert starts == [False]
    assert mt.running
    assert mt._closing is False


def test_backend_mode_change_preserves_service_rollback_until_resolution():
    snapshot = {
        "wdg": _ServiceSnapshot("wdg", "loaded", "inactive", "disabled"),
        "stock": _ServiceSnapshot("stock", "loaded", "active", "enabled"),
    }
    mt = MeshtasticManager()
    mt._backend_service_rollback = snapshot

    assert mt.set_backend_mode("fork_socket")
    assert mt._backend_service_rollback is snapshot
    mt._restore_service_snapshot = Mock(return_value=True)
    assert mt.rollback_backend_service_activation()
    mt._restore_service_snapshot.assert_called_once_with(
        snapshot, timeout=15.0)
    assert mt._backend_service_rollback is None


def test_unresolved_backend_activation_cannot_overwrite_rollback_snapshot():
    snapshot = {
        "wdg": _ServiceSnapshot("wdg", "loaded", "inactive", "disabled"),
        "stock": _ServiceSnapshot("stock", "loaded", "active", "enabled"),
    }
    mt = MeshtasticManager()
    mt._backend_service_rollback = snapshot
    mt._capture_service_snapshot = Mock(
        side_effect=AssertionError("must preserve the first snapshot"))

    assert not mt.activate_backend_service("fork_socket")
    assert mt._backend_service_rollback is snapshot
    assert "commit or roll it back" in mt.last_error


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
        assert mt.full_client_owner == "none"
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
        assert mt.set_phone_ble(True, "auto")
        assert mt.open_pairing(999)
        assert mt.forget_phone()
        assert mt.acquire_ble_scan_lease(999) == (True, "adapter yielded")
        assert mt.release_ble_scan_lease()
        assert mt.acquire_pairing_agent_lease(999)
        assert mt.release_pairing_agent_lease()
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
        daemon.emit("full_client_owner", {"owner": "tcp"})
        daemon.emit("pairing_pin", {"pin": "123456"})
        wait_for(lambda: mt.phone_connected and
                 mt.ble_status == "connected" and
                 mt.radio_status == "degraded" and
                 mt.full_client_owner == "tcp" and
                 mt.pairing_pin == "123456")
    finally:
        mt.close()
        daemon.close()
    assert mt.full_client_owner == "unknown"


def test_scan_lease_on_stopped_manager_connects_without_mutex_timeout(tmp_path):
    daemon = FakeWdgDaemon(tmp_path / "wdg.sock")
    mt = MeshtasticManager(
        backend_mode="fork_socket", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect)
    started = time.monotonic()
    try:
        assert not mt.running
        assert mt.acquire_ble_scan_lease(owner="first-scan") == (
            True, "adapter yielded")
        assert time.monotonic() - started < 2.0
        assert mt.running and mt.connected
        assert mt._ble_scan_lease_owner == "first-scan"
        assert mt.release_ble_scan_lease(owner="first-scan")
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


def test_socket_disconnect_clears_every_session_status_without_hiding_service(
        tmp_path):
    daemon = FakeWdgDaemon(tmp_path / "wdg.sock")
    mt = MeshtasticManager(
        backend_mode="fork_socket", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect)
    mt._service_states["wdg"] = "active"
    try:
        mt.start()
        wait_for(lambda: mt.connected)
        daemon.emit("phone_connected")
        daemon.emit("ble_status", {"state": "connected", "degraded": True})
        daemon.emit("radio_status", {"state": "ready"})
        daemon.emit("full_client_owner", {"owner": "bluetooth"})
        daemon.emit("pairing_pin", {"pin": "123456"})
        wait_for(lambda: mt.pairing_pin == "123456")

        # Prevent the reconnect loop from immediately repopulating status so
        # the transport-loss boundary itself can be inspected.
        mt._stop_event.set()
        daemon.disconnect()
        wait_for(lambda: not mt.running)

        assert not mt.connected
        assert not mt.phone_connected
        assert mt.pairing_pin == ""
        assert mt.ble_status == "unknown"
        assert mt.radio_status == "unknown"
        assert mt.full_client_owner == "unknown"
        assert not mt.host_ble_degraded
        assert mt.host_ble_pause_reason == ""
        assert mt.service_state == "active"
    finally:
        mt.close()
        daemon.close()


def test_pairing_agent_release_failure_retains_retryable_ownership():
    mt = MeshtasticManager()
    mt._pairing_agent_lease_active = True
    mt._fork_may_own_resources = Mock(return_value=True)
    mt.active_backend = "fork_socket"
    mt.connected = True
    mt._control_command = Mock(return_value=(False, "busy", {}))

    assert not mt.release_pairing_agent_lease()
    assert mt.pairing_agent_lease_active

    mt._control_command.return_value = (True, "", {})
    assert mt.release_pairing_agent_lease()
    assert not mt.pairing_agent_lease_active


def test_repeated_phone_disconnects_degrade_shared_host_scanning(tmp_path):
    now = [10.0]
    daemon = FakeWdgDaemon(tmp_path / "wdg.sock")
    mt = MeshtasticManager(
        backend_mode="fork_socket", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect,
        monotonic=lambda: now[0], phone_ble_enabled=True)
    try:
        mt.start()
        wait_for(lambda: mt.connected)
        assert mt.acquire_ble_scan_lease() == (True, "adapter yielded")
        for index in range(2):
            daemon.emit("phone_connected")
            daemon.emit("phone_disconnected")
            now[0] += 30
            wait_for(lambda: len(mt._phone_scan_disconnects) == index + 1)

        assert mt.host_ble_degraded
        assert mt.ble_scan_lease_active
        assert mt._ble_scan_lease_owner == "default"
        assert mt.acquire_ble_scan_lease() == (
            False,
            "Meshtastic phone disconnected repeatedly during host BLE scanning")
        assert mt.release_ble_scan_lease()
        assert mt.retry_shared_adapter()
        assert not mt.host_ble_degraded
    finally:
        mt.close()
        daemon.close()


def test_immediate_bluez_failure_only_degrades_when_phone_is_connected():
    mt = MeshtasticManager()
    mt.note_host_ble_failure("not powered")
    assert not mt.host_ble_degraded
    mt.phone_connected = True
    mt.note_host_ble_failure("org.bluez.Error.InProgress")
    assert mt.host_ble_degraded
    assert mt.host_ble_pause_reason == "org.bluez.Error.InProgress"


def test_degradation_preserves_lease_until_owning_scanner_releases(tmp_path):
    daemon = FakeWdgDaemon(tmp_path / "wdg.sock")
    mt = MeshtasticManager(
        backend_mode="fork_socket", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect)
    try:
        mt.start()
        wait_for(lambda: mt.connected)
        daemon.emit("phone_connected")
        wait_for(lambda: mt.phone_connected)
        assert mt.acquire_ble_scan_lease(owner="wardrive")[0]

        mt.note_host_ble_failure("org.bluez.Error.InProgress")

        assert mt.host_ble_degraded
        assert mt.ble_scan_lease_active
        assert mt._ble_scan_lease_owner == "wardrive"
        assert mt.release_ble_scan_lease(owner="watch")
        assert mt.ble_scan_lease_active
        assert mt.release_ble_scan_lease(owner="wardrive")
        assert not mt.ble_scan_lease_active
        assert any(command["name"] == "ble_scan_lease_release"
                   for command in daemon.commands)
    finally:
        mt.close()
        daemon.close()


def test_degradation_during_acquire_compensates_before_reporting_denial(
        tmp_path):
    class DegradingDaemon(FakeWdgDaemon):
        def handle(self, client, payload):
            request = json.loads(payload)
            if request["name"] != "ble_scan_lease_acquire":
                return super().handle(client, payload)
            self.commands.append(request)
            self._send(client, {
                "v": 1, "type": "event", "name": "ble_status",
                "body": {"degraded": True, "reason": "phone priority"},
            })
            self._reply(client, request, {
                "granted": True, "reason": "adapter yielded"})

    daemon = DegradingDaemon(tmp_path / "wdg.sock")
    mt = MeshtasticManager(
        backend_mode="fork_socket", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect)
    try:
        mt.start()
        wait_for(lambda: mt.connected)

        assert mt.acquire_ble_scan_lease(owner="wardrive") == (
            False, "phone priority")
        wait_for(lambda: any(
            command["name"] == "ble_scan_lease_release"
            for command in daemon.commands))
        assert not mt.ble_scan_lease_active
        assert mt._ble_scan_lease_owner == ""
    finally:
        mt.close()
        daemon.close()


def test_phone_policy_updates_offline_and_clears_obsolete_scan_degradation():
    mt = MeshtasticManager(
        backend_mode="fork_socket", socket_probe=lambda _path: False,
        service_runner=Mock(side_effect=no_fork_runner))
    mt.host_ble_degraded = True
    mt.host_ble_pause_reason = "old conflict"

    assert mt.set_phone_ble(False, "AA:BB:CC:DD:EE:FF")
    assert mt._phone_ble_enabled is False
    assert mt._phone_ble_adapter == "AA:BB:CC:DD:EE:FF"
    assert not mt.host_ble_degraded
    assert mt.acquire_ble_scan_lease() == (
        True, "meshtasticd-wdg is not active")


def test_lease_bypass_uses_applied_daemon_adapter_not_desired_setting():
    mt = MeshtasticManager(
        backend_mode="fork_socket",
        phone_ble_adapter="11:22:33:44:55:66")
    mt.active_backend = "fork_socket"
    mt.connected = True
    mt.phone_ble_enabled_applied = True
    mt.phone_ble_adapter_applied = "AA:BB:CC:DD:EE:FF"

    assert not mt.host_ble_requires_lease("11:22:33:44:55:66")
    assert mt.host_ble_requires_lease("AA:BB:CC:DD:EE:FF")
    assert mt.host_ble_requires_lease("auto")

    # Changing only the desired value cannot authorize a bypass before the
    # daemon acknowledges and reports the controller it actually applied.
    mt.configure_phone_ble(True, "22:33:44:55:66:77")
    assert not mt.host_ble_requires_lease("11:22:33:44:55:66")


def test_confirmed_disabled_phone_ble_needs_no_scan_lease():
    mt = MeshtasticManager(backend_mode="fork_socket")
    mt.active_backend = "fork_socket"
    mt.connected = True
    mt.phone_ble_enabled_applied = False
    assert not mt.host_ble_requires_lease("auto")


def test_reconnect_drops_applied_phone_policy_when_new_daemon_omits_it(
        tmp_path):
    class ChangingPolicyDaemon(FakeWdgDaemon):
        def handle(self, client, payload):
            request = json.loads(payload)
            if request["name"] != "get_status":
                return super().handle(client, payload)
            self.commands.append(request)
            body = {
                "state": "ready",
                "identity": {"node_id": "!00000001", "name": "WDG"},
                "channels": [],
            }
            if self.connections == 1:
                body.update(
                    phone_ble_enabled=True,
                    phone_ble_adapter_address="AA:BB:CC:DD:EE:FF")
            self._reply(client, request, body)

    daemon = ChangingPolicyDaemon(tmp_path / "wdg.sock")
    mt = MeshtasticManager(
        backend_mode="fork_socket", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect)
    try:
        mt.start()
        wait_for(lambda: mt.connected and daemon.connections == 1)
        assert mt.phone_ble_enabled_applied is True
        assert mt.phone_ble_adapter_applied == "AA:BB:CC:DD:EE:FF"

        daemon.disconnect()
        wait_for(lambda: daemon.connections >= 2 and mt.connected)

        assert mt.phone_ble_enabled_applied is None
        assert mt.phone_ble_adapter_applied == ""
        assert mt.host_ble_requires_lease("11:22:33:44:55:66")
    finally:
        mt.close()
        daemon.close()


def test_phone_ble_policy_and_shared_retry_fail_closed_during_scan_lease():
    mt = MeshtasticManager(
        backend_mode="fork_socket", phone_ble_enabled=True,
        phone_ble_adapter="AA:BB:CC:DD:EE:FF")
    mt._ble_scan_lease_active = True
    mt._ble_scan_lease_owner = "host-ble-7"
    mt.host_ble_degraded = True
    mt.host_ble_pause_reason = "phone priority"

    assert not mt.set_phone_ble(False, "11:22:33:44:55:66")
    assert mt._phone_ble_enabled is True
    assert mt._phone_ble_adapter == "AA:BB:CC:DD:EE:FF"
    assert not mt.apply_phone_ble()
    assert not mt.retry_shared_adapter()
    assert mt.host_ble_degraded
    assert mt.host_ble_pause_reason == "phone priority"
    assert mt.ble_scan_lease_active
    assert "active host BLE scan" in mt.last_error


def test_legacy_backend_ignores_stale_shared_adapter_degradation():
    mt = MeshtasticManager(
        backend_mode="legacy_tcp",
        service_runner=Mock(side_effect=no_fork_runner))
    mt.host_ble_degraded = True
    mt.host_ble_pause_reason = "old conflict"

    assert mt.acquire_ble_scan_lease() == (
        True, "meshtasticd-wdg is not active")


def test_renewal_preserves_owner_when_daemon_disappears():
    mt = MeshtasticManager(
        backend_mode="legacy_tcp",
        service_runner=Mock(side_effect=no_fork_runner))
    mt._ble_scan_lease_active = True
    mt._ble_scan_lease_owner = "host-ble-3"

    assert mt.acquire_ble_scan_lease(owner="host-ble-3") == (
        True, "meshtasticd-wdg is not active")
    assert mt.ble_scan_lease_active
    assert mt._ble_scan_lease_owner == "host-ble-3"
    assert mt.release_ble_scan_lease(owner="host-ble-3")
    assert not mt.ble_scan_lease_active


def test_suspend_then_resume_clears_client_stop_before_readiness_poll():
    state = {"starting": True, "enabled": True, "polls": 0}
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
        elif args[1] == "enable":
            state["enabled"] = True
        elif args[1] == "disable":
            state["enabled"] = False
        if args[1] == "show":
            if "meshtasticd.service" in args:
                    return NS(
                        returncode=0,
                        stdout=("LoadState=not-found\nActiveState=inactive\n"
                                "UnitFileState=not-found\n"), stderr="")
            active = "active" if state["starting"] else "inactive"
            return NS(
                returncode=0,
                stdout=(f"LoadState=loaded\nActiveState={active}\n"
                        f"UnitFileState={'enabled' if state['enabled'] else 'disabled'}\n"),
                stderr="")
        return NS(returncode=0, stdout="", stderr="")

    mt = MeshtasticManager(
        backend_mode="fork_socket", socket_probe=probe,
        service_runner=runner, sleep=lambda _seconds: None)

    assert mt.suspend_service()
    assert mt.resume_service(connect=False)
    assert state["polls"] >= 2
    assert state["enabled"]
    # The handoff snapshots both units before stopping them so it can restore
    # the actual owner even when the saved backend preference is stale.
    assert [call[1] for call in calls].count("show") >= 2
    assert "start" in [call[1] for call in calls]
    assert not mt._stop_event.is_set()


def test_service_handoff_uses_injected_allowlisted_controller():
    class Controller:
        def __init__(self):
            self.active = {"wdg": True, "stock": False}
            self.enabled = {"wdg": True, "stock": False}
            self.calls = []

        def status(self, target):
            self.calls.append(("status", target))
            active = self.active[target]
            return NS(
                active=active, load_state="loaded",
                active_state="active" if active else "inactive",
                unit_file_state=("enabled" if self.enabled[target]
                                 else "disabled"))

        def stop(self, target):
            self.calls.append(("stop", target))
            self.active[target] = False
            return self.status(target)

        def start(self, target):
            self.calls.append(("start", target))
            self.active[target] = True
            return self.status(target)

        def enable(self, target):
            self.calls.append(("enable", target))
            self.enabled[target] = True
            return self.status(target)

        def disable(self, target):
            self.calls.append(("disable", target))
            self.enabled[target] = False
            return self.status(target)

    controller = Controller()
    mt = MeshtasticManager(
        backend_mode="fork_socket", service_controller=controller,
        socket_probe=lambda _path: controller.active["wdg"],
        sleep=lambda _seconds: None)

    assert mt.suspend_service()
    assert mt.resume_service(connect=False)
    assert ("stop", "wdg") in controller.calls
    assert ("start", "wdg") in controller.calls
    assert ("disable", "wdg") in controller.calls
    assert ("enable", "wdg") in controller.calls
    assert ("status", "stock") in controller.calls
    assert ("stop", "stock") not in controller.calls


def test_handoff_restores_actual_active_unit_not_saved_preference():
    class Controller:
        def __init__(self):
            self.active = {"wdg": False, "stock": True}
            self.calls = []

        def status(self, target):
            self.calls.append(("status", target))
            active = self.active[target]
            return NS(
                active=active, load_state="loaded",
                active_state="active" if active else "inactive",
                unit_file_state="disabled")

        def stop(self, target):
            self.calls.append(("stop", target))
            self.active[target] = False
            return self.status(target)

        def start(self, target):
            self.calls.append(("start", target))
            self.active[target] = True
            return self.status(target)

    controller = Controller()
    mt = MeshtasticManager(
        backend_mode="fork_socket", service_controller=controller,
        socket_probe=lambda _path: controller.active["wdg"],
        port_probe=lambda _host, _port: controller.active["stock"],
        sleep=lambda _seconds: None)

    assert mt.suspend_service()
    assert mt._suspended_service_target == "stock"
    assert mt.resume_service(connect=False)
    assert ("start", "stock") in controller.calls
    assert ("start", "wdg") not in controller.calls
    assert mt._suspended_service_target is None


def test_service_handoff_stops_and_waits_through_transitional_states():
    class Controller:
        def __init__(self):
            self.states = {
                "wdg": ["deactivating", "deactivating", "inactive"],
                "stock": ["inactive"],
            }
            self.calls = []

        def status(self, target):
            self.calls.append(("status", target))
            states = self.states[target]
            state = states.pop(0) if len(states) > 1 else states[0]
            return NS(
                active=state == "active", load_state="loaded",
                active_state=state, unit_file_state="disabled")

        def stop(self, target):
            self.calls.append(("stop", target))
            return NS(
                active=False, load_state="loaded",
                active_state="deactivating", unit_file_state="disabled")

    controller = Controller()
    mt = MeshtasticManager(
        backend_mode="fork_socket", service_controller=controller,
        socket_probe=lambda _path: False, sleep=lambda _seconds: None)

    assert mt.suspend_service()
    # Snapshot capture waits for the externally initiated deactivation to
    # settle before any WDG mutation. Once inactive, no redundant stop occurs.
    assert ("stop", "wdg") not in controller.calls
    assert controller.calls.count(("status", "wdg")) >= 2


def test_direct_radio_handoff_allows_missing_or_inactive_service():
    for load_state, active_state in (
            ("not-found", "inactive"), ("loaded", "inactive")):
        runner = Mock(return_value=NS(
            returncode=0,
            stdout=(f"LoadState={load_state}\nActiveState={active_state}\n"
                    f"UnitFileState={'not-found' if load_state == 'not-found' else 'disabled'}\n"),
            stderr=""))
        mt = MeshtasticManager(
            backend_mode="legacy_tcp", service_runner=runner,
            port_probe=lambda _host, _port: False)

        assert mt.suspend_service()
        assert not any(
            call.args[0][1] == "stop" for call in runner.call_args_list)


@pytest.mark.parametrize(
    "unit_state",
    ["enabled-runtime", "masked", "masked-runtime", "linked",
     "linked-runtime"],
)
def test_service_transitions_fail_closed_for_non_restorable_unit_states(
        unit_state):
    class Controller:
        def __init__(self):
            self.mutations = []

        def require_current(self):
            return None

        def status(self, target):
            return NS(
                active=False, load_state="loaded", active_state="inactive",
                unit_file_state=(unit_state if target == "wdg" else
                                 "disabled"))

        def select(self, target):
            self.mutations.append(("select", target))
            raise AssertionError("unsupported state must stop before selection")

        def stop(self, target):
            self.mutations.append(("stop", target))
            raise AssertionError("unsupported state must stop before mutation")

        def enable(self, target):
            self.mutations.append(("enable", target))
            raise AssertionError("unsupported state must stop before mutation")

        def disable(self, target):
            self.mutations.append(("disable", target))
            raise AssertionError("unsupported state must stop before mutation")

    controller = Controller()
    mt = MeshtasticManager(
        service_controller=controller, socket_probe=lambda _path: False)
    mt.close = Mock(return_value=True)

    assert mt.activate_backend_service("fork_socket") is False
    assert mt.suspend_service() is False
    assert controller.mutations == []
    mt.close.assert_not_called()
    assert mt._suspended_service_target is None
    assert mt._suspended_service_snapshot is None


def test_enabled_runtime_is_never_classified_as_disabled():
    state = _ServiceSnapshot(
        "wdg", "loaded", "inactive", "enabled-runtime")

    assert state.enabled is True
    assert state.exactly_restorable is False


@pytest.mark.parametrize(
    "load_state,active_state,unit_state",
    [
        ("loaded", "failed", "enabled"),
        ("unknown", "unknown", "unknown"),
        ("loaded", "maintenance", "enabled"),
        ("loaded", "active", "unknown"),
        ("loaded", "inactive", "not-found"),
        ("not-found", "active", "not-found"),
        ("not-found", "inactive", "disabled"),
    ],
)
def test_service_handoffs_reject_unstable_or_inconsistent_snapshots(
        load_state, active_state, unit_state):
    class Controller:
        def __init__(self):
            self.mutations = []

        def require_current(self):
            return None

        def status(self, target):
            if target == "wdg":
                return NS(
                    load_state=load_state, active_state=active_state,
                    unit_file_state=unit_state,
                    active=active_state == "active")
            return NS(
                load_state="not-found", active_state="inactive",
                unit_file_state="not-found", active=False)

        def __getattr__(self, name):
            if name in {"select", "start", "stop", "enable", "disable"}:
                def mutate(*args):
                    self.mutations.append((name, *args))
                    raise AssertionError("invalid snapshot must not mutate")
                return mutate
            raise AttributeError(name)

    for operation in ("switch", "suspend"):
        controller = Controller()
        mt = MeshtasticManager(
            service_controller=controller, socket_probe=lambda _path: False,
            sleep=lambda _seconds: None)
        mt.close = Mock(
            side_effect=AssertionError("invalid snapshot must not close"))

        result = (mt.activate_backend_service("fork_socket")
                  if operation == "switch" else mt.suspend_service())

        assert result is False
        assert controller.mutations == []
        mt.close.assert_not_called()
        assert "unstable wdg state" in mt.last_error


def test_service_handoffs_reject_dual_active_snapshot_before_mutation():
    class Controller:
        def __init__(self):
            self.mutations = []

        def require_current(self):
            return None

        def status(self, target):
            return NS(
                load_state="loaded", active_state="active",
                unit_file_state="enabled", active=True)

        def select(self, target):
            self.mutations.append(("select", target))
            raise AssertionError("dual-active state must not mutate")

        def stop(self, target):
            self.mutations.append(("stop", target))
            raise AssertionError("dual-active state must not mutate")

    controller = Controller()
    mt = MeshtasticManager(
        service_controller=controller, socket_probe=lambda _path: False)
    mt.close = Mock(side_effect=AssertionError(
        "dual-active state must not close the client"))

    assert not mt.activate_backend_service("fork_socket")
    assert not mt.suspend_service()
    assert controller.mutations == []
    mt.close.assert_not_called()
    assert "both meshtasticd-wdg and stock meshtasticd are active" in (
        mt.last_error)


def test_never_settling_service_transition_is_bounded_and_fails_closed():
    class Controller:
        calls = 0

        def status(self, target):
            self.calls += 1
            return NS(
                load_state="loaded", active_state="activating",
                unit_file_state="enabled", active=False)

        def stop(self, _target):
            raise AssertionError("transitional state must not be mutated")

    controller = Controller()
    mt = MeshtasticManager(
        service_controller=controller, socket_probe=lambda _path: False,
        sleep=lambda _seconds: None)
    mt.close = Mock(side_effect=AssertionError(
        "transitional state must not close the client"))

    assert not mt.suspend_service()
    assert controller.calls == 40
    mt.close.assert_not_called()
    assert "loaded/activating/enabled" in mt.last_error


def test_new_suspend_cannot_overwrite_unresolved_restore_snapshot():
    snapshot = {
        "wdg": _ServiceSnapshot("wdg", "loaded", "active", "enabled"),
        "stock": _ServiceSnapshot(
            "stock", "loaded", "inactive", "disabled"),
    }
    mt = MeshtasticManager(socket_probe=lambda _path: False)
    mt._suspended_service_target = "stock"
    retained = dict(snapshot)
    mt._suspended_service_snapshot = retained
    mt._capture_service_snapshot = Mock(
        side_effect=AssertionError("must not replace the rollback token"))
    mt.close = Mock(return_value=True)
    mt._stop_service = Mock(
        side_effect=AssertionError("must not mutate services again"))

    assert mt.suspend_service() is False
    assert mt._suspended_service_target == "stock"
    assert mt._suspended_service_snapshot is retained
    mt._capture_service_snapshot.assert_not_called()
    mt.close.assert_not_called()
    mt._stop_service.assert_not_called()
    assert "retry resume_service" in mt.last_error


@pytest.mark.parametrize("failure", ["stop", "disable", "verify"])
def test_failed_suspend_restore_retains_exact_snapshot_until_retry(failure):
    snapshot = {
        "wdg": _ServiceSnapshot("wdg", "loaded", "active", "enabled"),
        "stock": _ServiceSnapshot(
            "stock", "loaded", "inactive", "disabled"),
    }
    mt = MeshtasticManager(socket_probe=lambda _path: False)
    mt._capture_service_snapshot = Mock(return_value=snapshot)
    mt.close = Mock(return_value=True)
    mt._stop_service = Mock(return_value=failure != "stop")
    mt._set_service_enabled = Mock(return_value=failure != "disable")
    mt._services_persistently_suspended = Mock(
        return_value=failure != "verify")
    mt._restore_service_snapshot = Mock(return_value=False)

    assert mt.suspend_service(timeout=0.0) is False
    assert mt._suspended_service_target == "wdg"
    assert mt._suspended_service_snapshot is snapshot
    mt._restore_service_snapshot.assert_called_once_with(
        snapshot, timeout=0.0)

    mt._restore_service_snapshot.return_value = True
    assert mt.resume_service(timeout=1.0, connect=False)
    assert mt._suspended_service_target is None
    assert mt._suspended_service_snapshot is None


def test_unresolved_suspend_restore_blocks_client_and_service_selection():
    snapshot = {
        "wdg": _ServiceSnapshot("wdg", "loaded", "inactive", "enabled"),
        "stock": _ServiceSnapshot(
            "stock", "loaded", "inactive", "disabled"),
    }
    mt = MeshtasticManager()
    mt._suspended_service_target = "wdg"
    mt._suspended_service_snapshot = snapshot
    mt._backend_service_rollback = dict(snapshot)
    mt._capture_service_snapshot = Mock(
        side_effect=AssertionError("service state must remain untouched"))
    mt._restore_service_snapshot = Mock(
        side_effect=AssertionError("other rollback must stay blocked"))

    assert not mt.start()
    assert not mt.activate_backend_service("fork_socket")
    assert not mt.rollback_backend_service_activation()
    assert not mt.running
    assert mt._suspended_service_snapshot is snapshot
    mt._capture_service_snapshot.assert_not_called()
    mt._restore_service_snapshot.assert_not_called()


def test_failed_resume_keeps_exact_snapshot_for_another_retry():
    snapshot = {
        "wdg": _ServiceSnapshot("wdg", "loaded", "active", "enabled"),
        "stock": _ServiceSnapshot(
            "stock", "loaded", "inactive", "disabled"),
    }
    mt = MeshtasticManager()
    mt._suspended_service_target = "wdg"
    mt._suspended_service_snapshot = snapshot
    mt._restore_service_snapshot = Mock(return_value=False)

    assert not mt.resume_service(connect=False)
    assert mt._suspended_service_target == "wdg"
    assert mt._suspended_service_snapshot is snapshot


def test_restore_exception_keeps_exact_snapshot_for_another_retry():
    snapshot = {
        "wdg": _ServiceSnapshot("wdg", "loaded", "active", "enabled"),
        "stock": _ServiceSnapshot(
            "stock", "loaded", "inactive", "disabled"),
    }
    mt = MeshtasticManager()
    mt._suspended_service_target = "wdg"
    mt._suspended_service_snapshot = snapshot
    mt._restore_service_snapshot = Mock(
        side_effect=OSError("helper disappeared"))

    assert not mt.resume_service(connect=False)
    assert mt._suspended_service_target == "wdg"
    assert mt._suspended_service_snapshot is snapshot
    assert "helper disappeared" in mt.last_error


def test_host_ble_needs_no_lease_without_wdg_daemon():
    mt = MeshtasticManager(
        backend_mode="auto", socket_probe=lambda _path: False,
        phone_ble_enabled=True,
        service_runner=Mock(side_effect=no_fork_runner))
    assert mt.acquire_ble_scan_lease() == (
        True, "meshtasticd-wdg is not active")
    assert mt.release_ble_scan_lease()


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
            return NS(
                returncode=0,
                stdout="LoadState=not-found\nActiveState=inactive\n",
                stderr="")
        return NS(returncode=0, stdout="", stderr="")

    mt = MeshtasticManager(
        backend_mode="auto", socket_probe=lambda _path: False,
        service_runner=runner, port_probe=lambda _host, _port: True,
        dependency_loader=lambda: (FakeInterface, pub, PortNums, "^all"))
    try:
        mt.start()
        wait_for(lambda: mt.connected)
        assert mt.active_backend == "legacy_tcp"
        assert len(calls) == 2
        assert all(call == [
            "systemctl", "show", "meshtasticd-wdg.service",
            "--property=LoadState", "--property=ActiveState"]
                   for call in calls)
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


def test_offline_phone_ble_preference_cannot_bypass_live_fork():
    pub = FakePub()
    mt = MeshtasticManager(
        backend_mode="legacy_tcp", phone_ble_enabled=False,
        socket_probe=lambda _path: True,
        port_probe=lambda _host, _port: True,
        dependency_loader=lambda: (FakeInterface, pub, PortNums, "^all"))
    mt.start()
    wait_for(lambda: not mt.running)
    assert not mt.connected
    assert "blocked" in mt.last_error


@pytest.mark.parametrize("mode", ["", "fork", "tcp", "AUTO"])
def test_rejects_unknown_backend_modes(mode):
    with pytest.raises(ValueError, match="Unsupported Meshtastic backend"):
        MeshtasticManager(backend_mode=mode)


def test_backend_activation_restores_exact_previous_service_on_timeout():
    class Controller:
        def __init__(self):
            self.active = {"wdg": False, "stock": True}
            self.enabled = {"wdg": False, "stock": True}
            self.calls = []

        def require_current(self):
            self.calls.append(("require_current",))

        def status(self, target):
            active = self.active[target]
            return NS(
                active=active, load_state="loaded",
                active_state="active" if active else "inactive",
                unit_file_state=("enabled" if self.enabled[target]
                                 else "disabled"))

        def select(self, target):
            self.calls.append(("select", target))
            other = "stock" if target == "wdg" else "wdg"
            self.active[other] = self.enabled[other] = False
            self.active[target] = self.enabled[target] = True
            return self.status(target)

        def start(self, target):
            self.calls.append(("start", target))
            self.active[target] = True
            return self.status(target)

        def stop(self, target):
            self.calls.append(("stop", target))
            self.active[target] = False
            return self.status(target)

        def enable(self, target):
            self.calls.append(("enable", target))
            self.enabled[target] = True
            return self.status(target)

        def disable(self, target):
            self.calls.append(("disable", target))
            self.enabled[target] = False
            return self.status(target)

    controller = Controller()
    mt = MeshtasticManager(
        backend_mode="auto", service_controller=controller,
        socket_probe=lambda _path: False,
        port_probe=lambda _host, _port: controller.active["stock"],
        sleep=lambda _seconds: None)

    assert not mt.activate_backend_service("fork_socket", timeout=0)
    assert controller.active == {"wdg": False, "stock": True}
    assert controller.enabled == {"wdg": False, "stock": True}
    assert ("select", "wdg") in controller.calls
    assert ("start", "stock") in controller.calls
    assert "previous stock service restored" in mt.last_error


def test_suspend_resume_restores_enabled_but_inactive_service_exactly():
    class Controller:
        def __init__(self):
            self.active = {"wdg": False, "stock": False}
            self.enabled = {"wdg": True, "stock": False}
            self.calls = []

        def status(self, target):
            active = self.active[target]
            return NS(
                active=active, load_state="loaded",
                active_state="active" if active else "inactive",
                unit_file_state=("enabled" if self.enabled[target]
                                 else "disabled"))

        def stop(self, target):
            self.calls.append(("stop", target))
            self.active[target] = False
            return self.status(target)

        def start(self, target):
            self.calls.append(("start", target))
            self.active[target] = True
            return self.status(target)

        def enable(self, target):
            self.calls.append(("enable", target))
            self.enabled[target] = True
            return self.status(target)

        def disable(self, target):
            self.calls.append(("disable", target))
            self.enabled[target] = False
            return self.status(target)

    controller = Controller()
    mt = MeshtasticManager(
        service_controller=controller, socket_probe=lambda _path: False,
        port_probe=lambda _host, _port: False, sleep=lambda _seconds: None)

    assert mt.active_service_target() == "wdg"
    assert mt.suspend_service()
    assert controller.enabled == {"wdg": False, "stock": False}
    assert mt.resume_service(connect=False)
    assert controller.enabled == {"wdg": True, "stock": False}
    assert controller.active == {"wdg": False, "stock": False}
    assert ("start", "wdg") not in controller.calls


def test_wait_connected_requires_completed_socket_negotiation(tmp_path):
    daemon = FakeWdgDaemon(tmp_path / "wdg.sock")
    mt = MeshtasticManager(
        backend_mode="fork_socket", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect)
    try:
        assert mt.start()
        assert mt.wait_connected(timeout=1.0, backend="fork_socket")
        assert any(command["name"] == "get_status"
                   for command in daemon.commands)
    finally:
        mt.close()
        daemon.close()


def test_socket_disconnect_retires_local_leases_before_reacquire(tmp_path):
    daemon = FakeWdgDaemon(tmp_path / "wdg.sock")
    mt = MeshtasticManager(
        backend_mode="fork_socket", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect)
    try:
        mt.start()
        wait_for(lambda: mt.connected)
        assert mt.acquire_ble_scan_lease(owner="old-owner")[0]
        assert mt.acquire_pairing_agent_lease()

        daemon.disconnect()
        wait_for(lambda: daemon.connections >= 2 and mt.connected, timeout=3)

        assert not mt.ble_scan_lease_active
        assert mt._ble_scan_lease_owner == ""
        assert not mt.pairing_agent_lease_active
        assert mt.acquire_ble_scan_lease(owner="new-owner")[0]
        assert mt._ble_scan_lease_owner == "new-owner"
    finally:
        mt.close()
        daemon.close()


def test_expired_destructive_control_is_never_sent_after_queue_delay():
    mt = MeshtasticManager(monotonic=time.monotonic)
    mt.active_backend = "fork_socket"
    mt.connected = True

    ok, reason, _body = mt._control_command(
        "forget_phone", timeout=0.01)
    assert not ok
    assert "request was cancelled" in reason
    command, data = mt._commands.get_nowait()
    assert command == "control"
    name, body, waiter = data
    mt._socket_command = Mock()

    assert not mt._dispatch_control_command(Mock(), name, body, waiter)
    mt._socket_command.assert_not_called()


def test_control_timeout_reply_races_are_resolved_once():
    waiter = _ControlWaiter(deadline=100.0)
    assert waiter.begin_send(1.0)
    assert waiter.resolve(True, "accepted", {"value": 1})
    assert waiter.timeout("forget_phone") is None
    assert waiter.result() == (True, "accepted", {"value": 1})

    late = _ControlWaiter(deadline=100.0)
    assert late.begin_send(1.0)
    timeout = late.timeout("forget_phone")
    assert timeout is not None
    assert "may have completed" in timeout[1]
    assert timeout[2] == {"_indeterminate": True, "_sent": True}
    assert not late.resolve(False, "late rejection")
    assert late.result()[1] == timeout[1]


class LostLeaseReplyDaemon(FakeWdgDaemon):
    """Drop selected lease replies while continuing to process the socket."""

    def __init__(self, path, *, lost):
        super().__init__(path)
        self.lost = set(lost)

    def handle(self, client, payload):
        request = json.loads(payload)
        if request["name"] in self.lost:
            self.commands.append(request)
            return
        super().handle(client, payload)


class DelayedPairingReplyDaemon(FakeWdgDaemon):
    def __init__(self, path):
        super().__init__(path)
        self.late_reply_sent = threading.Event()

    def handle(self, client, payload):
        request = json.loads(payload)
        if request["name"] != "pairing_agent_lease_acquire":
            return super().handle(client, payload)
        self.commands.append(request)

        def reply_late():
            self._reply(client, request)
            self.late_reply_sent.set()

        threading.Timer(0.65, reply_late).start()


def test_lost_scan_acquire_reply_is_compensated_over_live_socket(tmp_path):
    daemon = LostLeaseReplyDaemon(
        tmp_path / "wdg.sock", lost={"ble_scan_lease_acquire"})
    mt = MeshtasticManager(
        backend_mode="fork_socket", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect)
    mt._control_timeout = 0.4
    try:
        assert mt.start()
        wait_for(lambda: mt.connected)

        allowed, reason = mt.acquire_ble_scan_lease(owner="lost-scan")

        assert not allowed
        assert "may have completed" in reason
        wait_for(lambda: any(
            command["name"] == "ble_scan_lease_release"
            for command in daemon.commands))
        assert mt.ble_scan_lease_state == "inactive"
        assert mt.ble_scan_lease_owner == ""
    finally:
        mt.close()
        daemon.close()


def test_lost_scan_acquire_and_release_remain_owned_until_retry(tmp_path):
    daemon = LostLeaseReplyDaemon(
        tmp_path / "wdg.sock",
        lost={"ble_scan_lease_acquire", "ble_scan_lease_release"},
    )
    mt = MeshtasticManager(
        backend_mode="fork_socket", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect)
    mt._control_timeout = 0.4
    try:
        assert mt.start()
        wait_for(lambda: mt.connected)

        allowed, reason = mt.acquire_ble_scan_lease(owner="lost-scan")

        assert not allowed
        assert "lease may still be active" in reason
        assert mt.ble_scan_lease_state == "possibly-active"
        assert mt.ble_scan_lease_owner == "lost-scan"

        daemon.lost.remove("ble_scan_lease_release")
        assert mt.release_ble_scan_lease(owner="lost-scan")
        assert mt.ble_scan_lease_state == "inactive"
        assert mt.ble_scan_lease_owner == ""
    finally:
        mt.close()
        daemon.close()


def test_lost_pairing_acquire_and_release_remain_retryable(tmp_path):
    daemon = LostLeaseReplyDaemon(
        tmp_path / "wdg.sock",
        lost={"pairing_agent_lease_acquire", "pairing_agent_lease_release"},
    )
    mt = MeshtasticManager(
        backend_mode="fork_socket", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect)
    mt._control_timeout = 0.4
    try:
        assert mt.start()
        wait_for(lambda: mt.connected)

        assert not mt.acquire_pairing_agent_lease()
        wait_for(lambda: any(
            command["name"] == "pairing_agent_lease_release"
            for command in daemon.commands))
        assert mt.pairing_agent_lease_state == "possibly-active"

        # A later explicit release retries even though acquisition never
        # returned success. Only an acknowledged release retires ownership.
        daemon.lost.remove("pairing_agent_lease_release")
        assert mt.release_pairing_agent_lease()
        assert mt.pairing_agent_lease_state == "inactive"
        assert sum(
            command["name"] == "pairing_agent_lease_release"
            for command in daemon.commands) >= 2
    finally:
        mt.close()
        daemon.close()


def test_delayed_pairing_acquire_reply_cannot_hide_compensating_release(
        tmp_path):
    daemon = DelayedPairingReplyDaemon(tmp_path / "wdg.sock")
    mt = MeshtasticManager(
        backend_mode="fork_socket", socket_path=daemon.path,
        socket_probe=lambda _path: True, socket_connector=daemon.connect)
    mt._control_timeout = 0.4
    try:
        assert mt.start()
        wait_for(lambda: mt.connected)

        assert not mt.acquire_pairing_agent_lease()
        wait_for(lambda: any(
            command["name"] == "pairing_agent_lease_release"
            for command in daemon.commands))
        assert mt.pairing_agent_lease_state == "inactive"
        assert daemon.late_reply_sent.wait(1.0)
        # The late successful acquire reply belongs to a cancelled waiter and
        # cannot resurrect local ownership after the ordered release.
        time.sleep(0.02)
        assert mt.pairing_agent_lease_state == "inactive"
    finally:
        mt.close()
        daemon.close()
