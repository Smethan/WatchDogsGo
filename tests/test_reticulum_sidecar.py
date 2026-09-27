import sys
from types import SimpleNamespace

from watchdogs.reticulum_config import (
    ReticulumProfile,
    load_profile,
    save_profile,
)
from watchdogs.reticulum_sidecar import SidecarRuntime


def runtime(tmp_path, **updates):
    profile = ReticulumProfile(confirmed=True).with_updates(**updates)
    path = save_profile(tmp_path, profile)
    return SidecarRuntime(path, tmp_path / "runtime.sock")


def test_private_rns_config_and_two_line_loader(tmp_path):
    sidecar = runtime(
        tmp_path, network_name="field", network_passphrase="secret")
    sidecar._render_rns_config()
    config = (tmp_path / "reticulum" / "rns" / "config").read_text()
    assert "share_instance = No" in config
    assert "enable_transport = No" in config
    assert "type = AioSX1262Interface" in config
    assert "network_name = 'field'" in config
    assert "passphrase = 'secret'" in config
    loader = (tmp_path / "reticulum" / "rns" / "interfaces" /
              "AioSX1262Interface.py").read_text().splitlines()
    assert loader == [
        "from watchdogs.reticulum_interface import AioSX1262Interface",
        "interface_class = AioSX1262Interface",
    ]


def test_ifac_config_quoting_round_trips_comment_and_quote_bytes(tmp_path):
    from RNS.vendor.configobj import ConfigObj

    sidecar = runtime(
        tmp_path, network_name="field # one",
        network_passphrase='quote " and #')
    sidecar._render_rns_config()
    parsed = ConfigObj(str(
        tmp_path / "reticulum" / "rns" / "config"))
    interface = parsed["interfaces"]["AIO SX1262"]
    assert interface["network_name"] == "field # one"
    assert interface["passphrase"] == 'quote " and #'


def test_blank_display_name_derives_from_destination_and_persists(tmp_path):
    sidecar = runtime(tmp_path, display_name="")
    sidecar.identity = object()

    class Destination:
        @staticmethod
        def hash_from_name_and_identity(_name, _identity):
            return bytes.fromhex("0123456789abcdef" * 2)

    sidecar._configure_display_name(SimpleNamespace(Destination=Destination))
    assert sidecar.profile.display_name == "WatchDogs_01234567"
    assert load_profile(tmp_path).display_name == "WatchDogs_01234567"


def test_initialise_selects_configured_outbound_propagation_node(
        tmp_path, monkeypatch):
    node_hash = "90ab9d448f17f3a121dc0f1230af39be"
    sidecar = runtime(
        tmp_path, display_name="WDG", propagation_node_hash=node_hash)

    class Identity:
        @staticmethod
        def from_file(_path):
            return None

        @staticmethod
        def recall_app_data(_hash):
            return None

        def get_private_key(self):
            return b"private"

    class Destination:
        @staticmethod
        def hash_from_name_and_identity(_name, _identity):
            return bytes.fromhex("01" * 16)

    class Transport:
        @staticmethod
        def register_announce_handler(_handler):
            pass

        @staticmethod
        def hops_to(_hash):
            return 1

    class Router:
        def __init__(self, **_kwargs):
            self.node = None

        def set_outbound_propagation_node(self, node):
            self.node = node

        def register_delivery_identity(self, _identity, *, display_name):
            assert display_name == "WDG"
            return SimpleNamespace(hash=bytes.fromhex("02" * 16))

        def register_delivery_callback(self, callback):
            self.delivery_callback = callback

    fake_rns = SimpleNamespace(
        LOG_NOTICE=3,
        Reticulum=lambda **_kwargs: object(),
        Identity=Identity,
        Destination=Destination,
        Transport=Transport,
    )
    fake_lxmf = SimpleNamespace(
        LXMRouter=Router,
        display_name_from_app_data=lambda _data: None,
    )
    monkeypatch.setitem(sys.modules, "RNS", fake_rns)
    monkeypatch.setitem(sys.modules, "LXMF", fake_lxmf)

    sidecar.initialise()
    assert sidecar.router.node == bytes.fromhex(node_hash)


