"""Bounded network scan snapshots and optional BSSID selection for HS Capture."""
import json
import re
import secrets
import time
from .wardrive_protocol import MAC, TOKEN, integer, display_bytes

MAX_TARGETS = 16
MAX_EXCLUSIONS = 32
MAX_NETWORKS = 64
ERRORS = {
    'scan_expired': 'Scan changed or expired. Scan and select networks again.',
    'invalid_targets': 'Invalid target selection; scan and select again.',
    'target_unavailable': 'A selected network is unavailable or has no WPA handshake.',
    'target_channel': 'A selected channel is not supported by this firmware.',
    'sd_required': 'HS Capture requires an SD card on the ESP32. Use HS Capture no SD.',
    'busy': 'ESP32 is busy; stop the current operation and retry.',
    'start_failed': 'ESP32 could not start capture. Check the console and retry.',
}

def parse_target_record(line):
    try:
        if not line.startswith('HST:') or len(line.encode()) > 512:
            return None
        d = json.loads(line[4:])
        if not isinstance(d, dict) or type(d.get('v')) is not int or d['v'] != 1:
            return None
        kind = d.get('kind')
        if kind == 'capture_error':
            if d.get('storage') not in ('sd', 'serial') or d.get('error') not in ERRORS:
                return None
            return d
        if kind not in ('scan_started', 'ap', 'scan_done', 'scan_error'):
            return None
        if not isinstance(d.get('scan'), str) or not TOKEN.fullmatch(d['scan']):
            return None
        if kind == 'ap':
            integer(d, 'seq', 1, MAX_NETWORKS)
            integer(d, 'channel', 1, 196)
            integer(d, 'rssi', -127, 20)
            integer(d, 'auth', 0, 255)
            if not isinstance(d.get('bssid'), str) or not MAC.fullmatch(d['bssid']):
                return None
            raw = bytes.fromhex(d['bssid'].replace(':', ''))
            if raw[0] & 1 or not any(raw):
                return None
            if not isinstance(d.get('ssid_hex'), str) or not re.fullmatch(r'(?:[0-9a-fA-F]{2}){0,32}', d['ssid_hex']):
                return None
            d['bssid'] = d['bssid'].upper()
            d['name'] = display_bytes(d['ssid_hex']) or '<hidden>'
        elif kind == 'scan_done':
            integer(d, 'count', 0, MAX_NETWORKS)
        return d
    except (ValueError, TypeError, KeyError, RecursionError):
        return None


