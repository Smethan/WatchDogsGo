"""Exercise host-encoded C5 jobs against the production firmware parser."""
from __future__ import annotations

import base64
import json
import os
import selectors
import shutil
import subprocess
from pathlib import Path

import pytest

from watchdogs.c5_jobs import encode_job_start, parse_device_info
from watchdogs.radio_scheduler import CapacityError, RadioScheduler


WORKSPACE = Path(__file__).resolve().parents[2]
FIRMWARE_MAIN = Path(os.environ.get(
    "WDG_FIRMWARE_SOURCE", WORKSPACE / "projectZero/ESP32C5/main"))
PROJECT_ZERO = FIRMWARE_MAIN.parents[1]
HARNESS = PROJECT_ZERO / "tests/serial_wardrive/job_interop_harness.c"
STUBS = PROJECT_ZERO / "tests/serial_wardrive/stubs"
CJSON_SOURCE = PROJECT_ZERO / "tests/vendor/cjson/cJSON.c"


@pytest.fixture(scope="module")
def firmware_job_harness(tmp_path_factory):
    compiler = shutil.which("cc")
    if not compiler:
        message = "C compiler unavailable for production firmware interop"
        if os.environ.get("WDG_FIRMWARE_SOURCE"):
            pytest.fail(message)
        pytest.skip(message)
    if not HARNESS.is_file():
        message = f"firmware interop harness not present: {HARNESS}"
        if os.environ.get("WDG_FIRMWARE_SOURCE"):
            pytest.fail(message)
        pytest.skip(message)
    binary = tmp_path_factory.mktemp("c5-job-firmware-interop") / "job-interop"
    subprocess.run([
        compiler, "-std=c11", "-D_POSIX_C_SOURCE=200809L",
        "-Wno-unused-function", "-I", str(PROJECT_ZERO / "tests/vendor/cjson"),
        "-I", str(STUBS), "-I", str(FIRMWARE_MAIN), str(HARNESS),
        str(CJSON_SOURCE), "-lm", "-o", str(binary),
    ], check=True, capture_output=True, text=True)
    return binary


