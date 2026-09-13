"""Offline flasher tests: never open ports or write a device."""
import io
import subprocess
import sys
from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest

from watchdogs import firmware_flash as flash
from watchdogs.app import WatchDogsGame
from watchdogs.config import FLASH_BOARDS


def port(device="/dev/ttyACM0", serial="ESP-ONE", vid=0x303a, pid=0x1001, location="1-2"):
    return NS(device=device, vid=vid, pid=pid, serial_number=serial, location=location)


def test_port_follows_same_serial_after_boot_renumbering():
    target = flash.select_target("/dev/ttyACM0", [port()])
    devices = [port(serial="OTHER"), port("/dev/ttyACM2")]
    assert target.resolve(devices) == "/dev/ttyACM2"
    with pytest.raises(RuntimeError):
        target.resolve([port(serial="OTHER")])
    with pytest.raises(RuntimeError):
        target.resolve([port(), port("/dev/ttyACM2")])


def test_port_location_fallback_and_discovery_never_picks_arbitrary_tty():
    target = flash.select_target(None, [port(serial=None)])
    assert target.resolve([port("/dev/ttyACM3", serial=None)]) == "/dev/ttyACM3"
    with pytest.raises(RuntimeError):
        target.resolve([port(serial=None, location="1-3")])
    with pytest.raises(RuntimeError):
        flash.select_target(None, [port(vid=0x9999, pid=1)])
    with pytest.raises(RuntimeError):
        flash.select_target(None, [port(), port("/dev/ttyACM1", serial="OTHER")])
    with pytest.raises(RuntimeError):
        flash.select_target("/dev/missing", [])


def test_wait_for_same_port_does_not_fall_back_to_other_board(monkeypatch):
    target = flash.FlashTarget.from_port(port())
    readings = iter([[port(serial="OTHER")], [port("/dev/ttyACM2")]])
    monkeypatch.setattr(flash, "ports_now", lambda:next(readings))
    monkeypatch.setattr(flash.time, "sleep", Mock())
    assert flash.wait_for_target(target) == (target, "/dev/ttyACM2")
    monkeypatch.setattr(flash, "ports_now", lambda:[port(serial="OTHER")])
    with pytest.raises(RuntimeError):
        flash.wait_for_target(target, timeout=0)


@pytest.mark.parametrize("board", ["xiao", "wroom"])
@pytest.mark.parametrize("manual", [False, True])
def test_command_matches_script_reset_and_verified_bundle_layout(tmp_path, board, manual):
    command = flash.flash_command("/venv/bin/python", "/dev/ttyACM2", board, tmp_path, manual)
    assert command[command.index("--before")+1] == ("no-reset" if manual else "default-reset")
    assert command[command.index("--after")+1] == "watchdog-reset"
    assert command[command.index("--baud")+1] == ("115200" if manual else "460800")
    assert command[command.index("--flash-size")+1] == "detect"
    pairs = command[command.index("--flash-size")+2:]
    expected = [value for name, offset in FLASH_BOARDS[board]["offsets"].items()
                for value in (offset, str(tmp_path/name))]
    assert pairs == expected and "erase-flash" not in command and "0x410000" not in command


@pytest.mark.parametrize("value,accepted", [("5.0.0",False), ("5.3.0",False), ("5.4.0",True),
    ("5.4.0.dev0",False), ("6.0.0",False), ("no module",False)])
def test_tool_version_requirement(monkeypatch, value, accepted):
    monkeypatch.setattr(flash.subprocess,"run",Mock(return_value=NS(returncode=0,stdout=value)))
    assert bool(flash.esptool_version("python")) == accepted


def test_tool_setup_reuses_current_or_private_python(tmp_path, monkeypatch):
    run = Mock(); monkeypatch.setattr(flash.subprocess,"run",run)
    monkeypatch.setattr(flash,"esptool_version",lambda _:"5.4.0")
    assert flash.ensure_esptool(tmp_path,Mock()) == (sys.executable,"5.4.0")
    run.assert_not_called()
    monkeypatch.setattr(flash,"esptool_version",lambda p:None if str(p)==sys.executable else "5.4.0")
    python, version = flash.ensure_esptool(tmp_path,Mock())
    assert str(tmp_path/"esptool-venv") in python and version == "5.4.0"
    run.assert_not_called()


