from types import SimpleNamespace

from watchdogs.mitm import MITMAttack


def test_mitm_uses_dumpcap_pcapng(tmp_path, monkeypatch):
    calls = []

    class Process:
        pass

    monkeypatch.setattr(
        "watchdogs.mitm.subprocess.Popen",
        lambda command, **kwargs: calls.append((command, kwargs)) or Process(),
    )
    attack = MITMAttack(loot=SimpleNamespace(session_path=str(tmp_path)))
    attack._iface = "wlan0"
    attack._victims = [("192.0.2.10", "02:00:00:00:00:10")]

    attack._start_capture()

    assert attack._pcap_path.endswith(".pcapng")
    assert len(calls) == 1
    command = calls[0][0]
    assert command[:3] == ["dumpcap", "-q", "-n"]
    assert command[command.index("-w") + 1] == attack._pcap_path
    assert command[command.index("-f") + 1] == "host 192.0.2.10"
