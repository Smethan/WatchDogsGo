"""LoRa initialization reports missing AIO device paths clearly."""

import sys
from pathlib import Path
from types import SimpleNamespace as NS

from watchdogs.lora_manager import (
    LoRaManager, lora_spi_device, missing_spi_message)


def test_lora_spi_device_uses_official_aio_path(monkeypatch):
    monkeypatch.delenv("WDG_LORA_SPI_DEVICE", raising=False)
    assert lora_spi_device() == Path("/dev/spidev1.0")


def test_lora_spi_device_allows_test_and_specialized_override(monkeypatch):
    monkeypatch.setenv("WDG_LORA_SPI_DEVICE", "/tmp/spidev-test")
    assert lora_spi_device() == Path("/tmp/spidev-test")


def test_missing_spi_message_names_device_setup_and_reboot():
    message = missing_spi_message("/dev/spidev1.0")
    assert "/dev/spidev1.0" in message
    assert "WDG_ENABLE_AIO_LORA=1" in message
    assert "reboot" in message


def test_radio_preflight_stops_before_lorarf_touches_missing_spi(
        monkeypatch, tmp_path):
    missing = tmp_path / "spidev1.0"
    monkeypatch.setenv("WDG_LORA_SPI_DEVICE", str(missing))

    class MustNotConstruct:
        def __init__(self):
            raise AssertionError("LoRaRF must not touch a missing SPI device")

    monkeypatch.setitem(sys.modules, "LoRaRF", NS(SX126x=MustNotConstruct))
    manager = LoRaManager()

    assert manager._init_radio() is None
    text, attr = manager.queue.get_nowait()
    assert str(missing) in text and "reboot" in text
    assert attr == "error"
