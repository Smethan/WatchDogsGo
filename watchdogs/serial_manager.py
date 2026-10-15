"""Serial communication with ESP32-C5 device."""

import os
import sys
import time
import logging
import threading
from collections import deque
from typing import List, Optional, Callable, Tuple

import serial
import serial.tools.list_ports

from .config import BAUD_RATE, READ_TIMEOUT, SCAN_TIMEOUT, CRASH_KEYWORDS

log = logging.getLogger(__name__)

# Known USB VID:PID pairs for ESP32 USB-UART bridges
_ESP32_VID_PIDS: List[Tuple[int, int]] = [
    (0x10C4, 0xEA60),  # CP210x (Silicon Labs) — CP2102N on ESP32 devkits
    (0x1A86, 0x7523),  # CH340/CH341 (WCH) — common cheap ESP32 boards
    (0x1A86, 0x55D4),  # CH9102 (WCH) — newer variant
    (0x0403, 0x6001),  # FTDI FT232R
    (0x0403, 0x6015),  # FTDI FT231X
    (0x303A, 0x1001),  # Espressif native USB-JTAG (ESP32-S3/C3/C6/C5)
    (0x303A, 0x4001),  # Espressif native USB-CDC (ESP32-S2)
]
_ESP32_NATIVE_VID_PIDS = {(0x303A, 0x1001), (0x303A, 0x4001)}
_METADATA_COMMANDS = {"version", "get_device_info", "get_capabilities"}
_METADATA_WRITE_TIMEOUT = 0.25


class _NativeJTAGSerial(serial.Serial):
    """Apply the Linux native USB modem lines in one control request.

    cdc_acm may assert both lines on open. pyserial normally clears DTR and
    RTS separately, briefly leaving RTS asserted with DTR cleared. On C5's
    USB Serial/JTAG that intermediate state can reset/stall the console.
    Keep this behavior scoped to identified native JTAG endpoints on Linux;
    external USB-UART bridges retain pyserial's normal handling.
    """

    def _update_native_lines(self):
        import fcntl
        import struct
        import termios

        mask = termios.TIOCM_DTR | termios.TIOCM_RTS
        state = ((termios.TIOCM_DTR if self._dtr_state else 0) |
                 (termios.TIOCM_RTS if self._rts_state else 0))
        # Clearing the pair together avoids the RTS-only intermediate state.
        operation = termios.TIOCMSET if state else termios.TIOCMBIC
        value = state if state else mask
        fcntl.ioctl(self.fd, operation, struct.pack("I", value))

    def _update_dtr_state(self):
        self._update_native_lines()

    def _update_rts_state(self):
        self._update_native_lines()


def detect_esp32_port() -> Optional[str]:
    """Prefer one native Espressif device; never guess from a tty number.

    USB-UART bridge IDs are candidates, not proof of the attached chip. Multiple
    candidates require an explicit port instead of picking the first device.
    """
    try:
        ports = list(serial.tools.list_ports.comports())
    except Exception:
        return None
    native = [p for p in ports if (p.vid, p.pid) in _ESP32_NATIVE_VID_PIDS]
    candidates = native or [p for p in ports if (p.vid, p.pid) in _ESP32_VID_PIDS]
    if len(candidates) == 1:
        return candidates[0].device
    if candidates:
        log.warning("Multiple ESP32 candidates; select an explicit serial port")
    return None


def list_usb_serial_devices() -> List[Tuple[str, str, bool]]:
    """List all USB serial devices with descriptions.

    Returns list of (device_path, description, is_esp32) tuples.
    Description includes chip name (e.g. 'CP2102N') and VID:PID.
    """
    try:
        ports = serial.tools.list_ports.comports()
    except Exception:
        return []

    result = []
    for port in ports:
        if port.vid is None or port.pid is None:
            continue
        is_esp32 = (port.vid, port.pid) in _ESP32_VID_PIDS
        desc = port.description or f"{port.vid:04X}:{port.pid:04X}"
        log.info(
            "USB serial: %s [%04X:%04X] %s %s",
            port.device, port.vid, port.pid, desc,
            "(ESP32)" if is_esp32 else "",
        )
        result.append((port.device, desc, is_esp32))

    result.sort(key=lambda x: x[0])
    return result