def _wire_request(token, request):
    payload = base64.urlsafe_b64encode(json.dumps(
        request, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).decode("ascii").rstrip("=")
    return f"job_start {token} {payload}"


def _invoke(firmware_job_harness, make_command):
    process = subprocess.Popen(
        [str(firmware_job_harness)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    try:
        metadata_line = ""
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            for _ in range(8):
                if not selector.select(timeout=2):
                    pytest.fail("firmware interop harness timed out before device_info")
                candidate = process.stdout.readline()
                if not candidate:
                    break
                if candidate.startswith("WDG:"):
                    metadata_line = candidate.rstrip("\r\n")
                    break
        assert metadata_line, "firmware harness did not emit device_info first"
        metadata = parse_device_info(metadata_line)
        assert metadata is not None, f"invalid firmware device_info: {metadata_line}"
        raw_info = json.loads(metadata_line[4:])
        command = make_command(metadata, raw_info)
        process.stdin.write(command + "\n")
        process.stdin.flush()
        process.stdin.close()
        return_code = process.wait(timeout=10)
        output = process.stdout.read()
        stderr = process.stderr.read()
        records = []
        for line in output.splitlines():
            marker = line.find("WDG:")
            if marker < 0:
                continue
            try:
                records.append(json.loads(line[marker + 4:]))
            except (ValueError, TypeError):
                continue
        return metadata, raw_info, return_code, records, stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def _assert_clean_lifecycle(records, *, token, raw_info):
    assert not any(record.get("kind") == "error" or record.get("state") == "error"
                   or record.get("request_error") for record in records), records
    started = next((record for record in records
                    if record.get("kind") == "started"
                    and record.get("session") == token), None)
    stopped = next((record for record in records
                    if record.get("kind") == "stopped"
                    and record.get("session") == token), None)
    assert started is not None, f"firmware did not start token {token}: {records!r}"
    assert stopped is not None, f"firmware did not stop token {token}: {records!r}"
    expected_identity = (raw_info["device_id"], raw_info["boot_id"])
    assert (started.get("device_id"), started.get("boot_id")) == expected_identity
    assert (stopped.get("device_id"), stopped.get("boot_id")) == expected_identity
    assert started.get("state") == "running"
    assert stopped.get("state") == "stopped"
    assert stopped.get("seq", 0) > started.get("seq", 0)


def _plan(metadata, operation, *, target=None, options=None):
    targets = [target] if target else []
    if operation == "wardrive":
        return RadioScheduler().plan([metadata], operation, targets=targets)[
            "requests"][metadata["device_id"]]
    kwargs = {"targets": targets}
    if operation in ("ble_scan", "bt_track", "airtag_scan"):
        kwargs["channels"] = None
    elif target:
        kwargs["channels"] = [target["channel"]]
    else:
        kwargs["channels"] = [6]
    if options is not None:
        kwargs["options"] = options
    return RadioScheduler().plan([metadata], operation, **kwargs)[
        "requests"][metadata["device_id"]]


@pytest.mark.parametrize("operation,targeted", [
    ("wardrive", False), ("wifi_scan", False), ("ble_scan", False),
    ("hs_sniff", True), ("sniffer", True), ("handshake", True),
    ("deauth", True), ("sae", True), ("blackout", True),
    ("portal", True), ("evil_twin", True),
])
def test_host_scheduler_wire_job_is_admitted_by_production_firmware(
        firmware_job_harness, operation, targeted):
    target = ({"bssid": "02:11:22:33:44:55", "channel": 6,
               "auth": 3, "ssid_hex": "4142"} if targeted else None)
    options = ({"storage": "serial", "scope": "selected", "ssid_hex": "4142"}
               if operation in ("portal", "evil_twin") else None)

    def command(metadata, raw_info):
        # parse_device_info normalizes host identity to uppercase, while the
        # production JSON nonce stays lowercase on the wire.
        assert raw_info["boot_id"] == "a1b2c3d4e5f60789"
        request = _plan(metadata, operation, target=target, options=options)
        start = encode_job_start("interop_1", request,
                                 boot_id=metadata["boot_id"])
        payload = start.split(" ", 2)[2]
        decoded = json.loads(base64.urlsafe_b64decode(
            payload + "=" * (-len(payload) % 4)))
        # Preserve the exact firmware-emitted case for v1.8.1 and older, whose
        # parser used strcmp. Newer firmware accepts either hex case, but this
        # equality ensures an older parser can still admit host jobs.
        assert decoded["boot_id"] == raw_info["boot_id"]
        return start

    metadata, raw_info, rc, records, stderr = _invoke(firmware_job_harness, command)
    assert rc == 0, f"{operation} rejected by firmware: {records!r} {stderr}"
    assert metadata["boot_id"] == "A1B2C3D4E5F60789"
    _assert_clean_lifecycle(records, token="interop_1", raw_info=raw_info)


def test_firmware_rejects_stale_nonce_and_target_exclusion_overflow(firmware_job_harness):
    target = {"bssid": "02:11:22:33:44:55", "channel": 6,
              "auth": 3, "ssid_hex": "4142"}

    def stale_command(metadata, _raw):
        request = _plan(metadata, "handshake", target=target)
        request["boot_id"] = "0123456789ABCDEF"
        return encode_job_start("stale_1", request,
                                boot_id="0123456789ABCDEF")

    _metadata, _raw, rc, records, stderr = _invoke(firmware_job_harness, stale_command)
    assert rc == 2, f"stale nonce unexpectedly admitted: {records!r} {stderr}"
    assert any(record.get("request_error") or record.get("kind") == "error"
               for record in records)

    def oversized_targets(metadata, _raw):
        # Go beyond both firmware-reported capacity and the production parser's
        # XIAO limit. Raw encoding intentionally bypasses host-side preflight.
        request = _plan(metadata, "handshake", target=target)
        request["targets"] = [
            {**target, "bssid": f"02:11:22:33:{index // 256:02X}:{index % 256:02X}"}
            for index in range(17)
        ]
        return _wire_request("too_many_targets", request)

    _metadata, _raw, rc, records, stderr = _invoke(firmware_job_harness, oversized_targets)
    assert rc == 2, f"oversized target list unexpectedly admitted: {records!r} {stderr}"
    assert any(record.get("request_error") or record.get("kind") == "error"
               for record in records)

    def oversized_exclusions(metadata, _raw):
        request = _plan(metadata, "handshake", target=target)
        request["exclude"] = [
            f"02:66:{index // 65536:02X}:{(index // 256) % 256:02X}:{index % 256:02X}:01"
            for index in range(33)
        ]
        return _wire_request("too_many_exclusions", request)

    _metadata, _raw, rc, records, stderr = _invoke(
        firmware_job_harness, oversized_exclusions)
    assert rc == 2, f"oversized exclusion list unexpectedly admitted: {records!r} {stderr}"
    assert any(record.get("request_error") or record.get("kind") == "error"
               for record in records)


def test_scheduler_preserves_advertised_target_capacity(firmware_job_harness):
    def reject(metadata, _raw):
        targets = [
            {"bssid": f"02:22:33:44:{index // 256:02X}:{index % 256:02X}",
             "channel": 6, "auth": 3, "ssid_hex": "4142"}
            for index in range(metadata["limits"]["targets"] + 1)
        ]
        with pytest.raises(CapacityError):
            RadioScheduler().plan([metadata], "handshake", targets=targets)
        # The harness still needs a job to complete this process, so submit a
        # valid request after proving host-side capacity admission is strict.
        return encode_job_start("valid_after_capacity", _plan(metadata, "handshake"),
                                boot_id=metadata["boot_id"])

    metadata, raw_info, rc, records, stderr = _invoke(firmware_job_harness, reject)
    assert rc == 0, f"valid control request rejected: {records!r} {stderr}"
    _assert_clean_lifecycle(records, token="valid_after_capacity", raw_info=raw_info)
