import json
import threading
import time
from types import SimpleNamespace

from watchdogs.pwnagotchi_bridge import PwnagotchiBridge


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class _FakeSocket:
    def __init__(self, replies, requests, hold_open=False):
        self.replies = replies
        self.requests = requests
        self.hold_open = hold_open
        self.incoming = bytearray()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def settimeout(self, _timeout):
        pass

    def setblocking(self, _blocking):
        pass

    def connect(self, _path):
        pass

    def sendall(self, payload):
        request = json.loads(payload)
        self.requests.append(request)
        if request["op"] == "subscribe":
            messages = self.replies.pop(0) if self.replies else []
            self.incoming.extend(
                b"".join((json.dumps(item) + "\n").encode() for item in messages)
            )
        elif request["op"] == "snapshot":
            data = {"interface": "wlan2", "backend": "bettercap"}
            self.incoming.extend(
                (json.dumps({"type": "result", "data": data}) + "\n").encode()
            )
        else:
            self.incoming.extend(
                (
                    json.dumps(
                        {"type": "result", "data": {"enabled": request.get("enabled")}}
                    )
                    + "\n"
                ).encode()
            )

    def recv(self, size):
        if not self.incoming:
            return b""
        chunk = bytes(self.incoming[:size])
        del self.incoming[:size]
        return chunk


def _mock_socket(monkeypatch, replies, hold_open=False):
    requests = []
    connections = []

    def factory(*_a, **_k):
        client = _FakeSocket(replies, requests, hold_open=hold_open)
        connections.append(client)
        return client

    def select_readers(readers, _writers, _errors, timeout):
        client = readers[0]
        if client.incoming or not client.hold_open:
            return readers, [], []
        time.sleep(min(timeout, 0.02))
        return [], [], []

    monkeypatch.setattr(
        "watchdogs.pwnagotchi_bridge.socket.socket",
        factory,
    )
    monkeypatch.setattr(
        "watchdogs.pwnagotchi_bridge.select.select",
        select_readers,
    )
    return requests, connections


def _missing_cli(*_args, **_kwargs):
    return SimpleNamespace(returncode=1, stdout="", stderr="missing")


def test_subscription_updates_snapshot_and_bounded_newest_first_events(monkeypatch):
    messages = [
        {
            "type": "snapshot",
            "data": {
                "backend": "bettercap",
                "interface": "wlan1",
                "ui": {"face": "(•‿‿•)"},
            },
        },
        {
            "type": "event",
            "event": "action",
            "ts": 100,
            "data": {"action": "deauth", "result": "submitted", "bssid": "aa:bb"},
        },
        {
            "type": "event",
            "event": "epoch",
            "ts": 101,
            "data": {"number": 4, "stats": {"reward": 1.25, "num_handshakes": 2}},
        },
    ]
    requests, _connections = _mock_socket(monkeypatch, [messages])
    bridge = PwnagotchiBridge(
        socket_path="/mock/control.sock",
        poll_interval=30,
        reconnect_delay=0.1,
        event_limit=2,
        runner=_missing_cli,
    )
    try:
        assert _wait_for(lambda: len(bridge.state()["events"]) == 2)
        view = bridge.state()
        assert {"v": 1, "op": "subscribe"} in requests
        assert view["snapshot"]["interface"] == "wlan1"
        assert view["events"][0]["kind"] == "brain"
        assert view["events"][1]["kind"] == "decision"
        assert view["events"][1]["text"] == "Deauth submitted: aa:bb"
        assert view["unread"] == 2
        bridge.mark_read()
        assert bridge.state()["unread"] == 0
    finally:
        bridge.close()


def test_subscription_resync_fetches_fresh_snapshot(monkeypatch):
    requests, _connections = _mock_socket(
        monkeypatch,
        [
            [
                {
                    "type": "event",
                    "event": "resync_required",
                    "ts": 3,
                    "data": {"dropped": 4},
                }
            ]
        ],
    )
    bridge = PwnagotchiBridge(
        socket_path="/mock/control.sock",
        poll_interval=30,
        reconnect_delay=0.1,
        runner=_missing_cli,
    )
    try:
        assert _wait_for(
            lambda: any(x["kind"] == "sync" for x in bridge.state()["events"])
        )
        assert _wait_for(
            lambda: (bridge.state()["snapshot"] or {}).get("interface") == "wlan2"
        )
        assert {"v": 1, "op": "snapshot"} in requests
    finally:
        bridge.close()


