import json
import os
import stat

import pytest

from watchdogs.reticulum_config import (
    BANDWIDTH_CHOICES,
    ReticulumConfigError,
    ReticulumProfile,
    append_history,
    history_path,
    load_history,
    load_profile,
    profile_path,
    save_profile,
    validate_destination_hash,
    validate_message_text,
)


def test_profile_is_seeded_from_meshcore_without_confirmation():
    profile = ReticulumProfile.seed_from_meshcore(
        (910_525_000, 7, 5, 62_500, "US Narrow"))
    assert profile.frequency_hz == 910_525_000
    assert profile.bandwidth_hz == 62_500
    assert profile.spreading_factor == 7
    assert profile.coding_rate == 5
    assert profile.confirmed is False


@pytest.mark.parametrize("bandwidth", BANDWIDTH_CHOICES)
def test_every_documented_bandwidth_is_valid(bandwidth):
    assert ReticulumProfile(bandwidth_hz=bandwidth).validate().bandwidth_hz \
        == bandwidth


@pytest.mark.parametrize("updates", [
    {"frequency_hz": 149_999_999},
    {"frequency_hz": 960_000_001},
    {"bandwidth_hz": 100_000},
    {"spreading_factor": 4},
    {"spreading_factor": 13},
    {"coding_rate": 4},
    {"coding_rate": 9},
    {"tx_power_dbm": -10},
    {"tx_power_dbm": 23},
    {"airtime_short_percent": 0},
    {"airtime_long_percent": 11, "airtime_short_percent": 10},
    {"network_name": "private", "network_passphrase": ""},
    {"propagation_node_hash": "AB" * 16},
    {"propagation_node_hash": "ab" * 15},
    {"propagated_outbound": True},
    {"propagated_outbound": "yes"},
])
def test_invalid_profile_ranges_are_rejected(updates):
    with pytest.raises(ReticulumConfigError):
        ReticulumProfile().with_updates(**updates)


def test_utf8_limits_are_bytes_not_characters():
    assert validate_message_text("é" * 60) == "é" * 60
    with pytest.raises(ReticulumConfigError):
        validate_message_text("é" * 61)
    with pytest.raises(ReticulumConfigError):
        ReticulumProfile(display_name="é" * 33).validate()


def test_destination_must_be_lowercase_32_hex():
    assert validate_destination_hash("ab" * 16) == "ab" * 16
    for invalid in ("AB" * 16, "ab" * 15, "gg" * 16, ""):
        with pytest.raises(ReticulumConfigError):
            validate_destination_hash(invalid)


def test_optional_propagation_profile_is_validated_and_round_trips(tmp_path):
    profile = ReticulumProfile(
        confirmed=True,
        propagation_node_hash="90ab9d448f17f3a121dc0f1230af39be",
        propagated_outbound=True,
    ).validate()
    save_profile(tmp_path, profile)
    loaded = load_profile(tmp_path)
    assert loaded.propagation_node_hash == profile.propagation_node_hash
    assert loaded.propagated_outbound is True


def test_profile_and_history_are_private_and_atomic(tmp_path):
    profile = ReticulumProfile(confirmed=True, network_name="field",
                               network_passphrase="correct horse")
    path = save_profile(tmp_path, profile)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert load_profile(tmp_path) == profile.validate()

    append_history(tmp_path, {"event": "message", "text": "secret"})
    assert stat.S_IMODE(history_path(tmp_path).stat().st_mode) == 0o600
    assert load_history(tmp_path) == [
        {"event": "message", "text": "secret"}]


def test_corrupt_history_records_are_ignored(tmp_path):
    append_history(tmp_path, {"event": "message", "text": "first"})
    with history_path(tmp_path).open("a", encoding="utf-8") as fh:
        fh.write("not-json\n")
        fh.write(json.dumps(["not", "an", "object"]) + "\n")
        fh.write(json.dumps({"event": "message", "text": "last"}) + "\n")
    diagnostics = []
    assert [item["text"] for item in load_history(
            tmp_path, diagnostics=diagnostics)] == [
        "first", "last"]
    assert diagnostics == [
        "Ignored 2 malformed Reticulum history record(s)"]


def test_symlinked_profile_is_rejected(tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    state = tmp_path / "reticulum"
    state.mkdir()
    os.symlink(target, profile_path(tmp_path))
    with pytest.raises(ReticulumConfigError):
        load_profile(tmp_path)
