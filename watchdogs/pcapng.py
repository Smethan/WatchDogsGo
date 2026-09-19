"""Small, strict PCAPNG writer/validator for raw 802.11 handshake frames.

The ESP32 supplies frames without FCS plus channel/RSSI metadata.  PCAPNG uses
radiotap so analysis tools receive that metadata without pretending the ESP
captured PHY fields it does not expose.
"""

from dataclasses import dataclass
import struct
import time


SHB = 0x0A0D0D0A
IDB = 0x00000001
EPB = 0x00000006
BYTE_ORDER_MAGIC = 0x1A2B3C4D
LINKTYPE_IEEE802_11_RADIOTAP = 127
RADIOTAP_PRESENT = (1 << 1) | (1 << 3) | (1 << 5)  # flags, channel, dBm signal
TSRESOL_MICROSECONDS = 6
_EPOCH_2000_US = 946684800 * 1_000_000


@dataclass(frozen=True)
class PcapngInfo:
    packets: int
    first_timestamp_us: int | None
    last_timestamp_us: int | None


def _pad4(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) & 3)


def _option(code: int, value: bytes) -> bytes:
    return struct.pack("<HH", code, len(value)) + _pad4(value)


def _options(*items: tuple[int, bytes]) -> bytes:
    return b"".join(_option(code, value) for code, value in items) + b"\0\0\0\0"


def _block(kind: int, body: bytes) -> bytes:
    if len(body) & 3:
        raise ValueError("PCAPNG block body must be 32-bit aligned")
    total = len(body) + 12
    return struct.pack("<II", kind, total) + body + struct.pack("<I", total)


def section_header(*, hardware: str, os_name: str, application: str) -> bytes:
    options = _options(
        (2, hardware.encode("utf-8", "replace")),
        (3, os_name.encode("utf-8", "replace")),
        (4, application.encode("utf-8", "replace")),
    )
    return _block(SHB, struct.pack("<IHHq", BYTE_ORDER_MAGIC, 1, 0, -1) + options)


def interface_description(*, name: str, description: str) -> bytes:
    options = _options(
        (2, name.encode("utf-8", "replace")),
        (3, description.encode("utf-8", "replace")),
        (9, bytes((TSRESOL_MICROSECONDS,))),
    )
    return _block(
        IDB,
        struct.pack("<HHI", LINKTYPE_IEEE802_11_RADIOTAP, 0, 2304 + 15)
        + options,
    )


def channel_frequency(channel: int | None) -> tuple[int, int]:
    """Return MHz and radiotap band flags without guessing modulation."""
    if not channel or channel < 1:
        return 0, 0
    if channel == 14:
        return 2484, 0x0080
    if channel <= 13:
        return 2407 + channel * 5, 0x0080
    if channel <= 233:
        return 5000 + channel * 5, 0x0100
    return 0, 0


def radiotap_frame(frame: bytes, channel: int | None, rssi: int | None) -> bytes:
    frequency, flags = channel_frequency(channel)
    signal = max(-128, min(127, int(rssi or 0)))
    # The pad after the one-byte flags field aligns the channel pair to 16 bits.
    header = struct.pack(
        "<BBHIBxHHb", 0, 0, 15, RADIOTAP_PRESENT, 0,
        frequency, flags, signal,
    )
    return header + bytes(frame)


def enhanced_packet(frame: bytes, timestamp_us: int, *, channel: int | None,
                    rssi: int | None) -> bytes:
    packet = radiotap_frame(frame, channel, rssi)
    timestamp = max(0, int(timestamp_us))
    body = struct.pack(
        "<IIIII", 0, timestamp >> 32, timestamp & 0xFFFFFFFF,
        len(packet), len(packet),
    ) + _pad4(packet)
    return _block(EPB, body)


