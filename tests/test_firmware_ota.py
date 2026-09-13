"""Scripted firmware conversations; no real serial ports or network writes."""
from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest

from watchdogs.firmware_ota import (OtaRunner, SerialOtaTransport, OtaResult,
                                   validate_release, wifi_command)
from watchdogs.app import WatchDogsGame


def release(tag='v1.7.5'):
    return {'tag_name': tag, 'assets': [
        {'name': name, 'browser_download_url':
         f'https://github.com/Smethan/projectZero/releases/download/{tag}/{name}'}
        for name in ('projectZerobyLOCOSP.bin', 'projectZerobyLOCOSP-xiao.bin')]}


def info(slot='ota_0', state=2):
    other = 'ota_1' if slot == 'ota_0' else 'ota_0'
    return [f'OTA boot: {slot}', f'OTA running: {slot} state={state}', f'OTA next: {other}',
            'APP[0]: ota_0 state=2 ver=1.7.3', 'APP[1]: ota_1 state=-1']


class Fake:
    def __init__(self, wifi=True, applied=True, target=True):
        self.commands = []
        self.queue = []
        self.now = 0
        self.reboots = 0
        self.wifi, self.applied, self.target = wifi, applied, target
        self.capability = True
        self.slots = info()
        self.disconnect = False

    def send(self, cmd):
        self.commands.append(cmd)
        if cmd == 'stop':
            self.queue = ['All operations stopped.']
        elif cmd == 'version':
            self.queue = ['JanOS version: ' + ('1.7.5' if self.reboots and self.target else '1.7.3')]
        elif cmd == 'get_capabilities':
            self.queue = ['WDG:{"kind":"capabilities","wardrive_serial_v1":true}'] if self.capability else []
        elif cmd == 'ota_info':
            self.queue = info('ota_1') if self.reboots and self.target else self.slots
        elif cmd.startswith('wifi_connect'):
            # Echo contains the password: never emit it through report/result.
            self.queue = [cmd, 'DHCP IP: 192.168.0.10, Netmask: 255.255.255.0' if self.wifi
                          else "FAILED: Connection to 'home' failed (reason=202). Check SSID/password and signal."]
        elif cmd.startswith('ota_check'):
            if self.disconnect:
                raise OSError('secret connection failure')
            self.queue = (['OTA: current=1.7.3, target=v1.7.5', 'OTA: progress 50% (500/1000 bytes)',
                           'OTA: update applied, restarting'] if self.applied else
                          ['OTA: update failed: ESP_FAIL'])

    def read(self):
        lines, self.queue = self.queue, []
        return lines

    def sleep(self, duration):
        self.now += duration

    def reconnect(self):
        self.reboots += 1


def run(fake, **kwargs):
    report = Mock()
    runner = OtaRunner(fake, report, clock=lambda: fake.now, sleep=fake.sleep)
    return runner.run(release(), **kwargs), report


def test_ota_download_verifies_version_and_new_valid_slot():
    fake = Fake()
    result, report = run(fake, ssid='home network', password='special "pass\\word')
    assert result.state == 'success' and result.version == '1.7.5'
    assert fake.commands[:4] == ['stop', 'version', 'get_capabilities', 'ota_info']
    assert fake.commands.count('ota_check v1.7.5') == 1
    assert ('Downloading firmware over Wi-Fi: 50%', 50) in [c.args for c in report.call_args_list]
    assert 'pass' not in str(report.call_args_list) and 'pass' not in result.message


def test_wifi_rejected_never_requests_update_or_logs_credentials():
    fake = Fake(wifi=False)
    result, report = run(fake, ssid='home', password='secr3tpass')
    assert result.state == 'failed'
    assert not any(c.startswith('ota_check') for c in fake.commands)
    assert 'secr3tpass' not in str(report.call_args_list) + result.message


def test_missing_fork_or_slots_stops_before_network_and_update():
    fake = Fake()
    fake.capability = False
    result, _ = run(fake)
    assert result.state == 'failed'
    assert not any(c.startswith('ota_check') for c in fake.commands)
    fake = Fake()
    fake.slots = info()[:-1] + ['APP[1]: ota_1 missing']
    result, _ = run(fake)
    assert result.state == 'failed'
    assert not any(c.startswith('ota_check') for c in fake.commands)


