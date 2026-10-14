"""Coordinated multi-device job lifecycle and failure containment."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .c5_jobs import TOKEN_RE, validate_job_request, limits_for_operation


@dataclass
class JobGroup:
    group_id: str
    jobs: dict[str, tuple[str, dict[str, Any]]]
    states: dict[str, str] = field(default_factory=dict)
    started_at: float = 0.0
    error: str = ""
    requires_user_start: bool = False
    uncertain_since: dict[str, float] = field(default_factory=dict)
    stop_failures: set[str] = field(default_factory=set)


class JobController:
    """Run a logical operation across sessions with explicit stop/replace gates.

    ``on_event(name, group, details)`` is intended for the parent runtime/UI.
    Group starts return their group ID when accepted, or ``None`` on preflight
    rejection. Replacements remain pending until every old member confirms stop.
    """

    def __init__(self, registry: Any, sessions: Mapping[str, Any], *,
                 clock: Callable[[], float] = time.monotonic,
                 on_event: Callable[[str, JobGroup, dict[str, Any]], None] | None = None) -> None:
        self.registry = registry
        self.sessions = dict(sessions)
        self.clock = clock
        self.on_event = on_event or (lambda _name, _group, _details: None)
        self.groups: dict[str, JobGroup] = {}
        self.active_group: str | None = None
        self._pending: tuple[JobGroup, dict[str, str], float] | None = None
        self._device_group: dict[str, str] = {}

    def _prepare(self, jobs: Mapping[str, Any], group_id: str | None) -> JobGroup:
        if not isinstance(jobs, Mapping) or not jobs:
            raise ValueError("jobs must map device IDs to requests")
        prepared: dict[str, tuple[str, dict[str, Any]]] = {}
        for device_id, value in jobs.items():
            device_id = str(device_id).upper()
            session = self.sessions.get(device_id)
            entry = self.registry.get(device_id) if self.registry is not None else None
            if session is None or not session.connected:
                raise ValueError(f"device unavailable: {device_id}")
            if not session.c5_jobs_v1:
                raise ValueError(f"device lacks c5_jobs_v1: {device_id}")
            if isinstance(value, tuple) and len(value) == 2:
                token, request = value
            else:
                token, request = secrets.token_urlsafe(12), value
            if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
                raise ValueError(f"invalid job token for {device_id}")
            request = validate_job_request(
                request, boot_id=session.boot_id or None,
                limits=limits_for_operation(entry.device_info if entry else session.device_info, request["op"]),
            )
            prepared[device_id] = (token, request)
        gid = group_id or secrets.token_urlsafe(9)
        if gid in self.groups:
            raise ValueError("group_id already exists")
        return JobGroup(gid, prepared, {device_id: "queued" for device_id in prepared}, self.clock())

    def start_group(self, jobs: Mapping[str, Any], *, group_id: str | None = None) -> str | None:
        """Prevalidate all members, then start or queue behind acknowledged stops."""
        try:
            group = self._prepare(jobs, group_id)
        except (ValueError, TypeError) as exc:
            placeholder = JobGroup(group_id or "", {}, error=str(exc))
            self.on_event("rejected", placeholder, {"error": str(exc)})
            return None
        if self._pending is not None:
            self.on_event("rejected", group, {"error": "another replacement is awaiting stop acknowledgements"})
            return None
        active_matches = []
        for device_id, (token, request) in group.jobs.items():
            session = self.sessions[device_id]
            if session.job_token == token:
                if session.job_request != request:
                    self.on_event("rejected", group, {"error": "token reused with a different request",
                                                       "device_id": device_id})
                    return None
                active_matches.append(self._device_group.get(device_id))
        if active_matches:
            if len(active_matches) == len(group.jobs) and len(set(active_matches)) == 1 \
                    and active_matches[0] in self.groups:
                existing_id = active_matches[0]
                self.on_event("idempotent", self.groups[existing_id], {"requested_group": group.group_id})
                return existing_id
            self.on_event("rejected", group, {"error": "partial or unmanaged idempotent group retry"})
            return None
        external_busy = [device_id for device_id in group.jobs
                         if self.sessions[device_id].active and device_id not in self._device_group]
        if external_busy:
            self.on_event("rejected", group, {
                "error": "device has an unmanaged foreground job",
                "devices": sorted(external_busy),
            })
            return None
        # Replacements stop only foreground jobs on devices required by the new
        # plan. Unrelated members of another group are never commandeered.
        old_members = {device_id for device_id in group.jobs
                       if self.sessions[device_id].active and device_id in self._device_group}
        if old_members:
            # Stop requests are issued only after every new request has passed
            # validation. No replacement start is sent until all old stops ack.
            try:
                old_tokens: dict[str, str] = {}
                for device_id in old_members:
                    token = self.sessions[device_id].job_token
                    if not token:
                        raise RuntimeError(f"cannot prove current owner on {device_id}")
                    old_tokens[device_id] = token
                for device_id in old_members:
                    if not self.sessions[device_id].stop_job():
                        raise RuntimeError(f"no active job to stop on {device_id}")
            except Exception as exc:
                group.error = f"stop request failed: {exc}"
                group.requires_user_start = True
                self.on_event("stop_failed", group, {"error": group.error})
                return None
            self._pending = (group, old_tokens, self.clock())
            self.on_event("waiting_for_stop", group, {"devices": sorted(old_members)})
            return group.group_id
        self._launch(group)
        return group.group_id

    def _launch(self, group: JobGroup) -> None:
        self.groups[group.group_id] = group
        self.active_group = group.group_id
        attempted: list[str] = []
        try:
            for device_id, (token, request) in group.jobs.items():
                attempted.append(device_id)
                self._device_group[device_id] = group.group_id
                group.states[device_id] = "starting"
                group.uncertain_since[device_id] = self.clock()
                self.sessions[device_id].start_job(token, request)
        except Exception as exc:
            group.error = f"start uncertain: {exc}"
            group.requires_user_start = True
            for device_id in attempted:
                session = self.sessions[device_id]
                try:
                    session.query_status()
                except Exception:
                    # Status may fail while the start write has reached the
                    # device. Always make an independent compensating stop.
                    pass
                try:
                    stopped = session.stop_job()
                except Exception:
                    stopped = False
                group.states[device_id] = "uncertain_stopping" if stopped else "uncertain"
            self.on_event("start_uncertain", group, {"error": group.error, "devices": attempted})
            return
        self.on_event("started_pending", group, {"devices": sorted(group.jobs)})

    def stop_group(self, group_id: str | None = None) -> bool:
        group_id = group_id or self.active_group
        group = self.groups.get(group_id) if group_id else None
        if group is None:
            return False
        failures = []
        for device_id in group.jobs:
            session = self.sessions.get(device_id)
            if session and session.active:
                try:
                    if not session.stop_job():
                        failures.append(device_id)
                except Exception:
                    failures.append(device_id)
        if failures:
            self._report_stop_failure(group, failures, "stop request failed")
            return False
        self.on_event("stopping", group, {"devices": sorted(group.jobs)})
        return True

    def _report_stop_failure(self, group: JobGroup, device_ids: list[str] | set[str], reason: str) -> None:
        new_failures = set(device_ids) - group.stop_failures
        if not new_failures:
            return
        group.stop_failures.update(new_failures)
        group.error = f"{reason} for " + ", ".join(sorted(new_failures))
        group.requires_user_start = True
        self.on_event("stop_failed", group, {"error": group.error,
                                               "devices": sorted(new_failures)})

    def handle_event(self, device_id: str, record: Mapping[str, Any], *, compensate_peers: bool = True) -> None:
        """Consume a validated DeviceSession event and advance group barriers."""
        device_id = str(device_id).upper()
        kind, state = record.get("kind"), record.get("state")
        if kind not in ("started", "stopped", "error", "status", "heartbeat"):
            return
        group_id = self._device_group.get(device_id)
        group = self.groups.get(group_id) if group_id else None
        if group:
            if record.get("session") != group.jobs[device_id][0]:
                return
            if kind == "error" or state == "error":
                group.states[device_id] = "error"
                group.error = str(record.get("error", record.get("message", "device job error")))[:256]
                group.requires_user_start = True
                # An error invalidates the group even while cleanup is still
                # running. Its owner remains reserved until a terminal result.
                # A failed predecessor cannot release a replacement barrier.
                if self._pending is not None and device_id in self._pending[1]:
                    self._abort_pending(device_id, "predecessor reported an error")
                # A start error may race a start acknowledgement; query and
                # send a compensating stop independently for every member.
                for member in group.jobs:
                    if not compensate_peers and member != device_id:
                        continue
                    member_session = self.sessions.get(member)
                    if not member_session or not member_session.active:
                        continue
                    try:
                        member_session.query_status()
                    except Exception:
                        pass
                    try:
                        stopped = member_session.stop_job()
                    except Exception:
                        stopped = False
                    group.states[member] = "uncertain_stopping" if stopped else "uncertain"
                self.on_event("member_error", group, {"device_id": device_id, "error": group.error})
                return
            if state in ("running", "reporting") or (
                    kind == "started" and state not in (
                        "starting", "stopping", "stopped", "error")):
                group.states[device_id] = state or "running"
                group.uncertain_since.pop(device_id, None)
                self.on_event("member_running", group, {"device_id": device_id, "record": dict(record)})
            elif state == "starting":
                # Firmware may acknowledge an accepted job before its radio,
                # callback, and worker are usable. Preserve pending ownership;
                # only a later running/reporting record announces readiness.
                group.states[device_id] = "starting"
            elif kind == "stopped" or state == "stopped":
                group.states[device_id] = "stopped"
                group.uncertain_since.pop(device_id, None)
                self._device_group.pop(device_id, None)
                self.on_event("member_stopped", group, {"device_id": device_id, "record": dict(record)})
        if self._pending is not None:
            pending, waiting, _since = self._pending
            expected_token = waiting.get(device_id)
            if (expected_token and record.get("session") == expected_token
                    and (kind == "stopped" or state == "stopped")):
                waiting.pop(device_id, None)
                if not waiting:
                    self._pending = None
                    for old_id in list(self._device_group):
                        if old_id not in pending.jobs:
                            self._device_group.pop(old_id, None)
                    self._launch(pending)

    def _abort_pending(self, device_id: str, reason: str) -> None:
        if self._pending is None:
            return
        pending, _waiting, _since = self._pending
        self._pending = None
        pending.error = f"replacement aborted: {reason} on {device_id}"
        pending.requires_user_start = True
        self.on_event("replacement_aborted", pending, {
            "device_id": device_id, "error": pending.error,
        })

    def device_disconnected(self, device_id: str, *, stop_peers: bool = True) -> None:
        """Stop surviving peers and require an explicit user restart after loss."""
        device_id = str(device_id).upper()
        group_id = self._device_group.get(device_id)
        group = self.groups.get(group_id) if group_id else None
        if self._pending and device_id in self._pending[1]:
            self._abort_pending(device_id, "device disconnected during replacement")
        if group is None:
            self._device_group.pop(device_id, None)
            return
        group.error = f"device disconnected: {device_id}"
        group.requires_user_start = True
        group.states[device_id] = "disconnected"
        self._device_group.pop(device_id, None)
        for peer in group.jobs if stop_peers else ():
            if peer == device_id:
                continue
            session = self.sessions.get(peer)
            if session and session.active:
                try:
                    session.stop_job()
                except Exception:
                    pass
        self.on_event("group_interrupted" if stop_peers else "passive_interrupted", group, {
            "device_id": device_id, "requires_user_start": True,
        })

    def tick(self) -> None:
        now = self.clock()
        if self._pending and now - self._pending[2] >= 7.0:
            pending, waiting, _since = self._pending
            self._pending = None
            pending.error = "stop acknowledgement timed out; replacement was not started"
            pending.requires_user_start = True
            self.on_event("replacement_timeout", pending, {"devices": sorted(waiting)})
        for device_id, session in self.sessions.items():
            try:
                session.tick()
            except Exception as exc:
                self.device_disconnected(device_id)
                continue
            group_id = self._device_group.get(device_id)
            group = self.groups.get(group_id) if group_id else None
            if group and (getattr(session, "_stop_timeout_reported", False)
                          or str(getattr(session, "last_error", "")).startswith(
                              ("job_stop send failed", "job stop acknowledgement timed out"))):
                reason = ("job stop acknowledgement timed out" if
                          getattr(session, "_stop_timeout_reported", False) else
                          "job_stop send failed")
                self._report_stop_failure(group, {device_id}, reason)
            since = group.uncertain_since.get(device_id) if group else None
            if since is not None and now - since >= 7.0:
                if now - getattr(session, "last_status_query_at", 0.0) >= 7.0:
                    try:
                        session.query_status()
                    except Exception:
                        # A failed status query does not prove the endpoint or
                        # possibly-started job is gone.
                        pass
                if now - since >= 14.0:
                    try:
                        stopped = session.stop_job()
                    except Exception:
                        stopped = False
                    group.states[device_id] = "uncertain_stopping" if stopped else "uncertain"
                    group.error = f"start acknowledgement missing: {device_id}"
                    group.requires_user_start = True
                    group.uncertain_since[device_id] = now + 3600
                    self.on_event("compensating_stop", group, {"device_id": device_id})
