"""Application integration for source-isolated USB C5 jobs.

The selected serial endpoint remains a compatibility view. Distributed work
always goes through DeviceSession and JobController instead of that view.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
import hashlib
import math
import os
from pathlib import Path
import queue
import secrets
import shlex
import threading
import time
from types import MappingProxyType

from .c5_jobs import limits_for_operation, validate_job_request
from .device_registry import DeviceRegistry, supported_candidates
from .device_session import DeviceSession
from .job_controller import JobController
from .radio_scheduler import RadioScheduler, CapacityError
from .passive_capture import PassiveCapture, inspect_frame
from .hs_exchange import ExchangeValidator
from .serial_manager import SerialManager
from .sniffer_intel import SnifferIntelCollector, parse_sniffer_record


COMMAND_OPS = {
    'start_wardrive_serial': 'wardrive', 'start_wardrive_batch_serial': 'wardrive',
    'start_wardrive_wifi_serial': 'wardrive', 'start_wardrive_wifi_batch_serial': 'wardrive',
    'scan_networks': 'wifi_scan', 'scan_bt': 'ble_scan', 'scan_airtag': 'airtag_scan',
    'start_sniffer_all': 'sniffer', 'start_sniffer': 'sniffer',
    'start_hs_sniff_serial': 'hs_sniff', 'start_handshake': 'handshake',
    'start_handshake_serial': 'handshake', 'start_handshake_scope': 'handshake',
    'start_deauth': 'deauth', 'sae_overflow': 'sae', 'start_blackout': 'blackout',
    'start_evil_twin': 'evil_twin', 'start_portal': 'portal', 'hs_scan': 'wifi_scan',
}
PASSIVE_OPS = frozenset(('wardrive',))
AUTH = {'OPEN': 0, 'WEP': 1, 'WPA_PSK': 2, 'WPA2_PSK': 3,
        'WPA_WPA2_PSK': 4, 'WPA2_ENTERPRISE': 5, 'WPA3_PSK': 6,
        'WPA2_WPA3_PSK': 7, 'WAPI_PSK': 8, 'OWE': 9}
MAX_TERMINAL_GROUPS = 32


@dataclass
class GroupView:
    identity: str
    operation: str
    state_key: str
    requests: dict
    roles: dict
    tokens: dict
    passive: bool = False
    state: str = 'starting'
    error: str = ''
    restore: dict = field(default_factory=dict)
    stopped: set = field(default_factory=set)
    snapshots: dict = field(default_factory=dict)
    wifi_only: bool = False
    deadline: float = 0.0
    announced: bool = False
    capture_metrics: dict = field(default_factory=dict)
    host_ble: bool = False
    disconnected: set = field(default_factory=set)
    rewarded_exchanges: set = field(default_factory=set)


class ManagedScanView:
    """The existing wardrive screens' view of acknowledged pool coverage."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.error = self.probe_error = ''
        self.mode = 'wardrive'
        self.session = ''
        self.wifi_only = False
        self.last_seen = {}
        self.stats = {}
        self.record_counts = Counter()
        self.seq_gaps = 0
        self.seq = 0
        self.batch = self.legacy = False
        self.batch_number = 0
        self.batch_phase = 'idle'
        self.batch_started = 0
        self.batch_counts = {'wifi': 0, 'ble': 0}
        self.live_oui_enabled = True

    @property
    def state(self):
        group = self.runtime.current
        return group.state if group else 'idle'

    @property
    def active(self):
        return self.state in ('starting', 'running', 'stopping')

    @property
    def gap_percent(self):
        return 100*self.seq_gaps/self.seq if self.seq else 0

    @property
    def supported(self):
        return any('wardrive' in d.capabilities for d in self.runtime.devices())

    wifi_supported = supported
    batch_supported = supported
    wifi_batch_supported = supported
    live_oui_supported = supported
    live_oui_max = 8

    @property
    def hs_supported(self):
        return any('hs_sniff' in d.capabilities for d in self.runtime.devices())

    @property
    def capture_targets_supported(self):
        return any('handshake' in d.capabilities for d in self.runtime.devices())

    capture_exclusions_supported = capture_targets_supported
    capture_pcapng_supported = capture_targets_supported
    target_intel_supported = supported

    @property
    def sniffer_scope_supported(self):
        return any('sniffer' in d.capabilities for d in self.runtime.devices())

    sniffer_intel_supported = sniffer_scope_supported

    def reset(self):
        # A display/selection change does not revoke a radio owner.
        return None

    def probe(self):
        self.runtime.probe()
        return True

    def tick(self):
        # Poll/tick is centrally scheduled once per application frame.
        return None

    def stop(self):
        self.runtime.stop_foreground()

    def accept_plain_stop(self):
        return False

    def handle(self, _record):
        return False  # Only the source session may validate managed records.

    def start(self, mode='wardrive', wifi_only=False, live_ouis=()):
        return self.runtime.start(mode, state_key=mode, wifi_only=wifi_only,
                                  live_ouis=live_ouis)


