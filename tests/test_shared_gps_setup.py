"""Shared gpsd/Meshtastic configuration migration tests."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from configure_shared_gps import (
    configure_gpsd_defaults,
    configure_meshtastic,
)


def test_gpsd_defaults_are_owned_without_losing_unrelated_options():
    source = (
        '# existing\nSTART_DAEMON="false"\nUSBAUTO="true"\n'
        'DEVICES=""\nGPSD_OPTIONS="-G"\nOTHER="keep"\n')
    result = configure_gpsd_defaults(source, "/dev/serial0")
    assert 'START_DAEMON="true"' in result
    assert 'USBAUTO="false"' in result
    assert 'DEVICES="/dev/serial0"' in result
    assert 'GPSD_OPTIONS="-G -n"' in result
    assert 'OTHER="keep"' in result
    assert configure_gpsd_defaults(result, "/dev/serial0") == result


def test_meshtastic_platform_uart_migrates_to_gpsd_idempotently():
    source = (
        "---\nGPS:\n  SerialPath: /dev/ttyS0\n"
        "# GPS comment\n\nI2C:\n  I2CDevice: /dev/i2c-1\n")
    migrated, managed = configure_meshtastic(source, "/dev/serial0")
    assert managed
    assert "  GpsdHost: 127.0.0.1" in migrated
    assert "  GpsdPort: 2947" in migrated
    assert "WDG gpsd owns raw UART: SerialPath: /dev/ttyS0" in migrated
    assert "I2C:\n  I2CDevice" in migrated
    assert configure_meshtastic(migrated, "/dev/serial0")[0] == migrated


def test_meshtastic_external_uart_is_not_rewritten():
    source = "GPS:\n  SerialPath: /dev/ttyUSB9\nI2C:\n"
    migrated, managed = configure_meshtastic(source, "/dev/serial0")
    assert not managed
    assert migrated == source


def test_helper_writes_backups_marker_and_is_idempotent(tmp_path):
    defaults = tmp_path / "gpsd"
    meshtastic = tmp_path / "config.yaml"
    marker = tmp_path / "watchdogs" / "gpsd.conf"
    defaults.write_text('START_DAEMON="false"\n')
    meshtastic.write_text("GPS:\n  SerialPath: /dev/ttyAMA0\n")
    command = [
        sys.executable, str(ROOT / "scripts" / "configure_shared_gps.py"),
        "--gpsd-default", str(defaults),
        "--meshtastic-config", str(meshtastic),
        "--marker", str(marker),
        "--device", "/dev/serial0",
    ]
    subprocess.run(command, check=True)
    first = defaults.read_bytes(), meshtastic.read_bytes(), marker.read_bytes()
    subprocess.run(command, check=True)
    assert first == (defaults.read_bytes(), meshtastic.read_bytes(), marker.read_bytes())
    assert defaults.with_name("gpsd.wdg-before-shared-gps").exists()
    assert meshtastic.with_name("config.yaml.wdg-before-shared-gps").exists()


def test_setup_installs_and_configures_gpsd():
    setup = (ROOT / "setup.sh").read_text()
    assert "gpsd                              # one shared reader" in setup
    assert "scripts/configure_shared_gps.py" in setup
    assert "WDG_ENABLE_SHARED_GPSD" in setup
    assert "systemctl restart gpsd.service" in setup
