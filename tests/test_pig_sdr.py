"""Pig Detector SDR profiles: ownership, bounded events and metadata only."""

import os
from unittest.mock import MagicMock, Mock

import pytest

from watchdogs import sdr_manager


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
    thread.start.assert_called_once()
    manager._pig_p25_proc = None  # mocked process has no OS process group
    manager.stop()
    assert not os.path.exists(tempdir)
