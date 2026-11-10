"""Pig Detector SDR profiles: ownership, bounded events and metadata only."""

import io
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from watchdogs import sdr_manager


@pytest.fixture(autouse=True)
def no_readsb_service_by_default(monkeypatch):
    original_which = sdr_manager.shutil.which

    def which(name):
        return None if name == "systemctl" else original_which(name)

    monkeypatch.setattr(sdr_manager.shutil, "which", which)


def _mock_readsb(monkeypatch, initial="active", *, fail_stop=False,
                 fail_start=False, deactivate_on_failed_stop=False,
                 timeout_stop=False):
    state = {"value": initial}
    calls = []

    monkeypatch.setattr(
        sdr_manager.shutil, "which",
        lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)

    def run(command, **kwargs):
        action = command[1]
        calls.append(action)
        if action == "show":
            if state["value"] == "missing":
                stdout = "LoadState=not-found\nActiveState=inactive\n"
            else:
                stdout = f"LoadState=loaded\nActiveState={state['value']}\n"
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        if action == "stop":
            if fail_stop:
                if deactivate_on_failed_stop:
                    state["value"] = "inactive"
                return SimpleNamespace(
                    returncode=1, stdout="", stderr="stop rejected")
            state["value"] = "inactive"
            if timeout_stop:
                raise sdr_manager.subprocess.TimeoutExpired(command, 12)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if action == "start":
            if not fail_start:
                state["value"] = "active"
            return SimpleNamespace(
                returncode=0 if not fail_start else 1,
                stdout="", stderr="start rejected" if fail_start else "")
        raise AssertionError(command)

    monkeypatch.setattr(sdr_manager.subprocess, "run", run)
    return state, calls


def test_watchlist_validates_frequency_bandwidth_and_bounds():
    channels = sdr_manager.SDRManager._validated_channels([
        {"frequency_hz": 155_000_000, "bandwidth_hz": 25_000, "label": "VHF"},
    ])
    assert channels[0]["frequency_hz"] == 155_000_000
    assert channels[0]["label"] == "VHF"

    with pytest.raises(ValueError):
        sdr_manager.SDRManager._validated_channels([
            {"frequency_hz": 2_000_000_000},
        ])
    with pytest.raises(ValueError):
        sdr_manager.SDRManager._validated_channels([])


def test_rtl_power_csv_fixture_and_per_channel_threshold(monkeypatch):
    manager = sdr_manager.SDRManager()
    channel = manager._validated_channels([
        {"frequency_hz": 155_000_000, "bandwidth_hz": 25_000},
    ])[0]
    manager._pig_channels = [channel]
    manager._pig_channel_state[channel["key"]] = {}

    assert manager._parse_rtl_power_line(
        "2026-10-14, 12:00:00, 154987500, 155012500, 3125, 16, "
        "-101.0, -99.0, -98.0, -100.0") == (-98.0, -99.5, 16)
    assert manager._parse_rtl_power_line("not,csv") is None

    manager._record_pig_power(channel, -100.0, 16)
    manager._record_pig_power(channel, -88.0, 16)
    events = manager.poll_events()
    rf = [payload for kind, payload in events if kind == "pig_rf"]
    assert len(rf) == 2
    assert rf[0]["evidence"] == "noise_measurement"
    assert rf[1]["above_threshold"] is True
    assert rf[1]["evidence"] == "rf_activity"
    assert rf[1]["frequency_hz"] == 155_000_000
    assert rf[1]["last_visit"]
    assert rf[0]["revisit_seconds"] is None
    assert rf[1]["revisit_seconds"] is not None

    # A narrow carrier in one bin remains visible even when most bins are noise.
    manager = sdr_manager.SDRManager()
    manager._pig_channel_state[channel["key"]] = {}
    peak, noise, count = manager._parse_rtl_power_line(
        "2026-10-14,12:00:01,154900000,155100000,12500,16,"
        "-100,-101,-99,-100,-101,-100,-99,-100,-72,-100,-101,-99,-100,-101,-100,-99")
    manager._record_pig_power(channel, peak, count, noise_sample=noise)
    narrow = manager.poll_events()[0][1]
    assert narrow["above_threshold"] is True
    assert narrow["power_db"] == -72
    assert narrow["noise_floor_db"] == -100


