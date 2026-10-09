"""ctypes parity checks between WDG's host exchange validator and firmware C."""
import ctypes
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from watchdogs.hs_exchange import ExchangeValidator, parse_key_frame


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = Path(os.environ.get("WDG_FIRMWARE_SOURCE", ROOT / "projectZero/ESP32C5/main"))


class CResult(ctypes.Structure):
    _fields_ = [
        ("ok", ctypes.c_int), ("accepted", ctypes.c_int),
        ("new_message", ctypes.c_int), ("became_complete", ctypes.c_int),
        ("message", ctypes.c_int), ("complete", ctypes.c_int),
        ("message_mask", ctypes.c_uint8),
        ("hccapx", ctypes.c_uint8 * 393),
    ]


@pytest.fixture(scope="module")
def c_exchange(tmp_path_factory):
    compiler = shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler unavailable for firmware parity test")
    build = tmp_path_factory.mktemp("hs-exchange-ctypes")
    wrapper = build / "wrapper.c"
    library = build / "libhs_exchange.so"
    wrapper.write_text(r'''
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include "hs_exchange.h"
typedef struct {
    int ok, accepted, new_message, became_complete, message, complete;
    uint8_t message_mask;
    uint8_t hccapx[393];
} parity_result_t;
void *parity_new(void) {
    hsx_state_t *state = calloc(1, sizeof(*state));
    if (state) hsx_reset(state);
    return state;
}
void parity_free(void *opaque) { free(opaque); }
int parity_ingest(void *opaque, const uint8_t *frame, size_t len,
                  const uint8_t *ssid, size_t ssid_len, parity_result_t *out) {
    if (!opaque || !out) return 0;
    memset(out, 0, sizeof(*out));
    hsx_result_t result;
    out->ok = hsx_ingest((hsx_state_t *)opaque, frame, len, 1000,
                         ssid, ssid_len, &result);
    if (!out->ok) return 0;
    out->accepted = result.accepted;
    out->new_message = result.new_message;
    out->became_complete = result.became_complete;
    out->message = result.message;
    if (result.entry) {
        out->complete = result.entry->complete;
        out->message_mask = result.entry->message_mask;
        memcpy(out->hccapx, &result.entry->hccapx, sizeof(out->hccapx));
    }
    return 1;
}
''')
    subprocess.run([
        compiler, "-std=c11", "-shared", "-fPIC", "-O0",
        "-I", str(FIRMWARE), str(wrapper), str(FIRMWARE / "hs_exchange.c"),
        "-o", str(library),
    ], check=True, capture_output=True, text=True)
    lib = ctypes.CDLL(str(library))
    lib.parity_new.restype = ctypes.c_void_p
    lib.parity_free.argtypes = [ctypes.c_void_p]
    lib.parity_ingest.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.POINTER(CResult),
    ]
    lib.parity_ingest.restype = ctypes.c_int

    class NativeState:
        def __init__(self):
            self.ptr = lib.parity_new()
            assert self.ptr

        def ingest(self, frame, ssid=b"network"):
            native_frame = (ctypes.c_uint8 * len(frame)).from_buffer_copy(frame)
            native_ssid = (ctypes.c_uint8 * len(ssid)).from_buffer_copy(ssid) if ssid else None
            result = CResult()
            accepted_call = lib.parity_ingest(self.ptr, native_frame, len(frame),
                                               native_ssid, len(ssid), ctypes.byref(result))
            assert accepted_call == result.ok
            return result

        def close(self):
            if self.ptr:
                lib.parity_free(self.ptr)
                self.ptr = None

    return NativeState


AP = bytes.fromhex("020000000001")
AP2 = bytes.fromhex("020000000002")
STA = bytes.fromhex("0a0000000001")
STA2 = bytes.fromhex("0a0000000002")
LLC = bytes.fromhex("aaaa03000000888e")


def frame(message, ap=AP, sta=STA, replay=7, keyver=2):
    """Mirror the native C fixture's deterministic 131-byte EAPOL frames."""
    value = bytearray(131)
    value[0] = 0x08
    from_ap = message in (1, 3)
    value[1] = 2 if from_ap else 1
    value[4:10] = sta if from_ap else ap
    value[10:16] = ap if from_ap else sta
    value[16:22] = ap
    value[24:32] = LLC
    eapol = 32
    value[eapol:eapol + 4] = bytes((2, 3, 0, 95))
    key = eapol + 4
    value[key] = 2
    bits = {1: 0x0080 | 0x0008 | keyver,
            2: 0x0100 | 0x0008 | keyver,
            3: 0x0100 | 0x0080 | 0x0040 | 0x0008 | keyver,
            4: 0x0100 | 0x0008 | keyver}[message]
    value[key + 1:key + 3] = bits.to_bytes(2, "big")
    value[key + 12] = replay
    if message != 4:
        value[key + 13:key + 45] = bytes((message * 16 + i + 1) & 0xff for i in range(32))
    if message != 1:
        value[key + 77:key + 93] = bytes(range(0xA0, 0xB0))
    return bytes(value)


