from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from configure_sx1262_stack import broker_meshtastic_config, manager_config


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


def test_manager_config_contains_only_hardware_facts():
    value = manager_config(4)
    assert "spidev: spidev1.0" in value
    assert "gpiochip: 4" in value
    assert "IRQ: 26" in value and "Busy: 24" in value and "Reset: 25" in value
    assert "pin: 16" in value
    for forbidden in ("frequency", "region", "channel", "identity", "psk"):
        assert forbidden not in value.lower()


def test_setup_is_only_stack_mutation_entrypoint():
    setup = (ROOT / "setup.sh").read_text()
    assert "verify-pinned-tag" in setup
    assert "converge-tag" in setup
    assert "configure_sx1262_stack.py" in setup
    assert "rm -f /etc/sudoers.d/watchdogs-meshtastic" in setup
    assert "mask meshtasticd.service" in setup
