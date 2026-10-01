import json
import os
import stat

import pytest

from watchdogs.reticulum_config import ReticulumProfile, save_profile
from watchdogs.reticulum_manager import (
    EVENT_LIMIT,
    IPC_MAX_PACKET,
    ReticulumManager,
)


def test_release_waits_for_meshtastic_protocol_readiness(tmp_path):
    events = []

    class Controller:
        def release_mode(self):
            events.append(("release",))

        def wait_for_mode_ready(self, mode, *, timeout):
            events.append(("ready", mode, timeout))

    manager = ReticulumManager(
        tmp_path, sx1262_controller=Controller())
    manager._manager_lease_active = True

    manager._restore_service_after_failed_start()

    assert events == [
        ("release",), ("ready", "meshtastic", 10.0)]
    assert manager._manager_lease_active is False


def test_decode_rejects_malformed_and_oversized_envelopes():
    good = json.dumps({
        "v": 1, "type": "event", "name": "ready", "payload": {},
    }).encode()
    assert ReticulumManager._decode_packet(good)["name"] == "ready"
    for packet in (
        b"", b"not-json", b"{}",
        json.dumps({"v": 2, "type": "event", "name": "x",
                    "payload": {}}).encode(),
        json.dumps({"v": 1, "type": "event", "name": "unknown",
                    "payload": {}}).encode(),
        json.dumps({"v": 1, "type": "reply", "name": "status",
                    "payload": {}}).encode(),
        b"x" * (IPC_MAX_PACKET + 1),
    ):
        with pytest.raises((ValueError, json.JSONDecodeError)):
            ReticulumManager._decode_packet(packet)


def test_runtime_socket_directory_is_private(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    manager = ReticulumManager(tmp_path / "app", runtime_dir=runtime)
    private = manager._validated_runtime_root()
    assert private == runtime / "watchdogs"
    assert stat.S_IMODE(private.stat().st_mode) == 0o700


def test_unsafe_runtime_directory_is_rejected(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o777)
    os.chmod(runtime, 0o777)
    manager = ReticulumManager(tmp_path / "app", runtime_dir=runtime)
    with pytest.raises(RuntimeError):
        manager._validated_runtime_root()


def test_unconfirmed_profile_never_spawns(tmp_path):
    manager = ReticulumManager(tmp_path, process_factory=lambda *_a, **_k: None)
    assert manager.start(ReticulumProfile(confirmed=False)) is False
    event, payload = manager.poll_events()[0]
    assert event == "error"
    assert payload["code"] == "profile_unconfirmed"


def test_send_validation_and_parent_correlation(tmp_path):
    manager = ReticulumManager(tmp_path)
    manager._state = "ready"
    manager._socket = object()
    sent = []
    manager._send_request = lambda name, payload, **_kw: sent.append(
        (name, payload)) or "request"
    correlation = manager.send_text("ab" * 16, "hello")
    assert correlation
    assert sent[0][0] == "send_text"
    assert sent[0][1]["correlation_id"] == correlation
    event, payload = manager.poll_events()[0]
    assert event == "message"
    assert payload["state"] == "queued"
    assert payload["text"] == "hello"
    assert manager.send_text("AB" * 16, "hello") is None
    assert manager.send_text("ab" * 16, "x" * 121) is None


def test_event_queue_is_bounded_and_reports_overflow(tmp_path):
    manager = ReticulumManager(tmp_path)
    for index in range(EVENT_LIMIT + 20):
        manager._emit("radio_status", {"index": index})
    assert len(manager._events) <= EVENT_LIMIT
    assert len(manager.poll_events(EVENT_LIMIT)) <= EVENT_LIMIT
    assert manager.event_drops > 0


def test_profile_path_must_be_private_owned_state_file(tmp_path):
    manager = ReticulumManager(tmp_path)
    path = save_profile(tmp_path, ReticulumProfile(confirmed=True))
    assert manager._validated_profile_file(path) == path
    path.chmod(0o644)
    with pytest.raises(Exception):
        manager._validated_profile_file(path)


def test_failed_profile_and_rollback_emit_fatal_barrier(tmp_path, monkeypatch):
    old = ReticulumProfile(confirmed=True, display_name="old")
    save_profile(tmp_path, old)
    manager = ReticulumManager(tmp_path)
    manager._state = "ready"
    starts = iter((True, True))
    readiness = iter((False, False))
    monkeypatch.setattr(manager, "start", lambda *_a, **_k: next(starts))
    monkeypatch.setattr(manager, "wait_ready", lambda *_a, **_k: next(readiness))
    monkeypatch.setattr(manager, "stop", lambda *_a, **_k: True)

    assert not manager.restart_with_profile(
        old.with_updates(display_name="candidate"), timeout=0.1)

    assert any(name == "error"
               and payload.get("code") == "profile_rollback_failed"
               and payload.get("fatal")
               for name, payload in manager.poll_events(EVENT_LIMIT))


def test_rejected_send_reply_marks_correlation_failed(tmp_path):
    manager = ReticulumManager(tmp_path)
    correlation = "correlation"
    manager._outbound_pending.add(correlation)
    manager._pending_requests["request"] = {
        "name": "send_text", "correlation_id": correlation,
    }

    manager._handle_message({
        "v": 1,
        "type": "reply",
        "request_id": "request",
        "name": "send_text",
        "payload": {"error": "identity unknown"},
    })

    events = manager.poll_events(EVENT_LIMIT)
    status = next(payload for name, payload in events
                  if name == "outbound_status")
    assert status["correlation_id"] == correlation
    assert status["state"] == "failed"
    assert correlation not in manager._outbound_pending


def test_propagation_sync_requires_configured_ready_profile(tmp_path):
    manager = ReticulumManager(tmp_path)
    manager._socket = object()
    sent = []
    manager._send_request = lambda name, payload, **_kw: sent.append(
        (name, payload)) or "request"

    manager._state = "ready"
    manager._profile = ReticulumProfile(confirmed=True)
    assert not manager.sync_propagation()

    manager._profile = ReticulumProfile(
        confirmed=True,
        propagation_node_hash="90ab9d448f17f3a121dc0f1230af39be",
    )
    manager._propagation_node = manager._profile.propagation_node_hash
    assert manager.sync_propagation(25)
    assert sent[-1] == ("sync_propagation", {"max_messages": 25})
    assert manager.cancel_propagation_sync()
    assert sent[-1] == ("cancel_propagation", {})
    assert not manager.sync_propagation(201)


def test_propagation_status_updates_visible_manager_state(tmp_path):
    manager = ReticulumManager(tmp_path)
    node_hash = "90ab9d448f17f3a121dc0f1230af39be"
    manager._handle_message({
        "v": 1,
        "type": "event",
        "name": "propagation_status",
        "payload": {
            "node_hash": node_hash,
            "state": "receiving",
            "progress": 0.5,
        },
    })
    assert manager.propagation_node == node_hash
    assert manager.propagation_state == "receiving"
    assert manager.propagation_progress == 0.5


def test_stored_propagated_message_is_terminal_for_parent(tmp_path):
    manager = ReticulumManager(tmp_path)
    manager._outbound_pending.add("correlation")
    manager._handle_message({
        "v": 1,
        "type": "event",
        "name": "outbound_status",
        "payload": {
            "correlation_id": "correlation",
            "message_hash": "01",
            "state": "stored",
            "delivery_method": "propagated",
        },
    })
    assert "correlation" not in manager._outbound_pending
