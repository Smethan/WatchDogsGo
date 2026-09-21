"""LoRa polling-mode transmit and receive recovery tests."""

import sys
from types import SimpleNamespace as NS

from watchdogs.lora_manager import LoRaManager, LORARF_IRQ_POLLING


class FakeRadio:
    IRQ_TX_DONE = 0x0001
    IRQ_TIMEOUT = 0x0200
    RX_CONTINUOUS = 0xFFFFFF
    STANDBY_RC = 0

    def __init__(self, irq=IRQ_TX_DONE, waited=True, end_started=True):
        self.irq = irq
        self.waited = waited
        self.end_started = end_started
        self.calls = []

    def beginPacket(self):
        self.calls.append(("beginPacket",))

    def write(self, data, length):
        self.calls.append(("write", data, length))

    def endPacket(self, timeout):
        self.calls.append(("endPacket", timeout))
        return self.end_started

    def wait(self, timeout):
        self.calls.append(("wait", timeout))
        return self.waited

    def getIrqStatus(self):
        self.calls.append(("getIrqStatus",))
        return self.irq

    def setStandby(self, mode):
        self.calls.append(("setStandby", mode))

    def clearIrqStatus(self, mask):
        self.calls.append(("clearIrqStatus", mask))

    def setBufferBaseAddress(self, tx_base, rx_base):
        self.calls.append(("setBufferBaseAddress", tx_base, rx_base))

    def setFrequency(self, frequency):
        self.calls.append(("setFrequency", frequency))

    def setLoRaModulation(self, sf, bw, cr, ldro):
        self.calls.append(("setLoRaModulation", sf, bw, cr, ldro))

    def setSyncWord(self, sync_word):
        self.calls.append(("setSyncWord", sync_word))

    def setLoRaPacket(self, header, preamble, payload, crc):
        self.calls.append(("setLoRaPacket", header, preamble, payload, crc))

    def request(self, timeout):
        self.calls.append(("request", timeout))
        return True


def _configured_manager(packet=b"mesh-packet"):
    manager = LoRaManager()
    manager._radio_cfg = (869_618_000, 8, 5, 62_500, 0x1424, 16)
    manager._tx_queue.put(packet)
    return manager


def test_meshcore_tx_uses_lorarf_wait_then_public_receive_request():
    manager = _configured_manager()
    radio = FakeRadio()

    manager._do_tx(radio)

    names = [call[0] for call in radio.calls]
    assert names[:5] == [
        "beginPacket", "write", "endPacket", "wait", "getIrqStatus"]
    assert ("endPacket", 5000) in radio.calls
    assert ("wait", 5.5) in radio.calls
    assert radio.calls[-1] == ("request", radio.RX_CONTINUOUS)
    text, attr = manager.queue.get_nowait()
    assert "sent" in text
    assert attr == "success"


def test_meshcore_tx_timeout_is_reported_and_receive_resumes():
    manager = _configured_manager()
    radio = FakeRadio(irq=FakeRadio.IRQ_TIMEOUT, waited=True)

    manager._do_tx(radio)

    text, attr = manager.queue.get_nowait()
    assert "failed: radio timeout" in text
    assert attr == "error"
    assert radio.calls[-1] == ("request", radio.RX_CONTINUOUS)


def test_radio_initialization_disables_gpio_irq_callbacks(
        monkeypatch, tmp_path):
    spi = tmp_path / "spidev1.0"
    spi.touch()
    monkeypatch.setenv("WDG_LORA_SPI_DEVICE", str(spi))
    instances = []

    class InitRadio:
        DIO3_OUTPUT_1_8 = 2
        RX_GAIN_BOOSTED = 1
        TX_POWER_SX1262 = 2

        def __init__(self):
            self.begin_kwargs = None
            instances.append(self)

        def begin(self, **kwargs):
            self.begin_kwargs = kwargs
            return True

        def setDio2RfSwitch(self, enabled):
            pass

        def setDio3TcxoCtrl(self, voltage, delay):
            pass

        def setRxGain(self, gain):
            pass

        def setTxPower(self, power, chip=None):
            pass

    monkeypatch.setitem(sys.modules, "LoRaRF", NS(SX126x=InitRadio))
    manager = LoRaManager()

    assert manager._init_radio() is instances[0]
    assert instances[0].begin_kwargs["irq"] == LORARF_IRQ_POLLING == -1