def test_tool_setup_installs_privately_once_or_fails_before_flashing(tmp_path, monkeypatch):
    versions = iter([None,None,"5.4.0"])
    monkeypatch.setattr(flash,"esptool_version",lambda _:next(versions))
    run = Mock(return_value=NS(returncode=0,stdout="installed"))
    monkeypatch.setattr(flash.subprocess,"run",run)
    python, _ = flash.ensure_esptool(tmp_path,Mock())
    assert run.call_args_list[0].args[0] == [sys.executable,"-m","venv",str(tmp_path/"esptool-venv")]
    install = run.call_args_list[1].args[0]
    assert install[0] == python and install[-1] == flash.ESPTOOL_PACKAGE
    monkeypatch.setattr(flash,"esptool_version",lambda _:None)
    run.return_value = NS(returncode=1,stdout="venv is missing")
    with pytest.raises(RuntimeError,match="setup failed"):
        flash.ensure_esptool(tmp_path,Mock())


def test_esptool_output_is_logged_failure_not_retried(monkeypatch):
    process = Mock(stdout=io.StringIO("Writing...\nA fatal error occurred: packet transfer stopped\n"))
    process.wait.return_value = process.poll.return_value = 2
    popen = Mock(return_value=process); monkeypatch.setattr(flash.subprocess,"Popen",popen)
    report = Mock()
    with pytest.raises(RuntimeError,match="exit 2"):
        flash.run_flash(["python","-m","esptool"],report)
    assert popen.call_count == 1 and process.stdout.closed
    assert any("packet transfer stopped" in c.args[0] for c in report.call_args_list)


@pytest.fixture
def app(tmp_path, monkeypatch):
    import watchdogs.app as appmod
    a = WatchDogsGame.__new__(WatchDogsGame)
    a._app_dir = str(tmp_path)
    a.serial = Mock(device="/dev/ttyACM0",is_open=True,usb_port_info=None)
    a.state = NS(connected=True)
    a._esp32 = True; a._boot_serial_port = "/dev/ttyACM0"
    a._term_add = Mock(); a.msg = Mock(); a.wardrive = Mock()
    a._fw_version = "1.7.4"; a._fw_update_available = True
    a._flash_releases_loaded = True; a._flash_releases = []
    # AIO is present, as on the failing uConsole. It must never be power-cycled.
    a._aio_available = True
    monkeypatch.setattr(appmod.AioManager,"toggle",Mock(side_effect=AssertionError("USB power cycle")))
    monkeypatch.setattr(flash,"ports_now",lambda:[port()])
    return a


def test_wizard_reserves_io_before_boot_buttons_and_keeps_failure_reserved(app, monkeypatch):
    app._start_flash_esp32()
    assert app._flash_io_active and app._flash_screen
    app.serial.close.assert_called_once()
    app._send("version"); app._send("wardrive_keepalive s"); app._poll_serial()
    assert not app._try_reconnect_esp32()
    app.serial.send_command.assert_not_called(); app.serial.read_available.assert_not_called()
    app._flash_firmware = Mock(side_effect=RuntimeError("failed"))
    with pytest.raises(RuntimeError):
        app._flash_do("xiao")
    assert app._flash_io_active and not app._flash_running
    import watchdogs.app as appmod
    px = appmod.pyxel
    monkeypatch.setattr(px,"btnp",lambda k:k==px.KEY_ESCAPE)
    app._update_flash_screen()
    assert not app._flash_io_active and not app._flash_screen


def test_esc_hides_running_flash_without_releasing_io(app, monkeypatch):
    import watchdogs.app as appmod
    app._start_flash_esp32(); app._flash_running = True
    px = appmod.pyxel; monkeypatch.setattr(px,"btnp",lambda k:k==px.KEY_ESCAPE)
    app._update_flash_screen()
    assert app._flash_io_active and not app._flash_screen
    app._flash_firmware = Mock()
    app._flash_do("xiao")
    assert not app._flash_io_active and not app._flash_running