def test_pig_event_buffer_is_bounded():
    manager = sdr_manager.SDRManager()
    for index in range(sdr_manager.PIG_EVENT_LIMIT + 20):
        manager._put_pig_event("pig_rf", {"index": index})
    events = manager.poll_events()
    assert len(events) == sdr_manager.PIG_EVENT_LIMIT
    assert events[0][1]["index"] == 20
    assert events[-1][1]["index"] == sdr_manager.PIG_EVENT_LIMIT + 19


def test_raw_pig_log_is_bounded(tmp_path, monkeypatch):
    manager = sdr_manager.SDRManager()
    manager._pig_log = manager._open_pig_log(str(tmp_path), "rf.jsonl")
    monkeypatch.setattr(sdr_manager, "PIG_LOG_LIMIT_BYTES", 30)
    manager._write_pig_log({"power_db": -90})
    manager._write_pig_log({"power_db": -80})
    manager._pig_log.close()
    assert (tmp_path / "rf.jsonl").read_text().count("\n") == 1


def test_rf_start_is_optional_and_refuses_another_sdr_owner(monkeypatch):
    manager = sdr_manager.SDRManager()
    monkeypatch.setattr(sdr_manager, "find_rtl_power", lambda: None)
    assert not manager.start_pig_watchlist([
        {"frequency_hz": 155_000_000},
    ])
    assert manager.poll_events() == [("error", "rtl_power not installed")]

    manager = sdr_manager.SDRManager()
    manager.mode = "433"
    manager.running = True
    popen = Mock()
    monkeypatch.setattr(sdr_manager, "find_rtl_power", lambda: "/usr/bin/rtl_power")
    monkeypatch.setattr(sdr_manager.subprocess, "Popen", popen)
    assert not manager.start_pig_watchlist([{"frequency_hz": 155_000_000}])
    popen.assert_not_called()
    assert "433 is active" in manager.poll_events()[0][1]


def test_rf_start_command_and_cleanup_are_profile_owned(monkeypatch):
    manager = sdr_manager.SDRManager()
    monkeypatch.setattr(sdr_manager, "find_rtl_power", lambda: "/usr/bin/rtl_power")
    thread = Mock()
    monkeypatch.setattr(sdr_manager.threading, "Thread", Mock(return_value=thread))
    assert manager.start_pig_watchlist(
        [{"frequency_hz": 155_000_000, "bandwidth_hz": 25_000, "label": "local VHF"}],
        gain=22, ppm=-3)
    assert manager.mode == "pig_rf"
    thread.start.assert_called_once()
    command_holder = {}

    class Completed:
        pid = 123
        returncode = 0

        def communicate(self, timeout):
            command_holder["communicate_timeout"] = timeout
            manager._pig_stop_event.set()
            return ("2026-10-14,12:00:00,154987500,155012500,3125,16,-101,-99,-98,-100\n", "")

        def poll(self):
            return 0

    def popen(command, **kwargs):
        command_holder["command"] = command
        return Completed()

    monkeypatch.setattr(sdr_manager.subprocess, "Popen", popen)
    manager._stop_event.clear()
    manager._pig_rf_worker("/usr/bin/rtl_power")
    command = command_holder["command"]
    assert command[:2] == ["/usr/bin/rtl_power", "-f"]
    assert "-g" in command and "22.0" in command
    assert "-p" in command and "-3" in command
    assert manager.poll_events()[-1][0] == "pig_rf"
    # No shell or external service command is used by the Pig profile.


def test_existing_profiles_refuse_pig_ownership(monkeypatch):
    manager = sdr_manager.SDRManager()
    manager.mode = "pig_rf"
    manager._pig_rf_proc = object()
    monkeypatch.setattr(sdr_manager, "find_dump1090", lambda: "/usr/bin/dump1090")
    monkeypatch.setattr(sdr_manager.subprocess, "Popen", Mock())
    assert not manager.start_adsb()
    assert "pig_rf is active" in manager.poll_events()[0][1]


