import threading
import time

import pytest

from watchdogs.reticulum_interface import (
    AioSX1262Interface,
    RNodeCSMA,
    RNodeFraming,
)


@pytest.mark.parametrize("size,frame_sizes", [
    (1, (2,)),
    (254, (255,)),
    (255, (255, 2)),
    (256, (255, 3)),
    (508, (255, 255)),
])
def test_pinned_rnode_wire_vectors(size, frame_sizes):
    payload = bytes(index % 251 for index in range(size))
    frames = RNodeFraming.encode(payload, 0xA)
    assert tuple(map(len, frames)) == frame_sizes
    assert frames[0][0] == (0xA1 if size > 254 else 0xA0)
    if len(frames) == 2:
        assert frames[1][0] == 0xA1
    receiver = RNodeFraming(monotonic=lambda: 0.0)
    result = None
    for frame in frames:
        result = receiver.feed(frame, now=0.1)
    assert result == payload


def test_wrong_sequence_restarts_split_reassembly():
    framing = RNodeFraming()
    first_a, second_a = RNodeFraming.encode(b"a" * 300, 1)
    first_b, second_b = RNodeFraming.encode(b"b" * 300, 2)
    assert framing.feed(first_a, now=1.0) is None
    assert framing.feed(first_b, now=1.1) is None
    assert framing.feed(second_a, now=1.2) is None
    assert framing.feed(first_b, now=1.3) is None
    assert framing.feed(second_b, now=1.4) == b"b" * 300


def test_unsplit_packet_discards_partial_split():
    framing = RNodeFraming()
    first, second = RNodeFraming.encode(b"x" * 300, 4)
    assert framing.feed(first, now=1.0) is None
    assert framing.feed(RNodeFraming.encode(b"plain", 5)[0], now=1.1) \
        == b"plain"
    assert framing.feed(second, now=1.2) is None


def test_split_expires_and_duplicate_first_fragment_is_ignored():
    framing = RNodeFraming(fragment_timeout=2.0, duplicate_window=0.01)
    first, second = RNodeFraming.encode(b"x" * 300, 6)
    assert framing.feed(first, now=1.0) is None
    assert framing.feed(first, now=1.001) is None
    assert framing.feed(second, now=1.1) == b"x" * 300
    assert framing.feed(first, now=2.0) is None
    assert framing.feed(second, now=4.1) is None


@pytest.mark.parametrize("frame", [
    b"", b"\x00", b"\x02payload", b"\x0epayload", b"x" * 256,
])
def test_malformed_wire_frames_are_rejected(frame):
    assert RNodeFraming().feed(frame, now=1.0) is None


def test_encode_rejects_zero_and_oversized_payloads():
    for payload in (b"", b"x" * 509):
        with pytest.raises(ValueError):
            RNodeFraming.encode(payload, 0)


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class FixedRng:
    def __init__(self):
        self.calls = []

    def randrange(self, low, high):
        self.calls.append((low, high))
        return low


def make_csma(*, sf=8, bandwidth=62_500):
    clock = Clock()
    rng = FixedRng()
    return clock, rng, RNodeCSMA(
        sf=sf, bandwidth_hz=bandwidth, coding_rate=5,
        short_limit_percent=10, long_limit_percent=2,
        monotonic=clock, rng=rng)


def test_csma_uses_pinned_slot_preamble_and_contention_ranges():
    _clock, rng, csma = make_csma()
    assert csma.slot_seconds == pytest.approx(0.049)
    assert csma.difs_seconds == pytest.approx(0.098)
    assert csma.preamble_symbols == 18
    assert csma.contention_seconds() == 0
    assert rng.calls[-1] == (0, 14)


def test_fast_rate_uses_six_ms_firmware_adjustment():
    _clock, _rng, csma = make_csma(sf=5, bandwidth=500_000)
    assert csma.bitrate > 30_000
    assert csma.slot_seconds == pytest.approx(0.006)
    assert csma.preamble_symbols == 94


def test_low_data_rate_uses_pinned_integer_threshold():
    _clock, _rng, below = make_csma(sf=12, bandwidth=250_000)
    _clock, _rng, above = make_csma(sf=12, bandwidth=125_000)
    assert below.low_data_rate is False
    assert above.low_data_rate is True


def test_all_four_airtime_contention_bands_and_locks():
    clock, rng, csma = make_csma()
    for percent, band in ((0, 1), (8, 2), (48, 3), (86, 4)):
        csma._airtime.clear()
        csma._airtime.append((clock.value, 15 * percent / 100))
        assert csma.contention_band() == band
        csma.contention_seconds()
        assert rng.calls[-1] == ((band - 1) * 15, band * 15 - 1)
    csma._airtime.clear()
    csma._airtime.append((clock.value, 1.5))
    assert csma.airtime_locked()


def test_airtime_formula_matches_pinned_sx1262_equation():
    _clock, _rng, csma = make_csma(sf=8, bandwidth=62_500)
    symbols = (8 * 255 + 16 - 4 * 8 + 8 + 20) / (4 * 8) * 5
    expected = (csma.preamble_symbols + 0.25 + 8 + symbols) \
        * csma.symbol_seconds
    assert csma.packet_airtime(255) == pytest.approx(expected)


def test_record_transmission_applies_three_slot_yield():
    clock, _rng, csma = make_csma()
    clock.value = 12.0
    csma.record_transmission(0.2)
    assert csma.post_tx_until == pytest.approx(
        12.0 + 3 * csma.slot_seconds)


