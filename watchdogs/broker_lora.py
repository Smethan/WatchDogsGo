"""LoRaRF-shaped adapter backed exclusively by ``watchdogs-sx1262d``.

WDG's packet codecs historically consumed the small LoRaRF ``SX126x`` API.
Keeping that shape at the protocol boundary avoids moving MeshCore, APRS, or
scanner semantics into the hardware manager.  This class never imports
LoRaRF, opens spidev, or manipulates GPIO.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from .sx1262_client import BrokerError, RadioPacket, SX1262Client


class BrokerLoRa:
    """Minimal LoRaRF-compatible facade for one broker protocol lease."""

    RX_SINGLE = 0
    RX_CONTINUOUS = 1
    STANDBY_RC = 0
    SLEEP_COLD_START = 0
    HEADER_EXPLICIT = 0

    IRQ_TX_DONE = 1 << 0
    IRQ_RX_DONE = 1 << 1
    IRQ_HEADER_ERR = 1 << 5
    IRQ_CRC_ERR = 1 << 6
    IRQ_PREAMBLE_DETECTED = 1 << 7
    IRQ_SYNC_WORD_VALID = 1 << 8
    IRQ_HEADER_VALID = 1 << 10
    IRQ_TIMEOUT = 1 << 9

    def __init__(self, role: str = "meshcore", *, client: SX1262Client | None = None) -> None:
        if role not in {"meshcore", "reticulum"}:
            raise ValueError("BrokerLoRa role must be meshcore or reticulum")
        self.client = client or SX1262Client(role)
        self.role = role
        self._frequency = 0
        self._sf = 0
        self._bandwidth = 0
        self._coding_rate = 0
        self._sync_word = 0x12
        self._preamble = 8
        self._header_mode = "explicit"
        self._implicit_length = 0
        self._crc = True
        self._invert_iq = False
        self._tx_power = 22
        self._configured = False
        self._rx_continuous = False
        self._packets: deque[RadioPacket] = deque(maxlen=128)
        self._packet: RadioPacket | None = None
        self._last_packet: RadioPacket | None = None
        self._buffer_index = 0
        self._payloadTxRx = 0
        self._irq = 0
        self._tx_payload = bytearray()
        self._tx_pending = False
        self._lease_ready = False
        self._revoking = False
        self._last_metrics: dict[str, Any] = {}

    def begin(self, **_ignored: Any) -> bool:
        """Connect and wait for the controller-selected protocol lease."""
        self.client.connect()
        deadline = time.monotonic() + max(0.1, self.client.timeout)
        while time.monotonic() < deadline:
            self._pump_events()
            if self._lease_ready:
                return True
            try:
                self._last_metrics = self.client.get_metrics()
                self._lease_ready = True
                return True
            except BrokerError:
                self._pump_events()
                if self._lease_ready:
                    return True
                time.sleep(0.05)
        return False

    def close(self) -> None:
        self.client.close()
        self._lease_ready = False
        self._configured = False

    def end(self) -> None:
        self.close()

    def acknowledge_revoke(self, timeout: float = 2.0) -> bool:
        """Wait for ``prepare_revoke`` and prove protocol cleanup."""
        deadline = time.monotonic() + max(0.1, timeout)
        while time.monotonic() < deadline:
            self._pump_events()
            if self._revoking:
                self.client.quiesced()
                self._lease_ready = False
                return True
            # A rejected request still drains events received ahead of the
            # response, including prepare_revoke.
            try:
                self.client.get_metrics()
            except BrokerError:
                pass
            time.sleep(0.02)
        return False

    def setFrequency(self, frequency: int) -> None:
        self._frequency = int(frequency)
        self._configured = False

    def setLoRaModulation(self, sf: int, bandwidth: int, coding_rate: int,
                          _low_data_rate: bool = False) -> None:
        self._sf = int(sf)
        self._bandwidth = int(bandwidth)
        self._coding_rate = int(coding_rate)
        self._configured = False

    def setSyncWord(self, sync_word: int) -> None:
        self._sync_word = int(sync_word)
        self._configured = False

    def setLoRaPacket(self, header_type: int, preamble: int,
                      payload_length: int, crc: bool,
                      invert_iq: bool = False) -> None:
        self._header_mode = "implicit" if int(header_type) else "explicit"
        self._implicit_length = int(payload_length) if self._header_mode == "implicit" else 0
        self._preamble = int(preamble)
        self._crc = bool(crc)
        self._invert_iq = bool(invert_iq)
        self._configured = False

    def setTxPower(self, power: int, *_ignored: Any) -> None:
        self._tx_power = int(power)
        self._configured = False

    def setDio2RfSwitch(self, _enabled: bool) -> None:
        """Board wiring is manager-owned; retained only for compatibility."""

    def setDio3TcxoCtrl(self, *_ignored: Any) -> None:
        """TCXO configuration is manager-owned."""

    def setRxGain(self, *_ignored: Any) -> None:
        """RX gain policy is manager-owned."""

    def _apply_configuration(self) -> None:
        if self._configured:
            return
        if not all((self._frequency, self._sf, self._bandwidth, self._coding_rate)):
            raise RuntimeError("incomplete SX1262 PHY configuration")
        self.client.configure_phy(
            frequency=self._frequency,
            bandwidth=self._bandwidth,
            spreading_factor=self._sf,
            coding_rate=self._coding_rate,
            sync_word=self._sync_word,
            preamble_length=self._preamble,
            header_mode=self._header_mode,
            implicit_length=self._implicit_length,
            crc=self._crc,
            iq_inversion=self._invert_iq,
            tx_power=self._tx_power,
        )
        self._configured = True

    def request(self, mode: int) -> bool:
        self._apply_configuration()
        self.client.start_rx()
        self._rx_continuous = mode == self.RX_CONTINUOUS
        return True

    def getIrqStatus(self) -> int:
        try:
            self._last_metrics = self.client.get_metrics()
        except BrokerError:
            self._pump_events()
            raise
        self._pump_events()
        if self._packets and self._packet is None:
            self._irq |= self.IRQ_RX_DONE
        return self._irq

    def clearIrqStatus(self, mask: int) -> None:
        self._irq &= ~int(mask)

    def getRxBufferStatus(self) -> tuple[int, int]:
        self._load_packet()
        return (len(self._packet.payload) if self._packet else 0, 0)

    def available(self) -> int:
        self._load_packet()
        if self._packet is None:
            return 0
        return max(0, len(self._packet.payload) - self._buffer_index)

    def read(self) -> int:
        if not self.available():
            return -1
        assert self._packet is not None
        value = self._packet.payload[self._buffer_index]
        self._buffer_index += 1
        if self._buffer_index >= len(self._packet.payload):
            self._packet = None
            self._buffer_index = 0
            if self._packets:
                self._irq |= self.IRQ_RX_DONE
        return value

    def readBuffer(self, _index: int, length: int) -> bytes:
        return bytes(self.read() for _ in range(min(int(length), self.available())))

    def packetRssi(self) -> float:
        packet = self._packet or self._last_packet
        return packet.rssi if packet else -128.0

    def snr(self) -> float:
        packet = self._packet or self._last_packet
        return packet.snr if packet else 0.0

    def rssiInst(self) -> float:
        return float(self._last_metrics.get("rssi", self.packetRssi()))

    def beginPacket(self) -> None:
        self._tx_payload.clear()

    def write(self, payload: list[int] | bytes | bytearray, length: int | None = None) -> int:
        raw = bytes(payload)
        if length is not None:
            raw = raw[:int(length)]
        self._tx_payload.extend(raw)
        return len(raw)

    def endPacket(self, _timeout_ms: int = 5000) -> bool:
        self._apply_configuration()
        if not self._tx_payload:
            return False
        self._irq &= ~(self.IRQ_TX_DONE | self.IRQ_TIMEOUT)
        self._cad_busy = False
        self.client.cad()
        self._pump_events()
        if self._last_cad_busy():
            return False
        self.client.transmit(bytes(self._tx_payload))
        self._tx_pending = True
        return True

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            try:
                self._last_metrics = self.client.get_metrics()
            except BrokerError:
                self._pump_events()
                raise
            self._pump_events()
            if self._irq & (self.IRQ_TX_DONE | self.IRQ_TIMEOUT):
                return bool(self._irq & self.IRQ_TX_DONE)
            if self._tx_pending:
                pass
            elif self.available() > 0:
                return True
            time.sleep(0.02)
        if self._tx_pending:
            self._tx_pending = False
            self._irq |= self.IRQ_TIMEOUT
        return False

    def setStandby(self, _mode: int = STANDBY_RC) -> None:
        self.client.standby()

    def setBufferBaseAddress(self, *_ignored: Any) -> None:
        """The broker owns hardware FIFO offsets."""

    def sleep(self, *_ignored: Any) -> None:
        try:
            self.client.sleep()
        except BrokerError:
            # A transition may already have revoked the lease.
            pass

    def _load_packet(self) -> None:
        self._pump_events()
        if self._packet is None and self._packets:
            self._packet = self._packets.popleft()
            self._last_packet = self._packet
            self._buffer_index = 0
            self._payloadTxRx = len(self._packet.payload)

    def _last_cad_busy(self) -> bool:
        return bool(getattr(self, "_cad_busy", False))

    def _pump_events(self) -> None:
        while True:
            event = self.client.next_event(timeout=0)
            if event is None:
                return
            name = event.get("event")
            if name == "lease_granted":
                self._lease_ready = True
                self._revoking = False
            elif name == "prepare_revoke":
                self._revoking = True
            elif name == "lease_revoked":
                self._lease_ready = False
            elif name == "rx_packet":
                if len(self._packets) == self._packets.maxlen:
                    self._packets.popleft()
                self._packets.append(self.client.radio_packet(event))
                self._irq |= self.IRQ_RX_DONE
            elif name == "cad_result":
                self._cad_busy = bool(event.get("detected"))
            elif name == "tx_done":
                self._tx_pending = False
                self._irq |= self.IRQ_TX_DONE
            elif name == "tx_failed":
                self._tx_pending = False
                self._irq |= self.IRQ_TIMEOUT
            elif name in {"radio_fault", "power_changed"}:
                self._lease_ready = False


__all__ = ["BrokerLoRa"]
