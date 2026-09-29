from __future__ import annotations

import base64
from collections import deque

from watchdogs.broker_lora import BrokerLoRa


class FakeClient:
    timeout = 0.2

    def __init__(self):
        self.events = deque([{"type": "event", "event": "lease_granted"}])
        self.config = None
        self.transmitted = []
        self.closed = False
        self.rx_started = False

    def connect(self):
        return object()

    def next_event(self, timeout=0):
        return self.events.popleft() if self.events else None

    def get_metrics(self):
        return {"rssi": -112}

    def configure_phy(self, **config):
        self.config = config
        return {}

    def start_rx(self):
        self.rx_started = True
        return {}

    def standby(self):
        return {}

    def sleep(self):
        return {}

    def cad(self):
        self.events.append({"type": "event", "event": "cad_result", "detected": False})
        return {}

    def transmit(self, payload):
        self.transmitted.append(payload)
        self.events.append({"type": "event", "event": "tx_done"})
        return {}

    def quiesced(self):
        return {}

    def close(self):
        self.closed = True

    @staticmethod
    def radio_packet(event):
        from watchdogs.sx1262_client import RadioPacket
        return RadioPacket(
            base64.b64decode(event["payload"]), event["rssi"], event["snr"],
            event.get("frequency_error", 0), event["monotonic_ns"])


def configured_radio():
    client = FakeClient()
    radio = BrokerLoRa("meshcore", client=client)
    assert radio.begin()
    radio.setFrequency(869_618_000)
    radio.setLoRaModulation(8, 62_500, 5, False)
    radio.setSyncWord(0x1424)
    radio.setLoRaPacket(radio.HEADER_EXPLICIT, 16, 255, True)
    assert radio.request(radio.RX_CONTINUOUS)
    return radio, client


def test_configuration_is_forwarded_without_board_wiring():
    radio, client = configured_radio()
    assert client.config == {
        "frequency": 869_618_000,
        "bandwidth": 62_500,
        "spreading_factor": 8,
        "coding_rate": 5,
        "sync_word": 0x1424,
        "preamble_length": 16,
        "header_mode": "explicit",
        "implicit_length": 0,
        "crc": True,
        "iq_inversion": False,
        "tx_power": 22,
    }
    assert client.rx_started


def test_receive_preserves_payload_and_metrics_after_buffer_is_consumed():
    radio, client = configured_radio()
    client.events.append({
        "type": "event", "event": "rx_packet",
        "payload": base64.b64encode(b"mesh").decode(),
        "rssi": -73.0, "snr": 8.25, "frequency_error": 12.0,
        "monotonic_ns": 123,
    })
    assert radio.getIrqStatus() & radio.IRQ_RX_DONE
    assert radio.getRxBufferStatus() == (4, 0)
    assert radio.readBuffer(0, 4) == b"mesh"
    assert radio.packetRssi() == -73.0
    assert radio.snr() == 8.25


def test_cad_transmit_completion_and_revoke_acknowledgement():
    radio, client = configured_radio()
    radio.beginPacket()
    radio.write(b"hello", 5)
    assert radio.endPacket()
    assert radio.wait(0.2)
    assert radio.getIrqStatus() & radio.IRQ_TX_DONE
    assert client.transmitted == [b"hello"]

    client.events.append({"type": "event", "event": "prepare_revoke"})
    assert radio.acknowledge_revoke()
    radio.close()
    assert client.closed
