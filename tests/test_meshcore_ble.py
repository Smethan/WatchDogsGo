"""MeshCore companion protocol and BLE lifecycle tests."""

import logging
import stat
import struct
import threading
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from watchdogs.app import WatchDogsGame
from watchdogs.lora_manager import (
    PUBLIC_CHANNEL,
    load_meshcore_config,
    make_hashtag_channel,
    save_meshcore_config,
)
from watchdogs.meshcore_ble import (
    BLUEZ_REGISTRATION_TIMEOUT,
    MAX_SIGN_DATA_LEN,
    MESHCORE_RX_FLAGS,
    MESHCORE_RX_UUID,
    MESHCORE_SERVICE_UUID,
    MESHCORE_TX_FLAGS,
    MESHCORE_TX_UUID,
    MeshCoreBleManager,
    MeshCoreCompanionProtocol,
    _BluezRegistration,
    _PairingSecurity,
)


class FakeLora:
    def __init__(self):
        self.private = Ed25519PrivateKey.generate()
        self.public = self.private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        self.send_meshcore_channel_message = Mock(return_value=b"dedup")
        self.send_meshcore_control = Mock(return_value=b"control")
        self.send_meshcore_advert = Mock(return_value=b"advert")
        self._build_mc_advert = Mock(return_value=b"signed-advert")
        self.noise_floor = -112
        self.last_rssi = -78
        self.last_snr = 7.25
        self.packets_received = 42

    def _get_ed25519_keypair(self):
        return self.private, self.public


def _protocol():
    lora = FakeLora()
    state = {
        "name": "WatchDogs_deadbeef",
        "channels": [PUBLIC_CHANNEL],
        "region": "us_ca_narrow",
    }

    def set_channels(value):
        state["channels"] = list(value)

    protocol = MeshCoreCompanionProtocol(
        lora,
        get_node_name=lambda: state["name"],
        set_node_name=lambda value: state.__setitem__("name", value),
        get_channels=lambda: state["channels"],
        set_channels=set_channels,
        get_region=lambda: state["region"],
        get_location=lambda: (32.7767, -96.7970),
        clock=lambda: 1_700_000_000,
    )
    return protocol, lora, state


def test_standard_meshcore_nordic_uart_uuids_are_exposed():
    assert MESHCORE_SERVICE_UUID == "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
    assert MESHCORE_RX_UUID == "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
    assert MESHCORE_TX_UUID == "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"


def test_device_query_and_app_start_report_identity_and_radio_config():
    protocol, lora, _state = _protocol()

    device = protocol.handle_frame(bytes((22, 4)))[0]
    assert device[:4] == bytes((13, 12, 32, 8))
    assert device[20:60].split(b"\0", 1)[0] == b"WatchDogsGo AIO SX1262"

    self_info = protocol.handle_frame(
        b"\x01\x01\0\0\0\0\0MeshMapper")[0]
    assert self_info[:4] == bytes((5, 1, 22, 22))
    assert self_info[4:36] == lora.public
    assert struct.unpack_from("<ii", self_info, 36) == (
        32_776_700, -96_797_000)
    frequency_khz, bandwidth_hz = struct.unpack_from("<II", self_info, 48)
    assert frequency_khz == 910_525
    assert bandwidth_hz == 62_500
    assert self_info[56:58] == bytes((7, 5))
    assert self_info[58:] == b"WatchDogs_deadbeef"


def test_channel_scan_has_fixed_slots_and_meshmapper_can_create_wardriving():
    protocol, _lora, state = _protocol()
    public = protocol.handle_frame(bytes((31, 0)))[0]
    assert public[:2] == bytes((18, 0))
    assert public[2:34].split(b"\0", 1)[0] == b"Public"

    wardriving = make_hashtag_channel("#wardriving")
    command = (bytes((32, 5)) + b"#wardriving".ljust(32, b"\0")
               + wardriving.psk)
    assert protocol.handle_frame(command) == [b"\x00"]
    assert [channel.name for channel in state["channels"]] == [
        "public", "#wardriving"]
    stored = protocol.handle_frame(bytes((31, 5)))[0]
    assert stored[2:34].split(b"\0", 1)[0] == b"#wardriving"
    assert stored[34:50] == wardriving.psk
    assert protocol.handle_frame(bytes((31, 8))) == [b"\x01\x02"]


