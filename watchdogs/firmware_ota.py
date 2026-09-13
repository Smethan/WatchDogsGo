"""Application-mode OTA over Wi-Fi; USB carries only commands and status.

Only curated messages leave this module. Firmware echoes and Wi-Fi credentials
must never enter WDG's terminal/loot logs. No erase, ROM reset or image USB write.
"""
from collections import deque
from dataclasses import dataclass
import json
import re
import time

from .updates import asset_url, release_version

MIN_VERSION = (1, 7, 2)
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def wifi_command(ssid, password):
    # Existing firmware uses strncpy(size - 1), so reject values it truncates.
    for value in (ssid, password):
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Wi-Fi fields cannot contain control characters')
    if len(ssid.encode('utf-8')) > 31:
        raise ValueError('This firmware supports SSIDs up to 31 UTF-8 bytes')
    if password and not 8 <= len(password.encode('utf-8')) <= 63:
        raise ValueError('Use a Wi-Fi password of 8-63 bytes, or blank for open Wi-Fi')
    if not ssid:
        if password:
            raise ValueError('Enter an SSID with the password')
        return None  # Use an already-connected ESP32 network.
    def quote(value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return 'wifi_connect ' + quote(ssid) + (' ' + quote(password) if password else '')


def validate_release(release):
    tag = release['tag_name']
    if release.get('draft') or release.get('prerelease') or release_version(tag) < MIN_VERSION:
        raise ValueError('OTA requires a published Smethan firmware release v1.7.2 or newer')
    for asset in ('projectZerobyLOCOSP.bin', 'projectZerobyLOCOSP-xiao.bin'):
        asset_url(release, asset)
    return tag


@dataclass(frozen=True)
class OtaResult:
    state: str  # success / current / failed / unconfirmed
    message: str
    version: str = ''


class OtaError(Exception):
    pass


class SerialOtaTransport:
    """Exclusive serial owner, bound to the original USB identity after reboot."""
    def __init__(self, manager, target):
        self.manager = manager
        self.target = target

    def send(self, command):
        payload = (command + '\r\n').encode('utf-8')
        conn = self.manager.serial_conn
        conn.write_timeout = 2
        # flush() can wait indefinitely on the failing uConsole USB path.
        if conn.write(payload) != len(payload):
            raise OSError('Incomplete command write')

    def read(self):
        return self.manager.read_available()

    def close(self):
        if self.manager and self.manager.serial_conn:
            try:
                self.manager.serial_conn.reset_output_buffer()
            except Exception:
                pass
            self.manager.close()

    def reconnect(self):
        from .serial_manager import SerialManager
        self.close()
        port = self.target.resolve()  # Never fall back to another USB device.
        self.manager = SerialManager(port)
        self.manager.setup()


class OtaRunner:
    def __init__(self, transport, report, clock=time.monotonic, sleep=time.sleep):
        self.transport = transport
        self.report = report
        self.clock, self.sleep = clock, sleep
        self.pending = deque()
        self.requested = False

    def send(self, command):
        self.pending.clear()
        self.transport.send(command)

    def line(self):
        if not self.pending:
            self.pending.extend(self.transport.read())
        if not self.pending:
            self.sleep(0.05)
            return ''
        return ANSI.sub('', self.pending.popleft()).strip()

    def wait(self, predicate, timeout, failure):
        end = self.clock() + timeout
        while self.clock() < end:
            line = self.line()
            result = predicate(line)
            if result:
                return result
        raise OtaError(failure)

    def version(self):
        self.send('version')
        def parse(line):
            m = re.fullmatch(r'(?:JanOS|WatchDogsGo) version: v?(\d+\.\d+\.\d+)', line)
            return m.group(1) if m else None
        return self.wait(parse, 8, 'Firmware did not answer. Boot normally with BOOT released, then retry.')

    def info(self):
        self.send('ota_info')
        info = {'parts': set()}
        def parse(line):
            m = re.fullmatch(r'OTA (boot|next): (ota_[01])', line)
            if m:
                info[m[1]] = m[2]
            m = re.fullmatch(r'OTA running: (ota_[01]) state=(-?\d+)', line)
            if m:
                info['running'], info['state'] = m[1], int(m[2])
            m = re.fullmatch(r'APP\[[01]\]: (ota_[01]) state=-?\d+(?: ver=\S+)?', line)
            if m:
                info['parts'].add(m[1])
            return info if len(info) == 5 and len(info['parts']) == 2 else None
        return self.wait(parse, 8, 'Two OTA slots were not confirmed. Install the full fork bundle by USB first.')

    def run(self, release, ssid='', password=''):
        try:
            tag = validate_release(release)
            connection_command = wifi_command(ssid, password)
            password = ''
            self.report('Stopping scans and checking firmware...', None)
            self.send('stop')
            self.wait(lambda s: s == 'All operations stopped.', 30,
                      'ESP32 did not confirm stop. No update requested; reset normally and retry.')
            current = self.version()
            if release_version(current) < MIN_VERSION:
                raise OtaError('Install Smethan projectZero v1.7.2+ with the full USB bundle first.')
            self.send('get_capabilities')
            def capabilities(line):
                if not line.startswith('WDG:'):
                    return False
                try:
                    data = json.loads(line[4:])
                    return data.get('kind') == 'capabilities' and data.get('wardrive_serial_v1') is True
                except (ValueError, AttributeError):
                    return False
            self.wait(capabilities, 8, 'Expected fork capabilities missing. Use Smethan projectZero v1.7.2+.')
            before = self.info()
            if before['boot'] != before['running'] or before['next'] == before['running'] or before['state'] != 2:
                raise OtaError('Current OTA slot is not stable/valid. Reboot normally and check firmware first.')
            if release_version(current) == release_version(tag):
                return OtaResult('current', 'Already running the selected version; no update needed.', current)
            if connection_command:
                self.report('Connecting ESP32 to Wi-Fi (up to 30 seconds)...', None)
                self.send(connection_command)
                connection_command = None
                def connected(line):
                    if line.startswith(('FAILED: Connection', 'TIMEOUT: Connection',
                                        'Failed to reinitialize WiFi', 'STA interface not found')):
                        raise OtaError('ESP32 could not join Wi-Fi. Check SSID, password and signal.')
                    return bool(re.match(r'(?:DHCP|Static) IP: (?!0\.0\.0\.0)\d+\.\d+\.\d+\.\d+', line))
                self.wait(connected, 30, 'ESP32 has no confirmed IP address. Check Wi-Fi and retry.')
            self.report('Requesting ' + tag + ' from GitHub over ESP32 Wi-Fi...', None)
            self.requested = True  # A write error can still mean the command reached the board.
            self.send('ota_check ' + tag)
            def progress(line):
                if line.startswith(('OTA: failed', 'OTA: update failed', 'OTA: not connected',
                                    'OTA: WiFi not ready', 'OTA: out of memory', 'OTA: config not set')):
                    raise OtaError('Firmware reported an OTA failure. Check ESP32 internet access and retry.')
                if line == 'OTA: check already in progress':
                    raise OtaError('Another OTA is already running; leave power connected and check later.')
                m = re.match(r'OTA: progress (\d+)%', line)
                if m:
                    pct = min(100, int(m[1]))
                    self.report(f'Downloading firmware over Wi-Fi: {pct}%', pct)
                return line == 'OTA: update applied, restarting'
            try:
                self.wait(progress, 300, 'Update result not received')
            except OtaError as exc:
                # Explicit failure is reliable; timeout is not evidence of failure.
                if str(exc).startswith('Firmware reported'):
                    return OtaResult('failed', str(exc))
                if str(exc).startswith('Another OTA'):
                    return OtaResult('unconfirmed', str(exc))
            except (OSError, AttributeError):
                pass  # A reboot can disconnect USB before its last message arrives.
            return self.verify(tag, before['next'])
        except OtaError as exc:
            return OtaResult('unconfirmed' if self.requested else 'failed', str(exc))
        except Exception:
            # Exception text could contain a credential command; never forward it.
            return OtaResult('unconfirmed' if self.requested else 'failed',
                             'USB communication failed. ' + ('Update may still be running; keep power on.'
                             if self.requested else 'No update requested. Boot normally and check USB.'))

    def verify(self, tag, expected_slot):
        self.report('Waiting for reboot; checking version and valid OTA slot...', None)
        self.sleep(5)
        end = self.clock() + 90
        while self.clock() < end:
            try:
                self.transport.reconnect()
                current = self.version()
                info = self.info()
                if (release_version(current) == release_version(tag)
                        and info['running'] == expected_slot and info['boot'] == expected_slot
                        and info['state'] == 2):
                    return OtaResult('success', 'Verified ' + tag + ' after reboot (valid OTA slot).', current)
            except (OSError, OtaError, RuntimeError, AttributeError):
                pass
            self.sleep(3)
        return OtaResult('unconfirmed',
                         'Could not verify the new version/slot. Keep power on; check the ESP32 before retrying.')