class PcapngWriter:
    """Append-only single-interface PCAPNG writer using microsecond timestamps."""

    def __init__(self, fileobj, *, hardware="ESP32-C5",
                 os_name="Linux", application="WatchDogsGo"):
        self.file = fileobj
        self.file.write(section_header(
            hardware=hardware, os_name=os_name, application=application))
        self.file.write(interface_description(
            name="projectZero", description="ESP32-C5 802.11 monitor frames"))

    def write_packet(self, frame: bytes, at: float, *, channel: int | None,
                     rssi: int | None) -> None:
        self.file.write(enhanced_packet(
            frame, int(at * 1_000_000), channel=channel, rssi=rssi))


def _iter_blocks(data: bytes | bytearray):
    offset = 0
    while offset < len(data):
        if len(data) - offset < 12:
            raise ValueError("truncated PCAPNG block header")
        kind, size = struct.unpack_from("<II", data, offset)
        if size < 12 or size & 3 or offset + size > len(data):
            raise ValueError("invalid PCAPNG block length")
        if struct.unpack_from("<I", data, offset + size - 4)[0] != size:
            raise ValueError("PCAPNG block length trailer mismatch")
        yield offset, kind, size
        offset += size
    if offset != len(data):
        raise ValueError("trailing PCAPNG data")


def validate_pcapng(data: bytes | bytearray) -> PcapngInfo:
    """Validate the subset WDG emits and return packet/timestamp information."""
    blocks = list(_iter_blocks(data))
    if not blocks or blocks[0][1] != SHB or blocks[0][0] != 0:
        raise ValueError("PCAPNG must begin with a Section Header Block")
    if blocks[0][2] < 28 or struct.unpack_from("<I", data, 8)[0] != BYTE_ORDER_MAGIC:
        raise ValueError("unsupported PCAPNG byte order")
    interfaces = 0
    timestamps = []
    for offset, kind, size in blocks:
        if kind == IDB:
            if size < 20:
                raise ValueError("short Interface Description Block")
            linktype = struct.unpack_from("<H", data, offset + 8)[0]
            if linktype != LINKTYPE_IEEE802_11_RADIOTAP:
                raise ValueError("capture is not radiotap 802.11")
            interfaces += 1
        elif kind == EPB:
            if size < 32 or not interfaces:
                raise ValueError("Enhanced Packet Block precedes its interface")
            interface, high, low, captured, original = struct.unpack_from(
                "<IIIII", data, offset + 8)
            if interface >= interfaces or captured > original:
                raise ValueError("invalid Enhanced Packet Block metadata")
            if 28 + ((captured + 3) & ~3) + 4 > size:
                raise ValueError("truncated Enhanced Packet Block payload")
            if captured < 15:
                raise ValueError("short radiotap packet")
            radiotap_len = struct.unpack_from("<H", data, offset + 30)[0]
            if radiotap_len < 8 or radiotap_len > captured:
                raise ValueError("invalid radiotap header length")
            timestamps.append((high << 32) | low)
    if not interfaces:
        raise ValueError("PCAPNG has no interface")
    return PcapngInfo(
        packets=len(timestamps),
        first_timestamp_us=min(timestamps) if timestamps else None,
        last_timestamp_us=max(timestamps) if timestamps else None,
    )


def rebase_boot_timestamps(data: bytes, *, now_us: int | None = None) -> bytes:
    """Move boot-relative EPB timestamps to wall clock while preserving gaps."""
    info = validate_pcapng(data)
    if info.last_timestamp_us is None or info.last_timestamp_us >= _EPOCH_2000_US:
        return data
    target = int(now_us if now_us is not None else time.time_ns() // 1000)
    offset_us = max(0, target - info.last_timestamp_us)
    out = bytearray(data)
    for block_offset, kind, _ in _iter_blocks(out):
        if kind != EPB:
            continue
        high, low = struct.unpack_from("<II", out, block_offset + 12)
        timestamp = ((high << 32) | low) + offset_us
        struct.pack_into(
            "<II", out, block_offset + 12,
            timestamp >> 32, timestamp & 0xFFFFFFFF,
        )
    validate_pcapng(out)
    return bytes(out)