class MultiDeviceRuntime:
    def __init__(self, app, managers=(), *, ports=(), clock=time.monotonic):
        self.app, self.clock = app, clock
        self.ports = list(dict.fromkeys(ports))
        self._configured_usb = {}
        self.registry = DeviceRegistry()
        self.sessions = {}  # Factory IDs after negotiation.
        self.endpoints = {}  # Each endpoint has exactly one reader from open.
        self.controller = JobController(self.registry, {}, clock=clock, on_event=self._group_event)
        self.scheduler = RadioScheduler()
        self.groups = {}
        self.foreground = None
        self.background = None
        self.scan = ManagedScanView(self)
        self.captures = {}
        self.validators = {}
        self.sniffers = {}
        self.reserved = set()
        # Update ownership is endpoint-scoped because factory IDs are unknown
        # before negotiation and can change while a device reboots.
        self._reserved_sessions = {}
        self._reserved_ids_by_endpoint = {}
        self._reserved_usb_by_endpoint = {}
        self.legacy_owned = set()
        self.legacy_endpoints = set()
        self._portal_preparation = None
        self._next_discovery = clock()+3
        self._round_robin = 0
        for manager in managers:
            self.attach(manager)

    @property
    def available(self):
        return bool(self.devices())

    @property
    def current(self):
        group = self.groups.get(self.foreground) or self.groups.get(self.background)
        return group if group and group.state != 'stopped' else None

    def devices(self):
        return [entry for entry in self.registry.list(connected_only=True)
                if entry.session and entry.session.c5_jobs_v1
                and not entry.session.external_busy
                and not self.is_reserved(entry.session)
                and entry.device_id not in self.legacy_owned]

    def is_reserved(self, session):
        """Return whether this exact endpoint is updater-owned.

        ``reserved`` remains the public factory-ID set used by existing
        scheduler and caller checks. Endpoint claims cover blank IDs and
        identity changes without turning a blank ID into a global sentinel.
        """
        if session is None:
            return False
        return (self._reserved_sessions.get(session.endpoint) is session
                or session.device_id in self.reserved)

    @staticmethod
    def _usb_identity(manager):
        usb = getattr(manager, 'usb_port_info', None)
        return {key: getattr(usb, key, None)
                for key in ('vid', 'pid', 'serial_number', 'location')}

    @staticmethod
    def _same_reserved_usb(identity, candidate):
        if not identity:
            return False
        candidate_identity = candidate.get('usb_identity', {})
        if (identity.get('serial_number') and candidate_identity.get('serial_number')
                and identity['serial_number'] == candidate_identity['serial_number']):
            return (identity.get('vid') in (None, candidate_identity.get('vid'))
                    and identity.get('pid') in (None, candidate_identity.get('pid')))
        return (not identity.get('serial_number') and identity.get('location')
                and identity.get('location') == candidate_identity.get('location')
                and identity.get('vid') in (None, candidate_identity.get('vid'))
                and identity.get('pid') in (None, candidate_identity.get('pid')))

    def attach(self, manager):
        endpoint = manager.device
        if endpoint in self.endpoints:
            return self.endpoints[endpoint]
        session = DeviceSession(manager, endpoint, clock=self.clock)
        session.on_event = lambda record: self._record(session, record)
        session.on_line = lambda line: self._legacy_line(session, line)
        self.endpoints[endpoint] = session
        usb = manager.usb_port_info
        identity = {key:getattr(usb, key, None) for key in ('vid','pid','serial_number','location')}
        if identity['serial_number'] or identity['location']:
            for configured in self.ports:
                if os.path.realpath(configured) == os.path.realpath(endpoint):
                    self._configured_usb[configured] = identity
        if self.app.serial is None:
            self.app.serial = manager
        if self._metadata_probe_allowed(session):
            session.request_metadata_probe()
        return session

    def probe(self):
        for session in self.endpoints.values():
            if self._metadata_probe_allowed(session):
                session.refresh_metadata()

    def _metadata_probe_allowed(self, session):
        return (session.connected and not self.is_reserved(session)
                and session.device_id not in self.legacy_owned
                and not (getattr(self.app, '_serial_busy', False)
                         and session.serial is self.app.serial))

    def discover(self):
        candidates = supported_candidates()
        paths = []
        if self.ports:
            for configured in self.ports:
                identity = self._configured_usb.get(configured)
                if not identity:
                    paths.append(configured)
                    continue
                matches = [item for item in candidates
                    if item['usb_identity']['vid'] == identity['vid']
                    and item['usb_identity']['pid'] == identity['pid']
                    and ((identity['serial_number'] and item['usb_identity']['serial_number'] == identity['serial_number'])
                         or (not identity['serial_number'] and item['usb_identity']['location'] == identity['location']))]
                if len(matches) > 1 and identity['location']:
                    matches = [item for item in matches if item['usb_identity']['location'] == identity['location']]
                if len(matches) == 1:
                    paths.append(matches[0]['endpoint'])
        else:
            paths = [item['endpoint'] for item in candidates]
        for endpoint in dict.fromkeys(paths):
            if endpoint in self.endpoints:
                continue
            candidate = next((item for item in candidates
                if os.path.realpath(item['endpoint']) == os.path.realpath(endpoint)), None)
            if candidate:
                legacy_reserved_identities = [
                    self._usb_identity(session.serial)
                    for session in self.endpoints.values()
                    if session.device_id in self.reserved
                    and self._reserved_sessions.get(session.endpoint) is not session
                ]
                if (any(self._same_reserved_usb(identity, candidate)
                        for identity in self._reserved_usb_by_endpoint.values())
                        or any(self._same_reserved_usb(identity, candidate)
                               for identity in legacy_reserved_identities)):
                    continue
            try:
                manager = SerialManager(endpoint)
                manager.setup()
                self.attach(manager)
                if not self.app.serial:
                    self.app.serial = manager
            except (OSError, ValueError):
                continue

    def poll(self):
        endpoints = list(self.endpoints.values())
        if endpoints:
            start = self._round_robin % len(endpoints)
            for session in endpoints[start:]+endpoints[:start]:
                if self.is_reserved(session):
                    continue
                # Direct legacy transfers exclusively own only their endpoint.
                if (getattr(self.app, '_serial_busy', False)
                        and session.serial is self.app.serial):
                    continue
                try:
                    if not session.connected:
                        raise ConnectionError('endpoint closed')
                    session.poll()
                    if (session.last_poll_drained
                            and self._metadata_probe_allowed(session)):
                        session.request_metadata_probe()
                except (OSError, ConnectionError):
                    self._disconnected(session)
            self._round_robin += 1
        self.controller.tick()
        self._tick_groups()
        self._tick_portal_preparation()
        if self.clock() >= self._next_discovery:
            self._next_discovery = self.clock()+3
            self.discover()
        opened = [s.serial for s in self.endpoints.values()
                  if s.connected and not self.is_reserved(s)]
        if self.app.serial not in opened:
            self.app.serial = opened[0] if opened else None
        self.app._esp32 = self.app.state.connected = bool(opened)
        if not self.available and self.app.wardrive.scan is self.scan:
            self.app.wardrive.scan = self.app.wardrive.legacy_scan

    def _disconnected(self, session):
        did = session.device_id
        self._source_lost(did)
        for group in list(self.groups.values()):
            if did in group.requests and group.state in ('starting','running','stopping'):
                group.disconnected.add(did)
                if len(group.stopped | group.disconnected) == len(group.requests):
                    self._finish(group)
        if self._portal_preparation and did in self._portal_preparation['requests']:
            prep = self._portal_preparation
            prep['cancel'].set()
            self._portal_error(prep, f'{did} disconnected during portal preparation')
        self.registry.disconnect(session.endpoint)
        self.legacy_endpoints.discard(session.endpoint)
        session.disconnect()
        session.serial.close()
        self.endpoints.pop(session.endpoint, None)
        self.sessions.pop(did, None)
        self.controller.sessions.pop(did, None)
        self._close_source(did)
        self.app._term_add(f'[C5] {did or session.endpoint} disconnected', raw=True)

    def _record(self, session, record):
        kind = record['kind']
        did = session.device_id
        if kind in ('boot_changed', 'identity_changed'):
            lost_id = record['device_id']
            self._source_lost(lost_id)
            self._retire_group_member(lost_id, kind.replace('_', ' '), preserve_peers=True)
            self._close_source(lost_id)
            return
        if kind == 'device_info':
            usb = session.serial.usb_port_info
            identity = {key:getattr(usb, key, None) for key in ('vid','pid','serial_number','location')}
            prior = self.registry.by_endpoint(session.endpoint)
            if prior and prior.device_id != did:
                self.controller.device_disconnected(prior.device_id)
                self.registry.disconnect(session.endpoint)
            entry = self.registry.upsert(session.endpoint, identity, record, session=session)
            preferences = self.app.wardrive.settings.get('c5_devices', {}).get(did, {})
            if preferences.get('alias'):
                self.registry.assign_alias(did, preferences['alias'])
            if preferences.get('role'):
                self.registry.assign_role(did, preferences['role'])
            self.sessions[did] = self.controller.sessions[did] = session
            self.app._term_add(f'[C5] {entry.board} {did} on {session.endpoint}', raw=True)
            return
        if kind == 'capabilities':
            # A single legacy endpoint keeps its existing ScanController.
            if record.get('c5_jobs_v1') is True and did:
                self.legacy_endpoints.discard(session.endpoint)
                self.app.wardrive.scan = self.scan
            else:
                self.legacy_endpoints.add(session.endpoint)
                if session.serial is self.app.serial:
                    self.app.wardrive.legacy_scan.handle(record)
            return
        token = record.get('session')
        group = next((g for g in self.groups.values() if g.tokens.get(did) == token), None)
        self.controller.handle_event(did, record,
            compensate_peers=not (group and group.passive and group.announced))
        if not group or record.get('boot_id') != group.requests[did]['boot_id']:
            return
        if group.state in ('stopped','error'):
            return
        if kind == 'error' or record.get('state') == 'error':
            reason = record.get('error', record.get('message', 'device job error'))
            if record.get('state') == 'error' or not session.active:
                self._retire_group_member(did, reason, group=group,
                    preserve_peers=group.passive and group.announced)
            self.scan.stats['drops'] = sum(s.counts.get('drops',0) for s in self.sessions.values())
            return
        source = {'device_id':did, 'boot_id':record['boot_id'], 'job_token':token,
                  'group':group.identity, 'role':group.roles[did]}
        if kind == 'started' or (kind in ('status','heartbeat')
                                and record.get('state') in ('running','reporting')):
            self._open_capture(group, did, source)
        if kind in ('wifi', 'wifi_mgmt', 'ble', 'live_oui'):
            event = dict(record, source_session=token, session=group.identity)
            self.scan.last_seen['ble' if kind == 'ble' else 'wifi'] = self.clock()
            self.scan.record_counts[kind] += 1
            if kind == 'wifi':
                group.snapshots.setdefault(did, {})[record['mac']] = dict(record)
            if kind == 'live_oui':
                self.app.wardrive.live_oui(event)
            else:
                self.app.wardrive.observation(event, terminal=False)
                self._provenance(dict(source, sequence=record['seq'], kind=kind,
                                      mac=record.get('mac'), observed_at=time.time()-record['age_ms']/1000))
        elif kind == 'hs_packet':
            capture = self.captures.get((did, record['boot_id'], token))
            if capture:
                try:
                    capture.accept(record)
                    group.capture_metrics[did] = dict(frames=capture.frames,eapol=capture.eapol,
                        pmkids=capture.pmkids,lost=capture.lost,
                        validated=self.validators[(did,record['boot_id'],token)].complete)
                except OSError as exc:
                    self._storage_failure(group, str(exc))
        elif kind == 'legacy':
            self._job_line(group, did, record['line'])
        elif kind == 'stopped' or record.get('state') == 'stopped':
            group.stopped.add(did)
            self._close_capture((did, record['boot_id'], token))
            if len(group.stopped | group.disconnected) == len(group.requests):
                self._finish(group)
            self._advance_portal_preparation(did, token)
        self.scan.stats['drops'] = sum(s.counts.get('drops',0) for s in self.sessions.values())

    def _retire_group_member(self, device_id, reason, *, group=None, preserve_peers=False):
        """Mark a lost/terminal source done and preserve the peer stop barrier."""
        group = group or next((g for g in self.groups.values()
                               if device_id in g.requests and g.state in
                               ('starting', 'running', 'stopping')), None)
        if group is None or device_id not in group.requests:
            return
        group.error = str(reason)[:180]
        group.restore.clear()
        group.state = (('running' if group.announced else 'starting')
                       if group.passive and preserve_peers else 'stopping')
        group.disconnected.add(device_id)
        self.scan.error = group.error
        self._close_capture((device_id, group.requests[device_id]['boot_id'], group.tokens[device_id]))
        if len(group.stopped | group.disconnected) == len(group.requests):
            self._finish(group)

    def _source_lost(self, device_id):
        owners = [group for group in self.groups.values()
                  if device_id in group.requests and group.state in ('starting','running','stopping')
                  and device_id not in group.stopped | group.disconnected]
        self.controller.device_disconnected(device_id, stop_peers=any(not group.passive for group in owners))

    def _legacy_line(self, session, line):
        # Secondary legacy boards are visible, but their board-local indices
        # and artifacts must never enter the selected endpoint's state machine.
        if session.device_id and (session.job_token or session.device_id in self.legacy_owned):
            return
        if line.startswith('WDG:'):
            try:
                record = json.loads(line[4:])
            except (ValueError, TypeError):
                record = {}
            if (isinstance(record, dict) and record.get('v') == 1
                    and record.get('kind') == 'capabilities'
                    and record.get('c5_jobs_v1') is not True):
                self.legacy_endpoints.add(session.endpoint)
        if session.serial is self.app.serial:
            self.app._handle_serial_line(line, device_session=session)

    def _group_event(self, name, group, details):
        view = self.groups.get(group.group_id)
        if not view:
            return
        if name == 'passive_interrupted':
            view.error = details.get('error', group.error) or 'Passive coverage interrupted'
            view.restore.clear()
            self.scan.error = view.error
            self.app.msg('[C5] '+view.error[:100], 8)
            return
        if name == 'member_error' and view.passive and view.announced:
            view.error = details.get('error', group.error) or 'Passive radio error'
            view.restore.clear()
            self.scan.error = view.error
            self.app.msg('[C5] '+view.error[:100], 8)
            return
        if name in ('rejected', 'start_uncertain', 'stop_failed', 'member_error',
                    'replacement_aborted', 'replacement_timeout', 'group_interrupted', 'compensating_stop'):
            view.error = details.get('error', group.error) or name.replace('_', ' ')
            view.restore.clear()
            view.state = 'stopping'
            self.scan.error = view.error
            self.app.msg('[C5] '+view.error[:100], 8)
            if name in ('replacement_aborted','replacement_timeout'):
                view.stopped.update(view.requests)
                self._finish(view)
        elif name == 'member_running':
            allowed = ('running','reporting','disconnected','stopped') if view.passive else ('running','reporting')
            if all(state in allowed for state in group.states.values()):
                view.state = 'running'
                self._announce(view)
        elif name == 'stopping':
            view.state = 'stopping'

    def _job_line(self, group, did, line):
        # Import each completed sniffer transaction into its own collector.
        if line.startswith('SNIFF:'):
            collector = self.sniffers.setdefault((did,group.identity), SnifferIntelCollector())
            record = parse_sniffer_record(line)
            if record is None:
                group.error = 'Malformed source sniffer export'
                return
            if record['kind'] == 'summary':
                collector.start(record['session'])
            if collector.accept(record):
                if self.app.loot:
                    self.app.loot.save_sniffer_aps(collector.aps)
                    self.app.loot.save_sniffer_probes(collector.probes)
                self._provenance(dict(device_id=did, boot_id=group.requests[did]['boot_id'],
                    job_token=group.tokens[did], kind='sniffer_export', packets=collector.packets))
            return
        # Typed source envelopes can carry existing UI progress. Only its
        # foreground owner may change a portal UI; raw artifact dumps stay out.
        if group.identity == self.foreground and group.roles[did] == 'foreground':
            if group.operation in ('portal','evil_twin'):
                self.app._handle_serial_line(line, device_session=self.sessions[did])

    def _announce(self, group):
        if group.announced:
            return
        group.announced = True
        self.scan.session = group.identity
        self.scan.mode = group.operation
        self.scan.wifi_only = group.wifi_only
        self.scan.error = ''
        self.app._set_running(group.state_key, True)
        if group.operation == 'wardrive':
            owner = self.app.wardrive
            if group.host_ble and not owner.meshcore_ble_blocks_host_scan():
                if not owner.host_ble.start(group.identity, adapter=owner.settings.get('host_ble_adapter','auto'),
                                            require_lease=owner._host_ble_requires_lease()):
                    owner.host_ble_error('Host scanner did not start')
            owner.start_cell()
            owner.start_auxiliary_collectors()
        self.app.msg('[C5] '+group.operation+' running on '+str(len(group.requests))+' boards', 3)

    def start_portal(self, operation, html, ssid, targets, state_key):
        """Prepare and launch portal/Evil Twin on one explicit C5 owner.

        Returns ``True`` when launched or queued behind stop acknowledgements,
        and ``False`` when no suitable owner exists or prelaunch validation
        fails. Invalid operation/SSID and overlapping preparations raise
        ``ValueError``. Accepted work uploads asynchronously; later failures
        are reported with ``app.msg`` and set ``app._attack_step`` to ``error``.
        Serial writes always use the selected owner's manager directly.
        """
        if operation not in ('portal','evil_twin'):
            raise ValueError('portal operation must be portal or evil_twin')
        if not isinstance(ssid, str) or len(ssid.encode('utf-8')) > 32:
            raise ValueError('SSID must be at most 32 UTF-8 bytes')
        if self._portal_preparation:
            raise ValueError('another portal preparation is already pending')
        devices = [d for d in self.devices() if operation in d.capabilities]
        devices.sort(key=lambda d: (0 if d.board == 'xiao' else 1,
                                   0 if 'foreground' in d.roles else 1, d.device_id))
        if not devices:
            self._portal_error(None, f'No connected device supports {operation}')
            return False

        try:
            target_snapshot = tuple(MappingProxyType(dict(target)) for target in targets)
            options = {'storage':'serial', 'scope':'selected', 'ssid_hex':ssid.encode('utf-8').hex()}
        except (TypeError, ValueError) as exc:
            self._portal_error(None, 'Invalid portal targets: '+str(exc)[:90])
            return False
        owner = owner_request = None
        allocation_error = None
        for candidate in devices:
            active_group = self._passive_group_for(candidate.device_id)
            if candidate.session.active and active_group is None:
                continue
            try:
                owner_request = self.scheduler.plan(
                    [candidate], operation, targets=target_snapshot, options=options
                )['requests'][candidate.device_id]
            except CapacityError as exc:
                allocation_error = exc
                continue
            except (TypeError, ValueError, KeyError) as exc:
                self._portal_error(None, 'Portal target allocation failed: '+str(exc)[:90])
                return False
            owner = candidate
            break
        if owner is None:
            suffix = ': '+str(allocation_error)[:70] if allocation_error else ''
            self._portal_error(None, 'No free single portal device can cover all targets'+suffix)
            return False
        owner_request['exclude'] = [e.mac for e in getattr(self.app._whitelist, 'entries', ())
            if str(getattr(e,'type','')).lower() == 'wifi'] if operation == 'evil_twin' else []

        helper = None
        helper_request = None
        if target_snapshot or operation == 'portal':
            for candidate in (d for d in devices if d.device_id != owner.device_id):
                if candidate.session.active and self._passive_group_for(candidate.device_id) is None:
                    continue
                operation_options = ('hs_sniff','wardrive')
                helper_op = next((op for op in operation_options if op in candidate.capabilities), None)
                if not helper_op:
                    continue
                try:
                    plan = self.scheduler.plan([candidate], helper_op,
                        targets=target_snapshot, options={'storage':'serial','scope':'selected'})
                except (TypeError, ValueError, CapacityError):
                    continue
                if helper_op == 'wardrive':
                    plan['requests'][candidate.device_id]['ble'] = 'off'
                helper, helper_request = candidate, plan['requests'][candidate.device_id]
                break

        requests = {owner.device_id: owner_request}
        roles = {owner.device_id:'foreground'}
        if helper:
            requests[helper.device_id] = helper_request
            roles[helper.device_id] = 'passive-helper'
        restore, waiting = {}, {}
        for did in requests:
            entry = self.registry.get(did)
            session = entry.session if entry else None
            if not session or not session.connected:
                self._portal_error(None, f'Portal device disconnected: {did}')
                return False
            if not session.active:
                continue
            old = self._passive_group_for(did)
            if not old or not old.passive:
                self._portal_error(None, f'Portal radio is owned by foreground work: {did}')
                return False
            restore[did] = dict(session.job_request)
            waiting[did] = session.job_token

        prep = {'operation':operation, 'html':html, 'ssid':ssid,
                'targets':target_snapshot, 'state_key':state_key or operation,
                'owner_id':owner.device_id, 'helper_id':helper.device_id if helper else None,
                'owner_boot':owner.boot_id, 'requests':requests, 'roles':roles,
                'restore':restore, 'waiting':waiting, 'deadline':self.clock()+10.0,
                'cancel':threading.Event(), 'state':'waiting'}
        self._portal_preparation = prep
        self.legacy_owned.add(owner.device_id)
        self.app._attack_step = 'preparing'
        for did, token in list(waiting.items()):
            session = self.sessions[did]
            if not session.stop_job():
                prep['error'] = f'Could not stop passive owner {did}'
                self._portal_error(prep, prep['error'])
                return False
        if waiting:
            self.app.msg('[C5] Waiting for passive portal radio stop acknowledgement', 5)
            return True
        return self._launch_portal_preparation()

    def _passive_group_for(self, device_id):
        session = self.sessions.get(device_id)
        if not session or not session.job_token:
            return None
        return next((group for group in self.groups.values()
                     if group.tokens.get(device_id) == session.job_token
                     and group.state in ('starting','running','stopping')
                     and group.passive), None)

    def _advance_portal_preparation(self, device_id, token):
        prep = self._portal_preparation
        if not prep or prep['waiting'].get(device_id) != token:
            return
        prep['waiting'].pop(device_id, None)
        if not prep['waiting']:
            self._launch_portal_preparation()

    def _launch_portal_preparation(self):
        prep = self._portal_preparation
        if not prep:
            return False
        owner = self.registry.get(prep['owner_id'])
        if not owner or not owner.connected or owner.boot_id != prep['owner_boot']:
            self._portal_error(prep, 'Portal owner changed or disconnected during preparation')
            return False
        # Revalidate the boot-bound requests after waiting for any stop barrier.
        requests = {}
        for did, request in prep['requests'].items():
            entry = self.registry.get(did)
            if (not entry or not entry.connected or entry.boot_id != request['boot_id']
                    or not entry.session or entry.session.boot_id != request['boot_id']):
                self._portal_error(prep, f'Portal radio changed during preparation: {did}')
                return False
            try:
                requests[did] = validate_job_request(request, boot_id=entry.boot_id,
                    limits=limits_for_operation(entry.device_info, request['op']))
            except ValueError as exc:
                self._portal_error(prep, 'Portal request became invalid: '+str(exc)[:80])
                return False
        prep['requests'] = requests
        prep['state'] = 'uploading'
        prep['result'] = queue.Queue(maxsize=1)
        manager = owner.session.serial
        cancel = prep['cancel']

        def send_if_current(command):
            if cancel.is_set():
                raise RuntimeError('portal preparation cancelled')
            if not owner.session.connected or owner.session.boot_id != prep['owner_boot']:
                raise ConnectionError('portal owner disconnected or rebooted')
            strict_sender = getattr(manager, 'send_command_strict', None)
            if not callable(strict_sender):
                raise RuntimeError('selected serial manager has no strict write API')
            if strict_sender(command) is False:
                raise OSError('serial command write was rejected')

        def upload_worker():
            try:
                if prep['html'] is None:
                    send_if_current('clear_html')
                else:
                    from .portals import upload_html_to_esp32
                    upload_html_to_esp32(prep['html'], send_if_current)
                prep['result'].put((True, ''))
            except Exception as exc:
                prep['result'].put((False, str(exc)[:160]))

        prep['worker'] = threading.Thread(target=upload_worker, name='c5-portal-upload', daemon=True)
        prep['worker'].start()
        self.app.msg(f'[C5] Preparing {prep["operation"]} on {owner.board}', 5)
        return True

    def _portal_error(self, prep, error):
        if prep:
            prep['cancel'].set()
            if self._portal_preparation is prep:
                self._portal_preparation = None
            self.legacy_owned.discard(prep['owner_id'])
        self.app._attack_step = 'error'
        if hasattr(self.app, '_evil_twin_starting'):
            self.app._evil_twin_starting = False
        self.app.msg('[C5] '+str(error)[:110], 8)

    def _tick_portal_preparation(self):
        prep = self._portal_preparation
        if not prep:
            return
        if prep['state'] == 'waiting':
            if self.clock() >= prep['deadline']:
                self._portal_error(prep, 'Timed out waiting for passive portal radio stop acknowledgement')
            return
        if prep['state'] != 'uploading':
            return
        try:
            succeeded, error = prep['result'].get_nowait()
        except queue.Empty:
            return
        if not succeeded:
            self._portal_error(prep, 'Portal preparation upload failed: '+error)
            return
        self._start_portal_group(prep)

    def _start_portal_group(self, prep):
        if self._portal_preparation is not prep or prep['cancel'].is_set():
            return False
        owner = self.registry.get(prep['owner_id'])
        if (not owner or not owner.connected or owner.boot_id != prep['owner_boot']
                or not owner.session or owner.session.boot_id != prep['owner_boot']):
            self._portal_error(prep, 'Portal owner changed or disconnected after upload')
            return False
        for did, request in prep['requests'].items():
            entry = self.registry.get(did)
            if (not entry or not entry.connected or not entry.session
                    or entry.boot_id != request['boot_id'] or entry.session.boot_id != request['boot_id']):
                self._portal_error(prep, f'Portal radio changed after upload: {did}')
                return False

        gid = secrets.token_hex(8)
        tokens = {did:secrets.token_hex(8) for did in prep['requests']}
        view = GroupView(gid, prep['operation'], prep['state_key'], prep['requests'],
                         prep['roles'], tokens, passive=False,
                         restore=dict(prep['restore']))
        self.groups[gid] = view
        result = self.controller.start_group(
            {did:(tokens[did],request) for did,request in prep['requests'].items()}, group_id=gid)
        if result is None:
            self.groups.pop(gid, None)
            self._portal_error(prep, 'Portal job rejected after preparation')
            return False
        self._portal_preparation = None
        self.legacy_owned.discard(prep['owner_id'])
        self.foreground = gid
        self.scan.session = gid
        self.scan.mode = prep['operation']
        self.scan.wifi_only = False
        return True

    def start(self, operation, *, state_key='', targets=(), options=None,
              wifi_only=False, live_ouis=(), passive=None):
        devices = self.devices()
        if self._select_legacy_fallback(operation, devices):
            return False
        if not devices:
            return False
        foreground = passive is False or operation not in PASSIVE_OPS
        if foreground and any(not view.passive and view.state in ('starting','running','stopping')
                              for view in self.groups.values()):
            raise ValueError('Stop the foreground group before starting another operation')
        options = dict(options or {'storage':'serial', 'scope':'selected'})
        if options.get('storage') == 'sd':
            devices = [d for d in devices if d.board != 'waveshare-zero']
        plan = self.scheduler.plan(devices, operation, targets=targets,
            dwell_ms=400 if operation == 'handshake' else 160, options=options)
        requests = plan['requests']
        if operation == 'wardrive':
            requests = self._wardrive_preferences(devices, requests)
        excludes = [e.mac for e in getattr(self.app._whitelist, 'entries', ())
                    if str(getattr(e,'type','')).lower() == 'wifi']
        for did, request in requests.items():
            request['exclude'] = excludes if operation in ('handshake','deauth','sae','blackout','evil_twin') else []
            if wifi_only:
                request['ble'] = 'off'
            if live_ouis and operation in ('wardrive','wifi_scan'):
                request['live_ouis'] = list(live_ouis)
            if operation != 'wardrive':
                request['options'] = dict(options)
            validate_job_request(request, limits=limits_for_operation(
                self.registry.get(did).device_info, request['op']))
        roles = {did:('foreground' if foreground else 'background') for did in requests}
        restore = {}
        if foreground:
            previous = self.groups.get(self.background)
            if previous and previous.state in ('starting','running'):
                restore = {did:dict(req) for did,req in previous.requests.items()}
                previous.restore.clear()
            spare = [d for d in devices if d.device_id not in requests
                     and (not d.session.active or (d.session.job_request or {}).get('op') in PASSIVE_OPS)]
            if spare:
                helper = spare[0]
                target_channels = sorted({t['channel'] for t in targets})
                hop = [c for c in target_channels if c in helper.device_info['channels']]
                helper_op = 'hs_sniff' if hop and 'hs_sniff' in helper.capabilities else 'wardrive'
                helper_plan = self.scheduler.plan([helper], helper_op, channels=hop or None)
                helper_request = helper_plan['requests'][helper.device_id]
                helper_request['ble'] = 'off'
                requests[helper.device_id] = helper_request
                roles[helper.device_id] = 'passive-helper'
        gid = secrets.token_hex(8)
        tokens = {did:secrets.token_hex(8) for did in requests}
        view = GroupView(gid, operation, state_key or operation, requests, roles, tokens,
                         passive=not foreground, restore=restore, wifi_only=wifi_only,
                         host_ble=(operation == 'wardrive' and state_key == 'all_wardrive_host'))
        if operation in ('wifi_scan','ble_scan') and state_key != 'bt_scanning':
            view.deadline = self.clock()+12
        self.groups[gid] = view
        result = self.controller.start_group({did:(tokens[did],req) for did,req in requests.items()}, group_id=gid)
        if result is None:
            self.groups.pop(gid, None)
            return False
        if foreground:
            self.foreground = gid
        else:
            self.background = gid
        self.scan.session = gid
        self.scan.mode = operation
        self.scan.wifi_only = wifi_only
        return True

    def start_command(self, command, state_key='', name=''):
        try:
            args = shlex.split(command)
            op = COMMAND_OPS.get(args[0]) if args else None
            if op is None:
                return False
            if self._select_legacy_fallback(op, self.devices()):
                return False
            options = {'storage':'serial', 'scope':'all'}
            targets = []
            owner = self.app.wardrive
            if args[0] in ('start_deauth','sae_overflow'):
                if len(args) != 3:
                    raise ValueError('Enter a BSSID and channel')
                row = owner.targets.rows.get(args[1].upper(), {})
                targets = [{'bssid':args[1].upper(), 'channel':int(args[2]),
                            'auth':row.get('auth',6 if op=='sae' else 0),
                            'ssid_hex':row.get('ssid_hex','')}]
                options['scope'] = 'selected'
            elif args[0] == 'start_handshake_scope':
                if len(args) < 3:
                    raise ValueError('Capture scope is missing')
                options['storage'] = args[1]
                if args[2] not in ('all','all-except'):
                    if args[2] != owner.targets.token or owner.targets.state != 'ready':
                        raise ValueError('Scan changed; scan and select targets again')
                    if self.clock()-owner.targets.completed > 300:
                        raise ValueError('Target scan expired')
                    for mac in args[3].split(','):
                        row = owner.targets.rows[mac.upper()]
                        targets.append({key:row[key] for key in ('bssid','channel','auth','ssid_hex')})
                    options['scope'] = 'selected'
            elif args[0] == 'start_handshake':
                options['storage'] = 'sd'
            elif args[0] == 'scan_bt' and len(args) > 1:
                op = 'bt_track'
                options['mac'] = args[1].upper()
            elif args[0] == 'hs_scan':
                owner.targets.state = 'scanning'
            elif args[0] == 'start_portal':
                options['ssid_hex'] = getattr(self.app,'_portal_ssid','').encode().hex()
            wifi_only = state_key == 'all_wardrive_host' or 'wifi' in args[0]
            if args[0] == 'scan_networks' and state_key == 'wardriving':
                op, wifi_only = 'wardrive', True
            ok = self.start(op, state_key=state_key, targets=targets, options=options,
                            wifi_only=wifi_only, live_ouis=owner.detector.oui_prefixes())
            if not ok:
                self.app.msg('[C5] Assignment rejected; see device health', 9)
            return True
        except (ValueError, KeyError, IndexError, CapacityError) as exc:
            self.app.msg('[C5] '+str(exc)[:115], 9)
            return True

    def _select_legacy_fallback(self, operation, devices):
        selected_legacy = next((endpoint for endpoint, session in self.endpoints.items()
                                if session.serial is self.app.serial
                                and endpoint in self.legacy_endpoints), None)
        if (selected_legacy is None
                or any(operation in device.capabilities for device in devices)):
            return False
        # Let the selected legacy endpoint keep its existing parser and index
        # space. Never route fallback to a C5 device lacking this operation.
        self.app.wardrive.scan = self.app.wardrive.legacy_scan
        return True

    def stop_foreground(self):
        gid = self.foreground or self.background
        if gid:
            self.controller.stop_group(gid)

    def stop_operation(self, operation):
        for group in self.groups.values():
            if group.operation == operation and group.state in ('starting','running'):
                self.controller.stop_group(group.identity)

    def _wardrive_preferences(self, devices, requests):
        settings = self.app.wardrive.settings
        preferences = settings.get('c5_devices',{})
        if settings.get('c5_preset') == 'wifi-dedicated-ble' and len(devices) >= 2:
            wifi = next((d for d in devices if d.board == 'xiao'), devices[0])
            ble = next(d for d in devices if d.device_id != wifi.device_id)
            requests = self.scheduler.plan_wardrive([wifi])['requests']
            requests[wifi.device_id]['ble'] = 'off'
            requests.update(self.scheduler.plan([ble],'ble_scan')['requests'])
        for device in devices:
            request = requests.get(device.device_id)
            if request is None:
                continue
            role = preferences.get(device.device_id,{}).get('role','auto')
            if role in ('wifi24-ble','wifi5','wifi'):
                request['op'] = 'wardrive'
                request['channels'] = [c for c in device.device_info['channels']
                    if role == 'wifi' or (c <= 14) == (role == 'wifi24-ble')]
                if len(request['channels']) > 40:
                    raise ValueError('Role channel plan exceeds 40 channels')
                request['band'] = self.scheduler._band(request['channels'])
                request['ble'] = 'scan' if role == 'wifi24-ble' else 'off'
            elif role == 'ble':
                request.update(op='ble_scan', band='none', channels=[], ble='scan')
        return requests

    def stop_all(self):
        if self._portal_preparation:
            self._portal_error(self._portal_preparation, 'Portal preparation cancelled by global stop')
        for group in list(self.groups.values()):
            group.restore.clear()
            if group.state in ('starting','running','stopping'):
                self.controller.stop_group(group.identity)
        for session in self.endpoints.values():
            if session.external_busy and session.connected and not self.is_reserved(session):
                session.serial.send_command_strict('stop')
                session.serial.send_command('get_device_info')
        self.foreground = self.background = None

    def _tick_groups(self):
        for group in list(self.groups.values()):
            if group.deadline and group.state == 'running' and self.clock() >= group.deadline:
                group.deadline = 0
                self.controller.stop_group(group.identity)
        pending = getattr(self.app,'_c5_pending_update',None)
        if pending:
            method, session = pending
            if not session.active and not session.external_busy and session.device_id not in self.legacy_owned:
                self.app._c5_pending_update = None
                if session.connected:
                    self.app.serial = session.serial
                    getattr(self.app,method)()

    def _finish(self, group):
        if group.state in ('stopped', 'error'):
            return
        group.state = 'error' if group.error else 'stopped'
        self.app._set_running(group.state_key, False)
        if group.operation == 'wifi_scan':
            rows = {}
            for did, snapshot in group.snapshots.items():
                for mac, observation in snapshot.items():
                    row = dict(bssid=mac, channel=observation['channel'], rssi=observation['rssi'],
                               auth=AUTH.get(observation.get('auth'),255), ssid_hex=observation['ssid_hex'],
                               name=bytes.fromhex(observation['ssid_hex']).decode('utf-8','replace'),
                               clients=0, packets=0, probes=0, source_device=did,
                               source_boot=group.requests[did]['boot_id'], source_job=group.tokens[did])
                    if mac not in rows or row['rssi'] > rows[mac]['rssi']:
                        rows[mac] = row
            owner = self.app.wardrive.targets
            owner.rows, owner.state, owner.completed = rows, 'ready', self.clock()
            owner.started = True
            if getattr(self.app,'_et_scan_pending',False):
                self.app.wardrive._finish_evil_twin_scan()
        restore = group.restore
        group.restore = {}
        if self.foreground == group.identity:
            self.foreground = None
        if self.background == group.identity:
            self.background = None
        if restore and not group.error:
            # Restore only after the complete foreground/helper drain barrier.
            self.restore_background(restore)
        self._prune_groups()

    def _prune_groups(self):
        terminal = [gid for gid, group in self.groups.items()
                    if group.state in ('stopped','error')]
        while len(self.groups) > MAX_TERMINAL_GROUPS:
            gid = next((key for key in terminal
                        if key not in (self.foreground,self.background)), None)
            if gid is None:
                break
            terminal.remove(gid)
            self.groups.pop(gid, None)
            self.controller.groups.pop(gid, None)
            for key in list(self.sniffers):
                if key[1] == gid:
                    self.sniffers.pop(key, None)

    def restore_background(self, requests):
        # Keep the exact preempted band/BLE/options allocation. Boot changes
        # invalidate restoration rather than silently altering its owner.
        for did, request in requests.items():
            device = self.registry.get(did)
            if not device or not device.connected or device.boot_id != request['boot_id']:
                return False
        gid = secrets.token_hex(8)
        tokens = {did:secrets.token_hex(8) for did in requests}
        self.groups[gid] = GroupView(gid,'wardrive','all_wardrive',requests,
            {did:'background' for did in requests},tokens,passive=True)
        result = self.controller.start_group({did:(tokens[did],req) for did,req in requests.items()},group_id=gid)
        if result:
            self.background = gid
            return True
        self.groups.pop(gid,None)
        return False

    def _open_capture(self, group, did, source):
        request = group.requests[did]
        if request['op'] not in ('hs_sniff','handshake'):
            return
        loot = self.app.loot
        if not loot or not loot.active:
            self._storage_failure(group, 'No writable loot session')
            return
        key = (did, request['boot_id'], group.tokens[did])
        if key in self.captures:
            return
        directory = Path(loot.session_path)/'handshakes'/did/request['boot_id']/group.tokens[did]
        validator = ExchangeValidator()
        for target in request['targets']:
            validator.set_ssid(target['bssid'],bytes.fromhex(target['ssid_hex']))
        self.validators[key] = validator
        def frame_received(frame, metadata, at):
            info = inspect_frame(frame)
            if info['ssid_hex']:
                validator.set_ssid(info['bssid'],bytes.fromhex(info['ssid_hex']))
            if request['op'] != 'handshake':
                return  # Helpers remain independent passive evidence.
            artifact = validator.ingest(frame)
            if artifact:
                digest = hashlib.sha256(artifact['hccapx']).hexdigest()
                name = (artifact['bssid'].replace(':','')+'-'+artifact['station'].replace(':','')+
                        '-'+str(artifact['replay'])+'-'+digest[:12])
                path = directory/(name+'.hccapx')
                if not path.exists():
                    with path.open('xb') as fh:
                        fh.write(artifact['hccapx'])
                self._provenance(dict(source, kind='validated_exchange', observed_at=at,
                                      bssid=artifact['bssid'], station=artifact['station'],
                                      replay=artifact['replay'], path=str(path)))
                reward = (artifact['bssid'], artifact['station'], artifact['replay'], digest)
                if reward not in group.rewarded_exchanges:
                    group.rewarded_exchanges.add(reward)
                    self.app._trigger_hs_event()
        capture = PassiveCapture(source=source, on_frame=frame_received)
        try:
            capture.open(directory)
            self.captures[key] = capture
        except OSError as exc:
            self._storage_failure(group, str(exc))

    def _close_capture(self, key):
        capture = self.captures.pop(key, None)
        self.validators.pop(key,None)
        if capture:
            try:
                capture.close()
            except OSError as exc:
                self.app.msg('[C5] Capture flush failed: '+str(exc)[:70], 8)

    def _close_source(self, did):
        for key in list(self.captures):
            if key[0] == did:
                self._close_capture(key)

    def _storage_failure(self, group, error):
        group.error = 'Capture storage failed: '+error[:100]
        group.restore.clear()
        self.controller.stop_group(group.identity)
        self.app.msg('[C5] '+group.error, 8)

    def _provenance(self, record):
        loot = self.app.loot
        if loot and loot.active:
            try:
                output = dict(record)
                valid = getattr(self.app, 'gps_fix', None)
                if type(valid) is bool:
                    output['gps_valid'] = valid
                    if valid:
                        latitude = getattr(self.app, 'player_lat', None)
                        longitude = getattr(self.app, 'player_lon', None)
                        if (type(latitude) in (int,float) and type(longitude) in (int,float)
                                and math.isfinite(latitude) and math.isfinite(longitude)
                                and -90 <= latitude <= 90 and -180 <= longitude <= 180):
                            output['latitude'], output['longitude'] = latitude, longitude
                with (Path(loot.session_path)/'device_provenance.jsonl').open('a',encoding='utf-8') as fh:
                    fh.write(json.dumps(output, separators=(',',':'))+'\n')
            except OSError as exc:
                self.app.msg('[C5] Provenance write failed: '+str(exc)[:75],8)

    def reserve(self, manager):
        session = self.endpoints.get(manager.device)
        if not session or session.serial is not manager:
            return None
        if self._reserved_sessions.get(session.endpoint) is session:
            return session
        if session.device_id in self.legacy_owned:
            return False
        if session.external_busy:
            session.serial.send_command_strict('stop')
            session.serial.send_command('get_device_info')
            return False
        if session.active:
            view = next((g for g in self.groups.values() if g.tokens.get(session.device_id) == session.job_token),None)
            if view and not view.passive:
                view.restore = {did:req for did,req in view.restore.items() if did != session.device_id}
                self.controller.stop_group(view.identity)
            else:
                session.stop_job()
            return False  # UI retries only after acknowledged cleanup.
        self._reserved_sessions[session.endpoint] = session
        reserved_id = session.device_id
        self._reserved_ids_by_endpoint[session.endpoint] = reserved_id
        self._reserved_usb_by_endpoint[session.endpoint] = self._usb_identity(session.serial)
        if reserved_id:
            self.reserved.add(reserved_id)
        return session

    def release(self, session):
        if not session:
            return
        endpoint = session.endpoint
        # A stale or repeated release must not tear down a replacement session
        # or consume the replacement's endpoint-scoped reservation.
        if self._reserved_sessions.get(endpoint) is not session:
            return

        self._reserved_sessions.pop(endpoint, None)
        reserved_id = self._reserved_ids_by_endpoint.pop(endpoint, None)
        identity = self._reserved_usb_by_endpoint.pop(
            endpoint, self._usb_identity(session.serial))
        session.serial.close()
        if reserved_id and not any(value == reserved_id
                                   for value in self._reserved_ids_by_endpoint.values()):
            self.reserved.discard(reserved_id)
        if self.endpoints.get(endpoint) is session:
            self.endpoints.pop(endpoint, None)
            self.registry.disconnect(endpoint)
            self.sessions.pop(session.device_id, None)
            self.controller.sessions.pop(session.device_id, None)
        # Resolve a new tty by the saved physical USB identity before open.
        matches = [p for p in supported_candidates()
                   if self._same_reserved_usb(identity, p)]
        if len(matches) == 1:
            try:
                manager = SerialManager(matches[0]['endpoint'])
                manager.setup()
                self.attach(manager)
            except OSError:
                pass
        self.discover()

    def health_lines(self):
        lines = []
        for device in self.registry.list():
            session = device.session
            alias = next(iter(device.aliases), device.board)
            group = next((g for g in reversed(list(self.groups.values()))
                          if g.state != 'stopped' and device.device_id in g.requests
                          and (not session or g.tokens[device.device_id] == session.job_token)),None)
            req = group.requests[device.device_id] if group else {}
            role = group.roles[device.device_id] if group else (
                'external legacy' if session and session.external_busy else 'idle')
            channel = session.applied_channel or '--' if session else '--'
            lines.append(f"{alias}: {role} {req.get('band','')} ch:{channel} "
                         f"{req.get('op','')} {session.state if session else 'offline'} "
                         f"drops:{session.counts['drops'] if session else 0}")
            if session and any(session.losses.values()):
                lines.append('  Losses ' + ' '.join(
                    f'{name}:{count}' for name, count in session.losses.items()))
        for endpoint in sorted(self.legacy_endpoints):
            if endpoint in self.endpoints:
                selected = ' selected' if self.endpoints[endpoint].serial is self.app.serial else ''
                lines.append(f"Legacy USB {endpoint}: single-device fallback{selected}")
        return lines

    def coverage_gap(self):
        active = [s.job_request for s in self.sessions.values() if s.active and s.job_request]
        channels = {c for request in active for c in request['channels']}
        gaps = []
        if not any(c <= 14 for c in channels):
            gaps.append('2.4 GHz')
        if not any(c > 14 for c in channels):
            gaps.append('5 GHz')
        if not any(r['ble'] == 'scan' for r in active) and getattr(self.app.wardrive.host_ble,'state','idle') not in ('starting','running'):
            gaps.append('BLE')
        return 'Paused coverage: '+', '.join(gaps) if gaps else 'Coverage: 2.4 GHz / 5 GHz / BLE'

    def draw_capture(self, px, operation):
        if not self.available:
            return False
        group = next((g for g in reversed(list(self.groups.values())) if g.operation == operation),None)
        px.cls(0)
        px.rect(0,0,640,18,3)
        px.text(8,5,'MULTI C5 / '+('ACTIVE HS CAPTURE' if operation == 'handshake' else 'PASSIVE HS SNIFF'),0)
        px.text(8,28,group.state.upper() if group else 'IDLE',11)
        px.text(8,46,'Each row is independent evidence from one device, boot and job.',13)
        px.text(8,65,'BOARD / ROLE                 BAND       FRAMES EAPOL PMKID VALID LOST',3)
        if group:
            for index, (did,req) in enumerate(group.requests.items()):
                entry = self.registry.get(did)
                metrics = group.capture_metrics.get(did,{})
                line = f"{entry.board[:14]:14} {group.roles[did][:14]:14} {req['band']:7} "
                line += ' '.join(f"{metrics.get(k,0):5}" for k in ('frames','eapol','pmkids','validated','lost'))
                px.text(8,86+index*20,line,7)
                px.text(8,98+index*20,did+' / '+req['boot_id']+' / '+group.tokens[did],13)
            px.text(8,200,group.error[:115],8)
        px.text(8,254,'Files: loot/handshakes/<device>/<boot>/<job>/',13)
        px.text(8,271,'Helpers remain separate evidence; message counts never establish a usable exchange.',13)
        px.text(8,300,self.coverage_gap()[:115],9)
        px.text(8,331,'[ENTER] Start  [S] Stop this group  [N] Networks (active)  [ESC/TAB] Map',3)
        return True

    def close(self):
        self.stop_all()
        for key in list(self.captures):
            self._close_capture(key)
        for session in self.endpoints.values():
            if not self.is_reserved(session):
                session.serial.close()
