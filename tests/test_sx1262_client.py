import json
import socket
from unittest.mock import Mock

import pytest

from watchdogs.sx1262_client import (
    API_MAJOR,
    BrokerError,
    BrokerProtocolError,
    BrokerUnavailable,
    MAX_PACKET,
    SX1262Client,
    SX1262Controller,
)


class FakeSocket:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.sent = []
        self.closed = False
        self.path = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, path):
        self.path = path

    def sendall(self, payload):
        self.sent.append(json.loads(payload))

    def recv(self, size):
        if not self.responses:
            raise socket.timeout("no response")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return json.dumps(response, separators=(",", ":")).encode()

    def close(self):
        self.closed = True


def response(request_id, result=None, generation=4):
    return {
        "type": "response",
        "request_id": request_id,
        "ok": True,
        "generation": generation,
        "result": result or {},
    }


def hello_response(request_id=1):
    result = {
        "api": {"major": API_MAJOR, "minor": 0},
        "connection_id": 9,
        "generation": 4,
    }
    return response(request_id, result)


def test_connect_negotiates_role_and_records_generation():
    sock = FakeSocket([hello_response()])
    client = SX1262Client("meshcore", socket_factory=lambda *_: sock)

    session = client.connect()

    assert session.connection_id == 9
    assert session.generation == 4
    assert sock.path == "/run/watchdogs/sx1262d.sock"
    assert sock.sent == [{
        "api": {"major": 1, "minor": 0},
        "pid": sock.sent[0]["pid"],
        "request_id": 1,
        "role": "meshcore",
        "type": "hello",
    }]


def test_stale_generation_response_updates_and_replays_once():
    stale = {
        "type": "response",
        "request_id": 2,
        "ok": False,
        "generation": 5,
        "error": {"code": "stale_generation", "message": "changed"},
    }
    sock = FakeSocket([hello_response(), stale, response(3, generation=5)])
    client = SX1262Client("controller", socket_factory=lambda *_: sock)
    client.connect()

    assert client.request("heartbeat") == {}
    assert client.generation == 5
    assert [message["generation"] for message in sock.sent[1:]] == [4, 5]


def test_requests_carry_generation_and_update_it():
    sock = FakeSocket([hello_response(), response(2, {"state": "MESHTASTIC"}, 7)])
    client = SX1262Client("controller", socket_factory=lambda *_: sock)
    client.connect()

    result = client.request("get_status")

    assert result == {"state": "MESHTASTIC"}
    assert sock.sent[1]["generation"] == 4
    assert client.generation == 7


def test_control_event_is_retained_before_passive_rx_overflow():
    client = SX1262Client("meshcore", event_limit=2)
    client._queue_event({"type": "event", "event": "rx_packet", "payload": "YQ=="})
    client._queue_event({"type": "event", "event": "rx_packet", "payload": "Yg=="})
    client._queue_event({"type": "event", "event": "prepare_revoke"})

    events = [client.next_event(), client.next_event()]
    assert any(event["event"] == "prepare_revoke" for event in events)
    assert client.dropped_events == 1


def test_transmit_enforces_radio_payload_limit():
    client = SX1262Client("meshcore")
    with pytest.raises(ValueError):
        client.transmit(b"")
    with pytest.raises(ValueError):
        client.transmit(b"x" * 256)


def test_rejected_operation_raises_broker_error():
    denied = {
        "type": "response", "request_id": 2, "ok": False,
        "error": {"code": "unauthorized", "message": "controller required"},
    }
    sock = FakeSocket([hello_response(), denied])
    client = SX1262Client("meshcore", socket_factory=lambda *_: sock)
    client.connect()
    with pytest.raises(BrokerError, match="controller required"):
        client.request("activate_mode", mode="meshcore")


def test_invalid_response_id_fails_closed():
    sock = FakeSocket([hello_response(), response(99)])
    client = SX1262Client("meshcore", socket_factory=lambda *_: sock)
    client.connect()
    with pytest.raises(BrokerProtocolError, match="response ID mismatch"):
        client.start_rx()


def test_oversized_envelope_is_rejected_before_send():
    sock = FakeSocket([hello_response()])
    client = SX1262Client("meshcore", socket_factory=lambda *_: sock)
    client.connect()
    with pytest.raises(BrokerProtocolError, match=str(MAX_PACKET)):
        client.request("configure_phy", padding="x" * MAX_PACKET)


def test_connection_failure_is_visible_and_has_no_fallback():
    def broken(*_args):
        result = Mock()
        result.connect.side_effect = OSError("missing")
        return result

    client = SX1262Client("meshcore", socket_factory=broken)
    with pytest.raises(BrokerUnavailable, match="manager unavailable"):
        client.connect()
    assert not client.connected


def test_controller_validates_modes_without_contacting_broker():
    controller = SX1262Controller(socket_factory=lambda *_: Mock())
    with pytest.raises(ValueError):
        controller.activate_mode("invalid")