def test_submit_runs_service_action_and_rejects_second_busy_action():
    calls = []

    def runner(args, **kwargs):
        calls.append(list(args))
        if args[-2:] == ["status", "--json"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"backend": "bettercap", "services": []}),
                stderr="",
            )
        if args[-3:] == ["adapter", "list", "--json"]:
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        if args[-3:] == ["adapter", "status", "--json"]:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"interface": "wlan1"}), stderr=""
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    bridge = PwnagotchiBridge(
        socket_path="/missing/pwnagotchi.sock",
        poll_interval=30,
        reconnect_delay=0.1,
        runner=runner,
    )
    try:
        assert bridge.submit("start") is True
        assert bridge.submit("stop") is False
        assert _wait_for(lambda: not bridge.state()["busy"])
        assert any(call[-1] == "start" for call in calls)
        assert bridge.state()["last_result"] == "Pwnagotchi start requested"
    finally:
        bridge.close()


def test_deauth_submit_uses_versioned_socket_set(monkeypatch):
    bridge = PwnagotchiBridge(
        socket_path="/missing/pwnagotchi.sock",
        poll_interval=30,
        reconnect_delay=0.1,
        runner=_missing_cli,
    )
    requests = []
    monkeypatch.setattr(
        bridge,
        "_request",
        lambda message, timeout=2: requests.append(message) or {"type": "result"},
    )
    try:
        assert bridge.submit("deauth", False)
        assert _wait_for(lambda: not bridge.state()["busy"])
        assert requests == [
            {"v": 1, "op": "set", "setting": "deauth", "enabled": False}
        ]
        assert bridge.state()["last_result"] == "Deauth disabled"
    finally:
        bridge.close()


def test_failed_adapter_command_is_reported():
    def runner(args, **kwargs):
        if args[-3:] == ["adapter", "status", "--json"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"interface": "wlan1", "backend": "bettercap"}),
                stderr="",
            )
        if args[-2:] == ["status", "--json"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=2, stdout="", stderr="adapter restart failed")

    bridge = PwnagotchiBridge(
        socket_path="/missing/pwnagotchi.sock",
        poll_interval=30,
        reconnect_delay=0.1,
        runner=runner,
    )
    try:
        assert not bridge.submit("adapter", "bad name")
        assert bridge.submit("adapter", "wlan2")
        assert _wait_for(lambda: not bridge.state()["busy"])
        assert "adapter restart failed" in bridge.state()["error"]
    finally:
        bridge.close()


def test_poll_uses_pwnagotchi_service_and_interfaces_response():
    pwn_service_active = {"value": False}

    def runner(args, **kwargs):
        if args[-3:] == ["adapter", "status", "--json"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"interface": "wlan1", "backend": "bettercap"}),
                stderr="",
            )
        if args[-2:] == ["status", "--json"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "backend": "bettercap",
                        "services": [
                            {
                                "unit": "pwnagotchi-bettercap.service",
                                "active": True,
                                "enabled": True,
                            },
                            {
                                "unit": "pwnagotchi-uconsole.service",
                                "active": pwn_service_active["value"],
                                "enabled": pwn_service_active["value"],
                            },
                        ],
                    }
                ),
                stderr="",
            )
        if args[-3:] == ["adapter", "list", "--json"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "interfaces": [
                            {"name": "wlan0", "mode": "managed"},
                            {"name": "wlan1", "mode": "monitor"},
                        ]
                    }
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    bridge = PwnagotchiBridge(
        socket_path="/missing/pwnagotchi.sock",
        poll_interval=30,
        reconnect_delay=0.1,
        runner=runner,
    )
    bridge.close()
    bridge._poll_cli()
    view = bridge.state()
    assert view["service_active"] is False
    assert view["service_enabled"] is False
    assert view["adapters"] == [
        {"name": "wlan0", "mode": "managed"},
        {"name": "wlan1", "mode": "monitor"},
    ]
    assert view["interface"] == "wlan1"
    pwn_service_active["value"] = True
    bridge._poll_cli()
    assert bridge.state()["service_active"] is True
    assert bridge.state()["service_enabled"] is True


