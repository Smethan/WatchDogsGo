import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
import sys

import pytest
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


@pytest.mark.parametrize(("body", "expected_ok", "expected_message"), [
    ("hcxpcapngtool 6.3.5 reading capture", True, "hcxpcapngtool"),
    ("This capture file was already submitted.", True, "Already submitted"),
    ("No valid handshakes/PMKIDs found in the submitted file.",
     False, "rejected capture"),
    ("Not a valid capture file. We support pcap and pcapng.",
     False, "rejected capture"),
    ("", False, "empty response"),
])
def test_wpasec_only_confirms_accepted_server_responses(
        tmp_path, monkeypatch, body, expected_ok, expected_message):
    capture = touch(tmp_path / "capture.pcapng")
    response = NS(status_code=200, text=body)
    requests = NS(post=lambda *args, **kwargs: response)
    monkeypatch.setitem(sys.modules, "requests", requests)
    monkeypatch.setattr(upload_manager, "get_wpasec_key", lambda: "test-key")
    ok, message = upload_manager.upload_wpasec(capture)
    assert ok is expected_ok
    assert expected_message in message


def test_wpasec_uploads_once_and_persists_content_receipt(tmp_path, monkeypatch):
    loot = tmp_path / "loot"
    capture = touch(
        loot / "session" / "handshakes" / "Lab_020000000001_120000.pcapng",
        b"first-capture")
    calls = []
    monkeypatch.setattr(upload_manager, "get_wpasec_key", lambda: "account-a")
    monkeypatch.setattr(
        upload_manager, "upload_wpasec",
        lambda path: (calls.append(path) is None, "accepted"))

    assert upload_manager.upload_wpasec_all(loot) == (1, 1, "1/1 uploaded")
    assert calls == [capture]
    ledger_path = loot / upload_manager._UPLOAD_LEDGER_NAME
    ledger_text = ledger_path.read_text(encoding="utf-8")
    ledger = json.loads(ledger_text)
    account = hashlib.sha256(b"account-a").hexdigest()
    digest = hashlib.sha256(b"first-capture").hexdigest()
    assert digest in ledger["accounts"][account]["captures"]
    assert "account-a" not in ledger_text
    assert not ledger_path.with_name(ledger_path.name + ".tmp").exists()

    assert upload_manager.upload_wpasec_all(loot) == (
        0, 0, "No new captures | 1 already uploaded")
    assert calls == [capture]


def test_wpasec_retries_failures_and_rejected_captures(tmp_path, monkeypatch):
    loot = tmp_path / "loot"
    capture = touch(
        loot / "session" / "handshakes" / "Lab_020000000001_120000.pcapng")
    calls = []
    results = iter(((False, "timeout"), (True, "accepted")))
    monkeypatch.setattr(upload_manager, "get_wpasec_key", lambda: "account-a")
    monkeypatch.setattr(
        upload_manager, "upload_wpasec",
        lambda path: (calls.append(path), next(results))[1])

    first = upload_manager.upload_wpasec_all(loot)
    assert first[:2] == (0, 1) and "timeout" in first[2]
    second = upload_manager.upload_wpasec_all(loot)
    assert second[:2] == (1, 1)
    assert calls == [capture, capture]


def test_wpasec_receipts_follow_content_and_account(tmp_path, monkeypatch):
    loot = tmp_path / "loot"
    original = touch(
        loot / "one" / "handshakes" / "Original_020000000001_120000.pcapng",
        b"same-bytes")
    calls = []
    account = ["account-a"]
    monkeypatch.setattr(upload_manager, "get_wpasec_key", lambda: account[0])
    monkeypatch.setattr(
        upload_manager, "upload_wpasec",
        lambda path: (calls.append(path) is None, "accepted"))
    assert upload_manager.upload_wpasec_all(loot)[:2] == (1, 1)

    renamed = loot / "two" / "handshakes" / "Renamed_020000000001_120001.pcapng"
    renamed.parent.mkdir(parents=True)
    original.rename(renamed)
    assert upload_manager.upload_wpasec_all(loot)[:2] == (0, 0)

    renamed.write_bytes(b"changed-bytes")
    assert upload_manager.upload_wpasec_all(loot)[:2] == (1, 1)
    account[0] = "account-b"
    assert upload_manager.upload_wpasec_all(loot)[:2] == (1, 1)
    assert calls == [original, renamed, renamed]


def test_wpasec_corrupt_receipts_fail_open_and_are_replaced(
        tmp_path, monkeypatch):
    loot = tmp_path / "loot"
    capture = touch(
        loot / "session" / "handshakes" / "Lab_020000000001_120000.pcapng")
    ledger_path = loot / upload_manager._UPLOAD_LEDGER_NAME
    ledger_path.write_text("{broken", encoding="utf-8")
    calls = []
    monkeypatch.setattr(upload_manager, "get_wpasec_key", lambda: "account-a")
    monkeypatch.setattr(
        upload_manager, "upload_wpasec",
        lambda path: (calls.append(path) is None, "accepted"))
    uploaded, total, message = upload_manager.upload_wpasec_all(loot)
    assert (uploaded, total) == (1, 1)
    assert calls == [capture]
    assert "receipt file unreadable" in message
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["version"] == 1


def test_wpasec_receipt_write_failure_retries_next_run(tmp_path, monkeypatch):
    loot = tmp_path / "loot"
    capture = touch(
        loot / "session" / "handshakes" / "Lab_020000000001_120000.pcapng")
    calls = []
    monkeypatch.setattr(upload_manager, "get_wpasec_key", lambda: "account-a")
    monkeypatch.setattr(
        upload_manager, "upload_wpasec",
        lambda path: (calls.append(path) is None, "accepted"))
    real_replace = upload_manager.os.replace
    monkeypatch.setattr(
        upload_manager.os, "replace",
        lambda source, target: (_ for _ in ()).throw(PermissionError("read-only")))
    first = upload_manager.upload_wpasec_all(loot)
    assert first[:2] == (1, 1)
    assert "receipt was not saved" in first[2]

    monkeypatch.setattr(upload_manager.os, "replace", real_replace)
    second = upload_manager.upload_wpasec_all(loot)
    assert second[:2] == (1, 1)
    assert calls == [capture, capture]
