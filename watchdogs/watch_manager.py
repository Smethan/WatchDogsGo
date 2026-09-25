"""PipBoy Watch BLE Manager — connect, pair, command, receive events.

Uses bleak for BLE GATT + dbus-python Agent1 for PIN pairing.
Thread + queue pattern (same as serial_manager / sdr_manager).

Protocol: JSON lines over Nordic UART Service (NUS), \\n terminated.
"""

import asyncio
import json
import logging
import os
import threading
import time
from queue import Queue
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Nordic UART Service UUIDs
NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # game → watch (write)
NUS_TX_CHAR = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # watch → game (notify)

SCAN_TIMEOUT = 10.0
CONNECT_TIMEOUT = 15.0


class PairingAgentRequestError(RuntimeError):
    """A BlueZ agent request that must be rejected with a specific error."""

    def __init__(self, message: str, dbus_error_name: str):
        super().__init__(message)
        self.dbus_error_name = dbus_error_name


class WatchManager:
    """Manages BLE connection to PipBoy watch."""

    def __init__(self, *, pairing_lease_acquire=None,
                 pairing_lease_release=None, scan_lease_acquire=None,
                 scan_lease_release=None):
        self.connected = False
        self.paired = False
        self.scanning = False
        self.device_name: str = ""
        self.device_address: str = ""

        # Event queue for app.py (type, data)
        self._events: Queue = Queue()
        # Command TX queue
        self._tx_queue: Queue = Queue()

        # RX buffer for chunked NUS messages
        self._rx_buffer = ""

        # State
        self._client = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._scan_results: list = []

        # PIN pairing
        self._pin_requested = False
        self._pin_value: Optional[int] = None
        self._expected_confirmation_passkey: Optional[int] = None
        self._pin_event = threading.Event()
        self._agent_stop_event = threading.Event()
        self._agent_cleanup_confirmed = threading.Event()
        self._agent_ready = threading.Event()
        self._agent_registered = False
        self._agent_thread: Optional[threading.Thread] = None
        self._agent_cleanup_thread: Optional[threading.Thread] = None
        self._agent_thread_factory = threading.Thread
        self._agent_cleanup_thread_factory = threading.Thread
        self._agent_generation = 0
        self._agent_path = ""
        self._pin_wait_loop = None
        self._pairing_device_suffix = ""
        self._pairing_lease_acquire = pairing_lease_acquire
        self._pairing_lease_release = pairing_lease_release
        self._pending_pairing_lease_release = None
        self._scan_lease_acquire = scan_lease_acquire
        self._scan_lease_release = scan_lease_release
        # A failed release is deliberately retained.  The daemon keeps the
        # fixed ``watch`` owner until it observes a successful release (or its
        # own lease expires), so forgetting this callback would make later
        # scans fail with an invisible stale owner.
        self._pending_scan_lease_release = None

        # Watch state cache
        self.battery: int = 0
        self.version: str = ""
        self.features: list = []

        # Callbacks
        self.on_nfc_tag: Optional[Callable] = None
        self.on_lora_msg: Optional[Callable] = None

    def check_existing(self) -> Optional[str]:
        """Check if a PipBoy is already known/connected in BlueZ."""
        try:
            import subprocess
            # List all known devices (not just paired)
            result = subprocess.run(
                ["bluetoothctl", "devices"],
                capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                if "PipBoy" not in line:
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                addr = parts[1]
                name = " ".join(parts[2:])
                # Check if actually connected
                info = subprocess.run(
                    ["bluetoothctl", "info", addr],
                    capture_output=True, text=True, timeout=5)
                connected = "Connected: yes" in info.stdout
                if connected:
                    self._events.put(("log",
                        f"[Watch] Found connected: {name} [{addr}]"))
                    return addr
                else:
                    self._events.put(("log",
                        f"[Watch] Found known (not connected): {name} [{addr}]"))
        except Exception:
            pass
        return None

    def scan(self) -> bool:
        """Start scanning for PipBoy devices."""
        with self._state_lock:
            if self.worker_active:
                return False
            if not self._release_scan_lease_once():
                self._events.put((
                    "error", "Previous watch BLE scan lease is still being "
                    "released; retry after cleanup completes"))
                return False
            self._stop_event.clear()
            self._agent_stop_event.clear()
            self._scan_results.clear()
            self.scanning = True
            try:
                thread = threading.Thread(
                    target=self._run_scan, daemon=True)
                self._thread = thread
                thread.start()
            except Exception as exc:
                self._thread = None
                self.scanning = False
                self._events.put((
                    "error", "Watch scan worker could not start: " + str(exc)))
                return False
            return True

    def connect(self, address: str) -> bool:
        """Connect to a specific PipBoy device."""
        with self._state_lock:
            if self.connected or self.scanning or self.worker_active:
                return False
            if not self._release_scan_lease_once():
                self._events.put((
                    "error", "Previous watch BLE scan lease is still being "
                    "released; retry after cleanup completes"))
                return False
            if self._pending_pairing_lease_release is not None:
                self._schedule_agent_cleanup(
                    self._agent_thread,
                    self._pending_pairing_lease_release)
                self._events.put((
                    "error", "Previous pairing-agent lease is still being "
                    "released; retry after cleanup completes"))
                return False
            self._stop_event.clear()
            self.device_address = address
            try:
                thread = threading.Thread(
                    target=self._run_connect, args=(address,), daemon=True)
                self._thread = thread
                thread.start()
            except Exception as exc:
                self._thread = None
                self._events.put((
                    "error", "Watch connection worker could not start: "
                    + str(exc)))
                return False
            return True

    def disconnect(self) -> None:
        """Disconnect from watch."""
        self._stop_event.set()
        self._agent_stop_event.set()
        self._pin_requested = False
        self._pin_value = None
        self._expected_confirmation_passkey = None
        self._pin_event.set()
        loop = getattr(self, "_glib_loop", None)
        if loop is not None:
            try:
                loop.quit()
            except Exception:
                pass
        self._wake_pin_wait_loop()
        self.connected = False
        self.device_name = ""

    def close(self, timeout: float = 3.0) -> bool:
        """Cancel scan/pair/connect work and wait briefly for lease cleanup."""
        self.disconnect()
        if (self._pending_pairing_lease_release is not None
                and not (self._agent_cleanup_thread
                         and self._agent_cleanup_thread.is_alive())):
            self._schedule_agent_cleanup(
                self._agent_thread, self._pending_pairing_lease_release)
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            current = threading.current_thread()
            workers = []
            for thread in (self._thread, self._agent_thread,
                           self._agent_cleanup_thread):
                if (thread is not None and thread is not current
                        and thread.is_alive() and thread not in workers):
                    workers.append(thread)
            if not workers:
                pairing_released = (
                    self._pending_pairing_lease_release is None)
                scan_released = self._release_scan_lease_once()
                return pairing_released and scan_released
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            for thread in workers:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def forget(self) -> None:
        """Disconnect and remove BlueZ bonding for the watch."""
        address = self.device_address
        self.disconnect()
        if not address:
            return
        # Remove bonding via bluetoothctl
        try:
            import subprocess
            subprocess.run(
                ["bluetoothctl", "remove", address],
                capture_output=True, timeout=5)
            self._events.put(("status", f"Removed bonding for {address}"))
        except Exception as e:
            self._events.put(("error", f"Forget failed: {e}"))
        self.device_address = ""

    def provide_pin(self, pin: int) -> None:
        """Provide the 6-digit PIN shown on the watch."""
        self._pin_value = pin
        self._pin_event.set()
        self._wake_pin_wait_loop()

    def _wake_pin_wait_loop(self) -> None:
        loop = self._pin_wait_loop
        if loop is not None:
            try:
                loop.quit()
            except Exception:
                pass

    def _validate_pairing_device(self, device) -> None:
        """Accept agent requests only for the watch selected by the user."""
        path = str(device)
        if (not self._pairing_device_suffix
                or not path.endswith(self._pairing_device_suffix)):
            raise PairingAgentRequestError(
                "Pairing request is for another device",
                "org.bluez.Error.Rejected")
        if (self._stop_event.is_set()
                or self._agent_stop_event.is_set()):
            raise PairingAgentRequestError(
                "Pairing was cancelled", "org.bluez.Error.Canceled")

    def _request_pairing_pin(self, device, timeout: float = 60.0) -> int:
        """Wait for a UI-supplied PIN or fail closed on timeout/cancel."""
        self._begin_pairing_pin(device)
        self._pin_event.wait(timeout=max(0.0, float(timeout)))
        return self._finish_pairing_pin()

    def _begin_pairing_pin(
            self, device, *, expected_passkey: int | None = None) -> None:
        """Initialize one exact-device PIN request."""
        self._validate_pairing_device(device)
        self._pin_value = None
        self._expected_confirmation_passkey = expected_passkey
        self._pin_requested = True
        self._pin_event.clear()
        self._events.put(("pin_request", str(device)))

    def _finish_pairing_pin(self) -> int:
        """Finish a PIN request after the D-Bus-aware wait has completed."""
        supplied = self._pin_event.is_set()
        self._pin_requested = False
        pin = self._pin_value
        expected = self._expected_confirmation_passkey
        self._expected_confirmation_passkey = None
        if (not supplied or pin is None
                or self._stop_event.is_set()
                or self._agent_stop_event.is_set()):
            self._pin_value = None
            raise PairingAgentRequestError(
                "No passkey was supplied", "org.bluez.Error.Canceled")
        if expected is not None and int(pin) != int(expected):
            self._pin_value = None
            raise PairingAgentRequestError(
                "Passkey did not match the watch",
                "org.bluez.Error.Rejected")
        return int(pin)

    def _request_pairing_pin_with_glib(self, device, glib) -> int:
        """Wait for a passkey while continuing to dispatch BlueZ callbacks."""
        self._begin_pairing_pin(device)
        pin_loop = glib.MainLoop()
        self._pin_wait_loop = pin_loop

        def check_pin():
            if (self._pin_event.is_set()
                    or self._stop_event.is_set()
                    or self._agent_stop_event.is_set()):
                pin_loop.quit()
                return False
            return True

        glib.timeout_add(100, check_pin)
        try:
            pin_loop.run()
            return self._finish_pairing_pin()
        finally:
            self._pin_wait_loop = None

    def _confirm_pairing_passkey_with_glib(
            self, device, passkey: int, glib) -> None:
        """Require the user to enter the number displayed by the watch."""
        self._begin_pairing_pin(
            device, expected_passkey=int(passkey))
        pin_loop = glib.MainLoop()
        self._pin_wait_loop = pin_loop

        def check_pin():
            if (self._pin_event.is_set()
                    or self._stop_event.is_set()
                    or self._agent_stop_event.is_set()):
                pin_loop.quit()
                return False
            return True

        glib.timeout_add(100, check_pin)
        try:
            pin_loop.run()
            self._finish_pairing_pin()
        finally:
            self._pin_wait_loop = None

    def _agent_released(self) -> None:
        """Record that BlueZ released the agent and wake all waiters."""
        self._agent_registered = False
        self._agent_cleanup_confirmed.set()
        self._agent_stop_event.set()
        self._pin_requested = False
        self._pin_value = None
        self._expected_confirmation_passkey = None
        self._pin_event.set()
        self._wake_pin_wait_loop()
        loop = getattr(self, "_glib_loop", None)
        if loop is not None:
            try:
                loop.quit()
            except Exception:
                pass

    def _agent_cancelled(self) -> None:
        """Cancel a pending PIN prompt without manufacturing a passkey."""
        self._pin_requested = False
        self._pin_value = None
        self._expected_confirmation_passkey = None
        self._pin_event.set()
        self._wake_pin_wait_loop()

    def send_command(self, cmd: str, params: dict = None) -> None:
        """Queue a JSON command for the watch."""
        if not self.connected:
            self._events.put(("error", "Not connected to watch"))
            return
        msg = {"cmd": cmd}
        if params:
            msg["params"] = params
        self._tx_queue.put(json.dumps(msg) + "\n")

    def poll_events(self) -> list:
        """Drain event queue. Returns list of (type, data) tuples."""
        events = []
        while not self._events.empty():
            try:
                events.append(self._events.get_nowait())
            except Exception:
                break
        return events

    @property
    def scan_results(self) -> list:
        return list(self._scan_results)

    @property
    def pin_requested(self) -> bool:
        return self._pin_requested

    @property
    def worker_active(self) -> bool:
        return bool(
            (self._thread and self._thread.is_alive())
            or (self._agent_thread and self._agent_thread.is_alive())
            or (self._agent_cleanup_thread
                and self._agent_cleanup_thread.is_alive()))

    @property
    def pairing_lease_release_pending(self) -> bool:
        """Whether daemon agent ownership has not been released definitively."""
        with self._state_lock:
            return self._pending_pairing_lease_release is not None

    @property
    def scan_lease_release_pending(self) -> bool:
        """Whether the daemon's fixed watch scan owner remains unresolved."""
        with self._state_lock:
            return self._pending_scan_lease_release is not None

    def _retain_pairing_lease_release(self, release_lease) -> None:
        if release_lease is None:
            return
        with self._state_lock:
            self._pending_pairing_lease_release = release_lease

    def _release_pairing_lease_once(self, release_lease=None) -> bool:
        """Release once, retaining the callback when the result is uncertain."""
        with self._state_lock:
            callback = release_lease or self._pending_pairing_lease_release
        if callback is None:
            return True
        try:
            # Older integrations returned None.  Only an explicit False means
            # the daemon reported that ownership is still unresolved.
            released = callback() is not False
        except Exception as exc:
            released = False
            self._events.put((
                "log", f"[Watch] pairing lease release: {exc}"))
        with self._state_lock:
            if released and self._pending_pairing_lease_release is callback:
                self._pending_pairing_lease_release = None
            elif not released:
                self._pending_pairing_lease_release = callback
        return released

    def _take_scan_lease(self) -> tuple[bool, str]:
        if self._scan_lease_acquire is None:
            return True, ""
        if not self._release_scan_lease_once():
            return False, (
                "Previous watch BLE scan lease release is unresolved; "
                "retry after cleanup completes")
        result = self._scan_lease_acquire(20)
        if isinstance(result, bool):
            allowed = result
            reason = ("" if result else
                      "Meshtastic phone has Bluetooth priority")
        else:
            allowed, reason = result
            allowed = bool(allowed)
            reason = str(reason or "")
        if allowed and self._scan_lease_release is not None:
            with self._state_lock:
                self._pending_scan_lease_release = self._scan_lease_release
        return bool(allowed), str(reason or "")

    def _release_scan_lease_once(self) -> bool:
        """Release the scan owner once and retain uncertainty for retry."""
        with self._state_lock:
            callback = self._pending_scan_lease_release
        if callback is None:
            return True
        try:
            released = callback() is not False
        except Exception as exc:
            released = False
            self._events.put(("log", f"[Watch] scan lease release: {exc}"))
        with self._state_lock:
            if (released
                    and self._pending_scan_lease_release is callback):
                self._pending_scan_lease_release = None
            elif not released:
                self._pending_scan_lease_release = callback
        if not released:
            self._events.put((
                "error", "Meshtastic watch scan lease release was not "
                "confirmed; new Bluetooth work is blocked until retry"))
        return released

    def _drop_scan_lease(self) -> bool:
        """Compatibility wrapper for the scan/connect cleanup paths."""
        return self._release_scan_lease_once()

    # ------------------------------------------------------------------
    # BLE scan
    # ------------------------------------------------------------------
    def _run_scan(self):
        loop = None
        try:
            # BlueZ discovery and stale-session cleanup can each block for
            # seconds. Keep the Pyxel input thread responsive by doing the
            # complete preflight here.
            existing = self.check_existing()
            if self._stop_event.is_set():
                return

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            if existing:
                with self._state_lock:
                    self.device_address = existing
                result = loop.run_until_complete(self._run_bluetoothctl(
                    "disconnect", existing, timeout=5))
                if result is None or self._stop_event.wait(3.0):
                    return
                self._events.put((
                    "log", "[Watch] Cleared stale connection, reconnecting..."))
                self.scanning = False
                self._loop = loop
                loop.run_until_complete(self._async_connect(existing))
            else:
                loop.run_until_complete(self._async_scan())
        except Exception as e:
            self._events.put(("error", f"Scan failed: {e}"))
        finally:
            self.scanning = False
            self._loop = None
            if loop is not None:
                loop.close()

    async def _async_scan(self):
        try:
            from bleak import BleakScanner
        except ImportError:
            self._events.put(("error", "bleak not installed"))
            return

        self._events.put(("status", "Scanning for PipBoy (10s)..."))
        lease_held = False
        try:
            allowed, reason = self._take_scan_lease()
            if not allowed:
                self._events.put((
                    "error", "BLE scan paused: " + (
                        reason or "Meshtastic phone has Bluetooth priority")))
                return
            lease_held = self._scan_lease_acquire is not None
            if self._stop_event.is_set():
                return
            devices = await BleakScanner.discover(
                timeout=SCAN_TIMEOUT, return_adv=True)
        except Exception as e:
            self._events.put(("error", f"BLE scan error: {e}"))
            return
        finally:
            if lease_held:
                self._drop_scan_lease()

        total = len(devices)
        self._events.put(("log", f"[Watch] BLE scan: {total} devices total"))

        for dev, adv in devices.values():
            name = dev.name or adv.local_name or ""
            # Log all named devices for diagnostics
            if name:
                self._events.put(("log",
                    f"[Watch]   {name} [{dev.address}] "
                    f"RSSI:{adv.rssi}"))
            if name.startswith("PipBoy"):
                self._scan_results.append({
                    "name": name,
                    "address": dev.address,
                    "rssi": adv.rssi,
                })
                self._events.put(("device", {
                    "name": name, "address": dev.address,
                    "rssi": adv.rssi}))

        if not self._scan_results:
            self._events.put(("status",
                f"No PipBoy found ({total} other devices seen)"))
        else:
            n = len(self._scan_results)
            self._events.put(("status", f"Found {n} PipBoy device(s)"))

    # ------------------------------------------------------------------
    # BLE connect + pair + NUS
    # ------------------------------------------------------------------
    def _run_connect(self, address: str):
        loop = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            loop.run_until_complete(self._async_connect(address))
        except Exception as e:
            self._events.put(("error", f"Connection failed: {e}"))
            self.connected = False
        finally:
            self._loop = None
            if loop is not None:
                loop.close()

    async def _async_connect(self, address: str):
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError:
            self._events.put(("error", "bleak not installed"))
            return

        pairing_lease_acquired = False
        scan_lease_held = False
        agent_thread = None
        agent_thread_started = False
        agent_stopped = True
        proceed = True
        dev = None
        self._agent_stop_event.clear()
        self._agent_cleanup_confirmed.clear()
        self._agent_ready.clear()
        self._agent_registered = False
        self._pairing_device_suffix = (
            "/dev_" + address.upper().replace(":", "_"))
        self._pin_value = None
        try:
            already_bonded = self._is_paired_and_trusted(address)
            if proceed:
                allowed, reason = self._take_scan_lease()
                if not allowed:
                    self._events.put((
                        "error", "BLE scan paused: " + (
                            reason or "Meshtastic phone has Bluetooth priority")))
                    proceed = False
                else:
                    scan_lease_held = self._scan_lease_acquire is not None

            if self._stop_event.is_set():
                proceed = False

            if proceed:
                # Discover device first (populates D-Bus cache). The scan lease
                # is released before pairing or opening the GATT connection.
                self._events.put(("status", f"Scanning for {address}..."))
                try:
                    dev = await BleakScanner.find_device_by_address(
                        address, timeout=10)
                finally:
                    if scan_lease_held:
                        if not self._drop_scan_lease():
                            proceed = False
                        scan_lease_held = False
                if proceed and not dev:
                    self._events.put((
                        "error", f"Device {address} not found in scan"))
                    proceed = False
                if self._stop_event.is_set():
                    proceed = False

            # Acquire the agent lease only after discovery.  This leaves the
            # full 120-second window for agent registration, the PIN prompt,
            # pairing, trust, and verification instead of spending ten seconds
            # of it on an unrelated BLE scan.
            if self._stop_event.is_set():
                proceed = False
            if (proceed and not already_bonded
                    and self._pairing_lease_acquire is not None):
                pairing_lease_acquired = bool(
                    self._pairing_lease_acquire(120))
                if not pairing_lease_acquired:
                    self._events.put((
                        "error",
                        "Meshtastic phone pairing owns the BlueZ agent; "
                        "close its pairing window and retry"))
                    proceed = False
                else:
                    self._retain_pairing_lease_release(
                        self._pairing_lease_release)

            if self._stop_event.is_set():
                proceed = False
            if proceed and not already_bonded:
                # Watch firmware >= v0.4 requires MITM-authenticated
                # encryption. Register WDG's agent only for a new bond. A
                # reconnect to a paired/trusted watch never competes with the
                # Meshtastic phone agent.
                self._agent_generation += 1
                self._agent_path = (
                    f"/watchdogs/pipboy_agent_{self._agent_generation:x}")
                try:
                    agent_thread = self._agent_thread_factory(
                        target=self._run_dbus_agent, daemon=True)
                    self._agent_thread = agent_thread
                    agent_thread.start()
                    agent_thread_started = True
                except Exception as exc:
                    try:
                        agent_thread_started = bool(
                            agent_thread is not None
                            and agent_thread.is_alive())
                    except Exception:
                        agent_thread_started = False
                    if not agent_thread_started:
                        self._agent_thread = None
                        agent_thread = None
                        self._agent_cleanup_confirmed.set()
                    self._events.put((
                        "error", "Watch pairing agent could not start: "
                        + str(exc)))
                    proceed = False
                for _ in range(30):
                    if not proceed:
                        break
                    if self._agent_ready.is_set():
                        break
                    await asyncio.sleep(0.1)
                if (not self._agent_ready.is_set()
                        or not self._agent_registered):
                    self._events.put((
                        "error", "Watch pairing agent could not be registered"))
                    proceed = False

            # Explicit pairing keeps the PIN prompt inside this bounded agent
            # lease and marks the watch trusted before the GATT session.
            if self._stop_event.is_set():
                proceed = False
            if proceed and not already_bonded:
                proceed = await self._ensure_paired(
                    address,
                    renew_lease=(self._pairing_lease_acquire
                                 if pairing_lease_acquired else None))
            if self._stop_event.is_set():
                proceed = False
        finally:
            if scan_lease_held:
                self._drop_scan_lease()
            self._agent_stop_event.set()
            self._pin_requested = False
            self._pin_event.set()
            loop = getattr(self, "_glib_loop", None)
            if loop is not None:
                try:
                    loop.quit()
                except Exception:
                    pass
            if agent_thread is not None and agent_thread_started:
                agent_thread.join(timeout=3.0)
                agent_stopped = (
                    not agent_thread.is_alive()
                    and self._agent_cleanup_confirmed.is_set())
                if not agent_thread.is_alive():
                    with self._state_lock:
                        if self._agent_thread is agent_thread:
                            self._agent_thread = None
                if not agent_stopped:
                    self._events.put((
                        "error", "Watch pairing agent cleanup was not "
                        "confirmed; "
                        "Meshtastic agent lease retained"))
                    if pairing_lease_acquired:
                        self._schedule_agent_cleanup(
                            agent_thread, self._pairing_lease_release)
                        pairing_lease_acquired = False
                    proceed = False
            if (pairing_lease_acquired and agent_stopped
                    and self._pairing_lease_release is not None):
                if not self._release_pairing_lease_once(
                        self._pairing_lease_release):
                    self._events.put((
                        "error", "Meshtastic pairing-agent lease release was "
                        "not confirmed; retrying in the background"))
                    self._schedule_agent_cleanup(
                        agent_thread, self._pairing_lease_release)
                    proceed = False

        if not proceed or dev is None or self._stop_event.is_set():
            return
        self._events.put(("status", f"Connecting to {dev.name or address}..."))

        def on_disconnect(_client):
            self.connected = False
            self._events.put(("disconnected", address))

        async with BleakClient(
            dev,
            timeout=CONNECT_TIMEOUT,
            disconnected_callback=on_disconnect,
        ) as client:
            self._client = client
            self.connected = True
            self.device_name = client.address

            # Try to read device name
            for svc in client.services:
                for char in svc.characteristics:
                    if "2a00" in str(char.uuid).lower():
                        try:
                            name_bytes = await client.read_gatt_char(char)
                            self.device_name = name_bytes.decode(
                                "utf-8", errors="replace")
                        except Exception:
                            pass

            self._events.put(("connected", self.device_name))

            # Subscribe to NUS TX (watch → game)
            await client.start_notify(NUS_TX_CHAR, self._on_nus_notify)

            # Request initial status
            await self._nus_write(client, '{"cmd":"version"}\n')
            await self._nus_write(client, '{"cmd":"status"}\n')

            # Main loop: send queued commands, keepalive every 30s
            last_keepalive = time.time()
            while not self._stop_event.is_set() and client.is_connected:
                # Process TX queue
                while not self._tx_queue.empty():
                    try:
                        msg = self._tx_queue.get_nowait()
                        await self._nus_write(client, msg)
                        last_keepalive = time.time()
                    except Exception as e:
                        self._events.put(("error", f"TX: {e}"))

                # Keepalive: send status every 30s (resets 60s watchdog on watch)
                if time.time() - last_keepalive >= 30:
                    try:
                        await self._nus_write(client, '{"cmd":"status"}\n')
                        last_keepalive = time.time()
                    except Exception:
                        pass

                await asyncio.sleep(0.1)

            self._client = None
            self.connected = False

    def _schedule_agent_cleanup(self, agent_thread, release_lease):
        """Retry bounded agent teardown and release its daemon lease safely."""
        self._retain_pairing_lease_release(release_lease)
        existing = self._agent_cleanup_thread
        if existing is not None and existing.is_alive():
            return existing

        def cleanup():
            original_thread = agent_thread
            tracked_thread = agent_thread
            try:
                for _ in range(10):
                    self._agent_stop_event.set()
                    self._pin_event.set()
                    self._wake_pin_wait_loop()
                    loop = getattr(self, "_glib_loop", None)
                    if loop is not None:
                        try:
                            loop.quit()
                        except Exception:
                            pass
                    if tracked_thread is None:
                        break
                    try:
                        if not tracked_thread.is_alive():
                            break
                        tracked_thread.join(timeout=0.5)
                    except RuntimeError:
                        # Thread construction succeeded but start did not. No
                        # agent callback can still be active in that case.
                        tracked_thread = None
                        self._agent_cleanup_confirmed.set()
                        break

                confirmed = (
                    (tracked_thread is None or not tracked_thread.is_alive())
                    and self._agent_cleanup_confirmed.is_set())
                if confirmed and release_lease is not None:
                    released = False
                    for _ in range(10):
                        if self._release_pairing_lease_once(release_lease):
                            released = True
                            break
                        time.sleep(0.25)
                    if not released:
                        self._events.put((
                            "error", "Meshtastic pairing-agent lease remains "
                            "unreleased; retry the watch connection after the "
                            "WDG socket reconnects"))
                elif not confirmed:
                    self._events.put((
                        "error", "Watch pairing agent is still active; restart "
                        "WatchDogsGo before another pairing attempt"))
            finally:
                with self._state_lock:
                    if ((tracked_thread is None
                         or not tracked_thread.is_alive())
                            and self._agent_thread is original_thread):
                        self._agent_thread = None
                    if self._agent_cleanup_thread is threading.current_thread():
                        self._agent_cleanup_thread = None

        try:
            thread = self._agent_cleanup_thread_factory(
                target=cleanup, name="watch-agent-cleanup", daemon=True)
            self._agent_cleanup_thread = thread
            thread.start()
        except Exception as exc:
            self._agent_cleanup_thread = None
            self._events.put((
                "error", "Could not start watch agent cleanup; Meshtastic "
                "agent lease remains retained: " + str(exc)))
            return None
        return thread

    @staticmethod
    def _is_paired_and_trusted(address: str) -> bool:
        """Return whether BlueZ can reconnect without a pairing agent."""
        import subprocess
        try:
            info = subprocess.run(
                ["bluetoothctl", "info", address],
                capture_output=True, text=True, timeout=5).stdout
        except Exception:
            return False
        return "Paired: yes" in info and "Trusted: yes" in info

    async def _nus_write(self, client, data: str):
        """Write data to NUS RX characteristic, chunked by MTU."""
        raw = data.encode("utf-8")
        try:
            rx_char = client.services.get_characteristic(NUS_RX_CHAR)
            mtu = rx_char.max_write_without_response_size
        except Exception:
            mtu = 20

        for i in range(0, len(raw), mtu):
            chunk = raw[i:i + mtu]
            await client.write_gatt_char(NUS_RX_CHAR, chunk, response=False)

    def _on_nus_notify(self, _sender, data: bytearray):
        """Handle incoming NUS data (chunked, reassemble on \\n)."""
        self._rx_buffer += data.decode("utf-8", errors="replace")
        while "\n" in self._rx_buffer:
            line, self._rx_buffer = self._rx_buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                self._handle_message(msg)
            except json.JSONDecodeError:
                self._events.put(("log", f"[Watch] {line}"))

    def _handle_message(self, msg: dict):
        """Route parsed JSON message from watch."""
        # Version response
        if "version" in msg:
            self.version = msg["version"]
            self.features = msg.get("features", [])
            self._events.put(("version", msg))

        # Status response
        elif "bat" in msg:
            self.battery = msg.get("bat", 0)
            self._events.put(("status_data", msg))

        # Event: NFC tag scanned
        elif msg.get("event") == "nfc_tag":
            self._events.put(("nfc_tag", msg))

        # Event: LoRa message
        elif msg.get("event") == "lora_msg":
            self._events.put(("lora_msg", msg))

        # Compass response
        elif "heading" in msg and "roll" in msg:
            self._events.put(("compass", msg))

        # Event: Evil Twin credential
        elif msg.get("event") == "et_cred":
            self._events.put(("et_cred", msg))

        # Event: deauth detected (TSCM)
        elif msg.get("event") == "deauth_detected":
            self._events.put(("deauth_detected", msg))

        # NFC file download
        elif msg.get("type") == "nfc_file":
            self._events.put(("nfc_file", msg))

        # NFC tag list
        elif "tags" in msg:
            self._events.put(("nfc_list", msg))

        # Recon results (WiFi + BLE)
        elif "wifi" in msg or "ble" in msg:
            self._events.put(("recon", msg))

        # LoRa message history
        elif "messages" in msg:
            self._events.put(("lora_history", msg))

        # Generic OK/error
        elif "ok" in msg:
            self._events.put(("ack", msg))

        else:
            self._events.put(("log", f"[Watch] {json.dumps(msg)}"))

    # ------------------------------------------------------------------
    # Pairing helper — explicit bluetoothctl pair/trust before GATT
    # ------------------------------------------------------------------
    async def _run_bluetoothctl(self, *args: str, timeout: float):
        """Run bluetoothctl while remaining responsive to disconnect()."""
        import subprocess

        if self._stop_event.is_set() or self._agent_stop_event.is_set():
            return None
        try:
            process = subprocess.Popen(
                ["bluetoothctl", *args],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except Exception as exc:
            self._events.put((
                "log", f"[Watch] bluetoothctl {' '.join(args)}: {exc}"))
            return (-1, "", str(exc))

        deadline = asyncio.get_running_loop().time() + max(
            0.0, float(timeout))
        cancelled = False
        while process.poll() is None:
            if (self._stop_event.is_set()
                    or self._agent_stop_event.is_set()
                    or asyncio.get_running_loop().time() >= deadline):
                cancelled = True
                try:
                    process.terminate()
                    await asyncio.sleep(0.1)
                    if process.poll() is None:
                        process.kill()
                except Exception:
                    pass
                break
            await asyncio.sleep(0.1)

        try:
            stdout, stderr = process.communicate(timeout=1)
        except Exception:
            try:
                process.kill()
                stdout, stderr = process.communicate(timeout=1)
            except Exception:
                stdout, stderr = "", ""
        if cancelled:
            return None
        return (int(process.returncode or 0), stdout or "", stderr or "")

    async def _ensure_paired(self, address: str, *, renew_lease=None) -> bool:
        """If the watch isn't paired + trusted yet, drive `bluetoothctl pair`
        so our D-Bus agent gets the PIN prompt, then mark trusted."""
        import subprocess
        if self._stop_event.is_set() or self._agent_stop_event.is_set():
            self._events.put(("status", "Watch pairing cancelled"))
            return False
        try:
            info = subprocess.run(
                ["bluetoothctl", "info", address],
                capture_output=True, text=True, timeout=5).stdout
        except Exception:
            self._events.put((
                "error", "bluetoothctl is required to verify watch pairing"))
            return False
        paired = "Paired: yes" in info
        trusted = "Trusted: yes" in info
        if paired and trusted:
            return True  # already bonded; subsequent connect is silent

        self._events.put(("status", "Pairing — check the watch for PIN"))
        paired_result = await self._run_bluetoothctl(
            "pair", address, timeout=90)
        if paired_result is None:
            self._events.put(("status", "Watch pairing cancelled"))
            return False
        returncode, stdout, stderr = paired_result
        if returncode:
            detail = (stderr or stdout or "pairing failed").strip()
            self._events.put((
                "log", f"[Watch] pair command: {detail[:160]}"))
        if self._stop_event.is_set() or self._agent_stop_event.is_set():
            self._events.put(("status", "Watch pairing cancelled"))
            return False
        # Pairing can wait up to 90 seconds for user input. Renew before the
        # trust and verification commands so the daemon never reinstalls its
        # own default agent while WDG's agent is still registered.
        if renew_lease is not None:
            try:
                if not renew_lease(120):
                    self._events.put((
                        "error", "Watch pairing-agent lease expired before "
                        "bond verification"))
                    return False
            except Exception as exc:
                self._events.put((
                    "error", "Could not renew watch pairing-agent lease: "
                    + str(exc)[:120]))
                return False
        if self._stop_event.is_set() or self._agent_stop_event.is_set():
            self._events.put(("status", "Watch pairing cancelled"))
            return False
        # Mark trusted so BlueZ skips re-auth prompts on future connects.
        trust_result = await self._run_bluetoothctl(
            "trust", address, timeout=10)
        if trust_result is None:
            self._events.put(("status", "Watch pairing cancelled"))
            return False
        returncode, stdout, stderr = trust_result
        if returncode:
            detail = (stderr or stdout or "trust failed").strip()
            self._events.put((
                "log", f"[Watch] trust command: {detail[:160]}"))
        if self._stop_event.is_set() or self._agent_stop_event.is_set():
            self._events.put(("status", "Watch pairing cancelled"))
            return False
        if not self._is_paired_and_trusted(address):
            self._events.put((
                "error", "Watch pairing was not confirmed; GATT connection "
                "was not attempted"))
            return False
        return True

    # ------------------------------------------------------------------
    # D-Bus Agent for PIN pairing
    # ------------------------------------------------------------------
    def _run_dbus_agent(self):
        """Register BlueZ Agent1 for passkey pairing (runs GLib mainloop)."""
        try:
            import dbus
            import dbus.service
            import dbus.mainloop.glib
            from gi.repository import GLib
        except ImportError:
            self._events.put((
                "log", "[Watch] dbus/GLib not available — manual pair needed"))
            self._agent_cleanup_confirmed.set()
            self._agent_ready.set()
            return

        try:
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            bus = dbus.SystemBus()
        except Exception as exc:
            self._events.put((
                "log", f"[Watch] D-Bus agent setup failed: {exc}"))
            self._agent_registered = False
            self._agent_cleanup_confirmed.set()
            self._agent_ready.set()
            return
        manager = self

        class PipBoyAgent(dbus.service.Object):
            AGENT_PATH = manager._agent_path

            def __init__(self):
                super().__init__(bus, self.AGENT_PATH)

            @staticmethod
            def _check_device(device):
                try:
                    manager._validate_pairing_device(device)
                except PairingAgentRequestError as exc:
                    raise dbus.exceptions.DBusException(
                        str(exc), name=exc.dbus_error_name) from exc

            @classmethod
            def _request_pin(cls, device):
                try:
                    return manager._request_pairing_pin_with_glib(
                        device, GLib)
                except PairingAgentRequestError as exc:
                    raise dbus.exceptions.DBusException(
                        str(exc), name=exc.dbus_error_name) from exc

            @dbus.service.method("org.bluez.Agent1",
                                 in_signature="o", out_signature="u")
            def RequestPasskey(self, device):
                return dbus.UInt32(self._request_pin(device))

            @dbus.service.method("org.bluez.Agent1",
                                 in_signature="ouq", out_signature="")
            def DisplayPasskey(self, device, passkey, entered):
                self._check_device(device)
                manager._events.put((
                    "log", f"[Watch] Passkey: {passkey:06d}"))

            @dbus.service.method("org.bluez.Agent1",
                                 in_signature="ou", out_signature="")
            def RequestConfirmation(self, device, passkey):
                try:
                    manager._confirm_pairing_passkey_with_glib(
                        device, int(passkey), GLib)
                except PairingAgentRequestError as exc:
                    raise dbus.exceptions.DBusException(
                        str(exc), name=exc.dbus_error_name) from exc

            @dbus.service.method("org.bluez.Agent1",
                                 in_signature="o", out_signature="s")
            def RequestPinCode(self, device):
                return str(self._request_pin(device)).zfill(6)

            @dbus.service.method("org.bluez.Agent1",
                                 in_signature="", out_signature="")
            def Release(self):
                manager._agent_released()

            @dbus.service.method("org.bluez.Agent1",
                                 in_signature="", out_signature="")
            def Cancel(self):
                manager._agent_cancelled()

        registered = False
        try:
            agent = PipBoyAgent()
            agent_mgr = dbus.Interface(
                bus.get_object("org.bluez", "/org/bluez"),
                "org.bluez.AgentManager1")
            agent_mgr.RegisterAgent(PipBoyAgent.AGENT_PATH, "KeyboardDisplay")
            registered = True
            self._agent_registered = True
            agent_mgr.RequestDefaultAgent(PipBoyAgent.AGENT_PATH)
            self._events.put(("log", "[Watch] BLE agent registered"))
            self._agent_ready.set()
        except Exception as e:
            self._events.put(("log", f"[Watch] Agent error: {e}"))
            if registered:
                try:
                    agent_mgr.UnregisterAgent(PipBoyAgent.AGENT_PATH)
                except Exception as cleanup_exc:
                    self._events.put((
                        "error", "Watch pairing agent unregister failed: "
                        + str(cleanup_exc)[:120]))
                else:
                    self._agent_cleanup_confirmed.set()
            else:
                self._agent_cleanup_confirmed.set()
            self._agent_registered = False
            self._agent_ready.set()
            return

        loop = GLib.MainLoop()
        self._glib_loop = loop

        # Run until stop
        def check_stop():
            if (self._stop_event.is_set()
                    or self._agent_stop_event.is_set()):
                loop.quit()
                return False
            return True

        GLib.timeout_add(1000, check_stop)
        try:
            loop.run()
        except Exception:
            pass
        finally:
            if self._agent_registered:
                try:
                    agent_mgr.UnregisterAgent(PipBoyAgent.AGENT_PATH)
                except Exception as exc:
                    self._events.put((
                        "error", "Watch pairing agent unregister failed: "
                        + str(exc)[:120]))
                else:
                    self._agent_cleanup_confirmed.set()
            self._agent_registered = False
            self._glib_loop = None
