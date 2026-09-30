import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from configure_sx1262_stack import (
    atomic_write,
    broker_meshtastic_config,
    is_broker_meshtastic_config,
    lora_mapping,
    manager_config,
    replace_lora_mapping,
)


def test_meshtastic_lora_hardware_mapping_becomes_broker_idempotently():
    source = (
        "---\nLora:\n  Module: sx1262\n  spidev: spidev1.0\n"
        "  Reset: 25\nGPS:\n  GpsdHost: 127.0.0.1\n")
    expected = (
        "---\nLora:\n"
        "  # WDG unified manager is the sole SPI/GPIO/power owner.\n"
        "  Module: broker\n"
        "  BrokerSocket: /run/watchdogs/sx1262d.sock\n"
        "GPS:\n  GpsdHost: 127.0.0.1\n")
    assert broker_meshtastic_config(source) == expected
    assert broker_meshtastic_config(expected) == expected
    assert is_broker_meshtastic_config(expected)
    assert not is_broker_meshtastic_config(source)
    assert replace_lora_mapping(expected, lora_mapping(source)) == source


def test_atomic_write_repairs_content_and_metadata_without_following_symlink(
        tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o600)

    assert atomic_write(
        target, "new\n", uid=target.stat().st_uid,
        gid=target.stat().st_gid, mode=0o640)
    assert target.read_text(encoding="utf-8") == "new\n"
    assert target.stat().st_mode & 0o777 == 0o640
    assert not atomic_write(
        target, "new\n", uid=target.stat().st_uid,
        gid=target.stat().st_gid, mode=0o640)

    link = tmp_path / "link.yaml"
    link.symlink_to(target)
    try:
        atomic_write(
            link, "bad\n", uid=target.stat().st_uid,
            gid=target.stat().st_gid, mode=0o640)
    except ValueError as exc:
        assert "symlink" in str(exc)
    else:
        raise AssertionError("protected symlink was accepted")


def test_manager_config_contains_only_hardware_facts():
    value = manager_config(4)
    assert "spidev: spidev1.0" in value
    assert "spiSpeed: 2000000" in value
    assert "7800000" not in value
    assert "gpiochip: 4" in value
    assert "IRQ: 26" in value and "Busy: 24" in value and "Reset: 25" in value
    assert "pin: 16" in value
    for forbidden in ("frequency", "region", "channel", "identity", "psk"):
        assert forbidden not in value.lower()


def test_setup_is_only_stack_mutation_entrypoint():
    setup = (ROOT / "setup.sh").read_text()
    support = (ROOT / "scripts" / "setup_meshtastic.sh").read_text()
    assert "verify-pinned-tag" in setup
    assert "converge-tag" in setup
    assert "configure_sx1262_stack.py" not in setup
    assert "configure_sx1262_stack.py" in support
    assert "rm -f /etc/sudoers.d/watchdogs-meshtastic" in setup
    assert "mask meshtasticd.service" in setup