class FakeOwnership:
    def __init__(self, order):
        self.order = order
        self.held = False

    def acquire(self, name):
        self.order.append(("lock", name, threading.get_ident()))
        self.held = True

    def release(self):
        self.order.append(("unlock", threading.get_ident()))
        self.held = False


class FakeRadio:
    DIO3_OUTPUT_1_8 = 18
    RX_GAIN_BOOSTED = 1
    TX_POWER_SX1262 = 2
    HEADER_EXPLICIT = 0
    RX_CONTINUOUS = 0xFFFFFF
    STANDBY_RC = 0
    SLEEP_COLD_START = 1
    IRQ_PREAMBLE_DETECTED = 0x0004
    IRQ_SYNC_WORD_VALID = 0x0008
    IRQ_HEADER_VALID = 0x0010
    IRQ_HEADER_ERR = 0x0020
    IRQ_CRC_ERR = 0x0040
    IRQ_RX_DONE = 0x0002
    IRQ_TX_DONE = 0x0001

    def __init__(self, order):
        self.order = order
        self.calls = []
        self.frames = []
        self.current = bytearray()
        self.tx_done = False

    def _call(self, name, *args):
        self.calls.append((name, args, threading.get_ident()))

    def begin(self, **kwargs):
        self._call("begin", kwargs)
        return True

    def setDio2RfSwitch(self, *args): self._call("dio2", *args)
    def setDio3TcxoCtrl(self, *args): self._call("dio3", *args)
    def setRxGain(self, *args): self._call("gain", *args)
    def setTxPower(self, *args): self._call("power", *args)
    def setFrequency(self, *args): self._call("frequency", *args)
    def setLoRaModulation(self, *args): self._call("modulation", *args)
    def setSyncWord(self, *args): self._call("sync", *args)
    def setLoRaPacket(self, *args): self._call("packet", *args)
    def setStandby(self, *args): self._call("standby", *args)
    def clearIrqStatus(self, *args):
        self._call("clear_irq", *args)
        self.tx_done = False
    def setBufferBaseAddress(self, *args): self._call("buffer", *args)

    def request(self, *args):
        self._call("request", *args)
        self.tx_done = False
        return True

    def getIrqStatus(self):
        self._call("irq")
        return self.IRQ_TX_DONE if self.tx_done else 0

    def rssiInst(self): return -120
    def beginPacket(self):
        self._call("begin_packet")
        self.current = bytearray()
    def write(self, data, length):
        self._call("write", bytes(data), length)
        self.current.extend(bytes(data)[:length])
    def endPacket(self, timeout):
        self._call("end_packet", timeout)
        self.frames.append(bytes(self.current))
        self.tx_done = True
        return True
    def wait(self, timeout):
        self._call("wait", timeout)
        return True
    def getRxBufferStatus(self): return 0, 0
    def packetRssi(self): return -100
    def snr(self): return 3
    def readBuffer(self, _index, _length): return []
    def sleep(self, *args):
        self._call("sleep", *args)
        self.order.append(("sleep", threading.get_ident()))
    def close(self):
        self._call("close")
        self.order.append(("close", threading.get_ident()))


class FakeOwner:
    def inbound(self, _payload, _interface):
        raise AssertionError("unexpected inbound packet")


def test_fake_radio_configuration_serialization_and_teardown_order(monkeypatch):
    import RNS

    class ReticulumDefaults:
        def __getattr__(self, name):
            if name.startswith("_default_"):
                return lambda: 0
            raise AttributeError(name)

    defaults = ReticulumDefaults()
    monkeypatch.setattr(
        RNS.Reticulum, "get_instance", staticmethod(lambda: defaults))
    order = []
    radio = FakeRadio(order)
    ownership = FakeOwnership(order)
    interface = AioSX1262Interface(FakeOwner(), {
        "name": "test", "frequency": 910_525_000,
        "bandwidth": 62_500, "spreading_factor": 8,
        "coding_rate": 5, "tx_power": 14,
        "airtime_short_percent": 100,
        "airtime_long_percent": 100,
        "_radio_factory": lambda: radio,
        "_radio_ownership": ownership,
        "_sleep": lambda seconds: time.sleep(min(seconds, 0.001)),
    })
    interface.process_outgoing(b"x" * 300)
    deadline = time.monotonic() + 2
    while len(radio.frames) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert [len(frame) for frame in radio.frames] == [255, 47]
    assert radio.frames[0][0] == radio.frames[1][0]
    assert radio.frames[0][0] & 0x01
    assert interface.detach()

    packet_calls = [args for name, args, _thread in radio.calls
                    if name == "packet"]
    assert packet_calls
    assert packet_calls[0] == (
        radio.HEADER_EXPLICIT, interface._csma.preamble_symbols,
        255, True, False)
    modulation_calls = [args for name, args, _thread in radio.calls
                        if name == "modulation"]
    assert modulation_calls[0] == (8, 62_500, 5, False)
    assert ("sync", (0x1424,), radio.calls[0][2]) in radio.calls
    # begin/configure/RX/TX/close all stay on the same dedicated worker.
    assert len({thread_id for _name, _args, thread_id in radio.calls}) == 1
    labels = [item[0] for item in order]
    assert labels.index("sleep") < labels.index("close") < labels.index("unlock")