def test_live_events_merge_snapshot_fields_without_losing_other_radio_data():
    bridge = PwnagotchiBridge(
        socket_path="/missing/pwnagotchi.sock",
        poll_interval=30,
        reconnect_delay=0.1,
        runner=_missing_cli,
    )
    bridge.close()
    bridge._apply_snapshot(
        {
            "radio": {"channel": 6, "actions": {"deauth_submitted": 2}},
            "capture": {"handshakes_run": 3},
            "controls": {"deauth": True, "associate": False},
        }
    )
    bridge._handle_message(
        {
            "type": "event",
            "event": "ap_snapshot",
            "ts": 100,
            "data": {"eligible_ap_count": 7, "aps": [{"ssid": "test"}]},
        }
    )
    bridge._handle_message(
        {
            "type": "event",
            "event": "epoch",
            "ts": 101,
            "data": {"number": 9, "stats": {"reward": 2.5}},
        }
    )
    bridge._handle_message(
        {
            "type": "event",
            "event": "handshake",
            "ts": 102,
            "data": {"bssid": "aa:bb", "file": "capture.pcap"},
        }
    )
    bridge._handle_message(
        {
            "type": "event",
            "event": "control",
            "ts": 103,
            "data": {"setting": "deauth", "enabled": False, "persistent": True},
        }
    )
    bridge._handle_message(
        {
            "type": "event",
            "event": "c5",
            "ts": 104,
            "data": {"mode": "hs_sniff", "channel": "6", "ap_count": 4, "frames": 50},
        }
    )
    snapshot = bridge.state()["snapshot"]
    assert snapshot["radio"]["channel"] == "6"
    assert snapshot["radio"]["actions"]["deauth_submitted"] == 2
    assert snapshot["radio"]["eligible_ap_count"] == 7
    assert snapshot["epoch"]["number"] == 9
    assert snapshot["capture"]["handshakes_run"] == 4
    assert snapshot["capture"]["last_handshake"]["bssid"] == "aa:bb"
    assert snapshot["controls"]["deauth"] is False
    assert snapshot["controls"]["persistent"] is True
    assert snapshot["radio"]["frames"] == 50
    assert snapshot["c5"]["ap_count"] == 4


def test_mood_snapshot_and_events_are_persistent_and_logged_once():
    bridge = PwnagotchiBridge(
        socket_path="/missing/pwnagotchi.sock",
        poll_interval=30,
        reconnect_delay=0.1,
        runner=_missing_cli,
    )
    bridge.close()
    bridge._apply_snapshot(
        {
            "mood": {
                "name": "bored",
                "reason": "No new networks for 15 epochs",
                "since": 100,
            }
        }
    )
    bridge._handle_message(
        {
            "type": "event",
            "event": "mood",
            "ts": 101,
            "data": {
                "name": "bored",
                "reason": "No new networks for 15 epochs",
                "since": 100,
            },
        }
    )
    bridge._handle_message(
        {
            "type": "event",
            "event": "mood",
            "ts": 200,
            "data": {"name": "happy", "reason": "New APs found", "since": 200},
        }
    )
    view = bridge.state()
    assert view["snapshot"]["mood"] == {
        "name": "happy",
        "reason": "New APs found",
        "since": 200,
    }
    mood_events = [event for event in view["events"] if event["kind"] == "mood"]
    assert [event["text"] for event in mood_events] == [
        "HAPPY: New APs found",
        "BORED: No new networks for 15 epochs",
    ]


def test_old_snapshot_without_mood_does_not_invent_mood_event():
    bridge = PwnagotchiBridge(
        socket_path="/missing/pwnagotchi.sock",
        poll_interval=30,
        reconnect_delay=0.1,
        runner=_missing_cli,
    )
    bridge.close()
    bridge._apply_snapshot({"ui": {"face": "(◕‿‿◕)"}})
    assert bridge.state()["events"] == []


