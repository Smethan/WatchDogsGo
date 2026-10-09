"""Application-level routing with a selected legacy board and a managed peer."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

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