def test_verified_flash_uses_live_renumbered_device_and_keeps_full_log(app, tmp_path, monkeypatch):
    from watchdogs import updates
    app._start_flash_esp32(); app._flash_manual = True
    monkeypatch.setattr(updates,"prepare_firmware",lambda *a,**kw:("v1.7.4",tmp_path))
    monkeypatch.setattr(flash,"ensure_esptool",lambda *a:("/venv/python","5.4.0"))
    monkeypatch.setattr(flash,"ports_now",lambda:[port(serial="OTHER"),port("/dev/ttyACM2")])
    def run(command, report):
        assert app._flash_io_active
        assert command[command.index("--port")+1] == "/dev/ttyACM2"
        assert command[command.index("--before")+1] == "no-reset"
        report("Hash of data verified.")
    monkeypatch.setattr(flash,"run_flash",run)
    monkeypatch.setattr(flash.time,"sleep",Mock())
    app._flash_do("xiao")
    assert app._flash_io_active and not app._flash_running
    assert app._boot_serial_port == "/dev/ttyACM2" and not app._fw_version
    assert "Hash of data verified." in app._flash_log_path.read_text()
    assert "esptool 5.4.0" in app._flash_log_path.read_text()


def test_bad_download_does_not_install_tools_or_invoke_flasher(app, monkeypatch):
    from watchdogs import updates
    app._start_flash_esp32()
    monkeypatch.setattr(updates,"prepare_firmware",Mock(side_effect=ValueError("checksum mismatch")))
    setup = Mock(); write = Mock()
    monkeypatch.setattr(flash,"ensure_esptool",setup); monkeypatch.setattr(flash,"run_flash",write)
    app._flash_do("xiao")
    setup.assert_not_called(); write.assert_not_called()
    assert "checksum mismatch" in app._flash_log_path.read_text()
    assert app._flash_io_active


def test_post_flash_reconnect_follows_same_board(app, monkeypatch):
    import watchdogs.app as appmod
    app._reconnect_flash_target = flash.FlashTarget.from_port(port())
    monkeypatch.setattr(flash,"ports_now",lambda:[port(serial="OTHER"),port("/dev/ttyACM2")])
    manager = Mock(); monkeypatch.setattr(appmod,"SerialManager",manager)
    assert app._try_reconnect_esp32()
    manager.assert_called_once_with("/dev/ttyACM2")
    assert app._reconnect_flash_target is None


def test_version_picker_selects_rollback_and_passes_exact_release(app, tmp_path, monkeypatch):
    from watchdogs import updates
    old = dict(tag_name='v1.7.3')
    app._flash_releases = [dict(tag_name='v1.7.4'),old]
    app._start_flash_esp32()
    app._flash_cycle_version(1); app._flash_cycle_version(1)
    assert app._flash_release_tag == 'v1.7.3'
    prepare = Mock(return_value=('v1.7.3',tmp_path))
    monkeypatch.setattr(updates,'prepare_firmware',prepare)
    monkeypatch.setattr(flash,'ensure_esptool',lambda *a:('python','5.4.0'))
    monkeypatch.setattr(flash,'run_flash',Mock())
    monkeypatch.setattr(flash.time,'sleep',Mock())
    app._flash_do('xiao')
    assert prepare.call_args.kwargs['release'] is old
    assert 'Requested firmware: v1.7.3' in app._flash_log_path.read_text()
    app._flash_cycle_version(-1)
    assert app._flash_release_tag == 'v1.7.4'


def test_missing_selected_version_fails_without_latest_fallback(app, monkeypatch):
    from watchdogs import updates
    app._start_flash_esp32(); app._flash_release_tag='v1.0.0'
    prepare = Mock(); monkeypatch.setattr(updates,'prepare_firmware',prepare)
    app._flash_do('xiao')
    prepare.assert_not_called()
    assert 'Selected firmware version is unavailable' in app._flash_log_path.read_text()


def test_preferred_path_must_have_recognized_usb_identity():
    unrelated = port('/dev/ttyACM2', vid=0x2c7c, pid=0x0125, serial='MODEM')
    with pytest.raises(RuntimeError, match='not a recognized'):
        flash.select_target('/dev/ttyACM2', [unrelated, port()])
    with pytest.raises(RuntimeError, match='Previous ESP32 port is missing'):
        flash.select_target('/dev/ttyACM0', [port('/dev/ttyACM2', serial='DIFFERENT')])
    with pytest.raises(RuntimeError, match='identity unavailable'):
        flash.select_target(None, [port(serial=None, location=None)])


def test_native_esp_is_preferred_to_generic_uart_bridge():
    bridge = port('/dev/ttyACM0', vid=0x1a86, pid=0x7523, serial='UART')
    native = port('/dev/ttyACM2')
    assert flash.select_target(None, [bridge, native]).device == native.device
    # An explicit recognized connection still wins, for external UART boards.
    assert flash.select_target(bridge.device, [bridge, native]).device == bridge.device