def ingest_pair(native, host, value):
    before = None
    parsed = parse_key_frame(value)
    if parsed:
        host.set_ssid(parsed.ap, b"network")
        entry = host.entries.get((parsed.ap, parsed.client, parsed.replay))
        before = entry["frames"].get(parsed.message) if entry else None
        was_complete = entry["complete"] if entry else False
    else:
        was_complete = False
    host_artifact = host.ingest(value)
    c_result = native.ingest(value)
    assert bool(c_result.accepted) == bool(parsed and len(value) <= 512)
    if parsed:
        entry = host.entries.get((parsed.ap, parsed.client, parsed.replay))
        assert entry is not None
        after = entry["frames"].get(parsed.message)
        before_frame = before[1] if before else None
        host_new = after is not None and after[1] != before_frame
        assert bool(c_result.new_message) == host_new
        assert bool(c_result.became_complete) == bool(host_artifact)
        assert bool(c_result.complete) == entry["complete"]
        if host_artifact:
            assert bytes(c_result.hccapx) == host_artifact["hccapx"]
    else:
        assert not c_result.new_message and not c_result.became_complete
    assert not (host_artifact and was_complete)
    return c_result


def test_valid_same_replay_pair_and_hccapx_bytes_match(c_exchange):
    native, host = c_exchange(), ExchangeValidator()
    try:
        assert not ingest_pair(native, host, frame(1)).complete
        duplicate = ingest_pair(native, host, frame(1))
        assert not duplicate.new_message and not duplicate.became_complete
        result = ingest_pair(native, host, frame(2))
        assert result.complete and result.message_mask == 0x03
    finally:
        native.close()


def test_replay_client_and_ap_are_independent_exchange_keys(c_exchange):
    native, host = c_exchange(), ExchangeValidator()
    try:
        sequences = [
            frame(1, replay=7), frame(2, replay=7),
            frame(1, replay=8), frame(2, replay=8),
            frame(1, ap=AP2, replay=7), frame(2, ap=AP2, replay=7),
            frame(1, ap=AP, sta=STA2, replay=7), frame(2, ap=AP, sta=STA2, replay=7),
        ]
        for value in sequences:
            ingest_pair(native, host, value)
        assert len([entry for entry in host.entries.values() if entry["complete"]]) == 4
    finally:
        native.close()


def test_keyver_mismatch_does_not_complete_either_implementation(c_exchange):
    native, host = c_exchange(), ExchangeValidator()
    try:
        ingest_pair(native, host, frame(1, keyver=1))
        ingest_pair(native, host, frame(2, keyver=2))
        assert not any(entry["complete"] for entry in host.entries.values())
    finally:
        native.close()


@pytest.mark.parametrize("mutation", ["truncated", "bad_type", "bad_direction", "bad_keyver", "zero_mic"])
def test_invalid_eapol_inputs_are_rejected_equally(c_exchange, mutation):
    native, host = c_exchange(), ExchangeValidator()
    value = bytearray(frame(2))
    if mutation == "truncated":
        value = value[:45]
    elif mutation == "bad_type":
        value[33] = 2
    elif mutation == "bad_direction":
        value[1] = 2
    elif mutation == "bad_keyver":
        value[36 + 2] = 0
    elif mutation == "zero_mic":
        value[36 + 77:36 + 93] = bytes(16)
    try:
        ingest_pair(native, host, bytes(value))
        assert not host.entries
    finally:
        native.close()


def test_poisoned_message_retransmission_repairs_exchange_and_artifact(c_exchange):
    native, host = c_exchange(), ExchangeValidator()
    poisoned = bytearray(frame(1, replay=51))
    poisoned[36 + 13:36 + 45] = bytes(32)  # valid M1 shape, unusable ANonce
    sequence = [bytes(poisoned), frame(2, replay=51), frame(1, replay=51)]
    try:
        results = [ingest_pair(native, host, value) for value in sequence]
        assert not results[1].complete
        assert results[2].complete and results[2].became_complete
        assert results[2].message_mask == 0x03
    finally:
        native.close()


def test_m2_m3_arrival_order_matches_and_selects_pair_two(c_exchange):
    native, host = c_exchange(), ExchangeValidator()
    try:
        ingest_pair(native, host, frame(3, replay=41))
        result = ingest_pair(native, host, frame(2, replay=40))
        assert result.complete
        assert bytes(result.hccapx)[8] == 2
    finally:
        native.close()