def list_wifi_interfaces() -> List[Tuple[str, str, str, str]]:
    """List all WiFi interfaces with driver and chipset info.

    Returns list of (iface_name, mode, driver, chipset) tuples.
    Mode is 'managed', 'monitor', 'AP', etc.
    """
    import subprocess
    result_list: List[Tuple[str, str, str, str]] = []
    try:
        proc = subprocess.run(
            ["iw", "dev"], capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return result_list

    current_iface = None
    current_type = "managed"
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Interface "):
            if current_iface:
                driver, chipset = _get_iface_info(current_iface)
                result_list.append((current_iface, current_type, driver, chipset))
            current_iface = stripped.split()[1]
            current_type = "managed"
        elif stripped.startswith("type "):
            current_type = stripped.split()[1]
    if current_iface:
        driver, chipset = _get_iface_info(current_iface)
        result_list.append((current_iface, current_type, driver, chipset))

    return result_list


def _get_iface_info(iface: str) -> Tuple[str, str]:
    """Get driver name and chipset for a network interface."""
    driver = ""
    chipset = ""
    driver_path = f"/sys/class/net/{iface}/device/driver"
    try:
        driver = os.path.basename(os.readlink(driver_path))
    except Exception:
        pass
    product_path = f"/sys/class/net/{iface}/device/../product"
    try:
        real = os.path.realpath(product_path)
        with open(real) as f:
            chipset = f.read().strip()
    except Exception:
        pass
    return driver, chipset


class SerialLineBuffer:
    """Accumulate raw bytes and yield complete newline-terminated lines.

    Used with urwid's ``watch_file`` callback where reads are non-blocking
    and may deliver partial lines.
    """

    def __init__(self, max_line=8192) -> None:
        # Legacy handshake/base64 lines need more than the WDG protocol's 1024.
        self._buf = b""
        self.max_line = max_line
        self.discarding = False
        self.dropped_lines = 0

    def feed(self, raw: bytes) -> List[str]:
        lines = []
        parts = raw.split(b"\n")
        for i, part in enumerate(parts):
            end = i < len(parts)-1
            if end:
                part += b"\n"
            if not self.discarding:
                if len(self._buf) + len(part) > self.max_line:
                    self._buf = b""
                    self.discarding = True
                    self.dropped_lines += 1
                else:
                    self._buf += part
            if end:
                if not self.discarding:
                    line = self._buf.decode("utf-8", "replace").strip()
                    if line:
                        lines.append(line)
                self._buf = b""
                self.discarding = False
        return lines


class SerialManager:
    """Manage the serial connection to an ESP32-C5 device."""

    def __init__(self, device: str) -> None:
        self.device = device
        self.serial_conn: Optional[serial.Serial] = None
        self.usb_port_info = None
        self.baud_rate = BAUD_RATE
        self.line_buffer = SerialLineBuffer()
        self._pending_lines = deque()
        # read_available() is shared by the normal poller and updater workers.
        # Keep close/setup from invalidating pyserial's POSIX fd mid-read.
        self._io_lock = threading.RLock()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Open the serial port. Raises on failure."""
        if not os.path.exists(self.device):
            raise FileNotFoundError(f"Device {self.device} does not exist")

        if not os.access(self.device, os.R_OK | os.W_OK):
            raise PermissionError(
                f"No read/write access to '{self.device}'. "
                "Try: sudo usermod -a -G dialout $USER"
            )

        # Preserve USB identity while the connection is established. The tty
        # path may later disappear or be reused by another device after BOOT.
        self.usb_port_info = None
        try:
            matches = [p for p in serial.tools.list_ports.comports()
                       if os.path.realpath(p.device) == os.path.realpath(self.device)]
            if len(matches) == 1:
                self.usb_port_info = matches[0]
        except Exception:
            pass
        with self._io_lock:
            self.line_buffer = SerialLineBuffer()
            self._pending_lines.clear()
            native_jtag = (sys.platform.startswith("linux") and
                           self.usb_port_info is not None and
                           (self.usb_port_info.vid, self.usb_port_info.pid) ==
                           (0x303A, 0x1001))
            serial_type = _NativeJTAGSerial if native_jtag else serial.Serial
            conn = serial_type(
                port=None,
                baudrate=self.baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=READ_TIMEOUT,
                write_timeout=2,
            )
            # Set the desired state before open; native Linux JTAG applies both
            # lines together so pyserial does not introduce a reset sequence.
            conn.dtr = False
            conn.rts = False
            conn.port = self.device
            conn.open()
            self.serial_conn = conn
        log.info("Serial port %s opened at %d baud", self.device, self.baud_rate)

    def probe(self) -> bool:
        """Check whether the serial transport is writable or has pending input."""
        with self._io_lock:
            return self._probe_locked()

    def _probe_locked(self) -> bool:
        """Run the responsiveness probe while holding the serial I/O lock.

        Checks for pending data first (boot banner). If nothing is pending,
        tries a bounded newline write. A successful write only means the host
        accepted the bytes; firmware readiness still requires a validated reply.
        """
        if not self.serial_conn:
            return False
        try:
            # Check if anything is already waiting (boot output)
            if self.serial_conn.in_waiting > 0:
                return True
            # Bound the probe write and avoid flush(): POSIX tcdrain may block
            # indefinitely when native CDC stops draining its output queue.
            conn = self.serial_conn
            previous_timeout = getattr(conn, "write_timeout", None)
            try:
                conn.write_timeout = _METADATA_WRITE_TIMEOUT
                written = conn.write(b"\r\n")
                if written != 2:
                    reset = getattr(conn, "reset_output_buffer", None)
                    if callable(reset):
                        reset()
                    return False
                return True
            except Exception:
                reset = getattr(conn, "reset_output_buffer", None)
                if callable(reset):
                    try:
                        reset()
                    except Exception:
                        pass
                return False
            finally:
                conn.write_timeout = previous_timeout
        except Exception:
            return False

    def close(self) -> None:
        with self._io_lock:
            conn = self.serial_conn
            if conn is not None:
                try:
                    conn.close()
                finally:
                    if self.serial_conn is conn:
                        self.serial_conn = None

    def reset_output_buffer(self) -> bool:
        """Best-effort discard of queued output while excluding other I/O."""
        with self._io_lock:
            conn = self.serial_conn
            if conn is None:
                return False
            reset = getattr(conn, "reset_output_buffer", None)
            if not callable(reset):
                return False
            try:
                reset()
                return True
            except Exception as exc:
                log.debug("Could not reset serial output buffer: %s", exc)
                return False

    def write_payload(
        self,
        payload: bytes,
        *,
        timeout: float | None = None,
        chunk_size: int | None = None,
        inter_chunk_delay: float = 0.0,
    ) -> int:
        """Write raw protocol bytes atomically with bounded recovery.

        Unlike ``send_command``, this preserves the caller's payload exactly,
        which is required by OTA transfer framing. It never calls ``flush``;
        on an exception or short write it discards pending output best-effort
        before releasing the shared I/O lock.
        """
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("serial payload must be bytes-like")
        data = bytes(payload)
        if chunk_size is not None and chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        if inter_chunk_delay < 0:
            raise ValueError("inter_chunk_delay must not be negative")
        with self._io_lock:
            conn = self.serial_conn
            if conn is None:
                raise ConnectionError("Serial endpoint is disconnected")
            previous_timeout = getattr(conn, "write_timeout", None)
            try:
                if timeout is not None:
                    conn.write_timeout = timeout
                size = chunk_size or max(len(data), 1)
                written_total = 0
                for offset in range(0, len(data), size):
                    part = data[offset : offset + size]
                    written = conn.write(part)
                    if written != len(part):
                        raise OSError(
                            f"Incomplete serial write: wrote {written} of {len(part)} bytes"
                        )
                    written_total += written
                    if inter_chunk_delay and offset + size < len(data):
                        time.sleep(inter_chunk_delay)
                return written_total
            except Exception:
                reset = getattr(conn, "reset_output_buffer", None)
                if callable(reset):
                    try:
                        reset()
                    except Exception as reset_exc:
                        log.debug("Could not reset failed serial output: %s", reset_exc)
                raise
            finally:
                if timeout is not None:
                    try:
                        conn.write_timeout = previous_timeout
                    except Exception:
                        pass

    @property
    def is_open(self) -> bool:
        with self._io_lock:
            conn = self.serial_conn
            return conn is not None and bool(conn.is_open)

    @property
    def fd(self) -> int:
        """Return the file descriptor for use with urwid watch_file."""
        with self._io_lock:
            conn = self.serial_conn
            if conn is None:
                raise RuntimeError("Serial port not open")
            return conn.fileno()

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def send_command(self, command: str) -> bool:
        """Best-effort write; return whether the complete command was written."""
        return self._send_command(command, strict=False)

    def send_command_strict(self, command: str) -> None:
        """Write a command without hiding short or failed writes.

        The portal HTML transfer is a multi-command transaction: continuing
        after a short or failed write could start a job with incomplete HTML.
        """
        self._send_command(command, strict=True)

    def _send_command(self, command: str, *, strict: bool) -> bool:
        # Serialize writes with each other and with reads. In particular, a
        # metadata timeout must not temporarily change another writer's timeout
        # or clear its queued bytes during recovery.
        with self._io_lock:
            return self._send_command_locked(command, strict=strict)

    def _send_command_locked(self, command: str, *, strict: bool) -> bool:
        metadata = command in _METADATA_COMMANDS
        if not self.serial_conn:
            if strict or command.startswith("job_") or metadata:
                raise ConnectionError("Job endpoint is disconnected")
            log.error("Serial not connected — cannot send %r", command)
            return False
        conn = self.serial_conn
        try:
            payload = (command + "\r\n").encode("utf-8")
            if metadata:
                # Capability/identity probes run from the UI polling path.
                # Bound a blocked CDC write to a short interval and restore
                # the normal timeout immediately after the small command.
                previous_timeout = getattr(conn, "write_timeout", None)
                conn.write_timeout = _METADATA_WRITE_TIMEOUT
                try:
                    written = conn.write(payload)
                finally:
                    conn.write_timeout = previous_timeout
            else:
                written = conn.write(payload)
            if written != len(payload):
                raise OSError("Incomplete serial command write")
            # Do not call pySerial flush(): on POSIX it uses tcdrain, which can
            # wait forever when native USB CDC stops draining. USB writes retain
            # their byte order without an additional drain operation.
            if not strict and not (
                command.startswith("wardrive_keepalive ")
                or command.startswith("wardrive_status ")
                or command.startswith("job_")
                or metadata
            ):
                time.sleep(0.1)
                log.debug("TX: %s", command)
            return True
        except Exception as exc:
            log.error("Send error: %s", exc)
            # Any failed write may leave bytes queued in the OS or USB driver.
            # Reset while holding _io_lock so close or a later command cannot
            # wait on or inherit that stalled output. This is best effort only;
            # USB adapters may not discard every byte already in flight.
            reset = getattr(conn, "reset_output_buffer", None)
            if callable(reset):
                try:
                    reset()
                except Exception as reset_exc:
                    log.debug("Could not reset failed serial output: %s", reset_exc)
            if strict or command.startswith("job_") or metadata:
                raise
            return False

    def read_available(self, max_bytes: int = 16384, max_lines: int = 64) -> List[str]:
        """Non-blocking read: grab whatever bytes are waiting and return
        complete lines via the internal line buffer."""
        if max_bytes < 1 or max_lines < 1:
            raise ValueError("Serial read budgets must be positive")
        with self._io_lock:
            conn = self.serial_conn
            if conn is None:
                return []
            if not conn.is_open:
                raise ConnectionError("Serial endpoint is closed")
            # pyserial POSIX keeps is_open true if a transport's descriptor is
            # externally invalidated. Avoid calling ioctl(in_waiting, fd=None).
            if hasattr(conn, "fd") and conn.fd is None:
                raise ConnectionError("Serial endpoint has no file descriptor")
            # Pending lines count against this tick before accepting more bytes.
            # This bounds both dispatch time and the retained queue under floods.
            if not self._pending_lines:
                try:
                    waiting = min(conn.in_waiting, max_bytes)
                    if waiting > 0:
                        self._pending_lines.extend(self.line_buffer.feed(conn.read(waiting)))
                except TypeError as exc:
                    # Only normalize pyserial's known invalid-fd failure. Keep
                    # unrelated TypeErrors visible as programming errors.
                    if hasattr(conn, "fd") and conn.fd is None:
                        raise ConnectionError("Serial endpoint lost its file descriptor") from exc
                    raise
            return [self._pending_lines.popleft()
                    for _ in range(min(max_lines, len(self._pending_lines)))]

    def read_response(self, timeout: float = SCAN_TIMEOUT, idle_timeout: float = 1.5) -> List[str]:
        """Blocking read with timeout — kept for legacy / direct-call use."""
        if not self.serial_conn:
            return []

        lines: List[str] = []
        start = time.time()
        last_data = time.time()

        while time.time() - start < timeout:
            if self.serial_conn.in_waiting:
                try:
                    line = self.serial_conn.readline().decode("utf-8", errors="replace").strip()
                    if line:
                        lines.append(line)
                        last_data = time.time()
                except Exception:
                    continue
            else:
                if lines and (time.time() - last_data) >= idle_timeout:
                    break
                time.sleep(0.05)

        return lines

    # ------------------------------------------------------------------
    # Crash detection helper
    # ------------------------------------------------------------------

    @staticmethod
    def is_crash_line(line: str) -> bool:
        return any(kw in line for kw in CRASH_KEYWORDS)
