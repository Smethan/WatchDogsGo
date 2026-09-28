from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import watchdogs.app as app_module
from watchdogs.app import WatchDogsGame
from watchdogs.bluetooth_bonds import BluetoothPhoneBondStore

CONTROLLER = "88:A2:9E:76:54:EA"
PHONE = "2C:BE:EE:98:55:6F"


def _store(tmp_path: Path) -> BluetoothPhoneBondStore:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    return BluetoothPhoneBondStore(
        root / "bluetooth_bonds.json",
        owner_uid=root.stat().st_uid,
        owner_gid=root.stat().st_gid,
    )


def _game(tmp_path: Path):
    game = WatchDogsGame.__new__(WatchDogsGame)
    game._bluetooth_bonds = _store(tmp_path)
    game._bluetooth_bond_error = ""
    game._bluetooth_bond_results = app_module.Queue(maxsize=4)
    game._bluetooth_bond_thread = None
    game._bluetooth_bond_thread_factory = app_module.threading.Thread
    game._meshtastic_transition_lock = app_module.threading.Lock()
    game._meshtastic_transition_state = ""
    game._term_add = Mock()
    game.msg = Mock()
    game.wardrive = NS(
        settings={
            "meshcore_ble_enabled": True,
            "meshcore_ble_adapter": CONTROLLER,
            "meshtastic_phone_adapter": CONTROLLER,
            "meshcore_ble_paired_address": "",
            "meshcore_ble_paired_name": "",
        },
        persist_settings=Mock(),
        meshcore_ble_blocks_host_scan=Mock(return_value=False),
    )
    return game


def test_meshcore_start_uses_controller_shared_phone(tmp_path):
    game = _game(tmp_path)
    game._bluetooth_bonds.commit(
        CONTROLLER, PHONE, "Pixel", source="meshtastic")
    manager = NS(
        running=False,
        last_error="",
        resolve_controller_key=Mock(return_value=CONTROLLER),
        start=Mock(return_value=True),
    )
    game._meshcore_ble = manager
    game._lora = NS(meshcore_ready=True)
    game._mc_node_name = "WDG"

    assert game._start_meshcore_companion_ble() is True
    manager.start.assert_called_once_with(
        CONTROLLER, "WDG", paired_address=PHONE, paired_name="Pixel")


def test_meshtastic_reconcile_adopts_verified_shared_phone(
        tmp_path, monkeypatch):
    game = _game(tmp_path)
    game._bluetooth_bonds.commit(
        CONTROLLER, PHONE, "Pixel", source="meshcore")
    manager = NS(
        phone_ble_adapter_applied=CONTROLLER,
        phone_bond_address="",
        last_error="",
        set_phone_pairing_mode=Mock(return_value=True),
        adopt_phone_bond=Mock(return_value=True),
        clear_phone_identity=Mock(return_value=True),
    )
    game._meshtastic = manager
    monkeypatch.setattr(
        app_module, "verify_bluez_bond",
        lambda controller, address: {
            "controller": controller,
            "address": address,
            "paired": True,
            "bonded": True,
            "trusted": True,
        })

    assert game._meshtastic_bond_reconcile_worker() == (
        True, "Shared phone adopted by Meshtastic")
    manager.set_phone_pairing_mode.assert_called_once_with("random_pin")
    manager.adopt_phone_bond.assert_called_once_with(PHONE, CONTROLLER)


def test_reconcile_does_not_restart_already_random_pin_phone(
        tmp_path, monkeypatch):
    game = _game(tmp_path)
    game._bluetooth_bonds.commit(
        CONTROLLER, PHONE, "Pixel", source="meshcore")
    manager = NS(
        phone_ble_adapter_applied=CONTROLLER,
        phone_bond_address=PHONE,
        phone_bond_connected=True,
        phone_pairing_mode="random_pin",
        last_error="",
        set_phone_pairing_mode=Mock(return_value=False),
        adopt_phone_bond=Mock(return_value=True),
        clear_phone_identity=Mock(return_value=True),
    )
    game._meshtastic = manager
    monkeypatch.setattr(
        app_module, "verify_bluez_bond",
        lambda _controller, _address: {"paired": True})

    assert game._meshtastic_bond_reconcile_worker() == (
        True, "Shared phone adopted by Meshtastic")
    manager.set_phone_pairing_mode.assert_not_called()
    manager.adopt_phone_bond.assert_called_once_with(PHONE, CONTROLLER)


def test_reconcile_is_noop_when_daemon_already_authorizes_shared_phone(
        tmp_path, monkeypatch):
    game = _game(tmp_path)
    game._bluetooth_bonds.commit(
        CONTROLLER, PHONE, "Pixel", source="meshcore")
    manager = NS(
        phone_ble_adapter_applied=CONTROLLER,
        phone_bond_address=PHONE,
        phone_pairing_mode="random_pin",
        phone_bond={
            "present": True,
            "address": PHONE,
            "controller": CONTROLLER,
            "authentication": "random_pin",
            "paired": True,
            "bonded": True,
            "trusted": True,
            "service_authorized": True,
        },
        last_error="",
        set_phone_pairing_mode=Mock(return_value=True),
        adopt_phone_bond=Mock(return_value=True),
        clear_phone_identity=Mock(return_value=True),
    )
    game._meshtastic = manager
    monkeypatch.setattr(
        app_module, "verify_bluez_bond",
        lambda _controller, _address: {"paired": True})

    assert game._meshtastic_bond_reconcile_worker() == (
        True, "Shared phone already active in Meshtastic")
    manager.set_phone_pairing_mode.assert_not_called()
    manager.adopt_phone_bond.assert_not_called()


def test_meshtastic_reconcile_finishes_meshcore_cleanup(tmp_path):
    game = _game(tmp_path)
    game._bluetooth_bonds.mark_cleanup_pending(
        CONTROLLER, PHONE, "Pixel", source="meshcore")
    manager = NS(
        phone_ble_adapter_applied=CONTROLLER,
        phone_bond_address=PHONE,
        last_error="",
        set_phone_pairing_mode=Mock(return_value=True),
        adopt_phone_bond=Mock(return_value=True),
        clear_phone_identity=Mock(return_value=True),
    )
    game._meshtastic = manager

    assert game._meshtastic_bond_reconcile_worker() == (
        True, "Stale shared phone identity cleared")
    manager.clear_phone_identity.assert_called_once_with(PHONE)
    assert game._bluetooth_bonds.get(CONTROLLER) is None


def test_meshtastic_send_is_blocked_during_policy_transition(tmp_path):
    game = _game(tmp_path)
    game._meshtastic = NS(
        connected=True, send_text=Mock(return_value="mt-1"))
    game._meshtastic_update_running = False
    assert game._begin_meshtastic_transition("test") is True
    try:
        assert game._queue_meshtastic_text("hello", channel=2) is None
    finally:
        game._end_meshtastic_transition()
    game._meshtastic.send_text.assert_not_called()
