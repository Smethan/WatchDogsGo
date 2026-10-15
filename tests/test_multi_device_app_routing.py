"""Application-level routing with a selected legacy board and a managed peer."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from watchdogs.app import WatchDogsGame


def mixed_app():
    app = WatchDogsGame.__new__(WatchDogsGame)
    app.serial = Mock(is_open=True)
    legacy = NS(endpoint='/dev/old', connected=True, device_id='', serial=app.serial)
    peer = NS(endpoint='/dev/new', connected=True, device_id='A1B2C3D4E5F6',
              serial=Mock(is_open=True))
    app.c5_pool = NS(available=True, start_command=Mock(return_value=False),
        stop_all=Mock(), legacy_endpoints={'/dev/old'},
        endpoints={'/dev/old':legacy, '/dev/new':peer}, reserved=set())
    app.c5_pool.is_reserved = lambda session: session.device_id in app.c5_pool.reserved
    app.wardrive = NS(scan=object(), legacy_scan=object(), on_stop=Mock())
    app._clear_scan_state = Mock()
    app._term_add = Mock()
    app.msg = Mock()
    app.sniffing = False
    return app, peer


def test_legacy_scan_transition_does_not_stop_managed_peer():
    app, peer = mixed_app()
    app._start_scan_cmd('scan_airtag', 'bt_airtag', 'AirTag scan')
    app.serial.send_command.assert_called_once_with('stop')
    peer.serial.send_command.assert_not_called()
    app.c5_pool.stop_all.assert_not_called()
    assert app.wardrive.scan is app.wardrive.legacy_scan
    assert app._pending_cmd == 'scan_airtag'
    assert app._pending_state == 'bt_airtag'


def test_global_stop_cleans_managed_and_legacy_owners():
    app, peer = mixed_app()
    app._send('stop')
    app.c5_pool.stop_all.assert_called_once()
    app.serial.send_command.assert_called_once_with('stop')
    peer.serial.send_command.assert_not_called()
    app.wardrive.on_stop.assert_called_once()


def test_global_stop_does_not_write_reserved_legacy_endpoint():
    app, _ = mixed_app()
    app.c5_pool.reserved.add('')
    app._send('stop')
    app.c5_pool.stop_all.assert_called_once()
    app.serial.send_command.assert_not_called()


def test_global_stop_respects_endpoint_claim_before_firmware_identity_is_known():
    app, peer = mixed_app()
    selected = app.c5_pool.endpoints['/dev/old']
    app.c5_pool.is_reserved = lambda session: session is selected
    assert selected.device_id == '' and app.c5_pool.reserved == set()

    app._send('stop')

    app.c5_pool.stop_all.assert_called_once()
    app.serial.send_command.assert_not_called()
    peer.serial.send_command.assert_not_called()


@pytest.mark.parametrize("reconnect", [False, True])
def test_failed_ordinary_serial_write_shows_error_without_tx(reconnect):
    app = WatchDogsGame.__new__(WatchDogsGame)
    app.c5_pool = None
    app._flash_io_active = False
    app._term_add = Mock()
    serial = Mock(is_open=True)
    serial.send_command.return_value = False
    if reconnect:
        app.serial = None

        def reconnect_serial():
            app.serial = serial
            return True

        app._try_reconnect_esp32 = Mock(side_effect=reconnect_serial)
    else:
        app.serial = serial

    app._send("status")

    app._term_add.assert_called_once_with(
        "[ERR] status transport error: write failed", raw=True)
    assert not any(call.args[0] == "[TX] status"
                   or call.args[0] == "[TX] status (after reconnect)"
                   for call in app._term_add.call_args_list)
    serial.send_command.assert_called_once_with("status")


def test_failed_pool_stop_blocks_replacement_scan_start_and_preserves_local_state():
    app, peer = mixed_app()
    app.serial = peer.serial
    app.c5_pool.stop_all.return_value = False
    app.sniffing = False
    original_scan = app.wardrive.scan

    app._start_scan_cmd("unsupported_replacement", "wardriving", "replacement scan")

    app.c5_pool.stop_all.assert_called_once()
    assert app.wardrive.scan is original_scan
    app.wardrive.on_stop.assert_not_called()
    assert not hasattr(app, "_pending_cmd")
    assert not any(call.args[0] == "unsupported_replacement"
                   for call in app.serial.send_command.call_args_list)
    assert any("stop not accepted" in call.args[0]
               for call in app._term_add.call_args_list)


@pytest.mark.parametrize("failure", ["pool", "legacy"])
def test_failed_global_pool_stop_preserves_local_legacy_scan_state(failure):
    app, _peer = mixed_app()
    original_scan = app.wardrive.scan
    if failure == "pool":
        app.c5_pool.stop_all.return_value = False
    else:
        app.serial.send_command.return_value = False

    assert app._send("stop") is False

    assert app.wardrive.scan is original_scan
    app.wardrive.on_stop.assert_not_called()


@pytest.mark.parametrize("reconnect", [False, True])
def test_failed_direct_stop_preserves_local_scan_state(reconnect):
    app = WatchDogsGame.__new__(WatchDogsGame)
    app.c5_pool = None
    app._flash_io_active = False
    app._term_add = Mock()
    original_scan = object()
    app.wardrive = NS(scan=original_scan, on_stop=Mock())
    serial = Mock(is_open=True)
    serial.send_command.return_value = False
    if reconnect:
        app.serial = None

        def reconnect_serial():
            app.serial = serial
            return True

        app._try_reconnect_esp32 = Mock(side_effect=reconnect_serial)
    else:
        app.serial = serial

    assert app._send("stop") is False

    assert app.wardrive.scan is original_scan
    app.wardrive.on_stop.assert_not_called()
    assert app._term_add.call_args.args[0] == "[ERR] stop transport error: write failed"


def test_failed_legacy_transition_stop_preserves_scan_pointer_and_aborts_replacement():
    app, _peer = mixed_app()
    original_scan = app.wardrive.scan
    app.serial.send_command.return_value = False

    app._start_scan_cmd("scan_airtag", "bt_tracking", "AirTag scan")

    assert app.wardrive.scan is original_scan
    app.wardrive.on_stop.assert_not_called()
    app._clear_scan_state.assert_not_called()
    assert not hasattr(app, "_pending_cmd")
    assert app._term_add.call_args.args[0].startswith("[ERR] stop transport error")
