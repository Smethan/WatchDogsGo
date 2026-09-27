"""MeshCore companion protocol and BlueZ BLE peripheral for MeshMapper.

The AIO SX1262 remains owned by :class:`watchdogs.lora_manager.LoRaManager`.
This module never touches SPI.  It exposes the standard MeshCore companion
Nordic UART GATT service and translates companion frames into bounded calls on
that one radio worker.
"""

from __future__ import annotations

import hashlib
import logging
import re
import struct
import threading
import time
from collections import deque
from collections.abc import Callable
from queue import Empty, Full, Queue

from . import __version__
from .host_ble import list_ble_adapters, resolve_ble_adapter
from .lora_manager import (
    PUBLIC_CHANNEL,
    MeshCoreChannel,
    get_meshcore_preset,
    make_hashtag_channel,
    make_private_channel,
)

log = logging.getLogger(__name__)

MESHCORE_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
MESHCORE_RX_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
MESHCORE_TX_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

MAX_COMPANION_FRAME = 255
MAX_CHANNELS = 8
# Use the v7+ device-info layout and v10+ path-mode field, but stay below the
# v13 capability gate: WDG does not implement MeshMapper's repeater scope-
# discovery/admin surface and must not advertise that it does.
PROTOCOL_VERSION = 12
MAX_SIGN_DATA_LEN = 8192
SIGN_SESSION_TIMEOUT = 30.0
MAX_PAIRING_WINDOW = 120
BLUEZ_REGISTRATION_TIMEOUT = 10.0

# BlueZ security flags imply the corresponding operation property.  In
# particular, retaining a plain ``notify`` alongside authenticated-notify
# leaves the CCC permission unprotected on some BlueZ releases.
MESHCORE_RX_FLAGS = (
    "write-without-response",
    "encrypt-authenticated-write",
)
MESHCORE_TX_FLAGS = (
    "encrypt-authenticated-read",
    "encrypt-authenticated-notify",
)

_BLUEZ_ADAPTER_IFACE = "org.bluez.Adapter1"
_BLUEZ_DEVICE_IFACE = "org.bluez.Device1"
_ADDRESS_RE = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")


class _BluezRegistration:
    """Sequence BlueZ registration while the GLib dispatcher is running.

    BlueZ calls the application's ObjectManager and advertisement Properties
    interfaces before completing either registration.  The calls must
    therefore be asynchronous: a synchronous dbus-python call made before the
    GLib loop starts prevents WDG from servicing BlueZ's callbacks and BlueZ
    reports ``client_ready_cb() No object received``.
    """

    def __init__(self, gatt, advertising, application_path: str,
                 advertisement_path: str, *, object_path,
                 stop_requested: Callable[[], bool],
                 on_ready: Callable[[], None],
                 on_terminal: Callable[[], None],
                 clock: Callable[[], float] = time.monotonic,
                 timeout: float = BLUEZ_REGISTRATION_TIMEOUT) -> None:
        self.gatt = gatt
        self.advertising = advertising
        self.application_path = application_path
        self.advertisement_path = advertisement_path
        self.object_path = object_path
        self.stop_requested = stop_requested
        self.on_ready = on_ready
        self.on_terminal = on_terminal
        self.clock = clock
        self.timeout = max(1.0, float(timeout))
        self.started = False
        self.ready = False
        self.gatt_registered = False
        self.advertisement_registered = False
        self.deadline = 0.0
        self.failure = ""

    @staticmethod
    def _detail(error) -> str:
        return str(error).strip() or type(error).__name__

    def _fail(self, stage: str, error) -> None:
        if self.failure:
            return
        self.failure = f"{stage}: {self._detail(error)}"
        self.on_terminal()

    def begin(self) -> bool:
        if self.started:
            return False
        self.started = True
        self.deadline = self.clock() + self.timeout
        try:
            self.gatt.RegisterApplication(
                self.object_path(self.application_path), {},
                reply_handler=self._application_registered,
                error_handler=lambda error: self._fail(
                    "BlueZ GATT application registration failed", error))
        except Exception as exc:
            self._fail("BlueZ GATT application registration failed", exc)
        return not self.failure

    def _application_registered(self, *_args) -> None:
        self.gatt_registered = True
        if self.stop_requested():
            self.on_terminal()
            return
        try:
            self.advertising.RegisterAdvertisement(
                self.object_path(self.advertisement_path), {},
                reply_handler=self._advertisement_registered,
                error_handler=lambda error: self._fail(
                    "BlueZ advertisement registration failed", error))
        except Exception as exc:
            self._fail("BlueZ advertisement registration failed", exc)

    def _advertisement_registered(self, *_args) -> None:
        self.advertisement_registered = True
        if self.stop_requested():
            self.on_terminal()
            return
        self.ready = True
        self.on_ready()

    def expire(self) -> bool:
        if self.ready or self.failure or not self.started:
            return False
        if self.clock() < self.deadline:
            return False
        self._fail(
            "BlueZ peripheral registration failed",
            TimeoutError(
                f"no reply within {self.timeout:g} seconds"))
        return True

    def cleanup(self) -> list[str]:
        """Unregister only objects BlueZ confirmed it accepted."""
        errors = []
        if self.advertisement_registered:
            try:
                self.advertising.UnregisterAdvertisement(
                    self.object_path(self.advertisement_path))
            except Exception as exc:
                errors.append(
                    "advertisement unregister failed: " + self._detail(exc))
            self.advertisement_registered = False
        if self.gatt_registered:
            try:
                self.gatt.UnregisterApplication(
                    self.object_path(self.application_path))
            except Exception as exc:
                errors.append(
                    "GATT unregister failed: " + self._detail(exc))
            self.gatt_registered = False
        return errors


def _normalize_ble_address(value: str) -> str:
    address = str(value or "").strip().upper().replace("-", ":")
    return address if _ADDRESS_RE.fullmatch(address) else ""


def _utf8_prefix(value: str, size: int) -> bytes:
    """Return at most ``size`` bytes without splitting a UTF-8 codepoint."""
    raw = str(value).encode("utf-8")[:max(0, size)]
    return raw.decode("utf-8", errors="ignore").encode("utf-8")


def _c_string(value: str, size: int) -> bytes:
    raw = _utf8_prefix(value, size - 1)
    return raw + bytes(size - len(raw))


def _signed_byte(value: float | int) -> int:
    return max(-128, min(127, int(round(value)))) & 0xFF


