"""Synthetic format-2 firmware geometry and profile rejection tests."""
import hashlib
import io
import json
import struct
import zipfile

import pytest

from watchdogs.config import FIRMWARE_REPO, FLASH_BOARDS
from watchdogs.updates import validate_bundle, validate_geometry


VERSION = "9.99.0"


def _parts(board, extra=(), nvs_size=0x6000):
    if board == "waveshare-zero":
        entries = [
            (0, 0, 0, 0x20000, 0x3E0000, "factory"),
            (1, 2, 0, 0x9000, nvs_size, "nvs"),
        ]
    else:
        entries = [
            (0, 0x10, 0, 0x20000, 0x3F0000, "ota_0"),
            (0, 0x11, 0, 0x410000, 0x3F0000, "ota_1"),
            (1, 2, 0, 0x9000, nvs_size, "nvs"),
        ]
    entries.extend(extra)
    raw = b"".join(struct.pack("<HBBII16sI", 0x50AA, kind, subtype, offset, size,
                               name.encode().ljust(16, b"\0"), flags)
                    for kind, subtype, flags, offset, size, name in entries)
    return raw + b"\xff" * 32, [dict(name=name, type=kind, subtype=subtype,
                                    offset=offset, size=size, flags=flags)
                                for kind, subtype, flags, offset, size, name in entries]


def _bundle(board="wroom", *, nvs_size=0x6000, extra=(), modify_app_after_manifest=False):
    profile = FLASH_BOARDS[board]
    partition, partitions = _parts(board, extra, nvs_size)
    files = {
        "bootloader.bin": b"bootloader-fixture",
        "partition-table.bin": partition,
        "ota_data_initial.bin": b"ota-data-fixture",
        profile["bin_name"]: b"\xA5" * profile["app_limit"],
    }
    manifest = {
        "format": 2,
        "repository": FIRMWARE_REPO,
        "board": board,
        "chip": "esp32c5",
        "version": VERSION,
        "flash_bytes": profile["flash_bytes"],
        "application_limit": profile["app_limit"],
        "application_offset": 0x20000,
        "application_size": 0x3E0000 if board == "waveshare-zero" else 0x3F0000,
        "console": "uart" if board == "wroom" else "usb-serial-jtag",
        "updates": ["rom"] if board == "waveshare-zero" else ["usb-ota", "rom"],
        "psram": board != "waveshare-zero",
        "partitions": partitions,
        "files": {
            name: {"offset": profile["offsets"][name], "size": len(data),
                   "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in files.items()
        },
    }
    if modify_app_after_manifest:
        app = profile["bin_name"]
        files[app] = b"\x5A" * profile["app_limit"]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr("manifest.json", json.dumps(manifest, separators=(",", ":")))
    return out.getvalue(), files, manifest


def test_format2_full_application_with_generated_partition_geometry_is_accepted():
    blob, files, manifest = _bundle("wroom")
    validated = validate_bundle(blob, "wroom", VERSION)
    assert validated["projectZerobyLOCOSP.bin"] == files["projectZerobyLOCOSP.bin"]
    assert len(validated["projectZerobyLOCOSP.bin"]) == FLASH_BOARDS["wroom"]["app_limit"]
    assert validate_geometry(manifest, files, "wroom", {
        "board": "wroom", "flash_bytes": 8 * 1024 * 1024,
    }) is manifest


def test_zero_profile_geometry_accepts_rom_only_update_assets_and_four_megabytes():
    blob, files, manifest = _bundle("waveshare-zero")
    validated = validate_bundle(blob, "waveshare-zero", VERSION)
    profile = FLASH_BOARDS["waveshare-zero"]
    assert len(validated[profile["bin_name"]]) == profile["app_limit"]
    assert manifest["flash_bytes"] == 4 * 1024 * 1024
    assert manifest["updates"] == ["rom"] and manifest["psram"] is False
    assert validate_geometry(manifest, files, "waveshare-zero", {
        "board": "waveshare-zero", "flash_bytes": 4 * 1024 * 1024,
    }) is manifest


def test_selected_profile_must_match_connected_board_and_flash_size():
    blob, files, manifest = _bundle("xiao")
    with pytest.raises(ValueError, match="profile differs"):
        validate_geometry(manifest, files, "xiao", {
            "board": "wroom", "flash_bytes": 8 * 1024 * 1024,
        })
    with pytest.raises(ValueError, match="flash is too small"):
        validate_geometry(manifest, files, "xiao", {
            "board": "xiao", "flash_bytes": 4 * 1024 * 1024,
        })


def test_nvs_resize_and_partition_overlap_are_rejected_from_partition_bytes():
    _blob, files, manifest = _bundle("wroom", nvs_size=0x7000)
    with pytest.raises(ValueError, match="NVS partition layout"):
        validate_geometry(manifest, files, "wroom")

    overlap = (1, 0x82, 0, 0x20000, 0x1000, "overlap")
    _blob, files, manifest = _bundle("wroom", extra=[overlap])
    with pytest.raises(ValueError, match="overlap"):
        validate_geometry(manifest, files, "wroom")


def test_modified_application_bytes_are_rejected_even_when_archive_geometry_matches():
    blob, _files, _manifest = _bundle("wroom", modify_app_after_manifest=True)
    with pytest.raises(ValueError, match="file verification"):
        validate_bundle(blob, "wroom", VERSION)
