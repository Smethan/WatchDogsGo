"""LoRa initialization fails visibly when the manager is unavailable."""

from watchdogs.lora_manager import LoRaManager


def test_radio_preflight_reports_manager_unavailable_without_spi_fallback(
        monkeypatch):
    class UnavailableBroker:
        def __init__(self, role):
            assert role == "meshcore"

        def begin(self):
            raise RuntimeError("manager socket unavailable")

        def close(self):
            pass

    monkeypatch.setattr("watchdogs.lora_manager.BrokerLoRa", UnavailableBroker)
    manager = LoRaManager()

    assert manager._init_radio() is None
    text, attr = manager.queue.get_nowait()
    assert "manager socket unavailable" in text
    assert attr == "error"