def test_no_target_cannot_silently_choose_new_usb_device(monkeypatch):
    scan = Mock(return_value=[port()]); monkeypatch.setattr(flash, 'ports_now', scan)
    with pytest.raises(RuntimeError, match='No ESP32 selected'):
        flash.wait_for_target(None, timeout=0)
    scan.assert_not_called()


def test_duplicate_serial_number_uses_usb_location():
    original = port(serial='0001', location='1-2')
    target = flash.FlashTarget.from_port(original)
    assert target.resolve([port('/dev/ttyACM1', serial='0001', location='1-3'),
                           port('/dev/ttyACM2', serial='0001', location='1-2')]) == '/dev/ttyACM2'


def test_wizard_uses_connection_identity_when_old_path_is_reused(app, monkeypatch):
    app.serial.usb_port_info = port()
    monkeypatch.setattr(flash, 'ports_now', lambda:[
        port('/dev/ttyACM0', vid=0x2c7c, pid=0x0125, serial='MODEM'), port('/dev/ttyACM2')])
    app._start_flash_esp32()
    assert app._flash_target.serial_number == 'ESP-ONE'
    assert app._flash_target_label == '/dev/ttyACM2'
    assert app._flash_target.resolve() == '/dev/ttyACM2'


def test_missing_wizard_target_blocks_enter_until_explicit_refresh(app, monkeypatch):
    import watchdogs.app as appmod
    monkeypatch.setattr(flash, 'ports_now', lambda:[port(vid=0x9999, pid=1)])
    app._start_flash_esp32(); assert app._flash_target is None
    worker = Mock(); monkeypatch.setattr(appmod.threading, 'Thread', worker)
    monkeypatch.setattr(appmod.pyxel, 'btnp', lambda k:k==appmod.pyxel.KEY_RETURN)
    app._update_flash_screen(); worker.assert_not_called()
    assert not app._flash_running
    monkeypatch.setattr(flash, 'ports_now', lambda:[port('/dev/ttyACM2')])
    monkeypatch.setattr(appmod.pyxel, 'btnp', lambda k:k==appmod.pyxel.KEY_R)
    app._update_flash_screen()
    assert app._flash_target_label == '/dev/ttyACM2'
    assert app._flash_target.serial_number == 'ESP-ONE'


def test_refresh_does_not_switch_a_bound_target_to_another_esp(app, monkeypatch):
    app._start_flash_esp32(); original=app._flash_target
    monkeypatch.setattr(flash, 'ports_now', lambda:[port(serial='OTHER')])
    app._flash_refresh_target()
    assert app._flash_target == original
    assert 'missing or ambiguous' in app._flash_target_label


def test_serial_autodetection_does_not_use_tty_number_or_first_candidate(monkeypatch):
    from watchdogs import serial_manager as sm
    monkeypatch.setattr(sm.os.path, 'exists', lambda _:True)
    readings = iter([[], [port(vid=0x9999, pid=1)],
                     [port(),port('/dev/ttyACM2',serial='OTHER')],
                     [port('/dev/ttyUSB0',vid=0x10c4,pid=0xea60),port('/dev/ttyACM2')]])
    monkeypatch.setattr(sm.serial.tools.list_ports, 'comports', lambda:next(readings))
    assert sm.detect_esp32_port() is None
    assert sm.detect_esp32_port() is None
    assert sm.detect_esp32_port() is None
    assert sm.detect_esp32_port() == '/dev/ttyACM2'


def test_serial_connection_retains_usb_identity_without_probing_other_ports(monkeypatch):
    from watchdogs import serial_manager as sm
    original=port(); other=port('/dev/ttyACM2', serial='GPS')
    monkeypatch.setattr(sm.os.path,'exists',lambda _:True)
    monkeypatch.setattr(sm.os,'access',lambda *a:True)
    monkeypatch.setattr(sm.serial.tools.list_ports,'comports',lambda:[original,other])
    serial_open=Mock();monkeypatch.setattr(sm.serial,'Serial',serial_open)
    connection=sm.SerialManager(original.device);connection.setup();connection.close()
    assert connection.usb_port_info is original
    assert serial_open.call_count==1
    assert serial_open.call_args.kwargs['port']==original.device
