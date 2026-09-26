"""Wardrive integration kept outside the main game's rendering and menu code."""
from collections import OrderedDict, deque
from itertools import islice
import json
from pathlib import Path
from queue import Empty, Queue
import threading
import time

from .app_state import Network
from .wardrive_protocol import parse_record, display_bytes
from .scan_controller import ScanController
from .notable_detector import NotableDetector, ble_name
from .wardrive_trail import FixHistory, WardriveTrail, distance
from .lora_manager import (
    MC_DISCOVERY_INTERVAL, MC_DISCOVERY_MIN_DISTANCE_M,
)
from .meshtastic_manager import (
    MT_DISCOVERY_INTERVAL, MT_DISCOVERY_MIN_DISTANCE_M,
)
from .trail_layer import TrailLayer, heat_color
from .passive_capture import PassiveCapture
from .passive_screen import PassiveScreen
from .handshake_capture import HandshakeCapture, COMMANDS, capture_storage
from .handshake_targets import HandshakeTargets, parse_target_record, ERRORS
from .handshake_screen import HandshakeScreen
from .host_ble import HostBleScanner, list_ble_adapters
from .cell_monitor import HostCellScanner, find_unclean_cell_session
from .map_display import (
    MAP_LAYERS, MODE_OFF, MODE_KEEP, MODE_RECENT, color_for_record,
    cycle_mode, display_state, layer_label, mode_label,
)
from .wardrive_settings import (
    DEFAULTS, DOT_FADE_CHOICES, LORA_PROTOCOLS, MESHTASTIC_BACKENDS,
    TRAIL_MODES, load_settings, normalize_settings, save_settings,
    settings_path,
)

PURPLE, ORANGE, CYAN = 2, 9, 3
RADAR_TRAIL_POINT_LIMIT = 160

MAIN_SETTINGS = (
    ("flock", "Flock detection"),
    ("axon", "Axon detection"),
    ("precise", "Precise Flock/Axon markers"),
    ("_map_layers", "Map dot layers"),
    ("trail_mode", "Wardrive trail"),
    ("_collectors", "All Wardrive collectors"),
    ("_lora_settings", "LoRa settings"),
    ("lte_modem", "LTE modem integration"),
    ("cell_tracking", "Cell mast tracking"),
    ("cell_neighbors", "Experimental QMI neighbors"),
)

COLLECTOR_SETTINGS = (
    ("wardrive_lora", "Automatic LoRa collector"),
    ("wardrive_adsb", "ADS-B aircraft"),
    ("wardrive_433", "433 MHz sensors"),
)

LORA_SETTINGS = (
    ("lora_protocol", "LoRa protocol"),
    ("_meshcore_region", "MeshCore region"),
    ("_meshtastic", "Meshtastic service and phone BLE"),
)