def test_unverified_message_is_rejected(tmp_path):
    sidecar = runtime(tmp_path)
    events = []
    sidecar.event = lambda name, payload: events.append((name, payload))
    sidecar._inbound_message(SimpleNamespace(
        signature_validated=False, content="not trusted"))
    assert events[0][0] == "error"
    assert events[0][1]["code"] == "unverified_message"
    assert not any(name == "message" for name, _payload in events)


def test_verified_inbound_message_is_normalized(tmp_path, monkeypatch):
    sidecar = runtime(tmp_path)
    events = []
    sidecar.event = lambda name, payload: events.append((name, payload))
    fake_rns = SimpleNamespace(Identity=SimpleNamespace(
        recall_app_data=lambda _source: b"name"))
    fake_lxmf = SimpleNamespace(
        display_name_from_app_data=lambda _data: "Alice")
    monkeypatch.setitem(sys.modules, "RNS", fake_rns)
    monkeypatch.setitem(sys.modules, "LXMF", fake_lxmf)
    sidecar._inbound_message(SimpleNamespace(
        signature_validated=True, content="hello\nworld",
        source_hash=bytes.fromhex("ab" * 16), hash=b"message-hash",
        timestamp=123.0, rssi=-90, snr=4))
    name, payload = events[0]
    assert name == "message"
    assert payload["peer_hash"] == "ab" * 16
    assert payload["peer_name"] == "Alice"
    assert payload["text"] == "hello world"
    assert payload["state"] == "delivered"


def test_direct_outbound_announces_once_and_registers_callbacks(tmp_path):
    sidecar = runtime(tmp_path)
    announces = []
    handled = []

    class Identity:
        @staticmethod
        def recall(_destination):
            return object()

    class Destination:
        OUT = 1
        SINGLE = 2

        def __init__(self, *_args):
            pass

    class Message:
        DIRECT = 7
        PROPAGATED = 8
        OUTBOUND = 1
        GENERATING = 0
        SENDING = 2
        SENT = 3
        DELIVERED = 4
        REJECTED = 5
        CANCELLED = 6
        FAILED = 7

        def __init__(self, destination, source, text, *, desired_method,
                     include_ticket):
            self.destination = destination
            self.source = source
            self.text = text
            self.desired_method = desired_method
            self.include_ticket = include_ticket
            self.state = self.OUTBOUND
            self.hash = b"hash"
            self.delivered_callback = None
            self.failed_callback = None

        def register_delivery_callback(self, callback):
            self.delivered_callback = callback

        def register_failed_callback(self, callback):
            self.failed_callback = callback

    sidecar.delivery_destination = SimpleNamespace(hash=b"local")
    sidecar.router = SimpleNamespace(
        announce=lambda value: announces.append(value),
        handle_outbound=lambda message: handled.append(message))
    # Keep the status-monitor thread from outliving this unit test.
    sidecar.stop_event.set()
    RNS = SimpleNamespace(Identity=Identity, Destination=Destination)
    LXMF = SimpleNamespace(LXMessage=Message)
    payload = {
        "destination_hash": "ab" * 16,
        "text": "hello",
        "correlation_id": "correlation",
    }
    assert sidecar._send_text(RNS, LXMF, payload)["correlation_id"] \
        == "correlation"
    assert handled[0].desired_method == Message.DIRECT
    assert handled[0].include_ticket is True
    assert handled[0].delivered_callback is not None
    assert handled[0].failed_callback is not None
    assert announces == [b"local"]

    sidecar._send_text(RNS, LXMF, {**payload, "correlation_id": "second"})
    assert announces == [b"local"]