def test_explicit_failure_does_not_claim_success_or_retry():
    fake = Fake(applied=False)
    result, _ = run(fake)
    assert result.state == 'failed'
    assert fake.reboots == 0
    assert fake.commands.count('ota_check v1.7.5') == 1


def test_rollback_or_old_version_is_unconfirmed():
    fake = Fake(target=False)
    result, _ = run(fake)
    assert result.state == 'unconfirmed'
    assert 'Could not verify' in result.message
    assert fake.commands.count('ota_check v1.7.5') == 1


def test_write_failure_is_unconfirmed_and_never_exposes_exception_or_retries():
    fake = Fake()
    fake.disconnect = True
    result, _ = run(fake)
    assert result.state == 'unconfirmed'
    assert 'secret' not in result.message
    assert fake.commands.count('ota_check v1.7.5') == 1


def test_stalled_update_can_be_verified_without_last_serial_message():
    fake = Fake()
    original_send = fake.send
    def send(cmd):
        original_send(cmd)
        if cmd.startswith('ota_check'):
            fake.queue = []
    fake.send = send
    result, _ = run(fake)
    assert result.state == 'success' and fake.now >= 300


def test_matching_version_in_wrong_slot_or_pending_state_not_success():
    for bad_info in (info('ota_0'), info('ota_1', state=1)):
        fake = Fake()
        original_send = fake.send
        def send(cmd):
            original_send(cmd)
            if fake.reboots and cmd == 'ota_info':
                fake.queue = bad_info
        fake.send = send
        result, _ = run(fake)
        assert result.state == 'unconfirmed'


def test_current_version_checks_slots_but_does_not_connect_wifi_or_update():
    fake = Fake()
    runner = OtaRunner(fake, Mock(), clock=lambda: fake.now, sleep=fake.sleep)
    result = runner.run(release('v1.7.3'), 'home', 'secret123')
    assert result.state == 'current'
    assert fake.commands == ['stop', 'version', 'get_capabilities', 'ota_info']


@pytest.mark.parametrize('ssid,password', [('bad\r\nreboot', ''), ('x', 'pw'), ('x'*32, ''),
                                         ('é'*16, ''), ('x', 'p'*64), ('', 'password')])
def test_credentials_reject_injection_and_firmware_truncation(ssid, password):
    with pytest.raises(ValueError):
        wifi_command(ssid, password)


def test_console_escaping_and_blank_network():
    assert wifi_command('', '') is None
    assert wifi_command('open home', '') == 'wifi_connect "open home"'
    assert wifi_command('a "b\\c', '12345678') == 'wifi_connect "a \\"b\\\\c" "12345678"'
    assert wifi_command(' trailing ', ' password ') == 'wifi_connect " trailing " " password "'


def test_release_requires_fork_assets_and_supported_version():
    bad = release()
    bad['assets'][0]['browser_download_url'] = 'https://github.com/evil/repo/file'
    for data in (bad, release('v1.7.0'), dict(release(), prerelease=True)):
        with pytest.raises(ValueError):
            validate_release(data)


def test_transport_never_flushes_or_uses_normal_logging_send():
    manager = Mock()
    manager.serial_conn.write.side_effect = len
    target = Mock()
    transport = SerialOtaTransport(manager, target)
    transport.send('wifi_connect "home" "password"')
    manager.serial_conn.flush.assert_not_called()
    manager.send_command.assert_not_called()
    transport.close()
    manager.serial_conn.reset_output_buffer.assert_called_once()
    manager.close.assert_called_once()


def test_reconnect_refuses_other_device(monkeypatch):
    manager = Mock()
    target = Mock()
    target.resolve.side_effect = RuntimeError('Missing intended board')
    from watchdogs import serial_manager
    constructor = Mock()
    monkeypatch.setattr(serial_manager, 'SerialManager', constructor)
    with pytest.raises(RuntimeError):
        SerialOtaTransport(manager, target).reconnect()
    constructor.assert_not_called()