def test_p25_parser_accepts_control_metadata_and_rejects_noise():
    parsed = sdr_manager.SDRManager._parse_op25_status(
        '{"sysid":"0x123","wacn":"0xabcde","rfss":2,"site":4,'
        '"tgid":99,"frequency":851.0125}')
    assert parsed["sysid"] == "0x123"
    assert parsed["talkgroup"] == "99"
    assert parsed["frequency_hz"] == 851_012_500
    op25_update = sdr_manager.SDRManager._parse_op25_status(
        '[{"json_type":"trunk_update","0x293":{"sysid":659,'
        '"wacn":765432,"stid":4,"rfid":2,"tsbks":42,"frequency_data":'
        '{"851012500":{"tgids":[12001,12002]}}}}]')
    assert op25_update["site"] == "4"
    assert op25_update["talkgroups"] == [12001, 12002]
    assert op25_update["decoded_blocks"] == 42
    retune = sdr_manager.SDRManager._parse_op25_status(
        '{"json_type":"change_freq","freq":852000000,"tgid":12001}')
    assert retune["frequency_hz"] == 852_000_000
    assert retune["tuned_frequency_hz"] == 852_000_000
    combined = sdr_manager.SDRManager._parse_op25_status(
        '[{"json_type":"trunk_update","0x293":{"sysid":659,"stid":4}},'
        '{"json_type":"change_freq","freq":852000000,"tgid":12001}]')
    assert combined["sysid"] == "659"
    assert combined["tuned_frequency_hz"] == 852_000_000
    assert sdr_manager.SDRManager._parse_op25_status("no status here") is None


def test_p25_stops_if_op25_reports_voice_channel(monkeypatch):
    manager = sdr_manager.SDRManager()
    manager.running = True
    manager.mode = "pig_p25"
    proc = Mock()
    proc.poll.return_value = None
    manager._pig_p25_proc = proc
    manager._pig_p25_control_hz = 851_012_500
    manager._pig_p25_url = "http://127.0.0.1:18080/"
    response = MagicMock()
    response.read.return_value = (
        b'[{"json_type":"trunk_update","0x293":{"sysid":659}},'
        b'{"json_type":"change_freq","freq":852000000}]')
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=None)
    monkeypatch.setattr(sdr_manager.urllib.request, "urlopen", Mock(return_value=response))
    stop = Mock(side_effect=manager._pig_stop_event.set)
    monkeypatch.setattr(manager, "stop", stop)
    manager._pig_p25_worker()
    stop.assert_called_once()
    assert any("left the configured control channel" in message
               for kind, message in manager.poll_events() if kind == "error")


def test_p25_requires_optional_install_and_tuner_ownership(monkeypatch):
    manager = sdr_manager.SDRManager()
    monkeypatch.setattr(sdr_manager, "find_op25_rx", lambda: None)
    assert not manager.start_pig_p25(851_012_500, "Local P25")
    assert manager.poll_events() == [
        ("error", "Pinned OP25 source checkout unavailable; P25 metadata is unavailable")]

    manager = sdr_manager.SDRManager()
    manager.mode = "adsb"
    manager.running = True
    popen = Mock()
    monkeypatch.setattr(sdr_manager, "find_op25_rx", lambda: "/opt/op25/rx.py")
    monkeypatch.setattr(sdr_manager.subprocess, "Popen", popen)
    assert not manager.start_pig_p25(851_012_500, "Local P25")
    popen.assert_not_called()
    assert "adsb is active" in manager.poll_events()[0][1]


def test_p25_launch_has_full_blacklist_no_audio_and_loopback_metadata(monkeypatch):
    manager = sdr_manager.SDRManager()
    monkeypatch.setattr(sdr_manager, "find_op25_rx", lambda: "/opt/op25/rx.py")
    process = Mock()
    process.poll.return_value = None
    popen = Mock(return_value=process)
    monkeypatch.setattr(sdr_manager.subprocess, "Popen", popen)
    thread = Mock()
    monkeypatch.setattr(sdr_manager.threading, "Thread", Mock(return_value=thread))

    assert manager.start_pig_p25(851_012_500, "City P25", gain=30, ppm=2)
    command = popen.call_args.args[0]
    assert popen.call_args.kwargs["cwd"] == "/opt/op25"
    assert command[0:2] == ["/opt/op25/rx.py", "--args"]
    assert "--frequency" in command and "851012500" in command
    assert "--terminal-type" in command
    endpoint = command[command.index("--terminal-type") + 1]
    assert endpoint.startswith("http:127.0.0.1:")
    assert not any("audio" in arg.lower() or arg in ("-V", "-U")
                   for arg in command)
    assert "--gains" in command and "LNA:30" in command
    assert "--freq-corr" in command and "2" in command

    tempdir = manager._pig_p25_tmpdir
    with open(tempdir + "/trunk.tsv", encoding="utf-8") as source:
        tsv = source.read()
    header, row = tsv.rstrip("\n").splitlines()
    assert header.split("\t")[:2] == ["Sysname", "Control Channel List"]
    blacklist_path = row.split("\t")[7]
    assert blacklist_path == tempdir + "/all-talkgroups.tsv"
    with open(blacklist_path, encoding="utf-8") as source:
        assert source.read() == "0\t65535\n"
    assert thread.start.call_count == 2  # stderr drainer and metadata worker
    manager._pig_p25_proc = None  # mocked process has no OS process group
    manager.stop()
    assert not os.path.exists(tempdir)