def test_repeated_ui_and_ap_events_do_not_evict_decisions_or_epochs():
    bridge = PwnagotchiBridge(
        socket_path="/missing/pwnagotchi.sock",
        poll_interval=30,
        reconnect_delay=0.1,
        runner=_missing_cli,
    )
    bridge.close()
    for timestamp in (100, 101, 102, 103):
        bridge._handle_message(
            {
                "type": "event",
                "event": "ui",
                "ts": timestamp,
                "data": {"ui": {"face": "(•‿‿•)", "status": "Scanning"}},
            }
        )
        bridge._handle_message(
            {
                "type": "event",
                "event": "ap_snapshot",
                "ts": timestamp,
                "data": {"eligible_ap_count": 5, "aps": []},
            }
        )
    bridge._handle_message(
        {
            "type": "event",
            "event": "action",
            "ts": 104,
            "data": {"action": "deauth", "result": "submitted", "bssid": "aa:bb"},
        }
    )
    bridge._handle_message(
        {
            "type": "event",
            "event": "epoch",
            "ts": 105,
            "data": {"number": 1, "stats": {"reward": 0.5}},
        }
    )
    events = bridge.state()["events"]
    assert [event["kind"] for event in events] == [
        "brain",
        "decision",
        "networks",
        "status",
    ]


def test_subscription_stays_open_while_control_worker_runs_actions(monkeypatch):
    requests, connections = _mock_socket(
        monkeypatch,
        [
            [
                {"type": "snapshot", "data": {"running": True, "backend": "bettercap"}},
                {
                    "type": "event",
                    "event": "action",
                    "ts": 100,
                    "data": {
                        "action": "associate",
                        "result": "submitted",
                        "bssid": "aa:bb",
                    },
                },
            ]
        ],
        hold_open=True,
    )
    commands = []
    poll_started = threading.Event()
    poll_release = threading.Event()

    def runner(args, **kwargs):
        commands.append(list(args))
        if args[-2:] == ["status", "--json"]:
            poll_started.set()
            poll_release.wait(1.0)
            return SimpleNamespace(returncode=0, stdout='{"services":[]}', stderr="")
        if args[-3:] == ["adapter", "list", "--json"]:
            return SimpleNamespace(returncode=0, stdout='{"interfaces":[]}', stderr="")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    bridge = PwnagotchiBridge(
        socket_path="/mock/control.sock",
        poll_interval=30,
        reconnect_delay=0.1,
        runner=runner,
    )
    try:
        assert _wait_for(lambda: bridge.state()["connected"])
        assert poll_started.wait(1.0)
        assert _wait_for(
            lambda: any(
                event["kind"] == "decision" for event in bridge.state()["events"]
            )
        )
        assert not poll_release.is_set()
        poll_release.set()
        assert bridge.submit("start")
        assert _wait_for(
            lambda: bridge.state()["last_result"] == "Pwnagotchi start requested"
        )
        assert bridge.state()["connected"] is True
        assert len(connections) == 1
        assert any(command[-1] == "start" for command in commands)
        assert {"v": 1, "op": "subscribe"} in requests
    finally:
        poll_release.set()
        bridge.close()


def test_close_waits_for_an_in_flight_service_action():
    action_started = threading.Event()
    action_release = threading.Event()
    action_finished = threading.Event()

    def runner(args, **kwargs):
        if args[-1] == "start":
            action_started.set()
            action_release.wait(1.0)
            action_finished.set()
        if args[-2:] == ["status", "--json"]:
            return SimpleNamespace(returncode=0, stdout='{"services":[]}', stderr="")
        if args[-3:] == ["adapter", "list", "--json"]:
            return SimpleNamespace(returncode=0, stdout='{"interfaces":[]}', stderr="")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    bridge = PwnagotchiBridge(
        socket_path="/missing/pwnagotchi.sock",
        poll_interval=30,
        reconnect_delay=0.1,
        runner=runner,
    )
    assert bridge.submit("start")
    assert action_started.wait(1.0)
    releaser = threading.Timer(0.1, action_release.set)
    releaser.start()
    bridge.close()
    assert action_finished.is_set()
    assert not bridge._thread.is_alive()