def test_channel_send_keeps_meshmapper_slot_channel_and_timestamp():
    protocol, lora, _state = _protocol()
    wardriving = make_hashtag_channel("#wardriving")
    set_command = (bytes((32, 5)) + b"#wardriving".ljust(32, b"\0")
                   + wardriving.psk)
    protocol.handle_frame(set_command)
    command = (bytes((3, 0, 5)) + struct.pack("<I", 1_700_000_001)
               + b"MM:example")

    assert protocol.handle_frame(command) == [b"\x00"]
    lora.send_meshcore_channel_message.assert_called_once_with(
        "MM:example", "WatchDogs_deadbeef", 5, 1_700_000_001,
        channel=wardriving)


def test_control_discovery_and_received_push_frames_match_companion_format():
    protocol, lora, _state = _protocol()
    payload = bytes.fromhex("800c0102030400000000")
    assert protocol.handle_frame(b"\x37" + payload) == [b"\x00"]
    lora.send_meshcore_control.assert_called_once_with(payload)

    packet = bytes.fromhex("2e00") + payload
    assert protocol.raw_packet_event(packet, -87, 6.5) == (
        bytes((0x88, 26, 169)) + packet)
    response = bytes.fromhex("91f801020304") + bytes(32)
    assert protocol.control_event(response, 0, -87, 6.5) == (
        bytes((0x8E, 26, 169, 0)) + response)


def test_export_contact_name_time_stats_and_private_signing_requires_auth():
    protocol, _lora, state = _protocol()
    assert protocol.handle_frame(b"\x05") == [
        b"\x09" + struct.pack("<I", 1_700_000_000)]
    assert protocol.handle_frame(b"\x08Mapper Radio") == [b"\x00"]
    assert state["name"] == "Mapper Radio"
    assert protocol.handle_frame(b"\x11") == [b"\x0bsigned-advert"]
    assert protocol.handle_frame(bytes((56, 1)))[0][:2] == bytes((24, 1))

    # A caller cannot turn the peripheral into a signing oracle without a
    # BlueZ-authenticated bonded connection.
    assert protocol.handle_frame(b"\x21") == [b"\x01\x01"]
    assert protocol.handle_frame(b"\x22hello") == [b"\x01\x01"]
    assert protocol.handle_frame(b"\x23") == [b"\x01\x01"]


def test_authenticated_signing_is_bounded_protocol_compatible_and_verifiable():
    protocol, lora, _state = _protocol()
    start = protocol.handle_frame(b"\x21", authenticated=True)[0]
    assert start == bytes((19, 0)) + struct.pack("<I", MAX_SIGN_DATA_LEN)
    assert protocol.handle_frame(
        b"\x22hello ", authenticated=True) == [b"\x00"]
    assert protocol.handle_frame(
        b"\x22world", authenticated=True) == [b"\x00"]
    response = protocol.handle_frame(b"\x23", authenticated=True)[0]
    assert response[0] == 20
    assert len(response) == 65
    lora.private.public_key().verify(response[1:], b"hello world")
    assert protocol.handle_frame(
        b"\x23", authenticated=True) == [protocol.ERR_BAD_STATE]