def test_propagated_outbound_uses_configured_node_and_reports_stored(tmp_path):
    sidecar = runtime(
        tmp_path,
        propagation_node_hash="90ab9d448f17f3a121dc0f1230af39be",
        propagated_outbound=True,
    )
    events = []
    handled = []
    sidecar.event = lambda name, payload: events.append((name, payload))

    class Identity:
        @staticmethod
        def recall(_destination):
            return object()

    class Destination:
        OUT = 1
        SINGLE = 2

        def __init__(self, *_args):
            pass

    class Message:
        DIRECT = 7
        PROPAGATED = 8
        GENERATING = 0
        OUTBOUND = 1
        SENDING = 2
        SENT = 3
        DELIVERED = 4
        REJECTED = 5
        CANCELLED = 6
        FAILED = 7

        def __init__(self, _destination, _source, _text, *, desired_method,
                     include_ticket):
            self.desired_method = desired_method
            self.include_ticket = include_ticket
            self.state = self.OUTBOUND
            self.hash = b"hash"

        def register_delivery_callback(self, callback):
            self.delivered_callback = callback

        def register_failed_callback(self, callback):
            self.failed_callback = callback

    sidecar.delivery_destination = SimpleNamespace(hash=b"local")
    sidecar.router = SimpleNamespace(
        announce=lambda _value: None,
        handle_outbound=lambda message: handled.append(message),
    )
    sidecar.stop_event.set()
    LXMF = SimpleNamespace(LXMessage=Message)
    sidecar._send_text(
        SimpleNamespace(Identity=Identity, Destination=Destination), LXMF, {
            "destination_hash": "ab" * 16,
            "text": "store me",
            "correlation_id": "propagated",
        })
    message = handled[0]
    assert message.desired_method == Message.PROPAGATED
    message.state = Message.SENT
    sidecar._message_status(LXMF, message, "propagated")
    status = [payload for name, payload in events
              if name == "outbound_status"][-1]
    assert status["state"] == "stored"
    assert status["delivery_method"] == "propagated"


def test_propagation_sync_requests_messages_and_can_cancel(tmp_path):
    sidecar = runtime(
        tmp_path,
        propagation_node_hash="90ab9d448f17f3a121dc0f1230af39be",
    )
    events = []

    class Router:
        PR_IDLE = 0
        PR_PATH_REQUESTED = 1
        PR_LINK_ESTABLISHING = 2
        PR_LINK_ESTABLISHED = 3
        PR_REQUEST_SENT = 4
        PR_RECEIVING = 5
        PR_RESPONSE_RECEIVED = 6
        PR_COMPLETE = 7
        PR_NO_PATH = 0xF0
        PR_LINK_FAILED = 0xF1
        PR_TRANSFER_FAILED = 0xF2
        PR_NO_IDENTITY_RCVD = 0xF3
        PR_NO_ACCESS = 0xF4
        PR_FAILED = 0xFE

        def __init__(self):
            self.propagation_transfer_state = self.PR_IDLE
            self.propagation_transfer_progress = 0.0
            self.propagation_transfer_last_result = None
            self.propagation_transfer_last_duplicates = None
            self.requests = []

        def request_messages_from_propagation_node(
                self, identity, max_messages):
            self.requests.append((identity, max_messages))
            self.propagation_transfer_state = self.PR_COMPLETE
            self.propagation_transfer_progress = 1.0
            self.propagation_transfer_last_result = 3

        def cancel_propagation_node_requests(self):
            self.propagation_transfer_state = self.PR_IDLE
            self.propagation_transfer_progress = 0.0

    sidecar.router = Router()
    sidecar.identity = object()
    sidecar.event = lambda name, payload: events.append((name, payload))

    result = sidecar._sync_propagation(25)
    sidecar.propagation_thread.join(timeout=1)
    assert result["accepted"] is True
    assert sidecar.router.requests == [(sidecar.identity, 25)]
    assert any(name == "propagation_status"
               and payload["state"] == "complete"
               and payload["message_count"] == 3
               for name, payload in events)

    cancelled = sidecar._cancel_propagation()
    assert cancelled["state"] == "idle"
