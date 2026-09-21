import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
import sys

import pytest
from watchdogs import upload_manager


@pytest.fixture
def accept_test_captures(monkeypatch):
    """Most upload-flow tests use tiny sentinels rather than real PCAP data."""
    monkeypatch.setattr(
        upload_manager, "_capture_uploadable", lambda path: (True, ""))


def touch(path: Path, data=b"capture"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_wpasec_prefers_pcapng_twin_and_keeps_legacy_pcap(
        tmp_path, monkeypatch, accept_test_captures):
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


def test_wpasec_upload_includes_every_network_and_empty_message(
        tmp_path, monkeypatch, accept_test_captures):
    session = tmp_path / "session" / "handshakes"
    home = touch(session / "Home_AABBCCDDEEFF_120000.pcapng")
    nearby = touch(session / "Lab_020000000003_120001.pcapng")
    uploaded = []
    monkeypatch.setattr(
        upload_manager, "upload_wpasec",
        lambda path: (uploaded.append(path) is None, "ok"),
    )
    result = upload_manager.upload_wpasec_all(tmp_path)
    assert result[:2] == (2, 2)
    assert uploaded == [home, nearby]
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


def test_wpasec_uploads_once_and_persists_content_receipt(
        tmp_path, monkeypatch, accept_test_captures):
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

    # Version-1 ledgers written before rejection tracking have no status.
    # They remain accepted receipts and must not be uploaded again.
    ledger["accounts"][account]["captures"][digest].pop("status")
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    assert upload_manager.upload_wpasec_all(loot) == (
        0, 0, "No new captures | 1 already uploaded")
    assert calls == [capture]


def test_wpasec_retries_transient_failures(
        tmp_path, monkeypatch, accept_test_captures):
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


@pytest.mark.parametrize("rejection", [
    "WPA-sec rejected capture: Not a valid capture file. We support pcap and pcapng.",
    "WPA-sec rejected capture: No valid handshakes/PMKIDs found.",
    "WPA-sec rejected capture: No passwords found.",
])
def test_wpasec_persists_permanent_server_rejections(
        tmp_path, monkeypatch, accept_test_captures, rejection):
    loot = tmp_path / "loot"
    capture = touch(
        loot / "session" / "handshakes" / "Bad_020000000001_120000.pcapng",
        rejection.encode("utf-8"))
    calls = []
    account_key = ["account-a"]
    monkeypatch.setattr(
        upload_manager, "get_wpasec_key", lambda: account_key[0])
    monkeypatch.setattr(
        upload_manager, "upload_wpasec",
        lambda path: (calls.append(path), (False, rejection))[1])

    first = upload_manager.upload_wpasec_all(loot)
    assert first[:2] == (0, 1)
    assert "1 newly rejected" in first[2]
    assert calls == [capture]

    # Capture validity is independent of the WPA-sec account. A key change
    # must not cause identical unsupported bytes to be submitted again.
    account_key[0] = "account-b"
    second = upload_manager.upload_wpasec_all(loot)
    assert second[:2] == (0, 0)
    assert "1 permanently rejected" in second[2]
    assert calls == [capture]

    ledger = json.loads(
        (loot / upload_manager._UPLOAD_LEDGER_NAME).read_text())
    digest = hashlib.sha256(rejection.encode("utf-8")).hexdigest()
    receipt = ledger["permanent_rejections"][digest]
    assert receipt["status"] == "permanent_rejection"
    assert receipt["reason"] in {
        "unsupported capture format",
        "no usable handshake, PMKID, or password data",
    }


@pytest.mark.parametrize("failure", [
    "timeout",
    "connection reset by peer",
    "HTTP 429: rate limited",
    "HTTP 500: temporary server failure",
    "WPA-sec key not configured",
])
def test_wpasec_transport_and_service_failures_remain_retryable(failure):
    assert upload_manager._permanent_capture_rejection(failure) == ""


def test_wpasec_receipts_follow_content_and_account(
        tmp_path, monkeypatch, accept_test_captures):
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
        tmp_path, monkeypatch, accept_test_captures):
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


def test_wpasec_receipt_write_failure_retries_next_run(
        tmp_path, monkeypatch, accept_test_captures):
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


def test_wpasec_candidates_never_include_22000(tmp_path):
    session = tmp_path / "loot" / "session" / "handshakes"
    pcapng = touch(session / "Lab_020000000001_120000.pcapng")
    touch(session / "Lab_020000000001_120000.22000", b"WPA*02*hash")
    assert upload_manager._capture_candidates(tmp_path / "loot") == [pcapng]


def test_wpasec_local_preflight_magic_companion_and_no_hash(
        tmp_path, monkeypatch):
    invalid = touch(tmp_path / "invalid.pcap", b"\xd4\xc3\xb2\xa1" + b"\0" * 20)
    assert upload_manager._capture_uploadable(invalid) == (
        False, "invalid or empty PCAP/PCAPNG")

    capture = touch(
        tmp_path / "ready.pcapng", b"\x0a\x0d\x0d\x0a" + b"\0" * 100)
    companion = touch(capture.with_suffix(".22000"), b"WPA*02*hash")
    companion.touch()
    assert upload_manager._capture_uploadable(capture) == (True, "")

    companion.unlink()
    monkeypatch.setattr(upload_manager.shutil, "which", lambda name: "/hcx")
    calls = []
    monkeypatch.setattr(
        upload_manager.subprocess, "run",
        lambda *args, **kwargs: (
            calls.append((args, kwargs)),
            NS(returncode=0, stdout="no hashes", stderr=""),
        )[1])
    assert upload_manager._capture_uploadable(capture) == (
        False, "no crackable handshake or PMKID")
    command = calls[0][0][0]
    assert command[0] == "/hcx"
    assert "--nonce-error-corrections=8" in command
    assert "--eapoltimeout=30000" in command
    assert "--max-essids=1" in command


def test_wpasec_locally_skips_unusable_capture_without_upload(
        tmp_path, monkeypatch):
    loot = tmp_path / "loot"
    capture = touch(
        loot / "session" / "handshakes" / "Partial_020000000001_120000.pcapng")
    checks = []
    monkeypatch.setattr(
        upload_manager, "_capture_uploadable",
        lambda path: (checks.append(path), (
            False, "no crackable handshake or PMKID"))[1])
    monkeypatch.setattr(
        upload_manager, "upload_wpasec",
        lambda path: pytest.fail("unusable capture was uploaded"))
    result = upload_manager.upload_wpasec_all(loot)
    assert result[:2] == (0, 0)
    assert "1 newly rejected" in result[2]
    assert "no usable handshake, PMKID, or password data" in result[2]

    second = upload_manager.upload_wpasec_all(loot)
    assert second[:2] == (0, 0)
    assert "1 permanently rejected" in second[2]
    assert checks == [capture]
