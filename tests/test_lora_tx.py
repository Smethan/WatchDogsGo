"""LoRa polling-mode transmit and receive recovery tests."""

import sys
import struct
from types import SimpleNamespace as NS
from unittest.mock import Mock

from watchdogs.lora_manager import (
    LoRaManager, LORARF_IRQ_POLLING, MC_DISCOVERY_INTERVAL,
    MC_DISCOVERY_TYPE_FILTER, MC_DISCOVERY_WINDOW,
)


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


def test_meshcore_discovery_request_is_direct_zero_hop_and_filters_nodes():
    tag = bytes.fromhex("01020304")

    packet = LoRaManager._build_mc_discovery_request(tag)

    assert packet == (
        bytes([0x2E, 0x00, 0x80, MC_DISCOVERY_TYPE_FILTER])
        + tag + b"\x00\x00\x00\x00")
    assert MC_DISCOVERY_TYPE_FILTER == 0x0C


def test_meshcore_discovery_matches_tag_and_keeps_best_response(monkeypatch):
    manager = LoRaManager()
    manager.running = True
    manager.mode = "meshcore"
    manager._on_node = Mock()
    tag = bytes.fromhex("10203040")
    monkeypatch.setattr("watchdogs.lora_manager.os.urandom", lambda size: tag)

    assert manager.send_meshcore_discovery(40.1, -90.2, now=100) == tag
    packet = manager._tx_queue.get_nowait()
    assert packet[0] == 0x2E
    manager._mark_meshcore_discovery_tx(packet, True)
    deadline = manager._mc_discovery["deadline"]
    public_key = bytes.fromhex("0ce8abcd" + "ab" * 28)
    response = bytearray(
        bytes([0x92]) + struct.pack("b", -8) + tag + public_key)

    manager._decode_mc_control(response, -85, 2.0)
    manager._decode_mc_control(response, -72, 7.5)
    manager._decode_mc_control(
        bytearray(bytes([0x92, 0]) + b"bad!" + public_key), -30, 12)

    assert manager._finish_meshcore_discovery(
        now=deadline + MC_DISCOVERY_WINDOW) == 1
    manager._on_node.assert_called_once_with(
        "0ce8abcd", "Repeater", "0ce8abcd", 40.1, -90.2,
        -72.0, 7.5, public_key, 0)
    assert manager._known_pubkeys["0ce8abcd"] == public_key


def test_meshcore_discovery_pacing_and_failed_tx_cleanup(monkeypatch):
    manager = LoRaManager()
    manager.running = True
    manager.mode = "meshcore"
    tags = iter((b"one!", b"two!"))
    clock = [100.0]
    monkeypatch.setattr(
        "watchdogs.lora_manager.os.urandom", lambda size: next(tags))
    monkeypatch.setattr(
        "watchdogs.lora_manager.time.monotonic", lambda: clock[0])

    assert manager.send_meshcore_discovery(1, 2, now=100) == b"one!"
    packet = manager._tx_queue.get_nowait()
    manager._mark_meshcore_discovery_tx(packet, True)
    manager._finish_meshcore_discovery(force=True)
    clock[0] = 100 + MC_DISCOVERY_INTERVAL - 0.1
    assert manager.send_meshcore_discovery(
        1, 2, now=clock[0]) is None
    clock[0] = 100 + MC_DISCOVERY_INTERVAL
    assert manager.send_meshcore_discovery(
        1, 2, now=clock[0]) == b"two!"
    failed = manager._tx_queue.get_nowait()
    manager._mark_meshcore_discovery_tx(failed, False)
    assert manager._mc_discovery is None


def test_meshcore_discovery_preserves_receive_window_before_more_tx(
        monkeypatch):
    manager = _configured_manager(packet=b"ordinary-before")
    manager.running = True
    manager.mode = "meshcore"
    manager._tx_queue.get_nowait()
    monkeypatch.setattr(
        "watchdogs.lora_manager.os.urandom", lambda size: b"disc")
    assert manager.send_meshcore_discovery(40, -90, now=10) == b"disc"
    manager._tx_queue.put(b"ordinary-after")
    radio = FakeRadio()

    manager._do_tx(radio)

    assert manager._meshcore_discovery_listening()
    assert manager._tx_queue.get_nowait() == b"ordinary-after"