def test_signing_overflow_timeout_disconnect_and_unauthenticated_reset():
    now = [100.0]
    protocol, _lora, _state = _protocol()
    protocol.clock = lambda: now[0]
    assert protocol.handle_frame(b"\x21", authenticated=True)[0][0] == 19
    for _ in range(MAX_SIGN_DATA_LEN // 254):
        assert protocol.handle_frame(
            b"\x22" + bytes(254), authenticated=True) == [b"\x00"]
    assert protocol.handle_frame(
        b"\x22" + bytes(65), authenticated=True) == [protocol.ERR_TABLE_FULL]
    assert protocol.handle_frame(
        b"\x23", authenticated=True) == [protocol.ERR_BAD_STATE]

    protocol.handle_frame(b"\x21", authenticated=True)
    protocol.handle_frame(b"\x22secret", authenticated=True)
    assert protocol.handle_frame(b"\x21") == [protocol.ERR_UNSUPPORTED]
    assert protocol.handle_frame(
        b"\x23", authenticated=True) == [protocol.ERR_BAD_STATE]

    protocol.handle_frame(b"\x21", authenticated=True)
    now[0] += 31
    assert protocol.handle_frame(
        b"\x22late", authenticated=True) == [protocol.ERR_BAD_STATE]
    protocol.handle_frame(b"\x21", authenticated=True)
    protocol.cancel_signing()
    assert protocol.handle_frame(
        b"\x23", authenticated=True) == [protocol.ERR_BAD_STATE]


def test_secure_bluez_flags_do_not_retain_unprotected_read_notify_or_write():
    assert MESHCORE_RX_FLAGS == (
        "write-without-response", "encrypt-authenticated-write")
    assert MESHCORE_TX_FLAGS == (
        "encrypt-authenticated-read", "encrypt-authenticated-notify")
    source = Path("watchdogs/meshcore_ble.py").read_text()
    assert "dbus.SystemBus(private=True)" in source


def test_bluez_registration_is_async_and_ready_only_after_both_replies():
    gatt = NS(
        RegisterApplication=Mock(),
        UnregisterApplication=Mock(),
    )
    advertising = NS(
        RegisterAdvertisement=Mock(),
        UnregisterAdvertisement=Mock(),
    )
    ready = Mock()
    terminal = Mock()
    registration = _BluezRegistration(
        gatt, advertising, "/app", "/app/advertisement0",
        object_path=lambda path: "object:" + path,
        stop_requested=lambda: False,
        on_ready=ready,
        on_terminal=terminal,
    )

    assert registration.begin()
    gatt.RegisterApplication.assert_called_once()
    assert gatt.RegisterApplication.call_args.args == ("object:/app", {})
    advertising.RegisterAdvertisement.assert_not_called()
    ready.assert_not_called()

    app_reply = gatt.RegisterApplication.call_args.kwargs["reply_handler"]
    app_reply()
    advertising.RegisterAdvertisement.assert_called_once()
    assert advertising.RegisterAdvertisement.call_args.args == (
        "object:/app/advertisement0", {})
    ready.assert_not_called()

    advertisement_reply = (
        advertising.RegisterAdvertisement.call_args.kwargs["reply_handler"])
    advertisement_reply()
    ready.assert_called_once_with()
    assert registration.ready

    assert registration.cleanup() == []
    advertising.UnregisterAdvertisement.assert_called_once_with(
        "object:/app/advertisement0")
    gatt.UnregisterApplication.assert_called_once_with("object:/app")


def test_bluez_registration_failure_does_not_unregister_unaccepted_objects():
    gatt = NS(
        RegisterApplication=Mock(),
        UnregisterApplication=Mock(),
    )
    advertising = NS(
        RegisterAdvertisement=Mock(),
        UnregisterAdvertisement=Mock(),
    )
    terminal = Mock()
    registration = _BluezRegistration(
        gatt, advertising, "/app", "/advertisement",
        object_path=lambda path: path,
        stop_requested=lambda: False,
        on_ready=Mock(),
        on_terminal=terminal,
    )
    assert registration.begin()

    error = gatt.RegisterApplication.call_args.kwargs["error_handler"]
    error(RuntimeError("No object received"))

    assert registration.failure == (
        "BlueZ GATT application registration failed: No object received")
    terminal.assert_called_once_with()
    advertising.RegisterAdvertisement.assert_not_called()
    assert registration.cleanup() == []
    gatt.UnregisterApplication.assert_not_called()
    advertising.UnregisterAdvertisement.assert_not_called()


def test_bluez_advertisement_failure_unregisters_only_accepted_gatt_app():
    gatt = NS(
        RegisterApplication=Mock(),
        UnregisterApplication=Mock(),
    )
    advertising = NS(
        RegisterAdvertisement=Mock(),
        UnregisterAdvertisement=Mock(),
    )
    terminal = Mock()
    registration = _BluezRegistration(
        gatt, advertising, "/app", "/advertisement",
        object_path=lambda path: path,
        stop_requested=lambda: False,
        on_ready=Mock(),
        on_terminal=terminal,
    )
    assert registration.begin()
    gatt.RegisterApplication.call_args.kwargs["reply_handler"]()

    error = advertising.RegisterAdvertisement.call_args.kwargs["error_handler"]
    error(RuntimeError("advertising unavailable"))

    assert registration.failure == (
        "BlueZ advertisement registration failed: advertising unavailable")
    terminal.assert_called_once_with()
    assert registration.cleanup() == []
    advertising.UnregisterAdvertisement.assert_not_called()
    gatt.UnregisterApplication.assert_called_once_with("/app")


def test_bluez_registration_stop_and_timeout_are_bounded():
    stopped = [False]
    now = [100.0]
    gatt = NS(
        RegisterApplication=Mock(),
        UnregisterApplication=Mock(),
    )
    advertising = NS(
        RegisterAdvertisement=Mock(),
        UnregisterAdvertisement=Mock(),
    )
    terminal = Mock()
    registration = _BluezRegistration(
        gatt, advertising, "/app", "/advertisement",
        object_path=lambda path: path,
        stop_requested=lambda: stopped[0],
        on_ready=Mock(),
        on_terminal=terminal,
        clock=lambda: now[0],
        timeout=BLUEZ_REGISTRATION_TIMEOUT,
    )
    assert registration.begin()
    stopped[0] = True
    gatt.RegisterApplication.call_args.kwargs["reply_handler"]()
    advertising.RegisterAdvertisement.assert_not_called()
    terminal.assert_called_once_with()
    assert registration.cleanup() == []
    gatt.UnregisterApplication.assert_called_once_with("/app")

    gatt.RegisterApplication.reset_mock()
    terminal.reset_mock()
    stopped[0] = False
    registration = _BluezRegistration(
        gatt, advertising, "/app", "/advertisement",
        object_path=lambda path: path,
        stop_requested=lambda: False,
        on_ready=Mock(),
        on_terminal=terminal,
        clock=lambda: now[0],
        timeout=BLUEZ_REGISTRATION_TIMEOUT,
    )
    assert registration.begin()
    now[0] += BLUEZ_REGISTRATION_TIMEOUT
    assert registration.expire()
    assert "no reply within 10 seconds" in registration.failure
    terminal.assert_called_once_with()


def test_bluez_object_manager_and_loop_follow_official_registration_order():
    source = Path("watchdogs/meshcore_ble.py").read_text()
    application = source.split("class Application", 1)[1].split(
        "class Service", 1)[0]
    assert "dbus.ObjectPath(obj.path)" in application
    loop = source.index("self._loop = GLib.MainLoop()")
    begin = source.index("registration.begin()", loop)
    assert loop < begin


def test_invalid_frames_and_unsupported_scope_fail_closed():
    protocol, _lora, _state = _protocol()
    assert protocol.handle_frame(b"") == [b"\x01\x06"]
    assert protocol.handle_frame(bytes(256)) == [b"\x01\x06"]
    assert protocol.handle_frame(b"\xff") == [b"\x01\x01"]
    assert protocol.handle_frame(b"\x36\0" + bytes(16)) == [b"\x01\x01"]
    assert protocol.handle_frame(b"\x03\0\0" + struct.pack("<I", 1)) == [
        b"\x01\x06"]


def test_pairing_security_claims_first_device_and_requires_exact_bond_state():
    policy = _PairingSecurity("/org/bluez/hci1")
    first_path = "/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF"
    first = {
        "Adapter": "/org/bluez/hci1",
        "Address": "aa:bb:cc:dd:ee:ff",
        "Alias": "Mapper phone",
    }
    policy.open_window()
    assert policy.claim_pairing_device(first_path, first) == (
        "AA:BB:CC:DD:EE:FF", "Mapper phone")
    with pytest.raises(PermissionError):
        policy.retain_claim()
    assert policy.mark_passkey_displayed(first_path, first) == (
        "AA:BB:CC:DD:EE:FF", "Mapper phone")
    with pytest.raises(PermissionError):
        policy.claim_pairing_device(
            "/org/bluez/hci1/dev_11_22_33_44_55_66",
            {**first, "Address": "11:22:33:44:55:66"})
    with pytest.raises(PermissionError):
        policy.claim_pairing_device(
            "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
            {**first, "Adapter": "/org/bluez/hci0"})

    for missing in ("Paired", "Bonded", "Trusted"):
        props = {**first, "Paired": True, "Bonded": True, "Trusted": True}
        props[missing] = False
        with pytest.raises(PermissionError):
            policy.authorize_gatt(first_path, props)
    secure = {**first, "Paired": True, "Bonded": True, "Trusted": True}
    assert policy.authorize_gatt(first_path, secure)[0] == (
        "AA:BB:CC:DD:EE:FF")
    assert policy.retain_claim() == (
        "AA:BB:CC:DD:EE:FF", "Mapper phone")
    policy.close_window()
    assert policy.authorize_gatt(first_path, secure)[1] == "Mapper phone"


def test_pairing_security_promotes_private_address_to_bonded_identity():
    policy = _PairingSecurity("/org/bluez/hci1")
    path = "/org/bluez/hci1/dev_77_88_99_AA_BB_CC"
    private = {
        "Adapter": "/org/bluez/hci1",
        "Address": "77:88:99:AA:BB:CC",
        "AddressType": "random",
        "Alias": "Nothing phone",
    }
    identity = {
        **private,
        "Address": "2C:BE:EE:98:55:6F",
        "AddressType": "public",
        "Paired": True,
        "Bonded": True,
        "Trusted": True,
    }
    policy.open_window()
    policy.mark_passkey_displayed(path, private)

    with pytest.raises(PermissionError):
        policy.authorize_gatt(path, identity)
    assert policy.promote_paired_identity(path, identity) == (
        "2C:BE:EE:98:55:6F", "Nothing phone")
    assert policy.authorize_gatt(path, identity) == (
        "2C:BE:EE:98:55:6F", "Nothing phone")
    assert policy.retain_claim() == (
        "2C:BE:EE:98:55:6F", "Nothing phone")


def test_pairing_security_identity_promotion_keeps_exact_session_binding():
    policy = _PairingSecurity("/org/bluez/hci1")
    claimed_path = "/org/bluez/hci1/dev_77_88_99_AA_BB_CC"
    private = {
        "Adapter": "/org/bluez/hci1",
        "Address": "77:88:99:AA:BB:CC",
    }
    paired = {
        **private,
        "Address": "2C:BE:EE:98:55:6F",
        "Paired": True,
        "Bonded": True,
        "Trusted": True,
    }
    policy.open_window()
    policy.mark_passkey_displayed(claimed_path, private)

    with pytest.raises(PermissionError):
        policy.promote_paired_identity(
            "/org/bluez/hci1/dev_2C_BE_EE_98_55_6F", paired)
    with pytest.raises(PermissionError):
        policy.promote_paired_identity(
            claimed_path, {**paired, "Bonded": False})
    policy.claimed_authenticated = False
    with pytest.raises(PermissionError):
        policy.promote_paired_identity(claimed_path, paired)


def test_pairing_security_notify_requires_only_retained_authenticated_phone():
    retained_path = "/org/bluez/hci1/dev_AA_BB_CC_DD_EE_FF"
    retained = {
        "Adapter": "/org/bluez/hci1",
        "Address": "AA:BB:CC:DD:EE:FF",
        "Alias": "Mapper phone",
        "Connected": True,
        "Paired": True,
        "Bonded": True,
        "Trusted": True,
    }
    policy = _PairingSecurity(
        "/org/bluez/hci1", retained["Address"], retained["Alias"])
    assert policy.authorize_notify([(retained_path, retained)]) == (
        retained["Address"], retained["Alias"])

    other_path = "/org/bluez/hci1/dev_11_22_33_44_55_66"
    other = {
        **retained,
        "Address": "11:22:33:44:55:66",
        "Alias": "Other bonded device",
    }
    with pytest.raises(PermissionError):
        policy.authorize_notify([(other_path, other)])
    with pytest.raises(PermissionError):
        policy.authorize_notify([
            (retained_path, retained), (other_path, other)])
    with pytest.raises(PermissionError):
        policy.authorize_notify([
            (retained_path, {**retained, "Trusted": False})])


def test_bluez_agent_rejects_just_works_authorization_source():
    source = Path("watchdogs/meshcore_ble.py").read_text()
    block = source.split("def RequestAuthorization", 1)[1].split(
        "def AuthorizeService", 1)[0]
    assert "Just Works pairing is not permitted" in block
    assert "pairing_device(device)" not in block


def test_pairing_security_retained_phone_rejects_replacement_and_forgets_exactly():
    policy = _PairingSecurity(
        "/org/bluez/hci2", "AA:BB:CC:DD:EE:FF", "Old phone")
    policy.open_window()
    with pytest.raises(PermissionError):
        policy.claim_pairing_device(
            "/org/bluez/hci2/dev_11_22_33_44_55_66",
            {"Adapter": "/org/bluez/hci2",
             "Address": "11:22:33:44:55:66"})
    assert policy.forget() == ("AA:BB:CC:DD:EE:FF", "Old phone")
    assert policy.retained_address == ""


def test_manager_resolves_adapter_runs_backend_and_forwards_radio_packets():
    protocol, _lora, _state = _protocol()
    started = threading.Event()

    class Backend:
        def __init__(self, adapter, local_name, value, stop_event, callback,
                     **kwargs):
            self.adapter = adapter
            self.local_name = local_name
            self.protocol = value
            self.stop_event = stop_event
            self.callback = callback
            self.frames = []
            self.drop_count = 0

        def run(self):
            self.callback("ready", self.adapter)
            started.set()
            self.stop_event.wait(1.0)
            self.callback("stopped", "done")

        def notify(self, frame, important=False):
            self.frames.append((bytes(frame), important))
            return True

    manager = MeshCoreBleManager(
        protocol, backend_factory=Backend,
        adapter_resolver=lambda selected: (
            "hci1" if selected == "AA:BB:CC:DD:EE:FF" else None),
        adapter_lister=lambda: [("11:22:33:44:55:66", "hci0")],
    )
    assert manager.start("AA:BB:CC:DD:EE:FF", "WDG")
    assert started.wait(1.0)
    assert manager.state == "ready"
    assert manager._backend.local_name == "MeshCore-WDG"

    payload = bytes.fromhex("91f801020304") + bytes(32)
    manager.on_radio_packet(
        b"raw-packet", -70, 4.0, 0x0B, payload, 0)
    assert manager._backend.frames[0] == (
        bytes((0x88, 16, 186)) + b"raw-packet", False)
    assert manager._backend.frames[1] == (
        bytes((0x8E, 16, 186, 0)) + payload, True)
    assert manager.stop()
    assert manager.state == "stopped"
    assert not manager.worker_active


def test_manager_reports_missing_adapter_without_starting_worker():
    protocol, _lora, _state = _protocol()
    manager = MeshCoreBleManager(
        protocol, adapter_resolver=lambda _selected: None,
        adapter_lister=lambda: [],
    )
    assert not manager.start("auto", "WDG")
    assert manager.state == "error"
    assert "No BlueZ Bluetooth adapter" in manager.last_error
    assert not manager.worker_active


def test_manager_exposes_stable_controller_key_for_bond_store():
    protocol, _lora, _state = _protocol()
    manager = MeshCoreBleManager(
        protocol, adapter_resolver=lambda _selection: None,
        adapter_lister=lambda: [("11:22:33:44:55:66", "hci7")],
    )

    assert manager.resolve_controller_key("auto") == "11:22:33:44:55:66"

    explicit = MeshCoreBleManager(
        protocol,
        adapter_resolver=lambda _selection: "hci9",
        adapter_lister=lambda: [],
    )
    assert explicit.resolve_controller_key(
        "aa:bb:cc:dd:ee:ff") == "AA:BB:CC:DD:EE:FF"


def test_manager_pairing_lease_lifecycle_retains_and_forgets_phone():
    protocol, _lora, _state = _protocol()
    started = threading.Event()
    ordering = []

    class Backend:
        def __init__(self, adapter, name, value, stop_event, callback,
                     *, paired_address, paired_name):
            self.adapter = adapter
            self.stop_event = stop_event
            self.callback = callback
            self.paired_address = paired_address
            self.paired_name = paired_name
            self.drop_count = 0

        def run(self):
            self.callback("ready", self.adapter)
            started.set()
            self.stop_event.wait(2)
            self.callback("stopped", "done")

        def open_pairing(self, seconds):
            assert seconds == 120
            self.callback("pairing_open", seconds)
            self.callback("pairing_pin", "042731")
            return True

        def close_pairing(self, timeout=5):
            ordering.append("agent-unregistered-and-adapter-restored")
            self.callback("pairing_closed", "paired")
            return True

        def forget_phone(self, timeout=5):
            assert timeout == 5
            self.callback("bond_removed", {
                "address": self.paired_address,
                "name": self.paired_name,
            })
            return True

        def notify(self, frame, important=False):
            return True

    manager = MeshCoreBleManager(
        protocol,
        backend_factory=Backend,
        adapter_resolver=lambda _selection: "hci1",
        pairing_lease_acquire=lambda seconds: (
            ordering.append(("lease-acquired", seconds)) or True),
        pairing_lease_release=lambda: (ordering.append("lease-released") or True),
    )
    assert manager.start("auto", "WDG")
    assert started.wait(1)
    assert manager.open_pairing()
    assert manager.pairing_state == "open"
    assert manager.pairing_pin == "042731"
    manager._event("paired", {
        "address": "aa:bb:cc:dd:ee:ff", "name": "Pixel"})
    manager._backend.paired_address = manager.paired_address
    manager._backend.paired_name = manager.paired_name
    assert manager.close_pairing()
    assert ordering == [
        ("lease-acquired", 120),
        "agent-unregistered-and-adapter-restored",
        "lease-released",
    ]
    assert manager.pairing_state == "paired"
    assert manager.paired_address == "AA:BB:CC:DD:EE:FF"
    assert manager.forget_phone()
    assert manager.paired_address == ""
    assert manager.pairing_state == "closed"
    assert manager.stop()


def test_manager_pairing_cleanup_failure_is_fail_closed_and_keeps_lease():
    protocol, _lora, _state = _protocol()
    started = threading.Event()
    released = Mock(return_value=True)

    class Backend:
        drop_count = 0

        def __init__(self, adapter, name, value, stop_event, callback, **kwargs):
            self.adapter = adapter
            self.stop_event = stop_event
            self.callback = callback

        def run(self):
            self.callback("ready", self.adapter)
            started.set()
            self.stop_event.wait(2)
            self.callback("stopped", "done")

        def open_pairing(self, seconds):
            self.callback("pairing_open", seconds)
            return True

        def close_pairing(self, timeout=5):
            self.callback("pairing_cleanup_error", "agent unregister failed")
            return False

        def notify(self, frame, important=False):
            return True

    manager = MeshCoreBleManager(
        protocol, backend_factory=Backend,
        adapter_resolver=lambda _selection: "hci0",
        pairing_lease_acquire=lambda _seconds: True,
        pairing_lease_release=released,
    )
    assert manager.start()
    assert started.wait(1)
    assert manager.open_pairing(30)
    assert not manager.close_pairing()
    assert manager.pairing_cleanup_pending
    assert manager.pairing_state == "error"
    released.assert_not_called()
    assert not manager.stop()


def test_manager_retries_uncertain_daemon_release_after_local_cleanup():
    protocol, _lora, _state = _protocol()
    releases = Mock(return_value=True)
    manager = MeshCoreBleManager(
        protocol, pairing_lease_release=releases)
    manager._pairing_lease_held = True
    manager._pairing_local_cleanup_done = True
    manager.pairing_cleanup_pending = True
    manager.pairing_state = "error"

    assert manager.close_pairing()
    releases.assert_called_once_with()
    assert not manager._pairing_lease_held
    assert not manager.pairing_cleanup_pending


def test_manager_rejects_bad_retained_address_and_preserves_good_metadata():
    protocol, _lora, _state = _protocol()
    manager = MeshCoreBleManager(
        protocol, adapter_resolver=lambda _selection: "hci0")
    assert not manager.start(paired_address="not-an-address")
    assert "Invalid retained" in manager.last_error


def test_manager_rejects_forget_while_connected_and_preserves_critical_events():
    protocol, _lora, _state = _protocol()
    manager = MeshCoreBleManager(protocol)
    backend = NS(forget_phone=Mock(return_value=True))
    manager._backend = backend
    manager.state = "ready"
    manager.paired_address = "AA:BB:CC:DD:EE:FF"
    manager.paired_name = "Phone"
    manager.pairing_state = "paired"
    manager._event("connected", {"address": manager.paired_address})
    assert not manager.forget_phone()
    backend.forget_phone.assert_not_called()

    while not manager.events.full():
        manager._event("connected", "status")
    manager._event("pairing_cleanup_error", "agent unregister failed")
    queued = manager.poll_events(100)
    assert ("pairing_cleanup_error", "agent unregister failed") in queued
    assert manager.event_drop_count == 1
    assert "agent unregister failed" in manager.last_error
    assert "queue overflow" in manager.last_error


def test_manager_logs_bluez_and_pairing_cleanup_errors(caplog):
    protocol, _lora, _state = _protocol()
    manager = MeshCoreBleManager(protocol)

    with caplog.at_level(logging.ERROR, logger="watchdogs.meshcore_ble"):
        manager._event("error", "GATT registration failed")
        manager._event("pairing_cleanup_error", "agent unregister failed")

    assert "MeshCore companion BLE error: GATT registration failed" in caplog.text
    assert (
        "MeshCore companion pairing cleanup error: agent unregister failed"
        in caplog.text)


def test_manager_waits_for_confirmed_phone_removal():
    protocol, _lora, _state = _protocol()
    backend = NS(forget_phone=Mock(return_value=False))
    manager = MeshCoreBleManager(protocol)
    manager._backend = backend
    manager.state = "ready"
    manager.paired_address = "AA:BB:CC:DD:EE:FF"
    manager.paired_name = "Phone"
    manager.pairing_state = "paired"

    assert not manager.forget_phone(timeout=0.25)
    backend.forget_phone.assert_called_once_with(timeout=0.25)
    assert manager.pairing_state == "error"
    assert manager.pairing_cleanup_pending
    assert manager.paired_address == "AA:BB:CC:DD:EE:FF"


def test_app_starts_companion_then_pauses_conflicting_host_scan():
    game = WatchDogsGame.__new__(WatchDogsGame)
    game._mc_node_name = "WatchDogs_test"
    game._meshcore_startup_settings = {
        "meshcore_ble_enabled": True,
        "meshcore_ble_adapter": "AA:BB:CC:DD:EE:FF",
    }
    manager = NS(running=False, last_error="")

    def start(adapter, node_name, **kwargs):
        assert adapter == "AA:BB:CC:DD:EE:FF"
        assert node_name == "WatchDogs_test"
        assert kwargs == {"paired_address": "", "paired_name": ""}
        manager.running = True
        return True

    manager.start = Mock(side_effect=start)
    game._meshcore_ble = manager
    game._lora = NS(meshcore_ready=True)
    game.wardrive = NS(
        settings=game._meshcore_startup_settings,
        meshcore_ble_blocks_host_scan=Mock(
            side_effect=lambda: manager.running),
        pause_host_ble_for_meshcore=Mock(),
    )

    assert game._start_meshcore_companion_ble()
    game.wardrive.pause_host_ble_for_meshcore.assert_called_once_with()


def test_app_does_not_commit_companion_config_when_atomic_save_fails(
        monkeypatch):
    game = WatchDogsGame.__new__(WatchDogsGame)
    game._mc_node_name = "Old name"
    game._mc_region = "us_ca_narrow"
    game._mc_channels_list = [PUBLIC_CHANNEL]
    game._lora = NS(set_mc_channels=Mock())
    monkeypatch.setattr(
        "watchdogs.lora_manager.save_meshcore_config",
        lambda *_args, **_kwargs: False)

    with pytest.raises(OSError):
        game._set_meshcore_companion_name("New name")
    assert game._mc_node_name == "Old name"

    replacement = [PUBLIC_CHANNEL, make_hashtag_channel("#wardriving")]
    with pytest.raises(OSError):
        game._set_meshcore_companion_channels(replacement)
    assert game._mc_channels_list == [PUBLIC_CHANNEL]
    game._lora.set_mc_channels.assert_not_called()


def test_meshcore_config_is_atomic_private_and_keeps_mapper_channels(
        monkeypatch, tmp_path):
    path = tmp_path / ".watchdogs_meshcore.json"
    monkeypatch.setattr(
        "watchdogs.lora_manager._meshcore_config_path", lambda: str(path))
    channel = make_hashtag_channel("#wardriving")

    assert save_meshcore_config(
        "WatchDogs_test", [PUBLIC_CHANNEL, channel], region="us_ca_narrow")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert {value.name for value in tmp_path.iterdir()} == {
        ".watchdogs_meshcore.json"}
    loaded = load_meshcore_config()
    assert loaded["node_name"] == "WatchDogs_test"
    assert loaded["region"] == "us_ca_narrow"
    assert [value.name for value in loaded["_channels"]] == [
        "public", "#wardriving"]