def test_readsb_lease_pauses_and_restores_only_previously_active_service(monkeypatch):
    manager = sdr_manager.SDRManager()
    state, calls = _mock_readsb(monkeypatch, "active")
    assert manager._acquire_readsb_lease()
    assert state["value"] == "inactive"
    assert manager._readsb_paused is True
    assert "readsb ADS-B feed paused" in " ".join(
        str(data) for kind, data in manager.poll_events() if kind == "status")

    assert manager._release_readsb_lease()
    assert state["value"] == "active"
    assert manager._readsb_paused is False
    assert calls.count("stop") == 1
    assert calls.count("start") == 1


@pytest.mark.parametrize("initial", ["inactive", "failed", "missing"])
def test_readsb_lease_leaves_nonactive_service_alone(monkeypatch, initial):
    manager = sdr_manager.SDRManager()
    state, calls = _mock_readsb(monkeypatch, initial)
    assert manager._acquire_readsb_lease()
    assert not manager._readsb_paused
    assert state["value"] == initial
    assert "stop" not in calls
    assert manager._release_readsb_lease()
    assert "start" not in calls


def test_readsb_query_failure_fails_closed(monkeypatch):
    manager = sdr_manager.SDRManager()
    monkeypatch.setattr(
        sdr_manager.shutil, "which", lambda name: "/usr/bin/systemctl")

    def broken_run(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="bus unavailable")

    monkeypatch.setattr(sdr_manager.subprocess, "run", broken_run)
    assert not manager._acquire_readsb_lease()
    assert not manager._readsb_paused
    assert "cannot query readsb.service" in manager.poll_events()[0][1]


@pytest.mark.parametrize("deactivated", [False, True])
def test_readsb_failed_stop_does_not_claim_service(monkeypatch, deactivated):
    manager = sdr_manager.SDRManager()
    state, calls = _mock_readsb(
        monkeypatch, "active", fail_stop=True,
        deactivate_on_failed_stop=deactivated)
    assert not manager._acquire_readsb_lease()
    assert state["value"] == ("inactive" if deactivated else "active")
    assert manager._readsb_paused is False
    assert calls.count("stop") == 1
    assert calls.count("start") == 0


def test_readsb_stop_timeout_restores_if_it_stopped_service(monkeypatch):
    manager = sdr_manager.SDRManager()
    state, calls = _mock_readsb(monkeypatch, "active", timeout_stop=True)
    assert not manager._acquire_readsb_lease()
    assert state["value"] == "active"
    assert manager._readsb_paused is False
    assert calls.count("stop") == 1
    assert calls.count("start") == 1


def test_readsb_stop_timeout_with_failed_query_attempts_restore(monkeypatch):
    manager = sdr_manager.SDRManager()
    states = iter(["active", RuntimeError("bus unavailable"), "active"])
    calls = []

    def state():
        value = next(states)
        if isinstance(value, Exception):
            raise value
        return value

    def run(command, **kwargs):
        calls.append(command[1])
        if command[1] == "stop":
            raise sdr_manager.subprocess.TimeoutExpired(command, 12)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(manager, "_readsb_state", state)
    monkeypatch.setattr(sdr_manager.subprocess, "run", run)
    assert not manager._acquire_readsb_lease()
    assert calls == ["stop", "start"]
    assert manager._readsb_paused is False


def test_readsb_unknown_state_fails_closed(monkeypatch):
    manager = sdr_manager.SDRManager()
    _state, calls = _mock_readsb(monkeypatch, "activating")
    assert not manager._acquire_readsb_lease()
    assert "stop" not in calls


