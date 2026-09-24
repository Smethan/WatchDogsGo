"""meshtasticd remains the sole radio owner while WDG is a TCP client."""

import time
from types import SimpleNamespace as NS
from unittest.mock import Mock

from watchdogs.meshtastic_manager import MeshtasticManager


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


def manager(pub=None, probe=lambda _host, _port: True, runner=Mock()):
    pub = pub or FakePub()
    return MeshtasticManager(
        dependency_loader=lambda: (FakeInterface, pub, PortNums, "^all"),
        port_probe=probe, service_runner=runner, sleep=lambda _seconds: None), pub


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
        ["systemctl", "start", "meshtasticd"],
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
        ["systemctl", "stop", "meshtasticd"],
        capture_output=True, text=True, timeout=12)


def test_close_before_first_start_does_not_poison_next_connection():
    runner = Mock(return_value=NS(returncode=0, stdout="", stderr=""))
    mt, _pub = manager(runner=runner)

    mt.close(stop_daemon=True)
    mt.start()
    wait_for(lambda: mt.connected)

    assert mt.running
    mt.close()
