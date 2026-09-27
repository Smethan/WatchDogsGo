"""Reticulum external interface for the uConsole AIO v2 SX1262.

The AIO radio is not an RNode and has no serial KISS firmware.  This module
implements the small RNode-compatible LoRa framing layer directly over
LoRaRF, including split-packet reassembly and the RNode CSMA timing model.
All SPI access is serialized on one worker thread.
"""

from __future__ import annotations

import importlib
import logging
import math
import queue
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from .lora_manager import (
    LORARF_IRQ_POLLING,
    PIN_BUSY,
    PIN_RESET,
    SPI_BUS,
    SPI_CS,
    lora_spi_device,
    missing_spi_message,
)
from .radio_ownership import RadioOwnership, RadioOwnershipBusy

try:  # RNS is an optional runtime dependency for non-Reticulum WDG users.
    from RNS.Interfaces.Interface import Interface as _RNSInterface
except ImportError:  # pragma: no cover - real RNS path is covered on target
    class _RNSInterface:  # type: ignore[no-redef]
        DEFAULT_IFAC_SIZE = 16

        def __init__(self):
            self.rxb = 0
            self.txb = 0
            self.tx_drops = 0
            self.online = False
            self.detached = False

        @staticmethod
        def get_config_obj(value):
            return value


log = logging.getLogger(__name__)

RNodeEventSink = Callable[[str, dict[str, Any]], None]
_event_sink: RNodeEventSink | None = None


def set_interface_event_sink(sink: RNodeEventSink | None) -> None:
    """Install the sidecar-local event sink before RNS loads the interface."""
    global _event_sink
    _event_sink = sink


class RNodeFraming:
    """Encode and reassemble the one-byte RNode LoRa framing format."""

    MTU = 508
    RAW_MTU = 255
    HEADER_LEN = 1
    FRAGMENT_DATA = RAW_MTU - HEADER_LEN
    FLAG_SPLIT = 0x01

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic,
                 fragment_timeout: float = 2.0,
                 duplicate_window: float = 0.001) -> None:
        self._monotonic = monotonic
        self.fragment_timeout = max(0.01, float(fragment_timeout))
        self._partial_sequence: int | None = None
        self._partial = b""
        self._partial_started = 0.0
        self._last_split_frame = b""
        self._last_split_seen = 0.0
        self.duplicate_window = max(0.0, float(duplicate_window))

    @staticmethod
    def sequence(header: int) -> int:
        return (int(header) >> 4) & 0x0F

    @classmethod
    def encode(cls, payload: bytes, sequence: int) -> tuple[bytes, ...]:
        payload = bytes(payload)
        if not 1 <= len(payload) <= cls.MTU:
            raise ValueError("RNode payload must contain 1 through 508 bytes")
        header = (int(sequence) & 0x0F) << 4
        if len(payload) <= cls.FRAGMENT_DATA:
            return (bytes((header,)) + payload,)
        header |= cls.FLAG_SPLIT
        return (
            bytes((header,)) + payload[:cls.FRAGMENT_DATA],
            bytes((header,)) + payload[cls.FRAGMENT_DATA:],
        )

    def reset(self) -> None:
        self._partial_sequence = None
        self._partial = b""
        self._partial_started = 0.0

    def expire(self, now: float | None = None) -> bool:
        now = self._monotonic() if now is None else float(now)
        if (self._partial_sequence is not None
                and now - self._partial_started >= self.fragment_timeout):
            self.reset()
            return True
        return False

    def feed(self, frame: bytes, *, now: float | None = None) -> bytes | None:
        now = self._monotonic() if now is None else float(now)
        self.expire(now)
        frame = bytes(frame)
        if not 2 <= len(frame) <= self.RAW_MTU:
            self.reset()
            return None
        header, data = frame[0], frame[1:]
        if header & 0x0E:
            self.reset()
            return None
        split = bool(header & self.FLAG_SPLIT)
        sequence = self.sequence(header)
        if not split:
            self.reset()
            return data if len(data) <= self.MTU else None
        if (frame == self._last_split_frame
                and now - self._last_split_seen <= self.duplicate_window):
            return None
        self._last_split_frame = frame
        self._last_split_seen = now
        if self._partial_sequence is None or self._partial_sequence != sequence:
            self._partial_sequence = sequence
            self._partial = data
            self._partial_started = now
            return None
        payload = self._partial + data
        self.reset()
        if not 1 <= len(payload) <= self.MTU:
            return None
        return payload