def test_p25_start_failure_restores_readsb(monkeypatch):
    manager = sdr_manager.SDRManager()
    state, calls = _mock_readsb(monkeypatch, "active")
    monkeypatch.setattr(sdr_manager, "find_op25_rx", lambda: "/opt/op25/rx.py")
    monkeypatch.setattr(
        sdr_manager.subprocess, "Popen", Mock(side_effect=OSError("launch failed")))

    assert not manager.start_pig_p25(851_012_500, "City P25")
    assert state["value"] == "active"
    assert manager._readsb_paused is False
    assert calls.count("stop") == 1
    assert calls.count("start") == 1
    errors = [data for kind, data in manager.poll_events() if kind == "error"]
    assert any("OP25 failed to start" in data for data in errors)


def test_p25_stop_restores_readsb_once(monkeypatch):
    manager = sdr_manager.SDRManager()
    state, calls = _mock_readsb(monkeypatch, "active")
    monkeypatch.setattr(sdr_manager, "find_op25_rx", lambda: "/opt/op25/rx.py")
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(sdr_manager.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(
        sdr_manager.threading, "Thread", Mock(return_value=Mock()))

    assert manager.start_pig_p25(851_012_500, "City P25")
    assert state["value"] == "inactive"
    manager._pig_p25_proc = None
    manager.stop()
    manager.stop()
    assert state["value"] == "active"
    assert calls.count("start") == 1


def test_p25_stderr_drain_filters_blacklist_noise_and_bounds_detail():
    manager = sdr_manager.SDRManager()
    lines = ["added talkgroup 100 from /tmp/all-talkgroups.tsv\n"] * 100
    lines.extend(["usb_claim_interface error -6\n", "later warning\n"])
    manager._drain_op25_stderr(io.StringIO("".join(lines)))
    assert len(manager._pig_p25_stderr_tail) == 2
    assert "usb_claim_interface error -6" in manager._op25_failure_detail(
        SimpleNamespace(poll=lambda: 1))


def test_p25_child_exit_error_includes_op25_cause(monkeypatch):
    manager = sdr_manager.SDRManager()
    manager.running = True
    manager.mode = "pig_p25"
    proc = Mock()
    proc.poll.return_value = 1
    manager._pig_p25_proc = proc
    manager._pig_p25_stderr_tail.extend([
        "usb_claim_interface error -6",
        "AttributeError: source was None",
    ])
    stop = Mock()
    monkeypatch.setattr(manager, "stop", stop)

    manager._pig_p25_worker()

    errors = [data for kind, data in manager.poll_events() if kind == "error"]
    assert any("usb_claim_interface error -6" in data for data in errors)
    stop.assert_called_once()


def test_p25_early_exit_waits_for_its_stderr_drainer(monkeypatch):
    manager = sdr_manager.SDRManager()
    manager.running = True
    manager.mode = "pig_p25"
    proc = Mock()
    proc.poll.return_value = 1
    manager._pig_p25_proc = proc
    manager._pig_p25_stderr_owner = proc

    def finish_drain(*, timeout):
        manager._pig_p25_stderr_tail.append("usb_claim_interface error -6")

    drainer = Mock()
    drainer.join.side_effect = finish_drain
    manager._pig_p25_stderr_thread = drainer
    stop = Mock()
    monkeypatch.setattr(manager, "stop", stop)

    manager._pig_p25_worker()

    errors = [data for kind, data in manager.poll_events() if kind == "error"]
    assert any("usb_claim_interface error -6" in data for data in errors)
    drainer.join.assert_called_once()
    stop.assert_called_once()


def test_p25_old_stderr_cannot_be_attributed_to_new_process():
    manager = sdr_manager.SDRManager()
    old_proc = Mock()
    new_proc = Mock()
    manager._pig_p25_stderr_owner = new_proc
    manager._pig_p25_stderr_tail.append("old session USB failure")

    assert "old session" not in manager._op25_failure_detail(old_proc)


def test_rtl433_unexpected_exit_restores_readsb(monkeypatch):
    manager = sdr_manager.SDRManager()
    state, calls = _mock_readsb(monkeypatch, "active")
    assert manager._acquire_readsb_lease()
    manager.running = True
    manager.mode = "433"
    proc = Mock()
    proc.poll.return_value = 1
    proc.stdout = Mock()
    proc.stdout.readline.return_value = b""
    manager._rtl433_proc = proc

    manager._rtl433_reader()

    errors = [data for kind, data in manager.poll_events() if kind == "error"]
    assert any("rtl_433 exited unexpectedly" in data for data in errors)
    assert state["value"] == "active"
    assert calls.count("start") == 1
    assert manager.running is False


def test_adsb_unexpected_exit_restores_readsb(monkeypatch):
    manager = sdr_manager.SDRManager()
    state, calls = _mock_readsb(monkeypatch, "active")
    assert manager._acquire_readsb_lease()
    manager.running = True
    manager.mode = "adsb"
    proc = Mock()
    proc.poll.return_value = 1
    manager._dump1090_proc = proc
    monkeypatch.setattr(sdr_manager.time, "sleep", Mock())

    manager._sbs_reader()

    errors = [data for kind, data in manager.poll_events() if kind == "error"]
    assert any("dump1090 exited unexpectedly" in data for data in errors)
    assert state["value"] == "active"
    assert calls.count("start") == 1
    assert manager.running is False


def test_rf_watchlist_stops_after_three_child_failures_and_restores_readsb(
        monkeypatch):
    manager = sdr_manager.SDRManager()
    state, calls = _mock_readsb(monkeypatch, "active")
    assert manager._acquire_readsb_lease()
    manager.running = True
    manager.mode = "pig_rf"
    manager._pig_channels = manager._validated_channels([
        {"frequency_hz": 155_000_000, "bandwidth_hz": 25_000},
    ])
    process = Mock()
    process.returncode = 1
    process.poll.return_value = 1
    process.communicate.return_value = ("", "")
    popen = Mock(return_value=process)
    monkeypatch.setattr(sdr_manager.subprocess, "Popen", popen)
    monkeypatch.setattr(manager._pig_stop_event, "wait", Mock(return_value=False))

    manager._pig_rf_worker("/usr/bin/rtl_power")

    errors = [data for kind, data in manager.poll_events() if kind == "error"]
    assert popen.call_count == 3
    assert any("failed three times" in data for data in errors)
    assert state["value"] == "active"
    assert calls.count("start") == 1
    assert manager.running is False


def test_p25_status_timeout_is_reported(monkeypatch):
    manager = sdr_manager.SDRManager()
    manager.running = True
    manager.mode = "pig_p25"
    proc = Mock()
    proc.poll.return_value = None
    manager._pig_p25_proc = proc
    monkeypatch.setattr(sdr_manager.time, "monotonic", Mock(side_effect=[100.0, 131.0]))
    monkeypatch.setattr(manager._pig_stop_event, "wait", Mock(return_value=False))
    stop = Mock()
    monkeypatch.setattr(manager, "stop", stop)

    manager._pig_p25_worker()

    errors = [data for kind, data in manager.poll_events() if kind == "error"]
    assert any("tuning status unavailable" in data for data in errors)
    stop.assert_called_once()


def test_p25_launch_uses_comma_separated_control_channel_list(monkeypatch):
    manager = sdr_manager.SDRManager()
    monkeypatch.setattr(sdr_manager, "find_op25_rx", lambda: "/opt/op25/rx.py")
    process = Mock()
    process.poll.return_value = None
    popen = Mock(return_value=process)
    monkeypatch.setattr(sdr_manager.subprocess, "Popen", popen)
    monkeypatch.setattr(sdr_manager.threading, "Thread", Mock(return_value=Mock()))

    assert manager.start_pig_p25(
        856_212_500,
        "Lewisville",
        control_channels=[
            856_212_500,
            858_212_500,
            859_212_500,
        ],
    )
    tempdir = manager._pig_p25_tmpdir
    with open(tempdir + "/trunk.tsv", encoding="utf-8") as source:
        _, row = source.read().rstrip("\n").splitlines()
    assert row.split("\t")[1] == "856.212500,858.212500,859.212500"
    assert manager._pig_p25_control_channels == (856_212_500, 858_212_500, 859_212_500)
    manager._pig_p25_proc = None
    manager.stop()


def test_p25_control_channel_list_validation_is_bounded(monkeypatch):
    manager = sdr_manager.SDRManager()
    popen = Mock()
    monkeypatch.setattr(sdr_manager, "find_op25_rx", lambda: "/opt/op25/rx.py")
    monkeypatch.setattr(sdr_manager.subprocess, "Popen", popen)
    assert not manager.start_pig_p25(
        851_000_000,
        control_channels=[851_000_000 + index for index in range(1, 17)],
    )
    assert "exceeds 16" in manager.poll_events()[0][1]
    assert not manager.start_pig_p25(
        851_000_000, control_channels=[851_000_000, "852000000"]
    )
    assert "integer Hz" in manager.poll_events()[0][1]
    assert not manager.start_pig_p25(
        851_000_000, control_channels=[851_000_000, 2_000_000_000]
    )
    assert "outside supported" in manager.poll_events()[0][1]
    popen.assert_not_called()


def test_p25_decode_delta_is_attributed_only_after_same_frequency_samples(monkeypatch):
    manager = sdr_manager.SDRManager()
    manager.mode = "pig_p25"
    manager.running = True
    manager._pig_p25_proc = Mock()
    manager._pig_p25_proc.poll.return_value = None
    manager._pig_p25_control_hz = 856_212_500
    manager._pig_p25_control_channels = (856_212_500, 858_212_500)
    manager._pig_p25_url = "http://127.0.0.1:18080/"
    manager._pig_p25_system_label = "Lewisville"
    manager._pig_stop_event.wait = Mock(side_effect=[False, False, False, True])

    def response(frequency, blocks):
        value = (
            '[{"json_type":"trunk_update","0x293":{"tsbks":%d}},'
            '{"json_type":"change_freq","freq":%d}]'
        ) % (blocks, frequency)
        wrapped = MagicMock()
        wrapped.read.return_value = value.encode()
        wrapped.__enter__ = Mock(return_value=wrapped)
        wrapped.__exit__ = Mock(return_value=None)
        return wrapped

    monkeypatch.setattr(
        sdr_manager.urllib.request,
        "urlopen",
        Mock(
            side_effect=[
                response(856_212_500, 10),
                response(856_212_500, 12),
                response(858_212_500, 20),
            ]
        ),
    )
    manager._pig_p25_worker()

    assert manager.p25_quality["tuned_frequency_hz"] == 858_212_500
    assert manager.p25_quality["last_decoded_frequency_hz"] == 856_212_500
    assert manager.p25_quality["recent_decoded_frequency_hz"] == 856_212_500
    events = [data for kind, data in manager.poll_events() if kind == "pig_p25"]
    assert [event["control_frequency_hz"] for event in events] == [
        856_212_500,
        856_212_500,
        858_212_500,
    ]
    assert events[0]["decoded_frequency_hz"] is None
    assert events[1]["decoded_frequency_hz"] == 856_212_500
    # The first sample after a hop cannot prove activity on the newly tuned channel.
    assert events[2]["decoded_frequency_hz"] == 856_212_500


def test_p25_counter_reset_does_not_create_a_false_decode(monkeypatch):
    manager = sdr_manager.SDRManager()
    manager.mode = "pig_p25"
    manager.running = True
    manager._pig_p25_proc = Mock()
    manager._pig_p25_proc.poll.return_value = None
    manager._pig_p25_control_hz = 856_212_500
    manager._pig_p25_control_channels = (856_212_500,)
    manager._pig_p25_url = "http://127.0.0.1:18080/"
    manager._pig_stop_event.wait = Mock(side_effect=[False, False, False, True])
    responses = []
    for blocks in (10, 2, 3):
        wrapped = MagicMock()
        wrapped.read.return_value = (
            '[{"json_type":"trunk_update","0x293":{"tsbks":%d}},'
            '{"json_type":"change_freq","freq":856212500}]' % blocks
        ).encode()
        wrapped.__enter__ = Mock(return_value=wrapped)
        wrapped.__exit__ = Mock(return_value=None)
        responses.append(wrapped)
    monkeypatch.setattr(
        sdr_manager.urllib.request, "urlopen", Mock(side_effect=responses)
    )

    manager._pig_p25_worker()

    events = [data for kind, data in manager.poll_events() if kind == "pig_p25"]
    assert [event["decoded_frequency_hz"] for event in events] == [
        None,
        None,
        856_212_500,
    ]
