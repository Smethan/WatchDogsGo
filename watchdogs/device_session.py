"""Single-reader, bounded serial session for one C5 jobs v1 device."""
from __future__ import annotations

import json
import secrets
import threading
import time
from typing import Any, Callable, Mapping

from .c5_jobs import (
    LOSS_KEYS, limits_for_operation,
    TOKEN_RE, encode_job_start, parse_device_info, parse_job_record,
    validate_job_request,
)
from .wardrive_protocol import parse_record

READ_MAX_BYTES = 16 * 1024
READ_MAX_LINES = 64
LEASE_SECONDS = 15.0
KEEPALIVE_SECONDS = 5.0
HEARTBEAT_SECONDS = 2.0
WATCHDOG_SECONDS = 7.0
STOP_ACK_SECONDS = 5.0
STOP_RETRY_LIMIT = 2
IDENTITY_RECOVERY_INITIAL_SECONDS = 5.0
IDENTITY_RECOVERY_MAX_SECONDS = 15.0
METADATA_RETRY_INITIAL_SECONDS = 0.65
METADATA_RETRY_MAX_SECONDS = 5.0


class DeviceSession:
    """Adapt a SerialManager-like object to validated device/job events.

    ``on_event(record)`` receives only valid device_info, capability, and v3
    job records. Non-WDG lines and valid legacy v1/v2 WDG lines are forwarded
    to ``on_line(line)`` for the existing source-context pipeline.
    """

    def __init__(self, serial: Any, endpoint: str, device_info: Mapping[str, Any] | None = None,
                 *, clock: Callable[[], float] = time.monotonic,
                 on_event: Callable[[dict[str, Any]], None] | None = None,
                 on_line: Callable[[str], None] | None = None) -> None:
        self.serial = serial
        self.endpoint = endpoint
        self.clock = clock
        self.on_event = on_event or (lambda _record: None)
        self.on_line = on_line or (lambda _line: None)
        self.device_info = dict(device_info or {})
        self.device_id = str(self.device_info.get("device_id", "")).upper()
        self.boot_id = str(self.device_info.get("boot_id", "")).upper()
        self.device_info_seen = bool(self.device_id and self.boot_id)
        self.capabilities: set[str] = set()
        self.c5_jobs_v1 = False
        self.capabilities_seen = False
        self.legacy_capabilities_seen = False
        self._metadata_probe_attempts = 0
        self._next_metadata_probe_at = 0.0
        self._metadata_retry_delay = METADATA_RETRY_INITIAL_SECONDS
        self._refresh_info_pending = False
        self._refresh_capabilities_pending = False
        self._refresh_legacy_pair = False
        self.external_busy = self.device_info.get('legacy_busy') is True
        self._last_busy_probe = 0.0
        self.job_token: str | None = None
        self._terminal_tokens: list[str] = []
        self._token_seq: dict[str, int] = {}
        self.job_request: dict[str, Any] | None = None
        self.state = "idle"
        self.seq = 0
        self.last_record_at = 0.0
        self.last_keepalive_at = 0.0
        self.last_heartbeat_at = 0.0
        self.last_status_query_at = 0.0
        self._stop_pending = False
        self._stop_attempts = 0
        self._stop_status_queries = 0
        self._last_stop_attempt_at = 0.0
        self._stop_timeout_reported = False
        self._identity_recovery_attempts = 0
        self._next_identity_recovery_at = 0.0
        self._identity_recovery_delay = IDENTITY_RECOVERY_INITIAL_SECONDS
        self.job_started_at = 0.0
        self._watchdog_expired = False
        self._pending_capabilities: dict[str, Any] | None = None
        self.counts = {"wifi_count": 0, "ble_count": 0, "drops": 0}
        self.losses = dict.fromkeys(LOSS_KEYS, 0)
        self.last_error = ""
        self.capture_state: dict[str, Any] = {}
        self.applied_channel: int | None = None
        self._reader_lock = threading.Lock()
        self.last_poll_drained = False

    @property
    def connected(self) -> bool:
        return bool(getattr(self.serial, "is_open", True))

    @property
    def active(self) -> bool:
        return self.state in ("starting", "running", "reporting", "stopping")

    def set_device_info(self, device_info: Mapping[str, Any]) -> None:
        next_id = str(device_info["device_id"]).upper()
        next_boot = str(device_info["boot_id"]).upper()
        identity_or_boot_changed = bool(
            (self.device_id and next_id != self.device_id)
            or (self.boot_id and next_boot != self.boot_id)
        )
        if identity_or_boot_changed:
            self._identity_recovery_attempts = 0
            self._next_identity_recovery_at = 0.0
            self._identity_recovery_delay = IDENTITY_RECOVERY_INITIAL_SECONDS
            # Capabilities are bound to the new negotiation epoch. A v3 caps
            # record from the previous boot must not make this one look ready.
            self.c5_jobs_v1 = False
            self.capabilities.clear()
            self.capabilities_seen = False
            self.legacy_capabilities_seen = False
            self._pending_capabilities = None
        if self.device_id and next_id != self.device_id:
            self.last_error = "factory identity changed on serial endpoint"
            if self.active:
                self.state = "error"
                event = {"kind": "identity_changed", "device_id": self.device_id,
                         "new_device_id": next_id, "boot_id": self.boot_id}
                self.on_event(event)
            self.job_token = None
            self.job_request = None
        elif self.boot_id and next_boot != self.boot_id:
            if self.active:
                previous_token = self.job_token
                self.last_error = "device rebooted during an active job"
                self.state = "error"
                if previous_token:
                    self._remember_terminal(previous_token)
                self.job_token = None
                self.job_request = None
                self.on_event({"kind": "boot_changed", "device_id": next_id,
                               "old_boot_id": self.boot_id, "boot_id": next_boot})
            self.seq = 0
        self.device_info = dict(device_info)
        self.external_busy = device_info.get('legacy_busy') is True
        self.device_id = next_id
        self.boot_id = next_boot
        self.device_info_seen = True
        if self._pending_capabilities is not None:
            pending = self._pending_capabilities
            self._pending_capabilities = None
            self._apply_job_capabilities(pending)
            self.on_event(dict(pending))

    def _apply_job_capabilities(self, record: Mapping[str, Any]) -> None:
        self.c5_jobs_v1 = record.get("c5_jobs_v1") is True
        self.capabilities_seen = True
        self.legacy_capabilities_seen = not self.c5_jobs_v1
        self._refresh_capabilities_pending = False
        self.capabilities = {key for key, value in record.items()
                             if key.endswith("_v1") and value is True}

    def request_metadata_probe(self) -> list[str]:
        """Request only unanswered identity/capability metadata.

        Calls are throttled per endpoint. Runtime polling calls this after the
        bounded read, so any response already buffered gets consumed first.
        """
        now = self.clock()
        if now < self._next_metadata_probe_at:
            return []
        refreshing = self._refresh_info_pending or self._refresh_capabilities_pending
        if (not self.connected or self.active or self.external_busy
                or (self.legacy_capabilities_seen and not refreshing)):
            return []
        commands = []
        # Explicit refresh is sequenced identity first so an old boot's caps
        # cannot be mistaken for the refreshed boot. Ordinary first-time
        # negotiation keeps the existing info+caps pair in either reply order.
        if self._refresh_info_pending:
            commands.append("get_device_info")
            if self._refresh_legacy_pair:
                # A legacy device may never answer get_device_info. Keep
                # checking capabilities so an in-place firmware upgrade can
                # move it into the managed protocol.
                commands.append("get_capabilities")
        elif self._refresh_capabilities_pending:
            commands.append("get_capabilities")
        else:
            if not self.device_info_seen:
                commands.append("get_device_info")
            if not self.capabilities_seen:
                commands.append("get_capabilities")
        if not commands:
            return []
        for command in commands:
            try:
                self._send(command)
            except Exception as exc:
                self.last_error = f"metadata probe failed: {exc}"[:180]
        self._metadata_probe_attempts += 1
        delay = self._metadata_retry_delay
        self._next_metadata_probe_at = now + delay
        # Bound the interval itself rather than evaluating an ever-growing
        # exponent; silent endpoints can remain connected for days.
        self._metadata_retry_delay = min(delay * 2.0, METADATA_RETRY_MAX_SECONDS)
        return commands

    def refresh_metadata(self) -> list[str]:
        """Rate-limited deliberate identity refresh, including known metadata.

        Normal polling only asks for missing fields. An explicit UI probe may
        re-arm a complete session to detect a reboot or firmware replacement
        on the same tty. The runtime applies endpoint reservation and serial
        ownership guards before calling this method.
        """
        if not self.connected or self.active or self.external_busy:
            return []
        if not (self._refresh_info_pending or self._refresh_capabilities_pending):
            self._refresh_info_pending = True
            self._refresh_capabilities_pending = True
            self._refresh_legacy_pair = bool(
                self.legacy_capabilities_seen and not self.device_info_seen)
        return self.request_metadata_probe()

    def request_stop_timeout_identity_probe(self) -> bool:
        """Read identity after a timed-out managed stop, without changing ownership.

        The runtime calls this only after draining this endpoint's bounded read
        batch and after applying its reservation/serial-owner guards. A fresh
        boot nonce can retire a rebooted owner; a same-boot reply is inert.
        """
        now = self.clock()
        if (not self.connected or not self.active or self.state != "stopping"
                or not self.job_token or not self._stop_timeout_reported
                or not self.device_info_seen or not self.capabilities_seen
                or not self.c5_jobs_v1 or self.legacy_capabilities_seen
                or self.external_busy or now < self._next_identity_recovery_at):
            return False
        # Bound retries to 5, 10, then at most 15 seconds. A failed write is
        # throttled too, and never clears the old token or renews its lease.
        delay = self._identity_recovery_delay
        self._identity_recovery_attempts += 1
        self._next_identity_recovery_at = now + delay
        self._identity_recovery_delay = min(delay * 2.0,
                                            IDENTITY_RECOVERY_MAX_SECONDS)
        try:
            result = self._send("get_device_info")
            return result is not False
        except Exception:
            return False

    def _read_lines(self) -> list[str]:
        reader = getattr(self.serial, "read_available", None)
        if not callable(reader):
            raise RuntimeError("serial adapter must implement bounded read_available")
        # Do not fall back to an unbounded compatibility call: one endpoint
        # must not drain another endpoint's frame budget.
        lines = reader(max_bytes=READ_MAX_BYTES, max_lines=READ_MAX_LINES)
        if not isinstance(lines, (list, tuple)):
            return []
        bounded: list[str] = []
        total = 0
        for line in lines[:READ_MAX_LINES]:
            if not isinstance(line, str):
                continue
            size = len(line.encode("utf-8", "replace"))
            if size > READ_MAX_BYTES or total + size > READ_MAX_BYTES:
                break
            bounded.append(line)
            total += size
        return bounded

    def poll(self) -> list[dict[str, Any]]:
        """Read and dispatch one bounded serial batch; concurrent readers are skipped."""
        self.last_poll_drained = False
        if not self._reader_lock.acquire(blocking=False):
            return []
        self.last_poll_drained = True
        events: list[dict[str, Any]] = []
        try:
            for line in self._read_lines():
                if not line.startswith("WDG:"):
                    self.on_line(line)
                    continue
                try:
                    envelope = json.loads(line[4:])
                except (ValueError, TypeError, RecursionError):
                    continue
                if not isinstance(envelope, dict):
                    continue
                version = envelope.get("v")
                if version == 1 and envelope.get("kind") == "capabilities":
                    validated = parse_record(line)
                    if validated is None:
                        continue
                    self.capabilities_seen = True
                    if type(envelope.get("c5_jobs_v1")) is not bool:
                        self.legacy_capabilities_seen = True
                        self._refresh_capabilities_pending = False
                        if self._refresh_legacy_pair:
                            self._refresh_info_pending = False
                            self._refresh_legacy_pair = False
                        self.on_line(line)
                    elif envelope["c5_jobs_v1"] is False:
                        self.legacy_capabilities_seen = True
                        self._refresh_capabilities_pending = False
                        if self._refresh_legacy_pair:
                            self._refresh_info_pending = False
                            self._refresh_legacy_pair = False
                        if self.device_id and self.boot_id:
                            self._apply_job_capabilities(envelope)
                            events.append(dict(envelope))
                        else:
                            # Legacy firmware has no v3 device_info reply.
                            # Forward this explicit capability immediately and
                            # stop retrying for metadata it cannot emit.
                            self.on_line(line)
                    elif (self.device_id and self.boot_id
                          and not self._refresh_info_pending):
                        self._apply_job_capabilities(envelope)
                        events.append(dict(envelope))
                    else:
                        self.legacy_capabilities_seen = False
                        # During an explicit identity refresh, v1 caps carry no
                        # nonce and may still belong to the previous boot.
                        self._pending_capabilities = dict(envelope)
                    continue
                if version in (1, 2):
                    self.on_line(line)
                    continue
                if version != 3:
                    continue
                kind = envelope.get("kind")
                if kind == "device_info":
                    info = parse_device_info(line)
                    if info is None:
                        continue
                    self.set_device_info(info)
                    if self._refresh_info_pending:
                        self._refresh_info_pending = False
                        self._refresh_legacy_pair = False
                        # The identity reply has been drained; capabilities
                        # can be requested during this same runtime tick.
                        self._next_metadata_probe_at = min(
                            self._next_metadata_probe_at, self.clock())
                    events.append(info)
                elif kind == "capabilities":
                    if (type(envelope.get("c5_jobs_v1")) is not bool
                            or not isinstance(envelope.get("device_id"), str)
                            or not isinstance(envelope.get("boot_id"), str)
                            or envelope["device_id"].upper() != self.device_id
                            or envelope["boot_id"].upper() != self.boot_id):
                        continue
                    if len(line.encode("utf-8", "replace")) > 1024:
                        continue
                    self.c5_jobs_v1 = envelope["c5_jobs_v1"]
                    self.capabilities_seen = True
                    self.legacy_capabilities_seen = not self.c5_jobs_v1
                    self._refresh_capabilities_pending = False
                    self.capabilities = set(envelope.get("capabilities", [])) \
                        if isinstance(envelope.get("capabilities", []), list) else set()
                    event = dict(envelope)
                    events.append(event)
                else:
                    record_token = envelope.get("session")
                    last_seq = self._token_seq.get(record_token, 0) \
                        if isinstance(record_token, str) else 0
                    record = parse_job_record(
                        line, expected_device_id=self.device_id or None,
                        expected_boot_id=self.boot_id or None, last_seq=last_seq,
                    )
                    if record is None:
                        continue
                    if not self.job_token or record["session"] != self.job_token:
                        if record["session"] not in self._terminal_tokens:
                            continue
                        # A duplicate terminal packet is useful to observers but
                        # cannot mutate a later job's owner or counters.
                        if record.get("kind") not in ("stopped", "error"):
                            continue
                        events.append(record)
                        continue
                    self.seq = record["seq"]
                    self._token_seq[record["session"]] = record["seq"]
                    if record.get('request_error'):
                        self.last_error = str(record['request_error'])[:256]
                        if self.state != 'starting':
                            continue  # A rejected retry cannot revoke an existing owner.
                        self.external_busy = record['request_error'] in ('busy','external_busy','busy_cleanup')
                        record = dict(record, kind='error', state='error', message=self.last_error)
                    self.last_record_at = self.clock()
                    self._update_snapshot(record)
                    events.append(record)
            for event in events:
                self.on_event(event)
            return events
        finally:
            self._reader_lock.release()

    def _update_snapshot(self, record: Mapping[str, Any]) -> None:
        kind = record.get("kind")
        state = record.get("state")
        if state in ("running", "reporting", "stopping", "stopped", "error"):
            self.state = state
        elif kind == "started":
            self.state = "running"
        elif kind == "stopped":
            self.state = "stopped"
        elif kind == "error":
            self.state = "error"
        for key in self.counts:
            value = record.get(key)
            if type(value) is int and value >= self.counts[key]:
                self.counts[key] = value
        for key, value in record.get('losses', {}).items():
            if key in self.losses and value >= self.losses[key]:
                self.losses[key] = value
        if kind == "error":
            self.last_error = str(record.get("error", record.get("message", "job error")))[:256]
        self.capture_state = dict(record)
        if kind in ('status', 'started', 'heartbeat') and 'channel' in record:
            self.applied_channel = record['channel']
        if self.state in ("stopped", "error"):
            self._stop_pending = False
            if self.job_token:
                self._remember_terminal(self.job_token)
            self.job_token = None
            self.job_request = None

    def _remember_terminal(self, token: str) -> None:
        if token in self._terminal_tokens:
            self._terminal_tokens.remove(token)
        self._terminal_tokens.append(token)
        removed = self._terminal_tokens[:-16]
        del self._terminal_tokens[:-16]
        for old_token in removed:
            self._token_seq.pop(old_token, None)

    def _send(self, command: str) -> Any:
        if not self.connected:
            raise ConnectionError(f"serial endpoint disconnected: {self.endpoint}")
        sender = getattr(self.serial, "send_command", None)
        if not callable(sender):
            raise RuntimeError("serial adapter has no send_command")
        return sender(command)

    def start_job(self, token: str, request: Mapping[str, Any]) -> None:
        if not self.c5_jobs_v1:
            raise RuntimeError("device does not advertise c5_jobs_v1")
        normalized = validate_job_request(
            request, boot_id=self.boot_id or None, limits=limits_for_operation(self.device_info, request["op"]))
        if self.job_token == token:
            if self.job_request == normalized:
                return
            raise ValueError("token is already owned by a different request")
        if token in self._terminal_tokens:
            raise ValueError("terminal token cannot be reused for a new local job")
        if self.active:
            raise RuntimeError("device already has an active job")
        command = encode_job_start(
            token, normalized, boot_id=self.boot_id or None,
            limits=limits_for_operation(self.device_info, request["op"]),
        )
        self.job_token = token
        self.job_request = normalized
        self.state = "starting"
        self.seq = 0
        self.last_record_at = 0.0
        self.last_status_query_at = 0.0
        self._token_seq[token] = 0
        self.counts = {"wifi_count": 0, "ble_count": 0, "drops": 0}
        self.losses = dict.fromkeys(LOSS_KEYS, 0)
        self.applied_channel = None
        now = self.clock()
        self.last_keepalive_at = self.last_heartbeat_at = now
        self.job_started_at = now
        self.last_record_at = 0.0
        self._watchdog_expired = False
        self._stop_pending = False
        self._stop_attempts = 0
        self._stop_status_queries = 0
        self._last_stop_attempt_at = 0.0
        self._stop_timeout_reported = False
        self._identity_recovery_attempts = 0
        self._next_identity_recovery_at = 0.0
        self._identity_recovery_delay = IDENTITY_RECOVERY_INITIAL_SECONDS
        try:
            self._send(command)
        except Exception:
            # A write exception can happen after bytes reached the device. Keep
            # ownership so the controller can query and issue a compensating stop.
            raise

    def stop_job(self) -> bool:
        if not self.active or not self.job_token:
            return False
        self.state = "stopping"
        self._stop_pending = True
        self._stop_timeout_reported = False
        return self._send_stop()

    def _send_stop(self) -> bool:
        token = self.job_token
        if not token:
            return False
        self._stop_attempts += 1
        self._last_stop_attempt_at = self.clock()
        try:
            result = self._send("job_stop " + token)
            if result is False:
                raise RuntimeError("serial adapter rejected job_stop")
            return True
        except Exception as exc:
            # The write may have reached firmware despite the local failure;
            # preserve the owner and retry under the bounded stop policy.
            self.last_error = f"job_stop send failed: {exc}"[:256]
            self.on_event({"kind": "stop_send_failed", "device_id": self.device_id,
                           "boot_id": self.boot_id, "session": token,
                           "attempt": self._stop_attempts, "error": self.last_error})
            return False

    def query_status(self) -> bool:
        if not self.job_token:
            return False
        self.last_status_query_at = self.clock()
        self._send("job_status " + self.job_token)
        return True

    def tick(self) -> None:
        """Renew active-job lease and query after a lost control heartbeat."""
        if not self.active or not self.job_token:
            if self.external_busy and self.connected and self.clock()-self._last_busy_probe >= 5:
                self._last_busy_probe = self.clock()
                self._send('get_device_info')
            return
        now = self.clock()
        if self._stop_pending or self.state == "stopping":
            if now - self._last_stop_attempt_at >= STOP_ACK_SECONDS:
                try:
                    if (now - self.last_status_query_at >= STOP_ACK_SECONDS
                            and self._stop_status_queries < STOP_RETRY_LIMIT + 1):
                        self._stop_status_queries += 1
                        self.last_status_query_at = now
                        self.query_status()
                except Exception as exc:
                    self.last_error = f"job_status during stop failed: {exc}"[:256]
                    self.on_event({"kind": "stop_status_failed", "device_id": self.device_id,
                                   "boot_id": self.boot_id, "session": self.job_token,
                                   "error": self.last_error})
                if self._stop_attempts <= STOP_RETRY_LIMIT:
                    self._send_stop()
                elif not self._stop_timeout_reported:
                    self._stop_timeout_reported = True
                    self.last_error = "job stop acknowledgement timed out"
                    self.on_event({"kind": "stop_timeout", "device_id": self.device_id,
                                   "boot_id": self.boot_id, "session": self.job_token,
                                   "attempts": self._stop_attempts, "error": self.last_error})
            return
        if now - self.last_keepalive_at >= KEEPALIVE_SECONDS:
            self._send("job_keepalive " + self.job_token)
            self.last_keepalive_at = now
        if now - self.last_heartbeat_at >= HEARTBEAT_SECONDS:
            last_live = self.last_record_at or self.job_started_at
            if not self._watchdog_expired and now - last_live >= WATCHDOG_SECONDS:
                self._watchdog_expired = True
                try:
                    self.query_status()
                except Exception:
                    pass
                self.stop_job()
                self.last_error = "validated device heartbeat expired"
                self.on_event({"kind": "watchdog_expired", "device_id": self.device_id,
                               "boot_id": self.boot_id, "session": self.job_token,
                               "error": self.last_error})
            self.last_heartbeat_at = now

    def disconnect(self) -> None:
        self.state = "error" if self.active else "idle"
        self.job_token = None
        self.job_request = None
        self.last_error = "serial endpoint disconnected"