class WardriveUI:
    def __init__(self, app, initial_settings=None):
        self.app = app
        self.scan = ScanController(app._send, time.monotonic)
        meshtastic = getattr(app, "_meshtastic", None)
        lease_acquire = (
            (lambda seconds, owner: meshtastic.acquire_ble_scan_lease(
                seconds, owner=owner))
            if meshtastic is not None
            and hasattr(meshtastic, "acquire_ble_scan_lease") else None)
        lease_release = (
            (lambda owner: meshtastic.release_ble_scan_lease(owner=owner))
            if meshtastic is not None
            and hasattr(meshtastic, "release_ble_scan_lease") else None)
        self.host_ble = HostBleScanner(
            lease_acquire=lease_acquire, lease_release=lease_release)
        self.cell = HostCellScanner(getattr(app.gps, "modem_broker", None))
        self.cell_candidates = deque(maxlen=128)
        self.cell_legacy_next = 0
        self.cell_unclean = find_unclean_cell_session(app._app_dir)
        self.cell_unclean_reported = False
        self.passive = PassiveCapture()
        self.hs_screen = PassiveScreen(app, self)
        self.capture = HandshakeCapture()
        self.targets = HandshakeTargets(time.monotonic)
        self.capture_screen = HandshakeScreen(app, self)
        self.capture_stop_ack = False
        self.detector = NotableDetector()
        self.fixes = FixHistory()
        self.trail = WardriveTrail()
        self._trail_layer = TrailLayer(640, 16, 218)
        self.history_trail = None
        self._map_trail_key = None
        self._map_trail_segments = []
        self._radar_trail_key = None
        self._radar_trail_segments = []
        self.history_notables = OrderedDict()
        self.history_index = -1
        self.settings_path = settings_path(app._app_dir)
        self.settings = (load_settings(app._app_dir) if initial_settings is None
                         else normalize_settings(initial_settings))
        self.settings_open = False
        self.settings_page = "main"
        self.selection = 0
        self.layer_selection = 0
        self.collector_selection = 0
        self.lora_selection = 0
        self.meshtastic_selection = 0
        self._meshtastic_action_results = Queue()
        self._meshtastic_action_thread = None
        self._host_ble_retry_pending = False
        self.details = False
        self.detail_selection = 0
        self.notables = OrderedDict()
        self._notable_identities = frozenset()
        self._notable_revision = 0
        self.alerts = deque(maxlen=16)
        self.alert_until = 0
        self.connection = None
        self.store_path = None
        self.invalid_records = 0
        self.last_error = ""
        self.last_probe_error = ""
        self.host_ble_batch_lines = deque(maxlen=256)
        self.mc_discovery_session = None
        self.mc_discovery_next = 0.0
        self.mc_discovery_last_fix = None
        lora = getattr(app, "_lora", None)
        self._wdg_owned_lora = bool(
            (lora is not None and lora.running and lora.mode == "meshcore")
            or (meshtastic is not None and meshtastic.running))
        self._wdg_owned_sdr = False

    def on_stop(self):
        self.targets.cancel()
        if self.app._pending_cmd and self.app._pending_cmd.startswith("hs_scan "):
            self.app._pending_cmd = None
        self.capture.stop()
        self.host_ble.stop()
        self.host_ble_batch_lines.clear()
        self.cell.stop()
        self.scan.stop()
        self.app._clear_scan_state()
        self.app._gps_wait = False
        self.app._gps_wait_cmd = ""
        self.app._gps_wait_dialog = False
        self.trail.break_segment()
        self.detector.clear()
        self.mc_discovery_session = None
        self.mc_discovery_next = 0.0
        self.mc_discovery_last_fix = None

    def tick(self):
        app = self.app
        while True:
            try:
                ok, label, detail, on_success = (
                    self._meshtastic_action_results.get_nowait())
            except Empty:
                break
            if ok and on_success is not None:
                try:
                    on_success()
                except Exception as exc:
                    ok = False
                    detail = "could not save result: " + str(exc)[:110]
            message = f"[MT] {label}: {detail}"
            app._term_add(message, raw=True)
            app.msg(message, CYAN if ok else ORANGE)
            if ok and label == "Shared adapter":
                self._host_ble_retry_pending = True
        action_thread = self._meshtastic_action_thread
        if action_thread is not None and not action_thread.is_alive():
            self._meshtastic_action_thread = None
        connection = app.serial if app.serial and app.serial.is_open else None
        if connection is not self.connection:
            self.host_ble.stop()
            self.host_ble_batch_lines.clear()
            self.cell.stop()
            self.connection = connection
            self.close_passive()
            self.capture.finish("disconnected")
            self.capture_stop_ack = False
            app.capturing_hs = False
            self.scan.reset()
            self.targets.disconnect()
            app._clear_scan_state()
            self.detector.clear()
            self.trail.break_segment()
            if connection:
                self.scan.probe()
        # Legacy stop acknowledgement may precede the no-SD base64 dump.
        # Resume transitions only after its cleanup line was fully processed.
        if self.capture_stop_ack and self.capture.current and not self.capture.current.active:
            self.capture_stop_ack = False
            self.handle_line("All operations stopped.")
        now = time.monotonic()
        self.fixes.update(app.gps.fix, now)
        self.scan.tick()
        if self.targets.tick():
            if app._pending_cmd and app._pending_cmd.startswith("hs_scan "):
                app._pending_cmd = None
            app._send("stop")
        run = self.capture.current
        if run and run.state == "starting" and time.monotonic()-run.started_at > 15:
            run.note = "No capture acknowledgement; stopped. Check firmware and retry."
            self.capture.finish("error")
            app.capturing_hs = False
            app._send("stop")
        self.poll_host_ble(now)
        self.poll_cell(now)
        self.poll_lora_discovery(now)
        if not self.scan.active and self.passive.file:
            self.close_passive()
        if self.cell_unclean and not self.cell_unclean_reported:
            self.cell_unclean_reported = True
            app._term_add("[CELL] Previous cell session ended uncleanly: "
                          + str(self.cell_unclean), raw=True)
        if self.scan.probe_error != self.last_probe_error:
            self.last_probe_error = self.scan.probe_error
            if self.last_probe_error:
                app.msg("[WDG] " + self.last_probe_error, 8)
                app._term_add("[WDG] " + self.last_probe_error, raw=True)
        if self.scan.error and self.scan.error != self.last_error:
            self.last_error = self.scan.error
            app.msg("[WDG] " + self.last_error, 8)
            app._term_add("[WDG] " + self.last_error, raw=True)
        session = Path(app.loot.session_path) if app.loot and app.loot.active else None
        path = session / "notable_detections.jsonl" if session else None
        if path != self.store_path:
            self.store_path = path
            self.notables.clear()
            self.alerts.clear()
            if path and path.exists():
                with path.open(encoding="utf-8") as f:
                    for line in f:
                        try:
                            item = json.loads(line)
                            item["fix_seen"] = 0
                            item["seen"] = 0  # restored observations are historical
                            self.notables[item["key"]] = item
                            if len(self.notables) > 256:
                                self.notables.popitem(last=False)
                        except (ValueError, KeyError, TypeError):
                            pass
            self._refresh_notable_identities()
            self.trail.set_path(session / "wardrive_trail.jsonl" if session else None)
            self._trail_layer.invalidate()
        active = app.wifi_scanning or app.ble_scanning or (self.scan.state == "running" and self.scan.mode == "wardrive")
        fix = self.fixes.at(now) if app.gps.available else None
        try:
            trail_enabled = self.trail_mode() != "off" and active
            self.trail.sample(
                fix, now, trail_enabled,
                density=self.trail.recent_unique_count(now))
        except OSError as exc:
            app.msg("[TRAIL] " + str(exc)[:70], 8)
        if self.alerts and now >= self.alert_until:
            if self.alert_until:
                self.alerts.popleft()
            self.alert_until = now + 5 if self.alerts else 0

    def handle_line(self, line):
        s = line.strip()
        if s.startswith("HST:"):
            d = parse_target_record(s)
            if d is None:
                self.targets.cancel("Malformed scan result. Press R to rescan.")
            elif d["kind"] == "capture_error":
                run = self.capture.current
                if run and run.state == "starting" and d["storage"] == self.capture.storage:
                    run.note = ERRORS[d["error"]]
                    self.capture.finish("error")
                    self.app.capturing_hs = False
                    self.app.msg(run.note, 8)
            else:
                self.targets.accept(d)
            return True
        if self.capture.handle(s):
            return True
        if self.capture.current and self.capture.current.state in ("stopped", "error", "disconnected"):
            self.app.capturing_hs = False
        if s.startswith("WDG:"):
            d = parse_record(s)
            if d is None:
                self.invalid_records += 1
                return True
            if self.app.loot:
                self.app.loot.log_serial(s)
            if d["kind"] == "capabilities":
                support = "supported" if d.get("wardrive_wifi_serial_v1") is True else "needs firmware 1.7.3+"
                self.app._term_add("[WDG] WiFi-only serial for host BLE: " + support, raw=True)
                protocol = "batched v2" if d.get("wardrive_batch_serial_v2") is True else "legacy stream"
                self.app._term_add("[WDG] All Wardrive transport: " + protocol, raw=True)
            previous = self.scan.state
            if self.scan.handle(d):
                if d["kind"] == "hs_packet":
                    try:
                        self.passive.accept(d)
                    except OSError as exc:
                        self.app.msg("[HS SNIFF] Storage error: " + str(exc)[:60], 8)
                        self.app._send("stop")
                        self.close_passive()
                else:
                    self.observation(d)
            if d["kind"] == "batch_start":
                self.app._term_add(f"[ALL] Background scan #{d['batch']} started (10s)", raw=True)
            elif d["kind"] == "batch_results":
                self.app._term_add(
                    f"[ALL] Results #{d['batch']}: WiFi {d['batch_wifi']} | BLE {d['batch_ble']}", raw=True)
                while self.host_ble_batch_lines:
                    self.app._term_add(self.host_ble_batch_lines.popleft(), raw=True)
            elif d["kind"] == "batch_done":
                self.app._term_add(f"[ALL] Batch #{d['batch']} complete; starting next scan", raw=True)
                if self.app.loot:
                    self.app.loot.checkpoint_scan_loot()
                if self.cell.active:
                    self.cell.observe_batch(self.fixes.at(time.monotonic()), d["batch"])
            if previous == "starting" and self.scan.state == "running" and self.scan.wifi_only:
                if not self.host_ble.start(
                        self.scan.session,
                        adapter=self.settings.get("host_ble_adapter", "auto"),
                        require_lease=self._host_ble_requires_lease()):
                    self.host_ble_error("Previous Bluetooth scan is still closing; retry shortly")
                else:
                    self.app._term_add("[ALL] Wi-Fi: ESP32 | BLE: uConsole (starting)", raw=True)
            if previous == "starting" and self.scan.state == "running":
                self.start_cell()
                self.start_auxiliary_collectors()
            if d["kind"] == "stopped" and not self.scan.active:
                self.host_ble.stop()
                self.cell.stop()
                self.close_passive()
            return True
        # Completion, not the early 'stop command received' message.
        if "all operations stopped" in s.lower() or "all stopped" in s.lower():
            app = self.app
            if self.capture.current and self.capture.current.active and self.capture.current.cleanup_pending:
                self.capture_stop_ack = True
                return True
            self.capture.finish()
            # v2 emits a structured stop first, but this global cleanup line is
            # an independent acknowledgement if USB congestion lost that frame.
            self.scan.accept_plain_stop()
            app._wl_stop_ack()
            if self.scan.state != "running":
                app.sniffing = app.capturing_hs = False
                app._bt_tracking = app._bt_airtag = False
                app.state.portal_running = app.state.evil_twin_running = False
            if app._pending_cmd and self.scan.active:
                return True  # wait for this session's structured stopped event first
            if app._pending_cmd:
                cmd, state, name = app._pending_cmd, app._pending_state, app._pending_cmd_name
                app._pending_cmd = None
                if state in ("all_wardrive", "all_wardrive_host", "hs_sniff"):
                    self.detector.clear()
                    if state == "hs_sniff":
                        if not self.app.loot or not self.app.loot.active:
                            app.msg("[HS SNIFF] No writable loot session; capture cancelled.", 8)
                            return True
                        try:
                            self.passive.open(Path(app.loot.session_path) / "handshakes")
                        except OSError as exc:
                            app.msg("[HS SNIFF] Cannot save capture: " + str(exc)[:60], 8)
                            return True
                    if not self.scan.start(
                            "hs_sniff" if state == "hs_sniff" else "wardrive",
                            wifi_only=state == "all_wardrive_host"):
                        self.close_passive()
                        app.msg("[WDG] Scan unavailable; check firmware/connection", 8)
                        return True
                    if state != "hs_sniff":
                        self.host_ble_batch_lines.clear()
                        self.cell_candidates.clear()
                    self.last_error = ""
                else:
                    if capture_storage(cmd):
                        self.capture.start(cmd)
                    elif state == "hs_target_scan" and not self.targets.dispatched(cmd):
                        return True
                    app._send(cmd)
                    app._set_running(state, True)
                    if state in ("bt_scanning", "ble_scan"):
                        app._bt_scan_start_time = time.time()
                app.msg("[START] " + name, CYAN)
                return True
            if self.scan.state == "running":
                return True  # delayed legacy text cannot stop a confirmed new session
        return False

    def host_ble_error(self, message, *, shared_failure=False):
        meshtastic = getattr(self.app, "_meshtastic", None)
        if (shared_failure and meshtastic is not None
                and hasattr(meshtastic, "note_host_ble_failure")):
            meshtastic.note_host_ble_failure(message)
        self.host_ble.state = "error"
        self.scan.error = "uConsole BLE: " + message[:150]
        self.app._term_add("[ALL] " + self.scan.error, raw=True)
        self.app._term_add("[ALL] Enable Bluetooth in the OS and check that bleak is installed.", raw=True)
        self.app.msg("[ALL] Host BLE unavailable; WiFi and other collectors continue", ORANGE)

    def _host_ble_requires_lease(self):
        """Use only daemon-confirmed policy to bypass scan coordination."""
        host = str(self.settings.get("host_ble_adapter", "auto")).upper()
        manager = getattr(self.app, "_meshtastic", None)
        checker = getattr(manager, "host_ble_requires_lease", None)
        if checker is None:
            return True
        return bool(checker(host))

    def poll_host_ble(self, now):
        active = self.scan.state == "running" and self.scan.wifi_only
        if not active:
            self.host_ble.stop()
            self._host_ble_retry_pending = False
        meshtastic = getattr(self.app, "_meshtastic", None)
        if (active and meshtastic is not None
                and getattr(meshtastic, "host_ble_degraded", False)
                and self._host_ble_requires_lease()):
            reason = (getattr(meshtastic, "host_ble_pause_reason", "")
                      or "Meshtastic phone priority")
            active = False
            already_paused = self.host_ble.state == "paused"
            self.host_ble.stop()
            self.host_ble.state = "paused"
            if not already_paused:
                self.app._term_add(
                    "[ALL] Host BLE paused: " + reason[:120], raw=True)
                self.app.msg(
                    "[ALL] Host BLE paused; Meshtastic phone priority", ORANGE)
        if (active and self._host_ble_retry_pending
                and not getattr(meshtastic, "host_ble_degraded", False)
                and not self.host_ble.worker_active):
            if self.host_ble.start(
                    self.scan.session,
                    adapter=self.settings.get("host_ble_adapter", "auto"),
                    require_lease=self._host_ble_requires_lease()):
                self._host_ble_retry_pending = False
                self.app._term_add(
                    "[ALL] Retrying uConsole BLE scan", raw=True)
        for session, kind, data in self.host_ble.poll():
            if not active or session != self.scan.session:
                continue
            if kind == "error":
                self.host_ble_error(data)
                active = False
            elif kind == "shared_error":
                self.host_ble_error(data, shared_failure=True)
                active = False
            elif kind == "paused":
                self.host_ble.state = "paused"
                self.app._term_add(
                    "[ALL] Host BLE paused: " + str(data)[:120], raw=True)
                self.app.msg(
                    "[ALL] Host BLE paused; Meshtastic phone priority",
                    ORANGE)
            elif kind == "started":
                self.host_ble.state = "running"
                self.app._term_add("[ALL] uConsole BLE scanning", raw=True)
            elif kind == "ble":
                received, record = data
                age = max(0, now - received)
                if age > 2:
                    self.host_ble.drops += 1
                    continue
                record["age_ms"] = int(age * 1000)
                self.scan.last_seen["ble"] = received
                # Host BLE must never renew the ESP32 heartbeat.
                if self.scan.batch:
                    name = ble_name(bytes.fromhex(record["data_hex"])) or "?"
                    self.host_ble_batch_lines.append(
                        f"[BLE]  {name[:16]:<16} {record['mac']} {record['rssi']}dBm [host]")
                self.observation(record, terminal=not self.scan.batch)

    def start_cell(self):
        """Start cell collection only after the ESP wardrive is acknowledged."""
        if (not self.settings["lte_modem"] or not self.settings["cell_tracking"]
                or self.scan.mode != "wardrive"
                or self.scan.state != "running"
                or not self.app.gps.available
                or self.cell.active or not self.app.loot or not self.app.loot.active):
            return False
        started = self.cell.start(
            self.scan.session, Path(self.app.loot.session_path),
            experimental_neighbors=self.settings["cell_neighbors"])
        if started:
            self.app._term_add("[CELL] ModemManager serving-cell tracking starting", raw=True)
            if self.settings["cell_neighbors"]:
                self.app._term_add("[CELL] Experimental QMI neighbors enabled (60s minimum)", raw=True)
        else:
            self.app._term_add("[CELL] " + (self.cell.error or "Cell tracking unavailable"), raw=True)
            self.app.msg("[CELL] Unavailable; WiFi/BLE still running", 8)
        return started

    def start_auxiliary_collectors(self):
        """Ensure enabled host radios are collecting for All Wardrive.

        LoRa and SDR are host-side add-ons, so they are deliberately started
        only after the ESP32 acknowledges a real wardrive session. Wardrive
        Settings controls whether WDG may claim each radio automatically;
        SYSTEM still controls hardware power. Collectors keep running when the
        map/menu is opened or the ESP scan is stopped.
        """
        app = self.app
        if (self.scan.mode != "wardrive" or self.scan.state != "running"):
            return False

        started = False
        protocol = self.settings["lora_protocol"]
        protocol_label = "MeshCore" if protocol == "meshcore" else "Meshtastic"
        lora = getattr(app, "_lora", None)
        meshtastic = getattr(app, "_meshtastic", None)
        if not self.settings["wardrive_lora"]:
            app._term_add(
                "[ALL] LoRa disabled (Wardrive Settings > Collectors)",
                raw=True)
        elif getattr(app, "_lora_enabled", False):
            if protocol == "meshtastic":
                if meshtastic is None:
                    app._term_add(
                        "[ALL] Meshtastic unavailable: client manager missing",
                        raw=True)
                elif meshtastic.running:
                    app._term_add(
                        "[ALL] Meshtastic collection active"
                        if meshtastic.connected else
                        "[ALL] Meshtastic connection starting", raw=True)
                else:
                    try:
                        switch = getattr(app, "_switch_lora_protocol", None)
                        if switch:
                            accepted = bool(switch(
                                "meshtastic", start_if_enabled=True,
                                _transition_action="wardrive"))
                        else:
                            accepted = bool(meshtastic.start())
                        if accepted:
                            started = True
                            app._term_add(
                                "[ALL] Meshtastic client starting via "
                                "meshtasticd", raw=True)
                        else:
                            app._term_add(
                                "[ALL] Meshtastic start was not accepted; "
                                "WiFi/BLE continue", raw=True)
                            app.msg(
                                "[ALL] Meshtastic unavailable; WiFi/BLE continue",
                                ORANGE)
                    except Exception as exc:
                        app._term_add(
                            "[ALL] Meshtastic start failed: " + str(exc)[:100],
                            raw=True)
                        app.msg(
                            "[ALL] Meshtastic unavailable; WiFi/BLE continue",
                            ORANGE)
            else:
                if lora is None:
                    app._term_add(
                        "[ALL] MeshCore unavailable: LoRa manager missing",
                        raw=True)
                elif lora.running and lora.mode == "meshcore":
                    app._term_add("[ALL] MeshCore collection active", raw=True)
                elif lora.running:
                    app._term_add(
                        f"[ALL] MeshCore skipped: LoRa busy in "
                        f"{lora.mode or 'another'} mode", raw=True)
                    app.msg("[ALL] MeshCore skipped; LoRa is busy", ORANGE)
                else:
                    try:
                        switch = getattr(app, "_switch_lora_protocol", None)
                        if switch:
                            accepted = bool(switch(
                                "meshcore", start_if_enabled=True,
                                _transition_action="wardrive"))
                        else:
                            channels = getattr(app, "_mc_channels_list", None)
                            if channels:
                                lora.set_mc_channels(channels)
                            accepted = bool(lora.start_meshcore(
                                getattr(app, "_mc_region", None)))
                    except Exception as exc:
                        app._term_add(
                            "[ALL] MeshCore start failed: " + str(exc)[:100],
                            raw=True)
                        app.msg(
                            "[ALL] MeshCore unavailable; WiFi/BLE continue",
                            ORANGE)
                    else:
                        if accepted:
                            started = True
                            app._term_add(
                                "[ALL] MeshCore collection starting "
                                "(LoRa enabled)", raw=True)
                        else:
                            app._term_add(
                                "[ALL] MeshCore start failed; check LoRa status",
                                raw=True)
                            app.msg(
                                "[ALL] MeshCore unavailable; WiFi/BLE continue",
                                ORANGE)
        else:
            app._term_add(
                f"[ALL] {protocol_label} selected but LoRa power is off "
                "(SYSTEM > LoRa)",
                raw=True)

        sdr = getattr(app, "_sdr", None)
        sdr_choice = self._selected_sdr_collector()
        sdr_label = "ADS-B" if sdr_choice == "adsb" else "433 MHz"
        if not sdr_choice:
            app._term_add(
                "[ALL] SDR collectors disabled (Wardrive Settings > Collectors)",
                raw=True)
        elif not getattr(app, "_sdr_enabled", False):
            app._term_add(
                f"[ALL] {sdr_label} selected but SDR power is off (SYSTEM > SDR)",
                raw=True)
        elif sdr is None:
            app._term_add(
                f"[ALL] {sdr_label} unavailable: SDR manager missing", raw=True)
        elif sdr.running and sdr.mode == sdr_choice:
            app._term_add(f"[ALL] {sdr_label} collection active", raw=True)
        elif sdr.running:
            # Respect an add-on the user started manually. All Wardrive never
            # takes the single RTL-SDR away from its current owner.
            app._term_add(
                f"[ALL] {sdr_label} skipped: SDR busy in "
                f"{sdr.mode or 'another'} mode",
                raw=True)
            app.msg(f"[ALL] {sdr_label} skipped; SDR is busy", ORANGE)
        elif not app.loot or not app.loot.active:
            app._term_add(
                f"[ALL] {sdr_label} skipped: no writable loot session",
                raw=True)
            app.msg(
                f"[ALL] {sdr_label} needs a writable loot session", ORANGE)
        else:
            loot_path = str(Path(app.loot.session_path))
            try:
                if sdr_choice == "adsb":
                    ok = sdr.start_adsb(loot_path)
                else:
                    lat = app.player_lat if app.gps_fix else 0.0
                    lon = app.player_lon if app.gps_fix else 0.0
                    ok = sdr.start_433(loot_path, lat, lon)
            except Exception as exc:
                ok = False
                app._term_add(
                    f"[ALL] {sdr_label} start failed: " + str(exc)[:100],
                    raw=True)
            if ok:
                started = True
                self._wdg_owned_sdr = True
                app._term_add(
                    f"[ALL] {sdr_label} collection started (SDR enabled)",
                    raw=True)
                try:
                    app._earn_badge(
                        "skywatch" if sdr_choice == "adsb" else "iot_hunter")
                except Exception:
                    pass
            else:
                reason = f"{sdr_label} decoder unavailable"
                try:
                    for event, value in sdr.poll_events():
                        if event == "error":
                            reason = str(value)
                        app._term_add(f"[SDR] {value}", raw=True)
                except Exception:
                    pass
                app._term_add(
                    f"[ALL] {sdr_label} unavailable: " + reason[:100],
                    raw=True)
                app.msg(
                    f"[ALL] {sdr_label} unavailable; WiFi/BLE continue",
                    ORANGE)
        return started

    def _selected_sdr_collector(self):
        """Return the one automatic All Wardrive SDR collector, if any."""
        if self.settings["wardrive_adsb"]:
            return "adsb"
        if self.settings["wardrive_433"]:
            return "433"
        return ""

    def poll_lora_discovery(self, now):
        """Actively probe the selected mesh protocol during real wardrives.

        MeshCore follows MeshMapper's 30-second/25-metre DISCOVER_REQ cadence.
        Meshtastic uses a zero-hop NodeInfo request at a more conservative
        60-second/50-metre cadence. Both ESP BLE and host BLE All Wardrive
        modes share this scan state and therefore get the selected probe.
        """
        active = (self.scan.state == "running"
                  and self.scan.mode == "wardrive")
        if not active:
            self.mc_discovery_session = None
            self.mc_discovery_next = 0.0
            self.mc_discovery_last_fix = None
            return False

        if self.mc_discovery_session != self.scan.session:
            self.mc_discovery_session = self.scan.session
            self.mc_discovery_next = float(now)
            self.mc_discovery_last_fix = None

        if now < self.mc_discovery_next:
            return False

        app = self.app
        protocol = self.settings["lora_protocol"]
        interval = (MC_DISCOVERY_INTERVAL if protocol == "meshcore"
                    else MT_DISCOVERY_INTERVAL)
        min_distance = (MC_DISCOVERY_MIN_DISTANCE_M if protocol == "meshcore"
                        else MT_DISCOVERY_MIN_DISTANCE_M)
        if (not self.settings["wardrive_lora"]
                or not getattr(app, "_lora_enabled", False)):
            self.mc_discovery_next = float(now) + interval
            return False
        if protocol == "meshcore":
            lora = getattr(app, "_lora", None)
            ready = (lora is not None and lora.running
                     and lora.mode == "meshcore")
        else:
            lora = getattr(app, "_meshtastic", None)
            ready = bool(lora is not None and lora.connected)
        if not ready:
            self.mc_discovery_next = float(now) + interval
            return False
        fix = self.fixes.at(now) if app.gps.available else None
        if not fix:
            # A receiver can become ready before gpsd/AIO has produced its
            # first fresh fix.  Retry promptly rather than losing 30 seconds.
            self.mc_discovery_next = float(now) + 1.0
            return False
        point = (fix["latitude"], fix["longitude"])
        if (self.mc_discovery_last_fix is not None
                and distance(self.mc_discovery_last_fix, point)
                < min_distance):
            self.mc_discovery_next = float(now) + interval
            return False
        try:
            if protocol == "meshcore":
                queued = lora.send_meshcore_discovery(
                    point[0], point[1], now=now)
            else:
                queued = lora.request_discovery()
        except Exception as exc:
            app._term_add(
                f"[ALL] {protocol.title()} discovery failed: "
                + str(exc)[:100],
                raw=True,
            )
            self.mc_discovery_next = float(now) + interval
            return False
        if not queued:
            self.mc_discovery_next = float(now) + 1.0
            return False
        self.mc_discovery_last_fix = point
        self.mc_discovery_next = float(now) + interval
        if protocol == "meshcore":
            message = "[ALL] MeshCore DISC queued at current GPS; listening after TX"
        else:
            message = "[ALL] Meshtastic zero-hop NodeInfo discovery queued"
        app._term_add(message, raw=True)
        return True

    # Kept for third-party callers and older tests.
    poll_meshcore_discovery = poll_lora_discovery

    def poll_cell(self, now):
        active = (self.scan.state == "running" and self.scan.mode == "wardrive"
                  and self.settings["lte_modem"]
                  and self.settings["cell_tracking"]
                  and self.app.gps.available)
        if not active:
            self.cell.stop()
        elif not self.cell.active:
            self.start_cell()
        if active and self.scan.legacy and now >= self.cell_legacy_next:
            self.cell_legacy_next = now + 10
            self.cell.observe_batch(self.fixes.at(now), ("legacy", int(now // 10)))
        for session, kind, data in self.cell.poll():
            if session != self.scan.session or not active:
                continue
            if kind == "status":
                if data.get("error"):
                    self.app._term_add("[CELL] " + data["error"], raw=True)
                continue
            if kind == "neighbor_error":
                self.app._term_add("[CELL] Neighbors paused: " + str(data), raw=True)
                self.app.msg("[CELL] Neighbor probe paused; serving cells continue", ORANGE)
                continue
            if kind == "neighbors":
                path = Path(self.app.loot.session_path) / "cell_neighbor_candidates.jsonl"
                if data.candidates:
                    try:
                        with path.open("a", encoding="utf-8") as stream:
                            for candidate in data.candidates:
                                record = candidate.record()
                                stream.write(json.dumps(record, separators=(",", ":")) + "\n")
                                display_record = dict(record)
                                display_record["seen"] = now
                                self.cell_candidates.append(display_record)
                            stream.flush()
                            import os
                            os.fsync(stream.fileno())
                    except OSError as exc:
                        self.app._term_add("[CELL] Cannot save neighbor candidates: "
                                           + str(exc)[:100], raw=True)
                    self.cell.candidates += len(data.candidates)
                continue
            if kind != "cell":
                continue
            measured, cell, fix = data
            try:
                if self.app.loot.save_wardriving_cell(
                        cell.record(), observation_fix=fix, observed_at=measured):
                    self.cell.note_saved(cell)
                    self.app.loot_points.append({
                        "lat": fix["latitude"], "lon": fix["longitude"],
                        "type": "cell", "label": cell.identity,
                        "bssid": cell.identity})
                    self.cell_candidates.append({
                        "latitude": fix["latitude"],
                        "longitude": fix["longitude"],
                        "key": "serving:" + cell.identity,
                        "identity": cell.identity,
                        "seen": now,
                        "provisional": False,
                    })
                    self.app._loot_points_revision = getattr(
                        self.app, "_loot_points_revision", 0) + 1
            except Exception as exc:
                self.app._term_add("[CELL] Save failed: " + str(exc)[:120], raw=True)

    def close_passive(self):
        had_file = self.passive.file is not None
        try:
            self.passive.close()
        except OSError as exc:
            self.app.msg("[HS SNIFF] Save error: " + str(exc)[:60], 8)
        if had_file:
            self.app._term_add("[HS SNIFF] Capture file: " + str(self.passive.path), raw=True)
            self.app.msg(f"[HS SNIFF] EAPOL:{self.passive.eapol} PMKID:{self.passive.pmkids}", CYAN)

    def observation(self, d, terminal=True):
        now = time.monotonic()
        d = dict(d)
        d["fix"] = self.fixes.at(now - d["age_ms"] / 1000) if self.app.gps.available else None
        d["observed_at"] = time.time() - d["age_ms"] / 1000
        if d["kind"] == "wifi":
            net = Network(index="0", bssid=d["mac"], ssid=display_bytes(d["ssid_hex"]),
                          channel=str(d["channel"]), rssi=str(d["rssi"]),
                          auth=d.get("auth", "UNKNOWN"), band="5G" if d["channel"] > 14 else "2.4G")
            self.app._ingest_wifi(net, observation=d, terminal=terminal)
        elif d["kind"] == "ble":
            name = ble_name(bytes.fromhex(d["data_hex"]))
            self.app._ingest_ble(d["mac"], d["rssi"], name or "?", observation=d, terminal=terminal)
        else:
            self.detect(d)

    @staticmethod
    def save_args(observation):
        if observation is None:
            return {}
        return {"observation_fix": observation["fix"], "observed_at": observation["observed_at"]}

    def observe_legacy(self, kind, mac, name, rssi, observation):
        if observation is not None:
            self.detect(observation)
            return
        if not (self.app.wifi_scanning or self.app.ble_scanning):
            return
        d = {"kind": kind, "mac": mac.upper(), "rssi": rssi, "name": name,
             "ssid_hex": name.encode().hex() if kind == "wifi" else "", "data_hex": "",
             "addr_type": None, "fix": self.fixes.at(time.monotonic()), "observed_at": time.time()}
        self.detect(d)

    def detect(self, d):
        now = time.monotonic()
        kind = "wifi" if d["kind"] == "wifi_mgmt" else d["kind"]
        identity = kind + ":" + d["mac"]
        if identity in self.settings["suppressed_devices"]:
            return
        hits = self.detector.classify(d, now, self.app._whitelist.is_blocked, self.settings["suppressed_rules"])
        for hit in hits:
            if not self.settings[hit["category"]]:
                continue
            hit["evidence"] = [h for h in hit["evidence"] if h["id"] not in self.settings["suppressed_rules"]]
            if not hit["evidence"]:
                continue
            hit["strength"] = max(h["strength"] for h in hit["evidence"])
            key = identity + ":" + hit["category"]
            old = self.notables.pop(key, None)
            # Preserve evidence during intermittent sparse advertisements.
            if old and now-old["seen"] < 3 and old["strength"] > hit["strength"]:
                hit = {k:old[k] for k in ("category", "label", "strength", "evidence")}
            item = dict(hit, key=key, identity=identity, mac=d["mac"], kind=kind,
                        name=d.get("name") or (display_bytes(d.get("ssid_hex", "")) if kind=="wifi" else ble_name(bytes.fromhex(d.get("data_hex", "")))),
                        rssi=d["rssi"], first=old["first"] if old else d["observed_at"],
                        last=d["observed_at"], seen=now, fix=d["fix"] or (old["fix"] if old else None),
                        fix_seen=now if d["fix"] else old.get("fix_seen",0) if old else 0)
            # Stored current observation fix stays distinct from last known marker fix.
            item["observation_fix"] = d["fix"]
            self.notables[key] = item
            while len(self.notables) > 256:
                self.notables.popitem(last=False)
            if self.store_path and (not old or now-old.get("saved",0)>=1):
                item["saved"] = now
                try:
                    with self.store_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(item, separators=(",", ":")) + "\n")
                except OSError as exc:
                    self.app.msg("[DETECT] Save failed: " + str(exc)[:50], 8)
            elif old:
                item["saved"] = old.get("saved",0)
            absence = max(10, self.settings["realert_seconds"])
            if hit["strength"] > 0 and (not old or now-old["seen"] >= absence or hit["strength"] > old["strength"]):
                # One queued alert per identity/category; evidence updates replace it.
                for i, queued in enumerate(self.alerts):
                    if queued["key"] == key:
                        self.alerts[i] = item
                        break
                else:
                    self.alerts.append(item)
                self.app._term_add("[DETECT] " + item["label"] + " " + d["mac"] + " " + ",".join(h["id"] for h in hit["evidence"]), raw=True)
        self._refresh_notable_identities()

    def _refresh_notable_identities(self, now=None):
        now = time.monotonic() if now is None else now
        identities = frozenset(
            item["identity"] for item in self.notables.values()
            if self.settings.get(item["category"], False)
            and self.layer_state(
                item["category"], item, now,
                historical=item.get("seen", 0) == 0).visible
            and item.get("strength", 0) > 0)
        if identities != self._notable_identities:
            self._notable_identities = identities
            self._notable_revision += 1

    def live_notable_identities(self):
        return self._notable_identities, self._notable_revision

    def show_network_dots(self):
        """Whether ordinary Wi-Fi/BLE/cell map and radar dots are visible."""
        return any(self.layer_mode(layer) != MODE_OFF
                   for layer in ("wifi", "ble", "cell"))

    def layer_mode(self, layer):
        return self.settings.get(f"dot_{layer}_mode", MODE_RECENT)

    def fade_seconds(self):
        return self.settings.get("dot_fade_seconds", 30)

    def layer_state(self, layer, record, now=None, historical=False):
        mode = self.layer_mode(layer)
        if historical and mode != MODE_KEEP:
            return display_state(MODE_OFF, record, now=now,
                                 lifetime=self.fade_seconds())
        return display_state(mode, record, now=now,
                             lifetime=self.fade_seconds())

    def layer_color(self, layer, base_color, record, now=None,
                    historical=False):
        mode = self.layer_mode(layer)
        if historical and mode != MODE_KEEP:
            return None
        return color_for_record(base_color, mode, record, now=now,
                                lifetime=self.fade_seconds())

    def trail_mode(self):
        mode = self.settings.get("trail_mode")
        if mode in TRAIL_MODES:
            return mode
        return "solid" if self.settings.get("trail", False) else "off"

    def is_notable(self, kind, mac):
        now = time.monotonic()
        for cat in ("flock", "axon"):
            item = self.notables.get(kind + ":" + mac.upper() + ":" + cat)
            if (item and self.settings[cat] and item["strength"] > 0
                    and self.layer_state(
                        cat, item, now,
                        historical=item.get("seen", 0) == 0).visible):
                return True
        return False

    def position(self, item):
        if not item["fix"]:
            return None
        if self.settings["precise"]:
            return item["fix"]["latitude"], item["fix"]["longitude"]
        # The non-precise option intentionally uses the ordinary inventory's
        # decorative scatter when one exists.  The observation registry keeps
        # the real heard-here fix, so consult it only as a bounded fallback.
        objects = self.app.wifi_networks if item["kind"] == "wifi" else self.app.ble_devices
        for obj in objects:
            if getattr(obj, "bssid", getattr(obj, "mac", "")).upper() == item["mac"]:
                return obj.lat, obj.lon
        registry = getattr(self.app, "_map_observations", None)
        if registry is not None:
            observed = registry.get(item["kind"], item["mac"], mode=MODE_KEEP)
            if (observed is not None and observed.lat is not None
                    and observed.lon is not None):
                return observed.lat, observed.lon
        # Probe-only detections have no decorative inventory position.
        return item["fix"]["latitude"], item["fix"]["longitude"]

    def visible_notables(self):
        now = time.monotonic()
        for historical, values in (
                (True, self.history_notables.values()),
                (False, self.notables.values())):
            for item in values:
                if not (self.settings[item["category"]]
                        and item["strength"] > 0):
                    continue
                if not self.layer_state(
                        item["category"], item, now,
                        historical=(historical
                                    or item.get("seen", 0) == 0)).visible:
                    continue
                pos = self.position(item)
                if pos:
                    yield item, pos

    def draw_markers(self):
        import pyxel as px
        now = time.monotonic()
        for item in self.cell_candidates:
            color = self.layer_color("cell", 10, item, now)
            if color is None:
                continue
            x, y = self.app.proj.geo_to_screen(item["latitude"], item["longitude"])
            if self.app.proj.screen_visible(x, y):
                px.circb(x, y, 3, color)
                px.pset(x, y, color)
        for item, pos in self.visible_notables():
            base = ORANGE if item["category"] == "axon" else PURPLE
            color = self.layer_color(
                item["category"], base, item, now,
                historical=item.get("seen", 0) == 0)
            if color is None:
                continue
            x, y = self.app.proj.geo_to_screen(*pos)
            if self.app.proj.screen_visible(x,y):
                px.circb(x,y,5,color)
                if now-item.get("fix_seen",0) < 60:
                    px.circ(x,y,2,color)
                px.text(x+6,y-3,item["category"][0].upper(),color)

    def draw_trail(self):
        import pyxel as px
        style = self.trail_mode()
        if style == "off":
            self._trail_layer.invalidate()
            return
        source = self.history_trail or self.trail
        self._trail_layer.request(
            source.points, source.revision, style, self.app.proj)
        self._trail_layer.step()
        self._trail_layer.draw(px, self.app.proj)

    def draw_radar(self, rx, ry, rr, scale):
        import pyxel as px
        def xy(lat,lon):
            return rx+(lon-self.app.player_lon)*scale, ry+(self.app.player_lat-lat)*scale
        style = self.trail_mode()
        if style != "off":
            source = self.history_trail or self.trail
            key = (id(source), source.revision, style,
                   rx, ry, rr, round(scale, 4),
                   round(self.app.player_lat * scale),
                   round(self.app.player_lon * scale))
            if key != self._radar_trail_key:
                segments = []
                previous = None
                # The 40-pixel radar cannot resolve a 4,096-point route.
                # Use only its newest bounded tail so a fresh GPS sample does
                # not synchronously reproject the complete drive in draw().
                radar_points = list(islice(
                    reversed(source.points), RADAR_TRAIL_POINT_LIMIT))
                radar_points.reverse()
                for p in radar_points:
                    point = xy(p["lat"],p["lon"])
                    if previous and previous[0]["segment"] == p["segment"]:
                        # Clip a segment to the circle analytically before drawing.
                        a,b = previous[1],point
                        dx,dy = b[0]-a[0],b[1]-a[1]
                        qa = dx*dx+dy*dy
                        if qa:
                            qb = 2*((a[0]-rx)*dx+(a[1]-ry)*dy)
                            qc = (a[0]-rx)**2+(a[1]-ry)**2-(rr-1)**2
                            disc = qb*qb-4*qa*qc
                            if disc >= 0:
                                lo=max(0,(-qb-disc**0.5)/(2*qa))
                                hi=min(1,(-qb+disc**0.5)/(2*qa))
                                if lo<=hi:
                                    density = max(
                                        previous[0].get("density", 0),
                                        p.get("density", 0))
                                    color = (CYAN if style == "solid"
                                             else heat_color(density))
                                    segments.append((
                                        a[0]+lo*dx,a[1]+lo*dy,
                                        a[0]+hi*dx,a[1]+hi*dy,color))
                    previous = p,point
                self._radar_trail_key = key
                self._radar_trail_segments = segments
            for segment in self._radar_trail_segments:
                x1, y1, x2, y2, color = segment
                px.line(x1, y1, x2, y2, color)
                if abs(x2-x1) >= abs(y2-y1):
                    px.line(x1, y1+1, x2, y2+1, color)
                else:
                    px.line(x1+1, y1, x2+1, y2, color)
        for item,pos in self.visible_notables():
            base = ORANGE if item["category"] == "axon" else PURPLE
            color = self.layer_color(
                item["category"], base, item, time.monotonic(),
                historical=item.get("seen", 0) == 0)
            if color is None:
                continue
            x,y = xy(*pos)
            if (x-rx)**2+(y-ry)**2 < (rr-3)**2:
                px.circb(x,y,3,color)  # ring stays visible under observer dot
                if time.monotonic()-item.get("fix_seen",0) < 60:
                    px.pset(x,y,color)
        for item in self.cell_candidates:
            color = self.layer_color("cell", 10, item, time.monotonic())
            if color is None:
                continue
            x, y = xy(item["latitude"], item["longitude"])
            if (x-rx)**2+(y-ry)**2 < (rr-3)**2:
                px.circb(x, y, 2, color)

    def cycle_history(self):
        if not self.app.loot:
            return
        root = Path(self.app.loot.session_path).parent
        paths = sorted(root.glob("*/wardrive_trail.jsonl"), reverse=True)
        paths = [p for p in paths if p != self.trail.path]
        self.history_index += 1
        if self.history_index >= len(paths):
            self.history_index = -1
            self.history_trail = None
            self.history_notables.clear()
            self._trail_layer.invalidate()
            return
        path = paths[self.history_index]
        trail = WardriveTrail()
        trail.set_path(path, read_only=True)
        self.history_trail = trail
        self._trail_layer.invalidate()
        self.history_notables.clear()
        notable_path = path.parent / "notable_detections.jsonl"
        if notable_path.exists():
            with notable_path.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        item = json.loads(line)
                        item["seen"] = item["fix_seen"] = 0
                        if item["kind"] not in ("wifi","ble") or item["category"] not in ("flock","axon"):
                            continue
                        self.history_notables[item["key"]] = item
                        if len(self.history_notables)>256:
                            self.history_notables.popitem(last=False)
                    except (ValueError, KeyError, TypeError):
                        pass

    def update_settings(self):
        import pyxel as px
        if px.btnp(px.KEY_S):
            self.app._send("stop")
            return
        if self.settings_page == "layers":
            if px.btnp(px.KEY_TAB):
                self.settings_open = False
                self.settings_page = "main"
                return
            if px.btnp(px.KEY_ESCAPE):
                self.settings_page = "main"
                return
            row_count = len(MAP_LAYERS) + 1
            if px.btnp(px.KEY_UP):
                self.layer_selection = max(0, self.layer_selection - 1)
            if px.btnp(px.KEY_DOWN):
                self.layer_selection = min(row_count - 1,
                                           self.layer_selection + 1)
            direction = 0
            if px.btnp(px.KEY_LEFT):
                direction = -1
            elif px.btnp(px.KEY_RIGHT) or px.btnp(px.KEY_RETURN):
                direction = 1
            if direction:
                if self.layer_selection < len(MAP_LAYERS):
                    self.cycle_layer_mode(
                        MAP_LAYERS[self.layer_selection], direction)
                else:
                    self.cycle_fade_seconds(direction)
            return
        if self.settings_page == "meshtastic":
            if px.btnp(px.KEY_TAB):
                self.settings_open = False
                self.settings_page = "main"
                return
            if px.btnp(px.KEY_ESCAPE):
                self.settings_page = "lora"
                return
            row_count = 8
            if px.btnp(px.KEY_UP):
                self.meshtastic_selection = max(
                    0, self.meshtastic_selection - 1)
            if px.btnp(px.KEY_DOWN):
                self.meshtastic_selection = min(
                    row_count - 1, self.meshtastic_selection + 1)
            direction = 0
            if px.btnp(px.KEY_LEFT):
                direction = -1
            elif px.btnp(px.KEY_RIGHT):
                direction = 1
            activate = px.btnp(px.KEY_RETURN)
            row = self.meshtastic_selection
            if row == 0 and (direction or activate):
                self.cycle_meshtastic_backend(direction or 1)
            elif row == 1 and activate:
                self.toggle_meshtastic_phone_ble()
            elif row == 2 and (direction or activate):
                self.cycle_bluetooth_adapter(
                    "meshtastic_phone_adapter", direction or 1)
            elif row == 3 and (direction or activate):
                self.cycle_bluetooth_adapter(
                    "host_ble_adapter", direction or 1)
            elif row == 4 and activate:
                self._start_meshtastic_action(
                    "Pairing window",
                    lambda manager: manager.open_pairing(120),
                    "open for 120 seconds")
            elif row == 5 and activate:
                self._start_meshtastic_action(
                    "Forget phone", lambda manager: manager.forget_phone(),
                    "bond removed")
            elif row == 6 and activate:
                self.retry_meshtastic_shared_adapter()
            elif row == 7 and activate:
                start_update = getattr(
                    self.app, "_start_meshtastic_update", None)
                if start_update is None:
                    self.app.msg(
                        "[MT] Service updater is not installed", ORANGE)
                else:
                    start_update()
            return
        if self.settings_page == "lora":
            if px.btnp(px.KEY_TAB):
                self.settings_open = False
                self.settings_page = "main"
                return
            if px.btnp(px.KEY_ESCAPE):
                self.settings_page = "main"
                return
            if px.btnp(px.KEY_UP):
                self.lora_selection = max(0, self.lora_selection - 1)
            if px.btnp(px.KEY_DOWN):
                self.lora_selection = min(
                    len(LORA_SETTINGS) - 1, self.lora_selection + 1)
            direction = 0
            if px.btnp(px.KEY_LEFT):
                direction = -1
            elif px.btnp(px.KEY_RIGHT):
                direction = 1
            activate = px.btnp(px.KEY_RETURN)
            key = LORA_SETTINGS[self.lora_selection][0]
            if key == "lora_protocol" and (direction or activate):
                self.cycle_lora_protocol(direction or 1)
            elif key == "_meshcore_region" and activate:
                if self.settings["lora_protocol"] != "meshcore":
                    self.app.msg(
                        "[MC] Region applies only when protocol is MeshCore",
                        ORANGE)
                else:
                    self.open_meshcore_region_picker()
            elif key == "_meshtastic" and activate:
                self.settings_page = "meshtastic"
                self.meshtastic_selection = 0
            return
        if self.settings_page == "collectors":
            if px.btnp(px.KEY_TAB):
                self.settings_open = False
                self.settings_page = "main"
                return
            if px.btnp(px.KEY_ESCAPE):
                self.settings_page = "main"
                return
            collector_keys = tuple(key for key, _label in COLLECTOR_SETTINGS)
            if px.btnp(px.KEY_UP):
                self.collector_selection = max(
                    0, self.collector_selection - 1)
            if px.btnp(px.KEY_DOWN):
                self.collector_selection = min(
                    len(collector_keys) - 1,
                    self.collector_selection + 1)
            if px.btnp(px.KEY_RETURN):
                key = collector_keys[self.collector_selection]
                self.toggle_setting(key)
            return
        if px.btnp(px.KEY_ESCAPE) or px.btnp(px.KEY_TAB):
            self.settings_open = False
            return
        if px.btnp(px.KEY_H):
            try:
                self.cycle_history()
            except OSError as exc:
                self.app.msg("[TRAIL] Cannot load history: " + str(exc)[:50],8)
        if px.btnp(px.KEY_D):
            self.details = not self.details
        if self.details:
            items = list(self.notables.values())
            if px.btnp(px.KEY_UP): self.detail_selection = max(0,self.detail_selection-1)
            if px.btnp(px.KEY_DOWN): self.detail_selection = min(max(0,len(items)-1),self.detail_selection+1)
            if px.btnp(px.KEY_M) and items:
                identity = items[self.detail_selection]["identity"]
                if identity not in self.settings["suppressed_devices"]:
                    self.settings["suppressed_devices"].append(identity)
                self.alerts = deque((a for a in self.alerts if a["identity"] != identity),maxlen=16)
                self.persist_settings()
            if px.btnp(px.KEY_R):
                self.settings["suppressed_devices"] = []
                self.persist_settings()
            return
        keys = tuple(key for key, _label in MAIN_SETTINGS)
        if px.btnp(px.KEY_UP): self.selection = max(0,self.selection-1)
        if px.btnp(px.KEY_DOWN): self.selection = min(len(keys)-1,self.selection+1)
        if px.btnp(px.KEY_RETURN):
            key = keys[self.selection]
            if key == "_map_layers":
                self.settings_page = "layers"
                self.layer_selection = 0
            elif key == "_collectors":
                self.settings_page = "collectors"
                self.collector_selection = 0
            elif key == "_lora_settings":
                self.settings_page = "lora"
                self.lora_selection = 0
            elif key == "trail_mode":
                self.cycle_trail_mode()
            else:
                self.toggle_setting(key)

    def open_meshcore_region_picker(self):
        """Open the existing regional preset picker and return here after it."""
        from .lora_manager import MESHCORE_PRESETS

        keys = list(MESHCORE_PRESETS)
        try:
            self.app._mc_region_sel = keys.index(self.app._mc_region)
        except ValueError:
            self.app._mc_region_sel = 0
        self.app._mc_region_return_to_settings = True
        self.app._mc_region_screen = True
        self.settings_open = False

    def _map_policy_changed(self):
        self._refresh_notable_identities()
        self.app._cluster_sel = -1
        self.app._cluster_popup = None
        callback = getattr(self.app, "_on_map_policy_changed", None)
        if callback:
            callback()

    def cycle_layer_mode(self, layer, direction=1):
        key = f"dot_{layer}_mode"
        self.settings[key] = cycle_mode(self.settings.get(key), direction)
        self.settings["network_dots"] = any(
            self.layer_mode(kind) != MODE_OFF for kind in ("wifi", "ble"))
        self._map_policy_changed()
        self.persist_settings()

    def cycle_fade_seconds(self, direction=1):
        current = self.fade_seconds()
        try:
            index = DOT_FADE_CHOICES.index(current)
        except ValueError:
            index = 1
        step = -1 if direction < 0 else 1
        self.settings["dot_fade_seconds"] = DOT_FADE_CHOICES[
            (index + step) % len(DOT_FADE_CHOICES)]
        self._map_policy_changed()
        self.persist_settings()

    def cycle_trail_mode(self, direction=1):
        previous = self.trail_mode()
        try:
            index = TRAIL_MODES.index(previous)
        except ValueError:
            index = 0
        step = -1 if direction < 0 else 1
        current = TRAIL_MODES[(index + step) % len(TRAIL_MODES)]
        self.settings["trail_mode"] = current
        self.settings["trail"] = current != "off"
        # Crossing OFF starts or ends a recording interval.  Switching the
        # rendering style alone must not create a false GPS route break.
        if previous == "off" or current == "off":
            self.trail.break_segment()
        layer = getattr(self, "_trail_layer", None)
        if layer:
            layer.invalidate()
        self.persist_settings()

    def toggle_setting(self, key):
        """Apply one Wardrive Settings toggle and persist it."""
        if key not in DEFAULTS or type(DEFAULTS[key]) is not bool:
            return
        if (key == "wardrive_lora"
                and getattr(self.app, "_meshtastic_update_running", False)):
            self.app.msg("[MT] Service update is running", ORANGE)
            return
        self.settings[key] = not self.settings[key]
        if key == "wardrive_adsb" and self.settings[key]:
            self.settings["wardrive_433"] = False
        elif key == "wardrive_433" and self.settings[key]:
            self.settings["wardrive_adsb"] = False
        if key in ("flock", "axon"):
            self._refresh_notable_identities()
        if key == "lte_modem":
            # Stop the shared cell owner before changing broker ownership.
            # Wi-Fi, ESP BLE, and host BLE sessions remain untouched.
            self.cell.stop()
            self.app.gps.set_modem_enabled(self.settings[key], reconnect=True)
            if not self.app.gps.available:
                self.app.gps_fix = False
                self.app.gps_sats = 0
                self.app.gps_sats_vis = 0
            state = "enabled" if self.settings[key] else "disabled"
            provider = self.app.gps.provider or "no GPS provider"
            self.app._term_add(
                f"[LTE] Modem integration {state}; GPS: {provider}", raw=True)
            self.app.msg(
                f"[LTE] {state.upper()} | GPS: {provider}",
                CYAN if self.settings[key] else ORANGE)
        if key == "cell_tracking" and not self.settings[key]:
            self.cell.stop()
        if key == "cell_neighbors" and self.cell.active:
            self.app.msg("[CELL] Neighbor setting applies to the next wardrive session", 13)
        if key in ("wardrive_lora", "wardrive_adsb", "wardrive_433"):
            self._apply_collector_setting_change(key)
        self.persist_settings()

    def cycle_lora_protocol(self, direction=1):
        """Select the one protocol allowed to own the AIO SX1262."""
        if getattr(self.app, "_meshtastic_update_running", False):
            self.app.msg("[MT] Service update is running", ORANGE)
            return False
        previous = self.settings["lora_protocol"]
        try:
            index = LORA_PROTOCOLS.index(previous)
        except ValueError:
            index = 0
        step = -1 if direction < 0 else 1
        selected = LORA_PROTOCOLS[(index + step) % len(LORA_PROTOCOLS)]
        if selected == previous:
            return
        switch = getattr(self.app, "_switch_lora_protocol", None)
        if switch and switch(
                selected,
                start_if_enabled=self.settings["wardrive_lora"]) is False:
            return False
        self.settings["lora_protocol"] = selected
        lora = getattr(self.app, "_lora", None)
        manager = getattr(self.app, "_meshtastic", None)
        self._wdg_owned_lora = bool(
            (selected == "meshcore" and lora is not None
             and lora.running and lora.mode == "meshcore"
             and getattr(lora, "radio_owned", False))
            or (selected == "meshtastic" and manager is not None
                and manager.connected))
        self.persist_settings()
        return True

    def cycle_meshtastic_backend(self, direction=1):
        """Select the daemon transport off-thread without closing live leases."""
        current = self.settings.get("meshtastic_backend", "auto")
        try:
            index = MESHTASTIC_BACKENDS.index(current)
        except ValueError:
            index = 0
        step = -1 if direction < 0 else 1
        selected = MESHTASTIC_BACKENDS[
            (index + step) % len(MESHTASTIC_BACKENDS)]
        manager = getattr(self.app, "_meshtastic", None)
        if manager is None:
            self.app.msg("[MT] Meshtastic manager is unavailable", ORANGE)
            return False
        host_ble = getattr(self, "host_ble", None)
        if ((host_ble is not None and host_ble.worker_active)
                or getattr(manager, "ble_scan_lease_active", False)
                or getattr(manager, "pairing_agent_lease_active", False)):
            self.app.msg(
                "[MT] Finish the active Bluetooth operation first", ORANGE)
            return False
        watch = getattr(self.app, "_watch", None)
        if watch is not None and watch.worker_active:
            self.app.msg("[MT] Finish the active watch operation first", ORANGE)
            return False
        lora = getattr(self.app, "_lora", None)
        transition_active = getattr(
            self.app, "_lora_transition_active", lambda: False)
        if (transition_active()
                or (lora is not None and (
                    lora.running or lora.worker_active or lora.radio_owned))):
            self.app.msg(
                "[MT] Finish the current LoRa ownership transition first",
                ORANGE)
            return False

        # The backend choice is only meaningful once Meshtastic owns the
        # SX1262.  A completed direct MeshCore session deliberately retains
        # the exact pre-session service snapshot while both daemons remain
        # stopped.  Persist the future transport choice without touching that
        # rollback token; the normal MeshCore -> Meshtastic handoff restores
        # it and activates this selected backend transactionally.
        deferred = self.settings.get("lora_protocol") != "meshtastic"

        def persist_backend():
            self.settings["meshtastic_backend"] = selected
            self.persist_settings()
            if selected == "legacy_tcp":
                self._host_ble_retry_pending = True
            if deferred:
                self.app._term_add(
                    f"[MT] Backend preference set to {selected}; applies when "
                    "Meshtastic is selected",
                    raw=True)
            else:
                self.app._term_add(
                    f"[MT] Backend and service set to {selected}",
                    raw=True)

        if deferred:
            def defer_backend(value):
                if value.running or value.connected:
                    value.last_error = (
                        "Meshtastic client is still running while MeshCore is "
                        "selected; stop it before changing the preference")
                    return False
                return value.set_backend_mode(selected)

            return self._start_meshtastic_action(
                "Backend", defer_backend,
                "preference set to " + selected,
                on_success=persist_backend)

        def switch_backend(value):
            was_running = bool(value.running)
            previous_mode = value.backend_mode
            previous_expected = (
                None if previous_mode == "auto" else previous_mode)
            controller = getattr(value, "_service_controller", None)
            if controller is not None:
                try:
                    controller.require_current()
                except Exception as exc:
                    value.last_error = str(exc)
                    return False
            if not value.close():
                value.last_error = (
                    "Meshtastic client worker did not stop; backend switch "
                    "was aborted")
                return False

            def restore_previous(reason):
                # Closing the candidate client is a hard barrier.  Without it
                # systemd rollback could race a worker still using the new
                # endpoint, and restarting the old manager could create two
                # active clients.
                try:
                    closed = bool(value.close())
                except Exception as exc:
                    closed = False
                    reason += "; close failed: " + str(exc)[:100]
                if not closed:
                    value.last_error = (
                        reason + "; candidate client worker did not stop, so "
                        "service rollback was blocked")
                    return False
                rollback = getattr(
                    value, "rollback_backend_service_activation", None)
                try:
                    restored = bool(
                        rollback(timeout=15.0)) if rollback else True
                except Exception as exc:
                    restored = False
                    reason += "; service rollback failed: " + str(exc)[:100]
                if not restored:
                    value.last_error = (
                        reason + "; previous service state was not restored")
                    return False
                if not value.set_backend_mode(previous_mode):
                    value.last_error = (
                        reason + "; previous backend mode was not restored")
                    return False
                if was_running:
                    if not value.start():
                        value.last_error = (
                            reason + "; previous client did not restart")
                        return False
                    if not value.wait_connected(
                            timeout=15.0, backend=previous_expected):
                        value.last_error = (
                            reason + "; previous client did not reconnect")
                        return False
                value.last_error = reason
                return False

            activation_attempted = False
            try:
                activation_attempted = True
                if not value.activate_backend_service(selected):
                    return restore_previous(
                        "Selected Meshtastic service did not become ready")
                if not value.set_backend_mode(selected):
                    return restore_previous(
                        "Selected Meshtastic backend mode was rejected")
                if not value.start():
                    return restore_previous(
                        "Selected Meshtastic client did not start")
                expected = None if selected == "auto" else selected
                if not value.wait_connected(timeout=15.0, backend=expected):
                    return restore_previous(
                        "Selected Meshtastic endpoint opened, but protocol "
                        "negotiation did not complete")
                if not was_running and not value.close():
                    value.last_error = (
                        "Temporary Meshtastic client did not stop; backend "
                        "selection was not committed")
                    return False
                commit = getattr(
                    value, "commit_backend_service_activation", None)
                if commit is not None:
                    commit()
                return True
            except Exception as exc:
                if activation_attempted:
                    return restore_previous(
                        "Meshtastic backend switch failed: " + str(exc)[:120])
                value.last_error = str(exc)[:160]
                return False

        return self._start_meshtastic_action(
            "Backend", switch_backend, "set to " + selected,
            on_success=persist_backend)

    def _bluetooth_adapter_choices(self, current):
        choices = ["auto"]
        try:
            choices.extend(address for address, _name in list_ble_adapters())
        except OSError:
            pass
        current = str(current or "auto")
        if current.lower() != "auto" and current not in choices:
            choices.append(current)
        return choices

    def cycle_bluetooth_adapter(self, key, direction=1):
        """Cycle persistent controller MACs; never persist volatile hci names."""
        if getattr(self.app, "_meshtastic_update_running", False):
            self.app.msg("[MT] Service update is running", ORANGE)
            return False
        if getattr(self.app, "_lora_transition_active", lambda: False)():
            self.app.msg("[MT] Radio ownership transition is running", ORANGE)
            return False
        lora = getattr(self.app, "_lora", None)
        if (lora is not None and lora.worker_active
                and not lora.running):
            self.app.msg("[MT] Direct LoRa startup is still running", ORANGE)
            return False
        manager = getattr(self.app, "_meshtastic", None)
        if (getattr(self.host_ble, "worker_active", False)
                or getattr(manager, "ble_scan_lease_active", False)):
            self.app.msg(
                "[BLE] Stop the active host scan before changing adapters",
                ORANGE)
            return False
        current = self.settings.get(key, "auto")
        choices = self._bluetooth_adapter_choices(current)
        try:
            index = choices.index(current)
        except ValueError:
            index = 0
        step = -1 if direction < 0 else 1
        selected = choices[(index + step) % len(choices)]
        self.settings[key] = selected
        self.persist_settings()
        if key == "meshtastic_phone_adapter":
            if manager is not None:
                manager.configure_phone_ble(
                    self.settings["meshtastic_phone_ble_enabled"], selected)
            if (manager is not None and manager.connected
                    and manager.backend == "fork_socket"):
                self._start_meshtastic_action(
                    "Phone adapter",
                    lambda value: value.apply_phone_ble(),
                    "set to " + selected)
        elif self.host_ble.state in ("starting", "running"):
            self.app.msg(
                "[BLE] Host adapter changes on the next scan", ORANGE)
        elif (key == "host_ble_adapter"
              and self.host_ble.state in ("paused", "error")):
            self._host_ble_retry_pending = True

    def toggle_meshtastic_phone_ble(self):
        if getattr(self.app, "_meshtastic_update_running", False):
            self.app.msg("[MT] Service update is running", ORANGE)
            return False
        manager = getattr(self.app, "_meshtastic", None)
        if (getattr(getattr(self, "host_ble", None), "worker_active", False)
                or getattr(manager, "ble_scan_lease_active", False)):
            self.app.msg(
                "[BLE] Stop the active host scan before changing phone BLE",
                ORANGE)
            return False
        enabled = not self.settings["meshtastic_phone_ble_enabled"]
        self.settings["meshtastic_phone_ble_enabled"] = enabled
        self.persist_settings()
        if manager is not None:
            manager.configure_phone_ble(
                enabled, self.settings["meshtastic_phone_adapter"])
        if (manager is not None and manager.connected
                and manager.backend == "fork_socket"):
            self._start_meshtastic_action(
                "Phone BLE",
                lambda value: value.apply_phone_ble(),
                "enabled" if enabled else "disabled")
        else:
            self.app.msg(
                "[MT] Phone BLE setting saved for the next fork connection",
                CYAN)
        if not enabled:
            self._host_ble_retry_pending = True
        return True

    def retry_meshtastic_shared_adapter(self):
        """Retry coexistence only after host scanning releases BlueZ."""
        manager = getattr(self.app, "_meshtastic", None)
        if (getattr(getattr(self, "host_ble", None), "worker_active", False)
                or getattr(manager, "ble_scan_lease_active", False)):
            self.app.msg(
                "[BLE] Stop the active host scan before retrying the adapter",
                ORANGE)
            return False
        return self._start_meshtastic_action(
            "Shared adapter",
            lambda value: value.retry_shared_adapter(),
            "retry enabled")

    def _start_meshtastic_action(self, label, operation, success_detail,
                                 *, on_success=None):
        """Run a bounded daemon control call without stalling Pyxel."""
        if getattr(self.app, "_meshtastic_update_running", False):
            self.app.msg("[MT] Service update is running", ORANGE)
            return False
        if getattr(self.app, "_lora_transition_active", lambda: False)():
            self.app.msg("[MT] Radio ownership transition is running", ORANGE)
            return False
        lora = getattr(self.app, "_lora", None)
        if (lora is not None and lora.worker_active
                and not lora.running):
            self.app.msg("[MT] Direct LoRa startup is still running", ORANGE)
            return False
        thread = self._meshtastic_action_thread
        if thread is not None and thread.is_alive():
            self.app.msg("[MT] Another control action is running", ORANGE)
            return False
        manager = getattr(self.app, "_meshtastic", None)
        if manager is None:
            self.app.msg("[MT] Meshtastic manager is unavailable", ORANGE)
            return False
        begin = getattr(self.app, "_begin_meshtastic_transition", None)
        transition_acquired = bool(begin and begin("control"))
        if begin is not None and not transition_acquired:
            self.app.msg("[MT] Another radio/service operation is running",
                         ORANGE)
            return False

        def worker():
            try:
                ok = bool(operation(manager))
                detail = (success_detail if ok else
                          manager.last_error or "daemon rejected the request")
            except Exception as exc:
                ok = False
                detail = str(exc)[:140]
            finally:
                if transition_acquired:
                    end = getattr(
                        self.app, "_end_meshtastic_transition", None)
                    if end is not None:
                        end()
            self._meshtastic_action_results.put((
                ok, label, detail, on_success))

        try:
            factory = getattr(
                self, "_meshtastic_action_thread_factory", threading.Thread)
            thread = factory(
                target=worker, name="wdg-meshtastic-control", daemon=True)
            self._meshtastic_action_thread = thread
            thread.start()
        except Exception as exc:
            self._meshtastic_action_thread = None
            if transition_acquired:
                end = getattr(
                    self.app, "_end_meshtastic_transition", None)
                if end is not None:
                    end()
            self.app.msg(
                "[MT] Could not start control worker: " + str(exc)[:90],
                ORANGE)
            return False
        return True

    def _apply_collector_setting_change(self, key):
        """Release WDG-owned radios and apply enabled choices mid-session."""
        app = self.app
        release_ok = True
        if key == "wardrive_lora" and not self.settings[key]:
            lora = getattr(app, "_lora", None)
            meshtastic = getattr(app, "_meshtastic", None)
            cancel = getattr(app, "_cancel_pending_lora_start", None)
            cancelled = bool(cancel and cancel(collector_only=True))
            direct_pending = getattr(app, "_lora_start_pending", "")
            direct_action = (
                direct_pending[0] if isinstance(direct_pending, tuple)
                else direct_pending)
            if self._wdg_owned_lora or cancelled:
                if (lora is not None
                        and (lora.running or lora.worker_active
                             or lora.radio_owned)
                        and (lora.mode == "meshcore"
                             or direct_action == "wardrive")):
                    try:
                        direct_stopped = bool(lora.stop())
                    except Exception as exc:
                        direct_stopped = False
                        direct_error = str(exc)[:100]
                    else:
                        direct_error = ""
                    direct_survives = bool(
                        getattr(lora, "running", False)
                        or getattr(lora, "worker_active", False)
                        or getattr(lora, "radio_owned", False))
                    if not direct_stopped or direct_survives:
                        release_ok = False
                        self._wdg_owned_lora = True
                        detail = (
                            "direct LoRa worker or SX1262 lock is still active"
                            if direct_survives else
                            direct_error or "direct LoRa stop was not confirmed")
                        app._term_add(
                            "[ALL] LoRa collector stop incomplete: " + detail,
                            raw=True)
                        notify = getattr(app, "msg", None)
                        if notify:
                            notify(
                                "[ALL] LoRa stop incomplete; ownership retained",
                                ORANGE)
                # A pending daemon handoff will observe the cancelled epoch
                # and close itself after its worker exits.  Do not race close
                # against service selection here.
                if (meshtastic is not None
                        and (getattr(meshtastic, "running", False)
                             or getattr(meshtastic, "connected", False))):
                    handoff = getattr(app, "_lora_handoff_pending", None)
                    handoff_action = (
                        handoff[0] if isinstance(handoff, tuple) else "")
                    if handoff_action == "wardrive":
                        release_ok = False
                        self._wdg_owned_lora = True
                        app._term_add(
                            "[ALL] Meshtastic collector cancellation is "
                            "waiting for its service handoff", raw=True)
                        notify = getattr(app, "msg", None)
                        if notify:
                            notify(
                                "[ALL] Meshtastic stop pending; ownership "
                                "retained", ORANGE)
                    else:
                        try:
                            client_stopped = bool(meshtastic.close())
                        except Exception as exc:
                            client_stopped = False
                            client_error = str(exc)[:100]
                        else:
                            client_error = ""
                        client_survives = bool(
                            getattr(meshtastic, "running", False)
                            or getattr(meshtastic, "connected", False))
                        if not client_stopped or client_survives:
                            release_ok = False
                            self._wdg_owned_lora = True
                            detail = (
                                "Meshtastic client worker is still active"
                                if client_survives else
                                client_error
                                or "Meshtastic client close was not confirmed")
                            app._term_add(
                                "[ALL] Meshtastic collector stop incomplete: "
                                + detail, raw=True)
                            notify = getattr(app, "msg", None)
                            if notify:
                                notify(
                                    "[ALL] Meshtastic stop incomplete; "
                                    "ownership retained", ORANGE)
                if release_ok:
                    app._term_add("[ALL] WDG LoRa client stopped", raw=True)
            if release_ok:
                self._wdg_owned_lora = False

        sdr = getattr(app, "_sdr", None)
        desired = self._selected_sdr_collector()
        if (key in ("wardrive_adsb", "wardrive_433")
                and self._wdg_owned_sdr and sdr is not None
                and sdr.running and sdr.mode != desired):
            sdr.stop()
            self._wdg_owned_sdr = False
            app._term_add(
                "[ALL] WDG SDR collector stopped; tuner released", raw=True)
        elif (key in ("wardrive_adsb", "wardrive_433")
              and not desired):
            self._wdg_owned_sdr = False

        active = (self.scan.state == "running"
                  and self.scan.mode == "wardrive")
        enabled = ((key == "wardrive_lora" and self.settings[key])
                   or (key in ("wardrive_adsb", "wardrive_433")
                       and bool(desired)))
        if active and enabled:
            self.start_auxiliary_collectors()
        return release_ok

    def persist_settings(self):
        try:
            save_settings(self.app._app_dir, self.settings)
        except OSError as exc:
            self.app.msg("[WDG] Settings save failed: " + str(exc)[:50],8)

    def draw_overlay(self):
        import pyxel as px
        if self.settings_open:
            px.camera(0,-24)
            px.rect(40,35,560,285,0)
            px.rectb(40,35,560,285,PURPLE)
            if self.settings_page == "layers":
                px.text(55,47,"MAP DOT LAYERS   arrows / ENTER / ESC",7)
                for i, layer in enumerate(MAP_LAYERS):
                    selected = i == self.layer_selection
                    value = mode_label(self.layer_mode(layer), self.fade_seconds())
                    text = (("> " if selected else "  ")
                            + layer_label(layer) + ": " + value)
                    px.text(55,66+i*18,text,11 if selected else 7)
                row = len(MAP_LAYERS)
                selected = row == self.layer_selection
                px.text(55,66+row*18,
                        ("> " if selected else "  ")
                        + f"Fade time: {self.fade_seconds()} seconds",
                        11 if selected else 7)
                px.text(55,259,"OFF hides only the marker; collection and loot continue.",13)
                px.text(55,271,"FADE dims then removes recent dots. KEEP uses bounded history.",13)
                px.text(55,291,"MeshCore is the positioned LoRa layer.  ESC returns.",10)
                px.camera()
                self.app._draw_mc_toast()
                return
            if self.settings_page == "meshtastic":
                px.text(55,47,
                        "MESHTASTIC SERVICE   arrows / ENTER / ESC",7)
                manager = getattr(self.app, "_meshtastic", None)
                action_busy = bool(
                    self._meshtastic_action_thread is not None
                    and self._meshtastic_action_thread.is_alive())
                action_busy = action_busy or bool(getattr(
                    self.app, "_meshtastic_update_running", False))
                transition_busy = getattr(
                    self.app, "_meshtastic_transition_busy", None)
                if transition_busy is not None:
                    action_busy = action_busy or bool(transition_busy())
                rows = (
                    ("Backend", self.settings["meshtastic_backend"].upper()),
                    ("Phone BLE", "ON" if self.settings[
                        "meshtastic_phone_ble_enabled"] else "OFF"),
                    ("Phone BLE adapter", self.settings[
                        "meshtastic_phone_adapter"]),
                    ("Host scan adapter", self.settings["host_ble_adapter"]),
                    ("Open pairing", "BUSY" if action_busy else "120 SECONDS"),
                    ("Forget paired phone", "BUSY" if action_busy else "RUN"),
                    ("Retry shared adapter", "BUSY" if action_busy else "RUN"),
                    ("Update service", "BUSY" if action_busy else "CHECK"),
                )
                for i, (label, value) in enumerate(rows):
                    selected = i == self.meshtastic_selection
                    px.text(
                        55, 64+i*18,
                        (("> " if selected else "  ") + label + ": "
                         + str(value))[:100],
                        11 if selected else 7)
                connected = bool(manager is not None and manager.connected)
                backend = manager.backend if manager is not None else "unavailable"
                service = (manager.service_state if manager is not None
                           else "unavailable")
                client_label = (
                    "WDG socket" if backend == "fork_socket" else "TCP client")
                client_state = (
                    "CONNECTED" if connected else
                    "RECONNECTING" if manager is not None and manager.running
                    else "DISCONNECTED")
                ble = manager.ble_status if manager is not None else "unavailable"
                if manager is not None and manager.phone_connected:
                    ble = "phone connected"
                radio = manager.radio_status if manager is not None else "unavailable"
                px.text(55,216,
                        f"Service: {service.upper()}  backend: {backend}"[:100],13)
                px.text(55,230,
                        (f"{client_label}: {client_state}  Phone BLE: {ble}"
                         )[:100],13)
                px.text(55,244,
                        (f"Radio: {radio}  "
                         f"Full client: {getattr(manager, 'full_client_owner', 'unknown')}"
                         )[:100],13)
                if manager is not None and manager.pairing_pin:
                    px.text(55,258,
                            "Pairing PIN: " + manager.pairing_pin,11)
                elif manager is not None and manager.last_error:
                    px.text(55,258,
                            ("Last error: " + manager.last_error)[:100],8)
                px.text(55,276,
                        "Adapter choices use stable MACs; hci numbers may change.",10)
                px.text(55,288,
                        "A connected phone has priority on a shared controller.",10)
                px.text(55,300,"ESC returns   TAB closes settings",10)
                px.camera()
                self.app._draw_mc_toast()
                return
            if self.settings_page == "lora":
                from .lora_manager import MESHCORE_PRESETS

                px.text(55, 47,
                        "LORA SETTINGS   arrows / ENTER / ESC", 7)
                region = MESHCORE_PRESETS.get(
                    getattr(self.app, "_mc_region", ""))
                region_label = region[4] if region else "UNKNOWN"
                rows = (
                    ("LoRa protocol", self.settings["lora_protocol"].upper()),
                    ("MeshCore region", region_label),
                    ("Meshtastic service and phone BLE", "OPEN..."),
                )
                for i, (label, value) in enumerate(rows):
                    if (i == 1
                            and self.settings["lora_protocol"] != "meshcore"):
                        value += " (MESHCORE ONLY)"
                    selected = i == self.lora_selection
                    px.text(
                        55, 64+i*20,
                        (("> " if selected else "  ") + label + ": "
                         + str(value))[:100],
                        11 if selected else 7)
                px.text(55, 138,
                        "Protocol selects the one owner of the AIO SX1262.", 13)
                px.text(55, 154,
                        "MeshCore uses direct SPI; Meshtastic uses meshtasticd.",
                        13)
                px.text(55, 176,
                        "Automatic LoRa capture remains under All Wardrive",
                        10)
                px.text(55, 188, "collectors.", 10)
                px.text(55, 300, "ESC returns   TAB closes settings", 10)
                px.camera()
                self.app._draw_mc_toast()
                return
            if self.settings_page == "collectors":
                px.text(55,47,
                        "ALL WARDRIVE COLLECTORS   arrows / ENTER / ESC",7)
                for i, (key, label) in enumerate(COLLECTOR_SETTINGS):
                    value = "ON" if self.settings[key] else "OFF"
                    px.text(
                        55, 64+i*20,
                        ("> " if i == self.collector_selection else "  ")
                        + label + ": " + value,
                        11 if i == self.collector_selection else 7)
                px.text(55,153,
                        "These control automatic WDG radio ownership.",13)
                px.text(55,169,
                        "LoRa protocol and radio options are in LoRa Settings.",13)
                px.text(55,185,
                        "ADS-B and 433 MHz share one RTL-SDR; enabling one",10)
                px.text(55,197,
                        "automatically disables the other.",10)
                px.text(55,225,
                        "SYSTEM LoRa/SDR still controls hardware power.",7)
                px.text(55,241,
                        "Manual ADDONS actions remain available when powered.",7)
                px.text(55,291,"ESC returns   TAB closes settings",10)
                px.camera()
                self.app._draw_mc_toast()
                return
            px.text(55,47,"WARDRIVE SETTINGS   arrows / ENTER / ESC",7)
            for i, (key, label) in enumerate(MAIN_SETTINGS):
                if key in ("_map_layers", "_collectors", "_lora_settings"):
                    value = "OPEN..."
                elif key == "trail_mode":
                    value = self.trail_mode().upper()
                else:
                    value = "ON" if self.settings[key] else "OFF"
                if key in ("cell_tracking", "cell_neighbors") and not self.settings["lte_modem"]:
                    value += " (LTE OFF)"
                px.text(55,64+i*12,("> " if i==self.selection else "  ")+label+": "+value,11 if i==self.selection else 7)
            px.text(55,188,"LTE OFF skips ModemManager; AIO/external GPS and BLE still work.",13)
            px.text(55,199,"Precise = where YOU heard it, not the camera location.",13)
            px.text(55,210,"QMI neighbor dots are provisional and are not exported to WiGLE.",10)
            route = self.history_trail.path.parent.name if self.history_trail else "current session"
            px.text(55,222,"[H] Route history: " + route,13)
            px.text(55,235,"[D] DETECTIONS   [M] mute selected   [R] reset mutes",7)
            items = list(self.notables.values())
            offset = max(0,self.detail_selection-4) if self.details else max(0,len(items)-5)
            for i,item in enumerate(items[offset:offset+5]):
                selected = self.details and i+offset == self.detail_selection
                label = ("> " if selected else "  ")+item["label"]+" "+item["mac"]+" "+str(item["rssi"])+"dBm"
                if item["identity"] in self.settings["suppressed_devices"]: label += " MUTED"
                px.text(55,249+i*12,label[:104],7 if selected else ORANGE if item["category"]=="axon" else 14)
            if self.details and items:
                item = items[min(self.detail_selection,len(items)-1)]
                px.text(55,291,("Rules: "+", ".join(h["id"] for h in item["evidence"]))[:104],13)
                px.text(55,303,("Heard: "+time.strftime("%H:%M:%S",time.localtime(item["last"]))+"  "+("GPS recorded" if item["observation_fix"] else "GPS unavailable")),13)
            px.camera()
            self.app._draw_mc_toast()
        if self.scan.active and not self.settings_open and not self.hs_screen.open and not self.capture_screen.open:
            if self.scan.mode == "hs_sniff":
                text = f"Passive -> uConsole | EAPOL:{self.passive.eapol} PMKID:{self.passive.pmkids}"
                text += f" drops:{self.scan.stats.get('drops',0)} incomplete:{self.passive.lost}"
            else:
                now = time.monotonic()
                ages = " ".join(k+":"+(str(int(now-self.scan.last_seen[k]))+"s" if k in self.scan.last_seen else "--") for k in ("wifi","ble"))
                text = "Last heard "+ages+" drops:"+str(self.scan.stats.get("drops",0))+" bad:"+str(self.invalid_records)
                if self.scan.wifi_only:
                    text = "ESP WiFi / host BLE:"+self.host_ble.state+" "+ages+" drops:"+str(self.scan.stats.get("drops",0))+"/"+str(self.host_ble.drops)
                if self.scan.batch:
                    if self.scan.batch_phase == "scanning":
                        elapsed = min(10, max(0, now-self.scan.batch_started))
                        text = f"Background scan #{self.scan.batch_number} {elapsed:.0f}/10s | " + text
                    elif self.scan.batch_phase == "results":
                        counts = self.scan.batch_counts
                        text = f"Results #{self.scan.batch_number} WiFi:{counts['wifi']} BLE:{counts['ble']} | " + text
                    elif self.scan.batch_phase == "next":
                        text = f"Batch #{self.scan.batch_number} complete; next scan | " + text
                elif self.scan.legacy:
                    text = "LEGACY STREAM | " + text
                if not self.app.gps_fix: text += " GPS unavailable"
                if self.settings["lte_modem"] and self.settings["cell_tracking"]:
                    cell_state = self.cell.state.upper().replace("WAITING_GPS", "WAIT GPS")
                    text += f" CELL:{cell_state} {len(self.cell.unique)}/{self.cell.observations}"
                    if self.cell.latest:
                        value = self.cell.latest
                        signal = value.get("signal_dbm")
                        signal_text = "?dBm" if signal == -113 else f"{round(signal)}dBm"
                        text += " " + value["technology"] + " " + signal_text
                    if self.cell.neighbors_paused:
                        text += " NBR:PAUSED"
            px.rect(4,218,632,12,0)
            px.text(6,220,text,9 if self.scan.stats.get("drops",0) else 13)
        if self.alerts:
            item = self.alerts[0]
            color = ORANGE if item["category"]=="axon" else PURPLE
            # Separate from MeshCore's y=20..52 toast, above every menu.
            px.rect(55,56,530,39,0)
            px.rectb(55,56,530,39,color)
            px.rect(56,57,528,14,color)
            px.text(62,61,item["label"].upper()+"  "+item["mac"],7 if color==PURPLE else 0)
            px.text(62,74,(item["name"][:22]+"  "+str(item["rssi"])+"dBm  "+("heard here" if item["observation_fix"] else "GPS unavailable")),7)
            px.text(62,84,(", ".join(h["method"] for h in item["evidence"]))[:95],13)