class MeshCoreCompanionProtocol:
    """Pure companion-radio frame handler used by BLE and unit tests."""

    OK = b"\x00"
    ERR_UNSUPPORTED = b"\x01\x01"
    ERR_NOT_FOUND = b"\x01\x02"
    ERR_TABLE_FULL = b"\x01\x03"
    ERR_BAD_STATE = b"\x01\x04"
    ERR_ILLEGAL_ARG = b"\x01\x06"

    def __init__(
        self,
        lora,
        *,
        get_node_name: Callable[[], str],
        set_node_name: Callable[[str], None],
        get_channels: Callable[[], list[MeshCoreChannel]],
        set_channels: Callable[[list[MeshCoreChannel]], None],
        get_region: Callable[[], str],
        get_location: Callable[[], tuple[float, float]] | None = None,
        max_channels: int = MAX_CHANNELS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.lora = lora
        self.get_node_name = get_node_name
        self.set_node_name = set_node_name
        self.get_channels = get_channels
        self.set_channels = set_channels
        self.get_region = get_region
        self.get_location = get_location or (lambda: (0.0, 0.0))
        self.max_channels = max(1, min(32, int(max_channels)))
        self.clock = clock
        self.path_hash_mode = 0
        self._channel_slots: list[MeshCoreChannel | None] | None = None
        self._sign_data: bytearray | None = None
        self._sign_started_at = 0.0
        self._lock = threading.RLock()

    def _identity(self):
        return self.lora._get_ed25519_keypair()

    def _channels(self) -> list[MeshCoreChannel]:
        channels = list(self.get_channels() or [])
        if not channels or channels[0].name.lower() != "public":
            channels.insert(0, PUBLIC_CHANNEL)
        return channels[: self.max_channels]

    def _slots(self) -> list[MeshCoreChannel | None]:
        if self._channel_slots is None:
            channels = self._channels()
            self._channel_slots = channels + [None] * (
                self.max_channels - len(channels))
        return self._channel_slots

    def _device_info(self) -> bytes:
        build = time.strftime("%d-%b-%Y", time.gmtime())
        return b"".join((
            bytes((13, PROTOCOL_VERSION, 32, self.max_channels)),
            struct.pack("<I", 0),
            _c_string(build, 12),
            _c_string("WatchDogsGo AIO SX1262", 40),
            _c_string(f"WDG {__version__}", 20),
            bytes((0, self.path_hash_mode)),
        ))

    def _self_info(self) -> bytes:
        _private, public = self._identity()
        freq, sf, cr, bw, _label = get_meshcore_preset(self.get_region())
        lat, lon = self.get_location()
        return b"".join((
            bytes((5, 1, 22, 22)),
            public,
            struct.pack("<ii", int(lat * 1_000_000), int(lon * 1_000_000)),
            bytes((0, 0, 0, 0)),
            struct.pack("<II", int(freq // 1000), int(bw)),
            bytes((int(sf), int(cr))),
            _utf8_prefix(self.get_node_name(), 31),
        ))

    def _channel_info(self, index: int) -> bytes:
        if index < 0 or index >= self.max_channels:
            return self.ERR_NOT_FOUND
        channel = self._slots()[index]
        if channel is not None:
            # Companion apps recognise the built-in channel by this canonical
            # display label. WDG retains its lower-case internal/config name.
            name = "Public" if index == 0 else channel.name
            return bytes((18, index)) + _c_string(name, 32) + channel.psk
        return bytes((18, index)) + bytes(32 + 16)

    def _set_channel(self, frame: bytes) -> bytes:
        if len(frame) != 50:
            return self.ERR_ILLEGAL_ARG
        index = frame[1]
        if index >= self.max_channels or index == 0:
            return self.ERR_NOT_FOUND
        name = frame[2:34].split(b"\0", 1)[0].decode(
            "utf-8", errors="strict").strip()
        secret = bytes(frame[34:50])
        slots = self._slots()
        if name:
            if not any(secret):
                return self.ERR_ILLEGAL_ARG
            if name.startswith("#"):
                expected = make_hashtag_channel(name)
                channel = MeshCoreChannel(
                    name=name, psk=secret,
                    ch_hash=expected.ch_hash if expected.psk == secret
                    else hashlib.sha256(secret).digest()[0],
                    is_hashtag=expected.psk == secret,
                )
            else:
                channel = make_private_channel(name, secret.hex())
            slots[index] = channel
        else:
            slots[index] = None
        compact = [PUBLIC_CHANNEL] + [
            channel for channel in slots[1:] if channel is not None
        ]
        self.set_channels(compact)
        return self.OK

    def _send_channel_text(self, frame: bytes) -> bytes:
        if len(frame) < 8 or frame[1] != 0:
            return self.ERR_ILLEGAL_ARG
        index = frame[2]
        if index >= self.max_channels:
            return self.ERR_NOT_FOUND
        channel = self._slots()[index]
        if channel is None:
            return self.ERR_NOT_FOUND
        try:
            timestamp = struct.unpack_from("<I", frame, 3)[0]
            text = frame[7:].decode("utf-8")
        except (UnicodeDecodeError, struct.error):
            return self.ERR_ILLEGAL_ARG
        if not text or len(frame[7:]) > 180:
            return self.ERR_ILLEGAL_ARG
        result = self.lora.send_meshcore_channel_message(
            text, self.get_node_name(), index, timestamp, channel=channel)
        return self.OK if result is not None else self.ERR_TABLE_FULL

    def _send_control(self, frame: bytes) -> bytes:
        if len(frame) < 2:
            return self.ERR_ILLEGAL_ARG
        return (self.OK if self.lora.send_meshcore_control(bytes(frame[1:]))
                is not None else self.ERR_TABLE_FULL)

    def _export_contact(self) -> bytes:
        lat, lon = self.get_location()
        packet = self.lora._build_mc_advert(self.get_node_name(), lat, lon)
        return b"\x0b" + packet

    def _stats(self, kind: int) -> bytes:
        if kind == 1:
            noise = int(getattr(self.lora, "noise_floor", -110))
            rssi = int(getattr(self.lora, "last_rssi", -128))
            snr = float(getattr(self.lora, "last_snr", 0.0))
            return (bytes((24, 1)) + struct.pack("<h", noise)
                    + bytes((_signed_byte(rssi), _signed_byte(snr * 4)))
                    + struct.pack("<II", 0, 0))
        if kind == 0:
            return bytes((24, 0)) + struct.pack("<HIHB", 0, 0, 0, 0)
        if kind == 2:
            received = int(getattr(self.lora, "packets_received", 0))
            return bytes((24, 2)) + struct.pack(
                "<IIIIIII", received, 0, 0, 0, received, 0, 0)
        return self.ERR_ILLEGAL_ARG

    def cancel_signing(self) -> None:
        """Discard an in-progress signing transaction without exposing data."""
        with self._lock:
            if self._sign_data is not None:
                self._sign_data[:] = bytes(len(self._sign_data))
            self._sign_data = None
            self._sign_started_at = 0.0

    def _sign_expired(self) -> bool:
        return (self._sign_data is not None
                and self.clock() - self._sign_started_at
                > SIGN_SESSION_TIMEOUT)

    def _handle_signing(self, command: int, frame: bytes,
                        authenticated: bool) -> bytes:
        if not authenticated:
            self.cancel_signing()
            return self.ERR_UNSUPPORTED
        if self._sign_expired():
            self.cancel_signing()
            return self.ERR_BAD_STATE
        if command == 33:
            if len(frame) != 1:
                return self.ERR_ILLEGAL_ARG
            self.cancel_signing()
            self._sign_data = bytearray()
            self._sign_started_at = self.clock()
            # RESP_CODE_SIGN_START, reserved byte, little-endian capacity.
            return bytes((19, 0)) + struct.pack("<I", MAX_SIGN_DATA_LEN)
        if command == 34:
            if len(frame) <= 1:
                return self.ERR_ILLEGAL_ARG
            if self._sign_data is None:
                return self.ERR_BAD_STATE
            chunk = frame[1:]
            if len(self._sign_data) + len(chunk) > MAX_SIGN_DATA_LEN:
                self.cancel_signing()
                return self.ERR_TABLE_FULL
            self._sign_data.extend(chunk)
            return self.OK
        if command == 35:
            if len(frame) != 1:
                return self.ERR_ILLEGAL_ARG
            if self._sign_data is None:
                return self.ERR_BAD_STATE
            message = bytes(self._sign_data)
            self.cancel_signing()
            private, _public = self._identity()
            signature = bytes(private.sign(message))
            if len(signature) != 64:
                raise ValueError("Ed25519 signer returned an invalid signature")
            return b"\x14" + signature
        return self.ERR_UNSUPPORTED

    def handle_frame(self, value: bytes | bytearray,
                     authenticated: bool = False) -> list[bytes]:
        frame = bytes(value)
        if not frame or len(frame) > MAX_COMPANION_FRAME:
            return [self.ERR_ILLEGAL_ARG]
        command = frame[0]
        with self._lock:
            try:
                if command in (33, 34, 35):
                    return [self._handle_signing(
                        command, frame, bool(authenticated))]
                if command == 22 and len(frame) >= 2:
                    return [self._device_info()]
                if command == 1 and len(frame) >= 8:
                    return [self._self_info()]
                if command == 5:
                    return [b"\x09" + struct.pack("<I", int(self.clock()))]
                if command == 6 and len(frame) == 5:
                    return [self.OK]
                if command == 8 and len(frame) >= 2:
                    name = frame[1:].decode("utf-8", errors="strict").strip()
                    if not name or len(name.encode("utf-8")) > 31:
                        return [self.ERR_ILLEGAL_ARG]
                    self.set_node_name(name)
                    return [self.OK]
                if command == 7:
                    lat, lon = self.get_location()
                    sent = self.lora.send_meshcore_advert(
                        self.get_node_name(), lat, lon)
                    return [self.OK if sent is not None
                            else self.ERR_TABLE_FULL]
                if command == 17:
                    return [self._export_contact()]
                if command == 20:
                    return [b"\x0c" + struct.pack("<HII", 0, 0, 0)]
                if command == 31 and len(frame) == 2:
                    return [self._channel_info(frame[1])]
                if command == 32:
                    return [self._set_channel(frame)]
                if command == 3:
                    return [self._send_channel_text(frame)]
                if command == 55:
                    return [self._send_control(frame)]
                if command == 56 and len(frame) == 2:
                    return [self._stats(frame[1])]
                if command == 54 and len(frame) in (2, 18):
                    # WDG currently transmits global MeshCore flood packets.
                    # Accept only the explicit clear/global form.
                    return [self.OK if len(frame) == 2 else self.ERR_UNSUPPORTED]
                if command == 61 and len(frame) == 3 and frame[1] == 0:
                    if frame[2] > 2:
                        return [self.ERR_ILLEGAL_ARG]
                    self.path_hash_mode = frame[2]
                    return [self.OK]
                if command == 10:
                    return [b"\x0a"]
            except (ValueError, UnicodeError, struct.error) as exc:
                log.debug("Invalid MeshCore companion frame %s: %s", command, exc)
                return [self.ERR_ILLEGAL_ARG]
            except Exception as exc:
                log.warning("MeshCore companion command %s failed: %s", command, exc)
                return [self.ERR_BAD_STATE]
        return [self.ERR_UNSUPPORTED]

    @staticmethod
    def raw_packet_event(packet: bytes, rssi: float, snr: float) -> bytes:
        return bytes((0x88, _signed_byte(snr * 4), _signed_byte(rssi))) + bytes(packet)

    @staticmethod
    def control_event(payload: bytes, path_byte: int,
                      rssi: float, snr: float) -> bytes:
        return (bytes((0x8E, _signed_byte(snr * 4), _signed_byte(rssi),
                       path_byte & 0xFF)) + bytes(payload))


class _PairingSecurity:
    """Pure selected-adapter/device policy shared by D-Bus and tests."""

    def __init__(self, adapter_path: str, retained_address: str = "",
                 retained_name: str = "") -> None:
        self.adapter_path = str(adapter_path)
        self.retained_address = _normalize_ble_address(retained_address)
        self.retained_name = str(retained_name or "").strip()
        self.claimed_path = ""
        self.claimed_address = ""
        self.claimed_name = ""
        self.claimed_authenticated = False
        self.window_open = False

    def open_window(self) -> None:
        self.claimed_path = ""
        self.claimed_address = ""
        self.claimed_name = ""
        self.claimed_authenticated = False
        self.window_open = True

    def close_window(self) -> None:
        self.window_open = False
        self.claimed_path = ""
        self.claimed_address = ""
        self.claimed_name = ""
        self.claimed_authenticated = False

    def _identity(self, device_path: str, props: dict) -> tuple[str, str]:
        path = str(device_path or "")
        expected_prefix = self.adapter_path.rstrip("/") + "/dev_"
        if not path.startswith(expected_prefix):
            raise PermissionError("pairing request is on another adapter")
        if str(props.get("Adapter", "")) != self.adapter_path:
            raise PermissionError("device does not belong to selected adapter")
        address = _normalize_ble_address(props.get("Address", ""))
        if not address:
            raise PermissionError("device has no valid Bluetooth address")
        name = str(props.get("Alias") or props.get("Name") or address).strip()
        return address, name[:64]

    def claim_pairing_device(self, device_path: str,
                             props: dict) -> tuple[str, str]:
        if not self.window_open:
            raise PermissionError("MeshMapper pairing window is closed")
        address, name = self._identity(device_path, props)
        if self.retained_address and address != self.retained_address:
            raise PermissionError("another MeshMapper phone is already bonded")
        if self.claimed_path and str(device_path) != self.claimed_path:
            raise PermissionError("pairing window is already claimed")
        self.claimed_path = str(device_path)
        self.claimed_address = address
        self.claimed_name = name
        return address, name

    def mark_passkey_displayed(self, device_path: str,
                               props: dict) -> tuple[str, str]:
        """Record the MITM-capable passkey association chosen by BlueZ."""
        identity = self.claim_pairing_device(device_path, props)
        self.claimed_authenticated = True
        return identity

    def authorize_gatt(self, device_path: str,
                       props: dict) -> tuple[str, str]:
        address, name = self._identity(device_path, props)
        if not (bool(props.get("Paired"))
                and bool(props.get("Bonded"))
                and bool(props.get("Trusted"))):
            raise PermissionError(
                "MeshMapper link is not paired, bonded, and trusted")
        allowed = self.retained_address or self.claimed_address
        if not allowed or address != allowed:
            raise PermissionError("device is not the retained MeshMapper phone")
        return address, name

    def retain_claim(self) -> tuple[str, str]:
        if not self.claimed_address or not self.claimed_authenticated:
            raise PermissionError(
                "MeshMapper pairing did not use authenticated passkey entry")
        self.retained_address = self.claimed_address
        self.retained_name = self.claimed_name or self.claimed_address
        return self.retained_address, self.retained_name

    def authorize_notify(self, devices: list[tuple[str, dict]]) -> tuple[str, str]:
        """Bind BlueZ's device-less StartNotify call to one retained peer."""
        connected = []
        for device_path, props in devices:
            if str(props.get("Adapter", "")) != self.adapter_path:
                continue
            if not bool(props.get("Connected")):
                continue
            address, name = self._identity(device_path, props)
            connected.append((address, name, props))
        if len(connected) != 1:
            raise PermissionError(
                "retained MeshMapper phone must be the only connected device "
                "on the companion adapter")
        address, name, props = connected[0]
        if (not self.retained_address or address != self.retained_address
                or not bool(props.get("Paired"))
                or not bool(props.get("Bonded"))
                or not bool(props.get("Trusted"))):
            raise PermissionError(
                "notification subscriber is not the retained authenticated "
                "MeshMapper phone")
        return address, name

    def forget(self) -> tuple[str, str]:
        address, name = self.retained_address, self.retained_name
        self.retained_address = ""
        self.retained_name = ""
        self.close_window()
        return address, name


class _BluezPeripheral:
    """Blocking BlueZ GATT peripheral. One instance lives on its own thread."""

    def __init__(self, adapter: str, local_name: str, protocol,
                 stop_event: threading.Event, event_callback, *,
                 paired_address: str = "", paired_name: str = "") -> None:
        self.adapter = adapter
        self.local_name = local_name
        self.protocol = protocol
        self.stop_event = stop_event
        self.event_callback = event_callback
        self.paired_address = _normalize_ble_address(paired_address)
        self.paired_name = str(paired_name or "").strip()
        self._glib = None
        self._loop = None
        self._tx = None
        self._commands: Queue = Queue(maxsize=16)
        self._security: _PairingSecurity | None = None
        self._pairing_active = False
        self._pairing_requested = False
        self._pending = deque(maxlen=128)
        self._pending_lock = threading.Lock()
        self.drop_count = 0

    def _queue_command(self, name: str, payload=None,
                       completion: threading.Event | None = None,
                       result: list | None = None) -> bool:
        if self.stop_event.is_set() or self._glib is None:
            return False
        try:
            self._commands.put_nowait((name, payload, completion, result))
        except Full:
            return False
        self._glib.idle_add(self._drain_commands)
        return True

    def open_pairing(self, seconds: int) -> bool:
        accepted = self._queue_command("open_pairing", int(seconds))
        if accepted:
            self._pairing_requested = True
        return accepted

    def close_pairing(self, timeout: float = 5.0) -> bool:
        if not self._pairing_active and not self._pairing_requested:
            return True
        done = threading.Event()
        result: list[bool] = []
        if not self._queue_command("close_pairing", None, done, result):
            return False
        if not done.wait(max(0.0, float(timeout))):
            return False
        return bool(result and result[0])

    def forget_phone(self, timeout: float = 5.0) -> bool:
        if not self.paired_address:
            return False
        done = threading.Event()
        result: list[bool] = []
        if not self._queue_command(
                "forget_phone", self.paired_address, done, result):
            return False
        if not done.wait(max(0.0, float(timeout))):
            return False
        return bool(result and result[0])

    def _drain_commands(self):
        # Replaced by the GLib-thread closure in run(). Keeping a bounded no-op
        # here makes calls before BlueZ registration fail closed.
        return False

    def notify(self, frame: bytes, *, important: bool = False) -> bool:
        if not frame or len(frame) > MAX_COMPANION_FRAME:
            return False
        with self._pending_lock:
            if len(self._pending) >= self._pending.maxlen:
                if not important:
                    self.drop_count += 1
                    return False
                # Preserve command replies by dropping the oldest passive RX
                # when possible. If every slot is important, the oldest reply
                # is the only bounded fallback and the visible counter records
                # that loss.
                passive = next(
                    (index for index, (_frame, priority)
                     in enumerate(self._pending) if not priority), 0)
                del self._pending[passive]
                self.drop_count += 1
            self._pending.append((bytes(frame), bool(important)))
        glib = self._glib
        if glib is not None:
            glib.idle_add(self._drain_one)
        return True

    def _drain_one(self):
        with self._pending_lock:
            pending = self._pending.popleft() if self._pending else None
        frame = pending[0] if pending is not None else None
        tx = self._tx
        if frame is not None and tx is not None:
            tx.emit_frame(frame)
        return False

    def run(self) -> None:  # pragma: no cover - exercised on target BlueZ
        import dbus
        import dbus.mainloop.glib
        import dbus.service
        from gi.repository import GLib

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._glib = GLib
        # A private connection gives this peripheral its own BlueZ
        # "application" identity.  It therefore does not collide with the
        # WatchManager agent, even though BlueZ permits only one Agent1 per
        # D-Bus application/unique name.
        try:
            bus = dbus.SystemBus(private=True)
        except TypeError:  # older dbus-python
            bus = dbus.bus.BusConnection(dbus.bus.BUS_SYSTEM)
        root = dbus.Interface(
            bus.get_object("org.bluez", "/"),
            "org.freedesktop.DBus.ObjectManager")
        objects = root.GetManagedObjects()
        adapter_path = None
        adapter_interfaces = None
        for path, interfaces in objects.items():
            props = interfaces.get("org.bluez.Adapter1")
            if props is None:
                continue
            if (str(path).endswith("/" + self.adapter)
                    or str(props.get("Address", "")).upper()
                    == self.adapter.upper()):
                adapter_path = str(path)
                adapter_interfaces = interfaces
                break
        if adapter_path is None:
            raise RuntimeError(f"BlueZ adapter {self.adapter} is unavailable")
        if "org.bluez.GattManager1" not in adapter_interfaces:
            raise RuntimeError(f"BlueZ adapter {self.adapter} has no GATT server")
        if "org.bluez.LEAdvertisingManager1" not in adapter_interfaces:
            raise RuntimeError(f"BlueZ adapter {self.adapter} cannot advertise")

        self._security = _PairingSecurity(
            adapter_path, self.paired_address, self.paired_name)
        adapter_object = bus.get_object("org.bluez", adapter_path)
        adapter_properties = dbus.Interface(
            adapter_object, "org.freedesktop.DBus.Properties")
        adapter_api = dbus.Interface(adapter_object, _BLUEZ_ADAPTER_IFACE)
        agent_manager = dbus.Interface(
            bus.get_object("org.bluez", "/org/bluez"),
            "org.bluez.AgentManager1")

        def device_properties(device_path):
            wanted = str(device_path)
            for path, interfaces in root.GetManagedObjects().items():
                if str(path) == wanted:
                    return dict(interfaces.get(_BLUEZ_DEVICE_IFACE, {}))
            return {}

        def reject(message, name="org.bluez.Error.Rejected"):
            raise dbus.exceptions.DBusException(str(message), name=name)

        def pairing_device(device_path):
            props = device_properties(device_path)
            try:
                return self._security.claim_pairing_device(
                    str(device_path), props)
            except PermissionError as exc:
                reject(exc)

        def passkey_pairing_device(device_path):
            props = device_properties(device_path)
            try:
                return self._security.mark_passkey_displayed(
                    str(device_path), props)
            except PermissionError as exc:
                reject(exc)

        def authenticated_device(options):
            device_path = str(dict(options or {}).get("device", ""))
            props = device_properties(device_path)
            try:
                identity = self._security.authorize_gatt(device_path, props)
            except PermissionError as exc:
                reject(exc, "org.bluez.Error.NotAuthorized")
            self.paired_address, self.paired_name = identity
            return identity

        def authorize_service_device(device_path):
            props = device_properties(device_path)
            try:
                if self._security.window_open:
                    return self._security.claim_pairing_device(
                        str(device_path), props)
                return self._security.authorize_gatt(
                    str(device_path), props)
            except PermissionError as exc:
                reject(exc, "org.bluez.Error.NotAuthorized")

        def notify_device():
            devices = []
            for path, interfaces in root.GetManagedObjects().items():
                props = interfaces.get(_BLUEZ_DEVICE_IFACE)
                if props is not None:
                    devices.append((str(path), dict(props)))
            try:
                return self._security.authorize_notify(devices)
            except PermissionError as exc:
                reject(exc, "org.bluez.Error.NotAuthorized")

        peripheral = self

        class MeshMapperAgent(dbus.service.Object):
            PATH = "/net/watchdogs/meshcore/agent"
            IFACE = "org.bluez.Agent1"

            def __init__(agent_self):
                super().__init__(bus, agent_self.PATH)

            @dbus.service.method(IFACE, in_signature="", out_signature="")
            def Release(agent_self):
                GLib.idle_add(handle_agent_release)

            @dbus.service.method(IFACE, in_signature="", out_signature="")
            def Cancel(agent_self):
                GLib.idle_add(lambda: close_pairing_window("cancelled"))

            @dbus.service.method(IFACE, in_signature="ouq", out_signature="")
            def DisplayPasskey(agent_self, device, passkey, entered):
                passkey_pairing_device(device)
                peripheral.event_callback(
                    "pairing_pin", f"{int(passkey):06d}")

            @dbus.service.method(IFACE, in_signature="ou", out_signature="")
            def RequestConfirmation(agent_self, device, passkey):
                pairing_device(device)
                # DisplayOnly is passkey-entry on the phone, not numeric
                # comparison.  Auto-confirming here would silently downgrade
                # an unexpected association model.
                reject("numeric-comparison pairing is not supported")

            @dbus.service.method(IFACE, in_signature="o", out_signature="")
            def RequestAuthorization(agent_self, device):
                # RequestAuthorization is the unauthenticated Just Works
                # association path.  It cannot satisfy an authenticated GATT
                # characteristic and must never become the retained phone.
                reject("Just Works pairing is not permitted")

            @dbus.service.method(IFACE, in_signature="os", out_signature="")
            def AuthorizeService(agent_self, device, uuid):
                if str(uuid).upper() != MESHCORE_SERVICE_UUID:
                    reject("service is not the MeshCore companion service")
                authorize_service_device(device)

            @dbus.service.method(IFACE, in_signature="o", out_signature="s")
            def RequestPinCode(agent_self, device):
                pairing_device(device)
                reject("DisplayOnly agent cannot supply a PIN",
                       "org.bluez.Error.NotSupported")

            @dbus.service.method(IFACE, in_signature="o", out_signature="u")
            def RequestPasskey(agent_self, device):
                pairing_device(device)
                reject("DisplayOnly agent cannot supply a passkey",
                       "org.bluez.Error.NotSupported")

        class Application(dbus.service.Object):
            PATH = "/net/watchdogs/meshcore"

            def __init__(app_self):
                super().__init__(bus, app_self.PATH)
                app_self.objects = []

            @dbus.service.method("org.freedesktop.DBus.ObjectManager",
                                 out_signature="a{oa{sa{sv}}}")
            def GetManagedObjects(app_self):
                return {
                    dbus.ObjectPath(obj.path): obj.properties()
                    for obj in app_self.objects
                }

        class Service(dbus.service.Object):
            def __init__(svc_self, app):
                svc_self.path = app.PATH + "/service0"
                super().__init__(bus, svc_self.path)
                app.objects.append(svc_self)

            def properties(svc_self):
                return {"org.bluez.GattService1": {
                    "UUID": dbus.String(MESHCORE_SERVICE_UUID),
                    "Primary": dbus.Boolean(True),
                    "Includes": dbus.Array([], signature="o"),
                }}

            @dbus.service.method("org.freedesktop.DBus.Properties",
                                 in_signature="s", out_signature="a{sv}")
            def GetAll(svc_self, interface):
                return svc_self.properties().get(interface, {})

        class Characteristic(dbus.service.Object):
            IFACE = "org.bluez.GattCharacteristic1"

            def __init__(char_self, app, service, index, uuid, flags):
                char_self.path = service.path + f"/char{index}"
                char_self.uuid = uuid
                char_self.flags = flags
                char_self.notifying = False
                super().__init__(bus, char_self.path)
                app.objects.append(char_self)

            def properties(char_self):
                return {char_self.IFACE: {
                    "Service": dbus.ObjectPath(service.path),
                    "UUID": dbus.String(char_self.uuid),
                    "Flags": dbus.Array(char_self.flags, signature="s"),
                    "Notifying": dbus.Boolean(char_self.notifying),
                }}

            @dbus.service.method("org.freedesktop.DBus.Properties",
                                 in_signature="s", out_signature="a{sv}")
            def GetAll(char_self, interface):
                return char_self.properties().get(interface, {})

            @dbus.service.signal("org.freedesktop.DBus.Properties",
                                 signature="sa{sv}as")
            def PropertiesChanged(char_self, interface, changed, invalidated):
                pass

        class RxCharacteristic(Characteristic):
            @dbus.service.method(Characteristic.IFACE,
                                 in_signature="aya{sv}")
            def WriteValue(rx_self, value, options):
                address, name = authenticated_device(options)
                frame = bytes(value)
                for reply in self.protocol.handle_frame(
                        frame, authenticated=True):
                    self.notify(reply, important=True)
                self.event_callback(
                    "connected", {"address": address, "name": name})

        class TxCharacteristic(Characteristic):
            @dbus.service.method(Characteristic.IFACE,
                                 in_signature="a{sv}", out_signature="ay")
            def ReadValue(tx_self, options):
                authenticated_device(options)
                return dbus.Array([], signature="y")

            @dbus.service.method(Characteristic.IFACE)
            def StartNotify(tx_self):
                address, name = notify_device()
                tx_self.notifying = True
                tx_self.PropertiesChanged(
                    tx_self.IFACE, {"Notifying": dbus.Boolean(True)}, [])
                self.event_callback(
                    "connected", {"address": address, "name": name})

            @dbus.service.method(Characteristic.IFACE)
            def StopNotify(tx_self):
                tx_self.notifying = False
                self.protocol.cancel_signing()
                tx_self.PropertiesChanged(
                    tx_self.IFACE, {"Notifying": dbus.Boolean(False)}, [])
                self.event_callback("disconnected", "MeshMapper disconnected")

            def emit_frame(tx_self, frame):
                if not tx_self.notifying:
                    return
                try:
                    notify_device()
                except Exception as exc:
                    tx_self.notifying = False
                    tx_self.PropertiesChanged(
                        tx_self.IFACE,
                        {"Notifying": dbus.Boolean(False)}, [])
                    self.protocol.cancel_signing()
                    self.event_callback(
                        "error", "Notification authorization ended: "
                        + str(exc))
                    return
                tx_self.PropertiesChanged(
                    tx_self.IFACE,
                    {"Value": dbus.Array(frame, signature="y")}, [])

        class Advertisement(dbus.service.Object):
            IFACE = "org.bluez.LEAdvertisement1"
            PATH = "/net/watchdogs/meshcore/advertisement0"

            def __init__(adv_self):
                super().__init__(bus, adv_self.PATH)

            @dbus.service.method("org.freedesktop.DBus.Properties",
                                 in_signature="s", out_signature="a{sv}")
            def GetAll(adv_self, interface):
                if interface != adv_self.IFACE:
                    return {}
                return {
                    "Type": dbus.String("peripheral"),
                    "ServiceUUIDs": dbus.Array(
                        [MESHCORE_SERVICE_UUID], signature="s"),
                    "LocalName": dbus.String(self.local_name),
                    "Discoverable": dbus.Boolean(True),
                }

            @dbus.service.method(IFACE)
            def Release(adv_self):
                self.event_callback("stopped", "BlueZ released advertisement")

        app = Application()
        service = Service(app)
        RxCharacteristic(app, service, 0, MESHCORE_RX_UUID,
                         list(MESHCORE_RX_FLAGS))
        tx = TxCharacteristic(app, service, 1, MESHCORE_TX_UUID,
                              list(MESHCORE_TX_FLAGS))
        self._tx = tx
        advertisement = Advertisement()
        gatt = dbus.Interface(bus.get_object("org.bluez", adapter_path),
                              "org.bluez.GattManager1")
        advertising = dbus.Interface(
            bus.get_object("org.bluez", adapter_path),
            "org.bluez.LEAdvertisingManager1")

        pairing_agent = None
        pairing_registered = False
        pairing_snapshot = None
        pairing_deadline = 0.0

        def close_pairing_window(reason="closed"):
            nonlocal pairing_agent, pairing_registered, pairing_snapshot
            nonlocal pairing_deadline
            if not self._pairing_active and not pairing_registered:
                return True
            errors = []
            # Unregister first.  The manager does not release its external
            # pairing-agent lease until this function reports success.
            if pairing_registered:
                try:
                    agent_manager.UnregisterAgent(MeshMapperAgent.PATH)
                except Exception as exc:
                    errors.append("agent unregister failed: " + str(exc))
                else:
                    pairing_registered = False
            if pairing_snapshot is not None:
                old_pairable, old_timeout = pairing_snapshot
                try:
                    adapter_properties.Set(
                        _BLUEZ_ADAPTER_IFACE, "PairableTimeout", old_timeout)
                    adapter_properties.Set(
                        _BLUEZ_ADAPTER_IFACE, "Pairable", old_pairable)
                except Exception as exc:
                    errors.append("adapter state restore failed: " + str(exc))
                else:
                    pairing_snapshot = None
            pairing_deadline = 0.0
            self._security.close_window()
            self._pairing_active = bool(pairing_registered
                                        or pairing_snapshot is not None)
            self._pairing_requested = False
            pairing_agent = None if not pairing_registered else pairing_agent
            if errors:
                detail = "; ".join(errors)
                self.event_callback("pairing_cleanup_error", detail)
                return False
            self.event_callback("pairing_closed", str(reason))
            return True

        def handle_agent_release():
            nonlocal pairing_registered
            # Release may be delivered as a consequence of our own explicit
            # UnregisterAgent.  In that case cleanup has already marked the
            # registration gone and there is nothing left to do.
            if not pairing_registered:
                return False
            pairing_registered = False
            self._pairing_active = pairing_snapshot is not None
            self.event_callback(
                "error", "BlueZ unexpectedly released the MeshMapper agent")
            close_pairing_window("agent released")
            return False

        def open_pairing_window(seconds):
            nonlocal pairing_agent, pairing_registered, pairing_snapshot
            nonlocal pairing_deadline
            if self._pairing_active:
                raise RuntimeError("MeshMapper pairing window is already open")
            seconds = max(1, min(MAX_PAIRING_WINDOW, int(seconds)))
            self._pairing_requested = False
            try:
                old_pairable = adapter_properties.Get(
                    _BLUEZ_ADAPTER_IFACE, "Pairable")
                old_timeout = adapter_properties.Get(
                    _BLUEZ_ADAPTER_IFACE, "PairableTimeout")
                pairing_snapshot = (old_pairable, old_timeout)
                pairing_agent = MeshMapperAgent()
                agent_manager.RegisterAgent(
                    MeshMapperAgent.PATH, "DisplayOnly")
                pairing_registered = True
                agent_manager.RequestDefaultAgent(MeshMapperAgent.PATH)
                adapter_properties.Set(
                    _BLUEZ_ADAPTER_IFACE, "PairableTimeout",
                    dbus.UInt32(seconds))
                adapter_properties.Set(
                    _BLUEZ_ADAPTER_IFACE, "Pairable", dbus.Boolean(True))
                self._security.open_window()
                self._pairing_active = True
                self._pairing_requested = False
                pairing_deadline = time.monotonic() + seconds
                self.event_callback("pairing_open", seconds)
                return True
            except Exception:
                self._pairing_active = bool(
                    pairing_registered or pairing_snapshot is not None)
                if self._pairing_active:
                    close_pairing_window("open failed")
                else:
                    self.event_callback("pairing_closed", "open failed")
                raise

        def finish_pairing_if_ready():
            if not self._pairing_active or not self._security.claimed_path:
                return False
            props = device_properties(self._security.claimed_path)
            if not (bool(props.get("Paired"))
                    and bool(props.get("Bonded"))):
                return False
            if not self._security.claimed_authenticated:
                raise PermissionError(
                    "pairing completed without authenticated passkey entry")
            properties = dbus.Interface(
                bus.get_object("org.bluez", self._security.claimed_path),
                "org.freedesktop.DBus.Properties")
            if not bool(props.get("Trusted")):
                properties.Set(
                    _BLUEZ_DEVICE_IFACE, "Trusted", dbus.Boolean(True))
                props = device_properties(self._security.claimed_path)
            self._security.authorize_gatt(
                self._security.claimed_path, props)
            address, name = self._security.retain_claim()
            self.paired_address, self.paired_name = address, name
            self.event_callback("paired", {
                "address": address,
                "name": name,
            })
            close_pairing_window("paired")
            return True

        def forget_retained_phone(address):
            exact_address = _normalize_ble_address(address)
            if not exact_address or exact_address != self.paired_address:
                raise RuntimeError("retained MeshMapper address changed")
            match = ""
            for path, interfaces in root.GetManagedObjects().items():
                props = interfaces.get(_BLUEZ_DEVICE_IFACE)
                if props is None:
                    continue
                if (str(props.get("Adapter", "")) == adapter_path
                        and _normalize_ble_address(props.get("Address", ""))
                        == exact_address):
                    match = str(path)
                    break
            if not match:
                raise RuntimeError(
                    "retained MeshMapper bond is not present in BlueZ")
            adapter_api.RemoveDevice(dbus.ObjectPath(match))
            old_address, old_name = self._security.forget()
            self.paired_address = ""
            self.paired_name = ""
            self.protocol.cancel_signing()
            self.event_callback("bond_removed", {
                "address": old_address,
                "name": old_name,
            })

        def drain_commands():
            while True:
                try:
                    name, payload, completion, result = (
                        self._commands.get_nowait())
                except Empty:
                    break
                succeeded = False
                try:
                    if name == "open_pairing":
                        succeeded = open_pairing_window(payload)
                    elif name == "close_pairing":
                        succeeded = close_pairing_window("closed")
                    elif name == "forget_phone":
                        forget_retained_phone(payload)
                        succeeded = True
                    else:
                        raise RuntimeError("unknown BLE control command")
                except Exception as exc:
                    self.event_callback("error", str(exc) or type(exc).__name__)
                finally:
                    if result is not None:
                        result.append(bool(succeeded))
                    if completion is not None:
                        completion.set()
            return False

        self._drain_commands = drain_commands
        self._loop = GLib.MainLoop()
        registration = _BluezRegistration(
            gatt, advertising, app.PATH, advertisement.PATH,
            object_path=dbus.ObjectPath,
            stop_requested=self.stop_event.is_set,
            on_ready=lambda: self.event_callback("ready", self.adapter),
            on_terminal=self._loop.quit,
        )

        def check_stop():
            drain_commands()
            registration.expire()
            if registration.ready and self._pairing_active:
                try:
                    finish_pairing_if_ready()
                except Exception as exc:
                    self.event_callback("error", "Pairing verification failed: "
                                        + str(exc))
                    close_pairing_window("verification failed")
                if (self._pairing_active and pairing_deadline
                        and time.monotonic() >= pairing_deadline):
                    close_pairing_window("timeout")
            if self.stop_event.is_set():
                self._loop.quit()
                return False
            return True

        GLib.timeout_add(100, check_stop)
        security_cleanup_ok = True
        cleanup_errors = []
        try:
            if not registration.begin():
                raise RuntimeError(registration.failure)
            self._loop.run()
            if registration.failure and not self.stop_event.is_set():
                raise RuntimeError(registration.failure)
        finally:
            self.protocol.cancel_signing()
            if self._pairing_active or pairing_registered:
                security_cleanup_ok = close_pairing_window("stopped")
            cleanup_errors = registration.cleanup()
            self._tx = None
            self.event_callback("stopped", "MeshCore companion BLE stopped")
            bus_close_error = ""
            try:
                bus.close()
            except Exception as exc:
                bus_close_error = (
                    str(exc).strip() or type(exc).__name__)
            if cleanup_errors and not bus_close_error:
                log.warning(
                    "MeshCore companion cleanup completed through D-Bus "
                    "disconnect after explicit cleanup errors: %s",
                    "; ".join(cleanup_errors))
            elif cleanup_errors or bus_close_error:
                detail = "; ".join(cleanup_errors + ([
                    "private BlueZ connection close failed: "
                    + bus_close_error] if bus_close_error else []))
                if cleanup_errors:
                    raise RuntimeError(
                        "MeshCore companion cleanup could not be verified: "
                        + detail)
                log.warning(
                    "Private BlueZ connection close failed after explicit "
                    "peripheral unregistration: %s", bus_close_error)
            if not security_cleanup_ok:
                raise RuntimeError(
                    "MeshMapper pairing security cleanup could not be verified")


class MeshCoreBleManager:
    """Lifecycle wrapper around the standard MeshCore companion peripheral."""

    def __init__(self, protocol: MeshCoreCompanionProtocol,
                 *, backend_factory=None,
                 adapter_resolver=resolve_ble_adapter,
                 adapter_lister=list_ble_adapters,
                 thread_factory=threading.Thread,
                 pairing_lease_acquire=None,
                 pairing_lease_release=None) -> None:
        self.protocol = protocol
        self.backend_factory = backend_factory or _BluezPeripheral
        self.adapter_resolver = adapter_resolver
        self.adapter_lister = adapter_lister
        self.thread_factory = thread_factory
        self.pairing_lease_acquire = (
            pairing_lease_acquire or (lambda _seconds: True))
        self.pairing_lease_release = (
            pairing_lease_release or (lambda: True))
        self.events: Queue = Queue(maxsize=64)
        self.state = "stopped"
        self.last_error = ""
        self.adapter = ""
        self.connected = False
        self.drop_count = 0
        self.event_drop_count = 0
        self.pairing_state = "closed"
        self.pairing_pin = ""
        self.paired_address = ""
        self.paired_name = ""
        self.pairing_cleanup_pending = False
        self._pairing_lease_held = False
        self._pairing_local_cleanup_done = False
        self._thread = None
        self._backend = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        return self.state in ("starting", "ready", "connected", "stopping")

    @property
    def worker_active(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def _release_pairing_lease(self) -> bool:
        with self._lock:
            if not self._pairing_lease_held:
                self._pairing_local_cleanup_done = False
                return True
        try:
            released = self.pairing_lease_release()
            ok = released is not False
        except Exception as exc:
            ok = False
            self.last_error = "Pairing-agent lease release failed: " + str(exc)
        with self._lock:
            if ok:
                self._pairing_lease_held = False
                self._pairing_local_cleanup_done = False
                self.pairing_cleanup_pending = False
            else:
                self.pairing_cleanup_pending = True
                self.pairing_state = "error"
                if not self.last_error:
                    self.last_error = "Pairing-agent lease release failed"
        return ok

    def _event(self, name: str, detail) -> None:
        release_lease = False
        with self._lock:
            if name == "ready":
                self.state = "ready"
                self.adapter = detail
            elif name == "connected":
                self.connected = True
                self.state = "connected"
            elif name == "disconnected":
                self.connected = False
                self.protocol.cancel_signing()
                if self.state != "stopping":
                    self.state = "ready"
            elif name == "stopped":
                self.connected = False
                self.protocol.cancel_signing()
            elif name == "pairing_open":
                self.pairing_state = "open"
                self.pairing_pin = ""
            elif name == "pairing_pin":
                self.pairing_pin = str(detail or "")
            elif name == "paired":
                values = detail if isinstance(detail, dict) else {}
                self.paired_address = _normalize_ble_address(
                    values.get("address", ""))
                self.paired_name = str(values.get("name", "")).strip()[:64]
                self.pairing_state = "paired"
                self.pairing_pin = ""
            elif name == "pairing_closed":
                self.pairing_state = (
                    "paired" if self.paired_address else "closed")
                self.pairing_pin = ""
                self._pairing_local_cleanup_done = True
                release_lease = True
            elif name == "bond_removed":
                self.paired_address = ""
                self.paired_name = ""
                self.pairing_state = "closed"
                self.pairing_pin = ""
                self.pairing_cleanup_pending = False
            elif name == "pairing_cleanup_error":
                self._pairing_local_cleanup_done = False
                self.pairing_cleanup_pending = True
                self.pairing_state = "error"
                self.last_error = str(detail)
                log.error(
                    "MeshCore companion pairing cleanup error: %s", detail)
            elif name == "error":
                self.last_error = str(detail)
                log.error("MeshCore companion BLE error: %s", detail)
                if self.pairing_state == "forgetting":
                    self.pairing_cleanup_pending = True
                    self.pairing_state = "error"
        if release_lease:
            self._release_pairing_lease()

        critical = name in {
            "pairing_open", "pairing_pin", "paired", "pairing_closed",
            "bond_removed", "pairing_cleanup_error", "error", "stopped",
        }
        try:
            self.events.put_nowait((name, detail))
        except Full:
            with self.events.mutex:
                if critical:
                    replace = next((
                        index for index, (queued_name, _queued_detail)
                        in enumerate(self.events.queue)
                        if queued_name not in {
                            "pairing_open", "pairing_pin", "paired",
                            "pairing_closed", "bond_removed",
                            "pairing_cleanup_error", "error", "stopped",
                        }), 0)
                    del self.events.queue[replace]
                    self.events.queue.append((name, detail))
                    self.events.not_empty.notify()
                self.event_drop_count += 1
            if critical:
                with self._lock:
                    overflow = ("MeshCore BLE event queue overflow; an older "
                                "event was replaced")
                    if overflow not in self.last_error:
                        self.last_error = (
                            (self.last_error + "; ") if self.last_error else ""
                        ) + overflow

    def _resolve_adapter(self, selection: str) -> str:
        resolved = self.adapter_resolver(selection)
        if resolved:
            return resolved
        choices = self.adapter_lister()
        if not choices:
            raise RuntimeError("No BlueZ Bluetooth adapter is available")
        return choices[0][1]

    def start(self, adapter: str = "auto", node_name: str = "WDG", *,
              paired_address: str = "", paired_name: str = "") -> bool:
        with self._lock:
            if (self.worker_active or self.running
                    or self.pairing_cleanup_pending):
                return False
            self._stop_event = threading.Event()
            self.state = "starting"
            self.last_error = ""
            self.paired_address = _normalize_ble_address(paired_address)
            if paired_address and not self.paired_address:
                self.state = "error"
                self.last_error = "Invalid retained MeshMapper Bluetooth address"
                self._event("error", self.last_error)
                return False
            self.paired_name = str(paired_name or "").strip()[:64]
            self.pairing_state = (
                "paired" if self.paired_address else "closed")
            self.pairing_pin = ""
            self.pairing_cleanup_pending = False
            try:
                resolved = self._resolve_adapter(adapter)
                local_name = _utf8_prefix(
                    "MeshCore-" + str(node_name), 29).decode("utf-8")
                backend = self.backend_factory(
                    resolved, local_name, self.protocol,
                    self._stop_event, self._event,
                    paired_address=self.paired_address,
                    paired_name=self.paired_name)
                self._backend = backend
                thread = self.thread_factory(
                    target=self._run, args=(backend,),
                    name="wdg-meshcore-ble", daemon=True)
                self._thread = thread
                thread.start()
                return True
            except Exception as exc:
                self._backend = None
                self._thread = None
                self.state = "error"
                self.last_error = str(exc) or type(exc).__name__
                self._event("error", self.last_error)
                return False

    def _run(self, backend) -> None:
        try:
            backend.run()
        except Exception as exc:
            if not self._stop_event.is_set():
                self.state = "error"
                self.last_error = str(exc) or type(exc).__name__
                self._event("error", self.last_error)
        finally:
            with self._lock:
                self.connected = False
                if self.state != "error":
                    self.state = "stopped"
                if self._thread is threading.current_thread():
                    self._thread = None
                self._backend = None

    def open_pairing(self, seconds: int = MAX_PAIRING_WINDOW) -> bool:
        with self._lock:
            backend = self._backend
            if (backend is None or self.state not in ("ready", "connected")
                    or self.pairing_state in ("opening", "open", "closing")
                    or self.pairing_cleanup_pending
                    or self.paired_address):
                return False
            seconds = max(1, min(MAX_PAIRING_WINDOW, int(seconds)))
            try:
                leased = self.pairing_lease_acquire(seconds)
            except Exception as exc:
                self.last_error = "Pairing-agent lease failed: " + str(exc)
                self._event("error", self.last_error)
                return False
            if not leased:
                self.last_error = "Another Bluetooth pairing flow is active"
                self._event("error", self.last_error)
                return False
            self._pairing_lease_held = True
            self._pairing_local_cleanup_done = False
            self.pairing_state = "opening"
            self.pairing_pin = ""
            if backend.open_pairing(seconds):
                return True
            self.pairing_state = "closed"
        self._release_pairing_lease()
        return False

    def close_pairing(self, timeout: float = 5.0) -> bool:
        with self._lock:
            backend = self._backend
            if not self._pairing_lease_held:
                self.pairing_pin = ""
                if not self.pairing_cleanup_pending:
                    self.pairing_state = (
                        "paired" if self.paired_address else "closed")
                return not self.pairing_cleanup_pending
            if self._pairing_local_cleanup_done:
                retry_release = True
            else:
                retry_release = False
            self.pairing_state = "closing"
        if retry_release:
            released = self._release_pairing_lease()
            if released:
                with self._lock:
                    self.pairing_state = (
                        "paired" if self.paired_address else "closed")
            return released
        cleaned = bool(backend is not None
                       and backend.close_pairing(timeout=timeout))
        if not cleaned:
            with self._lock:
                self.pairing_cleanup_pending = True
                self.pairing_state = "error"
                self.last_error = (
                    "MeshMapper pairing-agent cleanup could not be verified")
            return False
        with self._lock:
            self._pairing_local_cleanup_done = True
        released = self._release_pairing_lease()
        with self._lock:
            self.pairing_pin = ""
            if released:
                self.pairing_state = (
                    "paired" if self.paired_address else "closed")
        return released

    def forget_phone(self, timeout: float = 5.0) -> bool:
        with self._lock:
            backend = self._backend
            if (backend is None or not self.paired_address
                    or self.connected
                    or self.pairing_state in (
                        "opening", "open", "closing", "forgetting")
                    or self.pairing_cleanup_pending):
                return False
            self.pairing_state = "forgetting"
        forgotten = bool(backend.forget_phone(timeout=timeout))
        with self._lock:
            if not forgotten:
                self.pairing_cleanup_pending = True
                self.pairing_state = "error"
                if not self.last_error:
                    self.last_error = (
                        "BlueZ phone-bond removal outcome is uncertain")
        return forgotten

    def stop(self, timeout: float = 5.0) -> bool:
        self.protocol.cancel_signing()
        security_ok = self.close_pairing(
            timeout=min(max(0.0, float(timeout)), 5.0))
        with self._lock:
            thread = self._thread
            if thread is None:
                self.state = "stopped"
                self.connected = False
                return security_ok and not self.pairing_cleanup_pending
            self.state = "stopping"
            self._stop_event.set()
        if thread is not threading.current_thread():
            thread.join(max(0.0, float(timeout)))
        stopped = not thread.is_alive()
        if stopped:
            with self._lock:
                self._thread = None
                self._backend = None
                self.state = "stopped"
                self.connected = False
        return stopped and security_ok and not self.pairing_cleanup_pending

    close = stop

    def on_radio_packet(self, packet: bytes, rssi: float, snr: float,
                        payload_type: int, payload: bytes,
                        path_byte: int) -> None:
        backend = self._backend
        if backend is None or not self.running:
            return
        if not backend.notify(self.protocol.raw_packet_event(packet, rssi, snr)):
            self.drop_count += 1
        if payload_type == 0x0B and payload and payload[0] & 0xF0 == 0x90:
            if not backend.notify(self.protocol.control_event(
                    payload, path_byte, rssi, snr), important=True):
                self.drop_count += 1
        self.drop_count = max(
            self.drop_count, int(getattr(backend, "drop_count", 0)))

    def poll_events(self, limit: int = 50) -> list[tuple[str, str]]:
        result = []
        for _ in range(max(0, int(limit))):
            try:
                result.append(self.events.get_nowait())
            except Empty:
                break
        return result
