"""ESP32 Watch Dogs — pyxel game frontend for ESP32 security device.

Standalone application — no external TUI required.
Controls:
  [TAB] Toggle cyberdeck menu    [SPACE] Hack nearby device
  [=/]] Zoom in   [-/[] Zoom out  [0] Reset zoom
  [S] Quick stop   [ESC] Quit + stop ESP32
  Arrow keys — manual pan (GPS overrides when fix available)
  PgUp/PgDn (Fn+U / Fn+K on uConsole) — scroll terminal
"""

import math
import os
import random
import re
import sys
import threading
import time
from collections import OrderedDict
from queue import Empty, Full, Queue
from pathlib import Path

import pyxel

from .serial_manager import SerialManager, detect_esp32_port
from .ota_ui import OtaMixin
from .gps_manager import GpsManager
from .wardrive_ui import WardriveUI
from .wardrive_settings import load_settings as load_wardrive_settings
from .loot_manager import LootManager
from .app_state import AppState, Network
from .network_manager import NetworkManager
from .coastline import COASTLINES
from .tile_manager import TileRenderer, download_tiles
from .map_index import GeoObjectIndex
from .map_layers import HistoricalNodeLayer, LiveNodeLayer, RadarNodeLayer
from .map_display import (
    MAX_RECENT_OBSERVATIONS, MODE_KEEP, MODE_OFF,
    RecentObservationRegistry,
)
from .dragon_drain import DragonDrainAttack
from .mitm import MITMAttack
from .bt_ducky import BlueDuckyAttack
from .race_attack import RACEAttack
from .aio_manager import AioManager
from .whitelist_manager import WhitelistManager
from .lora_manager import LoRaManager
from .radio_ownership import RadioOwnership, RadioOwnershipBusy
from .meshtastic_manager import MeshtasticManager
from .meshtastic_service import (
    MESHTASTIC_HELPER,
    MeshtasticInstallError,
    MeshtasticServiceController,
)
from .sdr_manager import SDRManager
from .plugin_loader import discover_plugins
from .watch_manager import WatchManager
from .flipper_manager import FlipperManager
from .portals import get_all_portals, upload_html_to_esp32
from . import upload_manager

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
W, H = 640, 360
FPS = 30
HUD_TOP = 16
HUD_BOT = 16
TERM_H = 110
MAP_H = H - HUD_TOP - HUD_BOT - TERM_H
TERM_Y = H - HUD_BOT - TERM_H
MAP_NODE_LIMIT = MAX_RECENT_OBSERVATIONS
RECENT_NODE_FX_LIMIT = 64

# Pyxel palette (16 colors)
C_WATER = 0
C_LAND = 1
C_COAST = 5
C_GRID = 1
C_COAT = 4
C_COAT_DARK = 5
C_CAP = 5
C_SCARF = 9
C_SKIN = 15
C_PANTS = 1
C_BOOTS = 0
C_HACK_CYAN = 3
C_HUD_BG = 0
C_HUD_LINE = 1
C_TEXT = 7
C_DIM = 13
C_WARNING = 10
C_ERROR = 8
C_SUCCESS = 11
C_DEVICE_SCREEN = 3
C_MENU_BG = 0
C_MENU_BORDER = 5
C_MENU_SEL = 3
C_MENU_TEXT = 7

HUD_FONT_W = 5


def _fit_hud_text(text: str, left: int, right: int,
                  *, align_right: bool = False) -> tuple[int, str]:
    """Fit fixed-width HUD text inside the half-open [left, right) region."""
    capacity = max(0, (right - left) // HUD_FONT_W)
    if capacity == 0:
        return left, ""
    if len(text) > capacity:
        text = text[:capacity]
    width = len(text) * HUD_FONT_W
    return (right - width if align_right else left), text


def _bottom_menu_hint(left: int, right: int) -> tuple[int, str]:
    """Choose the most useful complete shortcut hint that fits the HUD."""
    capacity = max(0, (right - left) // HUD_FONT_W)
    for text in ("[TAB]Menu [`]Loot [S]Stop", "[TAB]Menu [S]Stop", "[TAB]Menu"):
        if len(text) <= capacity:
            return _fit_hud_text(text, left, right, align_right=True)
    return left, ""

ZOOM_LEVELS = [
    (360.0, "WORLD"),
    (180.0, "HEMISPHERE"),
    (90.0, "CONTINENT"),
    (45.0, "REGION"),
    (20.0, "COUNTRY"),
    (10.0, "PROVINCE"),
    (5.0, "CITY"),
    (2.0, "DISTRICT"),
    (1.0, "QUARTER"),
    (0.5, "NEIGHBORHOOD"),
    (0.2, "STREET"),
    (0.1, "BLOCK"),
    (0.05, "BUILDING"),
    (0.02, "CLOSE-UP"),
]

# (xp_threshold, title)
LEVEL_TABLE = [
    (0,          "NOOB"),
    (100,        "SCRIPT_KIDDIE"),
    (500,        "SKIDDIE+"),
    (1500,       "WANNABE"),
    (3000,       "PACKET_MONKEY"),
    (6000,       "WARDRIVER"),
    (10000,      "HACKER"),
    (20000,      "NETRUNNER"),
    (35000,      "PHREAKER"),
    (50000,      "EXPLOIT_DEV"),
    (75000,      "ELITE"),
    (100000,     "SHADOW_OPS"),
    (150000,     "GHOST"),
    (250000,     "ZERO_DAY"),
    (400000,     "APT_AGENT"),
    (600000,     "CYBER_DEMON"),
    (1000000,    "CYBER_GOD"),
    (2500000,    "DIGITAL_DEITY"),
    (5000000,    "MATRIX_BREAKER"),
    (10000000,   "FINAL_BOSS"),
]

# Hacker quips per category (cycles every ~3 s in menu)
HACKER_QUIPS = [
    # SCAN
    ["scanning...", "i see you", "networks exposed", "who's hiding?", "SSID hunter"],
    # SNIFF
    ["sniff sniff!", "packets=power", "wardrive!", "collecting...", "GPS+WiFi=pwn"],
    # ATTACK
    ["hack the world", "no mercy", "DEAUTH ALL!", "shell acquired", "fear the cap"],
    # ADDONS
    ["LoRa waves~", "BLE signals", "HID inject!", "mesh me up", "radio active"],
    # SYSTEM
    ["connection", "is power", "stay dark", "ghost mode", "ctrl+alt+hack"],
]

# (cat_name, [(hotkey, label, cmd_or_action, state_key, input_type), ...])
# input_type: None | "bssid_ch" | "ssid" | "mac" | "text"
# cmd starting with "_" = special Python-side action (not ESP32 serial)
# Attacks that require an external WiFi adapter (monitor mode)
_NEEDS_EXT_WIFI = {"dragon_drain"}
_NEEDS_ESP_SD = {"start_handshake"}
_MENU_WIP_STATES = {"_bt_hid_wip", "_bd_wip", "_race_wip"}

MENU_CATS = [
    ("SCAN", [
        ("1", "WiFi Scan",       "scan_networks",          "wifi_scan",    None),
        ("2", "BLE Scan",        "scan_bt",                "ble_scan",     None),
        ("t", "BT Tracker",      "scan_bt",                "bt_tracking",  "mac"),
        ("a", "AirTag Scan",     "scan_airtag",            "bt_airtag",    None),
    ]),
    ("SNIFF", [
        ("1", "WiFi Wardrive",   "scan_networks",          "wardriving",   None),
        ("2", "BT Wardrive",     "scan_bt",                "bt_scanning",  None),
        ("6", "All Wardrive", "start_wardrive_serial", "all_wardrive", None),
        ("9", "All Wardrive (host BLE)", "start_wardrive_wifi_serial", "all_wardrive_host", None),
        ("8", "ESP Dual Test", "start_wardrive_serial", "all_wardrive_test", None),
        ("o", "Wardrive Settings", "_wardrive_settings", "_wardrive_settings", None),
        ("3", "Pkt Sniffer",     "start_sniffer",          "sniffer",      None),
        ("4", "HS Capture",      "start_handshake",        "_hs_capture_sd_menu",    None),
        ("5", "HS Capture no SD","start_handshake_serial", "_hs_capture_serial_menu",    None),
        ("7", "HS Sniff",        "_hs_sniff_menu",        "_hs_sniff_menu", None),
    ]),
    ("ATTACK", [
        ("1", "Deauth",          "start_deauth",           "deauth",       "bssid_ch"),
        ("2", "Blackout",        "start_blackout",         "blackout",     None),
        ("3", "HS Capture",      "start_handshake",        "_hs_capture_sd_menu",    None),
        ("4", "Evil Twin",       "_evil_twin",             "evil_twin",    None),
        ("5", "SAE Flood",       "sae_overflow",           "sae_flood",    "bssid_ch"),
        ("6", "HS Capture no SD","start_handshake_serial", "_hs_capture_serial_menu",    None),
        ("7", "Evil Portal",     "_evil_portal",           "portal",       None),
        ("d", "Dragon Drain",    "_dragon_drain",          "dragon_drain", None),
        ("m", "MITM",            "_mitm",                  "mitm",         None),
        ("k", "BlueDucky (WIP)", "_bd_wip",                "_bd_wip",      None),
        ("j", "RACE Attack (WIP)", "_race_wip",            "_race_wip",    None),
    ]),
    ("ADDONS", [
        ("h", "BLE HID (WIP)",   "_bt_hid_wip",            "_bt_hid_wip",  None),
        ("i", "HID Type (WIP)",  "_bt_hid_wip",            "_bt_hid_wip",  None),
        ("9", "Mesh Messenger",  "_meshcore",            "meshcore",     None),
        ("r", "MeshCore Region", "_meshcore_region",         "_mc_region_screen", None),
        ("f", "Flipper Zero",    "_flipper",               "_flipper",     None),
        ("a", "ADS-B Radar",     "_sdr_adsb",              "_sdr_adsb",    None),
        ("4", "433 MHz Scanner", "_sdr_433",               "_sdr_433",     None),
        ("p", "PipBoy Watch",   "_watch_connect",         "_watch",       None),
    ]),
    ("SYSTEM", [
        ("x", "STOP ALL",        "stop",                   "_stop_all",    None),
        ("r", "Reboot ESP32",    "_reboot_esp32",          "_reboot",      None),
        ("m", "Download Map",       "_download_map",       "_dl_map",      None),
        ("g", "GPS",             "_toggle_gps",            "_gps_toggle",  None),
        ("l", "LoRa",            "_toggle_lora",           "_lora_toggle", None),
        ("d", "SDR",             "_toggle_sdr",            "_sdr_toggle",  None),
        ("b", "USB",             "_toggle_usb",            "_usb_toggle",  None),
        ("w", "Whitelist",       "_whitelist",             "_wl_screen",   None),
        ("u", "Upload WPA-SEC",  "_wpasec_upload",         "_wpasec_up",   None),
        ("p", "Download WPA-SEC","_wpasec_download",       "_wpasec_dl",   None),
        ("f", "Flash ESP32",    "_flash_esp32",           "_flash_esp",   None),
        ("o", "OTA Update",       "_ota_esp32",             "_ota_esp",     None),
        ("a", "Update WDG",      "_update_app",            "_update_app",  None),
    ]),
]


def _menu_hotkey_key(hotkey: str):
    """Return the Pyxel key constant for one menu letter or digit."""
    if (not isinstance(hotkey, str) or len(hotkey) != 1
            or not hotkey.isascii() or not hotkey.isalnum()):
        return None
    return getattr(pyxel, "KEY_" + hotkey.upper(), None)


# BLE device line regex (from JanOS bt_wardriving)
# Groups: 1=MAC, 2=RSSI, 3=Name (optional), 4=[AirTag]/[SmartTag] (optional)
_BLE_RE = re.compile(
    r'^\s*\d+\.\s+([0-9A-Fa-f:]{17})\s+RSSI:\s*(-?\d+)\s*dBm'
    r'(?:\s+Name:\s*(.+?))?(\s*\[AirTag\]|\s*\[SmartTag\])?\s*$'
)
# AirTag scanner count line: "X,Y" (airtags, smarttags)
_AIRTAG_COUNT_RE = re.compile(r'^(\d+),(\d+)$')
_CAPTURE_KIND_RE = re.compile(r'^CAPTURE_KIND: (VALID|PMKID|PARTIAL)$')
_HS_ARTIFACT_META_RE = re.compile(
    r'^SSID:[ \t]*.*?[ \t]+AP:[ \t]*(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$'
)

# Lines to suppress in terminal
_TERM_SKIP_EXACT = {"", ">", "OK"}
_TERM_SKIP_PREFIX = (
    "> ", "[MEM]", "Command returned", "Stop command received",
    "Stopping ", "cleanup", "=====", "PCAP buffer:", "Dumping",
    "Total handshakes",
)
_TERM_SKIP_CONTAIN = ("already running",)


def _color_for_terminal_line(line: str) -> int:
    """Pick a palette color for a terminal line. Computed once at append time
    so the draw loop doesn't re-evaluate 11 string checks per line per frame."""
    if line.startswith("[WiFi]") or line.startswith("[BLE]"):
        return C_SUCCESS
    if line.startswith("[TRACKER]"):
        return C_WARNING
    if line.startswith("[WL]"):
        return C_DIM
    if ":PWD]" in line:
        return 12
    if ":CLIENT]" in line:
        return C_HACK_CYAN
    if line.startswith(">>>") or "RSSI:" in line or "dBm" in line:
        return C_HACK_CYAN
    if "SSID:" in line or ("SSID" in line and "scan" not in line.lower()):
        return C_SUCCESS
    if "AP:" in line or "BSSID:" in line:
        return C_SUCCESS
    low = line.lower()
    if "handshake" in low or "captured" in low:
        return C_WARNING
    if "[ERR]" in line or "[WARN]" in line:
        return C_ERROR
    if "[OK]" in line or "[SYS]" in line:
        return C_DIM
    if "Ch:" in line or "Channel" in line or "Auth:" in line:
        return 6
    return C_TEXT


# ---------------------------------------------------------------------------
# Game objects
# ---------------------------------------------------------------------------

class BleDevice:
    def __init__(self, lat, lon, mac, name, rssi):
        self.lat, self.lon, self.mac = lat, lon, mac
        self.name = name[:18]
        self.rssi, self.hacked = rssi, False
        self.blink_phase = random.random() * 6.28
        self.spawn_frame = 0
        self.last_seen = time.monotonic()

    @property
    def color(self):
        if self.rssi > -45: return 11
        if self.rssi > -60: return 10
        if self.rssi > -75: return 9
        return 8


class WifiNetwork:
    def __init__(self, lat, lon, bssid, ssid, channel, rssi):
        self.lat, self.lon, self.bssid = lat, lon, bssid
        self.ssid = ssid[:18]
        self.channel, self.rssi, self.hacked = channel, rssi, False
        self.spawn_frame = 0
        self.last_seen = time.monotonic()

    @property
    def color(self):
        if self.rssi > -50: return 11
        if self.rssi > -65: return 10
        if self.rssi > -80: return 9
        return 8


class Particle:
    def __init__(self, x, y, color=3):
        self.x, self.y = x, y
        a = random.random() * 6.28
        s = random.random() * 2.5 + 0.5
        self.vx, self.vy = math.cos(a) * s, math.sin(a) * s
        self.life = random.randint(10, 30)
        self.color = color


class MapMarker:
    def __init__(self, lat, lon, label, mtype, key="", last_seen=None):
        self.lat, self.lon, self.label, self.type = lat, lon, label, mtype
        self.key = key
        self.last_seen = (time.monotonic() if last_seen is None else last_seen)


# ---------------------------------------------------------------------------
# Map projection
# ---------------------------------------------------------------------------

class MapProjection:
    def __init__(self):
        self.center_lat, self.center_lon = 20.0, 0.0
        self.zoom = 0
        self._target_lat, self._target_lon = 20.0, 0.0

    @property
    def lon_span(self): return ZOOM_LEVELS[self.zoom][0]
    @property
    def lat_span(self): return self.lon_span * MAP_H / W
    @property
    def label(self): return ZOOM_LEVELS[self.zoom][1]

    def smooth_move(self, lat, lon):
        self._target_lat, self._target_lon = lat, lon

    def update(self):
        scale = W / self.lon_span
        lat_delta = self._target_lat - self.center_lat
        lon_delta = self._target_lon - self.center_lon
        # Exponential easing otherwise approaches the target forever.  Once
        # the remaining movement is below half a screen pixel, settle exactly
        # so map caches and tile placement stop receiving meaningless changes.
        if abs(lat_delta * scale) < 0.5:
            self.center_lat = self._target_lat
        else:
            self.center_lat += lat_delta * 0.08
        if abs(lon_delta * scale) < 0.5:
            self.center_lon = self._target_lon
        else:
            self.center_lon += lon_delta * 0.08

    def geo_to_screen(self, lat, lon):
        dx = lon - self.center_lon
        if dx > 180: dx -= 360
        elif dx < -180: dx += 360
        dy = self.center_lat - lat
        return (int(W/2 + dx * W / self.lon_span),
                int(HUD_TOP + MAP_H/2 + dy * MAP_H / self.lat_span))

    def screen_visible(self, sx, sy):
        return -20 <= sx <= W+20 and HUD_TOP-10 <= sy <= TERM_Y+10

    def zoom_in(self):
        if self.zoom < len(ZOOM_LEVELS) - 1: self.zoom += 1
    def zoom_out(self):
        if self.zoom > 0: self.zoom -= 1
    def reset_view(self):
        self.zoom = 0
        self._target_lat, self._target_lon = 20.0, 0.0


# ---------------------------------------------------------------------------
# Main game
# ---------------------------------------------------------------------------

class WatchDogsGame(OtaMixin):

    def __init__(self, serial_port=None, loot_path=None):
        pyxel.init(W, H, title="ESP32 Watch Dogs", fps=FPS,
                   quit_key=pyxel.KEY_NONE, display_scale=2)
        try:
            pyxel.fullscreen(True)
        except Exception:
            pass
        pyxel.mouse(False)

        # Load hacker character sprite into image bank 1
        self._hacker_sprite_ok = False
        self._hacker_w = 0
        self._hacker_h = 0
        self._menu_sprites: dict[int, str] = {}  # tab_idx → sprite path
        self._current_menu_sprite = -1           # currently loaded tab
        self._load_hacker_sprite()

        self.proj = MapProjection()
        self._coastlines = COASTLINES
        self._coast_bounds: list[tuple | None] = []
        for seg in self._coastlines:
            if len(seg) < 2:
                self._coast_bounds.append(None)
                continue
            min_lat = min_lon = 1e9
            max_lat = max_lon = -1e9
            for lat, lon in seg:
                if lat < min_lat: min_lat = lat
                if lat > max_lat: max_lat = lat
                if lon < min_lon: min_lon = lon
                if lon > max_lon: max_lon = lon
            antimerid = (max_lon - min_lon) >= 180
            self._coast_bounds.append(
                (min_lat, max_lat, min_lon, max_lon, antimerid))

        # Resolve the data root before GPS starts because LTE integration is a
        # persistent wardrive setting and OFF must skip ModemManager at boot.
        _project_root = Path(__file__).resolve().parent.parent
        _app_dir = loot_path or str(_project_root)
        _wardrive_settings = load_wardrive_settings(_app_dir)

        # --- Direct serial/GPS (no IPC) ---
        self.state = AppState()
        self.serial: SerialManager | None = None
        self.gps = GpsManager(modem_enabled=_wardrive_settings["lte_modem"])
        self.net_mgr = NetworkManager(self.state)
        self.loot: LootManager | None = None

        # AIO v2: auto-enable USB BEFORE serial detection
        # (ESP32 XIAO needs power via USB GPIO before we can find it)
        self._aio_available = AioManager.is_installed()
        self._usb_enabled = False
        self._gps_enabled = False
        self._lora_enabled = False
        self._sdr_enabled = False
        if self._aio_available:
            _aio_st = AioManager.get_status() or {}
            self._usb_enabled = _aio_st.get("usb", False)
            self._gps_enabled = _aio_st.get("gps", False)
            self._lora_enabled = _aio_st.get("lora", False)
            self._sdr_enabled = _aio_st.get("sdr", False)
            if not self._usb_enabled:
                if AioManager.toggle("usb", True):
                    self._usb_enabled = True
                    import time as _t
                    _t.sleep(6)  # ESP32 boot + USB enumerate on CM5

        # Open serial (with probe retry for cold-boot)
        port = serial_port or detect_esp32_port()
        if not port and self._usb_enabled:
            # USB just powered on — port may not be visible yet, retry
            import time as _t
            for _retry in range(3):
                _t.sleep(2)
                port = detect_esp32_port()
                if port:
                    break
        if port:
            try:
                self.serial = SerialManager(port)
                self.serial.setup()
                # Wait for firmware to be ready (cold-boot can take 2-6s)
                import time as _t
                _ready = False
                for _attempt in range(10):  # up to 5s (10 × 0.5s)
                    if self.serial.probe():
                        _ready = True
                        break
                    _t.sleep(0.5)
                if not _ready:
                    # Drain anything that arrived late
                    self.serial.serial_conn.reset_input_buffer()
                self.state.connected = True
                self._esp32 = True
                # Clear any leftover state from previous crashed session
                self.serial.send_command("stop")
                self.serial.serial_conn.reset_input_buffer()
            except Exception as e:
                self._esp32 = False
                self._term_add(f"[ERR] Serial {port}: {e}", raw=True)
        else:
            self._esp32 = False

        # Open GPS only after reading the AIO power rail. setup.sh configures
        # gpsd as the sole raw-UART owner, so this normally opens a local gpsd
        # client rather than competing with meshtasticd for NMEA bytes.
        if not self._aio_available or self._gps_enabled:
            self.gps.setup()
        else:
            self.gps.status_reason = "AIO GPS power is off"

        # Init loot manager (uses app dir or home)
        # Resolve project root from this file's location (works under sudo).
        # __file__ = .../esp32-watch-dogs/watchdogs/app.py → go up two levels.
        # Load bigger font for messenger (8x8 BDF) — kept for chat overlay
        _font_path = _project_root / "assets" / "font_8x8.bdf"
        try:
            self._big_font = pyxel.Font(str(_font_path))
        except Exception:
            self._big_font = None
        # Primary UI font — Spleen 5x8 BSD-2-Clause. Crisper than pyxel's
        # built-in 4x6 and applied globally via a pyxel.text monkey-patch
        # a few lines below.
        _ui_font_path = _project_root / "assets" / "spleen-5x8.bdf"
        try:
            self._font = pyxel.Font(str(_ui_font_path))
        except Exception:
            self._font = None
        self._term_font = self._font
        # Global default: every pyxel.text() call without an explicit font
        # now renders with Spleen 5x8.
        if self._font is not None:
            _orig_text = pyxel.text
            _ui_font = self._font
            def _patched_text(x, y, s, col, font=None, _o=_orig_text, _f=_ui_font):
                _o(x, y, s, col, font if font is not None else _f)
            pyxel.text = _patched_text
        try:
            self.loot = LootManager(_app_dir, gps_manager=self.gps)
        except Exception:
            self.loot = None

        # Tile map renderer (offline OSM tiles)
        self._app_dir = _app_dir
        _maps_dir = Path(_app_dir) / "maps"
        self.tile_renderer = TileRenderer(_maps_dir) if _maps_dir.exists() else None
        self._map_downloading = False
        self._map_download_cancel = False

        # Non-AIO systems consider an available provider their GPS power state.
        if not self._aio_available:
            self._gps_enabled = self.gps.available

        # Whitelist
        self._whitelist = WhitelistManager(Path(_app_dir) / "whitelist.json")
        self._wl_screen = False
        self._wl_sel = 0
        self._wl_add_step = ""   # "" | "type" | "scan_select" | (input via dialog)
        self._wl_add_type = ""   # "wifi" or "ble"
        self._wl_add_mac = ""
        self._wl_scan_list: list[dict] = []
        self._wl_scan_sel = 0
        self._wl_scan_results: dict[tuple[str, str], dict] = {}
        self._wl_scan_phase = "idle"
        self._wl_scan_deadline = 0.0
        self._wl_scan_error = ""
        self._wl_scan_partial = False
        self._wl_stop_outcome = "idle"

        # WPA-sec upload/download
        self._wpasec_busy = False
        self._wpasec_result: Queue = Queue()
        self._wpasec_pending_action = ""  # "upload" or "download"

        # Map clustering + popup
        self._clusters: list[dict] = []
        self._history_node_layer = HistoricalNodeLayer(
            W, HUD_TOP, MAP_H, wifi_color=C_SUCCESS,
            bt_color=C_HACK_CYAN, cell_color=11)
        self._cluster_layer_revision = 0
        self._cluster_sel = -1             # selected cluster index (-1 = none)
        self._cluster_popup: dict | None = None
        self._popup_scroll = 0
        self._cracked_ssids: dict = {}  # SSID → password (from potfile)

        # Firmware version tracking
        self._fw_version: str = ""          # detected from ESP32
        self._fw_remote_version: str = ""   # latest on GitHub
        self._fw_update_available = False

        # MeshCore Messenger (runs in background when LoRa enabled)
        self._meshtastic_service = (
            MeshtasticServiceController()
            if MESHTASTIC_HELPER.is_file() else None)
        self._meshtastic = MeshtasticManager(
            backend_mode=_wardrive_settings["meshtastic_backend"],
            phone_ble_enabled=_wardrive_settings[
                "meshtastic_phone_ble_enabled"],
            phone_ble_adapter=_wardrive_settings[
                "meshtastic_phone_adapter"],
            service_controller=self._meshtastic_service,
            operation_guard=lambda: (
                "SX1262 ownership is uncertain after a failed power-barrier "
                "release; restart WatchDogsGo before starting Meshtastic"
                if getattr(
                    self, "_lora_power_ownership_uncertain", False) else ""),
        )
        self._meshtastic_update_running = False
        self._meshtastic_update_result: Queue = Queue()
        self._meshtastic_update_thread = None
        self._meshtastic_transition_lock = threading.Lock()
        self._meshtastic_transition_state = ""
        self._lora_power_ownership_uncertain = False
        self._lora_power_uncertain_barrier = None
        self._lora = LoRaManager(
            service_handoff=self._meshtastic,
            operation_guard=lambda: (
                "SX1262 ownership is uncertain after a failed power-barrier "
                "release; restart WatchDogsGo before using LoRa"
                if self._lora_power_ownership_uncertain else
                "Meshtastic service update is running; direct LoRa is paused"
                if self._meshtastic_update_running else ""),
        )
        self._lora_transition_queue: Queue = Queue()
        self._lora_transition_thread = None
        self._lora_transition_threads: list[threading.Thread] = []
        self._lora_transition_thread_factory = threading.Thread
        self._lora_power_on_pending = False
        self._lora_start_pending = ""
        self._lora_start_epoch = 0
        self._lora_handoff_pending: tuple[str, int] | None = None
        self._lora_power_intent = self._lora_enabled
        self._meshtastic_update_thread_factory = threading.Thread
        self._lora._on_node = self._on_mc_node
        self._lora._on_message = self._on_mc_message
        self._lora._on_dm = self._on_mc_dm
        self._lora._on_dm_ack = self._on_mc_dm_ack
        self._sdr = SDRManager()
        self._sdr_aircraft_xp: set = set()  # ICAO set for XP dedup
        self._watch = WatchManager(
            pairing_lease_acquire=(
                self._meshtastic.acquire_pairing_agent_lease),
            pairing_lease_release=(
                self._meshtastic.release_pairing_agent_lease),
            scan_lease_acquire=(
                lambda seconds: self._meshtastic.acquire_ble_scan_lease(
                    seconds, owner="watch")),
            scan_lease_release=(
                lambda: self._meshtastic.release_ble_scan_lease(
                    owner="watch")),
        )
        self._watch_screen = False     # watch overlay active
        self._watch_scan_sel = 0       # selected device in scan list
        self._watch_pin_input = ""     # PIN being typed
        self._watch_menu_sel = 0       # selected item in watch menu
        self._watch_nfc_tags: list = []  # cached NFC tag list
        self._watch_log: list = []     # [(text, color)] for watch overlay
        # Auto-connect to watch if already connected in BlueZ
        _watch_addr = self._watch.check_existing()
        self._watch_autoconnect_address = _watch_addr or ""
        self._watch_autoconnect_next = 0.0
        self._watch_autoconnect_result: Queue = Queue(maxsize=1)
        self._watch_autoconnect_thread = None
        self._watch_autoconnect_cancel = threading.Event()
        self._watch_autoconnect_delay = 1.0
        self._watch_autoconnect_last_detail = ""
        # Plugins
        self._plugins = discover_plugins()
        self._plugin_overlay = None  # active plugin overlay
        for p in self._plugins:
            p.on_load(self)
        # Inject PLUGINS menu tab if any plugins loaded
        if self._plugins:
            plugin_items = []
            for p_idx, p in enumerate(self._plugins):
                for mi in p.menu_items():
                    plugin_items.append(
                        (mi.key, f"{mi.label}", f"_plugin_{p_idx}_{mi.action}",
                         f"_p_{p_idx}_{mi.action}", None))
            if plugin_items:
                MENU_CATS.append(("PLUGINS", plugin_items))
        self._lora._on_tx_confirm = self._on_mc_tx_confirm
        self._mc_dm_target = None  # node dict when in DM mode
        self._mc_dm_ack_map: dict[bytes, int] = {}  # ack_hash → log index
        self._mc_screen = False
        self._mc_log: list = []  # [(text, color, tag?)]
        self._mc_tx_pending: dict[bytes, int] = {}  # dedup_key → log index
        self._mc_input = ""
        self._mc_scroll = 0
        # Load MeshCore config (node name, channels)
        from .lora_manager import load_meshcore_config, save_meshcore_config
        _mc_cfg = load_meshcore_config()
        node_name = _mc_cfg.get("node_name", "")
        # Generate unique name from Ed25519 pubkey if missing OR still on legacy
        # default ("WatchDogs" with no suffix) — old configs from a buggy default.
        if not node_name or node_name == "WatchDogs":
            _, pub = self._lora._get_ed25519_keypair()
            node_name = f"WatchDogs_{pub[:4].hex()}"
            save_meshcore_config(node_name, _mc_cfg.get("_channels", []))
        self._mc_node_name = node_name
        # MeshCore regional preset (EU/UK, US/CA, ...) — picker in ADDONS menu.
        from .lora_manager import DEFAULT_MESHCORE_REGION
        self._mc_region = _mc_cfg.get("region", DEFAULT_MESHCORE_REGION)
        self._mc_region_screen = False
        self._mc_region_sel = 0
        self._mc_channels_list = _mc_cfg.get("_channels", [])
        self._mc_active_ch = 0
        self._mc_nodes_panel = False
        self._mc_node_sel = 0  # selected node index in panel
        self._mc_node_action = False  # action menu overlay
        self._mc_node_action_sel = 0
        self._mc_note_editing = False  # note edit mode
        self._mc_note_buf = ""
        self._mc_chan_picker = False
        self._mc_chan_sel = 0
        self._mc_bubbles: list[tuple[str, int]] = []  # (text, frame_expire)
        self._mc_nodes: list[dict] = []
        # Load persistent contacts from loot_db
        if self.loot:
            saved = self.loot.load_contacts()
            for nid, nd in saved.items():
                nd["id"] = nid
                self._mc_nodes.append(nd)
                # Restore pubkey to LoRa manager for DM decryption
                if nd.get("pubkey"):
                    self._lora._known_pubkeys[nid] = bytes.fromhex(
                        nd["pubkey"])

        # Flipper Zero
        self._flipper = FlipperManager()
        self._flipper_screen = False
        self._flipper_log: list[tuple[str, int]] = []  # (text, color)
        self._flipper_scroll = 0
        self._flipper_mode = "idle"  # idle, subghz_rx, subghz_files
        self._flipper_sel = 0
        self._flipper_freq = "433.92"
        self._flipper_files: list[str] = []
        # Badges (persistent via loot_db)
        self._badges: set[str] = set()
        if self.loot:
            try:
                self._badges = self.loot.load_badges()
            except Exception:
                pass
            # Bootstrap badges from historical loot data
            try:
                t = self.loot.loot_totals
                if t.get("wardriving", 0) > 0 or t.get("bt_devices", 0) > 0:
                    self._earn_badge("wardriver")
                if t.get("pcap", 0) > 0:
                    self._earn_badge("handshake_hunter")
                if t.get("et_captures", 0) > 0:
                    self._earn_badge("evil_twin")
                if (t.get("mc_messages", 0) > 0
                        or t.get("mt_messages", 0) > 0):
                    self._earn_badge("meshcore")
                # Check if wpasec potfile exists
                pwd_dir = self.loot.loot_root / "passwords"
                if (pwd_dir / "wpasec_cracked.potfile").exists():
                    self._earn_badge("wpasec_uploader")
                # Check if NFC files exist in any session
                for entry in self.loot.loot_root.iterdir():
                    if entry.is_dir() and (entry / "nfc").is_dir():
                        self._earn_badge("flipper")
                        break
            except Exception:
                pass
        self._flipper_used = "flipper" in self._badges
        self._mc_event_queue: Queue = Queue()  # thread-safe callback events

        # Player
        self.player_lat, self.player_lon = 51.1, 17.9  # Opole default
        self.gps_fix = False
        self.gps_sats = 0
        self.gps_sats_vis = 0
        self._manual_move = False
        self._breath = 0
        self._battery_pct = self._read_battery()  # -1 if unavailable

        # Game state — XP loaded from loot database below
        self.xp = 0
        self._xp_dirty = False
        if self.loot:
            saved_xp = self.loot.load_xp()
            if saved_xp > 0:
                self.xp = saved_xp
            else:
                self.xp = self.loot.calculate_xp_from_loot()
                if self.xp > 0:
                    self.loot.save_xp(self.xp)
        self.ble_devices: list[BleDevice] = []
        self.wifi_networks: list[WifiNetwork] = []
        self._recent_ble_fx: list[BleDevice] = []
        self._recent_wifi_fx: list[WifiNetwork] = []
        self._live_map_revision = 0
        self._map_node_sequence = 0
        self._session_wifi_discovered = 0
        self._session_ble_discovered = 0
        self._map_loot_view_key = None
        self._map_loot_view: list[dict] = []
        self._map_history_candidates_key = None
        self._map_history_candidates: list[dict] = []
        self._loot_points_revision = 0
        self._map_observations = RecentObservationRegistry(
            capacity=MAP_NODE_LIMIT,
            lifetime=_wardrive_settings.get("dot_fade_seconds", 30))
        self._map_live_view_key = None
        self._map_live_views = ((), ())
        self._map_display_next_tick = 0.0
        self._live_node_layer = LiveNodeLayer(W, HUD_TOP, MAP_H)
        self._radar_node_layer = RadarNodeLayer(20, colors={
            "wifi": C_SUCCESS, "bt": C_HACK_CYAN, "cell": 11,
            "handshake": C_ERROR, "meshcore": C_WARNING, "other": C_DIM,
            "wifi_live": C_WARNING, "hacked": C_SUCCESS,
        })
        self._ble_map_index = GeoObjectIndex()
        self._wifi_map_index = GeoObjectIndex()
        self._state_network_by_bssid: dict[str, int] = {}
        self._pwned_count = 0
        self.markers: list[MapMarker] = []
        self._marker_by_key: dict[str, MapMarker] = {}
        for node in self._mc_nodes:
            lat, lon = node.get("lat"), node.get("lon")
            if lat and lon:
                protocol = node.get("protocol", "meshcore")
                node_id = str(node.get("id", ""))
                marker = MapMarker(
                    lat, lon, node.get("name") or (
                        "MT" if protocol == "meshtastic" else "MC"),
                    "meshcore",
                    key=(node_id if protocol == "meshtastic"
                         else "meshcore:" + node_id),
                    last_seen=0)
                self.markers.append(marker)
                self._marker_by_key[marker.key] = marker
        self._map_aircraft = OrderedDict()
        self._map_sensors = OrderedDict()
        self.particles: list[Particle] = []
        self.msgs: list[tuple[str, int, int]] = []
        self.scan_pulse = 0
        self.glitch_timer = 0
        self.scan_lines: list[int] = []
        # Known devices — loaded from loot history for dedup XP
        self._known_ble: set[str] = set()
        self._known_wifi: set[str] = set()
        if self.loot:
            try:
                hist_ble, hist_wifi = self.loot.get_known_devices()
                self._known_ble = hist_ble
                self._known_wifi = hist_wifi
            except Exception:
                pass

        # Hack
        self.hack_target = None
        self.hack_progress = 0
        self.hacking = False

        # Operation state
        self.wifi_scanning = False
        self.ble_scanning = False
        self.sniffing = False
        self.capturing_hs = False
        self._wifi_scan_only = False   # SCAN tab WiFi (no GPS needed)
        self._ble_scan_only = False    # SCAN tab BLE (no GPS needed)
        self._bt_tracking = False      # BT Tracker
        self._bt_airtag = False        # AirTag Scan
        self._airtag_count = 0         # Apple AirTags detected
        self._smarttag_count = 0       # Samsung SmartTags detected
        self._last_hs_count = 0
        self._serial_capture_kind = None

        # Wardriving auto-repeat (continuous scan loop)
        self._wifi_scan_done_time = 0.0  # time.time() when last WiFi scan finished
        self._bt_scan_done_time = 0.0    # time.time() when last BT scan finished
        self._bt_scan_start_time = 0.0   # timeout guard for BT scan
        self._SCAN_INTERVAL = 2.0        # seconds between scan cycles
        self._BT_SCAN_TIMEOUT = 15.0     # auto-finish BT scan after this

        # GPS wait dialog (before wardriving)
        self._gps_wait = False           # waiting for GPS fix before starting
        self._gps_wait_cmd = ""          # command to run after fix
        self._gps_wait_state = ""        # state_key for the command
        self._gps_wait_name = ""         # display name
        self._gps_wait_dialog = False    # showing Y/N dialog

        # Menu
        self.menu_open = False
        self.menu_cat = 0       # selected category index
        self.menu_sel = 0       # selected item index within category
        self._pending_cmd = None
        self._pending_cmd_frame = 0
        self._pending_cmd_name = ""

        # Input dialog (for commands needing params)
        self.input_mode = False
        self.input_fields: list[dict] = []
        self.input_field_idx = 0
        self._input_pending_cat = 0
        self._input_pending_item = 0

        # Loot screen
        self.loot_screen = False
        self._loot_totals: dict = {}
        self._loot_search = ""              # search query
        self._loot_search_active = False    # cursor in search field
        self._loot_search_results: list[dict] = []
        self._loot_search_scroll = 0

        # Python-native attacks (no ESP32 needed)
        self._dragon_drain = DragonDrainAttack(msg_fn=self._attack_msg, loot=self.loot)
        self._mitm = MITMAttack(msg_fn=self._mitm_msg, loot=self.loot)
        self._blueducky = BlueDuckyAttack(msg_fn=self._attack_msg, loot=self.loot)
        self._race = RACEAttack(msg_fn=self._attack_msg, loot=self.loot)

        # Attack sub-screen state (for multi-step flows)
        self._attack_mode = ""  # "" = none, "dragon_drain", "blueducky", "race", "evil_portal", "evil_twin"
        self._attack_step = ""  # current step in flow
        self._attack_scan_results: list = []
        self._attack_iface = ""

        # Evil Portal / Evil Twin shared state
        self._portal_ssid = ""           # SSID for Evil Portal
        self._portal_list: list = []     # cached [(name, html|None)]
        self._portal_sel = 0             # cursor in portal picker
        self._portal_select_screen = False  # show portal picker overlay
        self._et_net_idx = 0             # primary network index (Evil Twin clone)
        self._et_net_sel = 0             # cursor in network picker
        self._et_net_selected: set[int] = set()  # multi-select: toggled rows
        self._et_select_args = ""        # "1 3 5" for select_networks cmd
        self._et_net_screen = False      # show network picker overlay
        self._et_scan_pending = False    # waiting for scan results

        # Loot password history viewer
        self._loot_pwd_screen = False
        self._loot_pwd_data: list[str] = []   # parsed credential lines
        self._loot_pwd_scroll = 0

        # MITM dedicated screen (JanOS-style sub-screen)
        self._mitm_screen = False
        self._mitm_state = "idle"  # idle/iface/target_mode/scan/input_ip/confirm/running
        self._mitm_ifaces: list = []
        self._mitm_hosts: list = []  # scan results [(ip, mac), ...]
        self._mitm_sel = 0  # cursor for lists
        self._mitm_log: list = []  # [(text, color_idx), ...]
        self._mitm_log_scroll = 0
        self._mitm_victim_ip = ""  # pending single IP
        self._mitm_error = ""  # error message for error state
        self._mitm_close_frame = -1  # frame when MITM screen closed (ESC guard)
        self._esc_consumed_frame = -1  # frame when ESC was consumed by sub-screen
        self._mitm_lock = threading.Lock()

        # Quit confirm dialog
        self.confirm_quit = False

        # Terminal
        self.terminal_lines: list[str] = []
        self._terminal_colors: list[int] = []
        self.term_scroll = 0
        self._term_lock = threading.Lock()

        # Loot GPS points (loaded from all loot sessions)
        self.loot_points: list[dict] = []
        self._loot_points_ts = 0.0
        self._loot_refresh_result: Queue = Queue(maxsize=1)
        self._loot_refresh_active = False

        # Init
        self.proj.smooth_move(self.player_lat, self.player_lon)
        self.proj.zoom = 6

        # --- Boot screen state ---
        self._boot_phase = True
        self._boot_lines: list[tuple[str, int]] = []  # (text, color)
        self._boot_frame = 0
        self._boot_checks_done = False
        self._boot_serial_port = port
        self._gps_retry_at = 0.0
        self._build_boot_checks()

        # Start the selected mesh client only when automatic LoRa collection
        # is enabled. MeshCore owns SPI directly; Meshtastic is a TCP client of
        # meshtasticd, which remains the sole SX1262 owner.
        if self._lora_enabled and _wardrive_settings["wardrive_lora"]:
            self._switch_lora_protocol(
                _wardrive_settings["lora_protocol"], start_if_enabled=True)
        elif self._lora_enabled:
            self._term_add(
                "[SYS] LoRa powered; automatic WDG mesh client is disabled",
                raw=True)

        self.wardrive = WardriveUI(self, initial_settings=_wardrive_settings)
        pyxel.run(self.update, self.draw)

    # ------------------------------------------------------------------
    # XP & Level
    # ------------------------------------------------------------------

    @property
    def level(self) -> int:
        """Calculate level index from XP using LEVEL_TABLE thresholds."""
        for i in range(len(LEVEL_TABLE) - 1, -1, -1):
            if self.xp >= LEVEL_TABLE[i][0]:
                return i + 1
        return 1

    @property
    def level_title(self) -> str:
        """Current level title from LEVEL_TABLE."""
        for i in range(len(LEVEL_TABLE) - 1, -1, -1):
            if self.xp >= LEVEL_TABLE[i][0]:
                return LEVEL_TABLE[i][1]
        return LEVEL_TABLE[0][1]

    @property
    def xp_for_next_level(self) -> int:
        """XP needed for next level (for progress bar)."""
        for i in range(len(LEVEL_TABLE) - 1):
            if self.xp < LEVEL_TABLE[i + 1][0]:
                return LEVEL_TABLE[i + 1][0] - LEVEL_TABLE[i][0]
        return 1

    @property
    def xp_in_current_level(self) -> int:
        """XP progress within current level."""
        for i in range(len(LEVEL_TABLE) - 1):
            if self.xp < LEVEL_TABLE[i + 1][0]:
                return self.xp - LEVEL_TABLE[i][0]
        return 0

    def _earn_badge(self, badge: str) -> None:
        """Award a badge if not already earned. Persists to loot_db."""
        if badge not in self._badges:
            self._badges.add(badge)
            if self.loot:
                self.loot.save_badge(badge)

    def gain_xp(self, amount: int) -> None:
        """Add XP and mark for periodic save."""
        self.xp += amount
        self._xp_dirty = True

    def _save_xp_if_dirty(self) -> None:
        """Persist XP to loot_db.json if changed."""
        if self._xp_dirty and self.loot:
            self.loot.save_xp(self.xp)
            self._xp_dirty = False

    def _load_hacker_sprite(self):
        """Load hacker character PNG into pyxel image bank 1.

        Priority:
        1. assets/hacker.png (user's original hi-res image) → convert to sprite
        2. assets/hacker_sprite.png (pre-converted or generated pixel art)
        3. Generate pixel art fallback
        """
        _project_root = Path(__file__).resolve().parent.parent
        sprite_path = _project_root / "assets" / "hacker_sprite.png"

        # Try converting hi-res source image first
        src = _project_root / "assets" / "hacker.png"
        if src.exists():
            try:
                from .convert_sprite import convert
                convert(str(src), str(sprite_path))
            except Exception:
                pass

        # If no sprite yet, generate pixel art fallback
        if not sprite_path.exists():
            try:
                from .generate_hacker_sprite import generate
                generate()
            except Exception:
                pass

        if not sprite_path.exists():
            return

        try:
            pyxel.image(1).load(0, 0, str(sprite_path))
            from PIL import Image
            img = Image.open(str(sprite_path))
            self._hacker_w, self._hacker_h = img.size
            self._hacker_sprite_ok = True
        except Exception:
            self._hacker_sprite_ok = False

        # Build per-tab sprite map: tab_index → sprite path
        # 0=SCAN, 1=SNIFF, 2=ATTACK, 3=ADDONS, 4=SYSTEM
        _tab_sprites = {
            0: "scan_sprite.png",      # SCAN tab
            1: "sniff_sprite.png",     # SNIFF tab
            2: "attack_sprite.png",    # ATTACK tab
            3: "addons_sprite.png",    # ADDONS tab
            4: "hacker_sprite.png",    # SYSTEM tab
        }
        for tab_idx, fname in _tab_sprites.items():
            p = _project_root / "assets" / fname
            if p.exists():
                self._menu_sprites[tab_idx] = str(p)
        self._current_menu_sprite = 4  # hacker_sprite loaded by default

        # Load CB radio sprite into image bank 2 (for MeshCore notifications)
        self._radio_sprite_ok = False
        radio_sprite = _project_root / "assets" / "radio_sprite.png"
        radio_src = _project_root / "assets" / "radio.png"
        # Convert hi-res source if available (like hacker sprite)
        if radio_src.exists():
            try:
                from .convert_radio_sprite import convert
                convert(str(radio_src), str(radio_sprite), target_h=48)
            except Exception:
                pass
        if radio_sprite.exists():
            try:
                pyxel.images[2].load(0, 0, str(radio_sprite))
                from PIL import Image
                img = Image.open(str(radio_sprite))
                self._radio_w, self._radio_h = img.size
                self._radio_sprite_ok = True
            except Exception:
                self._radio_w, self._radio_h = 32, 48

    def _build_boot_checks(self):
        """Prepare boot screen check lines."""
        import pyxel as px

        checks: list[tuple[str, int]] = [
            ("ESP32 WATCH DOGS v1.0", C_HACK_CYAN),
            ("=" * 40, C_DIM),
            ("", 0),
            ("SYSTEM CHECK", C_TEXT),
            ("", 0),
        ]

        # Python
        v = sys.version_info
        checks.append((f"  Python {v.major}.{v.minor}.{v.micro}", C_SUCCESS))

        # pyxel
        checks.append((f"  pyxel {px.VERSION}", C_SUCCESS))

        # pyserial
        try:
            import serial as ser
            checks.append((f"  pyserial {ser.__version__}", C_SUCCESS))
        except ImportError:
            checks.append(("  pyserial — NOT FOUND", C_ERROR))

        # Pillow
        try:
            import PIL
            checks.append((f"  Pillow {PIL.__version__}", C_SUCCESS))
        except ImportError:
            checks.append(("  Pillow — NOT FOUND", C_ERROR))

        # scapy (Dragon Drain, MITM)
        try:
            import scapy
            _sv = getattr(scapy, "VERSION", "?")
            checks.append((f"  scapy {_sv}", C_SUCCESS))
        except ImportError:
            checks.append(("  scapy — not found (Dragon Drain/MITM)", C_WARNING))

        # cryptography (MeshCore AES)
        try:
            import cryptography
            checks.append((f"  cryptography {cryptography.__version__}", C_SUCCESS))
        except ImportError:
            checks.append(("  cryptography — not found (MeshCore)", C_WARNING))

        # LoRaRF (LoRa SX1262)
        try:
            import LoRaRF
            checks.append(("  LoRaRF (SX1262)", C_SUCCESS))
        except ImportError:
            checks.append(("  LoRaRF — not found (LoRa radio)", C_WARNING))

        checks.append(("", 0))
        checks.append(("HARDWARE", C_TEXT))
        checks.append(("", 0))

        # ESP32
        if self._esp32:
            fw_info = f" (FW: v{self._fw_version})" if self._fw_version else ""
            checks.append((f"  ESP32 on {self._boot_serial_port}{fw_info}", C_SUCCESS))
        else:
            checks.append(("  ESP32 — not connected", C_WARNING))

        # GPS
        if self.gps.available:
            checks.append((f"  GPS transport: {self.gps.device}", C_SUCCESS))
        else:
            reason = getattr(self.gps, "status_reason", "")
            checks.append(("  GPS — " + (reason[:66] if reason else "not found"), C_WARNING))

        # AIO v2 (GPIO power control)
        if self._aio_available:
            _gps_st = "ON" if self._gps_enabled else "OFF"
            _lora_st = "ON" if self._lora_enabled else "OFF"
            checks.append((f"  AIO v2 — GPS:{_gps_st}  LoRa:{_lora_st}", C_SUCCESS))
        else:
            checks.append(("  AIO v2 — not detected", C_DIM))

        # pinctrl (GPIO toggling)
        import shutil
        if shutil.which("pinctrl"):
            checks.append(("  pinctrl available", C_SUCCESS))
        else:
            checks.append(("  pinctrl — not found (GPIO toggle)", C_WARNING))

        # SDR tools
        _sdr_tools = []
        if SDRManager.has_dump1090():
            _sdr_tools.append("dump1090")
        if shutil.which("rtl_433"):
            _sdr_tools.append("rtl_433")
        if _sdr_tools:
            checks.append((f"  SDR: {' + '.join(_sdr_tools)}", C_SUCCESS))
        else:
            checks.append(("  SDR: dump1090/rtl_433 not found", C_WARNING))

        # Loot
        loot_ok = self.loot is not None
        if loot_ok:
            checks.append(("  Loot manager ready", C_SUCCESS))
        else:
            checks.append(("  Loot manager — FAILED", C_ERROR))

        # Tile maps
        has_tiles = self.tile_renderer and self.tile_renderer.has_tiles()
        if has_tiles:
            checks.append(("  Offline map tiles loaded", C_SUCCESS))
        else:
            checks.append(("  Offline map — none (SYSTEM>Download Map)", C_DIM))

        checks.append(("", 0))
        checks.append(("=" * 40, C_DIM))

        # Check if any FAIL
        has_errors = any("NOT FOUND" in t or "FAILED" in t for t, c in checks if c == C_ERROR)
        if has_errors:
            checks.append(("ERRORS DETECTED — run setup.sh", C_ERROR))
            checks.append(("Press [ENTER] to continue anyway", C_DIM))
            checks.append(("Press [ESC] to quit", C_DIM))
        else:
            checks.append(("ALL SYSTEMS GO", C_SUCCESS))
            checks.append(("Starting in 2s... [ENTER] to skip", C_DIM))

        self._boot_checks = checks
        self._boot_has_errors = has_errors

    def _finish_boot(self):
        """Transition from boot screen to main game."""
        self._boot_phase = False
        port = self._boot_serial_port

        self._term_add("[SYS] ESP32 Watch Dogs v1.0", raw=True)
        self._term_add("[SYS] TAB=menu  SPACE=hack  =/]=zoom+  -/[=zoom-", raw=True)
        if self._esp32:
            self._term_add(f"[OK] ESP32 connected on {port}", raw=True)
            # Probe firmware version
            if not self._fw_version:
                try:
                    self.serial.send_command("version")
                except Exception:
                    pass
        else:
            self._term_add("[WARN] No ESP32 — serial offline", raw=True)
        if self.gps.available:
            self._term_add(f"[OK] GPS on {self.gps.device}", raw=True)
        else:
            reason = getattr(self.gps, "status_reason", "")
            self._term_add("[WARN] GPS " + (reason or "not found"), raw=True)

        self.msg("[SYS] ESP32 Watch Dogs", C_HACK_CYAN)
        self.msg("[SYS] TAB=menu  `=loot  SPACE=hack", C_DIM)

        self._refresh_loot_points()
        self._loot_points_ts = time.time()

    # Big pixel font for boot logo (5x7 bitmap per letter)
    _LOGO_FONT = {
        'W': ["10001","10001","10001","10101","10101","11011","10001"],
        'A': ["01110","10001","10001","11111","10001","10001","10001"],
        'T': ["11111","00100","00100","00100","00100","00100","00100"],
        'C': ["01110","10001","10000","10000","10000","10001","01110"],
        'H': ["10001","10001","10001","11111","10001","10001","10001"],
        'D': ["11100","10010","10001","10001","10001","10010","11100"],
        'O': ["01110","10001","10001","10001","10001","10001","01110"],
        'G': ["01110","10001","10000","10011","10001","10001","01110"],
        'S': ["01110","10001","10000","01110","00001","10001","01110"],
    }

    def _draw_logo_word(self, text, x0, y0, cw, ch, color, gx=0):
        """Render word using big pixel font with optional glitch offset."""
        cx = x0 + gx
        for letter in text:
            bmap = self._LOGO_FONT.get(letter)
            if not bmap:
                cx += cw * 3
                continue
            for ri, row in enumerate(bmap):
                for ci, bit in enumerate(row):
                    if bit == '1':
                        pyxel.rect(cx + ci * cw, y0 + ri * ch,
                                   cw - 1, ch - 1, color)
            cx += 6 * cw  # 5 cols + 1 gap

    def _draw_boot_screen(self):
        """Draw the startup check / boot screen with cyber glitch logo."""
        pyxel.cls(0)
        frame = self._boot_frame

        # Dark scanlines
        for y in range(0, H, 3):
            pyxel.line(0, y, W - 1, y, 1)

        # ── Logo: single line WATCH_DOGS_GO ──
        cw, ch = 6, 7  # cell size (fits 13 chars + 2 spaces on 640px)
        word = "WATCH DOGS GO"
        # Calculate total width
        logo_w = 0
        for c in word:
            if c == ' ':
                logo_w += cw * 3
            else:
                logo_w += 6 * cw
        x1 = (W - logo_w) // 2
        y1 = 25
        gx1 = (frame % 7) - 3 if frame % 19 < 3 else 0
        self._draw_logo_word(word, x1 + 2, y1 + 2, cw, ch, 1)  # shadow
        self._draw_logo_word(word, x1, y1, cw, ch, C_HACK_CYAN, gx1)

        # ── Glitch artifacts ──
        logo_bottom = y1 + 7 * ch
        if frame % 11 < 2:
            gy = random.randint(y1, logo_bottom)
            gw = random.randint(40, 200)
            gx = random.randint(0, W - gw)
            pyxel.rect(gx, gy, gw, 2,
                       random.choice([C_HACK_CYAN, C_ERROR, 1]))
        for _ in range(6):
            nx = random.randint(x1 - 15, x1 + logo_w + 15)
            ny = random.randint(y1 - 5, logo_bottom + 5)
            pyxel.pset(nx, ny, random.choice([C_HACK_CYAN, C_DIM, 1]))

        # ── Subtitle ──
        sub_y = logo_bottom + 8
        pyxel.text((W - 36) // 2 + 40, sub_y, "by LOCOSP", C_DIM)
        pyxel.text((W - 64) // 2, sub_y + 12, "ESP32-C5 Edition", C_TEXT)

        # ── System checks (bottom, auto-scroll) ──
        visible = min(len(self._boot_checks), frame // 4 + 1)
        cx0 = 30
        cy0 = sub_y + 30
        max_y = H - 14  # bottom limit for text
        max_visible_lines = (max_y - cy0) // 10
        # Auto-scroll: if more lines than fit, scroll up
        scroll_off = max(0, visible - max_visible_lines)

        pyxel.text(cx0, cy0 - 10, "SYSTEM CHECK", C_HACK_CYAN)

        for i in range(scroll_off, visible):
            text, color = self._boot_checks[i]
            if not text:
                continue
            y = cy0 + (i - scroll_off) * 10
            if y > max_y:
                break
            if i == visible - 1 and frame % 20 < 10:
                pyxel.text(cx0 + len(text) * 4 + 2, y, "_", C_HACK_CYAN)
            pyxel.text(cx0, y, text, color)

        # ── Progress bar ──
        bar_y = H - 8
        total = len(self._boot_checks)
        pct = min(visible / total, 1.0) if total > 0 else 1.0
        bar_w = int((W - 60) * pct)
        pyxel.rect(30, bar_y, bar_w, 3, C_HACK_CYAN)
        pyxel.rectb(30, bar_y, W - 60, 3, C_DIM)

    def msg(self, text, color=C_TEXT):
        self.msgs.append((text, 180, color))
        if len(self.msgs) > 8:
            self.msgs.pop(0)

    def _attack_msg(self, text, color=C_DIM):
        """Callback for Python-native attacks (thread-safe msg + terminal)."""
        self.msg(text, color)
        self._term_add(text, raw=True)

    def _mitm_msg(self, text, color=C_TEXT):
        """Callback for MITM attack — logs to MITM screen."""
        with self._mitm_lock:
            self._mitm_log.append((text, color))
            if len(self._mitm_log) > 500:
                self._mitm_log = self._mitm_log[-500:]

    def _try_reconnect_esp32(self) -> bool:
        """Try to detect and reconnect ESP32. Like JanOS wait_for_esp32."""
        if getattr(self, '_flash_io_active', False):
            return False
        target = getattr(self, '_reconnect_flash_target', None)
        if target:
            try:
                port = target.resolve()
            except RuntimeError:
                return False
        else:
            port = detect_esp32_port()
        if not port:
            return False
        try:
            self.serial = SerialManager(port)
            self.serial.setup()
            self._esp32 = True
            self.state.connected = True
            self._boot_serial_port = port
            self._reconnect_flash_target = None
            self._flash_target = None
            self._term_add(f"[SYS] ESP32 found on {port}", raw=True)
            # Probe firmware version
            if not self._fw_version:
                try:
                    self.serial.send_command("version")
                except Exception:
                    pass
            return True
        except Exception as e:
            self._term_add(f"[ERR] Reconnect failed: {e}", raw=True)
            return False

    def _check_fw_update(self) -> None:
        """Check only the fork's published firmware releases."""
        from .updates import latest_release, release_version
        try:
            data = latest_release()
            remote = data["tag_name"].removeprefix("v")
            self._fw_remote_version = remote
            if self._fw_version:
                self._fw_update_available = release_version(remote) > release_version(self._fw_version)
                if self._fw_update_available:
                    self.msg(f"[FW] Smethan fork v{remote} available", C_WARNING)
                    self._term_add(f"[FW] Smethan fork update: v{self._fw_version} -> v{remote}", raw=True)
        except Exception as exc:
            self._term_add(f"[FW] Fork update check unavailable: {exc}", raw=True)

    def _send(self, cmd: str):
        # Session lease renewals are transport housekeeping, not user actions.
        quiet = cmd.startswith("wardrive_keepalive ")
        if getattr(self, '_flash_io_active', False):
            if not quiet:
                self._term_add("[FLASH] Serial reserved until flashing finishes", raw=True)
            return
        if cmd == "stop" and hasattr(self, "wardrive"):
            self.wardrive.on_stop()
        if self.serial and self.serial.is_open:
            self.serial.send_command(cmd)
            if not quiet:
                self._term_add(f"[TX] {cmd}", raw=True)
        elif self._try_reconnect_esp32():
            self.serial.send_command(cmd)
            if not quiet:
                self._term_add(f"[TX] {cmd} (after reconnect)", raw=True)
        else:
            self._term_add("[ERR] No serial connection", raw=True)

    # ------------------------------------------------------------------
    # Terminal helpers
    # ------------------------------------------------------------------

    def _term_add(self, line: str, raw=False):
        """Add line to terminal. raw=True skips filtering."""
        if not raw:
            if not self._term_filter(line):
                return
        color = _color_for_terminal_line(line)
        with self._term_lock:
            self.terminal_lines.append(line)
            self._terminal_colors.append(color)
            if len(self.terminal_lines) > 500:
                self.terminal_lines = self.terminal_lines[-500:]
                self._terminal_colors = self._terminal_colors[-500:]

    def _term_filter(self, s: str) -> bool:
        """Return True if line should be shown in terminal."""
        s = s.strip()
        if s in _TERM_SKIP_EXACT:
            return False
        # Echo of sent commands
        if s.startswith("> ") and len(s) < 40:
            return False
        for prefix in _TERM_SKIP_PREFIX:
            if s.startswith(prefix):
                return False
        for substr in _TERM_SKIP_CONTAIN:
            if substr in s:
                return False
        # Long base64 blobs (outside HS capture)
        if not self.capturing_hs:
            if len(s) > 50 and all(
                c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n"
                for c in s
            ):
                return False
            if s.startswith("---") and ("PCAP" in s or "HCCAPX" in s):
                return False
        # Raw hex dumps (sniffer data without useful keywords)
        if len(s) > 30 and s.count(":") > 5 and not any(
            kw in s for kw in ("RSSI", "SSID", "Name", "AP", "BSSID")
        ):
            return False
        return True

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update(self):
        # --- Boot screen phase ---
        if self._boot_phase:
            self._boot_frame += 1
            if pyxel.btnp(pyxel.KEY_RETURN):
                self._finish_boot()
            elif pyxel.btnp(pyxel.KEY_ESCAPE) and self._boot_has_errors:
                pyxel.quit()
            elif not self._boot_has_errors and self._boot_frame >= 300:
                # Auto-skip after 4s if no errors
                self._finish_boot()
            return

        self._poll_gps()
        self._poll_serial()
        self._poll_lora()
        self._poll_meshtastic()
        self._poll_meshtastic_update()
        self._poll_sdr()
        self._poll_watch()
        self._poll_wpasec_result()
        self._breath = (self._breath + 1) % 120

        # Camera follows player
        self.proj.smooth_move(self.player_lat, self.player_lon)
        self.proj.update()
        map_now = time.monotonic()
        self._tick_map_display(map_now)
        if self.wardrive.show_network_dots():
            wifi_view, ble_view = self._get_live_map_views(map_now)
            history_layer = self._get_history_node_layer()
            map_loot = self._get_map_loot_points()
            history_layer.request(
                map_loot, self.proj,
                getattr(self, "_map_loot_view_revision", 0))
            history_layer.step()
            live_layer = self._get_live_node_layer()
            notable, notable_revision = self.wardrive.live_notable_identities()
            live_layer.request(
                wifi_view, ble_view,
                (self._map_observations.revision, notable_revision),
                notable, self.proj)
            live_layer.step()
            radar_scale = 20 / max(self.proj.lon_span * 0.5, 0.001)
            radar_layer = self._get_radar_node_layer()
            radar_layer.request(
                wifi_view, ble_view, map_loot,
                (self._map_observations.revision, notable_revision), notable,
                self.player_lat, self.player_lon, radar_scale)
            radar_layer.step()

        self.scan_pulse = (self.scan_pulse + 1) % 60

        self.wardrive.tick()
        if self._pending_cmd and time.monotonic() > self._pending_deadline:
            pending_state = getattr(self, "_pending_state", "")
            self._pending_cmd = None
            if pending_state in ("wl_wifi_scan", "wl_ble_scan"):
                self._wl_scan_fail("Stop not confirmed; scan cancelled")
            else:
                self.msg("[ERR] Stop not confirmed; command cancelled. Retry STOP.", C_ERROR)

        if not getattr(self, '_ota_screen', False):
            self._update_hack()
        self._update_wardriving_loop()
        if getattr(self, '_ota_screen', False):
            self._update_ota_screen()
            return
        if self.wardrive.settings_open:
            self.wardrive.update_settings()
            return
        if self.wardrive.capture_screen.open:
            self.wardrive.capture_screen.update()
            self.msgs = [(t, tm-1, c) for t, tm, c in self.msgs if tm > 1]
            return
        if self.wardrive.hs_screen.open:
            self.wardrive.hs_screen.update()
            # Keep transient messages aging while the capture screen is open.
            self.msgs = [(t, tm-1, c) for t, tm, c in self.msgs if tm > 1]
            return

        # Particles
        for p in self.particles:
            p.x += p.vx; p.y += p.vy; p.life -= 1
        self.particles = [p for p in self.particles if p.life > 0]
        self.msgs = [(t, tm-1, c) for t, tm, c in self.msgs if tm > 1]

        if self.glitch_timer > 0:
            self.glitch_timer -= 1
        if pyxel.frame_count % 15 == 0:
            self.scan_lines = [random.randint(0, H-1) for _ in range(random.randint(0, 2))]

        # Refresh historical files away from the Pyxel update thread.  Large
        # wardrive archives otherwise freeze input and serial polling here.
        self._poll_loot_refresh()
        if time.time() - self._loot_points_ts > 30:
            self._start_loot_refresh()
            self._save_xp_if_dirty()
            self._battery_pct = self._read_battery()
            self._loot_points_ts = time.time()

        # Keys
        # Loot screen toggle (backtick / grave accent)
        _bq = getattr(pyxel, "KEY_BACKQUOTE", getattr(pyxel, "KEY_GRAVE_ACCENT", 96))
        if pyxel.btnp(_bq):
            self.loot_screen = not self.loot_screen
            if not self.loot_screen:
                self._loot_pwd_screen = False

        # Overlay screens — block normal input
        if self._et_net_screen or self._portal_select_screen:
            self._update_picker_overlay()
        elif self.input_mode:
            self._update_input_dialog()
        elif self._mitm_screen:
            self._update_mitm_screen()
        elif self._flipper_screen:
            self._update_flipper_screen()
        elif getattr(self, '_flash_screen', False):
            self._update_flash_screen()
        elif any(getattr(p, 'overlay_active', False) for p in self._plugins):
            for p in self._plugins:
                if getattr(p, 'overlay_active', False):
                    p.on_update()
                    break
        elif self._watch_screen:
            self._update_watch_screen()
        elif self._wl_screen:
            self._update_wl_screen()
        elif self._mc_screen:
            self._update_mc_screen()
        elif self._mc_region_screen:
            self._update_mc_region_picker()
        elif self._cluster_popup:
            self._update_cluster_popup()
        elif self.loot_screen:
            self._update_loot_screen()
        else:
            if pyxel.btnp(pyxel.KEY_TAB):
                self.menu_open = not self.menu_open
                if self.menu_open:
                    self.menu_sel = 0

            if self.menu_open:
                self._update_menu()
            else:
                # Cluster keyboard navigation (C = enter/exit, arrows, ENTER)
                self._handle_cluster_nav()
                # Check Python-native attack key handling first
                if self._attack_mode:
                    for _k in range(pyxel.KEY_1, pyxel.KEY_9 + 1):
                        if pyxel.btnp(_k):
                            if self._handle_attack_key(_k):
                                pass  # consumed
                    for _k in (pyxel.KEY_S, pyxel.KEY_C, pyxel.KEY_E,
                               pyxel.KEY_H, pyxel.KEY_L, pyxel.KEY_R,
                               pyxel.KEY_X, pyxel.KEY_ESCAPE):
                        if pyxel.btnp(_k):
                            if self._handle_attack_key(_k):
                                pass  # consumed

                if (pyxel.btnp(pyxel.KEY_PLUS) or pyxel.btnp(pyxel.KEY_KP_PLUS)
                        or pyxel.btnp(pyxel.KEY_EQUALS)
                        or pyxel.btnp(pyxel.KEY_RIGHTBRACKET)):
                    self.proj.zoom_in()
                if (pyxel.btnp(pyxel.KEY_MINUS) or pyxel.btnp(pyxel.KEY_KP_MINUS)
                        or pyxel.btnp(pyxel.KEY_LEFTBRACKET)):
                    self.proj.zoom_out()
                if pyxel.btnp(pyxel.KEY_0):
                    self.proj.reset_view()
                if pyxel.btnp(pyxel.KEY_S) and not self._attack_mode:
                    self._send("stop")
                    self.wifi_scanning = False
                    self.ble_scanning = False
                    self._wifi_scan_only = False
                    self._ble_scan_only = False
                    self._bt_tracking = False
                    self._bt_airtag = False
                    self.sniffing = False
                    self.capturing_hs = False
                    self._wifi_scan_done_time = 0.0
                    self._bt_scan_done_time = 0.0
                    self._gps_wait = False
                    self._gps_wait_cmd = ""
                    # Stop Python-native attacks
                    if self._dragon_drain.running:
                        self._dragon_drain.stop()
                    if self._mitm.running:
                        self._mitm.stop()
                    if self._blueducky.running:
                        self._blueducky.stop()
                    if self._race.running:
                        self._race.stop()
                    self._attack_mode = ""
                    self._attack_step = ""
                    self._attack_scan_results = []
                    self.msg("[STOP] All operations stopped", C_WARNING)
                    self.glitch_timer = 3
                # Manual pan (disabled when cluster is selected — arrows navigate clusters)
                if self._cluster_sel < 0:
                    speed = self.proj.lon_span / W * 3
                    if pyxel.btn(pyxel.KEY_UP):
                        self.player_lat += speed; self._manual_move = True
                    if pyxel.btn(pyxel.KEY_DOWN):
                        self.player_lat -= speed; self._manual_move = True
                    if pyxel.btn(pyxel.KEY_LEFT):
                        self.player_lon -= speed; self._manual_move = True
                    if pyxel.btn(pyxel.KEY_RIGHT):
                        self.player_lon += speed; self._manual_move = True
                self.player_lat = max(-85, min(85, self.player_lat))

            # Terminal scroll
            if pyxel.btnp(pyxel.KEY_PAGEUP):
                with self._term_lock:
                    self.term_scroll = min(self.term_scroll + 5,
                                           max(0, len(self.terminal_lines) - 5))
            if pyxel.btnp(pyxel.KEY_PAGEDOWN):
                self.term_scroll = max(0, self.term_scroll - 5)
            if pyxel.btnp(pyxel.KEY_END):
                self.term_scroll = 0

        # GPS wait dialog (Y=wait, N=cancel)
        if self._gps_wait_dialog:
            if pyxel.btnp(pyxel.KEY_Y):
                self._gps_wait_dialog = False
                self._gps_wait = True
                self.msg("[GPS] Waiting for fix...", C_WARNING)
            elif pyxel.btnp(pyxel.KEY_N) or pyxel.btnp(pyxel.KEY_ESCAPE):
                self._gps_wait_dialog = False
                self._gps_wait = False
                self._gps_wait_cmd = ""
                self.msg("[GPS] Cancelled", C_DIM)
        elif self.confirm_quit:
            if pyxel.btnp(pyxel.KEY_Y):
                self._cleanup()
                pyxel.quit()
            if pyxel.btnp(pyxel.KEY_N) or pyxel.btnp(pyxel.KEY_ESCAPE):
                self.confirm_quit = False
        elif pyxel.btnp(pyxel.KEY_ESCAPE):
            if self._esc_consumed_frame == pyxel.frame_count:
                pass  # ESC already handled by sub-screen this frame
            elif any(hasattr(p, '_auth_pin_active') and p._auth_pin_active()
                     for p in self._plugins):
                # Dismiss plugin PIN popup — remember token so it doesn't reappear
                for p in self._plugins:
                    if hasattr(p, '_auth_pin_active') and p._auth_pin_active():
                        p._auth_dismissed_token = getattr(p, '_auth_current_token', '')
                        p._auth_pin = ""
                        p._auth_pin_expiry = 0
            elif self._mitm_screen:
                pass  # handled in _update_mitm_screen
            elif self._watch_screen:
                pass  # handled in _update_watch_screen
            elif self._wl_screen:
                pass  # handled in _update_wl_screen
            elif self._mc_screen:
                pass  # handled in _update_mc_screen
            elif self._gps_wait:
                self._gps_wait = False
                self._gps_wait_cmd = ""
                self.msg("[GPS] Wait cancelled", C_DIM)
            elif self.loot_screen:
                if self._loot_search_active:
                    self._loot_search_active = False
                elif self._loot_search_results or self._loot_search:
                    self._loot_search = ""
                    self._loot_search_results = []
                    self._loot_search_scroll = 0
                else:
                    self.loot_screen = False
            elif self.input_mode:
                pass  # handled in _update_input_dialog
            elif self.menu_open:
                self.menu_open = False
            else:
                self.confirm_quit = True

    def _update_menu(self):
        _, items = MENU_CATS[self.menu_cat]
        if pyxel.btnp(pyxel.KEY_LEFT):
            self.menu_cat = (self.menu_cat - 1) % len(MENU_CATS)
            self.menu_sel = 0
            return
        if pyxel.btnp(pyxel.KEY_RIGHT):
            self.menu_cat = (self.menu_cat + 1) % len(MENU_CATS)
            self.menu_sel = 0
            return
        _, items = MENU_CATS[self.menu_cat]
        if pyxel.btnp(pyxel.KEY_UP):
            self.menu_sel = (self.menu_sel - 1) % len(items)
            return
        if pyxel.btnp(pyxel.KEY_DOWN):
            self.menu_sel = (self.menu_sel + 1) % len(items)
            return
        if pyxel.btnp(pyxel.KEY_RETURN):
            self._activate_menu_item(self.menu_cat, self.menu_sel)
            return
        for item_idx, item in enumerate(items):
            hotkey = item[0]
            key = _menu_hotkey_key(hotkey)
            if key is not None and pyxel.btnp(key):
                self.menu_sel = item_idx
                self._activate_menu_item(self.menu_cat, item_idx)
                return

    def _menu_item_unavailable_reason(self, cmd: str, state_key: str) -> str:
        """Return a live reason an item cannot run, or an empty string.

        Menu availability must be derived from the same runtime switches used
        by the action itself.  The old renderer treated most Python-side
        commands as unavailable solely because their command began with ``_``;
        that left completed add-ons permanently grey even after their hardware
        was enabled.
        """
        if state_key in _MENU_WIP_STATES:
            return "Work in progress"
        if state_key in ("_sdr_adsb", "_sdr_433"):
            if not getattr(self, "_sdr_enabled", False):
                return "[SDR] Enable SDR first (SYSTEM > SDR)"
        if cmd == "_meshcore":
            protocol = self._mesh_protocol()
            if protocol == "meshtastic":
                if not self._lora_enabled:
                    return "[MT] Enable LoRa first"
                return ""
            watch = getattr(self, "_watch", None)
            if bool(getattr(watch, "connected", False)):
                return ""
            if not getattr(self, "_lora_enabled", False):
                return "[MC] Enable LoRa or connect PipBoy Watch"
            lora = getattr(self, "_lora", None)
            if (bool(getattr(lora, "running", False))
                    and getattr(lora, "mode", "") != "meshcore"):
                mode = getattr(lora, "mode", "") or "another mode"
                return f"[MC] LoRa busy in {mode}; stop it first"
        if cmd == "_meshcore_region" and hasattr(self, "wardrive"):
            if self.wardrive.settings.get("lora_protocol") != "meshcore":
                return "[MC] Region applies only when LoRa protocol is MeshCore"
        return ""

    def _activate_menu_item(self, cat_idx: int, item_idx: int):
        _, items = MENU_CATS[cat_idx]
        _hotkey, name, cmd, state_key, input_type = items[item_idx]
        unavailable = self._menu_item_unavailable_reason(cmd, state_key)
        if unavailable:
            self.msg(unavailable, C_WARNING)
            return
        if input_type:
            self.menu_open = False
            self._start_input_dialog(cat_idx, item_idx, input_type)
            return
        # Keep menu open for toggle actions (GPS, LoRa, SDR, USB)
        if state_key not in ("_gps_toggle", "_lora_toggle",
                             "_sdr_toggle", "_usb_toggle"):
            self.menu_open = False
        self._execute_item(cmd, state_key, name, [])

    def _start_input_dialog(self, cat_idx: int, item_idx: int, input_type: str):
        self._input_pending_cat = cat_idx
        self._input_pending_item = item_idx
        if input_type == "bssid_ch":
            self.input_fields = [{"label": "BSSID", "value": ""},
                                  {"label": "CH",    "value": ""}]
        elif input_type == "ssid":
            self.input_fields = [{"label": "SSID",  "value": ""}]
        elif input_type == "mac":
            self.input_fields = [{"label": "MAC",   "value": ""}]
        elif input_type == "text":
            self.input_fields = [{"label": "TEXT",  "value": ""}]
        else:
            self.input_fields = [{"label": "VALUE", "value": ""}]
        self.input_field_idx = 0
        self.input_mode = True

    def _execute_item(self, cmd: str, state_key: str, name: str,
                      field_values: list):
        if getattr(self, '_ota_reserved', False):
            self.msg('[OTA] Finish the OTA update and close its result first', C_WARNING)
            return
        if state_key == "_ota_esp":
            self._open_ota()
            return
        if state_key == "_update_app":
            if getattr(self, '_app_update_running', False):
                self.msg("[UPDATE] Already checking for updates", C_DIM)
                return
            self._app_update_running = True
            threading.Thread(target=self._update_app, daemon=True).start()
            return
        if state_key in ("_hs_capture_sd_menu", "_hs_capture_serial_menu"):
            self.wardrive.capture_screen.show("sd" if state_key == "_hs_capture_sd_menu" else "serial")
            return
        if state_key == "_hs_sniff_menu":
            self.wardrive.hs_screen.open = True
            return
        if state_key == "_wardrive_settings":
            self.wardrive.settings_open = True
            self.wardrive.settings_page = "main"
            return
        if state_key == "hs_sniff" and not self._is_running("hs_sniff"):
            scan = self.wardrive.scan
            if scan.hs_supported is not True:
                if scan.hs_supported is False:
                    self.msg("[HS SNIFF] Needs firmware with passive serial capture support.", C_WARNING)
                else:
                    scan.probe()
                    self.msg("[HS SNIFF] Checking firmware; retry in a few seconds.", C_WARNING)
                return
        if state_key == "all_wardrive_host" and not self._is_running(state_key) and not self.wardrive.scan.wifi_supported:
            scan = self.wardrive.scan
            if not self.serial or not self.serial.is_open:
                self.msg("[WDG] ESP32 disconnected; reconnect and retry All Wardrive.", C_WARNING)
            elif scan.wifi_supported is False:
                self.msg("[WDG] Host BLE mode needs firmware 1.7.3+ for WiFi-only serial.", C_WARNING)
            else:
                scan.probe()
                self.msg("[WDG] Checking firmware; retry All Wardrive in a few seconds.", C_WARNING)
            return
        if state_key in ("all_wardrive", "all_wardrive_test") and not self._is_running(state_key) and not self.wardrive.scan.supported:
            self.wardrive.scan.probe()
            self.msg("[WDG] Needs serial wardrive firmware; checking capabilities.", C_WARNING)
            return
        if state_key == "_stop_all":
            self._send("stop")
            self.wifi_scanning = False
            self.ble_scanning = False
            self._wifi_scan_only = False
            self._ble_scan_only = False
            self._bt_tracking = False
            self._bt_airtag = False
            self._airtag_count = 0
            self._smarttag_count = 0
            self.sniffing = False
            self.capturing_hs = False
            self._wifi_scan_done_time = 0.0
            self._bt_scan_done_time = 0.0
            self._gps_wait = False
            self._gps_wait_cmd = ""
            # Stop Python-native attacks
            if self._dragon_drain.running:
                self._dragon_drain.stop()
            if self._mitm.running:
                self._mitm.stop()
            if self._blueducky.running:
                self._blueducky.stop()
            if self._race.running:
                self._race.stop()
            if self.state.portal_running:
                self.state.reset_portal()
            if self.state.evil_twin_running:
                self.state.reset_evil_twin()
            self._et_scan_pending = False
            self._attack_mode = ""
            self._attack_step = ""
            self._attack_scan_results = []
            self.msg("[STOP] All operations stopped", C_WARNING)
            self.glitch_timer = 3
            return
        if state_key == "_reboot":
            self._send("reboot")
            self.msg("[SYS] Reboot command sent", C_DIM)
            return
        if state_key == "_dl_map":
            if self._map_downloading:
                self._map_download_cancel = True
                self.msg("[MAP] Cancelling download...", C_WARNING)
            else:
                self._start_map_download()
            return
        if state_key == "_bt_hid_wip":
            self.msg("[HID] BLE HID disabled — work in progress", C_WARNING)
            self.msg("[HID] Feature will return in a future update", C_DIM)
            return
        if state_key == "_bd_wip":
            self.msg("[BD] BlueDucky disabled — work in progress", C_WARNING)
            self.msg("[BD] Feature will return in a future update", C_DIM)
            return
        if state_key == "_race_wip":
            self.msg("[RACE] RACE Attack disabled — work in progress", C_WARNING)
            self.msg("[RACE] Feature will return in a future update", C_DIM)
            return
        if state_key == "_gps_toggle":
            self._toggle_gps()
            return
        if state_key == "_lora_toggle":
            self._toggle_lora()
            return
        if state_key == "_sdr_toggle":
            self._toggle_sdr()
            return
        if state_key == "_sdr_adsb":
            self._sdr_adsb()
            return
        if state_key == "_sdr_433":
            self._sdr_433()
            return
        if state_key == "_watch":
            self._watch_connect()
            return
        if state_key == "_usb_toggle":
            self._toggle_usb()
            return
        if state_key == "_wl_screen":
            self._wl_screen = True
            self._wl_sel = 0
            self._wl_add_step = ""
            return
        if state_key == "_wpasec_up":
            self._wpasec_upload()
            return
        if state_key == "_wpasec_dl":
            self._wpasec_download()
            return
        if state_key == "_flash_esp":
            self._start_flash_esp32()
            return
        # Plugin commands: format is "_p_<idx>_<action>"
        if state_key and state_key.startswith("_p_"):
            rest = state_key[3:]
            idx_str, _, action = rest.partition("_")
            try:
                p_idx = int(idx_str)
            except ValueError:
                return
            if 0 <= p_idx < len(self._plugins):
                p = self._plugins[p_idx]
                try:
                    getattr(p, action, lambda: None)()
                except Exception as exc:
                    log.exception("Plugin dispatch error")
                    self.msg(f"[PLUGIN] Error: {exc}", C_ERROR)
            return
        if cmd.startswith("_"):
            self._handle_python_attack(cmd, state_key, name, field_values)
            return
        if not self._esp32:
            # Try reconnect before giving up
            if self._try_reconnect_esp32():
                self.msg("[SYS] ESP32 reconnected!", C_SUCCESS)
            else:
                self.msg(f"[ERR] No ESP32 — {name}", C_ERROR)
                return

        # GPS fix check — only wardriving modes (SNIFF tab) need GPS for loot
        _is_wardrive = state_key in ("wardriving", "bt_scanning", "all_wardrive", "all_wardrive_host", "all_wardrive_test")
        running = self._is_running(state_key)
        if _is_wardrive and not running and not self.gps_fix:
            # No GPS fix — show wait/cancel dialog
            final_cmd = (cmd + " " + " ".join(v for v in field_values if v)
                         if field_values else cmd)
            self._gps_wait_cmd = final_cmd
            self._gps_wait_state = state_key
            self._gps_wait_name = name
            self._gps_wait_dialog = True
            return

        # Build final command with optional params
        final_cmd = (cmd + " " + " ".join(v for v in field_values if v)
                     if field_values else cmd)
        if running:
            self._send("stop")
            self.msg(f"[STOP] {name}", C_DIM)
            self._set_running(state_key, False)
            # Reset rescan timers
            if state_key == "wardriving":
                self._wifi_scan_done_time = 0.0
            elif state_key == "bt_scanning":
                self._bt_scan_done_time = 0.0
        else:
            self._start_scan_cmd(final_cmd, state_key, name)

    def _anything_running_on_esp(self) -> bool:
        """True if any ESP32 operation is currently active."""
        return (self.wardrive.scan.active or self.wifi_scanning or self.ble_scanning
                or self._wifi_scan_only or self._ble_scan_only
                or getattr(self, "_wl_scan_phase", "idle") in
                   ("waiting_stop", "scanning_wifi", "scanning_ble", "stopping")
                or self._bt_tracking or self._bt_airtag
                or self.sniffing or self.capturing_hs
                or self.state.portal_running
                or self.state.evil_twin_running)

    def _clear_scan_state(self):
        self.wifi_scanning = self.ble_scanning = False
        self._wifi_scan_only = self._ble_scan_only = False
        self._wifi_scan_done_time = self._bt_scan_done_time = 0.0
        self._bt_scan_start_time = 0.0
        self._pending_cmd = None

    def _start_scan_cmd(self, final_cmd: str, state_key: str, name: str):
        # All transitions wait for firmware's final stop message, never just a timer.
        self._send("stop")
        self._clear_scan_state()
        self._pending_cmd = final_cmd
        self._pending_state = state_key
        self._pending_cmd_name = name
        self._pending_deadline = time.monotonic() + 10
        self.msg(f"[INIT] Stopping before {name}...", C_DIM)

    def _is_running(self, state_key: str) -> bool:
        return {
            "all_wardrive": self.wardrive.scan.active and self.wardrive.scan.mode == "wardrive" and not self.wardrive.scan.diagnostic and not self.wardrive.scan.wifi_only,
            "all_wardrive_host": self.wardrive.scan.active and self.wardrive.scan.mode == "wardrive" and self.wardrive.scan.wifi_only,
            "all_wardrive_test": self.wardrive.scan.active and self.wardrive.scan.diagnostic,
            "hs_sniff": self.wardrive.scan.active and self.wardrive.scan.mode == "hs_sniff",
            "_hs_sniff_menu": self.wardrive.scan.active and self.wardrive.scan.mode == "hs_sniff",
            "wardriving":    self.wifi_scanning,
            "bt_scanning":   self.ble_scanning,
            "wifi_scan":     self._wifi_scan_only,
            "ble_scan":      self._ble_scan_only,
            "bt_tracking":   self._bt_tracking,
            "bt_airtag":     self._bt_airtag,
            "sniffer":       self.sniffing,
            "handshake":     self.capturing_hs,
            "_hs_capture_sd_menu": self.wardrive.capture.runs["sd"].active,
            "_hs_capture_serial_menu": self.wardrive.capture.runs["serial"].active,
            "mitm":          self._mitm.running,
            "dragon_drain":  self._dragon_drain.running,
            "blueducky":     self._blueducky.running or self._blueducky.connected,
            "race":          self._race.running,
            "portal":        self.state.portal_running,
            "evil_twin":     self.state.evil_twin_running,
            "meshcore":      (
                self._meshtastic.connected
                if (hasattr(self, "wardrive") and
                    self.wardrive.settings.get("lora_protocol") == "meshtastic")
                else self._lora.running and self._lora.mode == "meshcore"),
            "_sdr_adsb":     self._sdr.running and "adsb" in self._sdr.mode,
            "_sdr_433":      self._sdr.running and "433" in self._sdr.mode,
            "_watch":        self._watch.connected,
            "_gps_toggle":   self._gps_enabled,
            "_lora_toggle":  self._lora_enabled,
            "_sdr_toggle":   self._sdr_enabled,
            "_usb_toggle":   self._usb_enabled,
        }.get(state_key, False)

    def _set_running(self, state_key: str, val: bool):
        if state_key == "wardriving":
            self.wifi_scanning = val
            if val:
                self._earn_badge("wardriver")
        elif state_key == "bt_scanning":
            self.ble_scanning = val
            if val:
                self._earn_badge("wardriver")
        elif state_key == "wifi_scan":    self._wifi_scan_only = val
        elif state_key == "ble_scan":     self._ble_scan_only  = val
        elif state_key in ("wl_wifi_scan", "wl_ble_scan"):
            if val:
                kind = "wifi" if state_key == "wl_wifi_scan" else "ble"
                self._wl_scan_phase = "scanning_" + kind
                self._wl_scan_deadline = time.monotonic() + (
                    45.0 if kind == "wifi" else 20.0)
        elif state_key == "bt_tracking":  self._bt_tracking    = val
        elif state_key == "bt_airtag":    self._bt_airtag      = val
        elif state_key == "sniffer":      self.sniffing        = val
        elif state_key in ("handshake", "handshake_start"): self.capturing_hs = val

    # ------------------------------------------------------------------
    # Python-native attacks (Dragon Drain, MITM, BlueDucky, RACE)
    # ------------------------------------------------------------------

    def _handle_python_attack(self, cmd: str, state_key: str, name: str,
                              field_values: list | None):
        """Handle Python-native attacks with full scan→select→run flow."""

        # ── Dragon Drain ──
        if cmd == "_dragon_drain":
            if self._dragon_drain.running:
                self._dragon_drain.stop()
                self._attack_mode = ""
                return
            # Step 1: detect monitor interface
            mon = self._dragon_drain.detect_monitor_ifaces()
            if not mon:
                managed = self._dragon_drain.detect_managed_ifaces()
                if managed:
                    self.msg(f"[DD] Enabling monitor on {managed[0][0]}...", C_WARNING)
                    self._dragon_drain.enable_monitor(managed[0][0])
                    mon = self._dragon_drain.detect_monitor_ifaces()
                if not mon:
                    self.msg("[DD] No monitor iface — plug in WiFi adapter", C_ERROR)
                    return
            # Step 2: scan for WPA3 APs in background
            self._attack_mode = "dragon_drain"
            self._attack_step = "scanning"
            self.msg("[DD] Scanning for WPA3 APs (10s)...", C_HACK_CYAN)

            def _scan():
                iface = mon[0]
                aps = self._dragon_drain.scan_wpa3(iface, duration=10)
                self._attack_scan_results = aps
                if aps:
                    self._attack_step = "select"
                    self.msg(f"[DD] {len(aps)} WPA3 AP(s) — press [1-9] to select", C_TEXT)
                    for i, ap in enumerate(aps[:9]):
                        s = ap['ssid'] or '<hidden>'
                        self._term_add(
                            f"  {i+1}. {ap['bssid']}  CH:{ap['channel']}  "
                            f"{ap['rssi']}dBm  {s}", raw=True)
                else:
                    self.msg("[DD] No WPA3 APs found", C_WARNING)
                    self._attack_mode = ""

            threading.Thread(target=_scan, daemon=True).start()
            return

        # ── MITM ── (dedicated sub-screen)
        if cmd == "_mitm":
            if self._mitm.running:
                self._mitm.stop()
                self._attack_mode = ""
                return
            self._mitm_screen = True
            self._mitm_state = "idle"
            self._mitm_log = []
            self.menu_open = False
            return

        # ── BlueDucky ──
        if cmd == "_blueducky":
            if self._blueducky.running or self._blueducky.connected:
                self._blueducky.stop()
                self._attack_mode = ""
                return
            self._attack_mode = "blueducky"
            self._attack_step = "menu"
            self.msg("[BD] [s]Scan [c]Connect(MAC) [r]RickRoll [x]Stop", C_HACK_CYAN)
            return

        # ── RACE Attack ──
        if cmd == "_race":
            if self._race.running:
                self._race.stop()
                self._attack_mode = ""
                return
            self._attack_mode = "race"
            self._attack_step = "menu"
            self.msg("[RACE] [s]Scan [c]Check [e]Extract [h]Hijack [l]Listen [x]Stop", C_HACK_CYAN)
            return

        # ── Evil Portal ── (standalone captive portal)
        if cmd == "_evil_portal":
            if not self._esp32:
                if not self._try_reconnect_esp32():
                    self.msg("[EP] No ESP32 — plug in device", C_ERROR)
                    return
            self._attack_mode = "evil_portal"
            self._attack_step = "input_ssid"
            self._portal_ssid = ""
            self.input_fields = [{"label": "Portal SSID", "value": "", "pos": 0}]
            self.input_field_idx = 0
            self.input_mode = True
            self._input_pending_cat = -7   # marker for Evil Portal SSID
            self._input_pending_item = -1
            return

        # ── Evil Twin ── (spoof existing network)
        if cmd == "_evil_twin":
            if not self._esp32:
                if not self._try_reconnect_esp32():
                    self.msg("[ET] No ESP32 — plug in device", C_ERROR)
                    return
            self._attack_mode = "evil_twin"
            # Check if we have fresh scan results (<30s)
            nets = self.state.networks
            if nets and (time.time() - self._wifi_scan_done_time) < 30:
                self._attack_scan_results = list(nets)
                self._show_net_selection()
            else:
                # Need to scan first
                self._attack_step = "scanning"
                self._et_scan_pending = True
                self.state.networks.clear()
                self._state_network_by_bssid.clear()
                self._send("scan_networks")
                self.msg("[ET] Scanning for networks...", C_HACK_CYAN)
            return

        # ── MeshCore Messenger ──
        if cmd == "_meshcore":
            protocol = self.wardrive.settings["lora_protocol"]
            if protocol == "meshtastic":
                if not self._lora_enabled:
                    self.msg("[MT] Enable LoRa first", C_WARNING)
                    return
                self._switch_lora_protocol(
                    "meshtastic", start_if_enabled=True)
                self._mc_screen = True
                self._mc_scroll = 0
                self.menu_open = False
                if not getattr(self, "_mt_help_shown", False):
                    self._mc_log.append((
                        "\x11 Meshtastic via local meshtasticd", C_HACK_CYAN))
                    self._mc_log.append((
                        "\x11 Shortcuts: Ctrl+A=discover  Ctrl+H=nodes  "
                        "Ctrl+C=channel  Ctrl+X=clear", 13))
                    self._mt_help_shown = True
                return
            watch_lora = bool(self._watch.connected)
            # GPIO power and the receiver thread are separate states.  If the
            # radio is powered but its thread stopped (for example after a
            # transient init failure), selecting Messenger retries MeshCore
            # instead of incorrectly telling the user to enable LoRa again.
            if self._lora_enabled and not self._lora.running:
                try:
                    self._lora.set_mc_channels(self._mc_channels_list)
                    self._lora.start_meshcore(self._mc_region)
                    self._term_add(
                        "[MC] Starting MeshCore radio handoff", raw=True)
                except Exception as exc:
                    self._term_add(f"[MC] MeshCore start failed: {exc}", raw=True)
            aio_lora = (
                self._lora_enabled and self._lora.running
                and self._lora.mode == "meshcore")
            if (self._lora_enabled and self._lora.running and not aio_lora
                    and not watch_lora):
                mode = self._lora.mode or "another mode"
                self.msg(f"[MC] LoRa busy in {mode}; stop it first", C_WARNING)
                return
            if not aio_lora and not watch_lora:
                if self._lora_enabled:
                    self.msg("[MC] MeshCore receiver failed; check LoRa log",
                             C_ERROR)
                else:
                    self.msg("[MC] Enable LoRa or connect PipBoy Watch",
                             C_WARNING)
                return
            if watch_lora and not aio_lora:
                # Start watch LoRa if not AIO
                self._watch.send_command("lora_start")
                self._watch_log_add("[Watch] LoRa started for MeshCore", C_HACK_CYAN)
            self._mc_screen = True
            self._mc_scroll = 0
            self.menu_open = False
            # Show keyboard shortcuts on first open of this session
            if not getattr(self, "_mc_help_shown", False):
                self._mc_log.append(
                    (f"\x11 Your node: {self._mc_node_name}", C_HACK_CYAN))
                self._mc_log.append(
                    ("\x11 Shortcuts: Ctrl+N=name  Ctrl+A=advert  "
                     "Ctrl+C=channel  Ctrl+X=clear", 13))
                self._mc_help_shown = True
            return

        # ── MeshCore Region picker ──
        if cmd == "_meshcore_region":
            from .lora_manager import MESHCORE_PRESETS
            keys = list(MESHCORE_PRESETS.keys())
            try:
                self._mc_region_sel = keys.index(self._mc_region)
            except ValueError:
                self._mc_region_sel = 0
            self._mc_region_screen = True
            self.menu_open = False
            return

        if cmd == "_flipper":
            if not self._flipper.connected:
                self._flipper_log = []
                self.msg("[FLIP] Connecting...", C_DIM)
                if not self._flipper.connect():
                    self.msg("[FLIP] Not found — plug in Flipper Zero USB",
                             C_ERROR)
                    return
                self.msg(f"[FLIP] {self._flipper.device_name} "
                         f"({self._flipper.firmware})", C_SUCCESS)
                self._flipper_log.append(
                    (f"Connected: {self._flipper.device_name} "
                     f"({self._flipper.firmware})", C_SUCCESS))
                self._flipper_log.append(
                    (f"Region: {self._flipper.region}", C_DIM))
            self._flipper_screen = True
            self._flipper_scroll = 0
            self._flipper_mode = "idle"
            # Start selection on first actionable item (skip separators)
            self._flipper_sel = 0
            for i, (_, act) in enumerate(self._FLIPPER_MENU):
                if act is not None:
                    self._flipper_sel = i
                    break
            self.menu_open = False
            return

        self.msg(f"[N/A] {name}: not yet implemented", C_DIM)

    def _handle_attack_key(self, key: int) -> bool:
        """Handle keypresses for active Python-native attack flows.
        Returns True if key was consumed."""
        import pyxel as px

        if not self._attack_mode:
            return False

        # ── Dragon Drain: select AP ──
        if self._attack_mode == "dragon_drain" and self._attack_step == "select":
            if px.KEY_1 <= key <= px.KEY_9:
                idx = key - px.KEY_1
                aps = self._attack_scan_results
                if idx < len(aps):
                    ap = aps[idx]
                    if self._whitelist.is_blocked(ap['bssid']):
                        self.msg(f"[WL] {ap['bssid'][-8:]} whitelisted", C_WARNING)
                        return True
                    mon = self._dragon_drain.detect_monitor_ifaces()
                    iface = mon[0] if mon else ""
                    self._dragon_drain.start(ap['bssid'], iface)
                    self._attack_step = "running"
                    self.msg(f"[DD] Attacking {ap['bssid']} CH:{ap['channel']}", C_WARNING)
                return True
            if key == px.KEY_ESCAPE:
                self._attack_mode = ""
                self._esc_consumed_frame = pyxel.frame_count
                self.msg("[DD] Cancelled", C_DIM)
                return True

        # ── MITM: now handled by dedicated _mitm_screen ──

        # ── BlueDucky menu ──
        if self._attack_mode == "blueducky":
            if key == px.KEY_S and not self._blueducky.running:
                self._blueducky.scan()
                self._attack_step = "scan_done"
                return True
            if key == px.KEY_C:
                # MAC input
                self.input_mode = True
                self.input_fields = [{"label": "Target BT MAC", "value": "", "pos": 0}]
                self.input_field_idx = 0
                self._input_pending_cat = -2  # marker for BlueDucky
                self._input_pending_item = -1
                return True
            if key == px.KEY_R:
                if self._blueducky.connected:
                    from .bt_ducky import RICKROLL_PAYLOAD
                    self._blueducky.execute_payload(RICKROLL_PAYLOAD, "Rick Roll")
                else:
                    self.msg("[BD] Connect first, then [r]", C_WARNING)
                return True
            if key == px.KEY_X:
                self._blueducky.stop()
                self._attack_mode = ""
                return True
            # Quick-select from scan (1-9)
            if px.KEY_1 <= key <= px.KEY_9 and self._blueducky.scanned_devices:
                idx = key - px.KEY_1
                devs = self._blueducky.scanned_devices
                if idx < len(devs):
                    addr, name = devs[idx]
                    if self._whitelist.is_blocked(addr):
                        self.msg(f"[WL] {name} whitelisted", C_WARNING)
                        return True
                    self._blueducky.connect(addr, name)
                return True
            if key == px.KEY_ESCAPE:
                self._blueducky.stop()
                self._attack_mode = ""
                self._esc_consumed_frame = pyxel.frame_count
                return True

        # ── RACE menu ──
        if self._attack_mode == "race":
            if key == px.KEY_S and not self._race.running:
                self._race.scan()
                return True
            if key == px.KEY_C:
                if self._race.scanned:
                    self._attack_step = "select_race"
                    self.msg("[RACE] Select device [1-9]:", C_HACK_CYAN)
                elif self._race.target_addr:
                    self._race.check(self._race.target_addr, self._race.target_name)
                else:
                    self.msg("[RACE] Scan first [s]", C_WARNING)
                return True
            if key == px.KEY_E:
                self._race.extract()
                return True
            if key == px.KEY_H:
                self._race.hijack()
                return True
            if key == px.KEY_L:
                self._race.listen()
                return True
            if key == px.KEY_X:
                self._race.stop()
                self._attack_mode = ""
                return True
            # Quick-select from scan (1-9)
            if px.KEY_1 <= key <= px.KEY_9 and self._race.scanned:
                idx = key - px.KEY_1
                if idx < len(self._race.scanned):
                    d = self._race.scanned[idx]
                    if self._whitelist.is_blocked(d["addr"]):
                        self.msg(f"[WL] {d.get('name','')} whitelisted", C_WARNING)
                        return True
                    self._race.check(d["addr"], d.get("name", ""))
                return True
            if key == px.KEY_ESCAPE:
                self._race.stop()
                self._attack_mode = ""
                self._esc_consumed_frame = pyxel.frame_count
                return True

        # ── Evil Portal / Evil Twin: running → stop / show data ──
        if (self._attack_mode in ("evil_portal", "evil_twin")
                and self._attack_step == "running"):
            if key == px.KEY_D:
                # Toggle captured data overlay
                self._portal_data_screen = not getattr(self, '_portal_data_screen', False)
                self._portal_data_scroll = 0
                return True
            if key == px.KEY_X or key == px.KEY_ESCAPE:
                self._send("stop")
                tag = "EP" if self._attack_mode == "evil_portal" else "ET"
                self.msg(f"[{tag}] Stopped", C_DIM)
                if self.state.portal_running:
                    self.state.reset_portal()
                if self.state.evil_twin_running:
                    self.state.reset_evil_twin()
                self._attack_mode = ""
                self._attack_step = ""
                self._portal_data_screen = False
                if key == px.KEY_ESCAPE:
                    self._esc_consumed_frame = pyxel.frame_count
                return True
            if key == px.KEY_UP and getattr(self, '_portal_data_screen', False):
                self._portal_data_scroll = max(0, self._portal_data_scroll - 1)
                return True
            if key == px.KEY_DOWN and getattr(self, '_portal_data_screen', False):
                self._portal_data_scroll += 1
                return True

        return False

    def _show_portal_selection(self, tag: str):
        """Show portal picker overlay. Tag = 'EP' or 'ET'."""
        loot_dir = Path(self._app_dir) / "loot"
        self._portal_list = get_all_portals(loot_dir)
        self._portal_sel = 0
        self._portal_select_screen = True
        self._attack_step = "select_portal"

    def _show_net_selection(self):
        """Show network picker overlay for Evil Twin."""
        self._et_net_sel = 0
        self._et_net_selected.clear()
        self._et_net_screen = True
        self._attack_step = "select_net"

    @staticmethod
    def _parse_post_fields(line: str) -> str:
        """Parse 'Received POST data: k=v&k2=v2' into formatted string.

        Returns e.g. 'Email: user@gmail.com | Password: secret123'
        or empty string if no recognized fields.
        """
        m = re.search(r'[Pp]ost data:\s*(.+)$', line)
        if not m:
            return ""
        try:
            from urllib.parse import parse_qs
            fields = {k: v[0] for k, v in parse_qs(m.group(1)).items()}
        except Exception:
            return ""
        parts = []
        for key in ("username", "email", "login", "user"):
            if key in fields:
                parts.append(f"{key.title()}: {fields[key]}")
        if "password" in fields:
            parts.append(f"Password: {fields['password']}")
        # Any other fields not already shown
        for k, v in fields.items():
            if k not in ("username", "email", "login", "user", "password"):
                parts.append(f"{k}: {v}")
        return " | ".join(parts)

    # ------------------------------------------------------------------
    # Map tile download
    # ------------------------------------------------------------------

    def _start_map_download(self):
        """Download OSM tiles around current GPS position (or manual pos)."""
        # Preflight: Stadia returns HTTP 401 for every request without an
        # API key, so without this guard the user just sees hundreds of
        # `HTTP 401 Unauthorized` lines in the terminal and no map. Bail
        # early with a clear pointer to the secrets.conf line + signup URL.
        from .tile_manager import _load_stadia_key
        if not _load_stadia_key():
            self.msg("[MAP] No Stadia API key — add STADIA_API_KEY to secrets.conf",
                     C_WARNING)
            self._term_add(
                "[MAP] Free signup: https://stadiamaps.com", raw=True)
            return
        if not self.gps_fix:
            self.msg("[MAP] No GPS fix — using current view center", C_WARNING)
        lat = self.player_lat
        lon = self.player_lon
        self._map_downloading = True
        self._map_download_cancel = False
        self.msg(f"[MAP] Downloading tiles ({lat:.2f}, {lon:.2f})...", C_HACK_CYAN)
        self._term_add(f"[MAP] Downloading 10km area around ({lat:.4f}, {lon:.4f})", raw=True)
        self._term_add("[MAP] Press [m] in SYSTEM to cancel", raw=True)

        def _dl_thread():
            maps_dir = Path(self._app_dir) / "maps"
            try:
                def cb(pct, msg):
                    if self._map_download_cancel:
                        raise InterruptedError("cancelled")
                    if pct % 10 < 1 or pct >= 99:
                        self._term_add(f"[MAP] {pct:.0f}% — {msg}", raw=True)

                manifest = download_tiles(lat, lon, maps_dir,
                                          radius_km=10.0, callback=cb)
                # Reload renderer
                if self.tile_renderer is None:
                    self.tile_renderer = TileRenderer(maps_dir)
                else:
                    self.tile_renderer.reload_manifest()
                self._term_add(
                    f"[MAP] Done! {manifest['tile_count']} tiles "
                    f"({manifest['errors']} errors)", raw=True)
                self.msg("[MAP] Tiles ready! Zoom in to see streets", C_SUCCESS)
            except InterruptedError:
                self._term_add("[MAP] Download cancelled by user", raw=True)
                self.msg("[MAP] Download cancelled", C_WARNING)
                # Reload whatever was downloaded so far
                if self.tile_renderer:
                    self.tile_renderer.reload_manifest()
            except Exception as e:
                self._term_add(f"[MAP] Error: {e}", raw=True)
                self.msg("[MAP] Download failed", C_ERROR)
            finally:
                self._map_downloading = False
                self._map_download_cancel = False

        threading.Thread(target=_dl_thread, daemon=True).start()

    # ------------------------------------------------------------------
    # GPS / LoRa toggle (AIO v2 GPIO or software)
    # ------------------------------------------------------------------

    def _begin_meshtastic_transition(self, state: str) -> bool:
        """Serialize service controls, updates, and shared-radio changes."""
        lock = getattr(self, "_meshtastic_transition_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._meshtastic_transition_lock = lock
        if not lock.acquire(blocking=False):
            return False
        self._meshtastic_transition_state = str(state)
        return True

    def _end_meshtastic_transition(self) -> None:
        lock = getattr(self, "_meshtastic_transition_lock", None)
        self._meshtastic_transition_state = ""
        if lock is not None and lock.locked():
            lock.release()

    def _meshtastic_transition_busy(self) -> bool:
        lock = getattr(self, "_meshtastic_transition_lock", None)
        return bool(lock is not None and lock.locked())

    def _meshtastic_bluetooth_busy_reason(self) -> str:
        """Return the live Bluetooth owner that blocks service mutation."""
        wardrive = getattr(self, "wardrive", None)
        host_ble = getattr(wardrive, "host_ble", None)
        if host_ble is not None and getattr(host_ble, "worker_active", False):
            return "an active host BLE scan"
        watch = getattr(self, "_watch", None)
        if watch is not None and getattr(watch, "worker_active", False):
            return "an active watch Bluetooth operation"
        watch_ready = getattr(self, "_watch_autoconnect_thread", None)
        if watch_ready is not None and watch_ready.is_alive():
            return "an active watch auto-connect readiness check"
        manager = getattr(self, "_meshtastic", None)
        if manager is not None:
            if getattr(manager, "ble_scan_lease_active", False):
                return "an active Bluetooth scan lease"
            if getattr(manager, "pairing_agent_lease_active", False):
                return "an active Bluetooth pairing lease"
        return ""

    def _begin_lora_async_start(self, action: str) -> int:
        """Return a generation token used to reject stale start completions."""
        self._lora_start_epoch = int(
            getattr(self, "_lora_start_epoch", 0)) + 1
        return self._lora_start_epoch

    def _track_lora_transition_thread(self, thread) -> None:
        """Retain every owner-changing worker until its exit is confirmed."""
        threads = getattr(self, "_lora_transition_threads", None)
        if threads is None:
            threads = []
            self._lora_transition_threads = threads
        if thread is not None and not any(item is thread for item in threads):
            threads.append(thread)

    def _forget_lora_transition_thread(self, thread) -> None:
        threads = getattr(self, "_lora_transition_threads", None)
        if threads is not None:
            self._lora_transition_threads = [
                item for item in threads if item is not thread]

    def _live_lora_transition_threads(self) -> list:
        """Return and retain every transition worker still able to mutate state."""
        candidates = list(getattr(self, "_lora_transition_threads", []) or [])
        current = getattr(self, "_lora_transition_thread", None)
        if (current is not None
                and not any(item is current for item in candidates)):
            candidates.append(current)
        live = []
        for thread in candidates:
            try:
                alive = bool(thread.is_alive())
            except Exception:
                # An uninspectable worker is conservatively still live.
                alive = True
            if alive:
                live.append(thread)
        self._lora_transition_threads = live
        return list(live)

    def _cancel_pending_lora_start(self, *, collector_only: bool = False) -> bool:
        """Invalidate a pending direct or daemon start without racing its exit.

        A completion may already be queued when the user turns LoRa or its
        wardrive collector off.  Advancing the epoch makes that completion
        stale; the poller then stops/releases the newly started owner instead
        of claiming it.
        """
        direct = getattr(self, "_lora_start_pending", "")
        handoff = getattr(self, "_lora_handoff_pending", None)
        direct_action = direct[0] if isinstance(direct, tuple) else direct
        handoff_action = handoff[0] if isinstance(handoff, tuple) else ""
        actions = {str(direct_action or ""), str(handoff_action or "")}
        if collector_only and not actions.intersection(("wardrive", "power_on")):
            return False
        pending = bool(
            direct or handoff
            or self._live_lora_transition_threads())
        if not pending:
            return False
        self._lora_start_epoch = int(
            getattr(self, "_lora_start_epoch", 0)) + 1
        return True

    def _lora_start_completion_allowed(self, action: str, epoch: int) -> bool:
        if epoch != int(getattr(self, "_lora_start_epoch", 0)):
            return False
        if not bool(getattr(
                self, "_lora_power_intent",
                getattr(self, "_lora_enabled", False))):
            return False
        if action in ("wardrive", "power_on"):
            wardrive = getattr(self, "wardrive", None)
            if (wardrive is None
                    or not wardrive.settings.get("wardrive_lora", False)):
                return False
        return True

    def _lora_poweroff_handles_cleanup(self) -> bool:
        """Whether the serialized power worker owns stop/release cleanup."""
        return bool(
            not getattr(self, "_lora_power_intent", True)
            and getattr(self, "_meshtastic_transition_state", "")
            == "lora_power")

    def _toggle_gps(self):
        new_state = not self._gps_enabled
        if self._aio_available:
            ok = AioManager.toggle("gps", new_state)
            if not ok:
                self.msg("[GPS] GPIO toggle failed", C_ERROR)
                return
        if new_state:
            # Enable GPS — re-open serial
            if not self.gps.available:
                self.gps.setup()
            if self.gps.available:
                self._gps_enabled = True
                self.msg("[GPS] ON", C_SUCCESS)
                self._term_add("[SYS] GPS enabled", raw=True)
            else:
                self.msg("[GPS] No device found", C_ERROR)
                self._gps_enabled = False
        else:
            # Disable GPS — close serial, clear fix
            if hasattr(self, "wardrive"):
                self.wardrive.cell.stop()
            self.gps.close()
            self._gps_enabled = False
            self.gps_fix = False
            self.gps_sats = 0
            self.gps_sats_vis = 0
            self.msg("[GPS] OFF", C_WARNING)
            self._term_add("[SYS] GPS disabled", raw=True)
        self.glitch_timer = 2

    def _toggle_lora(self):
        if getattr(self, "_lora_power_ownership_uncertain", False):
            self.msg(
                "[LoRa] SX1262 ownership is uncertain; restart WatchDogsGo",
                C_ERROR)
            return False
        bluetooth_busy = self._meshtastic_bluetooth_busy_reason()
        if bluetooth_busy:
            self.msg(
                "[MT] Finish " + bluetooth_busy + " before changing LoRa power",
                C_WARNING)
            return False
        if not self._begin_meshtastic_transition("lora_power"):
            self.msg("[MT] Another radio/service operation is running", C_WARNING)
            return False
        if self._lora_enabled:
            pending_start_threads = tuple(
                self._live_lora_transition_threads())
            self._lora_power_intent = False
            self._cancel_pending_lora_start()
            factory = getattr(
                self, "_lora_transition_thread_factory", threading.Thread)
            thread = factory(
                target=self._run_lora_power_off,
                args=(pending_start_threads,),
                name="wdg-lora-power-off", daemon=True)
            self._lora_transition_thread = thread
            self._track_lora_transition_thread(thread)
            try:
                thread.start()
            except Exception as exc:
                self._forget_lora_transition_thread(thread)
                self._lora_transition_thread = None
                self._lora_power_intent = self._lora_enabled
                self._end_meshtastic_transition()
                self.msg(
                    "[LoRa] Could not start power-off worker: "
                    + str(exc)[:90], C_ERROR)
                return False
            self.msg("[LoRa] Stopping radio owners...", C_DIM)
            return True
        try:
            self._lora_power_intent = True
            accepted = self._toggle_lora_locked()
        except Exception as exc:
            self.msg("[LoRa] Startup failed: " + str(exc)[:100], C_ERROR)
            self._lora_enabled = True
            self._lora_power_on_pending = True
            self._start_failed_lora_power_on_rollback()
            return False
        if not accepted and not self._lora_power_on_pending:
            self._lora_power_intent = self._lora_enabled
        if not self._lora_power_on_pending:
            self._end_meshtastic_transition()
        return accepted

    def _start_failed_lora_power_on_rollback(self) -> None:
        """Finish a rejected asynchronous power-on through the safe off path."""
        pending_start_threads = tuple(self._live_lora_transition_threads())
        self._lora_power_intent = False
        self._cancel_pending_lora_start()
        factory = getattr(
            self, "_lora_transition_thread_factory", threading.Thread)
        thread = factory(
            target=self._run_lora_power_off,
            args=(pending_start_threads,),
            name="wdg-lora-power-on-rollback", daemon=True)
        self._lora_transition_thread = thread
        self._track_lora_transition_thread(thread)
        try:
            thread.start()
        except Exception as exc:
            self._forget_lora_transition_thread(thread)
            self._lora_transition_thread = None
            self._lora_power_on_pending = False
            self._lora_power_intent = self._lora_enabled
            self.msg(
                "[LoRa] Startup failed and safe power rollback could not "
                "start: " + str(exc)[:90], C_ERROR)
            self._end_meshtastic_transition()

    def _run_lora_power_off(self, pending_start_threads=()):
        """Release all radio owners and cut the rail outside Pyxel's thread."""
        ok = False
        rail_off = False
        detail = "LoRa power-off did not complete"
        direct_active = False
        prior_direct_mode = ""
        prior_service_target = None
        service_suspended = False
        barrier = None
        if pending_start_threads is None:
            pending_start_threads = ()
        elif not isinstance(pending_start_threads, (tuple, list, set)):
            pending_start_threads = (pending_start_threads,)
        # Do not cut power, release the transition barrier, or begin another
        # service mutation while a displaced handoff can still finish.  The
        # helper calls used by those workers are bounded; waiting here keeps
        # an overrun fail-closed without blocking the Pyxel thread.
        for pending_thread in pending_start_threads:
            if (pending_thread is not None
                    and pending_thread is not threading.current_thread()):
                pending_thread.join()
        # A displaced handoff can restore a direct owner as its final rollback
        # action.  Sample ownership only after every such worker has exited;
        # the values from before the join are no longer authoritative.
        direct_active = (
            self._lora.running or self._lora.worker_active
            or self._lora.radio_owned)
        prior_direct_mode = self._lora.mode if direct_active else ""
        try:
            prior_service_target = self._meshtastic.active_service_target()
        except Exception:
            pass
        try:
            bluetooth_busy = self._meshtastic_bluetooth_busy_reason()
            if bluetooth_busy:
                detail = "cannot stop services while " + bluetooth_busy
            elif direct_active and not self._lora.stop():
                detail = "direct LoRa worker did not release the SX1262"
            else:
                # A successful direct-radio session retains the exact service
                # snapshot captured before it claimed SPI.  That token already
                # proves both daemons are persistently suspended, and replacing
                # it with a snapshot of the suspended state would destroy the
                # only trustworthy rollback path.
                service_suspended = self._meshtastic_service_restore_pending()
                if not service_suspended:
                    service_suspended = self._meshtastic.suspend_service(
                        timeout=10.0)
                if not service_suspended:
                    detail = "Meshtastic service did not release the SX1262"
                    if prior_direct_mode:
                        self._restore_lora_power_owner(
                            prior_direct_mode, None)
                    elif prior_service_target:
                        try:
                            if (self._meshtastic.active_service_target()
                                    != prior_service_target):
                                self._meshtastic.resume_service(
                                    timeout=15.0, connect=True,
                                    target=prior_service_target)
                        except Exception:
                            pass
                else:
                    factory = getattr(
                        self, "_lora_power_barrier_factory", RadioOwnership)
                    barrier = factory()
                    try:
                        barrier.acquire("WatchDogsGo LoRa power transition")
                    except RadioOwnershipBusy as exc:
                        restored = self._restore_lora_power_owner(
                            prior_direct_mode, prior_service_target)
                        detail = (
                            str(exc)[:120] + "; LoRa rail remains on; "
                            + ("previous owner restored" if restored else
                               "previous owner could not be restored"))
                    except Exception as exc:
                        restored = self._restore_lora_power_owner(
                            prior_direct_mode, prior_service_target)
                        detail = (
                            "could not acquire the SX1262 power barrier: "
                            + str(exc)[:100] + "; LoRa rail remains on; "
                            + ("previous owner restored" if restored else
                               "previous owner could not be restored"))
                    else:
                        if (self._aio_available
                                and not AioManager.toggle("lora", False)):
                            try:
                                barrier.release()
                            except Exception as exc:
                                self._mark_lora_power_ownership_uncertain(
                                    barrier, exc)
                                detail = (
                                    "GPIO power-off failed and the SX1262 "
                                    "ownership barrier could not be released; "
                                    "restart WatchDogsGo")
                            else:
                                restored = self._restore_lora_power_owner(
                                    prior_direct_mode, prior_service_target)
                                detail = (
                                    "GPIO power-off failed; the LoRa rail remains "
                                    "on; "
                                    + ("previous owner restored" if restored else
                                       "previous owner could not be restored"))
                        else:
                            rail_off = True
                            ok = True
                            detail = "LoRa disabled, mesh clients stopped"
        except Exception as exc:
            detail = "LoRa power-off failed: " + str(exc)[:140]
            barrier_released = True
            if barrier is not None and getattr(barrier, "held", False):
                try:
                    barrier.release()
                except Exception:
                    barrier_released = False
                    self._mark_lora_power_ownership_uncertain(barrier)
            if ((service_suspended or prior_direct_mode or prior_service_target)
                    and barrier_released):
                restored = self._restore_lora_power_owner(
                    prior_direct_mode, prior_service_target)
                detail += ("; previous owner restored" if restored else
                           "; previous owner could not be restored")
            elif not barrier_released:
                detail += "; ownership barrier could not be released"
        finally:
            if barrier is not None and getattr(barrier, "held", False):
                try:
                    barrier.release()
                except Exception as exc:
                    self._mark_lora_power_ownership_uncertain(barrier, exc)
                    ok = False
                    detail += (
                        "; ownership barrier release failed; SX1262 ownership "
                        "is uncertain; restart WatchDogsGo: " + str(exc)[:80])
        queue = getattr(self, "_lora_transition_queue", None)
        if queue is not None:
            queue.put((ok, detail, "power_off", {"rail_off": rail_off}))
        else:
            self._end_meshtastic_transition()

    def _meshtastic_service_restore_pending(self) -> bool:
        """Read the manager's authoritative retained-service snapshot flag."""
        pending = getattr(
            getattr(self, "_meshtastic", None),
            "service_restore_pending", False)
        try:
            return bool(pending() if callable(pending) else pending)
        except Exception:
            return False

    def _mark_lora_power_ownership_uncertain(
            self, barrier, error=None) -> None:
        """Retain an unreleased barrier and block every later radio owner."""
        self._lora_power_ownership_uncertain = True
        self._lora_power_uncertain_barrier = barrier
        if error is not None:
            self._term_add(
                "[LoRa] SX1262 barrier release could not be confirmed: "
                + str(error)[:120], raw=True)

    def _restore_lora_power_owner(
            self, direct_mode: str, service_target: str | None) -> bool:
        """Restore the exact owner displaced before a failed rail cut."""
        def direct_start_accepted(start) -> bool:
            epoch = self._begin_lora_async_start("restore")
            marker = ("restore", epoch)
            self._lora_start_pending = marker
            accepted = bool(start())
            if accepted:
                wardrive = getattr(self, "wardrive", None)
                if wardrive is not None:
                    wardrive._wdg_owned_lora = False
            elif self._lora_start_pending == marker:
                self._lora_start_pending = ""
            return accepted

        try:
            # A direct owner can only be reopened after the retained systemd
            # snapshot is restored.  Its worker will then suspend that exact
            # state again before reclaiming SPI, producing a fresh rollback
            # token for the restored direct session.
            if (direct_mode
                    and self._meshtastic_service_restore_pending()
                    and not self._meshtastic.resume_service(
                        timeout=15.0, connect=False)):
                return False
            if direct_mode == "meshcore":
                self._lora.set_mc_channels(self._mc_channels_list)
                return direct_start_accepted(
                    lambda: self._lora.start_meshcore(self._mc_region))
            if direct_mode == "meshtastic":
                return direct_start_accepted(self._lora.start_meshtastic)
            if direct_mode == "scanner":
                return direct_start_accepted(self._lora.start_scanner)
            if direct_mode == "tracker":
                return direct_start_accepted(self._lora.start_tracker)
            if direct_mode == "sniffer":
                config = getattr(self._lora, "_radio_cfg", None)
                if config and len(config) >= 6:
                    freq, sf, cr, bw, sync_word, preamble = config[:6]
                    return direct_start_accepted(lambda: self._lora.start_sniffer(
                        freq, sf, cr, bw, "Restored", sync_word, preamble))
                return False
            if (service_target
                    or self._meshtastic_service_restore_pending()):
                return bool(self._meshtastic.resume_service(
                    timeout=15.0, connect=True, target=service_target))
            return True
        except Exception:
            pending = getattr(self, "_lora_start_pending", "")
            if (isinstance(pending, tuple) and pending
                    and pending[0] == "restore"):
                self._lora_start_pending = ""
            return False

    def _toggle_lora_locked(self):
        new_state = not self._lora_enabled
        # Power-up must precede client startup. Power-down is the inverse:
        # release daemon/SPI owners before cutting the SX1262 rail.
        if new_state and self._aio_available:
            ok = AioManager.toggle("lora", new_state)
            if not ok:
                self.msg("[LoRa] GPIO toggle failed", C_ERROR)
                return False
        # Start/stop the selected mesh protocol.
        if new_state:
            self._lora_enabled = True
            self._lora_power_intent = True
            automatic = self.wardrive.settings["wardrive_lora"]
            protocol = self.wardrive.settings["lora_protocol"]
            if automatic:
                self._lora_power_on_pending = True
                accepted = self._switch_lora_protocol_locked(
                    protocol, start_if_enabled=True,
                    transition_action="power_on")
                if not accepted:
                    self.wardrive._wdg_owned_lora = False
                    self.msg("[LoRa] Startup rejected; restoring power OFF",
                             C_ERROR)
                    self._start_failed_lora_power_on_rollback()
                    return False
                self.msg(
                    f"[LoRa] ON — {protocol.title()} starting", C_SUCCESS)
            else:
                self.msg("[LoRa] ON — WDG auto-start disabled", C_SUCCESS)
                self._term_add(
                    "[SYS] LoRa powered; no WDG mesh client started",
                    raw=True)
        self.glitch_timer = 2
        return True

    def _lora_transition_active(self) -> bool:
        return bool(self._live_lora_transition_threads())

    def _run_meshtastic_handoff(self, action: str = "handoff",
                                epoch: int | None = None) -> None:
        """Release direct SPI, then restore the selected daemon off-thread."""
        ok = False
        detail = ""
        direct_released = False
        activation_attempted = False
        safe_to_restore_direct = True
        if epoch is None:
            epoch = int(getattr(self, "_lora_start_epoch", 0))
        prior_direct_mode = (
            getattr(self._lora, "mode", "")
            if (getattr(self._lora, "running", False)
                or getattr(self._lora, "worker_active", False)
                or getattr(self._lora, "radio_owned", False)) else "")
        try:
            bluetooth_busy = self._meshtastic_bluetooth_busy_reason()
            if bluetooth_busy:
                detail = "Meshtastic start blocked by " + bluetooth_busy
            elif not self._lora.stop():
                detail = (
                    "direct radio worker did not release the SX1262; "
                    "Meshtastic was not started")
            elif self._lora.radio_owned:
                detail = (
                    "direct radio ownership remained held; Meshtastic was "
                    "not started")
            else:
                direct_released = True
                # LoRaManager intentionally leaves the pre-direct service
                # snapshot retained for the lifetime of a successful direct
                # session.  Restore it before beginning a new transactional
                # backend selection; activate_backend_service() correctly
                # refuses to overwrite an unresolved snapshot.
                if (self._meshtastic_service_restore_pending()
                        and not self._meshtastic.resume_service(
                            timeout=15.0, connect=False)):
                    safe_to_restore_direct = False
                    detail = (
                        "previous Meshtastic service state could not be "
                        "restored; backend selection was not attempted")
                else:
                    mode = self._meshtastic.backend_mode
                    activation_attempted = True
                    if not self._meshtastic.activate_backend_service(
                            mode, timeout=15.0):
                        detail = "Meshtastic service did not become ready"
                    elif not self._meshtastic.start():
                        detail = "Meshtastic client did not start"
                    elif not self._meshtastic.wait_connected(timeout=15.0):
                        detail = (
                            "Meshtastic endpoint opened but protocol negotiation "
                            "did not complete")
                    else:
                        self._meshtastic.commit_backend_service_activation()
                        ok = True
                        detail = "Meshtastic service ready"
        except Exception as exc:
            detail = "Meshtastic handoff failed: " + str(exc)[:140]
        if not ok and activation_attempted:
            # Once service selection has begun, disconnecting the candidate
            # client is a hard barrier.  Never mutate systemd state or reopen
            # direct SPI while that worker might still issue requests.
            try:
                closed = bool(self._meshtastic.close())
            except Exception as exc:
                closed = False
                detail += "; Meshtastic client close failed: " + str(exc)[:100]
            if not closed:
                safe_to_restore_direct = False
                detail += (
                    "; Meshtastic client worker did not stop; service rollback "
                    "and direct-radio restore were blocked")
            else:
                try:
                    restored = bool(
                        self._meshtastic.rollback_backend_service_activation(
                            timeout=15.0))
                except Exception as exc:
                    restored = False
                    detail += "; service rollback failed: " + str(exc)[:100]
                if not restored:
                    safe_to_restore_direct = False
                    detail += "; previous service state could not be restored"
        if (not ok and prior_direct_mode and direct_released
                and safe_to_restore_direct):
            if self._restore_lora_power_owner(prior_direct_mode, None):
                detail += "; previous direct radio restored"
            else:
                detail += "; previous direct radio could not be restored"
        queue = getattr(self, "_lora_transition_queue", None)
        if queue is not None:
            queue.put((ok, detail, action, epoch))

    def _start_meshtastic_handoff(self, action: str = "handoff") -> bool:
        """Start one nonblocking direct-radio to daemon transition."""
        if getattr(self, "_lora_power_ownership_uncertain", False):
            self.msg(
                "[MT] SX1262 ownership is uncertain; restart WatchDogsGo",
                C_ERROR)
            return False
        if getattr(self, "_meshtastic_update_running", False):
            self.msg("[MT] Service update is running", C_WARNING)
            return False
        bluetooth_busy = self._meshtastic_bluetooth_busy_reason()
        if bluetooth_busy:
            self.msg("[MT] Finish " + bluetooth_busy + " first", C_WARNING)
            return False
        if self._lora_transition_active():
            return False
        if not hasattr(self, "_lora_transition_queue"):
            self._lora_transition_queue = Queue()
        epoch = self._begin_lora_async_start(action)
        marker = (action, epoch)
        self._lora_handoff_pending = marker
        try:
            factory = getattr(
                self, "_lora_transition_thread_factory", threading.Thread)
            thread = factory(
                target=self._run_meshtastic_handoff,
                args=(action, epoch),
                name="wdg-lora-to-meshtastic",
                daemon=True,
            )
            self._lora_transition_thread = thread
            self._track_lora_transition_thread(thread)
            thread.start()
        except Exception as exc:
            self._forget_lora_transition_thread(
                locals().get("thread"))
            self._lora_transition_thread = None
            if self._lora_handoff_pending == marker:
                self._lora_handoff_pending = None
            self._term_add(
                "[MT] Could not start radio handoff: " + str(exc)[:120],
                raw=True)
            return False
        return True

    def _switch_lora_protocol(self, protocol: str,
                              *, start_if_enabled: bool = True,
                              _transition_owned: bool = False,
                              _transition_action: str = "handoff"):
        """Release the old radio owner and activate the selected protocol."""
        bluetooth_busy = self._meshtastic_bluetooth_busy_reason()
        if bluetooth_busy:
            self.msg("[MT] Finish " + bluetooth_busy + " first", C_WARNING)
            return False
        acquired = False
        if not _transition_owned:
            acquired = self._begin_meshtastic_transition("lora_protocol")
            if not acquired:
                self.msg("[MT] Another radio/service operation is running",
                         C_WARNING)
                return False
        try:
            return self._switch_lora_protocol_locked(
                protocol, start_if_enabled=start_if_enabled,
                transition_action=_transition_action)
        finally:
            if acquired:
                self._end_meshtastic_transition()

    def _switch_lora_protocol_locked(self, protocol: str,
                                     *, start_if_enabled: bool = True,
                                     transition_action: str = "handoff") -> bool:
        if protocol not in ("meshcore", "meshtastic"):
            self._term_add(
                "[LoRa] Unsupported protocol selection: " + str(protocol)[:40],
                raw=True)
            return False
        if getattr(self, "_lora_power_ownership_uncertain", False):
            self.msg(
                "[LoRa] SX1262 ownership is uncertain; restart WatchDogsGo",
                C_ERROR)
            return False
        if getattr(self, "_meshtastic_update_running", False):
            self.msg("[MT] Service update is running; radio switch deferred",
                     C_WARNING)
            self._term_add(
                "[MT] Radio protocol changes are blocked during service update",
                raw=True)
            return False
        if not start_if_enabled:
            # Changing the preferred protocol while automatic ownership is
            # disabled must not stop a daemon another app may be using.
            if self._lora.running and self._lora.mode == "meshcore":
                if not self._lora.stop():
                    return False
            return bool(self._meshtastic.close())
        if protocol == "meshtastic":
            if self._lora_enabled and start_if_enabled:
                if self._start_meshtastic_handoff(transition_action):
                    self._term_add(
                        "[SYS] Releasing direct radio before Meshtastic start",
                        raw=True)
                    return True
                return False
            return True

        if self._lora_transition_active():
            self._term_add(
                "[MC] Waiting for the current radio handoff to finish",
                raw=True)
            return False

        # LoRaManager performs the daemon handoff in its worker before it
        # acquires process ownership and touches SPI/GPIO.  This also covers
        # direct starts initiated by All Wardrive.
        if self._lora_enabled and start_if_enabled:
            epoch = self._begin_lora_async_start(transition_action)
            marker = (transition_action, epoch)
            self._lora_start_pending = marker
            try:
                self._lora.set_mc_channels(self._mc_channels_list)
                started = self._lora.start_meshcore(self._mc_region)
                if not started:
                    if self._lora_start_pending == marker:
                        self._lora_start_pending = ""
                    self._term_add(
                        "[MC] Direct radio is already starting or active",
                        raw=True)
                    return False
                lat = self.player_lat if self.gps_fix else 0.0
                lon = self.player_lon if self.gps_fix else 0.0
                try:
                    self._lora.send_meshcore_advert(
                        self._mc_node_name, lat, lon)
                except Exception as exc:
                    self._term_add(
                        "[MC] Initial advert was not queued: "
                        + str(exc)[:100], raw=True)
                self._term_add(
                    "[SYS] MeshCore radio handoff starting", raw=True)
                return True
            except Exception as exc:
                if self._lora_start_pending == marker:
                    self._lora_start_pending = ""
                self._term_add(
                    "[MC] MeshCore start failed: " + str(exc)[:120], raw=True)
                return False
        return True

    def _toggle_sdr(self):
        new_state = not self._sdr_enabled
        if not self._aio_available:
            self.msg("[SDR] AIO v2 not available", C_ERROR)
            return
        ok = AioManager.toggle("sdr", new_state)
        if not ok:
            self.msg("[SDR] GPIO toggle failed", C_ERROR)
            return
        self._sdr_enabled = new_state
        if new_state:
            self.msg("[SDR] ON", C_SUCCESS)
            self._term_add("[SYS] SDR enabled", raw=True)
        else:
            self._sdr.stop()
            self.wardrive._wdg_owned_sdr = False
            self.msg("[SDR] OFF", C_WARNING)
            self._term_add("[SYS] SDR disabled", raw=True)
        self.glitch_timer = 2

    def _sdr_adsb(self):
        if not self._sdr_enabled:
            self.msg("[SDR] Enable SDR first (SYSTEM > SDR)", C_WARNING)
            return
        if self._sdr.running and "adsb" in self._sdr.mode:
            self._sdr.stop()
            self.wardrive._wdg_owned_sdr = False
            self.msg("[ADS-B] Stopped", C_WARNING)
            self._term_add("[SDR] ADS-B radar stopped", raw=True)
            return
        try:
            loot_dir = self.loot.session_dir if self.loot else ""
        except Exception:
            loot_dir = ""
        ok = self._sdr.start_adsb(loot_dir)
        if ok:
            self.msg("[ADS-B] Radar active", C_SUCCESS)
            self._term_add("[SDR] ADS-B radar started — tracking aircraft", raw=True)
            self._earn_badge("skywatch")
        else:
            # Drain error events so user sees the reason
            for etype, data in self._sdr.poll_events():
                if etype == "error":
                    self.msg(f"[SDR] {data}", C_ERROR)
                    self._term_add(f"[SDR] {data}", raw=True)
            if not self._sdr.has_dump1090():
                self.msg("[ADS-B] dump1090 not installed", C_ERROR)
                self._term_add("[SDR] Run ./setup.sh — it builds the FlightAware",
                               raw=True)
                self._term_add("[SDR] fork from source (apt dump1090-mutability",
                               raw=True)
                self._term_add("[SDR] was archived upstream in 2018).",
                               raw=True)
        self.glitch_timer = 2

    def _sdr_433(self):
        if not self._sdr_enabled:
            self.msg("[SDR] Enable SDR first (SYSTEM > SDR)", C_WARNING)
            return
        if self._sdr.running and "433" in self._sdr.mode:
            self._sdr.stop()
            self.wardrive._wdg_owned_sdr = False
            self.msg("[433] Stopped", C_WARNING)
            self._term_add("[SDR] 433 MHz scanner stopped", raw=True)
            return
        try:
            loot_dir = self.loot.session_dir if self.loot else ""
        except Exception:
            loot_dir = ""
        ok = self._sdr.start_433(loot_dir, self.player_lat, self.player_lon)
        if ok:
            self.msg("[433] Scanner active", C_SUCCESS)
            self._term_add("[SDR] 433 MHz scanner started — listening for sensors", raw=True)
            self._earn_badge("iot_hunter")
        else:
            for etype, data in self._sdr.poll_events():
                if etype == "error":
                    self.msg(f"[SDR] {data}", C_ERROR)
                    self._term_add(f"[SDR] {data}", raw=True)
            if not self._sdr.has_rtl433():
                self.msg("[433] rtl_433 not installed", C_ERROR)
                self._term_add("[SDR] Install: sudo apt install rtl-433", raw=True)
        self.glitch_timer = 2

    def _toggle_usb(self):
        if getattr(self, '_flash_io_active', False):
            self.msg('[UPDATE] USB is reserved; finish the update first', C_WARNING)
            return
        new_state = not self._usb_enabled
        if not self._aio_available:
            self.msg("[USB] AIO v2 not available", C_ERROR)
            return
        ok = AioManager.toggle("usb", new_state)
        if not ok:
            self.msg("[USB] GPIO toggle failed", C_ERROR)
            return
        self._usb_enabled = new_state
        if new_state:
            self.msg("[USB] ON", C_SUCCESS)
            self._term_add("[SYS] USB enabled", raw=True)
        else:
            self.msg("[USB] OFF", C_WARNING)
            self._term_add("[SYS] USB disabled", raw=True)
        self.glitch_timer = 2

    # ------------------------------------------------------------------
    # WPA-sec upload / download
    # ------------------------------------------------------------------

    def _wpasec_upload(self):
        """Upload all handshake pcaps to WPA-sec."""
        if not upload_manager.wpasec_configured():
            # No token — ask user to enter one
            self._wpasec_pending_action = "upload"
            self.input_mode = True
            self.input_fields = [{"label": "WPA-SEC KEY", "value": ""}]
            self.input_field_idx = 0
            self._input_pending_cat = -6  # WPA-sec token input
            self._input_pending_item = -1
            return
        self._wpasec_do_upload()

    def _wpasec_do_upload(self):
        if self._wpasec_busy:
            self.msg("[WPA-sec] Operation in progress...", C_WARNING)
            return
        loot_dir = Path(self._app_dir) / "loot"
        if not loot_dir.exists():
            self.msg("[WPA-sec] No loot directory", C_ERROR)
            return
        self._wpasec_busy = True
        self.msg("[WPA-sec] Uploading capture files...", C_DIM)
        self._term_add("[WPA-sec] Starting handshake upload...", raw=True)

        def _bg():
            up, total, msg = upload_manager.upload_wpasec_all(loot_dir)
            self._wpasec_result.put(("upload", up, total, msg))

        threading.Thread(target=_bg, daemon=True).start()

    def _wpasec_download(self):
        """Download cracked passwords from WPA-sec."""
        if not upload_manager.wpasec_configured():
            self._wpasec_pending_action = "download"
            self.input_mode = True
            self.input_fields = [{"label": "WPA-SEC KEY", "value": ""}]
            self.input_field_idx = 0
            self._input_pending_cat = -6
            self._input_pending_item = -1
            return
        self._wpasec_do_download()

    def _wpasec_do_download(self):
        if self._wpasec_busy:
            self.msg("[WPA-sec] Operation in progress...", C_WARNING)
            return
        loot_dir = Path(self._app_dir) / "loot"
        loot_dir.mkdir(parents=True, exist_ok=True)
        self._wpasec_busy = True
        self.msg("[WPA-sec] Downloading potfile...", C_DIM)
        self._term_add("[WPA-sec] Fetching cracked passwords...", raw=True)

        def _bg():
            ok, count, msg = upload_manager.download_wpasec_potfile(loot_dir)
            self._wpasec_result.put(("download", ok, count, msg))

        threading.Thread(target=_bg, daemon=True).start()

    def _sync_sdr_map_history(self):
        """Retain bounded display records independently of SDR pruning."""
        if not hasattr(self, "_map_aircraft"):
            self._map_aircraft = OrderedDict()
            self._map_sensors = OrderedDict()
        now = time.monotonic()
        for key, obj in list(getattr(self._sdr, "aircraft", {}).items()):
            source_seen = getattr(obj, "last_seen", 0)
            old = self._map_aircraft.get(key)
            if old is None or old["source_seen"] != source_seen:
                self._map_aircraft[key] = {
                    "object": obj, "source_seen": source_seen,
                    "last_seen": now}
                self._map_aircraft.move_to_end(key)
        while len(self._map_aircraft) > 128:
            self._map_aircraft.popitem(last=False)
        for key, obj in list(getattr(self._sdr, "sensors", {}).items()):
            source_seen = getattr(obj, "last_seen", 0)
            old = self._map_sensors.get(key)
            if old is None or old["source_seen"] != source_seen:
                self._map_sensors[key] = {
                    "object": obj, "source_seen": source_seen,
                    "last_seen": now}
                self._map_sensors.move_to_end(key)
        while len(self._map_sensors) > 128:
            self._map_sensors.popitem(last=False)

    def _poll_sdr(self):
        """Process SDR events (ADS-B aircraft, 433 MHz sensors)."""
        self._sync_sdr_map_history()
        if not self._sdr.running:
            return
        try:
            # Prune stale entries every ~2 seconds
            if pyxel.frame_count % 60 == 0:
                self._sdr.prune_stale()
                self._sdr.update_gps(self.player_lat, self.player_lon)
            for etype, data in self._sdr.poll_events():
                if etype == "log":
                    self._term_add(data, raw=True)
                elif etype == "status":
                    self._term_add(f"[SDR] {data}", raw=True)
                elif etype == "error":
                    self.msg(f"[SDR] {data}", C_ERROR)
                elif etype == "aircraft_new":
                    ac = data
                    if ac.icao not in self._sdr_aircraft_xp:
                        self._sdr_aircraft_xp.add(ac.icao)
                        self.gain_xp(10)
                elif etype == "sensor_new":
                    self.gain_xp(5)
        except Exception:
            pass

    def _watch_log_add(self, text: str, color: int = C_DIM):
        """Add line to watch overlay log (and terminal)."""
        self._watch_log.append((text, color))
        if len(self._watch_log) > 100:
            self._watch_log = self._watch_log[-100:]
        self._term_add(text, raw=True)

    def _poll_watch(self):
        """Process PipBoy watch events."""
        self._poll_watch_autoconnect()
        try:
            for etype, data in self._watch.poll_events():
                if etype == "status":
                    self._watch_log_add(f"[Watch] {data}", C_HACK_CYAN)
                elif etype == "error":
                    self._watch_log_add(f"[Watch] ERROR: {data}", C_ERROR)
                    self.msg(f"[Watch] {data}", C_ERROR)
                elif etype == "log":
                    self._watch_log_add(data, C_DIM)
                elif etype == "connected":
                    self._watch_log_add(f"[Watch] Connected: {data}", C_SUCCESS)
                    self.msg(f"[Watch] Connected: {data}", C_SUCCESS)
                elif etype == "disconnected":
                    self._watch_log_add("[Watch] Disconnected", C_WARNING)
                    self.msg("[Watch] Disconnected", C_WARNING)
                elif etype == "pin_request":
                    self._watch_pin_input = ""
                    self._watch_log_add("[Watch] PIN requested — enter on screen", C_WARNING)
                elif etype == "device":
                    self._watch_log_add(
                        f"[Watch] Found: {data['name']} "
                        f"RSSI:{data['rssi']}", C_SUCCESS)
                elif etype == "version":
                    ver = data.get("version", "?")
                    feats = data.get("features", [])
                    self._watch_log_add(
                        f"[Watch] {ver} [{','.join(feats)}]", C_HACK_CYAN)
                elif etype == "status_data":
                    bat = data.get("bat", 0)
                    chrg = "CHG" if data.get("charging") else ""
                    gps_lat = data.get("gps_lat", 0)
                    gps_lon = data.get("gps_lon", 0)
                    t = data.get("time", "")
                    parts = [f"BAT:{bat}%{chrg}"]
                    if t:
                        parts.append(t)
                    if gps_lat:
                        parts.append(f"GPS:{gps_lat:.4f},{gps_lon:.4f}")
                    parts.append(f"heap:{data.get('heap',0)}K")
                    self._watch_log_add(f"[Watch] {' | '.join(parts)}", C_SUCCESS)
                elif etype == "nfc_tag":
                    uid = data.get("uid", "?")
                    ndef = data.get("ndef", "")
                    self._watch_log_add(
                        f"[NFC] Tag detected: {uid} {ndef[:40]}", C_SUCCESS)
                    self.msg(f"[NFC] Tag: {uid}", C_SUCCESS)
                    self.gain_xp(25)
                elif etype == "nfc_list":
                    self._watch_nfc_tags = data.get("tags", [])
                    self._watch_log_add(
                        f"[NFC] {len(self._watch_nfc_tags)} tags on watch", C_HACK_CYAN)
                elif etype == "nfc_file":
                    self._save_nfc_file(data)
                    self._watch_log_add(
                        f"[NFC] File saved: {data.get('name','?')}", C_SUCCESS)
                elif etype == "lora_msg":
                    ch = data.get("channel", "?")
                    txt = data.get("text", "")
                    hops = data.get("hops", 0)
                    rssi = data.get("rssi", 0)
                    ts = time.strftime("%H:%M")
                    # Add to MeshCore chat with [W] tag (watch source)
                    self._mc_log.append(
                        (f"\x10 {txt}", C_SUCCESS,
                         f"{ts} {hops}hop W"))
                    self._mc_bubbles.append(
                        (txt[:40], pyxel.frame_count + 300))
                    self._play_mc_notify()
                    self._earn_badge("meshcore")
                    self._watch_log_add(
                        f"[LoRa] [{ch}] {txt}", C_HACK_CYAN)
                elif etype == "recon":
                    wifi = data.get("wifi", [])
                    ble = data.get("ble", [])
                    self._watch_log_add(
                        f"[Recon] {len(wifi)} WiFi, {len(ble)} BLE", C_SUCCESS)
                    for net in wifi:
                        auth = net.get("auth", "?")
                        self._watch_log_add(
                            f"  {net.get('ssid','?'):<20} ch:{net.get('ch',''):>2} "
                            f"{net.get('rssi',0):>4}dBm {auth}", C_DIM)
                    for dev in ble:
                        name = dev.get("name", "") or dev.get("mac", "?")
                        tag = " [AirTag]" if dev.get("airtag") else ""
                        self._watch_log_add(
                            f"  {name:<20} {dev.get('rssi',0):>4}dBm{tag}",
                            C_DIM)
                elif etype == "compass":
                    h = data.get("heading", 0)
                    r = data.get("roll", 0)
                    p = data.get("pitch", 0)
                    self._watch_log_add(
                        f"[Compass] {h:.0f}\u00b0  roll:{r:.1f} pitch:{p:.1f}",
                        C_HACK_CYAN)
                elif etype == "et_cred":
                    email = data.get("email", "")
                    pwd = data.get("password", "")
                    self._watch_log_add(
                        f"[ET] CREDENTIAL: {email} / {pwd}", C_SUCCESS)
                    self.msg(f"[ET] Cred captured via watch!", C_SUCCESS)
                    self.gain_xp(150)
                    self._earn_badge("evil_twin")
                elif etype == "deauth_detected":
                    src = data.get("src", "?")
                    count = data.get("count", 0)
                    ch = data.get("ch", "?")
                    self._watch_log_add(
                        f"[TSCM] Deauth detected! src:{src} ch:{ch} x{count}",
                        C_ERROR)
                    self.msg(f"[TSCM] Deauth on ch:{ch}!", C_ERROR)
                elif etype == "ack":
                    m = data.get("msg", "OK")
                    self._watch_log_add(f"[Watch] {m}", C_HACK_CYAN)
        except Exception:
            pass

    def _save_nfc_file(self, data: dict):
        """Save NFC file from watch to loot directory."""
        import base64
        name = data.get("name", "tag.nfc")
        b64 = data.get("data", "")
        if not b64:
            return
        try:
            content = base64.b64decode(b64).decode("utf-8", errors="replace")
            loot_dir = self.loot.session_dir if self.loot else ""
            if loot_dir:
                nfc_dir = os.path.join(loot_dir, "nfc")
                os.makedirs(nfc_dir, exist_ok=True)
                fname = os.path.basename(name)
                path = os.path.join(nfc_dir, fname)
                with open(path, "w") as f:
                    f.write(content)
                self.msg(f"[NFC] Saved: {fname}", C_SUCCESS)
                self._term_add(f"[Watch] NFC saved: {path}", raw=True)
                self.gain_xp(25)
        except Exception as e:
            self._term_add(f"[Watch] NFC save error: {e}", raw=True)

    def _watch_connect(self):
        """Handle PipBoy Watch menu action."""
        if self._watch.connected:
            # Already connected — open watch control screen
            self._watch_screen = True
            self._watch_menu_sel = 0
            self._watch.send_command("status")
            self._watch.send_command("nfc_list")
            return
        # Start scanning
        self._watch_screen = True
        self._watch_scan_sel = 0
        self._watch.scan()

    def _poll_wpasec_result(self):
        """Check for WPA-sec background operation results (called from update)."""
        if self._wpasec_result.empty():
            return
        result = self._wpasec_result.get_nowait()
        self._wpasec_busy = False
        if result[0] == "upload":
            _, up, total, msg = result
            if up > 0:
                self.msg(f"[WPA-sec] {up}/{total} new uploaded", C_SUCCESS)
                self._earn_badge("wpasec_uploader")
            elif total == 0 and "already uploaded" in msg:
                self.msg("[WPA-sec] All captures already uploaded", C_DIM)
            else:
                self.msg(f"[WPA-sec] {msg}", C_WARNING if total == 0 else C_ERROR)
            self._term_add(f"[WPA-sec] Upload: {msg}", raw=True)
        elif result[0] == "download":
            _, ok, count, msg = result
            if ok and count > 0:
                self.msg(f"[WPA-sec] {count} passwords!", C_SUCCESS)
            elif ok:
                self.msg("[WPA-sec] No cracked passwords yet", C_DIM)
            else:
                self.msg(f"[WPA-sec] {msg}", C_ERROR)
            self._term_add(f"[WPA-sec] Download: {msg}", raw=True)

    # ------------------------------------------------------------------
    # Flash ESP32
    # ------------------------------------------------------------------

    def _update_app(self):
        from .updates import update_app
        from pathlib import Path
        try:
            changed = update_app(Path(__file__).resolve().parents[1], lambda line: self._term_add(line, raw=True))
            self.msg("[UPDATE] Restart WDG to apply changes" if changed else "[UPDATE] Already up to date", C_SUCCESS)
        except Exception as exc:
            self._term_add(f"[UPDATE] Stopped: {exc}", raw=True)
            self.msg("[UPDATE] Could not update; see console", C_WARNING)
        finally:
            self._app_update_running = False

    def _start_flash_esp32(self):
        """Pause normal serial I/O before the user can enter manual BOOT mode."""
        if getattr(self, '_ota_reserved', False):
            self.msg('[OTA] Finish the OTA update first', C_WARNING)
            return
        if getattr(self, '_serial_busy', False):
            self.msg("[FLASH] Serial is busy; finish the current operation first", C_WARNING)
            return
        if getattr(self, '_flash_running', False):
            self._flash_screen = True
            self.menu_open = False
            return
        from .config import FLASH_BOARDS
        from .firmware_flash import FlashTarget, select_target
        self._flash_boards = list(FLASH_BOARDS.keys())
        self._flash_sel = getattr(self, '_flash_sel', 0)
        self._flash_manual = getattr(self, '_flash_manual', False)
        self._flash_screen = True
        self._flash_running = False
        self._flash_status = "Choose board and connection mode"
        self._flash_release_tag = getattr(self, '_flash_release_tag', None)
        if not getattr(self, '_flash_releases_loaded', False) and not getattr(self, '_flash_versions_loading', False):
            self._flash_versions_loading = True
            threading.Thread(target=self._flash_load_releases, daemon=True).start()
        self.menu_open = False
        # This runs on the GUI thread, after its last poll/reconnect completes.
        # Hold the reservation throughout the wizard, including failed attempts.
        self._flash_io_active = True
        preferred = getattr(self.serial, 'device', None) or getattr(self, '_boot_serial_port', None)
        if not getattr(self, '_flash_target', None):
            try:
                identity = getattr(self.serial, 'usb_port_info', None)
                self._flash_target = (FlashTarget.from_port(identity) if identity is not None
                                      else select_target(preferred))
            except RuntimeError as exc:
                self._flash_target = None
                self._term_add("[FLASH] " + str(exc), raw=True)
        self._reconnect_flash_target = self._flash_target
        self._flash_refresh_target(discover=False)
        self.wardrive.on_stop()  # local state only: _send is now reserved
        self.wardrive.close_passive()
        self.wardrive.capture.finish("disconnected")
        self.sniffing = self.capturing_hs = False
        if self.serial:
            self.serial.close()
        self._esp32 = False
        self.state.connected = False
        self._term_add("[FLASH] WDG serial paused; USB power is left on. ESC resumes normal use.", raw=True)

    def _flash_refresh_target(self, discover=True):
        """Read USB metadata only; never open/reset a port to identify it."""
        from .firmware_flash import select_target
        try:
            if not getattr(self, '_flash_target', None):
                if not discover:
                    raise RuntimeError("No target selected; R refreshes recognized USB devices")
                self._flash_target = select_target()
            target = self._flash_target
            port = target.resolve()
            self._flash_target_label = port
            self._reconnect_flash_target = target
            self._term_add(f"[FLASH] Target: {port}, USB {target.vid:04X}:{target.pid:04X}, "
                           f"serial {target.serial_number or 'unknown'}, location {target.location or 'unknown'}", raw=True)
        except RuntimeError as exc:
            self._flash_target_label = str(exc)

    def _flash_load_releases(self):
        from .updates import firmware_releases
        try:
            self._flash_releases = firmware_releases()
            self._flash_releases_loaded = True
            self._flash_release_error = ""
        except Exception as exc:
            self._flash_release_error = str(exc)
            self._term_add("[FLASH] Version list unavailable: " + str(exc), raw=True)
        finally:
            self._flash_versions_loading = False

    def _flash_cycle_version(self, direction):
        if getattr(self, '_flash_versions_loading', False):
            self.msg("[FLASH] Firmware versions are still loading", C_DIM)
            return
        if not getattr(self, '_flash_releases_loaded', False):
            self._flash_versions_loading = True
            threading.Thread(target=self._flash_load_releases, daemon=True).start()
            return
        tags = [None] + [r['tag_name'] for r in self._flash_releases]
        current = getattr(self, '_flash_release_tag', None)
        index = tags.index(current) if current in tags else 0
        self._flash_release_tag = tags[(index + direction) % len(tags)]

    def _update_flash_screen(self):
        """Board/mode picker; closing an active flash never releases its port."""
        if self._flash_running:
            if pyxel.btnp(pyxel.KEY_ESCAPE):
                self._flash_screen = False
                self._esc_consumed_frame = pyxel.frame_count
            return
        if pyxel.btnp(pyxel.KEY_UP):
            self._flash_sel = max(0, self._flash_sel - 1)
        elif pyxel.btnp(pyxel.KEY_DOWN):
            self._flash_sel = min(len(self._flash_boards) - 1, self._flash_sel + 1)
        elif pyxel.btnp(pyxel.KEY_LEFT) or pyxel.btnp(pyxel.KEY_RIGHT):
            self._flash_manual = not self._flash_manual
        elif pyxel.btnp(pyxel.KEY_V):
            self._flash_cycle_version(1)
        elif pyxel.btnp(pyxel.KEY_B):
            self._flash_cycle_version(-1)
        elif pyxel.btnp(pyxel.KEY_R):
            self._flash_refresh_target()
        elif pyxel.btnp(pyxel.KEY_RETURN):
            if not getattr(self, '_flash_target', None):
                self.msg("[FLASH] No target selected. Press R and check the USB device first", C_WARNING)
                return
            board = self._flash_boards[self._flash_sel]
            self._flash_running = True
            self._term_add(f"[FLASH] Selected: {board}", raw=True)
            threading.Thread(target=self._flash_do, args=(board,),
                             daemon=True).start()
        elif pyxel.btnp(pyxel.KEY_ESCAPE):
            self._flash_screen = False
            self._flash_io_active = False
            self._esc_consumed_frame = pyxel.frame_count

    def _flash_do(self, board: str):
        try:
            self._flash_firmware(board)
        finally:
            # Keep ROM/stub untouched while the wizard remains open for retry.
            self._flash_io_active = bool(getattr(self, '_flash_screen', False))
            self._flash_running = False

    def _flash_firmware(self, board: str):
        """Verify the release/tools, then flash the same selected USB device."""
        from .config import FLASH_BOARDS
        from .updates import prepare_firmware
        from .firmware_flash import ensure_esptool, wait_for_target, flash_command, run_flash
        from pathlib import Path
        cache = Path(self._app_dir) / "firmware_cache"
        log_file = None
        def report(message):
            self._term_add("[FLASH] " + message, raw=True)
            if log_file:
                log_file.write(message + "\n")
                log_file.flush()
        try:
            if not getattr(self, '_flash_target', None):
                raise RuntimeError("No ESP32 selected; press R and check the target before flashing")
            cache.mkdir(parents=True, exist_ok=True)
            self._flash_log_path = cache / f"flash-{time.time_ns()}.log"
            log_file = self._flash_log_path.open("x", encoding="utf-8")
            report("Source: Smethan/projectZero")
            report("Board: " + FLASH_BOARDS[board]['label'])
            report("Log: " + str(self._flash_log_path))
            requested = getattr(self, '_flash_release_tag', None)
            release = None
            if requested is not None:
                release = next((r for r in getattr(self, '_flash_releases', []) if r['tag_name'] == requested), None)
                if release is None:
                    raise ValueError("Selected firmware version is unavailable; choose a listed release")
            report("Requested firmware: " + (requested or "latest stable"))
            self._flash_status = "Downloading and verifying firmware..."
            tag, fw_dir = prepare_firmware(cache, board, release=release)
            report(f"{tag}: board and checksums verified")
            self._flash_status = "Checking flashing tools..."
            python, version = ensure_esptool(cache, report)
            report(f"esptool {version} / {python}")
            self._flash_status = "Waiting for selected ESP32 port..."
            target, port = wait_for_target(getattr(self, '_flash_target', None))
            self._flash_target = self._reconnect_flash_target = target
            self._boot_serial_port = port
            self._flash_target_label = port
            report(f"Port: {port} / USB {target.vid:04X}:{target.pid:04X} / serial {target.serial_number or 'unknown'} / location {target.location or 'unknown'}")
            if port != target.device:
                report(f"Same USB device renumbered: {target.device} -> {port}")
            manual = getattr(self, '_flash_manual', False)
            report("Manual BOOT: no pre-reset, 115200 baud" if manual else "Automatic: script reset sequence, 460800 baud")
            self._flash_status = "Flashing; keep USB connected..."
            run_flash(flash_command(python, port, board, fw_dir, manual), report)
            self._fw_version = ""
            self._fw_update_available = False
            self._flash_status = "Flash verified. ESC to reconnect."
            report("Flash verified; watchdog reset requested. Release BOOT. ESC to reconnect WDG.")
            self.msg("[FLASH] Success! ESC to reconnect.", C_SUCCESS)
            time.sleep(1)  # allow USB re-enumeration before a hidden wizard resumes polling
        except Exception as exc:
            report("Error: " + str(exc))
            self._flash_status = "Failed; see console / saved flash log"
            self.msg("[FLASH] Failed; see console and flash log", C_ERROR)
        finally:
            if log_file:
                log_file.close()

    # ------------------------------------------------------------------
    # Wardriving auto-repeat loop
    # ------------------------------------------------------------------

    def _update_wardriving_loop(self):
        """Auto-repeat WiFi/BT scans for continuous wardriving (like JanOS)."""
        now = time.time()
        if self.wardrive.scan.active:
            return

        # GPS wait — start scanning once fix is acquired
        if self._gps_wait:
            if self.gps_fix:
                self._gps_wait = False
                cmd = self._gps_wait_cmd
                state = self._gps_wait_state
                name = self._gps_wait_name
                self._gps_wait_cmd = ""
                self.msg("[GPS] Fix acquired — starting!", C_SUCCESS)
                self._start_scan_cmd(cmd, state, name)
            else:
                # Show satellite info periodically
                if pyxel.frame_count % 60 == 0:
                    if self.gps_sats:
                        sat = f"Sat:{self.gps_sats}"
                    elif self.gps_sats_vis:
                        sat = f"Vis:{self.gps_sats_vis}"
                    else:
                        sat = "no satellites"
                    self.msg(f"[GPS] Waiting for fix... {sat}", C_WARNING)
            return

        # WiFi wardriving auto-repeat
        if (self.wifi_scanning and self._wifi_scan_done_time > 0
                and not self._pending_cmd):
            if now - self._wifi_scan_done_time >= self._SCAN_INTERVAL:
                self._wifi_scan_done_time = 0.0
                self._send("scan_networks")
                self.msg("[SCAN] WiFi rescan...", C_DIM)

        # BT wardriving auto-repeat
        if (self.ble_scanning and self._bt_scan_done_time > 0
                and not self._pending_cmd):
            if now - self._bt_scan_done_time >= self._SCAN_INTERVAL:
                self._bt_scan_done_time = 0.0
                self._send("scan_bt")
                self._bt_scan_start_time = now
                self.msg("[SCAN] BT rescan...", C_DIM)

        # BT scan timeout guard (auto-finish after 15s if no completion signal)
        if (self.ble_scanning and self._bt_scan_start_time > 0
                and self._bt_scan_done_time == 0.0):
            if now - self._bt_scan_start_time > self._BT_SCAN_TIMEOUT:
                self._bt_scan_done_time = now
                self.msg("[BT] Scan timeout — restarting cycle", C_DIM)

    # ------------------------------------------------------------------
    # Picker overlay (network / portal selection)
    # ------------------------------------------------------------------

    def _update_picker_overlay(self):
        """Handle UP/DOWN/ENTER/ESC for network or portal picker overlays."""
        import pyxel as px
        if self._et_net_screen:
            nets = self._attack_scan_results
            if px.btnp(px.KEY_UP):
                self._et_net_sel = max(0, self._et_net_sel - 1)
            elif px.btnp(px.KEY_DOWN):
                self._et_net_sel = min(len(nets) - 1, self._et_net_sel + 1)
            elif px.btnp(px.KEY_SPACE) and nets:
                # Toggle multi-select (SPACE)
                idx = self._et_net_sel
                net = nets[idx]
                if self._whitelist.is_blocked(net.bssid):
                    self.msg(f"[WL] {net.bssid[-8:]} whitelisted", C_WARNING)
                    return
                if idx in self._et_net_selected:
                    self._et_net_selected.discard(idx)
                else:
                    self._et_net_selected.add(idx)
            elif px.btnp(px.KEY_RETURN) and nets:
                # Confirm selection
                selected = sorted(self._et_net_selected)
                if not selected:
                    # Nothing toggled → use cursor position as single select
                    selected = [self._et_net_sel]
                # Whitelist check on primary target
                primary = nets[selected[0]]
                if self._whitelist.is_blocked(primary.bssid):
                    self.msg(f"[WL] {primary.bssid[-8:]} whitelisted", C_WARNING)
                    return
                # First selected = clone SSID, rest = deauth targets
                self._et_net_idx = primary.index if hasattr(primary, 'index') else selected[0]
                # Build "select_networks 1 3 5" command args
                idx_args = []
                for si in selected:
                    n = nets[si]
                    idx_args.append(n.index if hasattr(n, 'index') else str(si))
                self._et_select_args = " ".join(idx_args)
                self._portal_ssid = primary.ssid or "<hidden>"
                self._et_net_screen = False
                self._show_portal_selection("ET")
            elif px.btnp(px.KEY_ESCAPE):
                self._et_net_screen = False
                self._attack_mode = ""
                self._attack_step = ""
                self._esc_consumed_frame = pyxel.frame_count
                self.msg("[ET] Cancelled", C_DIM)
        elif self._portal_select_screen:
            portals = self._portal_list
            if px.btnp(px.KEY_UP):
                self._portal_sel = max(0, self._portal_sel - 1)
            elif px.btnp(px.KEY_DOWN):
                self._portal_sel = min(len(portals) - 1, self._portal_sel + 1)
            elif px.btnp(px.KEY_RETURN) and portals:
                name, html = portals[self._portal_sel]
                tag = "EP" if self._attack_mode == "evil_portal" else "ET"
                self._portal_select_screen = False
                self.msg(f"[{tag}] Portal: {name}", C_HACK_CYAN)

                def _start_portal_attack():
                    if html is not None:
                        self._term_add(f"[{tag}] Uploading portal HTML...",
                                       raw=True)
                        upload_html_to_esp32(html, self._send)
                        time.sleep(0.4)
                    if self._attack_mode == "evil_portal":
                        self._send(f"start_portal {self._portal_ssid}")
                        self.state.portal_running = True
                        self.state.portal_ssid = self._portal_ssid
                    else:
                        # Evil Twin: stop → clean state → select → start
                        self._term_add("[ET:CMD] stop (clean state)", raw=True)
                        self._send("stop")
                        time.sleep(1.5)
                        # Drain stale serial data
                        if self.serial and self.serial.is_open:
                            try:
                                while self.serial.ser.in_waiting:
                                    self.serial.ser.read(self.serial.ser.in_waiting)
                            except Exception:
                                pass

                        # Select target network(s)
                        args = self._et_select_args or str(self._et_net_idx)
                        cmd_sel = f"select_networks {args}"
                        self._term_add(f"[ET:CMD] {cmd_sel}", raw=True)
                        self._send(cmd_sel)
                        time.sleep(0.5)
                        if html is not None:
                            self._term_add("[ET:CMD] (HTML uploaded)", raw=True)
                        else:
                            self._term_add("[ET:CMD] Using firmware default portal", raw=True)
                        self._term_add("[ET:CMD] start_evil_twin", raw=True)
                        self._send("start_evil_twin")
                        self.state.evil_twin_running = True
                        self.state.evil_twin_ssid = self._portal_ssid
                    self._attack_step = "running"
                    self.msg(f"[{tag}] Started — SSID: {self._portal_ssid}",
                             C_WARNING)
                    self.glitch_timer = 8

                threading.Thread(target=_start_portal_attack,
                                 daemon=True).start()
            elif px.btnp(px.KEY_ESCAPE):
                self._portal_select_screen = False
                if self._attack_mode == "evil_twin":
                    self._show_net_selection()
                else:
                    self._attack_mode = ""
                    self._attack_step = ""
                self._esc_consumed_frame = pyxel.frame_count

    # ------------------------------------------------------------------
    # Input dialog
    # ------------------------------------------------------------------

    def _update_input_dialog(self):
        if not self.input_fields:
            self.input_mode = False
            return
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self.input_mode = False
            self._esc_consumed_frame = pyxel.frame_count
            # Cancel any pending attack flow that uses input dialog
            if self._input_pending_cat == -7:  # Evil Portal SSID
                self._attack_mode = ""
                self._attack_step = ""
            self.msg("[ESC] Input cancelled", C_DIM)
            return
        field = self.input_fields[self.input_field_idx]
        if pyxel.btnp(pyxel.KEY_BACKSPACE):
            field["value"] = field["value"][:-1]
            return
        if pyxel.btnp(pyxel.KEY_RETURN):
            if self.input_field_idx < len(self.input_fields) - 1:
                self.input_field_idx += 1
            else:
                self.input_mode = False
                vals = [f["value"].strip() for f in self.input_fields]
                # Special attack input handlers
                if self._input_pending_cat == -1:  # MITM victim IP
                    ip = vals[0] if vals else ""
                    if ip:
                        self._mitm_victim_ip = ip
                        self._mitm_state = "confirm"
                    else:
                        self._mitm_state = "idle"
                    return
                if self._input_pending_cat == -2:  # BlueDucky MAC
                    mac = vals[0].upper() if vals else ""
                    if mac:
                        if self._whitelist.is_blocked(mac):
                            self.msg(f"[WL] {mac[-8:]} whitelisted", C_WARNING)
                            return
                        self._blueducky.connect(mac)
                    return
                if self._input_pending_cat == -3:  # Whitelist MAC
                    mac = vals[0].upper().strip() if vals else ""
                    if mac:
                        self._wl_add_mac = mac
                        lbl = "SSID" if self._wl_add_type == "wifi" else "Name"
                        self.input_mode = True
                        self.input_fields = [{"label": lbl, "value": ""}]
                        self.input_field_idx = 0
                        self._input_pending_cat = -4
                        self._input_pending_item = -1
                    else:
                        self._wl_add_step = ""
                    return
                if self._input_pending_cat == -4:  # Whitelist Name/SSID
                    name_val = vals[0].strip() if vals else ""
                    if name_val and self._wl_add_mac:
                        ok = self._whitelist.add(
                            self._wl_add_type, self._wl_add_mac, name_val)
                        if ok:
                            self._term_add(
                                f"[WL] Added: {name_val} "
                                f"({self._wl_add_mac[-8:]})", raw=True)
                            self.msg("[WL] Entry added", C_SUCCESS)
                        else:
                            self.msg("[WL] MAC already exists", C_WARNING)
                    self._wl_add_step = ""
                    self._wl_add_mac = ""
                    return
                if self._input_pending_cat == -5:  # MeshCore node name
                    name_val = vals[0].strip() if vals else ""
                    if name_val:
                        self._mc_node_name = name_val
                        from .lora_manager import save_meshcore_config
                        save_meshcore_config(name_val, self._mc_channels_list)
                        self.msg(f"[MC] Node name: {name_val}", C_SUCCESS)
                    return
                if self._input_pending_cat == -6:  # WPA-sec token
                    token = vals[0].strip() if vals else ""
                    if token:
                        upload_manager.save_wpasec_key(self._app_dir, token)
                        self.msg("[WPA-sec] Token saved!", C_SUCCESS)
                        self._term_add("[WPA-sec] API key configured",
                                       raw=True)
                        # Continue with the pending action
                        if self._wpasec_pending_action == "upload":
                            self._wpasec_do_upload()
                        elif self._wpasec_pending_action == "download":
                            self._wpasec_do_download()
                        self._wpasec_pending_action = ""
                    else:
                        self.msg("[WPA-sec] No token — cancelled", C_DIM)
                        self._wpasec_pending_action = ""
                    return
                if self._input_pending_cat == -8:  # NFC tag save
                    name = vals[0].strip() if vals else ""
                    if name and hasattr(self, '_nfc_pending_save'):
                        tag = self._nfc_pending_save
                        safe_name = name.replace(" ", "_").replace("/", "_")
                        fname = f"{safe_name}.txt"
                        try:
                            nfc_dir = Path(self.loot.session_path) / "nfc"
                            nfc_dir.mkdir(exist_ok=True)
                            with open(nfc_dir / fname, "w") as f:
                                f.write("\n".join(tag["data"]))
                            self.msg(f"[NFC] Saved: {fname}", C_SUCCESS)
                            self._flipper_log.append(
                                (f"Saved: nfc/{fname}", C_SUCCESS))
                            self.gain_xp(25)
                        except Exception as exc:
                            self.msg(f"[NFC] Save error: {exc}", C_ERROR)
                        del self._nfc_pending_save
                    else:
                        self.msg("[NFC] Save cancelled", C_DIM)
                    self._flipper_screen = True
                    return
                if self._input_pending_cat == -7:  # Evil Portal SSID
                    ssid = vals[0].strip() if vals else ""
                    if ssid:
                        self._portal_ssid = ssid
                        self._show_portal_selection("EP")
                    else:
                        self.msg("[EP] No SSID — cancelled", C_DIM)
                        self._attack_mode = ""
                        self._attack_step = ""
                    return
                _, items = MENU_CATS[self._input_pending_cat]
                _hk, name, cmd, state_key, _it = items[self._input_pending_item]
                # Whitelist check for attack targets (BSSID/MAC)
                if _it in ("bssid_ch", "mac") and vals:
                    target = vals[0].upper()
                    if self._whitelist.is_blocked(target):
                        self.msg(f"[WL] {target[-8:]} whitelisted", C_WARNING)
                        return
                self._execute_item(cmd, state_key, name, vals)
            return
        c = self._get_char_input()
        if c and len(field["value"]) < 32:
            field["value"] += c

    def _get_char_input(self) -> str | None:
        ctrl = pyxel.btn(pyxel.KEY_LCTRL) or pyxel.btn(pyxel.KEY_RCTRL)
        if ctrl:
            return None  # Ctrl+key handled elsewhere, don't produce chars
        # Shift detection: btn() for normal keyboards, sticky for uConsole
        # (USB HID may report shift and letter in separate frames)
        shift_now = pyxel.btn(pyxel.KEY_LSHIFT) or pyxel.btn(pyxel.KEY_RSHIFT)
        if shift_now:
            self._shift_frames = 4  # hold shift state for 4 frames
        elif hasattr(self, '_shift_frames') and self._shift_frames > 0:
            self._shift_frames -= 1
        shift = shift_now or (getattr(self, '_shift_frames', 0) > 0)
        for k in range(pyxel.KEY_A, pyxel.KEY_Z + 1):
            if pyxel.btnp(k):
                c = chr(k)
                return c.upper() if shift else c
        for k in range(pyxel.KEY_0, pyxel.KEY_9 + 1):
            if pyxel.btnp(k):
                if shift:
                    return ")!@#$%^&*("[k - pyxel.KEY_0]
                return chr(k)
        specials = {
            pyxel.KEY_SEMICOLON:     (";", ":"),
            pyxel.KEY_PERIOD:        (".", ">"),
            pyxel.KEY_COMMA:         (",", "<"),
            pyxel.KEY_MINUS:         ("-", "_"),
            pyxel.KEY_SLASH:         ("/", "?"),
            pyxel.KEY_SPACE:         (" ", " "),
            pyxel.KEY_LEFTBRACKET:   ("[", "{"),
            pyxel.KEY_RIGHTBRACKET:  ("]", "}"),
            pyxel.KEY_BACKSLASH:     ("\\", "|"),
        }
        # Optional keys (not in all pyxel versions)
        for attr, pair in [("KEY_APOSTROPHE", ("'", '"')),
                           ("KEY_EQUAL", ("=", "+")),
                           ("KEY_GRAVE_ACCENT", ("`", "~"))]:
            k = getattr(pyxel, attr, None)
            if k is not None:
                specials[k] = pair
        for k, (norm, sh) in specials.items():
            if pyxel.btnp(k):
                return sh if shift else norm
        return None

    # ------------------------------------------------------------------
    # Serial polling + line parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _read_battery() -> int:
        """Read uConsole battery percentage. Returns -1 if unavailable."""
        try:
            with open("/sys/class/power_supply/axp20x-battery/capacity") as f:
                return int(f.read().strip())
        except Exception:
            return -1

    def _poll_serial(self):
        if getattr(self, '_serial_busy', False) or getattr(self, '_flash_io_active', False):
            return
        if not self.serial or not self.serial.is_open:
            # Auto-reconnect: try every ~3s (90 frames at 30 FPS)
            if not self._esp32 and pyxel.frame_count % 90 == 0:
                if self._try_reconnect_esp32():
                    self.msg("[SYS] ESP32 reconnected!", C_SUCCESS)
            return
        try:
            lines = self.serial.read_available()
        except Exception:
            # USB cable yanked or serial error — mark ESP32 offline
            self._esp32 = False
            self.state.connected = False
            try:
                self.serial.close()
            except Exception:
                pass
            self.serial = None
            self._serial_capture_kind = None
            if getattr(self, "_wl_scan_phase", "idle") in (
                    "waiting_stop", "scanning_wifi", "scanning_ble", "stopping"):
                self._wl_scan_fail("ESP32 disconnected during scan")
            self.msg("[ERR] ESP32 disconnected!", C_ERROR)
            self._term_add("[ERR] ESP32 disconnected — plug in & retry", raw=True)
            return
        for line in lines:
            self._handle_serial_line(line)

    def _handle_serial_line(self, line: str):
        if self.wardrive.handle_line(line):
            return
        s = line.strip()
        if not s:
            return

        # Firmware version detection (from boot banner, version cmd, ping)
        # Match both legacy "JanOS version" and newer "WatchDogsGo version" — the
        # ESP32 projectZero firmware still emits "JanOS" in older builds.
        if not self._fw_version:
            _vm = re.search(r"(?:JanOS|WatchDogsGo) version:\s*v?(\d+\.\d+\.\d+)", s)
            if not _vm:
                _vm = re.search(r"APP_MAIN START \(v?(\d+\.\d+\.\d+)\)", s)
            if not _vm:
                _vm = re.search(r"pong\s+v?(\d+\.\d+\.\d+)", s)
            if _vm:
                self._fw_version = _vm.group(1)
                self._term_add(f"[FW] Firmware v{self._fw_version}", raw=True)
                # Trigger background GitHub check
                threading.Thread(target=self._check_fw_update,
                                 daemon=True).start()

        kind_match = _CAPTURE_KIND_RE.fullmatch(s)
        if kind_match:
            self._serial_capture_kind = kind_match.group(1)

        # Log to loot (handles PCAP/HCCAPX detection internally)
        committed_kind = None
        if self.loot:
            try:
                committed_kind = self.loot.log_serial(s)
            except Exception:
                pass

        # The 1.7.9 serial protocol also commits PMKID-only and partial PCAPs
        # with this metadata line. Only a validated exchange earns the legacy
        # handshake event. Untyped older firmware keeps its historical event.
        if _HS_ARTIFACT_META_RE.fullmatch(s):
            announced_kind = getattr(self, "_serial_capture_kind", None)
            self._serial_capture_kind = None
            if announced_kind is None:
                self._trigger_hs_event()
            elif announced_kind == "VALID" and committed_kind == "VALID":
                self._trigger_hs_event()
            elif announced_kind == "PMKID" and committed_kind == "PMKID":
                self.msg("[HS] PMKID capture saved.", C_SUCCESS)
            elif announced_kind == "PARTIAL" and committed_kind == "PARTIAL":
                self.msg("[HS] Partial capture saved; exchange is not validated.", C_DIM)
            elif announced_kind in ("VALID", "PMKID", "PARTIAL"):
                self.msg("[HS] Incomplete serial artifact rejected.", C_WARNING)
            return

        if "handshake attack cleanup complete" in s.lower():
            self._serial_capture_kind = None

        # --- AirTag scanner count line: "X,Y" ---
        at_m = _AIRTAG_COUNT_RE.match(s)
        if at_m and self._bt_airtag:
            self._airtag_count = int(at_m.group(1))
            self._smarttag_count = int(at_m.group(2))
            total = self._airtag_count + self._smarttag_count
            parts = []
            if self._airtag_count:
                parts.append(f"AirTag:{self._airtag_count}")
            if self._smarttag_count:
                parts.append(f"SmartTag:{self._smarttag_count}")
            if total > 0:
                self._term_add(
                    f"[TRACKER] {' | '.join(parts)}  total:{total}",
                    raw=True)
                self.msg(f"[TAG] {' '.join(parts)}", C_WARNING)
            return

        m = _BLE_RE.match(s)
        if m:
            self._wl_observe_ble(
                m.group(1), int(m.group(2)),
                (m.group(3) or "").strip() or "?",
                (m.group(4) or "").strip())
            self._ingest_ble(m.group(1), int(m.group(2)), (m.group(3) or "").strip() or "?", (m.group(4) or "").strip())
            return
        net = self.net_mgr.parse_network_line(s)
        if net:
            self._wl_observe_wifi(net)
            self._ingest_wifi(net)
            return

        # --- Operation state from serial output ---
        sl = s.lower()
        self._wl_handle_scan_status(s, sl)
        if "sniffer start" in sl or "packet sniffer" in sl:
            self.sniffing = True
        elif "handshake attack cleanup complete" in sl or "handshake attack task finished" in sl:
            self.capturing_hs = False
        elif "handshake" in sl and ("start" in sl or "captur" in sl):
            capture = self.wardrive.capture.current
            self.capturing_hs = capture.active if capture else True
        elif "all operations stopped" in sl or "all stopped" in sl:
            if not self._pending_cmd:
                self.wifi_scanning = False
                self.ble_scanning = False
                self._wifi_scan_only = False
                self._ble_scan_only = False
                self._bt_tracking = False
                self._bt_airtag = False
                self._airtag_count = 0
                self._smarttag_count = 0
                self._wifi_scan_done_time = 0.0
                self._bt_scan_done_time = 0.0
            self.sniffing = False
            self.capturing_hs = False

        # --- Portal / Evil Twin capture parsing ---
        if self.state.portal_running or self.state.evil_twin_running:
            is_form = ("received post" in sl or "form submission" in sl)
            is_client = ("client connected" in sl or "client count" in sl)
            if is_form or is_client:
                tag = "EP" if self.state.portal_running else "ET"
                if self.state.portal_running and self.loot:
                    self.loot.save_portal_event(s)
                if self.state.evil_twin_running and self.loot:
                    self.loot.save_evil_twin_event(s)
                if is_form:
                    # URL-decode captured data for display
                    try:
                        from urllib.parse import unquote_plus
                        decoded = unquote_plus(s)
                    except Exception:
                        decoded = s
                    if self.state.portal_running:
                        self.state.submitted_forms += 1
                    if self.state.evil_twin_running:
                        self.state.evil_twin_captured_data.append(decoded)
                    # Parse POST fields for nice formatting
                    _fields = self._parse_post_fields(decoded)
                    if _fields:
                        self._term_add(f"[{tag}:PWD] {_fields}", raw=True)
                    else:
                        self._term_add(f"[{tag}:PWD] {decoded}", raw=True)
                    self.msg(f"[{tag}] CREDENTIAL CAPTURED!", 12)  # blue
                    self.gain_xp(150)
                    self.glitch_timer = 6
                    self._earn_badge("evil_twin")
                    self._load_loot_totals()  # refresh PWD count
                elif is_client:
                    if "client count" in sl:
                        self.state.portal_client_count = int(
                            re.search(r'\d+', s.split("=")[-1] if "=" in s else s).group()
                        ) if re.search(r'\d+', s) else 0
                    self.msg(f"[{tag}] Client connected", C_HACK_CYAN)
                    self._term_add(f"[{tag}:CLIENT] {s}", raw=True)
                    self.gain_xp(25)
                return  # don't add raw line to terminal again

        # --- Scan cycle completion ---
        # One-shot scans (SCAN tab): mark as done, stop running flag
        if "scan results printed" in sl:
            # Evil Twin scan pending → show network picker
            if self._et_scan_pending:
                self._et_scan_pending = False
                self._wifi_scan_done_time = time.time()
                nets = self.state.networks
                if nets:
                    self._attack_scan_results = list(nets)
                    self._show_net_selection()
                else:
                    self.msg("[ET] No networks found", C_WARNING)
                    self._attack_mode = ""
                    self._attack_step = ""
            elif self._wifi_scan_only:
                self._wifi_scan_only = False
                self.msg("[WiFi] Scan complete", C_SUCCESS)
            elif self.wifi_scanning:
                # Wardriving auto-repeat
                self._wifi_scan_done_time = time.time()
        if ("summary:" in sl or "scan complete" in sl
                or "ble scan done" in sl or "bt scan done" in sl):
            if self._ble_scan_only:
                self._ble_scan_only = False
                self.msg("[BLE] Scan complete", C_SUCCESS)
            elif self.ble_scanning:
                # Wardriving auto-repeat
                self._bt_scan_done_time = time.time()

        # Add to terminal panel (non-parsed lines)
        self._term_add(s)

    def _trigger_hs_event(self):
        """Trigger game event when handshake is captured."""
        self._last_hs_count += 1
        self.msg("[HS] Handshake captured!", C_SUCCESS)
        self.gain_xp(200)
        self._earn_badge("handshake_hunter")
        self.glitch_timer = 10
        self._remember_marker(MapMarker(
            self.player_lat, self.player_lon, "HS", "handshake",
            key=f"handshake:{time.monotonic_ns()}"))
        px, py = self.proj.geo_to_screen(self.player_lat, self.player_lon)
        for _ in range(30):
            self.particles.append(Particle(px, py, random.choice([11, 10, 3])))

    # ------------------------------------------------------------------
    # GPS polling
    # ------------------------------------------------------------------

    def _remember_live_map_node(self, node, kind):
        """Append a visual node and evict the globally oldest display node."""
        self._map_node_sequence = getattr(self, "_map_node_sequence", 0) + 1
        node.map_sequence = self._map_node_sequence
        if kind == "wifi":
            nodes = self.wifi_networks
            recent_name = "_recent_wifi_fx"
            count_name = "_session_wifi_discovered"
        else:
            nodes = self.ble_devices
            recent_name = "_recent_ble_fx"
            count_name = "_session_ble_discovered"
        nodes.append(node)
        setattr(self, count_name, getattr(self, count_name, 0) + 1)

        recent = getattr(self, recent_name, None)
        if recent is None:
            recent = []
            setattr(self, recent_name, recent)
        recent.append(node)
        if len(recent) > RECENT_NODE_FX_LIMIT:
            del recent[:-RECENT_NODE_FX_LIMIT]

        while len(self.wifi_networks) + len(self.ble_devices) > MAP_NODE_LIMIT:
            wifi_seq = (getattr(self.wifi_networks[0], "map_sequence", -1)
                        if self.wifi_networks else float("inf"))
            ble_seq = (getattr(self.ble_devices[0], "map_sequence", -1)
                       if self.ble_devices else float("inf"))
            if wifi_seq <= ble_seq:
                del self.wifi_networks[0]
            else:
                del self.ble_devices[0]
        self._live_map_revision = getattr(self, "_live_map_revision", 0) + 1

    def _ingest_ble(self, mac, rssi, name, tag_type="", observation=None, terminal=True):
        if self._whitelist.is_blocked(mac):
            return  # silently skip whitelisted BLE device
        self.wardrive.observe_legacy("ble", mac, name, rssi, observation)
        mac_upper = mac.upper()
        try:
            map_rssi = int(rssi)
        except (TypeError, ValueError):
            map_rssi = -100
        self._observe_map_radio(
            "ble", mac_upper, map_rssi, label=name, observation=observation)
        is_new = mac_upper not in self._known_ble
        if is_new:
            self._known_ble.add(mac_upper)
            if self.gps_fix:
                _dlat = (random.random()-0.5) * 0.002
                _dlon = (random.random()-0.5) * 0.002
            else:
                _dlat = _dlon = 0.0
            d = BleDevice(
                self.player_lat + _dlat,
                self.player_lon + _dlon,
                mac, name, rssi)
            d.spawn_frame = pyxel.frame_count
            self._remember_live_map_node(d, "ble")
            self.msg(f"[BLE] {d.name} {mac[-8:]} {rssi}dBm", C_HACK_CYAN)
            self.gain_xp(10)  # new device: full XP
        else:
            if observation is None:
                self.gain_xp(1)   # legacy duplicate XP only
        # Always save to loot (dedup + RSSI update handled by loot_manager)
        if self.loot:
            try:
                self.loot.save_bt_device(mac, rssi, name, False, False, **self.wardrive.save_args(observation))
                self.loot.save_wardriving_bt(mac, rssi, name, **self.wardrive.save_args(observation))
            except Exception:
                pass
        # Pretty terminal line instead of raw
        new_mark = "*" if is_new else " "
        suffix = f" {tag_type}" if tag_type else ""
        if terminal:
            self._term_add(
                f"[BLE]{new_mark} {name[:16]:<16} {mac} {rssi}dBm{suffix}",
                raw=True)
        return


    def _ingest_wifi(self, net, observation=None, terminal=True):
        bssid = net.bssid
        if bssid:
            if self._whitelist.is_blocked(bssid):
                return  # silently skip whitelisted WiFi
            try:
                detection_rssi = int(net.rssi)
            except (TypeError, ValueError):
                detection_rssi = -100
            self.wardrive.observe_legacy("wifi", bssid, net.ssid, detection_rssi, observation)
            bssid_upper = bssid.upper()
            self._observe_map_radio(
                "wifi", bssid_upper, detection_rssi,
                label=net.ssid or "<hidden>", observation=observation)
            # Keep full objects for target selection.  The old linear lookup
            # made large incoming scan batches progressively more expensive.
            network_index = getattr(self, "_state_network_by_bssid", None)
            if network_index is None:
                network_index = self._state_network_by_bssid = {
                    item.bssid.upper(): i
                    for i, item in enumerate(self.state.networks) if item.bssid}
            existing_index = network_index.get(bssid_upper)
            if (existing_index is not None
                    and existing_index < len(self.state.networks)
                    and self.state.networks[existing_index].bssid.upper() == bssid_upper):
                self.state.networks[existing_index] = net
            else:
                # Recover if a caller cleared or replaced the public list.
                if existing_index is not None:
                    network_index.clear()
                    network_index.update({
                        item.bssid.upper(): i
                        for i, item in enumerate(self.state.networks) if item.bssid})
                    existing_index = network_index.get(bssid_upper)
                if existing_index is None:
                    network_index[bssid_upper] = len(self.state.networks)
                    self.state.networks.append(net)
                else:
                    self.state.networks[existing_index] = net
            is_new = bssid_upper not in self._known_wifi
            if is_new:
                self._known_wifi.add(bssid_upper)
                try:
                    ch = int(net.channel)
                except (ValueError, TypeError):
                    ch = 0
                try:
                    rssi_v = int(net.rssi)
                except (ValueError, TypeError):
                    rssi_v = -80
                if self.gps_fix:
                    _dlat = (random.random()-0.5) * 0.002
                    _dlon = (random.random()-0.5) * 0.002
                else:
                    _dlat = _dlon = 0.0
                n = WifiNetwork(
                    self.player_lat + _dlat,
                    self.player_lon + _dlon,
                    bssid, net.ssid, ch, rssi_v)
                n.spawn_frame = pyxel.frame_count
                self._remember_live_map_node(n, "wifi")
                self.msg(f"[WiFi] {n.ssid} Ch:{ch}", C_WARNING)
                self.gain_xp(15)  # new network: full XP
            else:
                if observation is None:
                    self.gain_xp(1)   # legacy duplicate XP only
            # Always save to loot (dedup + RSSI update handled by loot_manager)
            if self.loot:
                try:
                    self.loot.save_wardriving_network(net, **self.wardrive.save_args(observation))
                except Exception:
                    pass
            # Pretty terminal line instead of raw CSV
            tag = "*" if is_new else " "
            ssid = net.ssid[:20] if net.ssid else "<hidden>"
            if terminal:
                self._term_add(
                    f"[WiFi]{tag} {ssid:<20} Ch:{net.channel:>2} "
                    f"{net.rssi:>4}dBm {net.auth[:8]:<8} {bssid}",
                    raw=True)
            return


    def _poll_gps(self):
        now = time.monotonic()
        if not self.gps.available:
            self.gps_fix = False
            if self._gps_enabled and now >= self._gps_retry_at:
                self._gps_retry_at = now + 5.0
                if self.gps.setup():
                    self._term_add(
                        f"[GPS] Reconnected through {self.gps.device}", raw=True)
            return
        sentences = self.gps.read_available()
        if sentences:
            self.gps.process_sentences(sentences)
        fix = self.gps.fix
        self.wardrive.fixes.update(fix, now)
        self.gps_sats = fix.satellites
        self.gps_sats_vis = fix.satellites_visible
        if fix.valid and now - fix.received_at <= 3:
            # Jitter filter: ignore moves < ~30 m (0.0003°)
            dlat = abs(fix.latitude - self.player_lat)
            dlon = abs(fix.longitude - self.player_lon)
            if dlat > 0.0003 or dlon > 0.0003 or not self.gps_fix:
                self.player_lat = fix.latitude
                self.player_lon = fix.longitude
            self.gps_fix = True
            self._manual_move = False
        else:
            self.gps_fix = False

    # ------------------------------------------------------------------
    # LoRa / MeshCore polling + callbacks
    # ------------------------------------------------------------------

    def _poll_lora(self):
        transition_queue = getattr(self, "_lora_transition_queue", None)
        if transition_queue is not None:
            try:
                while not transition_queue.empty():
                    result = transition_queue.get_nowait()
                    ok, detail = result[:2]
                    action = result[2] if len(result) > 2 else "handoff"
                    power_meta = (
                        result[3] if (action == "power_off"
                                      and len(result) > 3
                                      and isinstance(result[3], dict))
                        else {})
                    epoch = (
                        result[3] if (action != "power_off"
                                      and len(result) > 3) else
                        int(getattr(self, "_lora_start_epoch", 0)))
                    marker = (action, epoch)
                    if getattr(self, "_lora_handoff_pending", None) == marker:
                        self._lora_handoff_pending = None
                    prefix = "[SYS]" if ok else "[LoRa]"
                    self._term_add(f"{prefix} {detail}", raw=True)
                    if action == "power_off":
                        self._lora_power_on_pending = False
                        # Releasing the process lock is distinct from cutting
                        # the physical rail.  If GPIO succeeded but unlock
                        # acknowledgement failed, reflect the real powered-off
                        # state while the uncertainty guard blocks every new
                        # owner until restart.
                        rail_off = bool(power_meta.get("rail_off", ok))
                        if rail_off:
                            self._lora_enabled = False
                            self._lora_power_intent = False
                            self._lora_start_pending = ""
                            self._lora_handoff_pending = None
                            self.wardrive._wdg_owned_lora = False
                            self._mc_screen = False
                            self._mc_bubbles.clear()
                            if ok:
                                self.msg("[LoRa] OFF", C_WARNING)
                            else:
                                self.msg(f"[LoRa] OFF — {detail}", C_ERROR)
                            self.glitch_timer = 2
                        else:
                            self._lora_power_intent = self._lora_enabled
                            direct_pending = getattr(
                                self, "_lora_start_pending", "")
                            if (isinstance(direct_pending, tuple)
                                    and direct_pending[1] != int(getattr(
                                        self, "_lora_start_epoch", 0))):
                                self._lora_start_pending = ""
                            handoff_pending = getattr(
                                self, "_lora_handoff_pending", None)
                            if (isinstance(handoff_pending, tuple)
                                    and handoff_pending[1] != int(getattr(
                                        self, "_lora_start_epoch", 0))):
                                self._lora_handoff_pending = None
                            self.msg(f"[LoRa] {detail}", C_ERROR)
                        self._end_meshtastic_transition()
                    elif action == "power_on":
                        allowed = self._lora_start_completion_allowed(
                            action, epoch)
                        poweroff_cleanup = self._lora_poweroff_handles_cleanup()
                        if ok and allowed:
                            self._lora_power_on_pending = False
                            self.wardrive._wdg_owned_lora = True
                            self.msg("[LoRa] ON — Meshtastic ready", C_SUCCESS)
                            self._end_meshtastic_transition()
                        elif ok:
                            self.wardrive._wdg_owned_lora = False
                            if (not poweroff_cleanup
                                    and not self._meshtastic.close()):
                                self._term_add(
                                    "[MT] Cancelled start completed, but its "
                                    "client worker did not stop", raw=True)
                            if not poweroff_cleanup:
                                self._lora_power_on_pending = False
                                self._end_meshtastic_transition()
                        else:
                            self.wardrive._wdg_owned_lora = False
                            self.msg(f"[LoRa] {detail}", C_ERROR)
                            if (epoch != int(getattr(
                                    self, "_lora_start_epoch", 0))
                                    and self._lora_power_intent):
                                self._lora_power_on_pending = False
                                self._end_meshtastic_transition()
                            elif self._lora_power_intent:
                                self._start_failed_lora_power_on_rollback()
                    elif ok and self._lora_start_completion_allowed(
                            action, epoch):
                        self.wardrive._wdg_owned_lora = True
                    elif ok:
                        self.wardrive._wdg_owned_lora = False
                        if (not self._lora_poweroff_handles_cleanup()
                                and not self._meshtastic.close()):
                            self._term_add(
                                "[MT] Cancelled start completed, but its "
                                "client worker did not stop", raw=True)
                    elif not ok:
                        self.wardrive._wdg_owned_lora = False
                        self.msg(f"[LoRa] {detail}", C_ERROR)
            except Exception:
                pass
        if (getattr(self, "_lora_transition_thread", None) is not None
                and not self._lora_transition_active()):
            self._lora_transition_thread = None
        try:
            while not self._lora.queue.empty():
                text, attr = self._lora.queue.get_nowait()
                # Telemetry goes to terminal only, not chat
                self._term_add(f"[LoRa] {text}", raw=True)
                pending = getattr(self, "_lora_start_pending", "")
                if isinstance(pending, tuple):
                    pending_action, pending_epoch = pending
                else:
                    pending_action = pending
                    pending_epoch = int(
                        getattr(self, "_lora_start_epoch", 0))
                if attr == "success" and text.startswith("Sniffer started:"):
                    if pending:
                        self._lora_start_pending = ""
                        allowed = self._lora_start_completion_allowed(
                            pending_action, pending_epoch)
                        self.wardrive._wdg_owned_lora = allowed
                        if not allowed:
                            if (not self._lora_poweroff_handles_cleanup()
                                    and not self._lora.stop()):
                                self._term_add(
                                    "[LoRa] Cancelled direct-radio start did "
                                    "not release ownership", raw=True)
                        if pending_action == "power_on":
                            if allowed or not self._lora_poweroff_handles_cleanup():
                                self._lora_power_on_pending = False
                                self._end_meshtastic_transition()
                if attr == "error":
                    self.msg(f"[LoRa] {text}", C_ERROR)
                    if pending:
                        self._lora_start_pending = ""
                        self.wardrive._wdg_owned_lora = False
                        if pending_action == "power_on":
                            if (pending_epoch != int(getattr(
                                    self, "_lora_start_epoch", 0))
                                    and self._lora_power_intent):
                                self._lora_power_on_pending = False
                                self._end_meshtastic_transition()
                            elif self._lora_power_intent:
                                self._start_failed_lora_power_on_rollback()
        except Exception:
            pass
        # Process thread-safe event queue from callbacks
        try:
            while not self._mc_event_queue.empty():
                evt = self._mc_event_queue.get_nowait()
                if evt[0] == "msg":
                    _, channel, message, rssi, hops = evt
                    ts = time.strftime("%H:%M")
                    self._mc_log.append((f"\x10 {message}", C_SUCCESS, f"{ts} {hops}hop"))
                    self._mc_bubbles.append(
                        (message[:40], pyxel.frame_count + 300))
                    self.msg(f"[MC] {message[:30]}", C_SUCCESS)
                    # Play notification sound
                    self._play_mc_notify()
                    self._earn_badge("meshcore")
                elif evt[0] == "dm":
                    _, from_id, message, rssi, hops = evt
                    ts = time.strftime("%H:%M")
                    # Find sender name
                    sender = next(
                        (n["name"] for n in self._mc_nodes
                         if n["id"] == from_id), from_id[:8])
                    self._mc_log.append(
                        (f"\x10 [DM] {sender}: {message}",
                         12, f"{ts} {hops}hop"))  # 12 = blue
                    self._mc_bubbles.append(
                        (f"DM: {message[:30]}", pyxel.frame_count + 300))
                    self.msg(f"[DM] {sender}: {message[:25]}", 12)
                    self._play_mc_notify()
                elif evt[0] == "dm_ack":
                    _, ack_hash = evt
                    idx = self._mc_dm_ack_map.pop(ack_hash, None)
                    if idx is not None and idx < len(self._mc_log):
                        entry = self._mc_log[idx]
                        old_tag = entry[2] if len(entry) > 2 else ""
                        self._mc_log[idx] = (entry[0], entry[1],
                                             f"{old_tag} \u221a")
                elif evt[0] == "tx_confirm":
                    _, dk = evt
                    idx = self._mc_tx_pending.pop(dk, None)
                    if idx is not None and idx < len(self._mc_log):
                        entry = self._mc_log[idx]
                        # Replace tag with √ + timestamp
                        old_tag = entry[2] if len(entry) > 2 else ""
                        self._mc_log[idx] = (entry[0], entry[1],
                                             f"{old_tag} \u221a")
                elif evt[0] == "node":
                    (_, node_id, ntype, name, lat, lon, rssi, snr, pubkey,
                     hops) = evt
                    node_data = {
                        "id": node_id, "type": ntype, "name": name or "?",
                        "lat": lat, "lon": lon, "rssi": rssi,
                        "snr": snr or 0, "hops": hops,
                        "last_seen": time.time()}
                    if pubkey:
                        node_data["pubkey"] = pubkey.hex()
                    # Dedup by node_id
                    existing = next((n for n in self._mc_nodes
                                     if n["id"] == node_id), None)
                    if existing:
                        # Preserve persistent fields
                        node_data["first_seen"] = existing.get("first_seen", time.time())
                        node_data["note"] = existing.get("note", "")
                        existing.update(node_data)
                    else:
                        node_data["first_seen"] = time.time()
                        node_data["note"] = ""
                        self._mc_nodes.append(node_data)
                        self.gain_xp(20)  # XP for new contact
                    if lat and lon and lat != 0 and lon != 0:
                        self._remember_marker(MapMarker(
                            lat, lon, name or "MC", "meshcore",
                            key="meshcore:" + str(node_id)))
                    # Persist contact to loot_db
                    if self.loot:
                        self.loot.save_contact(node_id, dict(node_data))
                    # Node adverts go to terminal only, not chat
                    _type_names = {0: "Client", 1: "Client",
                                   2: "Repeater", 3: "Room", 4: "Sensor"}
                    tname = (ntype if isinstance(ntype, str)
                             else _type_names.get(ntype, f"T{ntype}"))
                    self.msg(f"[MC] {name or '?'} [{tname}] "
                             f"RSSI:{rssi:.0f}", C_HACK_CYAN)
        except Exception:
            pass
        # Prune expired bubbles
        frame = pyxel.frame_count
        self._mc_bubbles = [(t, e) for t, e in self._mc_bubbles if e > frame]

    def _poll_meshtastic(self):
        """Move meshtasticd callbacks onto the Pyxel/main thread."""
        try:
            events = self._meshtastic.poll_events()
        except Exception:
            return
        for event, data in events:
            if event in ("status", "discovery"):
                self._term_add(f"[MT] {data}", raw=True)
            elif event == "error":
                self._term_add(f"[MT] {data}", raw=True)
                self.msg(f"[MT] {str(data)[:70]}", C_ERROR)
            elif event == "connected":
                self._term_add(
                    f"[MT] Connected as {data.get('name') or data.get('node_id')}",
                    raw=True)
                self.msg("[MT] meshtasticd connected", C_SUCCESS)
            elif event == "disconnected":
                self._term_add(f"[MT] {data}", raw=True)
                self.msg("[MT] meshtasticd disconnected", C_WARNING)
            elif event == "message":
                sender = data.get("sender") or data.get("sender_id") or "?"
                text = str(data.get("text") or "")
                ts = time.strftime("%H:%M")
                hops = int(data.get("hops") or 0)
                self._mc_log.append((
                    f"\x10 {sender}: {text}", C_SUCCESS,
                    f"{ts} {hops}hop MT"))
                self._mc_bubbles.append(
                    (f"MT: {text[:36]}", pyxel.frame_count + 300))
                self.msg(f"[MT] {sender}: {text[:25]}", C_SUCCESS)
                self._play_mc_notify()
                if self.loot:
                    self.loot.save_meshtastic_message(
                        str(data.get("channel", 0)), sender, text,
                        float(data.get("rssi") or 0))
            elif event == "node":
                self._remember_meshtastic_node(data)

    def _poll_watch_autoconnect(self):
        """Reconnect a known watch only after BlueZ coordination is safe."""
        cancel = getattr(self, "_watch_autoconnect_cancel", None)
        if cancel is not None and cancel.is_set():
            return
        address = getattr(self, "_watch_autoconnect_address", "")
        if not address:
            return
        if self._watch.connected:
            self._watch_autoconnect_address = ""
            return
        if self._watch.worker_active:
            return
        manager = self._meshtastic
        if time.monotonic() < getattr(self, "_watch_autoconnect_next", 0.0):
            return
        # A starting fork has not opened the control socket yet. Waiting here
        # avoids racing its default pairing agent or advertising setup.
        if manager.connected:
            self._connect_watch_autoconnect(address)
            return
        result_queue = getattr(self, "_watch_autoconnect_result", None)
        if result_queue is None:
            result_queue = Queue(maxsize=1)
            self._watch_autoconnect_result = result_queue
        try:
            ready, detail = result_queue.get_nowait()
        except Empty:
            ready = None
            detail = ""
        if ready is True:
            self._watch_autoconnect_thread = None
            if self._connect_watch_autoconnect(address):
                self._watch_autoconnect_delay = 1.0
                self._watch_autoconnect_last_detail = ""
            else:
                self._watch_autoconnect_next = time.monotonic() + 0.25
            return
        if ready is False:
            self._watch_autoconnect_thread = None
            delay = float(getattr(self, "_watch_autoconnect_delay", 1.0))
            self._watch_autoconnect_next = time.monotonic() + delay
            self._watch_autoconnect_delay = min(15.0, delay * 2.0)
            if (detail and detail != getattr(
                    self, "_watch_autoconnect_last_detail", "")):
                self._term_add("[Watch] Auto-connect waiting: " + detail,
                               raw=True)
                self._watch_autoconnect_last_detail = detail
        thread = getattr(self, "_watch_autoconnect_thread", None)
        if thread is not None and thread.is_alive():
            return
        if not self._begin_meshtastic_transition("watch_autoconnect"):
            return

        def check_ready():
            try:
                if cancel is not None and cancel.is_set():
                    return
                value = bool(manager.ble_coordination_ready())
                if cancel is not None and cancel.is_set():
                    return
                result_queue.put_nowait((
                    value, "" if value else
                    "Meshtastic BLE coordination is not ready"))
            except Exception as exc:
                try:
                    result_queue.put_nowait((False, str(exc)[:100]))
                except Full:
                    pass
            finally:
                self._end_meshtastic_transition()

        self._watch_autoconnect_thread = threading.Thread(
            target=check_ready, name="wdg-watch-autoconnect", daemon=True)
        try:
            self._watch_autoconnect_thread.start()
        except Exception:
            self._watch_autoconnect_thread = None
            self._end_meshtastic_transition()
            raise

    def _connect_watch_autoconnect(self, address: str) -> bool:
        """Start the watch worker under the shared Bluetooth transition lock."""
        cancel = getattr(self, "_watch_autoconnect_cancel", None)
        if cancel is not None and cancel.is_set():
            return False
        if not self._begin_meshtastic_transition("watch_autoconnect_connect"):
            return False
        try:
            if cancel is not None and cancel.is_set():
                return False
            started = bool(self._watch.connect(address))
            if started:
                self._watch_autoconnect_address = ""
                self._watch_autoconnect_delay = 1.0
                self._watch_autoconnect_last_detail = ""
                return True
            delay = float(getattr(self, "_watch_autoconnect_delay", 1.0))
            self._watch_autoconnect_next = time.monotonic() + delay
            self._watch_autoconnect_delay = min(15.0, delay * 2.0)
            return False
        finally:
            self._end_meshtastic_transition()

    def _stop_watch_autoconnect(self, timeout: float = 7.0) -> bool:
        """Cancel and join readiness before the Meshtastic socket is closed."""
        cancel = getattr(self, "_watch_autoconnect_cancel", None)
        if cancel is not None:
            cancel.set()
        thread = getattr(self, "_watch_autoconnect_thread", None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        stopped = not thread or not thread.is_alive()
        if stopped:
            self._watch_autoconnect_thread = None
        return stopped

    def _start_meshtastic_update(self):
        """Check and install the newest compatible daemon release off-thread."""
        if self._meshtastic_update_running:
            self.msg("[MT] Service update is already running", C_DIM)
            return False
        if getattr(self, "_lora_power_ownership_uncertain", False):
            self.msg(
                "[MT] SX1262 ownership is uncertain; restart WatchDogsGo "
                "before updating the service", C_ERROR)
            return False
        if not self._begin_meshtastic_transition("update"):
            self.msg("[MT] Another radio/service operation is running",
                     C_WARNING)
            return False

        def reject(message):
            self.msg(message, C_WARNING)
            self._end_meshtastic_transition()
            return False

        if self._meshtastic_service is None:
            return reject(
                "[MT] Rerun setup.sh to install the protected updater")
        if not getattr(self, "_lora_enabled", False):
            return reject("[MT] Enable the LoRa power rail before updating")
        if self._lora_transition_active():
            return reject("[MT] Wait for the current radio handoff to finish")
        if (self._lora.running or self._lora.worker_active
                or self._lora.radio_owned):
            return reject("[MT] Stop MeshCore/direct LoRa before updating")
        bluetooth_busy = self._meshtastic_bluetooth_busy_reason()
        if bluetooth_busy:
            return reject("[MT] Finish " + bluetooth_busy + " before updating")
        wardrive = getattr(self, "wardrive", None)
        host_ble = getattr(wardrive, "host_ble", None)
        action_thread = getattr(wardrive, "_meshtastic_action_thread", None)
        if host_ble is not None and host_ble.worker_active:
            return reject("[MT] Stop the active host BLE scan before updating")
        if getattr(self._watch, "worker_active", False):
            return reject("[MT] Finish the active watch Bluetooth operation")
        if action_thread is not None and action_thread.is_alive():
            return reject("[MT] Wait for the current service control action")
        if (getattr(self._meshtastic, "ble_scan_lease_active", False)
                or getattr(self._meshtastic,
                           "pairing_agent_lease_active", False)):
            return reject("[MT] Finish the active Bluetooth lease before updating")
        self._meshtastic_update_running = True
        self.msg("[MT] Checking Smethan/meshtastic-firmware releases...", C_DIM)

        def worker():
            reconnect = False
            prior_backend = ""
            prior_service_target = None
            ok = False
            detail = "Meshtastic update did not complete"

            def restored_service_matches() -> bool:
                if prior_service_target not in ("wdg", "stock"):
                    return True
                current = self._meshtastic.active_service_target()
                return current == prior_service_target

            def reconnect_previous_backend() -> tuple[bool, str]:
                """Reconnect only after the helper proved rollback complete."""
                if not reconnect:
                    return True, ""
                try:
                    if not restored_service_matches():
                        return False, (
                            "the restored Meshtastic service does not match "
                            "the pre-update owner")
                    if not self._meshtastic.start():
                        return False, "the previous Meshtastic client did not restart"
                    if not self._meshtastic.wait_connected(
                            timeout=15.0, backend=prior_backend):
                        return False, (
                            "the previous backend did not recover ("
                            + prior_backend + ")")
                    return True, ""
                except Exception as exc:
                    return False, str(exc)[:120]

            try:
                # A stopped direct-radio session may still hold the exact
                # pre-session systemd snapshot.  Resolve that authoritative
                # token before release lookup or privileged package/service
                # mutation; an installer transaction must never coexist with
                # an older restore state that could later overwrite it.
                if self._meshtastic_service_restore_pending():
                    if not self._meshtastic.resume_service(
                            timeout=15.0, connect=False):
                        raise RuntimeError(
                            "Previous Meshtastic service state could not be "
                            "restored; update was not started")
                    if self._meshtastic_service_restore_pending():
                        raise RuntimeError(
                            "Previous Meshtastic service state remains "
                            "unresolved; update was not started")
                from .meshtastic_updates import (
                    meshtastic_releases, package_version_for_tag,
                )
                self._meshtastic_service.require_current()
                releases = meshtastic_releases()
                if not releases:
                    raise RuntimeError(
                        "No compatible Smethan Meshtastic release was found")
                tag = releases[0]["tag_name"]
                expected = package_version_for_tag(tag)
                status = self._meshtastic_service.status("wdg")
                if status.package_version == expected:
                    ok = True
                    detail = f"Already up to date ({tag})"
                else:
                    reconnect = bool(
                        self._meshtastic.running or self._meshtastic.connected)
                    prior_backend = str(
                        getattr(self._meshtastic, "active_backend", "") or "")
                    if prior_backend not in ("fork_socket", "legacy_tcp"):
                        prior_backend = (
                            "legacy_tcp"
                            if self._meshtastic.backend_mode == "legacy_tcp"
                            else "fork_socket")
                    try:
                        prior_service_target = (
                            self._meshtastic.active_service_target())
                    except Exception:
                        prior_service_target = None
                    if not self._meshtastic.close():
                        raise RuntimeError(
                            "Meshtastic client worker did not stop; package "
                            "update was aborted before installation")
                    try:
                        result = self._meshtastic_service.install_tag(tag)
                    except MeshtasticInstallError as install_exc:
                        if not install_exc.rollback_restored:
                            backup = (
                                " Backup: " + str(install_exc.backup)
                                if install_exc.backup is not None else "")
                            raise RuntimeError(
                                str(install_exc)[:150]
                                + "; automatic rollback was not confirmed; "
                                "the Meshtastic client remains stopped."
                                + backup) from install_exc
                        restored, restore_detail = reconnect_previous_backend()
                        if not restored:
                            raise RuntimeError(
                                str(install_exc)[:120]
                                + "; package rollback completed, but "
                                + restore_detail) from install_exc
                        raise RuntimeError(
                            str(install_exc)[:140]
                            + "; package update was rolled back and the "
                            "previous backend was restored") from install_exc
                    installed = result.get("package_version") or expected
                    expected_backend = (
                        "legacy_tcp"
                        if self._meshtastic.backend_mode == "legacy_tcp"
                        else "fork_socket")
                    try:
                        # The installer validates and enables the fork.
                        # Preserve an explicit legacy selection across reboot
                        # by restoring its exact service selection.
                        if self._meshtastic.backend_mode == "legacy_tcp":
                            self._meshtastic_service.select("stock")
                        if reconnect:
                            if not self._meshtastic.start():
                                raise RuntimeError(
                                    "updated Meshtastic client did not restart")
                            if not self._meshtastic.wait_connected(
                                    timeout=15.0, backend=expected_backend):
                                raise RuntimeError(
                                    "updated " + expected_backend
                                    + " backend did not complete negotiation")
                    except Exception as validation_exc:
                        # The privileged helper saved an immutable transaction
                        # pointer during install.  Roll back the package and
                        # exact service snapshot before attempting to restore
                        # WDG's previous client connection.
                        if (self._meshtastic.running
                                or self._meshtastic.connected):
                            if not self._meshtastic.close():
                                raise RuntimeError(
                                    str(validation_exc)[:110]
                                    + "; rollback is blocked because the "
                                    "updated client worker did not stop; the "
                                    "protected transaction was retained") \
                                    from validation_exc
                        try:
                            self._meshtastic_service.rollback()
                        except Exception as rollback_exc:
                            raise RuntimeError(
                                str(validation_exc)[:100]
                                + "; automatic rollback failed: "
                                + str(rollback_exc)[:100]) from validation_exc
                        restored, restore_detail = reconnect_previous_backend()
                        if not restored:
                            raise RuntimeError(
                                str(validation_exc)[:100]
                                + "; package was rolled back, but "
                                + restore_detail) from validation_exc
                        raise RuntimeError(
                            str(validation_exc)[:120]
                            + "; package update was rolled back and the "
                            "previous backend was restored") \
                            from validation_exc
                    ok = True
                    detail = f"Installed {tag} ({installed}); service ready"
            except Exception as exc:
                detail = str(exc)[:220]
            finally:
                self._meshtastic_update_result.put((ok, detail))
                self._end_meshtastic_transition()

        try:
            factory = getattr(
                self, "_meshtastic_update_thread_factory", threading.Thread)
            thread = factory(
                target=worker, name="wdg-meshtastic-update",
                # Package validation/rollback must outlive the UI.  Cleanup
                # joins this worker before it closes the control socket.
                daemon=False)
            self._meshtastic_update_thread = thread
            thread.start()
        except Exception as exc:
            self._meshtastic_update_thread = None
            self._meshtastic_update_running = False
            self._end_meshtastic_transition()
            self.msg(
                "[MT] Could not start update worker: " + str(exc)[:90],
                C_ERROR)
            return False
        return True

    def _poll_meshtastic_update(self):
        result_queue = getattr(self, "_meshtastic_update_result", None)
        if result_queue is None:
            return
        try:
            ok, detail = result_queue.get_nowait()
        except Empty:
            return
        self._meshtastic_update_running = False
        update_thread = getattr(self, "_meshtastic_update_thread", None)
        try:
            if update_thread is not None and not update_thread.is_alive():
                self._meshtastic_update_thread = None
        except Exception:
            pass
        message = "[MT] " + detail
        self._term_add(message, raw=True)
        self.msg(message, C_SUCCESS if ok else C_ERROR)

    def _remember_meshtastic_node(self, data):
        """Update the shared mesh contacts view and wardrive observations."""
        address = str(data.get("id") or "")
        if not address:
            return
        contact_id = "meshtastic:" + address
        node_data = {
            "id": contact_id,
            "address": address,
            "protocol": "meshtastic",
            "type": "Meshtastic",
            "name": data.get("name") or address,
            "lat": float(data.get("lat") or 0),
            "lon": float(data.get("lon") or 0),
            "rssi": float(data.get("rssi") or 0),
            "snr": float(data.get("snr") or 0),
            "hops": int(data.get("hops") or 0),
            "last_seen": float(data.get("last_heard") or time.time()),
            "hardware": data.get("hardware") or "",
        }
        existing = next((n for n in self._mc_nodes
                         if n.get("id") == contact_id), None)
        if existing:
            node_data["first_seen"] = existing.get("first_seen", time.time())
            node_data["note"] = existing.get("note", "")
            existing.update(node_data)
        else:
            node_data["first_seen"] = time.time()
            node_data["note"] = ""
            self._mc_nodes.append(node_data)
            if not data.get("cached"):
                self.gain_xp(20)

        if self.loot:
            self.loot.save_contact(contact_id, dict(node_data))
        if data.get("cached"):
            return

        heard_lat = self.player_lat if self.gps_fix else 0.0
        heard_lon = self.player_lon if self.gps_fix else 0.0
        marker_lat = node_data["lat"] or heard_lat
        marker_lon = node_data["lon"] or heard_lon
        if marker_lat and marker_lon:
            self._remember_marker(MapMarker(
                marker_lat, marker_lon, node_data["name"], "meshcore",
                key="meshtastic:" + address))
        if self.loot:
            self.loot.save_meshtastic_node(
                address, node_data["name"], node_data["lat"],
                node_data["lon"], node_data["rssi"], node_data["snr"],
                node_data["hops"], heard_lat, heard_lon,
                node_data["hardware"])

    def _on_mc_node(self, node_id, ntype, name, lat, lon, rssi, snr,
                    pubkey=None, hops=0):
        """Callback from LoRaManager background thread — enqueue for main."""
        self._mc_event_queue.put(
            ("node", node_id, ntype, name, lat, lon, rssi, snr, pubkey,
             hops))
        if self.loot:
            try:
                self.loot.save_meshcore_node(
                    node_id, ntype, name or "", lat, lon, rssi, snr,
                    pubkey.hex() if pubkey else "", hops)
            except Exception:
                pass

    _mc_sound_loaded = False

    def _play_mc_notify(self):
        """Play MeshCore notification sound via pyxel audio."""
        if not self._mc_sound_loaded:
            _project_root = Path(__file__).resolve().parent.parent
            wav = _project_root / "assets" / "meshcore_notify.wav"
            if not wav.exists():
                return
            try:
                pyxel.sounds[63].pcm(str(wav))
                self._mc_sound_loaded = True
            except Exception:
                return
        try:
            pyxel.play(3, 63, loop=False)
        except Exception:
            pass

    def _on_mc_message(self, channel, message, rssi, hops=0):
        """Callback from LoRaManager background thread — enqueue for main."""
        self._mc_event_queue.put(("msg", channel, message, rssi, hops))
        if self.loot:
            try:
                self.loot.save_meshcore_message(channel, message, rssi)
            except Exception:
                pass

    def _on_mc_dm(self, from_id, message, rssi, hops=0):
        """Callback: incoming DM addressed to us (dedup by content)."""
        import time as _time
        key = f"dm:{from_id}:{message}"
        now = _time.time()
        last = getattr(self, '_dm_dedup', {}).get(key, 0)
        if now - last < 30:
            return  # duplicate DM within 30s
        if not hasattr(self, '_dm_dedup'):
            self._dm_dedup = {}
        self._dm_dedup[key] = now
        # Prune old entries
        if len(self._dm_dedup) > 50:
            self._dm_dedup = {k: v for k, v in self._dm_dedup.items()
                              if now - v < 60}
        self._mc_event_queue.put(("dm", from_id, message, rssi, hops))

    def _on_mc_dm_ack(self, ack_hash):
        """Callback: ACK received for our sent DM."""
        self._mc_event_queue.put(("dm_ack", ack_hash))

    def _on_mc_tx_confirm(self, dedup_key):
        """Callback: our TX packet was retransmitted by another node."""
        self._mc_event_queue.put(("tx_confirm", dedup_key))

    # ------------------------------------------------------------------
    # Loot points refresh
    # ------------------------------------------------------------------

    def _refresh_loot_points(self):
        if not self.loot:
            return
        self._apply_loot_snapshot(self._collect_loot_snapshot())

    def _collect_loot_snapshot(self):
        """Read historical map and password data without mutating game state."""
        snapshot = {"points": None, "passwords": None, "totals": None}
        try:
            snapshot["points"] = self.loot.get_gps_points()
        except Exception:
            pass
        try:
            loot_dir = Path(self._app_dir) / "loot"
            pwd_data = upload_manager.load_wpasec_passwords(loot_dir)
            snapshot["passwords"] = {
                ssid: entries[0]["password"]
                for ssid, entries in pwd_data.get("by_ssid", {}).items()
            }
        except Exception:
            pass
        try:
            t = dict(self.loot.loot_totals)
            snapshot["totals"] = {
                "sessions":    t.get("sessions", 0),
                "wifi":        t.get("wardriving_wifi", t.get("wardriving", 0)),
                "bt":          t.get("bt_devices", 0),
                "cell":        t.get("cell_cells", 0),
                "hs":          t.get("hc22000", 0),
                "pcap":        t.get("pcap", 0),
                "passwords":   t.get("passwords", 0),
                "et_captures": t.get("et_captures", 0),
                "mc_nodes":    t.get("mc_nodes", 0) + t.get("mt_nodes", 0),
                "mc_msgs":     t.get("mc_messages", 0) + t.get("mt_messages", 0),
            }
        except Exception:
            pass
        return snapshot

    def _apply_loot_snapshot(self, snapshot):
        points = snapshot.get("points")
        if points is not None and points is not self.loot_points:
            self.loot_points = points
            self._loot_points_revision = getattr(
                self, "_loot_points_revision", 0) + 1
            self._map_loot_view_key = None
            self._map_history_candidates_key = None
        passwords = snapshot.get("passwords")
        if passwords is not None:
            self._cracked_ssids = passwords
        totals = snapshot.get("totals")
        if totals is not None:
            self._loot_totals = totals

    def _start_loot_refresh(self):
        if not self.loot or getattr(self, "_loot_refresh_active", False):
            return
        if not hasattr(self, "_loot_refresh_result"):
            self._loot_refresh_result = Queue(maxsize=1)
        self._loot_refresh_active = True

        def worker():
            try:
                result = self._collect_loot_snapshot()
            except Exception:
                result = None
            self._loot_refresh_result.put(result)

        threading.Thread(target=worker, name="loot-map-refresh", daemon=True).start()

    def _poll_loot_refresh(self):
        result_queue = getattr(self, "_loot_refresh_result", None)
        if result_queue is None:
            return
        try:
            snapshot = result_queue.get_nowait()
        except Exception:
            return
        self._loot_refresh_active = False
        if snapshot is not None:
            self._apply_loot_snapshot(snapshot)

    def _load_loot_totals(self):
        if not self.loot:
            return
        try:
            t = self.loot.loot_totals
            self._loot_totals = {
                "sessions":    t.get("sessions", 0),
                "wifi":        t.get("wardriving_wifi", t.get("wardriving", 0)),
                "bt":          t.get("bt_devices", 0),
                "cell":        t.get("cell_cells", 0),
                "hs":          t.get("hc22000", 0),
                "pcap":        t.get("pcap", 0),
                "passwords":   t.get("passwords", 0),
                "et_captures": t.get("et_captures", 0),
                "mc_nodes":    t.get("mc_nodes", 0) + t.get("mt_nodes", 0),
                "mc_msgs":     t.get("mc_messages", 0) + t.get("mt_messages", 0),
            }
        except Exception:
            pass

    def _scan_loot_dir(self, base: Path) -> list[dict]:
        """Read GPS points from any loot base directory (JanOS-compatible format)."""
        import csv as _csv
        points: list[dict] = []
        for session_dir in base.iterdir():
            if not session_dir.is_dir():
                continue
            # wardriving.csv (WiGLE format — skip 2 header lines)
            wd = session_dir / "wardriving.csv"
            if wd.is_file():
                try:
                    with open(wd, encoding="utf-8", newline="") as fh:
                        next(fh, None)
                        for row in _csv.DictReader(fh):
                            try:
                                lat = float(row.get("CurrentLatitude", 0))
                                lon = float(row.get("CurrentLongitude", 0))
                                if lat == 0.0 and lon == 0.0:
                                    continue
                                raw_type = row.get("Type", "WIFI").upper()
                                ptype = "bt" if raw_type in ("BT", "BLE") else "cell" if raw_type in ("GSM", "WCDMA", "LTE", "NR") else "wifi"
                                points.append({"lat": lat, "lon": lon,
                                               "type": ptype,
                                               "label": row.get("SSID") or row.get("MAC", "?")})
                            except (ValueError, TypeError):
                                pass
                except OSError:
                    pass
            # handshakes/*.gps.json
            hs_dir = session_dir / "handshakes"
            if hs_dir.is_dir():
                for gps_file in hs_dir.glob("*.gps.json"):
                    try:
                        import json as _json
                        data = _json.loads(gps_file.read_text())
                        lat = data.get("Latitude", data.get("lat", 0))
                        lon = data.get("Longitude", data.get("lon", 0))
                        if lat and lon and not (lat == 0.0 and lon == 0.0):
                            label = gps_file.stem.replace(".gps", "").split("_")[0]
                            points.append({"lat": float(lat), "lon": float(lon),
                                           "type": "handshake", "label": label[:20]})
                    except Exception:
                        pass
            # meshcore_nodes.csv
            mc = session_dir / "meshcore_nodes.csv"
            if mc.is_file():
                try:
                    with open(mc, encoding="utf-8") as fh:
                        for i, line in enumerate(fh):
                            if i == 0:
                                continue
                            p = line.strip().split(",")
                            if len(p) >= 6:
                                try:
                                    lat, lon = float(p[4]), float(p[5])
                                    if lat == 0.0 and lon == 0.0:
                                        continue
                                    points.append({"lat": lat, "lon": lon,
                                                   "type": "meshcore",
                                                   "label": p[3] if len(p) > 3 else ""})
                                except (ValueError, IndexError):
                                    pass
                except OSError:
                    pass
        return points

    # ------------------------------------------------------------------
    # Hack mechanic
    # ------------------------------------------------------------------

    def _update_hack(self):
        if pyxel.btn(pyxel.KEY_SPACE) and not self.menu_open:
            if not self.hacking:
                best, best_d = None, 999
                px, py = W // 2, HUD_TOP + MAP_H // 2
                for d in self.ble_devices + self.wifi_networks:
                    if d.hacked: continue
                    sx, sy = self.proj.geo_to_screen(d.lat, d.lon)
                    dist = math.hypot(sx - px, sy - py)
                    if dist < 40 and dist < best_d:
                        best, best_d = d, dist
                if best:
                    self.hacking, self.hack_target, self.hack_progress = True, best, 0
            elif self.hack_target:
                self.hack_progress += 1
                if pyxel.frame_count % 3 == 0: self.glitch_timer = 2
                if self.hack_progress >= 45:
                    hacked_before = self._current_hacked_count()
                    self.hack_target.hacked = True
                    # Live map rendering uses detached registry snapshots.
                    # Mirror the game-only hacked flag into that snapshot so
                    # completing a hack immediately changes the cached dot.
                    if isinstance(self.hack_target, WifiNetwork):
                        hack_layer = "wifi"
                        hack_identity = self.hack_target.bssid
                    else:
                        hack_layer = "ble"
                        hack_identity = self.hack_target.mac
                    self._map_registry().observe(
                        hack_layer, hack_identity,
                        seen_at=time.monotonic(),
                        lat=self.hack_target.lat,
                        lon=self.hack_target.lon,
                        rssi=self.hack_target.rssi,
                        metadata={"hacked": True})
                    self._live_map_revision = getattr(
                        self, "_live_map_revision", 0) + 1
                    self._pwned_count = hacked_before + 1
                    self.hacking = False
                    self.gain_xp(50)
                    name = getattr(self.hack_target, "name",
                                   getattr(self.hack_target, "ssid", "?"))
                    self.msg(f"[PWNED] {name}", C_SUCCESS)
                    sx, sy = self.proj.geo_to_screen(
                        self.hack_target.lat, self.hack_target.lon)
                    for _ in range(20):
                        self.particles.append(Particle(sx, sy, C_SUCCESS))
                    self.hack_target = None
        else:
            if self.hacking and self.hack_progress < 45:
                self.hacking, self.hack_target = False, None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _cleanup(self):
        """Send stop to ESP32, close serial and GPS."""
        watch_ready_stopped = self._stop_watch_autoconnect(timeout=7.0)
        host_ble_stopped = True
        watch_stopped = True
        if not watch_ready_stopped:
            self._term_add(
                "[Watch] Auto-connect readiness did not stop; preserving "
                "the Meshtastic socket", raw=True)
        if hasattr(self, "wardrive"):
            self.wardrive.on_stop()
            host_ble = getattr(self.wardrive, "host_ble", None)
            close_host_ble = getattr(host_ble, "close", None)
            if (close_host_ble is not None
                    and close_host_ble(timeout=7.0) is False):
                host_ble_stopped = False
                self._term_add(
                    "[BLE] Host scan did not stop before shutdown", raw=True)
            self.wardrive.close_passive()
        # Bluetooth workers must finish their daemon lease releases while the
        # restricted Meshtastic socket is still available.
        if (self._watch.connected or self._watch.worker_active
                or getattr(
                    self._watch, "pairing_lease_release_pending", False)
                or getattr(
                    self._watch, "scan_lease_release_pending", False)):
            if self._watch.close(timeout=7.0) is False:
                watch_stopped = False
                self._term_add(
                    "[Watch] Bluetooth worker did not stop before shutdown",
                    raw=True)
        update_thread = getattr(self, "_meshtastic_update_thread", None)
        if (update_thread is not None
                and update_thread is not threading.current_thread()):
            # A package transaction includes service validation and automatic
            # rollback.  Never let application shutdown close its control
            # socket or terminate the process between those steps.
            update_thread.join()
            try:
                if not update_thread.is_alive():
                    self._meshtastic_update_thread = None
                    self._meshtastic_update_running = False
            except Exception:
                pass
        if self.serial and self.serial.is_open:
            try:
                self.serial.send_command("stop")
                time.sleep(0.2)
            except Exception:
                pass
            self.serial.close()
        self.gps.close()
        self._cancel_pending_lora_start()
        transitions = self._live_lora_transition_threads()
        for transition in transitions:
            if transition is not threading.current_thread():
                # Service helper calls are bounded, and every displaced worker
                # stays registered.  Shutdown waits for all of them so no
                # daemon/service mutation can outlive application cleanup.
                transition.join()
        transition_pending = bool(self._live_lora_transition_threads())
        if not transition_pending:
            if self._lora.running or self._lora.worker_active:
                self._lora.stop()
            # Disconnect only WDG. Keep meshtasticd alive for the user's other
            # messaging clients after WDG exits.
            if watch_ready_stopped and watch_stopped and host_ble_stopped:
                self._meshtastic.close()
            else:
                self._term_add(
                    "[MT] Preserving the control socket until Bluetooth "
                    "lease cleanup completes", raw=True)
        else:
            self._term_add(
                "[LoRa] Radio handoff still active during shutdown", raw=True)
        if self._sdr.running:
            self._sdr.stop()
        for p in self._plugins:
            try:
                p.on_unload()
            except Exception:
                pass
        if self.loot:
            try:
                self._save_xp_if_dirty()
                self.loot.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Draw
    # ------------------------------------------------------------------

    def _draw_plugin_pin(self):
        """Draw auth PIN popup from plugins (on top of everything)."""
        for p in self._plugins:
            if hasattr(p, '_auth_pin_active') and p._auth_pin_active():
                p._draw_auth_pin()

    def draw(self):
        self._draw_inner()
        self._draw_plugin_pin()
        self.wardrive.draw_overlay()

    def _draw_inner(self):
        if self._boot_phase:
            self._draw_boot_screen()
            return
        if self.wardrive.capture_screen.open:
            self.wardrive.capture_screen.draw()
            return
        if self.wardrive.hs_screen.open:
            self.wardrive.hs_screen.draw()
            return
        if self._mitm_screen:
            self._draw_mitm_screen()
            if self.input_mode:
                self._draw_input_dialog()
            self._draw_mc_toast()
            return
        for p in self._plugins:
            if getattr(p, 'overlay_active', False):
                p.draw(0, 0, 640, 360)
                self._draw_mc_toast()
                return
        if self._watch_screen:
            self._draw_watch_screen()
            self._draw_mc_toast()
            return
        if self._wl_screen:
            self._draw_wl_screen()
            if self.input_mode:
                self._draw_input_dialog()
            self._draw_mc_toast()
            return
        if self._mc_screen:
            self._draw_mc_screen()
            return
        if self._mc_region_screen:
            self._draw_mc_region_picker()
            return
        if self.loot_screen:
            self._draw_loot_screen()
            self._draw_mc_toast()
            return
        if self._flipper_screen:
            self._draw_flipper_screen()
            self._draw_mc_toast()
            return
        tiles_drawn = False
        if self.tile_renderer and self.tile_renderer.has_tiles():
            pyxel.cls(0)  # black bg for dark tiles
            tiles_drawn = self.tile_renderer.draw(
                self.proj, W, H, HUD_TOP, TERM_Y)
        if not tiles_drawn:
            pyxel.cls(0)  # dark bg even without tiles
        if not tiles_drawn:
            self._draw_coastlines()
        self._draw_grid()
        self.wardrive.draw_trail()
        self._draw_wardrive_nodes()
        self._draw_scan_fx()
        self._draw_player()
        self.wardrive.draw_markers()
        # Radar-style overlays go on top of the player skull so they're
        # visible even at low zoom, where every source within the dongle
        # range lands within the 12-pixel ring around the centre.
        self._draw_aircraft()
        self._draw_sensors()
        self._draw_markers()
        self._draw_particles()
        self._draw_hack_bar()
        self._draw_glitch()
        self._draw_terminal()
        self._draw_hud_top()
        self._draw_hud_bottom()
        self._draw_messages()
        self._draw_radar()
        self._draw_scanlines()
        if self._cluster_popup:
            self._draw_cluster_popup()
        # Cluster mode hint (above terminal, visible on map)
        visible_cluster_count = (len(
            self._get_history_node_layer().visible_clusters(self.proj))
            if self.wardrive.show_network_dots() else 0)
        if not self.menu_open and not self._cluster_popup and visible_cluster_count:
            if self._cluster_sel >= 0:
                cl = self._clusters[self._cluster_sel] if self._cluster_sel < len(self._clusters) else None
                n = cl["count"] if cl else 0
                hint = f"[C]exit  [arrows]nav  [RET]open  ({n} APs)"
                pyxel.text(W - len(hint) * 4 - 4, TERM_Y - 8, hint, C_HACK_CYAN)
            else:
                hint = f"[C] Cluster Select  ({visible_cluster_count})"
                pyxel.text(W - len(hint) * 4 - 4, TERM_Y - 8, hint, C_TEXT)
        if self.menu_open:
            self._draw_menu()
            self._draw_menu_hacker()
        if self._et_net_screen:
            self._draw_net_picker()
        if self._portal_select_screen:
            self._draw_portal_picker()
        if getattr(self, '_portal_data_screen', False):
            self._draw_captured_data()
        if getattr(self, '_flash_screen', False):
            self._draw_flash_screen()
        if self.input_mode:
            self._draw_input_dialog()
        if self.confirm_quit:
            self._draw_confirm_quit()
        if self._gps_wait_dialog:
            self._draw_gps_wait_dialog()
        if getattr(self, '_ota_screen', False):
            self._draw_ota_screen()

    def _draw_coastlines(self):
        vl = self.proj.center_lat - self.proj.lat_span
        vh = self.proj.center_lat + self.proj.lat_span
        wl = self.proj.center_lon - self.proj.lon_span
        wh = self.proj.center_lon + self.proj.lon_span
        hi_zoom = self.proj.zoom >= 5
        geo2scr = self.proj.geo_to_screen
        visible = self.proj.screen_visible
        W_THRESH = W * 0.8
        for seg, bounds in zip(self._coastlines, self._coast_bounds):
            if bounds is None: continue
            min_lat, max_lat, min_lon, max_lon, antimerid = bounds
            if max_lat < vl or min_lat > vh: continue
            if not antimerid and (max_lon < wl or min_lon > wh): continue
            psx, psy = None, None
            for lat, lon in seg:
                sx, sy = geo2scr(lat, lon)
                if psx is not None and abs(sx - psx) < W_THRESH:
                    pyxel.line(psx, psy, sx, sy, C_LAND)
                if hi_zoom and visible(sx, sy):
                    pyxel.pset(sx, sy, C_COAST)
                psx, psy = sx, sy

    def _draw_grid(self):
        if self.proj.zoom < 4: return
        sp = max(0.01, self.proj.lon_span / 8)
        for e in [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 30]:
            if e >= sp: sp = e; break
        lat = int((self.proj.center_lat - self.proj.lat_span) / sp) * sp
        while lat < self.proj.center_lat + self.proj.lat_span:
            _, sy = self.proj.geo_to_screen(lat, 0)
            if HUD_TOP < sy < TERM_Y:
                pyxel.line(0, sy, W-1, sy, C_GRID)
            lat += sp
        lon = int((self.proj.center_lon - self.proj.lon_span) / sp) * sp
        while lon < self.proj.center_lon + self.proj.lon_span:
            sx, _ = self.proj.geo_to_screen(0, lon)
            if 0 < sx < W:
                pyxel.line(sx, HUD_TOP, sx, TERM_Y, C_GRID)
            lon += sp

    # ------------------------------------------------------------------
    # Map clustering
    # ------------------------------------------------------------------

    def _map_registry(self):
        registry = getattr(self, "_map_observations", None)
        if registry is None:
            wardrive = getattr(self, "wardrive", None)
            lifetime = (wardrive.fade_seconds()
                        if wardrive and hasattr(wardrive, "fade_seconds") else 30)
            registry = self._map_observations = RecentObservationRegistry(
                capacity=MAP_NODE_LIMIT, lifetime=lifetime)
        return registry

    def _tick_map_display(self, now=None):
        now = time.monotonic() if now is None else now
        if now < getattr(self, "_map_display_next_tick", 0.0):
            return
        self._map_display_next_tick = now + 0.5
        registry = self._map_registry()
        lifetime = self.wardrive.fade_seconds()
        if registry.lifetime != lifetime:
            registry.lifetime = float(lifetime)
        result = registry.tick(now)
        refresh_notables = getattr(
            getattr(self, "wardrive", None),
            "_refresh_notable_identities", None)
        if refresh_notables is not None:
            refresh_notables(now)
        if result.expired:
            # Expired pixels must disappear now, even if an older incremental
            # build was still in flight.
            for name in ("_live_node_layer", "_radar_node_layer"):
                layer = getattr(self, name, None)
                if layer is not None:
                    layer.invalidate()

    def _get_live_map_views(self, now=None):
        now = time.monotonic() if now is None else now
        registry = self._map_registry()
        wardrive = getattr(self, "wardrive", None)
        modes = ((wardrive.layer_mode("wifi"), wardrive.layer_mode("ble"))
                 if wardrive and hasattr(wardrive, "layer_mode")
                 else (MODE_KEEP, MODE_KEEP))
        key = (registry.revision, modes, registry.lifetime)
        if key != getattr(self, "_map_live_view_key", None):
            self._map_live_view_key = key
            self._map_live_views = (
                registry.snapshot(layer="wifi", mode=modes[0], now=now),
                registry.snapshot(layer="ble", mode=modes[1], now=now),
            )
        return self._map_live_views

    def _on_map_policy_changed(self):
        """Immediately discard overlays built under an older display policy."""
        self._map_live_view_key = None
        self._map_loot_view_key = None
        self._map_history_candidates_key = None
        self._map_display_next_tick = 0.0
        registry = self._map_registry()
        registry.lifetime = float(self.wardrive.fade_seconds())
        for name in ("_history_node_layer", "_live_node_layer",
                     "_radar_node_layer"):
            layer = getattr(self, name, None)
            if layer is not None:
                layer.invalidate()

    def _observe_map_radio(self, layer, identity, rssi, *, label="",
                           observation=None):
        now = time.monotonic()
        fix = observation.get("fix") if observation else None
        if fix:
            lat, lon = fix.get("latitude"), fix.get("longitude")
        else:
            lat, lon = self.player_lat, self.player_lon
        self._map_registry().observe(
            layer, identity, seen_at=now, lat=lat, lon=lon, rssi=rssi,
            metadata={"label": label})
        self.wardrive.trail.note_observation(f"{layer}:{identity}", now)

    def _get_map_loot_points(self):
        """Return enabled KEEP history within the bounded display budget."""
        points = self.loot_points
        wifi_view, ble_view = self._get_live_map_views()
        live_count = len(wifi_view) + len(ble_view)
        if not hasattr(self, "wardrive"):
            live_count = (len(getattr(self, "wifi_networks", ()))
                          + len(getattr(self, "ble_devices", ())))
        live_count = min(MAP_NODE_LIMIT, live_count)
        block = 64 if hasattr(self, "wardrive") else 128
        live_reservation = min(
            MAP_NODE_LIMIT, ((live_count + block - 1) // block) * block)
        history_limit = MAP_NODE_LIMIT - live_reservation
        wardrive = getattr(self, "wardrive", None)
        modes = (tuple(wardrive.layer_mode(layer)
                       for layer in ("wifi", "ble", "cell"))
                 if wardrive and hasattr(wardrive, "layer_mode")
                 else (MODE_KEEP, MODE_KEEP, MODE_KEEP))
        live_ids = frozenset(
            (item.layer, item.identity)
            for item in tuple(wifi_view) + tuple(ble_view))
        if wardrive is not None:
            live_ids = live_ids.union(
                ("cell", str(item.get("identity", "")).upper())
                for item in getattr(wardrive, "cell_candidates", ())
                if item.get("identity"))
        source_key = (id(points), len(points),
                      getattr(self, "_loot_points_revision", 0), modes)
        if source_key != getattr(self, "_map_history_candidates_key", None):
            self._map_history_candidates_key = source_key
            enabled = {
                "wifi": modes[0] == MODE_KEEP,
                "bt": modes[1] == MODE_KEEP,
                "cell": modes[2] == MODE_KEEP,
            }
            candidates = [
                point for point in points
                if not point.get("type", "")
                or enabled.get(point.get("type", ""), False)
            ]
            # At most 512 live identities can mask saved history. Keeping
            # twice the display budget guarantees enough older candidates
            # without rescanning every point on each incoming scan burst.
            self._map_history_candidates = candidates[-2 * MAP_NODE_LIMIT:]
        key = (source_key, history_limit, hash(live_ids))
        if key != getattr(self, "_map_loot_view_key", None):
            self._map_loot_view_key = key
            filtered = []
            for point in reversed(self._map_history_candidates):
                point_type = point.get("type", "")
                identity = str(point.get("bssid", "")).upper()
                live_type = "ble" if point_type == "bt" else point_type
                if identity and (live_type, identity) in live_ids:
                    continue
                filtered.append(point)
                if len(filtered) >= history_limit:
                    break
            filtered.reverse()
            self._map_loot_view = filtered if history_limit else []
            self._map_loot_view_revision = getattr(
                self, "_map_loot_view_revision", 0) + 1
        return self._map_loot_view

    def _get_history_node_layer(self):
        """Return the cached historical-node layer, including test instances."""
        layer = getattr(self, "_history_node_layer", None)
        if layer is None:
            layer = self._history_node_layer = HistoricalNodeLayer(
                W, HUD_TOP, MAP_H, wifi_color=C_SUCCESS,
                bt_color=C_HACK_CYAN, cell_color=11)
        return layer

    def _update_clusters(self):
        """Advance the bounded cluster build used by legacy callers/tests."""
        layer = self._get_history_node_layer()
        layer.request(self._get_map_loot_points(), self.proj,
                      getattr(self, "_map_loot_view_revision", 0))
        layer.step()
        self._clusters = layer.clusters

    def _draw_loot_points(self):
        layer = self._get_history_node_layer()
        layer.request(self._get_map_loot_points(), self.proj,
                      getattr(self, "_map_loot_view_revision", 0))
        layer.draw(pyxel, self.proj, self._cluster_sel)
        self._clusters = layer.clusters
        revision = getattr(self, "_cluster_layer_revision", -1)
        if layer.revision != revision:
            if self._cluster_sel >= len(self._clusters):
                self._cluster_sel = -1
            self._cluster_layer_revision = layer.revision

    # ------------------------------------------------------------------
    # Cluster popup (mouse click)
    # ------------------------------------------------------------------

    def _classify_auth(self, auth: str, ssid: str) -> tuple[str, int]:
        """Return (type_label, color) for an AP based on auth + potfile."""
        if ssid and ssid in self._cracked_ssids:
            return "Cracked", C_SUCCESS
        if not auth or auth == "[ESS]":
            return "Open", C_WARNING
        if auth == "[BLE]":
            return "BLE", C_HACK_CYAN
        if auth.upper() in ("GSM", "WCDMA", "LTE", "NR"):
            return auth.upper(), 11
        return "Secured", C_ERROR

    def _find_nearest_cluster(self, dx: int, dy: int) -> int:
        """Find nearest cluster in given direction from current selection.

        dx/dy: -1, 0, +1 for direction (LEFT/RIGHT, UP/DOWN).
        Returns cluster index or -1.
        """
        layer = self._get_history_node_layer()
        visible = layer.visible_clusters(self.proj)
        if not visible:
            return -1
        visible_by_index = {index: (cluster, x, y)
                            for index, cluster, x, y in visible}
        if self._cluster_sel not in visible_by_index:
            # No selection — pick cluster closest to screen center
            cx, cy = W // 2, (HUD_TOP + TERM_Y) // 2
            best, bd = -1, 999999
            for i, _cl, x, y in visible:
                d = (x - cx) ** 2 + (y - cy) ** 2
                if d < bd:
                    bd, best = d, i
            return best
        _cur, cur_x, cur_y = visible_by_index[self._cluster_sel]
        best, bd = -1, 999999
        for i, _cl, x, y in visible:
            if i == self._cluster_sel:
                continue
            # Direction filter — only consider clusters in the pressed direction
            ok = True
            if dx < 0 and x >= cur_x:
                ok = False
            if dx > 0 and x <= cur_x:
                ok = False
            if dy < 0 and y >= cur_y:
                ok = False
            if dy > 0 and y <= cur_y:
                ok = False
            if not ok:
                continue
            d = (x - cur_x) ** 2 + (y - cur_y) ** 2
            if d < bd:
                bd, best = d, i
        return best if best >= 0 else self._cluster_sel

    def _handle_cluster_nav(self):
        """Navigate clusters with C + arrow keys, ENTER opens popup.

        C key: toggle cluster navigation mode (select nearest / deselect).
        Arrows: jump between clusters when one is selected.
        ENTER: open popup for selected cluster.
        ESC: exit cluster selection.
        """
        if not self.wardrive.show_network_dots():
            self._cluster_sel = -1
            self._cluster_popup = None
            return
        if not self._clusters:
            self._cluster_sel = -1
            return
        # Clamp selection after clusters were recomputed
        if self._cluster_sel >= len(self._clusters):
            self._cluster_sel = -1

        # C key — enter or exit cluster navigation
        if pyxel.btnp(pyxel.KEY_C) and not self._attack_mode:
            if self._cluster_sel >= 0:
                self._cluster_sel = -1  # exit cluster mode
                self.msg("[MAP] Cluster nav OFF", C_DIM)
            else:
                self._cluster_sel = self._find_nearest_cluster(0, 0)  # select nearest to center
                if self._cluster_sel >= 0:
                    self.msg(f"[MAP] Cluster selected ({len(self._clusters)} total)", C_HACK_CYAN)
                else:
                    self.msg("[MAP] No clusters visible", C_WARNING)
            return

        # ESC — exit cluster selection (before other ESC handlers)
        if pyxel.btnp(pyxel.KEY_ESCAPE) and self._cluster_sel >= 0:
            self._cluster_sel = -1
            self._esc_consumed_frame = pyxel.frame_count
            return

        # Nothing to do if no cluster selected
        if self._cluster_sel < 0:
            return

        # ENTER — open popup
        if pyxel.btnp(pyxel.KEY_RETURN):
            if 0 <= self._cluster_sel < len(self._clusters):
                self._cluster_popup = self._clusters[self._cluster_sel]
                self._popup_scroll = 0
            return

        # Arrow keys — navigate between clusters
        if pyxel.btnp(pyxel.KEY_LEFT):
            self._cluster_sel = self._find_nearest_cluster(-1, 0)
        elif pyxel.btnp(pyxel.KEY_RIGHT):
            self._cluster_sel = self._find_nearest_cluster(1, 0)
        elif pyxel.btnp(pyxel.KEY_UP):
            self._cluster_sel = self._find_nearest_cluster(0, -1)
        elif pyxel.btnp(pyxel.KEY_DOWN):
            self._cluster_sel = self._find_nearest_cluster(0, 1)

    def _update_cluster_popup(self):
        """Handle popup scroll and close keys."""
        if not self._cluster_popup:
            return
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._cluster_popup = None
            self._esc_consumed_frame = pyxel.frame_count
            return
        pts = self._cluster_popup["points"]
        max_scroll = max(0, len(pts) - 8)
        if pyxel.btnp(pyxel.KEY_UP):
            self._popup_scroll = max(0, self._popup_scroll - 1)
        if pyxel.btnp(pyxel.KEY_DOWN):
            self._popup_scroll = min(max_scroll, self._popup_scroll + 1)

    def _draw_cluster_popup(self):
        if not self._cluster_popup:
            return
        pts = self._cluster_popup["points"]
        n = len(pts)

        # Count types
        n_open, n_sec, n_crk = 0, 0, 0
        for p in pts:
            lbl, _c = self._classify_auth(p.get("auth", ""), p.get("label", ""))
            if lbl == "Open":
                n_open += 1
            elif lbl == "Cracked":
                n_crk += 1
            else:
                n_sec += 1

        # Popup dimensions — 14px rows for 5x8 font
        ROW_H = 14
        pw, ph = 340, min(200, 50 + min(n, 8) * ROW_H + 16)
        position = self._get_history_node_layer().screen_position(
            self._cluster_popup, self.proj)
        anchor_x, anchor_y = position or (
            self._cluster_popup["x"], self._cluster_popup["y"])
        px = max(4, min(W - pw - 4, anchor_x - pw // 2))
        py = max(HUD_TOP + 4, min(TERM_Y - ph - 4,
                                   anchor_y - ph // 2))
        # Background + border
        pyxel.rect(px, py, pw, ph, 0)
        pyxel.rectb(px, py, pw, ph, C_HACK_CYAN)

        # Header
        pyxel.text(px + 4, py + 4, f"Point networks: {n}", C_TEXT)
        summary = f"Open:{n_open} | Secured:{n_sec} | Cracked:{n_crk}"
        pyxel.text(px + 4, py + 16, summary, C_DIM)

        # Close hint (ESC)
        pyxel.text(px + pw - 24, py + 4, "ESC", C_DIM)

        # Column headers
        hdr_y = py + 30
        pyxel.text(px + 14,  hdr_y, "SSID", C_DIM)
        pyxel.text(px + 190, hdr_y, "RSSI", C_DIM)
        pyxel.text(px + 230, hdr_y, "Type", C_DIM)
        pyxel.text(px + 295, hdr_y, "Ch",   C_DIM)

        # Rows
        visible = pts[self._popup_scroll:self._popup_scroll + 8]
        for i, p in enumerate(visible):
            ry = hdr_y + 14 + i * ROW_H
            ssid = p.get("label", "?")
            auth = p.get("auth", "")
            rssi = p.get("rssi", "")
            ch = p.get("channel", "")
            type_lbl, type_c = self._classify_auth(auth, ssid)

            # Lock icon for secured/cracked
            if type_lbl in ("Secured", "Cracked"):
                pyxel.text(px + 4, ry, "\x0f", type_c)  # filled block
            else:
                pyxel.text(px + 4, ry, "\x07", C_WARNING)  # dot for open

            pyxel.text(px + 14,  ry, ssid[:28], C_TEXT)
            pyxel.text(px + 190, ry, str(rssi)[:5], C_DIM)
            pyxel.text(px + 230, ry, type_lbl, type_c)
            pyxel.text(px + 295, ry, str(ch)[:3], C_DIM)

        # Scroll indicator
        if n > 8:
            bar_h = max(4, ph * 8 // n)
            bar_y = py + 30 + (ph - 46) * self._popup_scroll // max(1, n - 8)
            pyxel.rect(px + pw - 3, bar_y, 2, bar_h, C_DIM)

    def _remember_marker(self, marker):
        key = marker.key or f"{marker.type}:{time.monotonic_ns()}"
        marker.key = key
        existing = getattr(self, "_marker_by_key", {}).get(key)
        if existing is not None:
            existing.lat, existing.lon = marker.lat, marker.lon
            existing.label = marker.label
            existing.last_seen = marker.last_seen
            return existing
        if not hasattr(self, "_marker_by_key"):
            self._marker_by_key = {}
        self.markers.append(marker)
        self._marker_by_key[key] = marker
        if len(self.markers) > 256:
            oldest = min(self.markers, key=lambda value: value.last_seen)
            self.markers.remove(oldest)
            self._marker_by_key.pop(oldest.key, None)
        return marker

    def _draw_markers(self):
        """Draw enabled handshake and MeshCore markers at any zoom level."""
        zoom = self.proj.zoom
        now = time.monotonic()
        for m in self.markers:
            base_color = C_HACK_CYAN if m.type == "meshcore" else C_ERROR
            c = self.wardrive.layer_color(
                m.type, base_color, m, now,
                historical=m.last_seen <= 0)
            if c is None:
                continue
            sx, sy = self.proj.geo_to_screen(m.lat, m.lon)
            if not self.proj.screen_visible(sx, sy):
                continue
            if m.type == "meshcore":
                # Cyan diamond + pulsing ring.
                if pyxel.frame_count % 40 < 22:
                    pyxel.circb(sx, sy, 6, c)
                pyxel.rect(sx - 2, sy - 2, 5, 5, c)
                pyxel.pset(sx, sy, 0)
                pyxel.pset(sx, sy - 3, c)
                pyxel.pset(sx, sy + 3, c)
                pyxel.pset(sx - 3, sy, c)
                pyxel.pset(sx + 3, sy, c)
                if zoom >= 4:
                    pyxel.text(sx + 6, sy - 3, m.label, c)
            else:
                # Handshake — red skull style
                if pyxel.frame_count % 30 < 20:
                    pyxel.circb(sx, sy, 4, c)
                pyxel.rect(sx - 2, sy - 1, 5, 4, c)
                pyxel.rect(sx - 1, sy - 3, 3, 2, c)
                pyxel.pset(sx, sy, C_WARNING)
                if zoom >= 5:
                    pyxel.text(sx + 5, sy - 3, m.label, c)

    def _get_live_node_layer(self):
        layer = getattr(self, "_live_node_layer", None)
        if layer is None:
            layer = self._live_node_layer = LiveNodeLayer(W, HUD_TOP, MAP_H)
        return layer

    def _draw_wardrive_nodes(self):
        """Draw ordinary nodes only when enabled; special overlays stay live."""
        if not self.wardrive.show_network_dots():
            return
        self._draw_loot_points()
        self._draw_live_nodes()
        self._draw_wifi()
        self._draw_ble()

    def _get_radar_node_layer(self):
        layer = getattr(self, "_radar_node_layer", None)
        if layer is None:
            layer = self._radar_node_layer = RadarNodeLayer(20, colors={
                "wifi": C_SUCCESS, "bt": C_HACK_CYAN, "cell": 11,
                "handshake": C_ERROR, "meshcore": C_WARNING,
                "other": C_DIM, "wifi_live": C_WARNING,
                "hacked": C_SUCCESS,
            })
        return layer

    def _draw_live_nodes(self):
        layer = self._get_live_node_layer()
        notable, notable_revision = self.wardrive.live_notable_identities()
        wifi_view, ble_view = self._get_live_map_views()
        revision = self._map_registry().revision
        layer.request(wifi_view, ble_view,
                      (revision, notable_revision), notable, self.proj)
        layer.draw(pyxel, self.proj)

    def _draw_wifi(self):
        """Draw only short-lived discovery rings; mature nodes are cached."""
        if self.wardrive.layer_mode("wifi") == MODE_OFF:
            self._recent_wifi_fx = []
            return
        recent = getattr(self, "_recent_wifi_fx", [])
        keep = []
        for net in recent:
            age = pyxel.frame_count - net.spawn_frame
            if age >= 15:
                continue
            keep.append(net)
            if self.wardrive.is_notable("wifi", net.bssid):
                continue
            sx, sy = self.proj.geo_to_screen(net.lat, net.lon)
            if self.proj.screen_visible(sx, sy) and age % 2 == 0:
                pyxel.circb(sx, sy, 15 - age, C_WARNING)
        self._recent_wifi_fx = keep

    def _draw_ble(self):
        """Draw only short-lived discovery rings; mature nodes are cached."""
        if self.wardrive.layer_mode("ble") == MODE_OFF:
            self._recent_ble_fx = []
            return
        recent = getattr(self, "_recent_ble_fx", [])
        keep = []
        for device in recent:
            age = pyxel.frame_count - device.spawn_frame
            if age >= 15:
                continue
            keep.append(device)
            if self.wardrive.is_notable("ble", device.mac):
                continue
            sx, sy = self.proj.geo_to_screen(device.lat, device.lon)
            if self.proj.screen_visible(sx, sy) and age % 2 == 0:
                pyxel.circb(sx, sy, 15 - age, C_HACK_CYAN)
        self._recent_ble_fx = keep

    def _visible_live_objects(self, objects, index_name, center_lat=None,
                              center_lon=None, lat_span=None, lon_span=None):
        index = getattr(self, index_name, None)
        if index is None:
            index = GeoObjectIndex()
            setattr(self, index_name, index)
        index.ensure(objects)
        return index.query(
            self.proj.center_lat if center_lat is None else center_lat,
            self.proj.center_lon if center_lon is None else center_lon,
            self.proj.lat_span if lat_span is None else lat_span,
            self.proj.lon_span if lon_span is None else lon_span)

    def _draw_aircraft(self):
        """Draw ADS-B aircraft allowed by the map display policy."""
        try:
            aircraft_list = list(self._map_aircraft.values())
        except Exception:
            return
        zoom = self.proj.zoom
        now = time.monotonic()
        for record in aircraft_list:
            ac = record["object"]
            if not ac.has_position:
                continue
            sx, sy = self.proj.geo_to_screen(ac.lat, ac.lon)
            if not self.proj.screen_visible(sx, sy):
                continue
            # Altitude-based color: low=green, mid=yellow, high=cyan
            if ac.altitude < 5000:
                c = C_SUCCESS
            elif ac.altitude < 20000:
                c = C_WARNING
            else:
                c = C_HACK_CYAN
            c = self.wardrive.layer_color("adsb", c, record, now)
            if c is None:
                continue
            # Plane icon — visible plus-shape with blinking pulse ring
            pyxel.line(sx - 3, sy, sx + 3, sy, c)       # wings
            pyxel.pset(sx, sy - 2, c)                   # nose
            pyxel.pset(sx, sy - 1, c)
            pyxel.pset(sx, sy + 1, c)
            pyxel.pset(sx - 1, sy + 2, c)               # tail fins
            pyxel.pset(sx + 1, sy + 2, c)
            if pyxel.frame_count % 40 < 14:
                pyxel.circb(sx, sy, 5, c)
            # Label at zoom >= 4
            if zoom >= 4:
                label = ac.callsign or ac.icao
                alt_k = ac.altitude // 1000
                pyxel.text(sx + 6, sy - 4, f"{label} {alt_k}k", c)

    def _draw_sensors(self):
        """Draw 433 MHz sensors on the map."""
        try:
            sensor_list = list(self._map_sensors.values())
        except Exception:
            return
        now = time.monotonic()
        for record in sensor_list:
            s = record["object"]
            if s.lat == 0.0 and s.lon == 0.0:
                continue
            sx, sy = self.proj.geo_to_screen(s.lat, s.lon)
            if not self.proj.screen_visible(sx, sy):
                continue
            blink = math.sin(pyxel.frame_count * 0.08 + hash(s.sid) % 100)
            base = 12 if blink > 0 else 2  # blue blink
            c = self.wardrive.layer_color("sensor433", base, record, now)
            if c is None:
                continue
            pyxel.rect(sx - 1, sy - 1, 3, 3, c)
            pyxel.pset(sx, sy - 2, c)  # antenna dot
            if self.proj.zoom >= 5:
                pyxel.text(sx + 4, sy - 3, s.model[:12], c)

    def _draw_scan_fx(self):
        cx, cy = W // 2, HUD_TOP + MAP_H // 2
        if self.scan_pulse < 30:
            r = int(self.scan_pulse * 1.5)
            pyxel.circb(cx, cy, r, C_GRID if self.scan_pulse > 20 else C_HACK_CYAN)
        if self.ble_scanning or self.wifi_scanning:
            r = 25 + int(math.sin(pyxel.frame_count * 0.3) * 8)
            pyxel.circb(cx, cy, r, 12)

    def _draw_player(self):
        if self.menu_open:
            return
        cx, cy = W // 2, HUD_TOP + MAP_H // 2

        # Skull sprite (9x9 pixel art)
        # Subtle breathing bob
        b = int(math.sin(self._breath * 0.06) * 0.8)
        sy = cy - 4 + b

        # Shadow
        pyxel.elli(cx - 4, cy + 6, 9, 3, 1)

        # Skull top (cranium)
        pyxel.rect(cx - 3, sy - 3, 7, 3, 7)       # top row
        pyxel.rect(cx - 4, sy,     9, 4, 7)       # middle (widest)
        # Eye sockets
        pyxel.rect(cx - 3, sy + 1, 2, 2, 0)       # left eye
        pyxel.rect(cx + 2, sy + 1, 2, 2, 0)       # right eye
        # Eye glow (blinks)
        if pyxel.frame_count % 120 < 115:
            pyxel.pset(cx - 2, sy + 1, C_HACK_CYAN)  # left pupil
            pyxel.pset(cx + 2, sy + 1, C_HACK_CYAN)  # right pupil
        # Nose
        pyxel.pset(cx, sy + 3, 13)
        # Jaw
        pyxel.rect(cx - 3, sy + 4, 7, 2, 7)
        # Teeth (dark gaps)
        pyxel.pset(cx - 2, sy + 5, 0)
        pyxel.pset(cx,     sy + 5, 0)
        pyxel.pset(cx + 2, sy + 5, 0)
        # Crossbones under skull
        pyxel.line(cx - 4, sy + 7, cx + 4, sy + 9, 7)
        pyxel.line(cx + 4, sy + 7, cx - 4, sy + 9, 7)

        # Scan ring (rotating, pulsing)
        ring_r = 10 + int(math.sin(self._breath * 0.03) * 2)
        pyxel.circb(cx, cy, ring_r, C_HACK_CYAN)

    def _draw_particles(self):
        for p in self.particles:
            if p.life > 7:
                pyxel.pset(int(p.x), int(p.y), p.color)

    def _draw_hack_bar(self):
        if not self.hacking or not self.hack_target: return
        sx, sy = self.proj.geo_to_screen(self.hack_target.lat, self.hack_target.lon)
        hy = sy - 14
        fill = int(30 * self.hack_progress / 45)
        pyxel.rect(sx-15, hy, 30, 4, 0)
        pyxel.rect(sx-15, hy, fill, 4, C_SUCCESS)
        pyxel.rectb(sx-15, hy, 30, 4, C_HACK_CYAN)
        pyxel.text(sx-18, hy-8, f"HACKING {int(100*self.hack_progress/45)}%", C_HACK_CYAN)

    def _draw_glitch(self):
        if self.glitch_timer > 0:
            for _ in range(4):
                pyxel.rect(random.randint(0, W-1), random.randint(0, H-1),
                           random.randint(5, 50), 1,
                           random.choice([C_HACK_CYAN, C_TEXT, C_SUCCESS]))

    def _draw_scanlines(self):
        for sl in self.scan_lines:
            pyxel.rect(0, sl, W, 1, C_GRID)

    def _draw_terminal(self):
        pyxel.rect(0, TERM_Y, W, TERM_H, 0)
        pyxel.line(0, TERM_Y, W - 1, TERM_Y, C_MENU_BORDER)
        pyxel.text(3, TERM_Y + 1, "> OUTPUT", C_HACK_CYAN)

        tool_name = ""
        if self.wardrive.scan.active:
            if self.wardrive.scan.mode == "hs_sniff":
                p = self.wardrive.passive
                tool_name = f"HS SNIFF {self.wardrive.scan.state.upper()} EAPOL:{p.eapol} PMKID:{p.pmkids}"
            else:
                label = "ESP DUAL TEST" if self.wardrive.scan.diagnostic else "ALL WARDRIVE (HOST BLE)" if self.wardrive.scan.wifi_only else "ALL WARDRIVE"
                tool_name = label + " " + self.wardrive.scan.state.upper()
        elif self.capturing_hs: tool_name = "HANDSHAKE"
        elif self.wifi_scanning or self._wifi_scan_only: tool_name = "WiFi SCAN"
        elif self.ble_scanning or self._ble_scan_only: tool_name = "BT SCAN"
        elif self._bt_airtag:
            parts = []
            if self._airtag_count: parts.append(f"AT:{self._airtag_count}")
            if self._smarttag_count: parts.append(f"ST:{self._smarttag_count}")
            tool_name = "TAG SCAN " + " ".join(parts) if parts else "TAG SCAN"
        elif self._bt_tracking: tool_name = "BT TRACK"
        elif self.sniffing: tool_name = "SNIFFER"
        elif self._mitm.running: tool_name = f"MITM [{self._mitm.packets}pkt]"
        elif self._dragon_drain.running: tool_name = "DRAGON DRAIN"
        elif self.state.portal_running:
            tool_name = f"EVIL PORTAL [{self.state.submitted_forms}cap]"
        elif self.state.evil_twin_running:
            tool_name = f"EVIL TWIN [{len(self.state.evil_twin_captured_data)}cap]"
        if tool_name:
            pyxel.text(54, TERM_Y + 1, tool_name, C_WARNING)

        with self._term_lock:
            lines_snap = list(self.terminal_lines)
            colors_snap = list(self._terminal_colors)
        total = len(lines_snap)

        if self.term_scroll > 0:
            pyxel.text(W - 60, TERM_Y + 1, f"SCROLL +{self.term_scroll}", C_DIM)
        pyxel.text(W - 30, TERM_Y + 1, f"L:{total}", C_DIM)

        # Spleen 5x8 — set as global default via monkey-patch, so pyxel.text
        # calls below render with it even without an explicit font arg.
        if self._font is not None:
            line_h = 9        # 8px glyphs + 1px gap
            char_w = 5
            content_y = TERM_Y + 10
            max_chars = (W - 8) // char_w  # ~126 chars fit at 640px
        else:
            line_h = 5
            char_w = 4
            content_y = TERM_Y + 8
            max_chars = 150
        content_h = TERM_H - (content_y - TERM_Y) - 2
        max_visible = content_h // line_h

        if self.term_scroll == 0:
            start = max(0, total - max_visible)
            end = total
        else:
            end = max(0, total - self.term_scroll)
            start = max(0, end - max_visible)

        y = content_y
        have_colors = len(colors_snap) == total
        for i in range(start, end):
            line = lines_snap[i]
            c = colors_snap[i] if have_colors else C_TEXT
            pyxel.text(4, y, line[:max_chars], c)
            y += line_h
            if y >= TERM_Y + TERM_H - line_h:
                break

        if self.term_scroll == 0 and pyxel.frame_count % 30 < 20:
            pyxel.text(4, min(y, TERM_Y + TERM_H - line_h - 1), "_",
                       C_HACK_CYAN)
        if total > max_visible and self.term_scroll == 0:
            pyxel.text(W - 120, TERM_Y + TERM_H - line_h - 1,
                       "Fn+U/Fn+K scroll", C_DIM)

    def _draw_mc_toast(self):
        """Draw MeshCore message toast on top of any screen."""
        if not self._mc_bubbles:
            return
        msg, expire = self._mc_bubbles[-1]
        remaining = expire - pyxel.frame_count
        if remaining <= 0:
            return
        # Large toast bar — centered, double height (5px per char)
        tw = max(len(msg) * 5 + 60, 300)
        if tw > W - 20:
            tw = W - 20
        tx = (W - tw) // 2
        ty = 20
        th = 32
        # Background with thick border
        pyxel.rect(tx, ty, tw, th, 0)
        pyxel.rect(tx + 1, ty + 1, tw - 2, th - 2, 1)
        pyxel.rectb(tx, ty, tw, th, 2)
        pyxel.rectb(tx + 1, ty + 1, tw - 2, th - 2, 2)
        # Icon + label
        pyxel.text(tx + 6, ty + 4, "MESHCORE", 2)
        # Message text — larger area
        pyxel.text(tx + 6, ty + 16, msg[:70], C_SUCCESS)
        # Blinking radio icon
        if pyxel.frame_count % 30 < 20:
            pyxel.circ(tx + tw - 10, ty + th // 2, 3, 2)
            pyxel.circ(tx + tw - 10, ty + th // 2, 1, C_SUCCESS)

    def _get_hat_profile(self) -> tuple[str, int]:
        """Determine hacker hat color based on activity profile.

        Returns (hat_name, pyxel_color).
        Hat types:
          White  (7)  — recon only: scanning, wardriving, no attacks
          Blue   (12) — defensive: mostly scanning, few handshakes
          Grey   (13) — mixed: scanning + some attacks
          Red    (8)  — offensive: heavy attacks, handshakes, deauth
          Black  (0)  — aggressive: ET credentials, MITM, exploits
        """
        t = self._loot_totals
        scan = t.get("wifi", 0) + t.get("bt", 0)
        hs = t.get("hs", 0) + t.get("pcap", 0)
        creds = (t.get("passwords", 0) + t.get("et_captures", 0)
                 + self.state.submitted_forms
                 + len(self.state.evil_twin_captured_data))
        hacked = self._current_hacked_count()

        attack_score = hs * 3 + creds * 10 + hacked * 2
        total = scan + attack_score

        if total == 0:
            return "WHITE", 7

        attack_ratio = attack_score / max(total, 1)

        if creds >= 5 or attack_ratio > 0.6:
            return "BLACK", 0
        if hs >= 10 or attack_ratio > 0.35:
            return "RED", 8
        if hs >= 3 or attack_ratio > 0.15:
            return "GREY", 13
        if scan > 50:
            return "BLUE", 12
        return "WHITE", 7

    def _current_hacked_count(self) -> int:
        cached = getattr(self, "_pwned_count", None)
        if cached is None:
            cached = (sum(1 for d in self.ble_devices if d.hacked)
                      + sum(1 for n in self.wifi_networks if n.hacked))
            self._pwned_count = cached
        return cached

    def _draw_hud_top(self):
        # Spleen 5x8: each char = 5px wide, 8px tall. HUD is 16px tall so
        # text at y=4 sits with 4px margins. All horizontal jumps are
        # computed from actual string widths so nothing overlaps.
        CW, TY = 5, 4
        pyxel.rect(0, 0, W, HUD_TOP, C_HUD_BG)
        pyxel.line(0, HUD_TOP - 1, W - 1, HUD_TOP - 1, C_HUD_LINE)
        x = 4
        # Title
        title = "WATCH DOGS"
        pyxel.text(x, TY, title, C_HACK_CYAN)
        x += len(title) * CW + 6
        # Level
        lv_txt = f"LV:{self.level}"
        pyxel.text(x, TY, lv_txt, C_SUCCESS)
        x += len(lv_txt) * CW + 4
        lvl = self.level_title
        pyxel.text(x, TY, lvl, C_SUCCESS)
        x += len(lvl) * CW + 4
        # XP bar
        xw = 40
        nxt = self.xp_for_next_level
        cur = self.xp_in_current_level
        xf = int(xw * cur / nxt) if nxt > 0 else xw
        pyxel.rect(x, TY + 1, xw, 7, C_GRID)
        pyxel.rect(x, TY + 1, xf, 7, C_HACK_CYAN)
        pyxel.rectb(x, TY + 1, xw, 7, C_COAST)
        x += xw + 3
        xp_txt = f"{self.xp}"
        pyxel.text(x, TY, xp_txt, C_DIM)
        x += len(xp_txt) * CW + 6
        # Hat badge
        hat_name, hat_color = self._get_hat_profile()
        pyxel.circ(x + 3, HUD_TOP // 2, 3, 7)
        pyxel.circ(x + 3, HUD_TOP // 2, 2, hat_color)
        x += 9
        pyxel.text(x, TY, hat_name, C_DIM)
        x += len(hat_name) * CW + 4
        # Achievement badges
        _BADGE_HUD = [
            ("flipper",          "FLP", C_WARNING),
            ("wardriver",        "WDR", C_SUCCESS),
            ("meshcore",         "MSH", 2),
            ("handshake_hunter", "HS",  C_ERROR),
            ("wpasec_uploader",  "WPA", 12),
            ("evil_twin",        "ET",  8),
            ("skywatch",         "ADB", C_HACK_CYAN),
            ("iot_hunter",       "433", 12),
        ]
        for badge_id, label, color in _BADGE_HUD:
            if badge_id in self._badges:
                lw = len(label) * CW + 4
                if x + lw + 2 > W - 70:
                    break  # don't overflow past zoom / battery
                pyxel.rectb(x, 2, lw, HUD_TOP - 4, color)
                pyxel.text(x + 2, TY, label, color)
                x += lw + 2
        # Battery + Zoom — far right
        bat = self._battery_pct
        if bat >= 0:
            bc = C_SUCCESS if bat > 25 else (C_WARNING if bat > 10 else C_ERROR)
            bat_txt = f"BAT:{bat}%"
            bx = W - len(bat_txt) * CW - 4
            pyxel.text(bx, TY, bat_txt, bc)
            zoom_txt = f"Z:{self.proj.label}"
            pyxel.text(bx - len(zoom_txt) * CW - 6, TY, zoom_txt, C_DIM)
        else:
            zoom_txt = f"Z:{self.proj.label}"
            pyxel.text(W - len(zoom_txt) * CW - 4, TY, zoom_txt, C_DIM)

    def _draw_hud_bottom(self):
        pyxel.rect(0, H - HUD_BOT, W, HUD_BOT, C_HUD_BG)
        pyxel.line(0, H - HUD_BOT, W - 1, H - HUD_BOT, C_HUD_LINE)
        y = H - HUD_BOT + 5

        # All-time totals from loot_db + current session additions
        t_bt = self._loot_totals.get("bt", 0) + getattr(
            self, "_session_ble_discovered", len(self.ble_devices))
        t_wifi = self._loot_totals.get("wifi", 0) + getattr(
            self, "_session_wifi_discovered", len(self.wifi_networks))
        n_hs_ses = sum(1 for m in self.markers if m.type == "handshake")
        t_hs   = self._loot_totals.get("hs",   0) + n_hs_ses
        t_cell = self._loot_totals.get("cell", 0) + len(self.wardrive.cell.unique)
        n_pwn  = self._current_hacked_count()
        t_pwd  = (self._loot_totals.get("passwords", 0)
                  + self._loot_totals.get("et_captures", 0)
                  + self.state.submitted_forms
                  + len(self.state.evil_twin_captured_data))
        # Columns sized for Spleen 5x8 + 4-digit counts (e.g. "BLE:9999"=40px).
        pyxel.text(4,   y, f"BLE:{t_bt}",    C_HACK_CYAN)
        pyxel.text(60,  y, f"WiFi:{t_wifi}", C_WARNING)
        pyxel.text(125, y, f"HS:{t_hs}",    C_ERROR)
        pyxel.text(170, y, f"PWD:{t_pwd}",  12)  # blue
        pyxel.text(220, y, f"PWN:{n_pwn}",  C_SUCCESS)
        cell_x = 275
        cell_txt = f"CELL:{t_cell}"
        pyxel.text(cell_x, y, cell_txt, 11)

        # Reserve the exact region used by GPS and, when visible, LoRa before
        # placing the center status.  Counts can grow beyond four digits, so
        # derive the left edge from the rendered CELL text instead of using a
        # second fixed x coordinate that can overlap it.
        if self.gps_fix:
            gps_left = W - 108
        elif self.gps.available:
            gps_left = W - (120 if self.gps_sats_vis > 0 else 100)
        else:
            gps_left = W - 60
        right_status_edge = gps_left - 6
        if self._lora.running or self._lora_enabled:
            right_status_edge = min(right_status_edge, W - 206)
        left_status_edge = cell_x + len(cell_txt) * HUD_FONT_W + 8

        tools = []
        if self.wardrive.scan.active:
            label = "HS Sniff" if self.wardrive.scan.mode == "hs_sniff" else "WiFi+BLE"
            tools.append(label + " " + self.wardrive.scan.state)
        if self.wifi_scanning: tools.append("WiFi")
        if self.ble_scanning: tools.append("BT")
        if self.sniffing: tools.append("SNF")
        if self.capturing_hs: tools.append("HS")
        if self.state.portal_running: tools.append("EP")
        if self.state.evil_twin_running: tools.append("ET")
        if self._sdr.running and "adsb" in self._sdr.mode:
            n_pos = sum(1 for a in self._sdr.aircraft.values() if a.has_position)
            n_all = len(self._sdr.aircraft)
            tools.append(f"ADS-B:{n_pos}/{n_all}")
        if self._sdr.running and "433" in self._sdr.mode:
            tools.append(f"433:{self._sdr.total_sensors_seen}")
        if tools:
            dots = "." * ((pyxel.frame_count // 10) % 4)
            status_x, status_txt = _fit_hud_text(
                " ".join(tools) + dots, left_status_edge, right_status_edge)
            if status_txt:
                pyxel.text(status_x, y, status_txt, 12)
        elif not self.menu_open:
            status_x, status_txt = _bottom_menu_hint(
                left_status_edge, right_status_edge)
            if status_txt:
                pyxel.text(status_x, y, status_txt, C_COAST)

        # GPS status — "DD.DDDDDS DDD.DDDDDW" = 20 chars × 5 = 100px
        if self.gps_fix:
            lat_c = "N" if self.player_lat >= 0 else "S"
            lon_c = "E" if self.player_lon >= 0 else "W"
            gps_txt = (f"{abs(self.player_lat):.5f}{lat_c} "
                       f"{abs(self.player_lon):.5f}{lon_c}")
            pyxel.text(W - 108, y, gps_txt, C_SUCCESS)
        elif self.gps.available:
            if self.gps_sats_vis > 0:
                pyxel.text(W - 120, y,
                           f"Waiting fix Vis:{self.gps_sats_vis}", C_WARNING)
            else:
                pyxel.text(W - 100, y, "Waiting for GPS fix", C_ERROR)
        else:
            pyxel.text(W - 60, y, "GPS offline", C_DIM)

        # LoRa status (left of GPS, same line). "LoRa:ON pkt:9999" = 16×5 = 80
        if self._lora.running:
            pkts = self._lora.packets_received
            pyxel.text(W - 200, y, f"LoRa:ON pkt:{pkts}", C_SUCCESS)
        elif self._lora_enabled:
            pyxel.text(W - 200, y, "LoRa:IDLE", C_WARNING)


    def _draw_messages(self):
        y = TERM_Y - 12
        for text, timer, col in reversed(self.msgs):
            c = col if min(timer, 30) / 30 > 0.5 else C_COAST
            pyxel.text(4, y, text[:80], c)
            y -= 10
            if y < HUD_TOP + 10: break

    def _draw_radar(self):
        rx, ry, rr = W - 30, HUD_TOP + 24, 20
        pyxel.rect(rx-rr-1, ry-rr-1, rr*2+3, rr*2+3, 0)
        pyxel.circb(rx, ry, rr, C_GRID)
        pyxel.line(rx, ry-rr, rx, ry+rr, C_GRID)
        pyxel.line(rx-rr, ry, rx+rr, ry, C_GRID)
        sa = pyxel.frame_count * 0.04
        pyxel.line(rx, ry, rx+int(math.cos(sa)*rr), ry+int(math.sin(sa)*rr), C_HACK_CYAN)
        scale = rr / max(self.proj.lon_span * 0.5, 0.001)
        notable, notable_revision = self.wardrive.live_notable_identities()
        radar_layer = self._get_radar_node_layer()
        if self.wardrive.show_network_dots():
            wifi_view, ble_view = self._get_live_map_views()
            radar_layer.request(
                wifi_view, ble_view,
                self._get_map_loot_points(),
                (self._map_registry().revision, notable_revision),
                notable, self.player_lat, self.player_lon, scale)
            radar_layer.draw(
                pyxel, rx, ry, self.player_lat, self.player_lon, scale)
        now = time.monotonic()
        for m in self.markers:
            base = C_HACK_CYAN if m.type == "meshcore" else C_ERROR
            color = self.wardrive.layer_color(
                m.type, base, m, now,
                historical=m.last_seen <= 0)
            if color is None:
                continue
            dx = (m.lon - self.player_lon) * scale
            dy = (self.player_lat - m.lat) * scale
            if abs(dx) < rr and abs(dy) < rr:
                pyxel.pset(rx+int(dx), ry+int(dy), color)
        # ADS-B aircraft on radar
        try:
            for record in list(self._map_aircraft.values()):
                ac = record["object"]
                if not ac.has_position:
                    continue
                color = self.wardrive.layer_color(
                    "adsb", C_HACK_CYAN, record, now)
                if color is None:
                    continue
                dx = (ac.lon - self.player_lon) * scale
                dy = (self.player_lat - ac.lat) * scale
                if abs(dx) < rr and abs(dy) < rr:
                    pyxel.pset(rx+int(dx), ry+int(dy), color)
            for record in list(self._map_sensors.values()):
                sensor = record["object"]
                color = self.wardrive.layer_color(
                    "sensor433", 12, record, now)
                if color is None or (sensor.lat == 0 and sensor.lon == 0):
                    continue
                dx = (sensor.lon - self.player_lon) * scale
                dy = (self.player_lat - sensor.lat) * scale
                if abs(dx) < rr and abs(dy) < rr:
                    pyxel.pset(rx+int(dx), ry+int(dy), color)
        except Exception:
            pass

        self.wardrive.draw_radar(rx, ry, rr, scale)
        pyxel.pset(rx, ry, C_TEXT)

        # MeshCore: CB radio sprite + message bubbles (under radar)
        if self._lora.running:
            # Radio sprite centered under radar
            rw = getattr(self, '_radio_w', 32)
            rh = getattr(self, '_radio_h', 48)
            ix = rx - rw // 2   # centered on radar X
            iy = ry + rr + 4    # just below radar
            if self._radio_sprite_ok:
                pyxel.blt(ix, iy, 2, 0, 0, rw, rh, 0)  # 0=transparent
            else:
                # Fallback pixel radio
                pyxel.rect(ix + 8, iy + 4, 16, 20, C_HACK_CYAN)
                pyxel.rect(ix + 14, iy, 2, 6, C_HACK_CYAN)

            # Signal waves when bubbles active
            if self._mc_bubbles:
                if pyxel.frame_count % 30 < 20:
                    pyxel.circb(ix - 2, iy + rh // 2, 4, C_WARNING)
                if pyxel.frame_count % 30 < 12:
                    pyxel.circb(ix - 5, iy + rh // 2, 8, C_WARNING)

            # Message bubbles (left of radio) — 5x8 font + 2px padding
            if self._mc_bubbles:
                by = iy + 4
                bx_right = ix - 4
                for txt, _exp in self._mc_bubbles[-3:]:
                    disp = txt[:28]
                    tw = len(disp) * 5 + 8
                    bx = bx_right - tw
                    pyxel.rect(bx, by, tw, 12, 1)
                    pyxel.rectb(bx, by, tw, 12, C_WARNING)
                    pyxel.text(bx + 3, by + 2, disp, C_SUCCESS)
                    by += 12

        # SDR indicator (under LoRa radio, or under radar if no LoRa)
        if self._sdr.running:
            if self._lora.running:
                rw = getattr(self, '_radio_w', 32)
                rh = getattr(self, '_radio_h', 48)
                sy = ry + rr + 4 + rh + 4  # below radio sprite
            else:
                sy = ry + rr + 6            # below radar
            sx = rx - 10
            # Pixel-art satellite dish
            pyxel.line(sx + 4, sy + 8, sx + 10, sy + 2, C_HACK_CYAN)
            pyxel.circ(sx + 11, sy + 1, 2, C_HACK_CYAN)
            pyxel.rect(sx + 3, sy + 9, 3, 3, C_HACK_CYAN)
            # Scanning waves
            t = pyxel.frame_count % 40
            if t < 15:
                pyxel.circb(sx + 11, sy + 1, 4 + t // 5, C_SUCCESS)
            # Counter label
            parts = []
            if "adsb" in self._sdr.mode:
                n_pos = sum(1 for a in self._sdr.aircraft.values() if a.has_position)
                parts.append(f"AC:{n_pos}/{len(self._sdr.aircraft)}")
            if "433" in self._sdr.mode:
                parts.append(f"S:{self._sdr.total_sensors_seen}")
            if parts:
                pyxel.text(sx - 2, sy + 14, " ".join(parts), C_HACK_CYAN)

    def _draw_menu(self):
        # Dim background — black scanlines
        for y in range(HUD_TOP, TERM_Y):
            if y % 2 == 0:
                pyxel.line(0, y, W - 1, y, 0)

        # ── Left panel: category tabs + item list ──────────────────────
        PX = 6       # left panel left edge
        PW = 310     # left panel width
        TAB_Y = HUD_TOP + 3

        # Category tabs — 12px tall to fit 8px text + 2px padding each side
        TAB_H = 12
        cat_w = PW // len(MENU_CATS)
        for i, (cat_name, _items) in enumerate(MENU_CATS):
            tx = PX + i * cat_w
            sel = (i == self.menu_cat)
            if sel:
                pyxel.rect(tx, TAB_Y - 1, cat_w - 1, TAB_H, C_HACK_CYAN)
                pyxel.text(tx + 3, TAB_Y + 2, cat_name[:6], 0)
            else:
                pyxel.rectb(tx, TAB_Y - 1, cat_w - 1, TAB_H, C_COAST)
                pyxel.text(tx + 3, TAB_Y + 2, cat_name[:6], C_DIM)

        # Item list — 13px tall rows
        _, items = MENU_CATS[self.menu_cat]
        IY = TAB_Y + TAB_H + 3
        IH = 13
        max_vis = (TERM_Y - IY - 24) // IH
        for i, (hotkey, label, cmd, state_key, input_type) in enumerate(items):
            if i >= max_vis:
                break
            ty = IY + i * IH
            sel = (i == self.menu_sel)
            running = self._is_running(state_key)
            is_stop = (state_key == "_stop_all")
            unavailable = bool(
                self._menu_item_unavailable_reason(cmd, state_key))
            if sel:
                bg = (C_ERROR if is_stop else
                      (C_COAST if unavailable else C_HACK_CYAN))
                pyxel.rect(PX, ty - 1, PW, IH, bg)
                pyxel.rectb(PX, ty - 1, PW, IH, C_TEXT)
                tc = 0
            else:
                bc = (C_ERROR if is_stop else
                      (C_COAST if unavailable else C_MENU_BORDER))
                pyxel.rectb(PX, ty - 1, PW, IH, bc)
                tc = (C_ERROR if is_stop else
                      (C_DIM if unavailable else C_TEXT))
            pyxel.text(PX + 4, ty + 2, f"[{hotkey}] {label}", tc)
            needs_warning = state_key in _NEEDS_EXT_WIFI or cmd in _NEEDS_ESP_SD
            if needs_warning:
                # Red warning triangle with black "!"
                wx = PX + PW - 32
                wy = ty + 1
                pyxel.tri(wx, wy + 8, wx + 4, wy, wx + 8, wy + 8, C_ERROR)
                pyxel.text(wx + 3, wy + 2, "!", 0)
            if running:
                pyxel.text(PX + PW - 18, ty + 2, "ON",
                           C_SUCCESS if not sel else 0)
            if input_type:
                pyxel.text(PX + PW - 8, ty + 2, ">", C_DIM if not sel else 0)

        # Show ext WiFi warning if selected item needs it
        _, sel_items = MENU_CATS[self.menu_cat]
        if self.menu_sel < len(sel_items):
            _sk = sel_items[self.menu_sel][3]
            _cmd = sel_items[self.menu_sel][2]
            unavailable = self._menu_item_unavailable_reason(_cmd, _sk)
            if unavailable:
                pyxel.text(PX + 3, TERM_Y - 22,
                           unavailable, C_WARNING)
            elif _sk in _NEEDS_EXT_WIFI:
                pyxel.text(PX + 3, TERM_Y - 22,
                           "\x17 Requires external WiFi adapter", C_WARNING)
            elif _cmd in _NEEDS_ESP_SD:
                pyxel.text(PX + 3, TERM_Y - 22,
                           "\x17 Requires SD card on ESP32", C_WARNING)
            elif _sk == "_hs_sniff_menu":
                pyxel.text(PX + 3, TERM_Y - 22,
                           "Passive HS/PMKID -> uConsole (no SD)", C_HACK_CYAN)

        # Navigation hint
        pyxel.text(PX + 1, TERM_Y - 10,
                   "\x1c/\x1d cat  \x1e/\x1f sel  ENTER  TAB", C_DIM)

        # ─ ESP32 status + category (top-left of right panel) ─
        sx2 = PX + 310 + 8
        esp_c = C_SUCCESS if self._esp32 else C_ERROR
        pyxel.text(sx2, HUD_TOP + 4,  "ESP32:", C_DIM)
        pyxel.text(sx2, HUD_TOP + 16, "OK" if self._esp32 else "OFFLINE", esp_c)
        cat_name = MENU_CATS[self.menu_cat][0]
        pyxel.text(sx2, HUD_TOP + 30, f"// {cat_name}", C_HACK_CYAN)

    def _draw_menu_hacker(self):
        """Draw hacker sprite + speech bubble ON TOP of menu dim overlay.

        Black rect under sprite covers the dim-overlay scanlines completely.
        Then blt with colkey=0 makes the sprite's black background transparent,
        revealing the black rect beneath — so the sprite keeps its dark look
        with no blue scanlines bleeding through.

        Sprite changes per active menu tab (SCAN, SNIFF, etc.)
        """
        # Swap sprite if tab changed
        target_tab = self.menu_cat
        if target_tab != self._current_menu_sprite:
            if target_tab in self._menu_sprites:
                try:
                    pyxel.image(1).load(0, 0, self._menu_sprites[target_tab])
                    self._current_menu_sprite = target_tab
                except Exception:
                    pass
            elif self._current_menu_sprite != 4 and 4 in self._menu_sprites:
                # Fallback to default (SYSTEM/hacker) for tabs without sprite
                try:
                    pyxel.image(1).load(0, 0, self._menu_sprites[4])
                    self._current_menu_sprite = 4
                except Exception:
                    pass

        rpx = 490
        feet_y = TERM_Y + 60  # legs extend below terminal line

        if self._hacker_sprite_ok and self._hacker_w > 0:
            sx = rpx - self._hacker_w // 2
            sy = feet_y - self._hacker_h
            # BLACK rect covers dim overlay scanlines in sprite area
            bg_top = max(sy, HUD_TOP)
            pyxel.rect(sx, bg_top, self._hacker_w, feet_y - bg_top, 0)
            # Sprite on top — colkey=0 transparent pixels show black beneath
            pyxel.blt(sx, sy, 1, 0, 0, self._hacker_w, self._hacker_h, 0)
            bubble_cx = sx + self._hacker_w // 2
            bubble_y = sy + self._hacker_h // 3
        else:
            pyxel.rect(rpx - 10, feet_y - 80, 20, 80, 1)
            pyxel.circ(rpx, feet_y - 88, 10, 1)
            bubble_cx = rpx
            bubble_y = feet_y - 100

        # ─ Comic speech bubble (bigger, at torso level) ──────────────
        quip_list = HACKER_QUIPS[self.menu_cat % len(HACKER_QUIPS)]
        quip = quip_list[(pyxel.frame_count // 90) % len(quip_list)]
        quip_upper = quip.upper()
        qw = len(quip_upper) * 5 + 16
        qh = 20
        qx = bubble_cx - (self._hacker_w if self._hacker_w else 60) - qw // 2
        qy = bubble_y
        if qx < 2:
            qx = 2
        if qy < HUD_TOP + 2:
            qy = HUD_TOP + 2
        if qx + qw > W - 4:
            qx = W - qw - 4
        # Bubble body
        pyxel.rect(qx + 2, qy, qw - 4, qh, 0)
        pyxel.rect(qx, qy + 2, qw, qh - 4, 0)
        pyxel.rect(qx + 1, qy + 1, qw - 2, qh - 2, 0)
        # Border
        pyxel.line(qx + 3, qy, qx + qw - 4, qy, C_HACK_CYAN)
        pyxel.line(qx + 3, qy + qh - 1, qx + qw - 4, qy + qh - 1, C_HACK_CYAN)
        pyxel.line(qx, qy + 3, qx, qy + qh - 4, C_HACK_CYAN)
        pyxel.line(qx + qw - 1, qy + 3, qx + qw - 1, qy + qh - 4, C_HACK_CYAN)
        # Corners
        for cx, cy in [(qx+1,qy+1),(qx+2,qy+1),(qx+1,qy+2),
                        (qx+qw-2,qy+1),(qx+qw-3,qy+1),(qx+qw-2,qy+2),
                        (qx+1,qy+qh-2),(qx+2,qy+qh-2),(qx+1,qy+qh-3),
                        (qx+qw-2,qy+qh-2),(qx+qw-3,qy+qh-2),(qx+qw-2,qy+qh-3)]:
            pyxel.pset(cx, cy, C_HACK_CYAN)
        # Tail → right
        tail_x = qx + qw
        tail_y = qy + qh // 2
        pyxel.line(tail_x, tail_y - 2, tail_x + 6, tail_y, C_HACK_CYAN)
        pyxel.line(tail_x, tail_y + 2, tail_x + 6, tail_y, C_HACK_CYAN)
        # Text — Spleen 5x8 centered in bubble
        text_x = qx + (qw - len(quip_upper) * 5) // 2
        text_y = qy + (qh - 8) // 2
        pyxel.text(text_x, text_y, quip_upper, C_HACK_CYAN)

    # ------------------------------------------------------------------
    # Picker overlays (network / portal selection)
    # ------------------------------------------------------------------

    def _draw_net_picker(self):
        """Draw Evil Twin network selection overlay."""
        nets = self._attack_scan_results
        # Build client count lookup from sniffer data (if available)
        cli_map: dict[str, int] = {}
        for sap in self.state.sniffer_aps:
            if sap.ssid:
                cli_map[sap.ssid] = sap.client_count
        # Solid dim background
        pyxel.rect(0, HUD_TOP, W, TERM_Y - HUD_TOP, 0)
        # Dialog box — 14px rows for 5x8 font
        ROW_H = 14
        dw, dh = 540, min(260, 60 + len(nets) * ROW_H)
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_HACK_CYAN)
        pyxel.rectb(dx + 1, dy + 1, dw - 2, dh - 2, C_COAST)
        # Title
        pyxel.text(dx + 4, dy + 4, "EVIL TWIN — Select Target Network", C_HACK_CYAN)
        pyxel.line(dx + 2, dy + 14, dx + dw - 3, dy + 14, C_COAST)
        # Column headers — spaced for 5x8 font (widened from 4x6)
        hy = dy + 18
        pyxel.text(dx + 26,  hy, "SSID",  C_DIM)
        pyxel.text(dx + 220, hy, "BSSID", C_DIM)
        pyxel.text(dx + 360, hy, "CH",    C_DIM)
        pyxel.text(dx + 390, hy, "RSSI",  C_DIM)
        pyxel.text(dx + 430, hy, "Auth",  C_DIM)
        pyxel.text(dx + 495, hy, "Cli",   C_DIM)
        pyxel.line(dx + 2, hy + 10, dx + dw - 3, hy + 10, 1)
        # Network list with scroll
        max_vis = (dh - 70) // ROW_H
        start = max(0, self._et_net_sel - max_vis + 1)
        y = hy + 14
        picked = self._et_net_selected
        for i in range(start, min(len(nets), start + max_vis)):
            net = nets[i]
            cursor = (i == self._et_net_sel)
            checked = (i in picked)
            if cursor:
                pyxel.rect(dx + 2, y - 1, dw - 4, ROW_H - 1, C_HACK_CYAN)
            # Checkbox: [X] or [ ]
            mark = "X" if checked else " "
            pyxel.text(dx + 4, y + 2, f"[{mark}]",
                       0 if cursor else (C_WARNING if checked else C_DIM))
            ssid = (net.ssid or "<hidden>")[:24]
            bssid = net.bssid if hasattr(net, 'bssid') else "?"
            ch = str(net.channel) if hasattr(net, 'channel') else "?"
            rssi = str(net.rssi) if hasattr(net, 'rssi') else "?"
            auth = (net.auth[:8] if hasattr(net, 'auth') else "?")
            n_cli = cli_map.get(net.ssid, -1)
            cli_str = str(n_cli) if n_cli >= 0 else "-"
            c = 0 if cursor else C_SUCCESS
            pyxel.text(dx + 26,  y + 2, ssid,  c)
            pyxel.text(dx + 220, y + 2, bssid, 0 if cursor else C_TEXT)
            pyxel.text(dx + 360, y + 2, ch,    0 if cursor else C_DIM)
            pyxel.text(dx + 390, y + 2, rssi,  0 if cursor else C_DIM)
            pyxel.text(dx + 430, y + 2, auth,  0 if cursor else C_DIM)
            cli_c = (0 if cursor else C_WARNING) if n_cli > 0 else (
                    0 if cursor else C_DIM)
            pyxel.text(dx + 495, y + 2, cli_str, cli_c)
            y += ROW_H
        # Scroll indicator
        if len(nets) > max_vis:
            bar_total = dh - 70
            bar_h = max(4, bar_total * max_vis // len(nets))
            bar_y = dy + 30 + (bar_total - bar_h) * start // max(
                1, len(nets) - max_vis)
            pyxel.rect(dx + dw - 4, bar_y, 2, bar_h, C_HACK_CYAN)
        # Selection info
        n_sel = len(picked)
        if n_sel > 0:
            pyxel.text(dx + dw - 130, dy + 4,
                       f"1st=clone {n_sel-1}=deauth" if n_sel > 1 else "1 target",
                       C_WARNING)
        # Hints
        has_cli = bool(cli_map)
        hint = "SPACE mark  ENTER confirm  ESC cancel"
        if not has_cli:
            hint += "  (run Sniffer first for client counts)"
        pyxel.text(dx + 4, dy + dh - 11, hint, C_DIM)
        # Count
        pyxel.text(dx + dw - 90, dy + 4, f"{len(nets)} networks", C_DIM)

    def _draw_portal_picker(self):
        """Draw portal selection overlay."""
        portals = self._portal_list
        tag = "EVIL PORTAL" if self._attack_mode == "evil_portal" else "EVIL TWIN"
        # Solid dim background
        pyxel.rect(0, HUD_TOP, W, TERM_Y - HUD_TOP, 0)
        # Dialog box — 14px rows for 5x8 font
        ROW_H = 14
        dw, dh = 420, min(220, 60 + len(portals) * ROW_H)
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_WARNING)
        pyxel.rectb(dx + 1, dy + 1, dw - 2, dh - 2, C_COAST)
        # Title
        pyxel.text(dx + 4, dy + 4, f"{tag} — Select Portal", C_WARNING)
        pyxel.line(dx + 2, dy + 14, dx + dw - 3, dy + 14, C_COAST)
        # SSID info
        pyxel.text(dx + 4, dy + 18, f"SSID: {self._portal_ssid}", C_HACK_CYAN)
        pyxel.line(dx + 2, dy + 28, dx + dw - 3, dy + 28, 1)
        # Portal list
        max_vis = (dh - 60) // ROW_H
        start = max(0, self._portal_sel - max_vis + 1)
        y = dy + 32
        for i in range(start, min(len(portals), start + max_vis)):
            name, html = portals[i]
            sel = (i == self._portal_sel)
            if sel:
                pyxel.rect(dx + 2, y - 1, dw - 4, ROW_H - 1, C_WARNING)
            # Type indicator
            if html is None:
                src = "[FW]"
                sc = C_DIM
            elif name.startswith("[C]"):
                src = "[USR]"
                sc = C_HACK_CYAN
            else:
                src = "[B-IN]"
                sc = C_SUCCESS
            c = 0 if sel else C_TEXT
            pyxel.text(dx + 6, y + 3, src, 0 if sel else sc)
            pyxel.text(dx + 44, y + 3, name[:28], c)
            # Upload time estimate
            if html is None:
                t_str = "instant"
            else:
                import math
                b64_len = math.ceil(len(html.encode("utf-8")) * 4 / 3)
                secs = b64_len / 128 * 0.2
                t_str = f"~{int(secs)}s" if secs >= 1 else "<1s"
            pyxel.text(dx + dw - 46, y + 3, t_str, 0 if sel else C_DIM)
            y += ROW_H
        # Hints
        hint = "UP/DOWN select  ENTER confirm  ESC "
        hint += "back" if self._attack_mode == "evil_twin" else "cancel"
        pyxel.text(dx + 4, dy + dh - 11, hint, C_DIM)

    # ------------------------------------------------------------------
    # MeshCore region picker (ADDONS > MeshCore Region)
    # ------------------------------------------------------------------

    def _update_mc_region_picker(self):
        """UP/DOWN/ENTER/ESC for the MeshCore regional preset picker."""
        import pyxel as px
        from .lora_manager import MESHCORE_PRESETS, save_meshcore_config
        keys = list(MESHCORE_PRESETS.keys())
        if not keys:
            self._mc_region_screen = False
            return
        if px.btnp(px.KEY_UP):
            self._mc_region_sel = max(0, self._mc_region_sel - 1)
        elif px.btnp(px.KEY_DOWN):
            self._mc_region_sel = min(len(keys) - 1, self._mc_region_sel + 1)
        elif px.btnp(px.KEY_RETURN):
            new_region = keys[self._mc_region_sel]
            old_region = self._mc_region
            self._mc_region = new_region
            # Persist to ~/.watchdogs_meshcore.json
            try:
                save_meshcore_config(self._mc_node_name,
                                     self._mc_channels_list,
                                     region=new_region)
            except Exception as exc:
                self._term_add(f"[MC] config save failed: {exc}", raw=True)
            label = MESHCORE_PRESETS[new_region][4]
            self.msg(f"[MC] Region: {label}", C_SUCCESS)
            # Re-tune the radio right away if LoRa is currently sniffing
            if self._lora_enabled and self._lora.running and \
                    new_region != old_region:
                try:
                    self._lora.stop()
                except Exception:
                    pass
                try:
                    self._lora.start_meshcore(new_region)
                    self._term_add(f"[MC] Retuned to {label}", raw=True)
                except Exception as exc:
                    self._term_add(f"[MC] retune failed: {exc}", raw=True)
            self._mc_region_screen = False
        elif px.btnp(px.KEY_ESCAPE):
            self._mc_region_screen = False
            self._esc_consumed_frame = pyxel.frame_count

    def _draw_mc_region_picker(self):
        """Draw MeshCore regional preset picker overlay."""
        from .lora_manager import MESHCORE_PRESETS
        keys = list(MESHCORE_PRESETS.keys())
        # Solid dim background
        pyxel.rect(0, HUD_TOP, W, TERM_Y - HUD_TOP, 0)
        ROW_H = 14
        dw = 440
        dh = min(240, 70 + len(keys) * ROW_H)
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_WARNING)
        pyxel.rectb(dx + 1, dy + 1, dw - 2, dh - 2, C_COAST)
        pyxel.text(dx + 4, dy + 4, "MESHCORE — Select Region", C_WARNING)
        pyxel.line(dx + 2, dy + 14, dx + dw - 3, dy + 14, C_COAST)
        pyxel.text(dx + 4, dy + 18,
                   f"Current: {MESHCORE_PRESETS[self._mc_region][4]}",
                   C_HACK_CYAN)
        pyxel.line(dx + 2, dy + 28, dx + dw - 3, dy + 28, 1)
        y = dy + 32
        for i, key in enumerate(keys):
            freq, sf, cr, bw, label = MESHCORE_PRESETS[key]
            sel = (i == self._mc_region_sel)
            if sel:
                pyxel.rect(dx + 2, y - 1, dw - 4, ROW_H - 1, C_WARNING)
            c_label = 0 if sel else C_TEXT
            c_hz = 0 if sel else C_DIM
            pyxel.text(dx + 6, y + 3, label, c_label)
            freq_str = f"{freq / 1_000_000:.3f}MHz"
            pyxel.text(dx + dw - 64, y + 3, freq_str, c_hz)
            y += ROW_H
        pyxel.text(dx + 4, dy + dh - 11,
                   "UP/DOWN select   ENTER apply   ESC cancel", C_DIM)

    # ------------------------------------------------------------------
    # Flipper Zero screen
    # ------------------------------------------------------------------

    _FLIPPER_MENU = [
        # SubGHz Toolkit
        ("--- SUBGHZ TOOLKIT ---",      None),
        ("Signal Scanner (433 MHz)",    "rx_433"),
        ("Signal Scanner (868 MHz)",    "rx_868"),
        ("Signal Scanner (RAW 433)",    "rx_raw_433"),
        ("Replay Signals (SD card)",    "files"),
        ("Flipper Chat (SubGHz)",       "chat"),
        # NFC Toolkit
        ("--- NFC TOOLKIT ---",         None),
        ("NFC Read Tag",                "nfc_read"),
        ("NFC Scanner",                 "nfc_scan"),
        ("NFC Emulate (SD card)",       "nfc_files"),
        # System
        ("--- SYSTEM ---",              None),
        ("Device Info",                 "info"),
    ]

    def _update_flipper_screen(self):
        px = pyxel
        if self._flipper_mode == "idle":
            menu_len = len(self._FLIPPER_MENU)
            if px.btnp(px.KEY_UP):
                sel = self._flipper_sel - 1
                while sel >= 0 and self._FLIPPER_MENU[sel][1] is None:
                    sel -= 1
                if sel >= 0:
                    self._flipper_sel = sel
            elif px.btnp(px.KEY_DOWN):
                sel = self._flipper_sel + 1
                while sel < menu_len and self._FLIPPER_MENU[sel][1] is None:
                    sel += 1
                if sel < menu_len:
                    self._flipper_sel = sel
            elif px.btnp(px.KEY_RETURN):
                _, action = self._FLIPPER_MENU[self._flipper_sel]
                if action:
                    self._flipper_action(action)
            elif px.btnp(px.KEY_ESCAPE):
                self._flipper_screen = False
                self._esc_consumed_frame = px.frame_count
        elif self._flipper_mode == "rx_active":
            if px.btnp(px.KEY_ESCAPE) or px.btnp(px.KEY_X):
                self._flipper.subghz_stop()
                self._flipper.stop_rx()
                self._flipper_mode = "idle"
                self._flipper_log.append(("Scanner stopped", C_WARNING))
                self._esc_consumed_frame = px.frame_count
            elif px.btnp(px.KEY_PAGEUP):
                self._flipper_scroll = min(self._flipper_scroll + 5,
                                           max(0, len(self._flipper_log) - 10))
            elif px.btnp(px.KEY_PAGEDOWN):
                self._flipper_scroll = max(0, self._flipper_scroll - 5)
        elif self._flipper_mode == "files":
            if px.btnp(px.KEY_UP):
                self._flipper_sel = max(0, self._flipper_sel - 1)
            elif px.btnp(px.KEY_DOWN):
                self._flipper_sel = min(len(self._flipper_files) - 1,
                                        self._flipper_sel + 1)
            elif px.btnp(px.KEY_RETURN) and self._flipper_files:
                path, display = self._flipper_files[self._flipper_sel]
                self._flipper_log.append((f"Transmitting: {display}", C_WARNING))
                self._flipper.subghz_tx_file(path)
                self.gain_xp(25)
                self._flipper_log.append((f"Signal sent!", C_SUCCESS))
            elif px.btnp(px.KEY_ESCAPE):
                self._flipper_mode = "idle"
                self._esc_consumed_frame = px.frame_count
        elif self._flipper_mode == "nfc_emulate":
            if px.btnp(px.KEY_UP):
                self._flipper_sel = max(0, self._flipper_sel - 1)
            elif px.btnp(px.KEY_DOWN):
                self._flipper_sel = min(len(self._flipper_files) - 1,
                                        self._flipper_sel + 1)
            elif px.btnp(px.KEY_RETURN) and self._flipper_files:
                path, display = self._flipper_files[self._flipper_sel]
                self._flipper_log.append(
                    (f"Emulating: {display}", C_WARNING))
                self._flipper.start_rx(self._on_flipper_line)
                self._flipper.nfc_emulate(path)
                self._flipper_mode = "nfc_active"
                self.gain_xp(25)
            elif px.btnp(px.KEY_ESCAPE):
                self._flipper_mode = "idle"
                self._esc_consumed_frame = px.frame_count
        elif self._flipper_mode == "nfc_active":
            if px.btnp(px.KEY_ESCAPE) or px.btnp(px.KEY_X):
                self._flipper.nfc_stop()
                self._flipper.stop_rx()
                self._flipper_mode = "idle"
                self._flipper_log.append(("NFC stopped", C_WARNING))
                self._esc_consumed_frame = px.frame_count
        elif self._flipper_mode == "folders":
            if px.btnp(px.KEY_UP):
                self._flipper_sel = max(0, self._flipper_sel - 1)
            elif px.btnp(px.KEY_DOWN):
                self._flipper_sel = min(len(self._flipper_folders) - 1,
                                        self._flipper_sel + 1)
            elif px.btnp(px.KEY_RETURN) and self._flipper_folders:
                folder = self._flipper_folders[self._flipper_sel]
                self._load_flipper_folder(folder)
            elif px.btnp(px.KEY_ESCAPE):
                self._flipper_mode = "idle"
                self._esc_consumed_frame = px.frame_count
        else:
            if px.btnp(px.KEY_ESCAPE):
                self._flipper_mode = "idle"
                self._esc_consumed_frame = px.frame_count

    def _flipper_action(self, action: str):
        if not self._flipper.ensure_connected():
            self._flipper_log.append(("Flipper not connected!", C_ERROR))
            return
        if not self._flipper_used:
            self._flipper_used = True
            self._earn_badge("flipper")

        if action == "rx_433":
            self._flipper_log.append(("Scanning 433.92 MHz...", C_HACK_CYAN))
            self._flipper_mode = "rx_active"
            self._flipper_scroll = 0
            self._flipper.start_rx(self._on_flipper_line)
            self._flipper.subghz_rx(433920000)
        elif action == "rx_868":
            self._flipper_log.append(("Scanning 868.35 MHz...", C_HACK_CYAN))
            self._flipper_mode = "rx_active"
            self._flipper_scroll = 0
            self._flipper.start_rx(self._on_flipper_line)
            self._flipper.subghz_rx(868350000)
        elif action == "rx_raw_433":
            self._flipper_log.append(("RAW capture 433.92 MHz...", C_HACK_CYAN))
            self._flipper_mode = "rx_active"
            self._flipper_scroll = 0
            self._flipper.start_rx(self._on_flipper_line)
            self._flipper.subghz_rx_raw(433920000)
        elif action == "files":
            self._flipper_log.append(("Loading SD card...", C_DIM))
            self._load_flipper_sd()
        elif action == "nfc_read":
            self._flipper_log.append(("Reading NFC tag...", 12))
            self._flipper_log.append(("Hold tag near Flipper", C_DIM))
            def _nfc_read():
                lines = self._flipper.send("nfc")
                time.sleep(0.3)
                lines = self._flipper.send("mfu info")
                tag_data = []
                uid = ""
                tag_type = ""
                for l in lines:
                    if l and not l.startswith("[nfc]"):
                        self._flipper_log.append((l, C_SUCCESS))
                        tag_data.append(l)
                        if "UID:" in l:
                            uid = l.split("UID:")[-1].strip()
                        if "Type:" in l:
                            tag_type = l.split("Type:")[-1].strip()
                self._flipper.send("exit")
                self.gain_xp(15)
                # Store tag data for save dialog
                if tag_data and self.loot:
                    self._nfc_pending_save = {
                        "data": tag_data,
                        "uid": uid,
                        "type": tag_type,
                    }
                    # Show name input dialog
                    default_name = tag_type.replace(" ", "_") if tag_type else "tag"
                    self._flipper_screen = False
                    self.input_mode = True
                    self.input_fields = [{"label": "Tag name", "value": default_name}]
                    self.input_field_idx = 0
                    self._input_pending_cat = -8  # NFC tag save
            threading.Thread(target=_nfc_read, daemon=True).start()
        elif action == "nfc_scan":
            self._flipper_log.append(("NFC Scanner starting...", C_HACK_CYAN))
            self._flipper_mode = "rx_active"
            self._flipper_scroll = 0
            self._flipper.start_rx(self._on_flipper_line)
            self._flipper.nfc_scan()
        elif action == "nfc_files":
            self._flipper_log.append(("Loading NFC cards...", C_DIM))
            lines = self._flipper.storage_list("/ext/nfc")
            files = []
            for l in lines:
                if l.startswith("[F]") and ".nfc" in l:
                    fname = l.split("]")[-1].strip().split(" ")[0]
                    path = f"/ext/nfc/{fname}"
                    display = fname.replace(".nfc", "")
                    files.append((path, display))
            if files:
                self._flipper_files = files
                self._flipper_mode = "nfc_emulate"
                self._flipper_sel = 0
                self._flipper_log.append(
                    (f"Found {len(files)} NFC cards", C_SUCCESS))
            else:
                self._flipper_log.append(("No .nfc files on SD", C_WARNING))
        elif action == "chat":
            self._flipper_log.append(("SubGHz Chat — not yet implemented", C_DIM))
        elif action == "info":
            lines = self._flipper.send("device_info")
            for l in lines[:20]:
                self._flipper_log.append((l, C_DIM))
        elif action == "led":
            self._flipper.led(0, 0, 255)
            self._flipper_log.append(("LED: blue", C_HACK_CYAN))
            threading.Thread(target=lambda: (
                time.sleep(1), self._flipper.led(0, 255, 0),
                time.sleep(1), self._flipper.led(255, 0, 0),
                time.sleep(1), self._flipper.led(0, 0, 0)),
                daemon=True).start()

    def _load_flipper_sd(self):
        """Load SubGHz folder structure from Flipper SD."""
        lines = self._flipper.send("storage tree /ext/subghz")
        folders = set()
        root_files = []
        for l in lines:
            if l.startswith("[D] /ext/subghz/"):
                name = l.split("/ext/subghz/")[-1]
                if "/" not in name and name not in ("assets", "playlist", "remote"):
                    folders.add(name)
            elif l.startswith("[F] /ext/subghz/") and ".sub" in l:
                path = l.split(" ")[1] if " " in l else l[4:]
                path = path.split(" ")[0]  # remove size
                fname = path.split("/")[-1]
                # Only root-level .sub files
                parts = path.replace("/ext/subghz/", "").split("/")
                if len(parts) == 1:
                    root_files.append((path, fname))
                elif len(parts) == 2:
                    folders.add(parts[0])

        self._flipper_folders = sorted(folders)
        self._flipper_root_files = root_files
        if self._flipper_folders:
            self._flipper_mode = "folders"
            self._flipper_sel = 0
            self._flipper_log.append(
                (f"Found {len(self._flipper_folders)} folders, "
                 f"{len(root_files)} root files", C_SUCCESS))
        elif root_files:
            self._flipper_files = root_files
            self._flipper_mode = "files"
            self._flipper_sel = 0
        else:
            self._flipper_log.append(("No .sub files found on SD", C_WARNING))

    def _load_flipper_folder(self, folder: str):
        """Load .sub files from a specific folder."""
        lines = self._flipper.storage_list(f"/ext/subghz/{folder}")
        files = []
        for l in lines:
            if l.startswith("[F]") and ".sub" in l:
                fname = l.split("]")[-1].strip().split(" ")[0]
                path = f"/ext/subghz/{folder}/{fname}"
                files.append((path, fname))
        if files:
            self._flipper_files = files
            self._flipper_mode = "files"
            self._flipper_sel = 0
            self._flipper_log.append(
                (f"{folder}: {len(files)} signals", C_SUCCESS))
        else:
            self._flipper_log.append((f"{folder}: empty", C_WARNING))

    def _on_flipper_line(self, line: str):
        """Callback from Flipper RX background thread."""
        if line.startswith(">"):
            return
        self._flipper_log.append((line, C_SUCCESS))
        self.gain_xp(5)  # XP for captured signal
        if len(self._flipper_log) > 500:
            self._flipper_log = self._flipper_log[-500:]

    _FLIPPER_ASCII = [
        "_.-------.._                    -,",
        '.-"```"--..,,_/ /`-,               -,  \\',
        '.:"          /:/  /\'\\  \\     ,_...,  `. |  |',
        "/       ,----/:/  /`\\ _\\~`_-\"`     _;",
        "'      / /`\"\"\"'\\ \\ \\.~`_-'      ,-\"'/",
        "|      | |  0    | | .-'      ,/`  /",
        "|    ,..\\  \\     ,.-\"`       ,/`    /",
        ';    :    `/`""\\`           ,/--==,/-----,',
        "|    `-...|        -.___-Z:_______J...---;",
        ":         `                           _-'",
        " _L_  _     ___  ___  ___  ___  ____--\"`",
        " | __|| |   |_ _|| _ || _ || __|| _ |",
        " | _| | |__  | | |  _||  _|| _| |   /",
        " |_|  |____||___||_|  |_|  |___||_|_\\",
    ]

    def _draw_flipper_screen(self):
        """Draw fullscreen Flipper Zero interface."""
        pyxel.cls(0)
        pyxel.rectb(1, 1, W - 2, H - 2, C_WARNING)
        pyxel.rect(2, 2, W - 4, 14, 1)
        name = self._flipper.device_name or "Flipper Zero"
        fw = self._flipper.firmware or "?"
        conn = "ONLINE" if self._flipper.connected else "OFFLINE"
        conn_c = C_SUCCESS if self._flipper.connected else C_ERROR
        pyxel.text(6, 5, f"FLIPPER ZERO // {name} // {fw}", C_WARNING)
        pyxel.text(W - 75, 5, conn, conn_c)
        pyxel.text(W - 35, 5, "[ESC]", C_DIM)
        pyxel.line(1, 16, W - 2, 16, C_WARNING)

        mid_y = 18
        split_y = H // 2 + 50  # top: ASCII + menu, bottom: log
        ROW_H = 13              # 5x8 font with 5px gap/padding

        if self._flipper_mode == "idle":
            # ASCII dolphin on left — 9px per line (8px glyph + 1)
            for i, line in enumerate(self._FLIPPER_ASCII):
                pyxel.text(10, mid_y + 2 + i * 9, line, C_WARNING)

            # Menu on right
            mx = 310
            y = mid_y + 2
            for i, (label, action) in enumerate(self._FLIPPER_MENU):
                if action is None:
                    # Section header
                    pyxel.text(mx, y + 2, label.strip("- "), C_HACK_CYAN)
                    pyxel.line(mx, y + 11, mx + 140, y + 11, 1)
                    y += ROW_H
                else:
                    sel = (i == self._flipper_sel)
                    if sel:
                        pyxel.rect(mx - 2, y - 1, 200, ROW_H - 1, C_WARNING)
                    c = 0 if sel else C_TEXT
                    pyxel.text(mx + 2, y + 2, label, c)
                    y += ROW_H

            pyxel.text(mx, split_y - 12,
                       "UP/DOWN  ENTER  ESC=close", C_DIM)

        elif self._flipper_mode == "folders":
            pyxel.text(10, mid_y + 2, "SIGNAL LIBRARY (SD)", C_HACK_CYAN)
            pyxel.line(10, mid_y + 12, 200, mid_y + 12, 1)
            max_vis = (split_y - mid_y - 32) // ROW_H
            scroll = max(0, self._flipper_sel - max_vis + 1)
            folders = self._flipper_folders
            for i in range(scroll, min(len(folders), scroll + max_vis)):
                y = mid_y + 16 + (i - scroll) * ROW_H
                sel = (i == self._flipper_sel)
                if sel:
                    pyxel.rect(8, y - 1, 300, ROW_H - 2, C_WARNING)
                c = 0 if sel else C_TEXT
                pyxel.text(10, y + 1, f"[DIR] {folders[i]}", c)
            # Also show root files count
            if self._flipper_root_files:
                pyxel.text(320, mid_y + 2,
                           f"+{len(self._flipper_root_files)} root signals",
                           C_DIM)
            pyxel.text(10, split_y - 12,
                       "ENTER=open folder  ESC=back", C_DIM)

        elif self._flipper_mode == "files":
            pyxel.text(10, mid_y + 2, "SIGNALS — ENTER to transmit", C_HACK_CYAN)
            pyxel.line(10, mid_y + 12, 260, mid_y + 12, 1)
            max_vis = (split_y - mid_y - 32) // ROW_H
            scroll = max(0, self._flipper_sel - max_vis + 1)
            files = self._flipper_files
            for i in range(scroll, min(len(files), scroll + max_vis)):
                y = mid_y + 16 + (i - scroll) * ROW_H
                sel = (i == self._flipper_sel)
                if sel:
                    pyxel.rect(8, y - 1, 400, ROW_H - 2, C_WARNING)
                c = 0 if sel else C_TEXT
                _, display = files[i]
                pyxel.text(10, y + 1, f"[TX] {display}", c)
            pyxel.text(10, split_y - 12,
                       "ENTER=transmit  ESC=back", C_DIM)

        elif self._flipper_mode == "nfc_emulate":
            pyxel.text(10, mid_y + 2, "NFC CARDS — select to emulate", C_HACK_CYAN)
            pyxel.line(10, mid_y + 12, 270, mid_y + 12, 1)
            max_vis = (split_y - mid_y - 32) // ROW_H
            scroll = max(0, self._flipper_sel - max_vis + 1)
            files = self._flipper_files
            for i in range(scroll, min(len(files), scroll + max_vis)):
                y = mid_y + 16 + (i - scroll) * ROW_H
                sel = (i == self._flipper_sel)
                if sel:
                    pyxel.rect(8, y - 1, 400, ROW_H - 2, 12)
                c = 0 if sel else C_TEXT
                _, display = files[i]
                pyxel.text(10, y + 1, f"[NFC] {display}", c)
            pyxel.text(10, split_y - 12,
                       "ENTER=emulate  ESC=back", C_DIM)

        elif self._flipper_mode == "nfc_active":
            pyxel.text(10, mid_y + 2, "NFC EMULATION — ACTIVE", 12)
            dots = "." * ((pyxel.frame_count // 15) % 4)
            pyxel.text(10, mid_y + 16, f"Card emulating{dots}", C_SUCCESS)
            pyxel.text(10, mid_y + 30,
                       "Hold Flipper near reader", C_DIM)
            pyxel.text(220, mid_y + 2, "[X] or [ESC] to stop", C_DIM)

        elif self._flipper_mode == "rx_active":
            pyxel.text(10, mid_y + 2, "SIGNAL SCANNER — LIVE", C_HACK_CYAN)
            freq_txt = "Listening for signals..."
            pyxel.text(10, mid_y + 16, freq_txt, C_SUCCESS)
            dots = "." * ((pyxel.frame_count // 15) % 4)
            pyxel.text(10, mid_y + 30, f"Scanning{dots}", C_DIM)
            pyxel.text(220, mid_y + 2, "[X] or [ESC] to stop", C_DIM)

        # Bottom: log output (9px per line for 5x8 font)
        pyxel.line(1, split_y, W - 2, split_y, C_COAST)
        pyxel.text(6, split_y + 3, "OUTPUT", C_DIM)
        cnt = len(self._flipper_log)
        pyxel.text(70, split_y + 3, f"({cnt} lines)", C_DIM)
        log_y = split_y + 14
        LINE_H = 9
        max_lines = (H - log_y - 4) // LINE_H
        total = len(self._flipper_log)
        if self._flipper_scroll == 0:
            start = max(0, total - max_lines)
            end = total
        else:
            end = max(0, total - self._flipper_scroll)
            start = max(0, end - max_lines)
        y = log_y
        for i in range(start, end):
            text, color = self._flipper_log[i]
            pyxel.text(6, y, text[:120], color)
            y += LINE_H
            if y >= H - 4:
                break

    def _draw_flash_screen(self):
        """Board picker with a manual mode that preserves ROM boot state."""
        from .config import FLASH_BOARDS
        boards = list(FLASH_BOARDS.items())
        dw, dh = 500, 252
        dx, dy = (W - dw) // 2, (H - dh) // 2
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_WARNING)
        pyxel.text(dx + 8, dy + 6, "FLASH ESP32 / Smethan fork", C_WARNING)
        pyxel.line(dx + 4, dy + 18, dx + dw - 5, dy + 18, C_COAST)
        pyxel.text(dx + 8, dy + 25, getattr(self, '_flash_status', '')[:115], C_HACK_CYAN)
        if self._flash_running:
            pyxel.text(dx + 8, dy + 48, "Check the console for progress. Full output is saved to firmware_cache/flash-*.log", C_DIM)
            pyxel.text(dx + 8, dy + 75, "Keep USB connected until flashing finishes.", C_WARNING)
            pyxel.text(dx + 8, dy + dh - 16, "ESC hides this window; flashing continues", C_DIM)
            return
        for i, (_, profile) in enumerate(boards):
            y = dy + 45 + i * 16
            selected = i == self._flash_sel
            if selected:
                pyxel.rect(dx + 4, y - 2, dw - 8, 15, C_WARNING)
            pyxel.text(dx + 8, y, profile['label'], 0 if selected else C_TEXT)
        manual = getattr(self, '_flash_manual', False)
        version = getattr(self, '_flash_release_tag', None) or "Latest stable"
        if getattr(self, '_flash_versions_loading', False):
            version += " (loading older versions...)"
        elif getattr(self, '_flash_release_error', ''):
            version += " (V retries version list)"
        pyxel.text(dx + 8, dy + 84, "V/B version: " + version, C_WARNING)
        pyxel.text(dx + 8, dy + 104, "LEFT/RIGHT: " + ("Manual BOOT (no reset, 115200)" if manual else "Automatic (script reset, 460800)"), C_HACK_CYAN)
        if manual:
            pyxel.text(dx + 8, dy + 126, "Hold BOOT, tap RESET (or reconnect USB), then release BOOT.", C_WARNING)
            pyxel.text(dx + 8, dy + 140, "Press ENTER once the board is in bootloader mode.", C_DIM)
        else:
            pyxel.text(dx + 8, dy + 126, "USB power stays on. If automatic fails, try Manual BOOT.", C_DIM)
        fw_info = "Firmware: " + ("v" + self._fw_version if self._fw_version else "unknown")
        if self._fw_update_available:
            fw_info += " -> v" + self._fw_remote_version
        pyxel.text(dx + 8, dy + 160, "Target: " + getattr(self, '_flash_target_label', 'Not selected')[:110], C_HACK_CYAN)
        target = getattr(self, '_flash_target', None)
        if target:
            identity = f"USB {target.vid:04X}:{target.pid:04X}  Serial: {target.serial_number or 'unknown'}  Location: {target.location or 'unknown'}"
            pyxel.text(dx + 8, dy + 174, identity[:119], C_DIM)
        pyxel.text(dx + 8, dy + 192, fw_info, C_DIM)
        pyxel.text(dx + 8, dy + 206, "R refreshes target (no USB reset). Check it before ENTER.", C_WARNING)
        pyxel.text(dx + 8, dy + 220, "WDG serial is paused while this window is open.", C_DIM)
        pyxel.text(dx + 8, dy + dh - 16, "UP/DOWN board   ENTER flash   ESC exit / reconnect", C_DIM)

    def _draw_captured_data(self):
        """Draw overlay showing captured credentials from portal/evil twin."""
        # Collect captured lines from terminal
        with self._term_lock:
            captures = [l for l in self.terminal_lines
                        if ":PWD]" in l]
        tag = "EVIL PORTAL" if self._attack_mode == "evil_portal" else "EVIL TWIN"
        n_clients = self.state.portal_client_count
        n_forms = (self.state.submitted_forms
                   + len(self.state.evil_twin_captured_data))
        # Solid background
        pyxel.rect(0, HUD_TOP, W, TERM_Y - HUD_TOP, 0)
        # Dialog box — 12px rows for 5x8 font
        ROW_H = 12
        dw, dh = 560, 240
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, 12)  # blue border
        pyxel.rectb(dx + 1, dy + 1, dw - 2, dh - 2, C_COAST)
        # Title
        pyxel.text(dx + 4, dy + 4,
                   f"{tag} — Captured Data", 12)
        pyxel.text(dx + dw - 150, dy + 4,
                   f"Clients:{n_clients} Forms:{n_forms}", C_DIM)
        pyxel.line(dx + 2, dy + 14, dx + dw - 3, dy + 14, C_COAST)
        # SSID info
        pyxel.text(dx + 4, dy + 18,
                   f"SSID: {self._portal_ssid}", C_HACK_CYAN)
        pyxel.line(dx + 2, dy + 28, dx + dw - 3, dy + 28, 1)
        # Captured data list
        if not captures:
            pyxel.text(dx + (dw - 27 * 5) // 2, dy + dh // 2,
                       "No credentials captured yet", C_DIM)
        else:
            max_vis = (dh - 60) // ROW_H
            scroll = getattr(self, '_portal_data_scroll', 0)
            scroll = min(scroll, max(0, len(captures) - max_vis))
            self._portal_data_scroll = scroll
            y = dy + 32
            for i in range(scroll, min(len(captures), scroll + max_vis)):
                line = captures[i]
                # Strip the [EP:PWD] or [ET:PWD] prefix for cleaner display
                display = line
                if ":PWD] " in line:
                    display = line.split(":PWD] ", 1)[1]
                pyxel.text(dx + 6, y, display[:90], 12)  # blue text
                y += ROW_H
            # Scroll indicator
            if len(captures) > max_vis:
                bar_total = dh - 60
                bar_h = max(4, bar_total * max_vis // len(captures))
                bar_y = dy + 32 + (bar_total - bar_h) * scroll // max(
                    1, len(captures) - max_vis)
                pyxel.rect(dx + dw - 4, bar_y, 2, bar_h, 12)
        # Hints
        pyxel.text(dx + 4, dy + dh - 11,
                   "[D] close  [UP/DOWN] scroll  [X] stop attack", C_DIM)

    def _draw_input_dialog(self):
        # Dim overlay
        for y in range(HUD_TOP, TERM_Y):
            if y % 2 == 0:
                pyxel.line(0, y, W - 1, y, 0)
        # Dialog box — 20px per field (was 16 for 4x6 font; now 8px text + 12 gap)
        FIELD_H = 20
        dh = 40 + len(self.input_fields) * FIELD_H
        dw = 320
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_HACK_CYAN)
        # Title — special cat codes use custom labels
        _SPECIAL_TITLES = {
            -1: "MITM Target",
            -2: "BlueDucky",
            -3: "Whitelist",
            -4: "Whitelist",
            -5: "MeshCore",
            -6: "WPA-sec Token",
            -7: "Evil Portal",
        }
        if self._input_pending_cat < 0:
            name = _SPECIAL_TITLES.get(self._input_pending_cat, "Input")
        else:
            _, items = MENU_CATS[self._input_pending_cat]
            _hk, name, _cmd, _sk, _it = items[self._input_pending_item]
        pyxel.text(dx + 4, dy + 4, f"INPUT: {name}", C_HACK_CYAN)
        pyxel.line(dx, dy + 14, dx + dw - 1, dy + 14, C_COAST)
        # Fields
        for i, field in enumerate(self.input_fields):
            fy = dy + 20 + i * FIELD_H
            active = (i == self.input_field_idx)
            pyxel.text(dx + 6, fy, f"{field['label']}:",
                       C_HACK_CYAN if active else C_DIM)
            bx = dx + 56
            bw2 = dw - 64
            pyxel.rect(bx, fy - 2, bw2, 12, 1)
            pyxel.rectb(bx, fy - 2, bw2, 12, C_HACK_CYAN if active else C_COAST)
            val = field["value"]
            if active and pyxel.frame_count % 30 < 20:
                val += "_"
            pyxel.text(bx + 3, fy, val[:36], C_SUCCESS if active else C_TEXT)
        # Hint
        pyxel.text(dx + 4, dy + dh - 11, "ENTER confirm  ESC cancel", C_DIM)

    def _draw_confirm_quit(self):
        # Dim overlay
        for y in range(HUD_TOP, TERM_Y):
            if y % 2 == 0:
                pyxel.line(0, y, W - 1, y, 0)
        # Dialog box — larger for readability (5x8 font)
        dw, dh = 260, 78
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_ERROR)
        pyxel.rectb(dx + 1, dy + 1, dw - 2, dh - 2, C_ERROR)
        # Glitch accent line
        pyxel.line(dx + 3, dy + 2, dx + dw - 4, dy + 2, C_ERROR)
        # Title (15 chars × 5 = 75px)
        pyxel.text(dx + (dw - 15 * 5) // 2, dy + 8,
                   "QUIT WATCHDOGS?", C_ERROR)
        pyxel.line(dx + 2, dy + 20, dx + dw - 3, dy + 20, C_COAST)
        # Subtitle (25 chars × 5 = 125px)
        pyxel.text(dx + (dw - 25 * 5) // 2, dy + 26,
                   "All operations will stop.", C_DIM)
        # Prompt — centered, larger spacing
        bx = dx + dw // 2 - 60
        pyxel.text(bx, dy + 46, "[Y]", C_SUCCESS)
        pyxel.text(bx + 20, dy + 46, "Quit", C_TEXT)
        pyxel.text(bx + 70, dy + 46, "[N]", C_ERROR)
        pyxel.text(bx + 90, dy + 46, "Cancel", C_TEXT)
        # Blink hint (12 chars × 5 = 60px)
        if pyxel.frame_count % 40 < 28:
            pyxel.text(dx + (dw - 12 * 5) // 2, dy + 62,
                       "press Y or N", C_DIM)

    def _draw_gps_wait_dialog(self):
        # Dim overlay
        for y in range(HUD_TOP, TERM_Y):
            if y % 2 == 0:
                pyxel.line(0, y, W - 1, y, 0)
        # Dialog box
        dw, dh = 280, 70
        dx = (W - dw) // 2
        dy = (H - dh) // 2
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, C_WARNING)
        pyxel.line(dx + 2, dy + 1, dx + dw - 3, dy + 1, C_WARNING)
        # Title
        pyxel.text(dx + 4, dy + 4, "NO GPS FIX", C_WARNING)
        pyxel.line(dx, dy + 13, dx + dw - 1, dy + 13, C_COAST)
        # Info
        if self.gps_sats:
            sat = f"Satellites: {self.gps_sats}"
        elif self.gps_sats_vis:
            sat = f"Visible: {self.gps_sats_vis}"
        else:
            sat = "No satellites detected"
        pyxel.text(dx + 4, dy + 18, sat, C_DIM)
        pyxel.text(dx + 4, dy + 30, "Wait for GPS fix?", C_TEXT)
        # Buttons
        pyxel.text(dx + 18, dy + 44, "[Y]", C_SUCCESS)
        pyxel.text(dx + 38, dy + 44, "wait", C_TEXT)
        pyxel.text(dx + 90, dy + 44, "[N]", C_ERROR)
        pyxel.text(dx + 110, dy + 44, "cancel", C_TEXT)

    # ------------------------------------------------------------------
    # MITM dedicated screen (JanOS-style sub-screen)
    # ------------------------------------------------------------------

    def _update_mitm_screen(self):
        """Handle keys for the MITM sub-screen state machine."""
        st = self._mitm_state

        if st == "idle":
            if pyxel.btnp(pyxel.KEY_S):
                ifaces = self._mitm.get_interfaces()
                if not ifaces:
                    self._mitm_msg("[MITM] No network interface with IP", C_ERROR)
                    return
                self._mitm_ifaces = ifaces
                if len(ifaces) == 1:
                    self._attack_iface = ifaces[0][0]
                    self._mitm_msg(f"[MITM] Interface: {ifaces[0][0]} ({ifaces[0][1]})")
                    self._mitm_state = "target_mode"
                    self._mitm_sel = 0
                else:
                    self._mitm_state = "iface"
                    self._mitm_sel = 0
            elif pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mitm_screen = False
                self._mitm_state = "idle"
                self._mitm_close_frame = pyxel.frame_count
                self._esc_consumed_frame = pyxel.frame_count
            return

        if st == "iface":
            if pyxel.btnp(pyxel.KEY_UP):
                self._mitm_sel = max(0, self._mitm_sel - 1)
            elif pyxel.btnp(pyxel.KEY_DOWN):
                self._mitm_sel = min(len(self._mitm_ifaces) - 1, self._mitm_sel + 1)
            elif pyxel.btnp(pyxel.KEY_RETURN):
                idx = self._mitm_sel
                if idx < len(self._mitm_ifaces):
                    self._attack_iface = self._mitm_ifaces[idx][0]
                    self._mitm_msg(f"[MITM] Interface: {self._mitm_ifaces[idx][0]}")
                    self._mitm_state = "target_mode"
                    self._mitm_sel = 0
            elif pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mitm_state = "idle"
            return

        if st == "target_mode":
            if pyxel.btnp(pyxel.KEY_UP):
                self._mitm_sel = max(0, self._mitm_sel - 1)
            elif pyxel.btnp(pyxel.KEY_DOWN):
                self._mitm_sel = min(2, self._mitm_sel + 1)
            elif pyxel.btnp(pyxel.KEY_RETURN):
                if self._mitm_sel == 0:
                    # Single target — use input dialog
                    self._mitm_state = "input_ip"
                    self.input_mode = True
                    self.input_fields = [{"label": "Victim IP", "value": "", "pos": 0}]
                    self.input_field_idx = 0
                    self._input_pending_cat = -1
                    self._input_pending_item = -1
                elif self._mitm_sel == 1:
                    # Scan subnet
                    self._mitm_state = "scan"
                    self._mitm_hosts = []
                    self._mitm_sel = 0
                    self._mitm_error = ""

                    def _do_scan():
                        try:
                            iface = self._attack_iface
                            subnet = self._mitm.get_subnet(iface)
                            self._mitm_msg(f"[MITM] Scanning {subnet}...", C_HACK_CYAN)
                            hosts = self._mitm.arp_scan(subnet, iface)
                            gw = self._mitm.get_default_gateway()
                            my_ips = {ip for _, ip in self._mitm.get_interfaces()}
                            filtered = [(ip, mac) for ip, mac in hosts
                                        if ip != gw and ip not in my_ips]
                            self._mitm_hosts = filtered
                            if filtered:
                                self._mitm_msg(f"[MITM] Found {len(filtered)} hosts")
                            else:
                                self._mitm_error = "No hosts found on subnet"
                                self._mitm_state = "error"
                        except Exception as e:
                            self._mitm_error = str(e)[:80]
                            self._mitm_state = "error"

                    threading.Thread(target=_do_scan, daemon=True).start()
                elif self._mitm_sel == 2:
                    # All devices — scan first, then confirm
                    self._mitm_state = "scan_all"
                    self._mitm_hosts = []
                    self._mitm_victim_ip = ""  # means target_all
                    self._mitm_error = ""

                    def _resolve_all():
                        try:
                            iface = self._attack_iface
                            subnet = self._mitm.get_subnet(iface)
                            self._mitm_msg(f"[MITM] Scanning {subnet} (all hosts)...", C_WARNING)
                            hosts = self._mitm.arp_scan(subnet, iface)
                            gw = self._mitm.get_default_gateway()
                            my_ips = {ip for _, ip in self._mitm.get_interfaces()}
                            filtered = [(ip, mac) for ip, mac in hosts
                                        if ip != gw and ip not in my_ips]
                            self._mitm_hosts = filtered
                            if filtered:
                                self._mitm_msg(f"[MITM] {len(filtered)} hosts found")
                                self._mitm_state = "confirm"
                            else:
                                self._mitm_error = "No hosts found on subnet"
                                self._mitm_state = "error"
                        except Exception as e:
                            self._mitm_error = str(e)[:80]
                            self._mitm_state = "error"

                    threading.Thread(target=_resolve_all, daemon=True).start()
            elif pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mitm_state = "idle"
            return

        if st == "scan":
            if self._mitm_hosts:
                if pyxel.btnp(pyxel.KEY_UP):
                    self._mitm_sel = max(0, self._mitm_sel - 1)
                elif pyxel.btnp(pyxel.KEY_DOWN):
                    self._mitm_sel = min(len(self._mitm_hosts) - 1, self._mitm_sel + 1)
                elif pyxel.btnp(pyxel.KEY_RETURN):
                    idx = self._mitm_sel
                    if idx < len(self._mitm_hosts):
                        self._mitm_victim_ip = self._mitm_hosts[idx][0]
                        self._mitm_state = "confirm"
                elif pyxel.btnp(pyxel.KEY_ESCAPE):
                    self._mitm_state = "idle"
            elif pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mitm_state = "idle"
            return

        if st == "scan_all":
            # Waiting for scan to finish — thread transitions to confirm or error
            if pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mitm_state = "idle"
            return

        if st == "error":
            if pyxel.btnp(pyxel.KEY_ESCAPE) or pyxel.btnp(pyxel.KEY_RETURN):
                self._mitm_state = "idle"
                self._mitm_error = ""
            return

        if st == "confirm":
            if pyxel.btnp(pyxel.KEY_Y):
                self._mitm_state = "running"
                self._mitm_log_scroll = 0

                def _start():
                    if self._mitm_victim_ip:
                        ok = self._mitm.start(self._attack_iface,
                                              victim_ip=self._mitm_victim_ip)
                    else:
                        ok = self._mitm.start(self._attack_iface, target_all=True)
                    if not ok:
                        self._mitm_error = "Attack failed to start (check logs)"
                        self._mitm_state = "error"

                threading.Thread(target=_start, daemon=True).start()
            elif pyxel.btnp(pyxel.KEY_N) or pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mitm_state = "idle"
            return

        if st == "running":
            if pyxel.btnp(pyxel.KEY_X):
                threading.Thread(target=self._mitm.stop, daemon=True).start()
                self._mitm_state = "idle"
                self._mitm_msg("[MITM] Stopped", C_WARNING)
            elif pyxel.btnp(pyxel.KEY_PAGEUP):
                self._mitm_log_scroll = min(
                    self._mitm_log_scroll + 5,
                    max(0, len(self._mitm_log) - 5))
            elif pyxel.btnp(pyxel.KEY_PAGEDOWN):
                self._mitm_log_scroll = max(0, self._mitm_log_scroll - 5)
            elif pyxel.btnp(pyxel.KEY_END):
                self._mitm_log_scroll = 0
            elif pyxel.btnp(pyxel.KEY_ESCAPE):
                # ESC while running → go back to idle (attack keeps running)
                pass
            return

    def _draw_mitm_screen(self):
        """Draw the MITM sub-screen (full overlay like loot_screen)."""
        pyxel.cls(0)
        st = self._mitm_state
        BAR_H = 14  # title/status bar height (8px text + padding)

        # ── Info bar (top) ──
        pyxel.rect(0, 0, W, BAR_H, 1)
        if self._mitm.running:
            victims = ", ".join(ip for ip, _ in self._mitm._victims[:4])
            if len(self._mitm._victims) > 4:
                victims += f" +{len(self._mitm._victims) - 4}"
            gw = self._mitm._gateway_ip
            info = f"MITM RUNNING | {victims} <-> {gw} | Packets: {self._mitm.packets}"
            pyxel.text(4, 4, info, C_WARNING)
        else:
            pyxel.text(4, 4, "MITM -- idle", C_DIM)

        # ── Status bar (bottom) ──
        pyxel.rect(0, H - BAR_H, W, BAR_H, 1)
        if st == "running":
            pyxel.text(4, H - 10, "[X]Stop", C_ERROR)
        elif st == "idle":
            pyxel.text(4, H - 10, "[S]Start  [ESC]Back", C_DIM)
        elif st in ("iface", "target_mode", "scan"):
            pyxel.text(4, H - 10, "[Enter]Select  [ESC]Cancel", C_DIM)
        elif st == "confirm":
            pyxel.text(4, H - 10, "[Y]Yes  [N]No", C_DIM)
        elif st == "error":
            pyxel.text(4, H - 10, "[ESC]Back", C_DIM)
        elif st == "scan_all":
            pyxel.text(4, H - 10, "Scanning...  [ESC]Cancel", C_DIM)

        # ── Dividers ──
        pyxel.line(0, BAR_H, W - 1, BAR_H, C_MENU_BORDER)
        pyxel.line(0, H - BAR_H - 1, W - 1, H - BAR_H - 1, C_MENU_BORDER)

        # ── Content area ──
        CY = BAR_H + 4       # content start Y
        CH = H - BAR_H * 2 - 6  # content height
        ROW_H = 12           # 5x8 font

        if st == "idle":
            pyxel.text(20, CY + 10, "MITM -- ARP Spoofing Attack", C_HACK_CYAN)
            pyxel.text(20, CY + 32, "Intercepts traffic between victim(s)", C_TEXT)
            pyxel.text(20, CY + 42, "and the network gateway.", C_TEXT)
            pyxel.text(20, CY + 62, "Captures: DNS queries, HTTP requests,", C_DIM)
            pyxel.text(20, CY + 72, "cleartext credentials (FTP/Telnet/POP3)", C_DIM)
            pyxel.text(20, CY + 92, "Saves full pcap to loot/mitm/", C_DIM)
            pyxel.text(20, CY + 116, "Press [S] to start", C_HACK_CYAN)

        elif st == "iface":
            pyxel.text(20, CY + 6, "Select interface:", C_HACK_CYAN)
            for i, (iface, ip) in enumerate(self._mitm_ifaces):
                ty = CY + 24 + i * ROW_H
                if i == self._mitm_sel:
                    pyxel.rect(18, ty - 2, W - 36, ROW_H, C_HACK_CYAN)
                    pyxel.text(22, ty, f"{iface} ({ip})", 0)
                else:
                    pyxel.text(22, ty, f"{iface} ({ip})", C_TEXT)

        elif st == "target_mode":
            pyxel.text(20, CY + 6, "Target mode:", C_HACK_CYAN)
            options = [
                "1. Single target (enter IP)",
                "2. Scan subnet + select",
                "3. All devices on subnet",
            ]
            for i, opt in enumerate(options):
                ty = CY + 26 + i * (ROW_H + 2)
                if i == self._mitm_sel:
                    pyxel.rect(18, ty - 2, W - 36, ROW_H + 2, C_HACK_CYAN)
                    pyxel.text(22, ty, opt, 0)
                else:
                    pyxel.text(22, ty, opt, C_TEXT)

        elif st == "scan":
            if not self._mitm_hosts:
                pyxel.text(20, CY + 20, ">>> Scanning subnet...", C_HACK_CYAN)
            else:
                pyxel.text(20, CY + 6, f"Found {len(self._mitm_hosts)} hosts:",
                           C_HACK_CYAN)
                for i, (ip, mac) in enumerate(self._mitm_hosts[:15]):
                    ty = CY + 24 + i * ROW_H
                    label = f"{ip}  ({mac})"
                    if i == self._mitm_sel:
                        pyxel.rect(18, ty - 2, W - 36, ROW_H, C_HACK_CYAN)
                        pyxel.text(22, ty, label, 0)
                    else:
                        pyxel.text(22, ty, label, C_TEXT)

        elif st == "scan_all":
            pyxel.text(20, CY + 20, ">>> Scanning all hosts on subnet...",
                       C_WARNING)
            pyxel.text(20, CY + 38, "Please wait", C_DIM)

        elif st == "error":
            pyxel.text(20, CY + 20, "ERROR", C_ERROR)
            pyxel.text(20, CY + 40, self._mitm_error or "Unknown error",
                       C_WARNING)
            # Show recent log messages for context
            with self._mitm_lock:
                recent = list(self._mitm_log[-8:])
            for i, (txt, _) in enumerate(recent):
                pyxel.text(20, CY + 60 + i * 10, txt[:120], C_DIM)
            pyxel.text(20, CY + 60 + len(recent) * 10 + 14,
                       "Press [ESC] or [ENTER] to go back", C_HACK_CYAN)

        elif st == "confirm":
            # Dialog overlay
            dw, dh = 340, 90
            dx = (W - dw) // 2
            dy = (H - dh) // 2
            pyxel.rect(dx, dy, dw, dh, 0)
            pyxel.rectb(dx, dy, dw, dh, C_HACK_CYAN)
            pyxel.text(dx + 4, dy + 4, "-- Confirm --", C_HACK_CYAN)
            pyxel.text(dx + 8, dy + 20, "Start MITM attack?", C_TEXT)
            if self._mitm_victim_ip:
                pyxel.text(dx + 8, dy + 32,
                           f"Victim: {self._mitm_victim_ip}", C_TEXT)
            elif self._mitm_hosts:
                pyxel.text(dx + 8, dy + 32,
                           f"Victims: {len(self._mitm_hosts)} hosts", C_TEXT)
            else:
                pyxel.text(dx + 8, dy + 32,
                           "Victims: all on subnet", C_TEXT)
            gw = self._mitm.get_default_gateway() or "?"
            pyxel.text(dx + 8, dy + 44, f"Gateway: {gw}", C_TEXT)
            pyxel.text(dx + 8, dy + 56,
                       f"Interface: {self._attack_iface}", C_TEXT)
            pyxel.text(dx + 8, dy + 74, "[Y] Yes   [N] No", C_DIM)

        elif st == "running":
            # Live scrolling log (9px per line for 5x8 font)
            with self._mitm_lock:
                log_snap = list(self._mitm_log)
            total = len(log_snap)
            line_h = 9
            max_vis = CH // line_h
            if self._mitm_log_scroll == 0:
                start = max(0, total - max_vis)
                end = total
            else:
                end = max(0, total - self._mitm_log_scroll)
                start = max(0, end - max_vis)
            y = CY
            for i in range(start, end):
                text, color = log_snap[i]
                # Color-code by content
                if "[DNS]" in text:
                    c = C_HACK_CYAN
                elif "[HTTP]" in text:
                    c = C_SUCCESS
                elif "[CREDS]" in text or "[AUTH" in text:
                    c = C_ERROR
                elif "[MITM]" in text:
                    c = C_WARNING
                else:
                    c = C_TEXT
                pyxel.text(4, y, text[:155], c)
                y += line_h
                if y >= CY + CH:
                    break
            # Scroll indicator
            if total > max_vis and self._mitm_log_scroll == 0:
                pyxel.text(W - 110, H - 22, "PgUp/PgDn scroll", C_DIM)

    # ------------------------------------------------------------------
    # Whitelist screen
    # ------------------------------------------------------------------

    @staticmethod
    def _wl_rssi(value) -> int:
        try:
            return int(str(value).replace("dBm", "").strip())
        except (TypeError, ValueError):
            return -100

    def _wl_scan_active(self) -> bool:
        return getattr(self, "_wl_scan_phase", "idle") in (
            "waiting_stop", "scanning_wifi", "scanning_ble", "stopping")

    def _wl_build_scan_list(self):
        """Build the picker from the dedicated scan cache, never map nodes."""
        items = [
            dict(item)
            for item in getattr(self, "_wl_scan_results", {}).values()
            if not self._whitelist.is_blocked(item["mac"])
        ]
        items.sort(key=lambda item: (-self._wl_rssi(item["rssi"]), item["mac"]))
        return items

    def _wl_refresh_scan_list(self, reset_selection=False):
        self._wl_scan_list = self._wl_build_scan_list()
        if reset_selection:
            self._wl_scan_sel = 0
        else:
            self._wl_scan_sel = min(
                getattr(self, "_wl_scan_sel", 0),
                max(0, len(self._wl_scan_list) - 1))

    def _wl_store_result(self, kind, mac, name, rssi, extra=""):
        if getattr(self, "_wl_scan_phase", "idle") != "scanning_" + kind:
            return
        mac = (mac or "").upper().strip()
        if not mac or self._whitelist.is_blocked(mac):
            return
        value = self._wl_rssi(rssi)
        key = (kind, mac)
        previous = self._wl_scan_results.get(key)
        display_name = (name or "").strip()
        if kind == "wifi" and (not display_name or display_name == "?"):
            display_name = "<hidden>"
        elif not display_name:
            display_name = "?"
        if previous:
            previous["rssi"] = max(previous["rssi"], value)
            if display_name not in ("?", "<hidden>"):
                previous["name"] = display_name
            if extra:
                previous["extra"] = extra
            return
        self._wl_scan_results[key] = {
            "type": kind,
            "mac": mac,
            "name": display_name,
            "rssi": value,
            "extra": extra,
        }

    def _wl_observe_wifi(self, net):
        self._wl_store_result(
            "wifi", getattr(net, "bssid", ""), getattr(net, "ssid", ""),
            getattr(net, "rssi", -100),
            f"Ch:{getattr(net, 'channel', '?')}")

    def _wl_observe_ble(self, mac, rssi, name, _tag_type=""):
        self._wl_store_result("ble", mac, name, rssi)

    def _wl_start_scan(self, kind):
        if kind not in ("wifi", "ble"):
            raise ValueError("Unknown whitelist scan kind")
        if self._wl_scan_active() or self._pending_cmd:
            self.msg("[SCAN] Another operation is still changing state",
                     C_WARNING)
            return False
        self._wl_scan_results = {}
        self._wl_scan_list = []
        self._wl_scan_sel = 0
        self._wl_scan_error = ""
        self._wl_scan_partial = False
        self._wl_scan_deadline = 0.0
        self._wl_stop_outcome = "idle"
        self._wl_scan_phase = "waiting_stop"
        command = "scan_networks" if kind == "wifi" else "scan_bt"
        state = "wl_wifi_scan" if kind == "wifi" else "wl_ble_scan"
        self._start_scan_cmd(
            command, state, "Whitelist " + kind.upper() + " scan")
        return True

    def _wl_finish_scan(self):
        self._wl_refresh_scan_list(reset_selection=True)
        self._wl_scan_deadline = 0.0
        self._wl_scan_error = ""
        self._wl_scan_partial = False
        self._wl_scan_phase = "complete"
        self.msg(f"[SCAN] Done — {len(self._wl_scan_list)} devices",
                 C_SUCCESS)

    def _wl_scan_fail(self, message, *, stop=False, partial=False):
        self._wl_refresh_scan_list(reset_selection=True)
        self._wl_scan_deadline = 0.0
        self._wl_scan_error = message
        self._wl_scan_partial = partial
        if stop and getattr(self, "_wl_scan_phase", "idle") in (
                "scanning_wifi", "scanning_ble"):
            self._wl_scan_phase = "stopping"
            self._wl_stop_outcome = "error"
            self._send("stop")
        else:
            self._wl_scan_phase = "error"
        suffix = (
            f" — {len(self._wl_scan_list)} partial" if partial else "")
        self.msg("[SCAN] " + message + suffix, C_WARNING)

    def _wl_cancel_scan(self):
        phase = getattr(self, "_wl_scan_phase", "idle")
        if phase == "waiting_stop":
            if getattr(self, "_pending_state", "") in (
                    "wl_wifi_scan", "wl_ble_scan"):
                self._pending_cmd = None
            self._wl_scan_phase = "stopping"
            self._wl_stop_outcome = "idle"
            return
        if phase in ("scanning_wifi", "scanning_ble"):
            self._wl_scan_phase = "stopping"
            self._wl_stop_outcome = "idle"
            self._send("stop")

    def _wl_stop_ack(self):
        if getattr(self, "_wl_scan_phase", "idle") != "stopping":
            return
        self._wl_scan_deadline = 0.0
        self._wl_scan_phase = (
            "error"
            if getattr(self, "_wl_stop_outcome", "idle") == "error"
            else "idle")

    def _wl_tick_scan(self):
        phase = getattr(self, "_wl_scan_phase", "idle")
        deadline = getattr(self, "_wl_scan_deadline", 0.0)
        if (phase in ("scanning_wifi", "scanning_ble") and deadline
                and time.monotonic() >= deadline):
            self._wl_scan_fail("Timed out", stop=True, partial=True)

    def _wl_handle_scan_status(self, original, lowered):
        phase = getattr(self, "_wl_scan_phase", "idle")
        if phase == "scanning_wifi" and "scan results printed" in lowered:
            self._wl_finish_scan()
            return
        if phase == "scanning_ble" and "summary:" in lowered:
            self._wl_finish_scan()
            return
        if phase not in ("scanning_wifi", "scanning_ble"):
            return
        failures = (
            "scan already in progress",
            "wardrive is active",
            "failed to start scan",
            "scan start failed",
            "ble scan start failed",
            "operation rejected",
        )
        if any(text in lowered for text in failures):
            self._wl_scan_fail(
                original[:90], stop=True,
                partial=bool(self._wl_scan_results))

    # ------------------------------------------------------------------
    # PipBoy Watch screen
    # ------------------------------------------------------------------
    def _update_watch_screen(self):
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._watch_screen = False
            self._esc_consumed_frame = pyxel.frame_count
            return

        # PIN entry mode
        if self._watch.pin_requested:
            for k in range(10):
                if pyxel.btnp(pyxel.KEY_0 + k):
                    self._watch_pin_input += str(k)
            if pyxel.btnp(pyxel.KEY_BACKSPACE) and self._watch_pin_input:
                self._watch_pin_input = self._watch_pin_input[:-1]
            if pyxel.btnp(pyxel.KEY_RETURN) and len(self._watch_pin_input) == 6:
                self._watch.provide_pin(int(self._watch_pin_input))
                self._watch_pin_input = ""
            return

        # Scanning — pick device or rescan
        if not self._watch.connected:
            results = self._watch.scan_results
            if pyxel.btnp(pyxel.KEY_R) and not self._watch.scanning:
                self._watch.scan()
                self._watch_scan_sel = 0
            if pyxel.btnp(pyxel.KEY_UP) and self._watch_scan_sel > 0:
                self._watch_scan_sel -= 1
            if pyxel.btnp(pyxel.KEY_DOWN) and results:
                self._watch_scan_sel = min(
                    self._watch_scan_sel + 1, len(results) - 1)
            if pyxel.btnp(pyxel.KEY_RETURN) and results:
                sel = results[min(self._watch_scan_sel, len(results) - 1)]
                self._watch.connect(sel["address"])
            return

        # Connected — watch command menu
        if self._watch.connected:
            cmds = self._watch_menu_items()
            if pyxel.btnp(pyxel.KEY_UP) and self._watch_menu_sel > 0:
                self._watch_menu_sel -= 1
            if pyxel.btnp(pyxel.KEY_DOWN):
                self._watch_menu_sel = min(
                    self._watch_menu_sel + 1, len(cmds) - 1)
            if pyxel.btnp(pyxel.KEY_RETURN) and cmds:
                cmd_key = cmds[min(self._watch_menu_sel, len(cmds) - 1)][0]
                self._exec_watch_cmd(cmd_key)

    def _watch_menu_items(self) -> list:
        """Return list of (cmd_key, label) for connected watch."""
        items = [
            ("status",        "Request Status"),
            ("compass",       "Compass Heading"),
            ("haptic",        "Haptic Buzz"),
            ("recon_wifi",    "WiFi Recon (auto results)"),
            ("recon_ble",     "BLE Recon (auto results)"),
            ("recon_deauth",  "Deauth Target"),
            ("deauth_all",    "Blackout (all channels)"),
            ("evil_twin",     "Evil Twin"),
            ("evil_twin_stop", "Evil Twin Stop"),
            ("sniffer_start", "Sniffer Start"),
            ("sniffer_stop",  "Sniffer Stop"),
            ("deauth_detect", "Deauth Detector (TSCM)"),
            ("recon_stop",    "Stop All Recon"),
            ("nfc_scan",      "NFC Scan"),
            ("nfc_stop",      "NFC Stop"),
            ("nfc_save",      "NFC Save Tag"),
            ("nfc_list",      "NFC List Tags"),
            ("nfc_export",    "NFC Download All"),
            ("lora_start",    "LoRa Start"),
            ("lora_stop",     "LoRa Stop"),
            ("lora_advert",   "LoRa Advertise"),
            ("gps_on",        "GPS ON"),
            ("gps_off",       "GPS OFF"),
            ("disconnect",    "Disconnect"),
            ("forget",        "Forget Watch (unpair)"),
        ]
        return items

    def _exec_watch_cmd(self, cmd_key: str):
        if cmd_key == "disconnect":
            self._watch.disconnect()
            self._watch_screen = False
            self.msg("[Watch] Disconnected", C_WARNING)
            return
        if cmd_key == "forget":
            self._watch.forget()
            self._watch_screen = False
            self.msg("[Watch] Unpaired — bonding removed", C_WARNING)
            self._term_add("[Watch] Bonding removed, will need re-pair", raw=True)
            return
        if cmd_key == "nfc_export":
            for i, tag in enumerate(self._watch_nfc_tags):
                self._watch.send_command("nfc_download", {"idx": i})
            self.msg(f"[NFC] Downloading {len(self._watch_nfc_tags)} tags",
                     C_HACK_CYAN)
            return
        if cmd_key == "sniffer_start":
            # Channel hopping by default
            self._watch.send_command("sniffer_start", {"ch": 0})
            self._watch_log_add("[Watch] Sniffer started (ch hopping)", C_HACK_CYAN)
            return
        if cmd_key == "evil_twin":
            # Use last scanned WiFi results if available
            # For now send without params — firmware uses default
            self._watch.send_command("evil_twin",
                                     {"ssid": "FreeWiFi", "ch": 6})
            self._watch_log_add("[Watch] Evil Twin started", C_WARNING)
            return
        if cmd_key == "deauth_all":
            self._watch.send_command("deauth_all")
            self._watch_log_add("[Watch] Blackout started — all channels", C_ERROR)
            return
        if cmd_key == "deauth_detect":
            self._watch.send_command("deauth_detect")
            self._watch_log_add("[Watch] Deauth detector active (TSCM)", C_HACK_CYAN)
            return
        self._watch.send_command(cmd_key)
        self._watch_log_add(f"[Watch] {cmd_key}", C_HACK_CYAN)

    def _draw_watch_screen(self):
        W, H = 640, 360
        # Background
        pyxel.rect(0, 0, W, H, 0)

        # Title bar — 14px tall for 5x8 font + padding
        BAR_H = 14
        if self._watch.connected:
            title = f"PIPBOY WATCH — {self._watch.device_name}"
            bat = self._watch.battery
            pyxel.rect(0, 0, W, BAR_H, 1)
            pyxel.text(4, 4, title, C_SUCCESS)
            if bat > 0:
                bc = C_SUCCESS if bat > 25 else (
                    C_WARNING if bat > 10 else C_ERROR)
                pyxel.text(W - 60, 4, f"BAT:{bat}%", bc)
        else:
            pyxel.rect(0, 0, W, BAR_H, 1)
            pyxel.text(4, 4, "PIPBOY WATCH — SCAN", C_HACK_CYAN)

        # PIN entry overlay — watch firmware since v0.4 requires MITM-
        # authenticated encryption on NUS. First connect displays a 6-digit
        # PIN on the watch screen; user types it here, bond is stored and
        # subsequent connects are silent.
        if self._watch.pin_requested:
            cy = H // 2
            pyxel.rect(W // 2 - 160, cy - 56, 320, 112, 1)
            pyxel.rectb(W // 2 - 160, cy - 56, 320, 112, C_WARNING)
            pyxel.text(W // 2 - 60, cy - 44, "PAIRING PIPBOY WATCH", C_WARNING)
            pyxel.text(W // 2 - 140, cy - 28,
                       "Look at the watch — it shows a 6-digit PIN.", C_TEXT)
            pyxel.text(W // 2 - 140, cy - 16,
                       "Type it here to complete pairing.", C_DIM)
            # PIN digits — centred, Spleen 5x8 so 6 chars * 5 = 30px wide
            pin_disp = self._watch_pin_input + "_" * (
                6 - len(self._watch_pin_input))
            pyxel.text(W // 2 - 18, cy + 4, pin_disp, C_HACK_CYAN)
            pyxel.text(W // 2 - 90, cy + 28,
                       "[0-9] Type   [BKSP] Erase   [ENTER] Confirm", C_DIM)
            return

        # Scanning / device list
        if not self._watch.connected:
            y = BAR_H + 6
            if self._watch.scanning:
                dots = "." * ((pyxel.frame_count // 10) % 4)
                pyxel.text(4, y, f"Scanning for PipBoy devices{dots}",
                           C_HACK_CYAN)
                y += 16

            results = self._watch.scan_results
            if results:
                pyxel.text(4, y, "PipBoy devices found:", C_SUCCESS)
                y += 14
                for i, dev in enumerate(results):
                    sel = i == self._watch_scan_sel
                    c = C_TEXT if sel else C_DIM
                    prefix = "\x10" if sel else " "
                    pyxel.text(8, y,
                               f"{prefix} {dev['name']}  {dev['address']}  "
                               f"RSSI:{dev['rssi']}", c)
                    y += 12

            if not self._watch.scanning and not results:
                pyxel.text(4, y, "No PipBoy devices found", C_WARNING)
                y += 16

            # Scan log (show what BLE found)
            y += 8
            pyxel.text(4, y, "-- BLE Scan Log --", C_DIM)
            y += 12
            # Pull recent watch log lines from terminal
            with self._term_lock:
                watch_lines = [l for l in self.terminal_lines[-30:]
                               if "[Watch]" in l]
            for line in watch_lines[-10:]:
                pyxel.text(8, y, line[:100], C_DIM)
                y += 10

            # Checklist
            cx = 340
            cy = BAR_H + 6
            pyxel.text(cx, cy, "-- Pairing Checklist --", C_HACK_CYAN)
            cy += 16
            checks = [
                "1. Watch BLE enabled (WiFi app > BLE ON)",
                "2. Watch advertising as PipBoy-xxxxx",
                "3. uConsole Bluetooth ON (bluetoothctl power on)",
                "4. Watch in range (<10m)",
                "5. No other device connected to watch",
            ]
            for check in checks:
                pyxel.text(cx, cy, check, C_DIM)
                cy += 12

            pyxel.text(4, H - 14,
                       "[R] Rescan  [ENTER] Connect  [ESC] Back", C_DIM)
            return

        # Connected — command menu + live log
        if self._watch.connected:
            # Left panel: commands
            cmds = self._watch_menu_items()
            pyxel.text(4, 16, "-- Commands --", C_HACK_CYAN)
            y = 28
            for i, (key, label) in enumerate(cmds):
                sel = i == self._watch_menu_sel
                c = C_TEXT if sel else C_DIM
                prefix = "\x10" if sel else " "
                pyxel.text(4, y, f"{prefix} {label}", c)
                y += 10
                if y > H - 20:
                    break

            # Right panel: live log
            lx = 220
            pyxel.line(lx - 4, 14, lx - 4, H - 16, 1)
            pyxel.text(lx, 16, "-- Watch Log --", C_HACK_CYAN)
            log_y = 28
            max_lines = (H - 44) // 8
            visible = self._watch_log[-max_lines:]
            for text, color in visible:
                pyxel.text(lx, log_y, text[:52], color)
                log_y += 8

            pyxel.text(4, H - 12, "[ENTER] Execute  [ESC] Back", C_DIM)

    def _update_wl_screen(self):
        self._wl_tick_scan()
        if self._wl_add_step == "type":
            if pyxel.btnp(pyxel.KEY_1) or pyxel.btnp(pyxel.KEY_W):
                self._wl_add_type = "wifi"
                self._wl_add_step = ""
                self.input_mode = True
                self.input_fields = [{"label": "BSSID/MAC", "value": ""}]
                self.input_field_idx = 0
                self._input_pending_cat = -3
                self._input_pending_item = -1
            elif pyxel.btnp(pyxel.KEY_2) or pyxel.btnp(pyxel.KEY_B):
                self._wl_add_type = "ble"
                self._wl_add_step = ""
                self.input_mode = True
                self.input_fields = [{"label": "MAC", "value": ""}]
                self.input_field_idx = 0
                self._input_pending_cat = -3
                self._input_pending_item = -1
            elif pyxel.btnp(pyxel.KEY_S):
                self._wl_refresh_scan_list()
                self._wl_scan_sel = 0
                self._wl_add_step = "scan_select"
            elif pyxel.btnp(pyxel.KEY_ESCAPE):
                self._wl_add_step = ""
            return

        if self._wl_add_step == "scan_select":
            items = self._wl_scan_list
            if pyxel.btnp(pyxel.KEY_UP) and items:
                self._wl_scan_sel = (self._wl_scan_sel - 1) % len(items)
            elif pyxel.btnp(pyxel.KEY_DOWN) and items:
                self._wl_scan_sel = (self._wl_scan_sel + 1) % len(items)
            elif pyxel.btnp(pyxel.KEY_RETURN) and items:
                it = items[self._wl_scan_sel]
                ok = self._whitelist.add(it["type"], it["mac"], it["name"])
                if ok:
                    self._term_add(
                        f"[WL] Added: {it['name']} ({it['mac'][-8:]})",
                        raw=True)
                    self.msg("[WL] Entry added", C_SUCCESS)
                    self._wl_refresh_scan_list()
                else:
                    self.msg("[WL] MAC already exists", C_WARNING)
            elif pyxel.btnp(pyxel.KEY_W) and not self._wl_scan_active():
                self._wl_start_scan("wifi")
            elif pyxel.btnp(pyxel.KEY_B) and not self._wl_scan_active():
                self._wl_start_scan("ble")
            elif pyxel.btnp(pyxel.KEY_R):
                self._wl_refresh_scan_list()
                self.msg(f"[WL] {len(self._wl_scan_list)} devices",
                         C_HACK_CYAN)
            elif pyxel.btnp(pyxel.KEY_ESCAPE):
                self._wl_cancel_scan()
                self._wl_add_step = ""
            return

        # Main list view
        entries = self._whitelist.entries
        if pyxel.btnp(pyxel.KEY_UP) and entries:
            self._wl_sel = (self._wl_sel - 1) % len(entries)
        if pyxel.btnp(pyxel.KEY_DOWN) and entries:
            self._wl_sel = (self._wl_sel + 1) % len(entries)
        if pyxel.btnp(pyxel.KEY_S):
            # Direct shortcut: scan & select from main whitelist screen
            self._wl_refresh_scan_list()
            self._wl_scan_sel = 0
            self._wl_add_step = "scan_select"
            return
        if pyxel.btnp(pyxel.KEY_A):
            self._wl_add_step = "type"
            return
        if pyxel.btnp(pyxel.KEY_D) and entries:
            e = entries[self._wl_sel]
            self._whitelist.remove(self._wl_sel)
            self._term_add(f"[WL] Removed: {e.name} ({e.mac[-8:]})", raw=True)
            self.msg("[WL] Entry removed", C_WARNING)
            if self._wl_sel >= self._whitelist.count:
                self._wl_sel = max(0, self._whitelist.count - 1)
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._wl_cancel_scan()
            self._wl_screen = False
            self._esc_consumed_frame = pyxel.frame_count

    def _draw_wl_screen(self):
        pyxel.cls(0)
        # Title bar
        pyxel.rect(0, 0, W, 12, 1)
        pyxel.text(4, 3, f"WHITELIST  [{self._whitelist.count} entries]",
                   C_HACK_CYAN)

        if self._wl_add_step == "type":
            # Type selection — manual or scan
            n_dev = len(self._wl_scan_list)
            pyxel.text(20, 36, "Add device to whitelist:", C_TEXT)
            pyxel.text(20, 54, "[1] WiFi  (manual BSSID + SSID)", C_SUCCESS)
            pyxel.text(20, 66, "[2] BLE   (manual MAC + Name)", C_HACK_CYAN)
            pyxel.text(20, 86, f"[S] Scan & Select  ({n_dev} devices nearby)",
                       C_WARNING if n_dev else C_DIM)
            pyxel.text(20, 106, "[ESC] Cancel", C_DIM)
            return

        if self._wl_add_step == "scan_select":
            self._draw_wl_scan_select()
            return

        entries = self._whitelist.entries
        if not entries:
            pyxel.text(20, 50, "No entries.", C_DIM)
            pyxel.text(20, 65, "Press [A] to add a device.", C_DIM)
        else:
            ROW_H = 12
            y = 20
            max_vis = (H - 44) // ROW_H
            start = max(0, self._wl_sel - max_vis + 1)
            for i in range(start, min(len(entries), start + max_vis)):
                e = entries[i]
                sel = (i == self._wl_sel)
                if sel:
                    pyxel.rect(2, y - 1, W - 4, ROW_H, 1)
                tag = "WiFi" if e.type == "wifi" else " BLE"
                tc = C_SUCCESS if e.type == "wifi" else C_HACK_CYAN
                lbl = f"[{tag}] {e.mac}  {e.name[:24]}"
                pyxel.text(6, y + 2, lbl, tc if not sel else 7)
                pyxel.text(W - 80, y + 2, e.added_date[:10], C_DIM)
                y += ROW_H

        # Bottom bar
        pyxel.rect(0, H - 14, W, 14, 1)
        pyxel.text(4, H - 10,
                   "[A]Add manual  [S]Scan & Select  [D]Delete  [ESC]Back",
                   C_DIM)

    def _draw_wl_scan_select(self):
        """Draw the scan-and-select device picker for whitelist."""
        items = self._wl_scan_list
        # Sub-title + scan status
        phase = getattr(self, "_wl_scan_phase", "idle")
        scanning = phase in (
            "waiting_stop", "scanning_wifi", "scanning_ble", "stopping")
        title = f"Select device to whitelist  ({len(items)} found)"
        pyxel.text(4, 18, title, C_TEXT)
        if scanning and pyxel.frame_count % 30 < 20:
            status = {
                "waiting_stop": "Preparing scan...",
                "scanning_wifi": "WiFi scanning...",
                "scanning_ble": "BLE scanning...",
                "stopping": "Stopping scan...",
            }[phase]
            pyxel.text(4 + len(title) * 5 + 8, 18, status, C_WARNING)
        elif phase == "complete":
            pyxel.text(4 + len(title) * 5 + 8, 18, "cached", C_DIM)
        elif phase == "error" and self._wl_scan_error:
            pyxel.text(4, 28, self._wl_scan_error[:80], C_WARNING)

        if not items:
            pyxel.text(20, 50, "No devices discovered yet.", C_DIM)
            pyxel.text(20, 72, "Press [W] to scan WiFi", C_SUCCESS)
            pyxel.text(20, 86, "Press [B] to scan BLE", C_HACK_CYAN)
            pyxel.text(20, 104, "List auto-refreshes after scan.", C_DIM)
            pyxel.rect(0, H - 14, W, 14, 1)
            pyxel.text(4, H - 10,
                       "[W]WiFi scan  [B]BLE scan  [R]Refresh  [ESC]Back",
                       C_DIM)
            return

        # Column headers
        hdr_y = 32
        pyxel.text(6, hdr_y, "Type", C_DIM)
        pyxel.text(44, hdr_y, "Name/SSID", C_DIM)
        pyxel.text(240, hdr_y, "MAC", C_DIM)
        pyxel.text(380, hdr_y, "RSSI", C_DIM)
        pyxel.text(420, hdr_y, "Info", C_DIM)

        # Rows
        row_h = 12
        max_vis = (H - 58) // row_h
        start = max(0, self._wl_scan_sel - max_vis + 1)
        y = hdr_y + 12
        for i in range(start, min(len(items), start + max_vis)):
            it = items[i]
            sel = (i == self._wl_scan_sel)
            if sel:
                pyxel.rect(2, y - 1, W - 4, row_h, 1)

            is_wifi = it["type"] == "wifi"
            tag = "WiFi" if is_wifi else " BLE"
            tc = C_SUCCESS if is_wifi else C_HACK_CYAN
            c = 7 if sel else tc

            pyxel.text(6, y + 2, f"[{tag}]", tc)
            pyxel.text(44, y + 2, (it["name"] or "???")[:32], c)
            pyxel.text(240, y + 2, it["mac"], c)
            pyxel.text(380, y + 2, str(it["rssi"]), C_DIM)
            pyxel.text(420, y + 2, it["extra"][:12], C_DIM)
            y += row_h

        # Scroll indicator
        if len(items) > max_vis:
            bar_h = max(4, (H - 58) * max_vis // len(items))
            bar_y = 44 + (H - 58 - bar_h) * start // max(1, len(items) - max_vis)
            pyxel.rect(W - 3, bar_y, 2, bar_h, C_DIM)

        # Bottom bar
        pyxel.rect(0, H - 14, W, 14, 1)
        pyxel.text(4, H - 10,
                   "[ENTER]Add [W]WiFi [B]BLE [R]Refresh [ESC]Back", C_DIM)

    # ------------------------------------------------------------------
    # Mesh messenger screen
    # ------------------------------------------------------------------

    def _mesh_protocol(self):
        if hasattr(self, "wardrive"):
            return self.wardrive.settings.get("lora_protocol", "meshcore")
        return "meshcore"

    def _mesh_nodes(self):
        protocol = self._mesh_protocol()
        return [node for node in self._mc_nodes
                if node.get("protocol", "meshcore") == protocol]

    def _mesh_channels(self):
        if self._mesh_protocol() == "meshtastic":
            return self._meshtastic.channels
        return self._mc_channels_list

    @staticmethod
    def _mesh_channel_name(channel):
        if isinstance(channel, dict):
            return channel.get("name") or f"Channel {channel.get('index', 0)}"
        return getattr(channel, "name", "?")

    def _mesh_local_name(self):
        if self._mesh_protocol() == "meshtastic":
            return self._meshtastic.local_name
        return self._mc_node_name

    def _mesh_active_channel_index(self):
        """Translate the visible channel row to its protocol channel index."""
        channels = self._mesh_channels()
        if not channels:
            return 0
        self._mc_active_ch = max(0, min(self._mc_active_ch,
                                        len(channels) - 1))
        channel = channels[self._mc_active_ch]
        if isinstance(channel, dict):
            return int(channel.get("index", self._mc_active_ch))
        return self._mc_active_ch

    def _update_mc_screen(self):
        protocol = self._mesh_protocol()
        # Ctrl+H toggles contacts panel (always, even when panel is open)
        ctrl = pyxel.btn(pyxel.KEY_LCTRL) or pyxel.btn(pyxel.KEY_RCTRL)
        if ctrl and pyxel.btnp(pyxel.KEY_H):
            self._mc_nodes_panel = not self._mc_nodes_panel
            self._mc_node_action = False
            self._mc_note_editing = False
            return

        # Overlay inputs take priority over everything
        # The panel remains modal even with no contacts, so its empty state
        # can be dismissed without accidentally closing Messenger.
        if self._mc_nodes_panel:
            self._update_mc_nodes_panel()
            return

        # ESC — exit DM mode or hide chat
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            if self._mc_dm_target:
                self._mc_dm_target = None
                self._mc_log.append(
                    ("\x10 DM mode off — back to channel", C_HACK_CYAN))
                return
            self._mc_screen = False
            self._esc_consumed_frame = pyxel.frame_count
            return

        # Scroll
        if pyxel.btnp(pyxel.KEY_PAGEUP):
            self._mc_scroll = min(self._mc_scroll + 5,
                                  max(0, len(self._mc_log) - 5))
        if pyxel.btnp(pyxel.KEY_PAGEDOWN):
            self._mc_scroll = max(0, self._mc_scroll - 5)

        # Channel picker overlay input
        if self._mc_chan_picker:
            channels = self._mesh_channels()
            if pyxel.btnp(pyxel.KEY_UP):
                self._mc_chan_sel = max(0, self._mc_chan_sel - 1)
            elif pyxel.btnp(pyxel.KEY_DOWN):
                self._mc_chan_sel = min(len(channels) - 1,
                                        self._mc_chan_sel + 1)
            elif pyxel.btnp(pyxel.KEY_RETURN):
                self._mc_active_ch = self._mc_chan_sel
                if protocol == "meshcore":
                    self._lora.set_mc_active_channel(self._mc_active_ch)
                ch = channels[self._mc_active_ch]
                self._mc_log.append((
                    f"Channel: {self._mesh_channel_name(ch)}", C_HACK_CYAN))
                self._mc_chan_picker = False
            elif pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mc_chan_picker = False
            return

        # ENTER — send message (DM or channel)
        if pyxel.btnp(pyxel.KEY_RETURN):
            text = self._mc_input.strip()
            if text:
                ts = time.strftime("%H:%M")
                if self._mc_dm_target:
                    if protocol == "meshtastic":
                        target = (self._mc_dm_target.get("address")
                                  or self._mc_dm_target.get("id", "").removeprefix(
                                      "meshtastic:"))
                        sent = self._meshtastic.send_text(
                            text, target, self._mesh_active_channel_index())
                        tgt = self._mc_dm_target["name"]
                        self._mc_log.append((
                            f"\x11 [DM\u2192{tgt}] {text}",
                            C_WARNING if sent else C_ERROR,
                            ts if sent else f"{ts} NOT SENT"))
                    else:
                        # Encrypted MeshCore DM
                        pk_hex = self._mc_dm_target.get("pubkey", "")
                        if not pk_hex:
                            self._mc_log.append(
                                ("\x10 No pubkey — can't send DM", C_WARNING))
                            self._mc_input = ""
                            return
                        dest_pub = bytes.fromhex(pk_hex)
                        dedup_key = self._lora.send_meshcore_dm(
                            text, self._mc_node_name, dest_pub)
                        tgt = self._mc_dm_target["name"]
                        idx = len(self._mc_log)
                        self._mc_log.append(
                            (f"\x11 [DM\u2192{tgt}] {text}", 12, ts))
                        # Map pending ACK hashes to log index for √
                        for ah in list(self._lora._pending_dm_acks):
                            self._mc_dm_ack_map[ah] = idx
                        if dedup_key:
                            self._mc_tx_pending[dedup_key] = idx
                else:
                    if protocol == "meshtastic":
                        sent = self._meshtastic.send_text(
                            text, channel=self._mesh_active_channel_index())
                        self._mc_log.append((
                            f"\x11 {self._mesh_local_name()}: {text}",
                            C_WARNING if sent else C_ERROR,
                            ts if sent else f"{ts} NOT SENT"))
                    # MeshCore channel message — AIO LoRa or watch fallback
                    elif self._lora.running and self._lora.mode == "meshcore":
                        dedup_key = self._lora.send_meshcore_message(
                            text, self._mc_node_name)
                        idx = len(self._mc_log)
                        self._mc_log.append(
                            (f"\x11 {self._mc_node_name}: {text}",
                             C_WARNING, ts))
                        if dedup_key:
                            self._mc_tx_pending[dedup_key] = idx
                    elif self._watch.connected:
                        # Send via watch LoRa
                        self._watch.send_command("lora_send",
                                                 {"text": text})
                        self._mc_log.append(
                            (f"\x11 {self._mc_node_name}: {text}",
                             C_WARNING, f"{ts} W"))
                self._mc_input = ""
                self._mc_scroll = 0
            return

        # Backspace
        if pyxel.btnp(pyxel.KEY_BACKSPACE):
            self._mc_input = self._mc_input[:-1]
            return

        # Shortcut keys (Ctrl+key — always available, don't block typing)
        ctrl = pyxel.btn(pyxel.KEY_LCTRL) or pyxel.btn(pyxel.KEY_RCTRL)
        if ctrl:
            if pyxel.btnp(pyxel.KEY_A):
                if protocol == "meshtastic":
                    queued = self._meshtastic.request_discovery()
                    self._mc_log.append((
                        "\x11 Zero-hop NodeInfo discovery queued"
                        if queued else "\x10 Meshtastic is not connected",
                        C_HACK_CYAN if queued else C_WARNING))
                else:
                    lat = self.player_lat if self.gps_fix else 0.0
                    lon = self.player_lon if self.gps_fix else 0.0
                    self._lora.send_meshcore_advert(
                        self._mc_node_name, lat, lon)
                    self._mc_log.append((
                        f"\x11 Advert: {self._mc_node_name}", C_HACK_CYAN))
                return
            if pyxel.btnp(pyxel.KEY_N):
                if protocol == "meshtastic":
                    self._mc_log.append((
                        "\x10 Change the node name in Meshtastic settings",
                        C_WARNING))
                    return
                # Name change via input dialog
                self.input_mode = True
                self.input_fields = [{"label": "Node Name", "value":
                                      self._mc_node_name}]
                self.input_field_idx = 0
                self._input_pending_cat = -5
                self._input_pending_item = -1
                return
            if pyxel.btnp(pyxel.KEY_X):
                self._mc_log.clear()
                self._mc_scroll = 0
                return
            if pyxel.btnp(pyxel.KEY_C):
                self._mc_chan_picker = not self._mc_chan_picker
                self._mc_chan_sel = self._mc_active_ch
                return
            return  # Ctrl held — don't pass to typing

        # Channel quick-switch with [ and ]
        if pyxel.btnp(pyxel.KEY_LEFTBRACKET):
            channels = self._mesh_channels()
            if channels:
                self._mc_active_ch = (self._mc_active_ch - 1) % len(channels)
                if protocol == "meshcore":
                    self._lora.set_mc_active_channel(self._mc_active_ch)
                ch = channels[self._mc_active_ch]
                self._mc_log.append((
                    f"Channel: {self._mesh_channel_name(ch)}", C_HACK_CYAN))
            return
        if pyxel.btnp(pyxel.KEY_RIGHTBRACKET):
            channels = self._mesh_channels()
            if channels:
                self._mc_active_ch = (self._mc_active_ch + 1) % len(channels)
                if protocol == "meshcore":
                    self._lora.set_mc_active_channel(self._mc_active_ch)
                ch = channels[self._mc_active_ch]
                self._mc_log.append((
                    f"Channel: {self._mesh_channel_name(ch)}", C_HACK_CYAN))
            return

        # Typing
        c = self._get_char_input()
        if c and len(self._mc_input) < 120:
            self._mc_input += c

    def _update_mc_nodes_panel(self):
        """Handle input for the contacts panel overlay."""
        nodes = self._mesh_nodes()
        if not nodes:
            self._mc_node_action = False
            self._mc_note_editing = False
            if pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mc_nodes_panel = False
            return

        # Note editing mode
        if self._mc_note_editing:
            if pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mc_note_editing = False
            elif pyxel.btnp(pyxel.KEY_RETURN):
                nd = nodes[self._mc_node_sel]
                nd["note"] = self._mc_note_buf
                if self.loot:
                    self.loot.save_contact_note(nd["id"], self._mc_note_buf)
                self._mc_note_editing = False
            elif pyxel.btnp(pyxel.KEY_BACKSPACE):
                self._mc_note_buf = self._mc_note_buf[:-1]
            else:
                c = self._get_char_input()
                if c and len(self._mc_note_buf) < 40:
                    self._mc_note_buf += c
            return

        # Action menu
        if self._mc_node_action:
            _actions = ["DM", "Note", "Info", "Delete", "Close"]
            if pyxel.btnp(pyxel.KEY_UP):
                self._mc_node_action_sel = max(0, self._mc_node_action_sel - 1)
            elif pyxel.btnp(pyxel.KEY_DOWN):
                self._mc_node_action_sel = min(
                    len(_actions) - 1, self._mc_node_action_sel + 1)
            elif pyxel.btnp(pyxel.KEY_ESCAPE):
                self._mc_node_action = False
            elif pyxel.btnp(pyxel.KEY_RETURN):
                nd = nodes[self._mc_node_sel]
                act = _actions[self._mc_node_action_sel]
                if act == "DM":
                    if self._mesh_protocol() == "meshtastic":
                        self._mc_dm_target = nd
                        self._mc_input = ""
                        self._mc_log.append(
                            (f"\x10 DM mode: {nd['name']} "
                             f"(ESC to exit)", 12))
                    else:
                        ntype = nd.get("type", 0)
                        # Repeaters/Room/Sensor don't process MeshCore DMs.
                        ntype_str = str(ntype)
                        is_client = ntype in (0, 1, "Client")
                        if not nd.get("pubkey"):
                            self._mc_log.append(
                                (f"\x10 No pubkey for {nd['name']} "
                                 f"— need advert first", C_WARNING))
                        elif not is_client:
                            self._mc_log.append(
                                (f"\x10 {nd['name']} is {ntype_str} "
                                 f"— DM only works with Clients",
                                 C_WARNING))
                        else:
                            self._mc_dm_target = nd
                            self._mc_input = ""
                            self._mc_log.append(
                                (f"\x10 DM mode: {nd['name']} "
                                 f"(ESC to exit)", 12))
                    self._mc_node_action = False
                    self._mc_nodes_panel = False
                elif act == "Note":
                    self._mc_note_buf = nd.get("note", "")
                    self._mc_note_editing = True
                    self._mc_node_action = False
                elif act == "Info":
                    fs = nd.get("first_seen", 0)
                    fs_str = time.strftime("%m-%d %H:%M",
                                           time.localtime(fs)) if fs else "?"
                    note = nd.get("note", "")
                    self._mc_log.append(
                        (f"\x10 [{nd['name']}] id:{nd['id']} "
                         f"type:{nd.get('type','?')} "
                         f"first:{fs_str}", C_HACK_CYAN))
                    if note:
                        self._mc_log.append(
                            (f"  Note: {note}", C_DIM))
                    if nd.get("lat") and nd.get("lon"):
                        self._mc_log.append(
                            (f"  GPS: {nd['lat']:.5f}, {nd['lon']:.5f}",
                             C_DIM))
                    self._mc_node_action = False
                elif act == "Delete":
                    name = nd.get("name", "?")
                    nid = nd["id"]
                    self._mc_nodes.remove(nd)
                    if self.loot:
                        self.loot.delete_contact(nid)
                    if self._mesh_protocol() == "meshcore":
                        # Remove from LoRa known pubkeys.
                        self._lora._known_pubkeys.pop(nid, None)
                    self._mc_log.append(
                        (f"\x10 Deleted contact: {name}", C_WARNING))
                    self._mc_node_action = False
                    nodes = self._mesh_nodes()
                    self._mc_node_sel = min(self._mc_node_sel,
                                            max(0, len(nodes) - 1))
                else:
                    self._mc_node_action = False
            return

        # Node list navigation
        if pyxel.btnp(pyxel.KEY_UP):
            self._mc_node_sel = max(0, self._mc_node_sel - 1)
        elif pyxel.btnp(pyxel.KEY_DOWN):
            self._mc_node_sel = min(len(nodes) - 1,
                                    self._mc_node_sel + 1)
        elif pyxel.btnp(pyxel.KEY_RETURN):
            self._mc_node_action = True
            self._mc_node_action_sel = 0
        elif pyxel.btnp(pyxel.KEY_ESCAPE):
            self._mc_nodes_panel = False

    def _draw_mc_screen(self):
        pyxel.cls(0)
        protocol = self._mesh_protocol()
        protocol_name = "Meshtastic" if protocol == "meshtastic" else "MeshCore"
        nodes = self._mesh_nodes()
        channels = self._mesh_channels()
        manager = self._meshtastic if protocol == "meshtastic" else self._lora
        # Title bar — 14px tall for 5x8 font + padding
        BAR_H = 14
        pyxel.rect(0, 0, W, BAR_H, 1)
        status = "ACTIVE" if manager.running else "OFF"
        pkts = getattr(manager, "packets_received", 0)
        ch_name = "?"
        if channels and self._mc_active_ch < len(channels):
            ch_name = self._mesh_channel_name(channels[self._mc_active_ch])
        if self._mc_dm_target:
            dm_name = self._mc_dm_target.get("name", "?")
            pyxel.text(4, 4, f"{protocol_name.upper()} [DM: {dm_name}] "
                       f"[{status}] pkts:{pkts} node:{self._mesh_local_name()}", 12)
        else:
            pyxel.text(4, 4, f"{protocol_name.upper()} [{ch_name}] [{status}] "
                       f"pkts:{pkts} node:{self._mesh_local_name()} "
                       f"nodes:{len(nodes)}", C_HACK_CYAN)

        # Polish character transliteration (BDF/pyxel fonts are ASCII-only)
        _PL = str.maketrans(
            "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
            "acelnoszzACELNOSZZ")

        # Chat log — now uses the global Spleen 5x8 via pyxel.text monkey-patch
        log = self._mc_log
        total = len(log)
        line_h = 10
        char_w = 5
        chat_top = BAR_H + 2
        chat_bot = H - 24
        # Reserve line for char counter when typing
        msg_bot = chat_bot - (line_h if self._mc_input else 0)
        max_vis = (msg_bot - chat_top) // line_h

        if self._mc_scroll == 0:
            start = max(0, total - max_vis)
            end = total
        else:
            end = max(0, total - self._mc_scroll)
            start = max(0, end - max_vis)

        y = chat_top
        for i in range(start, end):
            entry = log[i]
            text, color = entry[0], entry[1]
            tag = entry[2] if len(entry) > 2 else None
            full = text.translate(_PL)
            # Reserve space for actual tag length (right-aligned to edge)
            tag_chars = len(tag) + 1 if tag else 0  # +1 for gap
            first_max = (W - 8) // char_w - tag_chars
            wrap_max = (W - 8) // char_w  # continuation lines use full width
            pyxel.text(4, y, full[:first_max], color)
            if tag:
                tx = W - len(tag) * char_w - 4
                pyxel.text(tx, y, tag, C_DIM)
            y += line_h
            # Wrap remaining text
            pos = first_max
            while pos < len(full) and y < msg_bot:
                pyxel.text(4, y, full[pos:pos + wrap_max], color)
                pos += wrap_max
                y += line_h
            if y >= msg_bot:
                break

        if total == 0:
            pyxel.text(20, chat_top + 40, "No messages yet.", C_DIM)
            pyxel.text(20, chat_top + 54,
                       "Type a message and press ENTER to send.", C_DIM)

        # Scroll indicator
        if self._mc_scroll > 0:
            pyxel.text(W - 120, chat_top, f"SCROLL +{self._mc_scroll}", C_DIM)

        # Input bar
        cursor = "_" if pyxel.frame_count % 30 < 20 else ""
        prefix = "DM> " if self._mc_dm_target else "> "
        bar_bg = 12 if self._mc_dm_target else 1
        bar_fg = 0 if self._mc_dm_target else C_TEXT
        pyxel.rect(0, chat_bot, W, 14, bar_bg)
        visible_w = (W - 8) // char_w - len(prefix)
        inp = self._mc_input
        if len(inp) > visible_w:
            inp = inp[len(inp) - visible_w:]
        pyxel.text(4, chat_bot + 3, f"{prefix}{inp}{cursor}", bar_fg)

        # Character counter above input bar (right side)
        inp_len = len(self._mc_input)
        if inp_len > 0:
            at_max = inp_len >= 120
            cnt_c = C_ERROR if at_max else (
                C_WARNING if inp_len > 100 else C_DIM)
            cnt_txt = f"{inp_len}/120"
            pyxel.text(W - len(cnt_txt) * char_w - 4, chat_bot - line_h,
                       cnt_txt, cnt_c)
            # Flash "MAX" warning when limit reached
            if at_max and pyxel.frame_count % 40 < 25:
                pyxel.text(W - len(cnt_txt) * char_w - 45,
                           chat_bot - line_h, "MAX", C_ERROR)

        # Contacts panel overlay — 12px rows for 5x8 font
        if self._mc_nodes_panel:
            pw = 280
            px_x = W - pw - 2
            panel_top = BAR_H + 2
            panel_bot = chat_bot - 14
            row_h = 12
            max_vis = (panel_bot - 28) // row_h
            pyxel.rect(px_x, panel_top, pw, panel_bot, 0)
            pyxel.rectb(px_x, panel_top, pw, panel_bot, 2)
            pyxel.text(px_x + 4, panel_top + 4,
                       f"CONTACTS ({len(nodes)})", 2)
            pyxel.text(px_x + pw - 90, panel_top + 4,
                       "ENTER=action" if nodes else "ESC=back", C_DIM)
            pyxel.line(px_x + 2, panel_top + 14,
                       px_x + pw - 3, panel_top + 14, 1)
            if not nodes:
                pyxel.text(px_x + 8, panel_top + 30,
                           f"No {protocol_name} nodes heard yet.", C_TEXT)
                pyxel.text(px_x + 8, panel_top + 44,
                           "Waiting for nearby node adverts..." if manager.running
                           else "LoRa is off; enable it to hear nodes.", C_DIM)
                pyxel.text(px_x + 8, panel_top + 64,
                           "ESC or Ctrl+H: return to chat", C_HACK_CYAN)
            _type_icons = {0: "C", 1: "C", 2: "R", 3: "M", 4: "S",
                           "Client": "C", "Repeater": "R",
                           "Room": "M", "Sensor": "S",
                           "Meshtastic": "T"}
            # Clamp selection
            self._mc_node_sel = max(0, min(
                self._mc_node_sel, len(nodes) - 1))
            # Scroll to keep selection visible
            scroll_off = max(0, self._mc_node_sel - max_vis + 1)
            ny = panel_top + 18
            for idx in range(scroll_off,
                             min(len(nodes), scroll_off + max_vis)):
                nd = nodes[idx]
                raw_type = nd.get("type", 0)
                try:
                    raw_type = int(raw_type)
                except (ValueError, TypeError):
                    pass
                icon = _type_icons.get(raw_type, "?")
                name = nd.get("name", "?")[:14]
                rssi = nd.get("rssi", 0)
                snr = nd.get("snr", 0)
                note = nd.get("note", "")
                age = ""
                ls = nd.get("last_seen", 0)
                if ls:
                    elapsed = int(time.time() - ls)
                    if elapsed < 60:
                        age = f"{elapsed}s"
                    elif elapsed < 3600:
                        age = f"{elapsed // 60}m"
                    else:
                        age = f"{elapsed // 3600}h"
                is_rep = icon == "R"
                selected = idx == self._mc_node_sel
                # Row background
                if selected:
                    pyxel.rect(px_x + 2, ny - 1, pw - 4, row_h, 2)
                elif is_rep:
                    pyxel.rect(px_x + 2, ny - 1, pw - 4, row_h, 1)
                c_icon = (0 if selected else
                          C_SUCCESS if is_rep else
                          (2 if icon == "M" else C_TEXT))
                c_name = 0 if selected else (C_SUCCESS if is_rep else C_TEXT)
                c_info = 0 if selected else (C_SUCCESS if is_rep else C_DIM)
                pyxel.text(px_x + 4, ny, icon, c_icon)
                pyxel.text(px_x + 14, ny, name, c_name)
                if note:
                    pyxel.text(px_x + 120, ny, f'"{note[:10]}"', c_info)
                pyxel.text(px_x + 185, ny, f"{rssi:.0f}dB", c_info)
                pyxel.text(px_x + 220, ny, age, c_info)
                ny += row_h

            # Action menu popup
            if self._mc_node_action and self._mc_node_sel < len(nodes):
                _actions = ["DM", "Note", "Info", "Delete", "Close"]
                aw = 80
                ah = len(_actions) * 12 + 8
                ax = px_x + 40
                ay = 40
                pyxel.rect(ax, ay, aw, ah, 0)
                pyxel.rectb(ax, ay, aw, ah, C_WARNING)
                for ai, act in enumerate(_actions):
                    sel = ai == self._mc_node_action_sel
                    if sel:
                        pyxel.rect(ax + 2, ay + 4 + ai * 12, aw - 4, 11,
                                   C_WARNING)
                    pyxel.text(ax + 6, ay + 6 + ai * 12, act,
                               0 if sel else C_TEXT)

            # Note editing overlay
            if self._mc_note_editing:
                nw = 200
                nx = (W - nw) // 2
                ny2 = 80
                pyxel.rect(nx, ny2, nw, 30, 0)
                pyxel.rectb(nx, ny2, nw, 30, C_HACK_CYAN)
                pyxel.text(nx + 4, ny2 + 3, "EDIT NOTE (ENTER=save ESC=cancel)",
                           C_HACK_CYAN)
                cur = "_" if pyxel.frame_count % 30 < 20 else ""
                pyxel.text(nx + 4, ny2 + 16,
                           f"{self._mc_note_buf}{cur}", C_TEXT)

        # Channel picker overlay
        if self._mc_chan_picker:
            pw = 200
            px_x = (W - pw) // 2
            py_y = 40
            ph = len(channels) * 12 + 30
            pyxel.rect(px_x, py_y, pw, ph, 0)
            pyxel.rectb(px_x, py_y, pw, ph, C_HACK_CYAN)
            pyxel.text(px_x + 4, py_y + 3, "SELECT CHANNEL", C_HACK_CYAN)
            pyxel.line(px_x + 2, py_y + 11, px_x + pw - 3, py_y + 11, 1)
            cy = py_y + 15
            for i, ch in enumerate(channels):
                sel = (i == self._mc_chan_sel)
                active = (i == self._mc_active_ch)
                if sel:
                    pyxel.rect(px_x + 2, cy - 1, pw - 4, 11, C_HACK_CYAN)
                c = 0 if sel else C_TEXT
                marker = " <<<" if active else ""
                pyxel.text(px_x + 6, cy + 1,
                           f"{self._mesh_channel_name(ch)}{marker}", c)
                cy += 12
            pyxel.text(px_x + 4, cy + 2, "ENTER=select  ESC=cancel", C_DIM)

        # Bottom hints
        pyxel.rect(0, H - 10, W, 10, 0)
        if protocol == "meshtastic":
            hints = "C-A Discover  C-H Nodes  C-C Chan  [/]Switch  C-X Clear  ESC Back"
        else:
            hints = "C-A Advert  C-N Name  C-H Nodes  C-C Chan  [/]Switch  C-X Clear  ESC Back"
        pyxel.text(4, H - 8, hints, C_DIM)

    # ------------------------------------------------------------------
    # Loot screen search
    # ------------------------------------------------------------------

    def _update_loot_screen(self):
        """Handle input on the loot database screen."""
        # Password history viewer overlay
        if self._loot_pwd_screen:
            if pyxel.btnp(pyxel.KEY_ESCAPE) or pyxel.btnp(pyxel.KEY_D):
                self._loot_pwd_screen = False
            elif pyxel.btnp(pyxel.KEY_UP):
                self._loot_pwd_scroll = max(0, self._loot_pwd_scroll - 1)
            elif pyxel.btnp(pyxel.KEY_DOWN):
                self._loot_pwd_scroll += 1
            return

        if self._loot_search_active:
            # Typing in search field
            if pyxel.btnp(pyxel.KEY_BACKSPACE) and self._loot_search:
                self._loot_search = self._loot_search[:-1]
            elif pyxel.btnp(pyxel.KEY_RETURN):
                self._loot_search_exec()
                self._loot_search_active = False
            else:
                c = self._get_char_input()
                if c and len(self._loot_search) < 28:
                    self._loot_search += c
        elif self._loot_search_results:
            # Scrolling results
            if pyxel.btnp(pyxel.KEY_UP):
                self._loot_search_scroll = max(0, self._loot_search_scroll - 1)
            elif pyxel.btnp(pyxel.KEY_DOWN):
                self._loot_search_scroll += 1
        else:
            # Normal loot screen — activate search or password viewer
            if pyxel.btnp(pyxel.KEY_SLASH) or pyxel.btnp(pyxel.KEY_F):
                self._loot_search_active = True
                self._loot_search = ""
                self._loot_search_results = []
                self._loot_search_scroll = 0
            elif pyxel.btnp(pyxel.KEY_D):
                self._load_all_captures()
                self._loot_pwd_screen = True
                self._loot_pwd_scroll = 0

    def _loot_search_exec(self):
        """Search all loot points (all sessions) by SSID/name or BSSID/MAC."""
        q = self._loot_search.lower().strip()
        if not q:
            self._loot_search_results = []
            return
        results = []
        for pt in self.loot_points:
            label = (pt.get("label") or "").lower()
            bssid = (pt.get("bssid") or "").lower()
            if q in label or q in bssid:
                results.append(pt)
        # Dedup by BSSID/MAC — keep strongest RSSI
        seen: dict[str, dict] = {}
        for r in results:
            key = r.get("bssid") or r.get("label", "")
            if key not in seen:
                seen[key] = r
            else:
                try:
                    if int(r.get("rssi", -99)) > int(seen[key].get("rssi", -99)):
                        seen[key] = r
                except (ValueError, TypeError):
                    pass
        self._loot_search_results = sorted(
            seen.values(), key=lambda x: (x.get("label") or "").lower())
        self._loot_search_scroll = 0

    def _load_all_captures(self):
        """Scan all session dirs for portal_passwords.log & evil_twin_capture.log."""
        from urllib.parse import unquote_plus
        loot_dir = Path(self._app_dir) / "loot"
        entries: list[tuple[str, str]] = []  # (session_date, formatted_line)
        if not loot_dir.is_dir():
            self._loot_pwd_data = []
            return
        for session in sorted(loot_dir.iterdir(), reverse=True):
            if not session.is_dir():
                continue
            name = session.name
            # Validate session dir format: YYYY-MM-DD_HH-MM-SS
            if len(name) < 19 or name[4] != "-" or name[7] != "-":
                continue
            date_tag = name[:10]  # YYYY-MM-DD
            for logfile in ("portal_passwords.log", "evil_twin_capture.log"):
                fpath = session / logfile
                if not fpath.is_file():
                    continue
                tag = "EP" if "portal" in logfile else "ET"
                try:
                    for raw_line in fpath.read_text(
                            encoding="utf-8", errors="replace").splitlines():
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        decoded = unquote_plus(raw_line)
                        fields = self._parse_post_fields(decoded)
                        if fields:
                            entries.append((date_tag, f"[{tag}] {date_tag} {fields}"))
                        # Skip non-credential ET/EP diagnostics (client
                        # connects, portal counts, channel switches) — they
                        # live in the log file for forensics but shouldn't
                        # appear under "Captured Passwords".
                except OSError:
                    pass
        # Sort newest first (already reversed session order)
        self._loot_pwd_data = [e[1] for e in entries]

    def _draw_loot_screen(self):
        pyxel.cls(0)
        ROW_H = 10   # 8px text + 2px gap for 5x8 font
        # Border + header
        pyxel.rectb(1, 1, W - 2, H - 2, C_HACK_CYAN)
        pyxel.rect(2, 2, W - 4, 14, 1)
        pyxel.text(6, 5, "LOOT DATABASE", C_HACK_CYAN)
        pyxel.text(120, 5, "//", C_COAST)
        pyxel.text(134, 5, "ESP32 WATCH DOGS", C_DIM)
        pyxel.text(W - 70, 5, "[`] close", C_DIM)
        pyxel.line(1, 16, W - 2, 16, C_HACK_CYAN)

        t = self._loot_totals
        col1, col2, col3 = 10, 230, 440  # 3 columns

        # ─── COLUMN 1: ALL-TIME TOTALS ───
        y = 22
        pyxel.text(col1, y, "ALL-TIME TOTALS", C_HACK_CYAN)
        pyxel.line(col1, y + 10, col1 + 100, y + 10, 1)
        y += 14
        pyxel.text(col1, y, f"Sessions", C_DIM)
        pyxel.text(col1 + 80, y, f"{t.get('sessions', 0)}", C_TEXT)
        y += ROW_H
        pyxel.text(col1, y, f"WiFi nets", C_DIM)
        pyxel.text(col1 + 80, y, f"{t.get('wifi', 0)}", C_SUCCESS)
        y += ROW_H
        pyxel.text(col1, y, f"BT devices", C_DIM)
        pyxel.text(col1 + 80, y, f"{t.get('bt', 0)}", C_HACK_CYAN)
        y += ROW_H
        pyxel.text(col1, y, f"Handshakes", C_DIM)
        pyxel.text(col1 + 80, y, f"{t.get('pcap', 0)}", C_ERROR)
        y += ROW_H
        pyxel.text(col1, y, f"HC22000", C_DIM)
        pyxel.text(col1 + 80, y, f"{t.get('hs', 0)}", C_ERROR)
        y += ROW_H
        t_pwd_all = t.get('passwords', 0) + t.get('et_captures', 0)
        pyxel.text(col1, y, f"Passwords", C_DIM)
        pyxel.text(col1 + 80, y, f"{t_pwd_all}", 12)  # blue
        y += ROW_H
        pyxel.text(col1, y, f"Mesh Nodes", C_DIM)
        pyxel.text(col1 + 80, y, f"{t.get('mc_nodes', 0)}", C_WARNING)
        y += ROW_H
        pyxel.text(col1, y, f"Mesh Msgs", C_DIM)
        pyxel.text(col1 + 80, y, f"{t.get('mc_msgs', 0)}", C_WARNING)
        y += ROW_H
        n_contacts = len(self.loot.load_contacts()) if self.loot else 0
        pyxel.text(col1, y, f"Contacts", C_DIM)
        pyxel.text(col1 + 80, y, f"{n_contacts}", C_HACK_CYAN)
        y += ROW_H
        pyxel.text(col1, y, f"GPS points", C_DIM)
        pyxel.text(col1 + 80, y, f"{len(self.loot_points)}", C_TEXT)

        # ─── COLUMN 2: THIS SESSION ───
        y = 22
        pyxel.text(col2, y, "THIS SESSION", C_HACK_CYAN)
        pyxel.line(col2, y + 10, col2 + 100, y + 10, 1)
        y += 14
        n_hs_ses = sum(1 for m in self.markers if m.type == "handshake")
        n_pwn = self._current_hacked_count()
        pyxel.text(col2, y, f"WiFi", C_DIM)
        pyxel.text(col2 + 70, y,
                   f"{getattr(self, '_session_wifi_discovered', len(self.wifi_networks))}",
                   C_SUCCESS)
        y += ROW_H
        pyxel.text(col2, y, f"BLE", C_DIM)
        pyxel.text(col2 + 70, y,
                   f"{getattr(self, '_session_ble_discovered', len(self.ble_devices))}",
                   C_HACK_CYAN)
        y += ROW_H
        pyxel.text(col2, y, f"Handshakes", C_DIM)
        pyxel.text(col2 + 70, y, f"{n_hs_ses}", C_ERROR)
        y += ROW_H
        n_pwd_ses = (self.state.submitted_forms
                     + len(self.state.evil_twin_captured_data))
        pyxel.text(col2, y, f"Passwords", C_DIM)
        pyxel.text(col2 + 70, y, f"{n_pwd_ses}", 12)  # blue
        y += ROW_H
        pyxel.text(col2, y, f"Hacked", C_DIM)
        pyxel.text(col2 + 70, y, f"{n_pwn}", C_SUCCESS)
        y += 14
        # XP bar
        lv = self.level_title
        pyxel.text(col2, y, f"LV:{self.level}", C_SUCCESS)
        pyxel.text(col2 + 36, y, lv, C_HACK_CYAN)
        y += ROW_H
        pyxel.text(col2, y, f"XP", C_DIM)
        xw = 60
        nxt = self.xp_for_next_level
        cur = self.xp_in_current_level
        xf = int(xw * cur / nxt) if nxt > 0 else xw
        pyxel.rect(col2 + 24, y, xw, 8, 1)
        pyxel.rect(col2 + 24, y, xf, 8, C_HACK_CYAN)
        pyxel.rectb(col2 + 24, y, xw, 8, C_COAST)
        pyxel.text(col2 + 90, y, f"{self.xp}", C_DIM)
        y += 16
        # Hat profile badge
        hat_name, hat_color = self._get_hat_profile()
        _HAT_DESC = {
            "WHITE": "Ethical recon — scanning & mapping only",
            "BLUE":  "Blue Team — defensive security research",
            "GREY":  "Grey Hat — mixed recon & offensive ops",
            "RED":   "Red Team — active penetration testing",
            "BLACK": "Black Hat — aggressive attack operator",
        }
        pyxel.circ(col2 + 4, y + 4, 4, 7)          # white outline
        pyxel.circ(col2 + 4, y + 4, 3, hat_color)   # hat color
        pyxel.text(col2 + 14, y, f"{hat_name} HAT",
                   hat_color if hat_color != 0 else C_TEXT)
        y += ROW_H
        desc = _HAT_DESC.get(hat_name, "")
        pyxel.text(col2 + 14, y, desc, C_DIM)
        # Badges (3 columns x 2 rows grid) — 5x8 font sizing
        y += 14
        _BADGE_INFO = [
            ("flipper",          "FLIPPER",   C_WARNING),
            ("wardriver",        "WARDRIVER", C_SUCCESS),
            ("meshcore",         "MESHCORE",  2),
            ("handshake_hunter", "HS HUNTER", C_ERROR),
            ("wpasec_uploader",  "WPA-SEC",   12),
            ("evil_twin",        "EVIL TWIN", 8),
            ("skywatch",         "SKYWATCH",  C_HACK_CYAN),
            ("iot_hunter",       "IOT HUNTER", 12),
        ]
        earned = [(b, l, c) for b, l, c in _BADGE_INFO if b in self._badges]
        cols = 3
        cw = 72  # column width
        for i, (badge_id, label, color) in enumerate(earned):
            row = i // cols
            col = i % cols
            bx = col2 + col * cw
            by = y + row * 15
            lw = len(label) * 5 + 6
            pyxel.rectb(bx, by, lw, 12, color)
            pyxel.text(bx + 2, by + 2, label, color)

        # ─── COLUMN 3: STATUS ───
        y = 22
        pyxel.text(col3, y, "STATUS", C_HACK_CYAN)
        pyxel.line(col3, y + 10, col3 + 80, y + 10, 1)
        y += 14
        # ESP32
        pyxel.text(col3, y, "ESP32", C_DIM)
        if self._esp32:
            pyxel.text(col3 + 60, y, "ONLINE", C_SUCCESS)
        else:
            pyxel.text(col3 + 60, y, "OFFLINE", C_ERROR)
        y += ROW_H
        # GPS
        pyxel.text(col3, y, "GPS", C_DIM)
        if self.gps_fix:
            pyxel.text(col3 + 60, y, "FIX", C_SUCCESS)
        elif self.gps.available:
            pyxel.text(col3 + 60, y, f"Vis:{self.gps_sats_vis}", C_WARNING)
        else:
            pyxel.text(col3 + 60, y, "N/A", C_ERROR)
        y += ROW_H
        # AIO modules
        if self._aio_available:
            pyxel.text(col3, y, "LoRa", C_DIM)
            pyxel.text(col3 + 60, y,
                       "ON" if self._lora_enabled else "OFF",
                       C_SUCCESS if self._lora_enabled else C_ERROR)
            y += ROW_H
            pyxel.text(col3, y, "SDR", C_DIM)
            pyxel.text(col3 + 60, y,
                       "ON" if self._sdr_enabled else "OFF",
                       C_SUCCESS if self._sdr_enabled else C_ERROR)
            y += ROW_H
            pyxel.text(col3, y, "USB", C_DIM)
            pyxel.text(col3 + 60, y,
                       "ON" if self._usb_enabled else "OFF",
                       C_SUCCESS if self._usb_enabled else C_ERROR)
            y += ROW_H
        # Active operations
        pyxel.text(col3, y, "Active", C_DIM)
        ops = []
        if self.wifi_scanning: ops.append("WiFi")
        if self.ble_scanning: ops.append("BT")
        if self.sniffing: ops.append("Sniff")
        if self.capturing_hs: ops.append("HS")
        if self.state.portal_running: ops.append("EP")
        if self.state.evil_twin_running: ops.append("ET")
        if self._gps_wait: ops.append("GPS wait")
        pyxel.text(col3 + 60, y, " ".join(ops) if ops else "idle",
                   C_HACK_CYAN if ops else C_DIM)

        # ─── BOTTOM: Last discovered devices ───
        # Divider line
        div_y = 180
        pyxel.line(4, div_y, W - 5, div_y, 1)
        pyxel.text(6, div_y + 4, "RECENT WiFi", C_SUCCESS)
        pyxel.text(W // 2 + 4, div_y + 4, "RECENT BLE", C_HACK_CYAN)
        pyxel.line(4, div_y + 14, W - 5, div_y + 14, 1)
        # Vertical divider
        mid_x = W // 2
        pyxel.line(mid_x, div_y, mid_x, H - 22, 1)

        # WiFi list (last N) — 10px rows for 5x8 font
        LIST_H = 10
        y = div_y + 17
        max_rows = (H - 22 - y) // LIST_H
        recent_wifi = list(reversed(self.wifi_networks[-max_rows:]))
        for n in recent_wifi:
            if y > H - 22:
                break
            rssi_c = n.color
            ssid = n.ssid[:14] or "?"
            pyxel.text(6, y, ssid, C_TEXT)
            pyxel.text(6 + 76, y, f"Ch:{n.channel:>2}", C_DIM)
            pyxel.text(6 + 110, y, f"{n.rssi}dBm", rssi_c)
            if n.hacked:
                pyxel.text(6 + 150, y, "PWN", C_SUCCESS)
            y += LIST_H

        # BLE list (last N)
        y = div_y + 17
        recent_ble = list(reversed(self.ble_devices[-max_rows:]))
        bx = mid_x + 6
        for d in recent_ble:
            if y > H - 22:
                break
            rssi_c = d.color
            dname = d.name[:12] or "?"
            pyxel.text(bx, y, dname, C_TEXT)
            pyxel.text(bx + 66, y, d.mac[-8:], C_DIM)
            pyxel.text(bx + 110, y, f"{d.rssi}dBm", rssi_c)
            if d.hacked:
                pyxel.text(bx + 150, y, "PWN", C_SUCCESS)
            y += LIST_H

        # GPS + controls bar
        y = H - 18
        pyxel.line(1, y - 3, W - 2, y - 3, 1)
        if self.gps_fix:
            lat_c = "N" if self.player_lat >= 0 else "S"
            lon_c = "E" if self.player_lon >= 0 else "W"
            pyxel.text(6, y,
                       f"GPS {abs(self.player_lat):.5f}{lat_c} "
                       f"{abs(self.player_lon):.5f}{lon_c}", C_SUCCESS)
        else:
            pyxel.text(6, y, "GPS: no fix", C_ERROR)
        if not self._loot_search_active and not self._loot_search_results:
            pyxel.text(W - 280, y,
                       "[/] search  [D] passwords  [`] close  [ESC] back  [S] stop",
                       C_DIM)
        else:
            pyxel.text(W - 160, y, "[`] close  [ESC] back  [S] stop", C_DIM)

        # ─── OVERLAYS ───
        if self._loot_pwd_screen:
            self._draw_loot_pwd_viewer()
        elif self._loot_search_active or self._loot_search_results or self._loot_search:
            self._draw_loot_search_overlay()

    def _draw_loot_pwd_viewer(self):
        """Draw overlay showing ALL captured credentials across all sessions."""
        captures = self._loot_pwd_data
        # Solid background
        pyxel.rect(2, 14, W - 4, H - 32, 0)
        # Dialog box
        dw, dh = W - 20, H - 40
        dx = (W - dw) // 2
        dy = 16
        pyxel.rect(dx, dy, dw, dh, 0)
        pyxel.rectb(dx, dy, dw, dh, 12)  # blue border
        pyxel.rectb(dx + 1, dy + 1, dw - 2, dh - 2, C_COAST)
        # Title
        pyxel.text(dx + 4, dy + 3,
                   f"CAPTURED PASSWORDS — All Sessions", 12)
        pyxel.text(dx + dw - 80, dy + 3,
                   f"Total: {len(captures)}", C_DIM)
        pyxel.line(dx + 2, dy + 11, dx + dw - 3, dy + 11, C_COAST)
        # Column headers
        pyxel.text(dx + 6, dy + 14, "TAG", C_DIM)
        pyxel.text(dx + 30, dy + 14, "DATE", C_DIM)
        pyxel.text(dx + 90, dy + 14, "CREDENTIALS", C_DIM)
        pyxel.line(dx + 2, dy + 22, dx + dw - 3, dy + 22, 1)
        # Data
        if not captures:
            pyxel.text(dx + (dw - 160) // 2, dy + dh // 2,
                       "No credentials captured in any session", C_DIM)
        else:
            max_vis = (dh - 55) // 10
            scroll = min(self._loot_pwd_scroll,
                         max(0, len(captures) - max_vis))
            self._loot_pwd_scroll = scroll
            y = dy + 26
            for i in range(scroll, min(len(captures), scroll + max_vis)):
                line = captures[i]
                # Truncate long lines to fit
                max_chars = (dw - 16) // 4  # approx 4px per char
                display = line[:max_chars]
                pyxel.text(dx + 6, y, display, 12)  # blue
                y += 10
            # Scrollbar
            if len(captures) > max_vis:
                bar_total = dh - 55
                bar_h = max(4, bar_total * max_vis // len(captures))
                bar_y = (dy + 26 + (bar_total - bar_h) * scroll
                         // max(1, len(captures) - max_vis))
                pyxel.rect(dx + dw - 4, bar_y, 2, bar_h, 12)
        # Hints
        pyxel.text(dx + 4, dy + dh - 9,
                   "[D/ESC] close  [UP/DOWN] scroll", C_DIM)

    def _draw_loot_search_overlay(self):
        """Draw search field + results over the loot screen."""
        # Solid black background over loot content
        pyxel.rect(2, 14, W - 4, H - 32, 0)

        # Search box
        sx, sy, sw = 20, 20, W - 40
        pyxel.rect(sx, sy, sw, 16, 0)
        pyxel.rectb(sx, sy, sw, 16,
                    C_HACK_CYAN if self._loot_search_active else C_COAST)
        cursor = "_" if self._loot_search_active and pyxel.frame_count % 30 < 20 else ""
        pyxel.text(sx + 4, sy + 5, f"SEARCH: {self._loot_search}{cursor}",
                   C_TEXT if self._loot_search else C_DIM)
        if not self._loot_search and self._loot_search_active:
            pyxel.text(sx + 36, sy + 5, "type SSID, name or MAC...", C_DIM)

        # Results area
        ry = sy + 20
        results = self._loot_search_results

        if not results and self._loot_search and not self._loot_search_active:
            pyxel.text(sx + 4, ry + 10,
                       f'No matches for "{self._loot_search}"', C_WARNING)
            pyxel.text(sx + 4, ry + 22, "[ESC] clear search", C_DIM)
            return

        if not results:
            return

        # Header
        pyxel.text(sx + 4, ry,
                   f"Found: {len(results)} devices", C_HACK_CYAN)
        ry += 10
        # Column headers
        pyxel.text(sx + 4, ry, "Type", C_DIM)
        pyxel.text(sx + 30, ry, "SSID / Name", C_DIM)
        pyxel.text(sx + 170, ry, "BSSID / MAC", C_DIM)
        pyxel.text(sx + 280, ry, "Ch", C_DIM)
        pyxel.text(sx + 300, ry, "RSSI", C_DIM)
        pyxel.text(sx + 340, ry, "Auth", C_DIM)
        ry += 2
        pyxel.line(sx + 2, ry + 6, sx + sw - 4, ry + 6, 1)
        ry += 9

        # Rows
        max_rows = (H - 40 - ry) // 9
        scroll = min(self._loot_search_scroll,
                     max(0, len(results) - max_rows))
        self._loot_search_scroll = scroll

        for i in range(scroll, min(scroll + max_rows, len(results))):
            pt = results[i]
            ptype = pt.get("type", "?")
            label = (pt.get("label") or "<hidden>")[:18]
            bssid = pt.get("bssid", "")
            ch = pt.get("channel", "")
            rssi = pt.get("rssi", "")
            auth = pt.get("auth", "")[:10]
            ssid_str = pt.get("label", "")

            # Type color
            if ptype == "wifi":
                tc = C_SUCCESS
                tl = "WiFi"
            elif ptype == "bt":
                tc = C_HACK_CYAN
                tl = "BLE"
            else:
                tc = C_DIM
                tl = ptype[:4]

            # Cracked?
            cracked = ssid_str and ssid_str in self._cracked_ssids
            if cracked:
                tc = C_SUCCESS
                label = f"* {label}"

            pyxel.text(sx + 4, ry, tl, tc)
            pyxel.text(sx + 30, ry, label, C_TEXT if not cracked else C_SUCCESS)
            pyxel.text(sx + 170, ry, bssid[-17:], C_DIM)
            pyxel.text(sx + 280, ry, str(ch)[:3], C_DIM)
            # RSSI color
            try:
                rv = int(rssi)
                rc = C_SUCCESS if rv > -60 else C_WARNING if rv > -75 else C_ERROR
            except (ValueError, TypeError):
                rc = C_DIM
            pyxel.text(sx + 300, ry, str(rssi), rc)
            pyxel.text(sx + 340, ry, auth, C_DIM)
            ry += 9

        # Scroll hint
        if len(results) > max_rows:
            pyxel.text(sx + sw - 100, sy + 20,
                       f"[UP/DN] {scroll+1}-{min(scroll+max_rows, len(results))}"
                       f"/{len(results)}", C_DIM)
