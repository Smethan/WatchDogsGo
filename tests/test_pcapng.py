import io
import struct

import pytest

from watchdogs.pcapng import (
    EPB,
    PcapngWriter,
    channel_frequency,
    rebase_boot_timestamps,
    validate_pcapng,
)


def blocks(data):
    offset = 0
    while offset < len(data):
        kind, size = struct.unpack_from("<II", data, offset)
        yield offset, kind, size
        offset += size


def capture(*packets):
    output = io.BytesIO()
    writer = PcapngWriter(output)
    for frame, at, channel, rssi in packets:
        writer.write_packet(frame, at, channel=channel, rssi=rssi)
    return output.getvalue()


def test_pcapng_radiotap_channel_signal_and_lengths():
    frame = bytes.fromhex("80000000") + bytes(range(20))
    data = capture((frame, 1_700_000_000.25, 6, -42))
    info = validate_pcapng(data)
    assert info.packets == 1
    assert info.first_timestamp_us == 1_700_000_000_250_000
    epb_offset = next(offset for offset, kind, _ in blocks(data) if kind == EPB)
    interface, high, low, captured, original = struct.unpack_from(
        "<IIIII", data, epb_offset + 8)
    packet = data[epb_offset + 28:epb_offset + 28 + captured]
    assert interface == 0 and captured == original == len(frame) + 15
    assert struct.unpack_from("<H", packet, 2)[0] == 15
    assert struct.unpack_from("<H", packet, 10)[0] == 2437
    assert struct.unpack_from("<H", packet, 12)[0] == 0x0080
    assert struct.unpack_from("<b", packet, 14)[0] == -42
    assert packet[15:] == frame
    assert (high << 32) | low == info.first_timestamp_us
    assert all(size % 4 == 0 for _, _, size in blocks(data))


def test_rebase_boot_timestamps_preserves_capture_intervals():
    frame = b"x" * 24
    data = capture((frame, 1.0, 36, -60), (frame, 1.125, 36, -61))
    before = validate_pcapng(data)
    rebased = rebase_boot_timestamps(data, now_us=1_800_000_000_000_000)
    after = validate_pcapng(rebased)
    assert before.last_timestamp_us - before.first_timestamp_us == 125_000
    assert after.last_timestamp_us == 1_800_000_000_000_000
    assert after.last_timestamp_us - after.first_timestamp_us == 125_000
    assert channel_frequency(14) == (2484, 0x0080)
    assert channel_frequency(36) == (5180, 0x0100)


def test_pcapng_validation_rejects_corrupt_trailer_and_radiotap():
    data = bytearray(capture((b"x" * 24, 1.0, 1, -20)))
    data[-1] ^= 1
    with pytest.raises(ValueError, match="trailer"):
        validate_pcapng(data)

    data = bytearray(capture((b"x" * 24, 1.0, 1, -20)))
    epb_offset = next(offset for offset, kind, _ in blocks(data) if kind == EPB)
    struct.pack_into("<H", data, epb_offset + 30, 0xffff)
    with pytest.raises(ValueError, match="radiotap"):
        validate_pcapng(data)