class HandshakeTargets:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.choices = {'sd': None, 'serial': None}
        self.token = ''
        self.state = 'idle'
        self.rows = {}
        self.seq = 0
        self.started = False
        self.deadline = self.completed = 0
        self.error = ''
        self.query = ''
        self.min_rssi = None
        self.sort_name = False
        self.draft = set()

    @property
    def busy(self):
        return self.state in ('waiting', 'scanning')

    def prepare_scan(self):
        self.token = secrets.token_hex(8)
        self.rows.clear(); self.draft.clear()
        self.seq = 0; self.started = False
        self.state = 'waiting'; self.error = ''
        self.deadline = self.clock() + 15
        return 'hs_scan ' + self.token

    def dispatched(self, command):
        if command == 'hs_scan ' + self.token and self.state == 'waiting':
            self.state = 'scanning'
            self.deadline = self.clock() + 60
            return True
        return False

    def cancel(self, message='Scan cancelled. Press R to scan again.'):
        if self.busy:
            self.state = 'error'; self.error = message

    def disconnect(self):
        # Retain selected mode as stale, never silently switch it to ALL.
        self.state = 'error'; self.token = ''; self.rows.clear(); self.draft.clear()
        self.error = 'ESP32 connection changed. Scan again before selecting targets.'

    def tick(self):
        if self.busy and self.clock() >= self.deadline:
            self.cancel('Scan timed out or stop was not confirmed. Press R to retry.')
            return True
        return False

    def accept(self, d):
        if not d or d.get('scan') != self.token or self.state != 'scanning':
            return
        if d['kind'] == 'scan_started':
            self.started = True
        elif d['kind'] == 'scan_error':
            self.cancel('ESP32 scan failed or was cancelled. Press R to retry.')
        elif d['kind'] == 'ap':
            if not self.started or d['seq'] != self.seq + 1 or d['bssid'] in self.rows:
                self.cancel('Scan results were incomplete. Press R to rescan.'); return
            self.rows[d['bssid']] = d
            self.seq = d['seq']
        elif d['kind'] == 'scan_done':
            if not self.started or d['count'] != self.seq:
                self.cancel('Scan results were incomplete. Press R to rescan.'); return
            self.state = 'ready'; self.completed = self.clock()

    def visible(self):
        rows = [d for d in self.rows.values() if self.query.casefold() in d['name'].casefold()
                and (self.min_rssi is None or d['rssi'] >= self.min_rssi)]
        return sorted(rows, key=(lambda d: (d['name'].casefold(), -d['rssi'], d['bssid']))
                      if self.sort_name else (lambda d: (-d['rssi'], d['name'].casefold(), d['bssid'])))

    def toggle(self, bssid, blocked=lambda _: False):
        if self.state != 'ready':
            raise ValueError('Wait for a complete scan before selecting networks.')
        if bssid in self.draft:
            self.draft.remove(bssid); return
        row = self.rows[bssid]
        if blocked(bssid):
            raise ValueError('This network is whitelisted.')
        if row['auth'] in (0, 1):
            raise ValueError('Open/WEP networks have no WPA handshake to capture.')
        if len(self.draft) >= MAX_TARGETS:
            raise ValueError('Select up to 16 networks at once.')
        self.draft.add(bssid)

    def apply(self, storage, blocked=lambda _: False):
        if self.state != 'ready' or self.clock()-self.completed > 300:
            raise ValueError('Scan changed or expired. Press R to scan again.')
        if not self.draft:
            raise ValueError('Select at least one network, or choose A for all nearby.')
        if len(self.draft) > MAX_TARGETS or any(mac not in self.rows for mac in self.draft):
            raise ValueError('Selection changed. Select the networks again.')
        if any(blocked(mac) for mac in self.draft):
            raise ValueError('A selected network is whitelisted; deselect it first.')
        if any(self.rows[mac]['auth'] in (0, 1) for mac in self.draft):
            raise ValueError('Open/WEP networks have no WPA handshake to capture.')
        self.choices[storage] = (self.token, tuple(sorted(self.draft)))

    def command(self, storage, supported, blocked=lambda _: False,
                exclusions_supported=False, excluded=()):
        from .handshake_capture import COMMANDS
        selected = self.choices[storage]
        if selected is None:
            exclusions = []
            for value in excluded:
                mac = str(value).strip().upper()
                try:
                    raw = bytes.fromhex(mac.replace(':', ''))
                except ValueError:
                    raw = b''
                if (not MAC.fullmatch(mac) or len(raw) != 6 or raw[0] & 1
                        or not any(raw)):
                    raise ValueError('The Wi-Fi whitelist contains an invalid BSSID; fix it before all-nearby capture.')
                if mac not in exclusions:
                    exclusions.append(mac)
            if exclusions:
                if supported is not True or exclusions_supported is not True:
                    raise ValueError('All-nearby whitelist protection requires firmware 1.7.11+. Update the ESP32 first.')
                if len(exclusions) > MAX_EXCLUSIONS:
                    raise ValueError('All-nearby capture supports up to 32 whitelisted Wi-Fi BSSIDs.')
                return f'start_handshake_scope {storage} all-except ' + ','.join(exclusions)
            return f'start_handshake_scope {storage} all' if supported is True else COMMANDS[storage]
        if supported is not True:
            raise ValueError('Selected capture requires firmware 1.7.9+. Update the ESP32 first.')
        token, macs = selected
        if self.state != 'ready' or token != self.token or self.clock()-self.completed > 300:
            raise ValueError('Selected networks need a new scan. Open N, then R to rescan.')
        if not 1 <= len(macs) <= MAX_TARGETS or any(mac not in self.rows for mac in macs):
            raise ValueError('Selection changed. Select the networks again.')
        if any(blocked(mac) for mac in macs):
            raise ValueError('A selected network is whitelisted; deselect it first.')
        if any(self.rows[mac]['auth'] in (0, 1) for mac in macs):
            raise ValueError('Open/WEP networks have no WPA handshake to capture.')
        return f'start_handshake_scope {storage} {token} ' + ','.join(macs)

    def label(self, storage):
        choice = self.choices[storage]
        if choice is None:
            return 'ALL NEARBY'
        stale = self.state != 'ready' or choice[0] != self.token or self.clock()-self.completed > 300
        return f'SELECTED: {len(choice[1])}' + (' (rescan needed)' if stale else '')