def test_ota_reservation_blocks_flash_power_and_other_commands():
    app = WatchDogsGame.__new__(WatchDogsGame)
    app._ota_reserved = app._flash_io_active = True
    app.msg = Mock()
    app.serial = Mock()
    app._start_flash_esp32()
    app._toggle_usb()
    app._execute_item('reboot', '_reboot', 'Reboot', [])
    app.serial.close.assert_not_called()
    app.serial.send_command.assert_not_called()
    assert app.msg.call_count == 3


def test_ui_transfers_serial_ownership_clears_password_and_retains_reservation(monkeypatch):
    from watchdogs import firmware_flash, ota_ui
    from queue import Queue
    device = NS(device='/dev/ttyACM0', vid=0x303a, pid=0x1001,
                serial_number='ESP', location='1-2')
    monkeypatch.setattr(firmware_flash, 'ports_now', lambda: [device])
    scheduled = []
    monkeypatch.setattr(ota_ui.threading, 'Thread', lambda target, **kw: NS(start=lambda: scheduled.append(target)))
    runner = Mock()
    runner.run.return_value = OtaResult('success', 'Verified v1.7.5', '1.7.5')
    monkeypatch.setattr(ota_ui, 'OtaRunner', Mock(return_value=runner))
    app = WatchDogsGame.__new__(WatchDogsGame)
    app._ota_running = False
    app._ota_result = None
    app._ota_releases = [release()]
    app._ota_selection = 0
    app._ota_fields = ['home', 'secret123']
    app._ota_events = Queue()
    app.wardrive = Mock()
    app.serial = Mock(device=device.device, usb_port_info=device, is_open=True)
    app.state = NS(connected=True)
    app._term_add = Mock()
    manager = app.serial
    app._start_ota()
    assert app.serial is None and app._ota_reserved and app._flash_io_active
    assert not app.state.connected and app._ota_fields[1] == ''
    app._term_add.assert_not_called()
    assert len(scheduled) == 1
    scheduled[0]()
    manager.close.assert_called_once()
    monkeypatch.setattr(ota_ui.pyxel, 'btnp', lambda *args: False)
    app._update_ota_screen()
    assert not app._ota_running and app._ota_result.state == 'success'
    assert app._ota_reserved and app._flash_io_active
    assert 'secret123' not in str(app._term_add.call_args_list)
    app._try_reconnect_esp32 = Mock()
    monkeypatch.setattr(ota_ui.pyxel, 'btnp', lambda key, *args: key == ota_ui.pyxel.KEY_ESCAPE)
    app._update_ota_screen()
    assert not app._ota_reserved and not app._flash_io_active
    app._try_reconnect_esp32.assert_called_once()


def test_escape_cannot_release_ownership_during_update(monkeypatch):
    from watchdogs import ota_ui
    from queue import Queue
    app = WatchDogsGame.__new__(WatchDogsGame)
    app._ota_events = Queue()
    app._ota_running = app._ota_screen = app._ota_reserved = app._flash_io_active = True
    monkeypatch.setattr(ota_ui.pyxel, 'btnp', lambda key, *args: key == ota_ui.pyxel.KEY_ESCAPE)
    app._update_ota_screen()
    assert app._ota_screen and app._ota_reserved and app._flash_io_active


def test_no_stop_reply_never_sends_update():
    fake = Fake()
    fake.send = lambda cmd: fake.commands.append(cmd)
    result, _ = run(fake)
    assert result.state == 'failed'
    assert fake.commands == ['stop']


def test_firmware_171_project_name_bug_requires_usb_upgrade():
    fake = Fake()
    original = fake.send
    def send(cmd):
        original(cmd)
        if cmd == 'version':
            fake.queue = ['JanOS version: 1.7.1']
    fake.send = send
    result, _ = run(fake)
    assert result.state == 'failed'
    assert fake.commands == ['stop', 'version']
    with pytest.raises(ValueError):
        validate_release(release('v1.7.1'))
