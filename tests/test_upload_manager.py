from pathlib import Path

from watchdogs import upload_manager


def touch(path: Path, data=b"capture"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_wpasec_prefers_pcapng_twin_and_keeps_legacy_pcap(tmp_path, monkeypatch):
    session = tmp_path / "loot" / "session" / "handshakes"
    preferred_pcap = touch(session / "Lab_020000000001_120000.pcap")
    preferred_ng = touch(session / "Lab_020000000001_120000.pcapng")
    legacy = touch(session / "Old_020000000002_120001.pcap")
    uploaded = []
    monkeypatch.setattr(
        upload_manager, "upload_wpasec",
        lambda path: (uploaded.append(path) is None, "ok"),
    )
    count, total, message = upload_manager.upload_wpasec_all(tmp_path / "loot")
    assert (count, total) == (2, 2)
    assert uploaded == sorted((preferred_ng, legacy))
    assert preferred_pcap not in uploaded
    assert message == "2/2 uploaded"


def test_wpasec_pcapng_whitelist_filter_and_empty_message(tmp_path, monkeypatch):
    session = tmp_path / "session" / "handshakes"
    blocked = touch(session / "Home_AABBCCDDEEFF_120000.pcapng")
    allowed = touch(session / "Lab_020000000003_120001.pcapng")
    uploaded = []
    monkeypatch.setattr(
        upload_manager, "upload_wpasec",
        lambda path: (uploaded.append(path) is None, "ok"),
    )
    result = upload_manager.upload_wpasec_all(
        tmp_path, {"AA:BB:CC:DD:EE:FF"})
    assert result[:2] == (1, 1)
    assert uploaded == [allowed] and blocked not in uploaded
    assert "1 skipped" in result[2]
    assert upload_manager.upload_wpasec_all(tmp_path / "missing") == (
        0, 0, "No PCAP/PCAPNG files found")