class RNodeCSMA:
    """Deterministic RNode-compatible channel-access and airtime accounting."""

    FAST_THRESHOLD_BPS = 30_000
    SLOT_MAX_SECONDS = 0.100
    SLOT_MIN_SECONDS = 0.024
    SLOT_FAST_MIN_SECONDS = 0.006
    SLOT_SYMBOLS = 12
    PREAMBLE_TARGET_SECONDS = 0.024
    PREAMBLE_FAST_TARGET_SECONDS = 0.006
    PREAMBLE_MIN_SYMBOLS = 18
    CW_WINDOWS_PER_BAND = 15
    POST_TX_YIELD_SLOTS = 3
    INTERFERENCE_THRESHOLD_DB = 11.0
    SHORT_WINDOW_SECONDS = 15.0
    LONG_WINDOW_SECONDS = 3600.0

    def __init__(self, *, sf: int, bandwidth_hz: int, coding_rate: int,
                 short_limit_percent: float, long_limit_percent: float,
                 monotonic: Callable[[], float] = time.monotonic,
                 rng: random.Random | None = None) -> None:
        self.sf = int(sf)
        self.bandwidth_hz = int(bandwidth_hz)
        self.coding_rate = int(coding_rate)
        self.short_limit = float(short_limit_percent) / 100.0
        self.long_limit = float(long_limit_percent) / 100.0
        self.monotonic = monotonic
        self.rng = rng or random.SystemRandom()
        self.symbol_seconds = (2 ** self.sf) / float(self.bandwidth_hz)
        # RNode's SX1262 driver calculates LDRO with integer millisecond
        # arithmetic and enables it only when that result is strictly above
        # 16 ms. Preserve that boundary instead of using a generic LoRa
        # approximation, since mismatched LDRO prevents RF interoperability.
        bandwidth_khz = max(1, self.bandwidth_hz // 1000)
        self.low_data_rate = ((1 << self.sf) // bandwidth_khz) > 16
        self.bitrate = int(
            self.sf * ((4.0 / self.coding_rate)
                       / ((2 ** self.sf) / (self.bandwidth_hz / 1000.0)))
            * 1000.0)
        fast = self.bitrate > self.FAST_THRESHOLD_BPS
        minimum = (self.SLOT_FAST_MIN_SECONDS if fast
                   else self.SLOT_MIN_SECONDS)
        # The firmware stores the calculated slot in an integer millisecond
        # field, truncating before it applies the 24/100 ms limits.
        calculated_ms = int(self.symbol_seconds * 1000 * self.SLOT_SYMBOLS)
        calculated = min(self.SLOT_MAX_SECONDS, calculated_ms / 1000.0)
        # Preserve the pinned firmware's fast-rate adjustment exactly: its
        # threshold comparison remains the ordinary 24 ms minimum and then
        # substitutes the fast 6 ms value.
        self.slot_seconds = (
            minimum if calculated < self.SLOT_MIN_SECONDS else calculated)
        self.difs_seconds = 2 * self.slot_seconds
        target = (self.PREAMBLE_FAST_TARGET_SECONDS if fast
                  else self.PREAMBLE_TARGET_SECONDS)
        self.preamble_symbols = max(
            self.PREAMBLE_MIN_SYMBOLS,
            int(math.ceil(target / self.symbol_seconds)))
        self._airtime: deque[tuple[float, float]] = deque()
        self.post_tx_until = 0.0

    def _prune(self, now: float) -> None:
        while self._airtime and now - self._airtime[0][0] > self.LONG_WINDOW_SECONDS:
            self._airtime.popleft()

    def utilization(self, window: float, now: float | None = None) -> float:
        now = self.monotonic() if now is None else float(now)
        self._prune(now)
        used = sum(duration for ended, duration in self._airtime
                   if now - ended <= window)
        return min(1.0, used / window)

    def airtime_locked(self, now: float | None = None) -> bool:
        now = self.monotonic() if now is None else float(now)
        return (self.utilization(self.SHORT_WINDOW_SECONDS, now)
                >= self.short_limit
                or self.utilization(self.LONG_WINDOW_SECONDS, now)
                >= self.long_limit)

    def contention_band(self, now: float | None = None) -> int:
        percent = int(self.utilization(
            self.SHORT_WINDOW_SECONDS, now) * 100)
        if percent <= 7:
            return 1
        mapped = 2 + int(((percent + 7) - 7) * 2 / (85 - 7))
        return max(2, min(4, mapped))

    def contention_seconds(self, now: float | None = None) -> float:
        band = self.contention_band(now)
        low = (band - 1) * self.CW_WINDOWS_PER_BAND
        high = band * self.CW_WINDOWS_PER_BAND - 1
        # Arduino random(min, max) excludes max. The pinned firmware passes
        # cw_max (already band*15-1), so retain that exact range here.
        if hasattr(self.rng, "randrange"):
            slots = self.rng.randrange(low, high)
        else:
            slots = self.rng.randint(low, high - 1)
        return slots * self.slot_seconds

    def record_transmission(self, duration: float,
                            now: float | None = None) -> None:
        now = self.monotonic() if now is None else float(now)
        duration = max(0.0, float(duration))
        self._airtime.append((now, duration))
        self._prune(now)
        self.post_tx_until = now + self.POST_TX_YIELD_SLOTS * self.slot_seconds

    def packet_airtime(self, payload_bytes: int) -> float:
        """Return LoRa airtime using the explicit-header Semtech formula."""
        payload_bytes = max(1, int(payload_bytes))
        low_rate = 1 if self.low_data_rate else 0
        if self.sf < 7:
            numerator = 8 * payload_bytes + 16 - 4 * self.sf + 20
            denominator = 4 * self.sf
            modem_overhead = 2.25
        else:
            numerator = 8 * payload_bytes + 16 - 4 * self.sf + 8 + 20
            denominator = 4 * (self.sf - 2 * low_rate)
            modem_overhead = 0.25
        payload_symbols = (numerator / denominator) * self.coding_rate
        total_symbols = (self.preamble_symbols + modem_overhead + 8
                         + payload_symbols)
        return total_symbols * self.symbol_seconds


class AioSX1262Interface(_RNSInterface):
    """Direct AIO SX1262 transport loaded by Reticulum as an interface."""

    DEFAULT_IFAC_SIZE = 16
    FIXED_MTU = True
    HW_MTU = RNodeFraming.MTU
    MAX_QUEUE_PACKETS = 16
    MAX_QUEUE_BYTES = 8 * 1024
    POLL_SECONDS = 0.01

    def __init__(self, owner, configuration):
        super().__init__()
        config = self.get_config_obj(configuration)
        self.owner = owner
        self.name = str(config.get("name", "AIO SX1262"))
        self.HW_MTU = RNodeFraming.MTU
        self.FIXED_MTU = True
        self.shared_medium = True
        self.reports_phy_stats = True
        self.online = False
        self.detached = False
        self.bitrate = 0
        self.frequency_hz = int(config["frequency"])
        self.bandwidth_hz = int(config["bandwidth"])
        self.spreading_factor = int(config["spreading_factor"])
        self.coding_rate = int(config["coding_rate"])
        self.tx_power_dbm = int(config["tx_power"])
        self.short_limit = float(config.get("airtime_short_percent", 10.0))
        self.long_limit = float(config.get("airtime_long_percent", 2.0))
        self._monotonic = config.get("_monotonic", time.monotonic)
        self._sleep = config.get("_sleep", time.sleep)
        self._rng = config.get("_rng", random.SystemRandom())
        self._radio_factory = config.get("_radio_factory")
        self._ownership = config.get("_radio_ownership") or RadioOwnership()
        self._stop_event = threading.Event()
        self._tx_queue: queue.Queue[bytes] = queue.Queue(
            maxsize=self.MAX_QUEUE_PACKETS)
        self._tx_bytes = 0
        self._tx_lock = threading.Lock()
        self._fatal_error = ""
        self._close_uncertain = False
        self._closed_clean = False
        self._radio = None
        self._noise_samples: deque[float] = deque(maxlen=128)
        self._noise_floor: float | None = None
        self._interference_since: float | None = None
        self._carrier_since: float | None = None
        self._framing = RNodeFraming(monotonic=self._monotonic)
        self._csma = RNodeCSMA(
            sf=self.spreading_factor,
            bandwidth_hz=self.bandwidth_hz,
            coding_rate=self.coding_rate,
            short_limit_percent=self.short_limit,
            long_limit_percent=self.long_limit,
            monotonic=self._monotonic,
            rng=self._rng,
        )
        self.bitrate = self._csma.bitrate
        max_airtime = self._csma.packet_airtime(RNodeFraming.RAW_MTU)
        self._framing.fragment_timeout = max(2.0, 2 * max_airtime + 0.5)
        self._hardware_ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="wdg-reticulum-sx1262", daemon=True)
        self._thread.start()
        if not self._hardware_ready.wait(timeout=15.0):
            self._stop_event.set()
            raise TimeoutError("SX1262 worker did not initialise within 15 seconds")
        if self._fatal_error:
            raise RuntimeError(self._fatal_error)

    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        sink = _event_sink
        if sink is not None:
            try:
                sink(name, payload)
            except Exception:
                log.exception("Reticulum interface event sink failed")

    def _open_hardware(self) -> None:
        try:
            self._ownership.acquire("WatchDogsGo Reticulum")
        except RadioOwnershipBusy:
            raise
        try:
            spi_device = lora_spi_device()
            if not spi_device.exists() and self._radio_factory is None:
                raise FileNotFoundError(missing_spi_message(spi_device))
            if self._radio_factory is None:
                from LoRaRF import SX126x
                radio = SX126x()
            else:
                radio = self._radio_factory()
            self._radio = radio
            if not radio.begin(
                    bus=SPI_BUS, cs=SPI_CS, reset=PIN_RESET, busy=PIN_BUSY,
                    irq=LORARF_IRQ_POLLING):
                raise RuntimeError("SX1262 not detected on SPI bus")
            radio.setDio2RfSwitch(True)
            radio.setDio3TcxoCtrl(radio.DIO3_OUTPUT_1_8, 10)
            radio.setRxGain(radio.RX_GAIN_BOOSTED)
            try:
                radio.setTxPower(self.tx_power_dbm, radio.TX_POWER_SX1262)
            except TypeError:
                radio.setTxPower(self.tx_power_dbm)
            self._configure_radio()
            if not radio.request(radio.RX_CONTINUOUS):
                raise RuntimeError("radio refused continuous receive mode")
        except Exception:
            raise

    def _configure_radio(self) -> None:
        radio = self._radio
        radio.setFrequency(self.frequency_hz)
        radio.setLoRaModulation(
            self.spreading_factor, self.bandwidth_hz,
            self.coding_rate, self._csma.low_data_rate)
        radio.setSyncWord(0x1424)
        radio.setLoRaPacket(
            radio.HEADER_EXPLICIT, self._csma.preamble_symbols,
            RNodeFraming.RAW_MTU, True, False)

    def process_outgoing(self, data) -> None:
        if not self.online or self.detached:
            return
        payload = bytes(data)
        if not 1 <= len(payload) <= self.HW_MTU:
            self.tx_drops += 1
            self._emit("error", {
                "code": "invalid_outbound_size",
                "detail": f"Reticulum packet was {len(payload)} bytes",
                "fatal": False,
            })
            return
        with self._tx_lock:
            if (self._tx_queue.full()
                    or self._tx_bytes + len(payload) > self.MAX_QUEUE_BYTES):
                self.tx_drops += 1
                self._emit("error", {
                    "code": "tx_queue_full",
                    "detail": "Reticulum radio transmit queue is full",
                    "fatal": False,
                })
                return
            self._tx_queue.put_nowait(payload)
            self._tx_bytes += len(payload)

    def _carrier_detected(self, irq: int) -> bool:
        radio = self._radio
        mask = (radio.IRQ_PREAMBLE_DETECTED | radio.IRQ_SYNC_WORD_VALID
                | radio.IRQ_HEADER_VALID)
        return bool(irq & mask)

    def _medium_free(self) -> bool:
        radio = self._radio
        irq = radio.getIrqStatus()
        carrier = self._carrier_detected(irq)
        now = self._monotonic()
        if carrier:
            valid = bool(irq & (radio.IRQ_SYNC_WORD_VALID
                                | radio.IRQ_HEADER_VALID))
            if self._carrier_since is None:
                self._carrier_since = now
            timeout = (self._csma.packet_airtime(RNodeFraming.RAW_MTU) + 0.5
                       if valid else
                       self._csma.preamble_symbols
                       * self._csma.symbol_seconds + 0.05)
            if (not irq & radio.IRQ_RX_DONE
                    and now - self._carrier_since > timeout):
                radio.clearIrqStatus(0x03FF)
                radio.setStandby(radio.STANDBY_RC)
                self._configure_radio()
                if not radio.request(radio.RX_CONTINUOUS):
                    raise RuntimeError(
                        "radio refused receive mode after carrier recovery")
                carrier = False
                self._carrier_since = None
        else:
            self._carrier_since = None
        rssi = float(radio.rssiInst())
        if not carrier:
            if (self._noise_floor is None
                    or rssi < self._noise_floor
                    + RNodeCSMA.INTERFERENCE_THRESHOLD_DB):
                self._noise_samples.append(rssi)
                if len(self._noise_samples) == self._noise_samples.maxlen:
                    self._noise_floor = sum(self._noise_samples) / len(
                        self._noise_samples)
        interference = bool(
            not carrier and self._noise_floor is not None
            and rssi > self._noise_floor
            + RNodeCSMA.INTERFERENCE_THRESHOLD_DB)
        if interference and rssi < -83.0:
            if self._interference_since is None:
                self._interference_since = now
            elif now - self._interference_since >= 2.5:
                self._noise_samples.clear()
                self._noise_floor = None
                self._interference_since = None
                interference = False
        else:
            self._interference_since = None
        return not carrier and not interference

    def _wait_for_medium(self) -> bool:
        while not self._stop_event.is_set() and self._csma.airtime_locked():
            self._poll_receive_once()
            self._sleep(self.POLL_SECONDS)
        if self._stop_event.is_set():
            return False
        now = self._monotonic()
        while now < self._csma.post_tx_until:
            self._poll_receive_once()
            self._sleep(min(self.POLL_SECONDS,
                            self._csma.post_tx_until - now))
            now = self._monotonic()
        difs_started: float | None = None
        contention_remaining = self._csma.contention_seconds(now)
        contention_tick: float | None = None
        while not self._stop_event.is_set():
            self._poll_receive_once()
            now = self._monotonic()
            if not self._medium_free():
                difs_started = None
                contention_tick = None
                self._sleep(self.POLL_SECONDS)
                continue
            if difs_started is None:
                difs_started = now
            if now - difs_started < self._csma.difs_seconds:
                self._sleep(self.POLL_SECONDS)
                continue
            if contention_tick is None:
                contention_tick = now
            else:
                contention_remaining -= max(0.0, now - contention_tick)
                contention_tick = now
            if contention_remaining <= 0:
                return True
            self._sleep(min(self.POLL_SECONDS, contention_remaining))
        return False

    def _send_frame(self, frame: bytes) -> float:
        radio = self._radio
        radio.beginPacket()
        radio.write(list(frame), len(frame))
        timeout_seconds = max(1.0,
                              self._csma.packet_airtime(len(frame)) + 0.75)
        timeout_ms = min(0x3FFFF, int(timeout_seconds * 1000))
        if not radio.endPacket(timeout_ms):
            raise RuntimeError("radio refused Reticulum transmission")
        if not radio.wait(timeout_seconds):
            raise TimeoutError("Reticulum transmission timed out")
        irq = radio.getIrqStatus()
        if not irq & radio.IRQ_TX_DONE:
            raise RuntimeError(f"Reticulum TX completed without TX_DONE: 0x{irq:04x}")
        duration = self._csma.packet_airtime(len(frame))
        return duration

    def _transmit(self, payload: bytes) -> None:
        if not self._wait_for_medium():
            return
        sequence = self._rng.randrange(16)
        frames = RNodeFraming.encode(payload, sequence)
        total_airtime = 0.0
        radio = self._radio
        radio.setStandby(radio.STANDBY_RC)
        radio.clearIrqStatus(0x03FF)
        for frame in frames:
            total_airtime += self._send_frame(frame)
        self.txb += len(payload)
        self._csma.record_transmission(total_airtime)
        radio.setStandby(radio.STANDBY_RC)
        radio.clearIrqStatus(0x03FF)
        radio.setBufferBaseAddress(0x00, 0x00)
        self._configure_radio()
        if not radio.request(radio.RX_CONTINUOUS):
            raise RuntimeError("radio refused receive mode after transmission")

    def _poll_receive_once(self) -> None:
        radio = self._radio
        irq = radio.getIrqStatus()
        error_mask = radio.IRQ_HEADER_ERR | radio.IRQ_CRC_ERR
        if irq & error_mask:
            radio.clearIrqStatus(0x03FF)
            radio.setStandby(radio.STANDBY_RC)
            self._configure_radio()
            if not radio.request(radio.RX_CONTINUOUS):
                raise RuntimeError(
                    "radio refused receive mode after packet error")
            return
        if not irq & radio.IRQ_RX_DONE:
            self._framing.expire()
            return
        length, index = radio.getRxBufferStatus()
        rssi = float(radio.packetRssi())
        snr = float(radio.snr())
        frame = bytes(radio.readBuffer(index, length)) if length else b""
        radio.clearIrqStatus(0x03FF)
        payload = self._framing.feed(frame)
        if payload is None:
            return
        self.r_stat_rssi = rssi
        self.r_stat_snr = snr
        self.r_stat_q = None
        self.rxb += len(payload)
        self.owner.inbound(payload, self)

    def _run(self) -> None:
        try:
            self._open_hardware()
            self.online = True
            self._emit("radio_status", {
                "state": "online", "frequency_hz": self.frequency_hz,
                "bandwidth_hz": self.bandwidth_hz,
                "spreading_factor": self.spreading_factor,
                "coding_rate": self.coding_rate,
                "bitrate": self.bitrate,
            })
            self._hardware_ready.set()
            while not self._stop_event.is_set():
                self._poll_receive_once()
                try:
                    payload = self._tx_queue.get_nowait()
                except queue.Empty:
                    self._sleep(self.POLL_SECONDS)
                    continue
                with self._tx_lock:
                    self._tx_bytes = max(0, self._tx_bytes - len(payload))
                self._transmit(payload)
        except Exception as exc:
            self._fatal_error = str(exc)
            self.online = False
            self._emit("error", {
                "code": "radio_worker_failed", "detail": str(exc)[:240],
                "fatal": True,
            })
            log.exception("Reticulum SX1262 worker failed")
        finally:
            self.online = False
            self._closed_clean = self._close_hardware()
            if not self._closed_clean:
                self._emit("error", {
                    "code": "radio_close_uncertain",
                    "detail": "Reticulum radio cleanup could not be verified",
                    "fatal": True,
                })
            self._hardware_ready.set()

    def _close_hardware(self) -> bool:
        radio = self._radio
        ok = True
        if radio is not None:
            try:
                radio.sleep(radio.SLEEP_COLD_START)
            except Exception:
                pass
            try:
                module = importlib.import_module(type(radio).__module__)
                spi = getattr(module, "spi", None)
                if spi is None:
                    from LoRaRF import SX126x as sx_module
                    spi = getattr(sx_module, "spi", None)
                if spi is None:
                    raise RuntimeError("LoRaRF SPI handle is unavailable")
                spi.close()
            except Exception as exc:
                # Test radios can expose an explicit close() without the
                # LoRaRF module-level SPI handle.
                close = getattr(radio, "close", None)
                try:
                    if callable(close):
                        close()
                    else:
                        raise exc
                except Exception:
                    ok = False
                    self._close_uncertain = True
                    log.warning("Could not confirm Reticulum SPI close: %s", exc)
        if ok and getattr(self._ownership, "held", True):
            try:
                self._ownership.release()
            except Exception as exc:
                ok = False
                self._close_uncertain = True
                log.warning("Could not release Reticulum radio lock: %s", exc)
        self._radio = None
        return ok

    def detach(self):
        if self.detached:
            return not self._close_uncertain
        self.detached = True
        self.online = False
        self._stop_event.set()
        thread = getattr(self, "_thread", None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        stopped = thread is None or not thread.is_alive()
        closed = self._closed_clean
        if not stopped or not closed:
            self._close_uncertain = True
            if not stopped:
                self._emit("error", {
                    "code": "radio_close_uncertain",
                    "detail": "Reticulum radio worker did not stop",
                    "fatal": True,
                })
        self._emit("radio_status", {
            "state": "stopped" if stopped and closed else "uncertain"})
        return stopped and closed

    def should_ingress_limit(self):
        return False

    def __str__(self) -> str:
        return f"AioSX1262Interface[{self.name}]"


interface_class = AioSX1262Interface
