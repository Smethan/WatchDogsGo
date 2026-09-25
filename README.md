# Watch Dogs Go

**Smethan fork:** custom features and updates live on `main`. Use
[fork update instructions](docs/FORK_UPDATES.md) for existing installations,
**SYSTEM → Update WDG** for app updates, and **SYSTEM → Flash ESP32** for
[firmware releases](https://github.com/Smethan/projectZero/releases/latest).

Open-world hacking RPG with real cybersecurity tooling. A pyxel game frontend for the ESP32-C5 security device, inspired by Watch Dogs aesthetics.

![Watch Dogs Go — main screen](docs/screenshots/01_main_screen.png)

> *Lv.13 GHOST · 203k XP · 21 badges · synced with the wdgwars.pl community server*

![Loot Database](docs/screenshots/02_loot.png)

> *Loot database — 588 sessions, 6686 WiFi networks, 474 handshakes, 500 MeshCore nodes, BLACK HAT classification.*

![SYSTEM menu](docs/screenshots/03_system_menu.png)

> *SYSTEM menu — hardware toggles (GPS, LoRa, SDR, USB), whitelist, WPA-sec upload/download, ESP32 flasher. The hacker sprite changes per menu tab.*

![ADDONS menu](docs/screenshots/04_addons_menu.png)

> *ADDONS menu — MeshCore Messenger (LoRa mesh), Flipper Zero, ADS-B Radar, 433 MHz Scanner, PipBoy Watch. Each tab has its own sprite — here the radio recon operator with antenna and mesh nodes.*

![MeshCore Messenger](docs/screenshots/05_meshcore.png)

> *MeshCore Messenger — encrypted off-grid chat over LoRa 869 MHz. Node identity `WDG_locosp` was synced from the wdgwars.pl portal username automatically. 107 contacts visible from a single Polish MeshCore mesh, RSSI from -20dB to -113dB.*

![SCAN menu](docs/screenshots/06_scan_menu.png)

> *SCAN menu — entry point for WiFi and BLE scanning, MAC capture, sniffer modes. The cyber recon operator sprite watches over an arsenal of devices.*

Landing page: [locosp.org](https://locosp.org) — choose your path.

### Quick Install (uConsole / Raspberry Pi OS / Debian)

```bash
curl -fsSL https://raw.githubusercontent.com/Smethan/WatchDogsGo/main/docs/install | sudo bash
```

The installer clones the repo to `~/python/esp32-watch-dogs/`, creates a Python virtual environment, installs all dependencies, and adds a desktop launcher.

> **Optimized for [ClockworkPi uConsole](https://www.clockworkpi.com/uconsole)** — runs on 640x360 display at 30 FPS. Designed for field use with integrated hardware modules.

### First Run

After installation:

```bash
cd ~/python/esp32-watch-dogs
sudo ./run.sh
```

Or double-click **Watch Dogs Go** on your desktop.

**On first launch the game will:**

1. Load player profile (XP, level, badges) from `loot/loot_db.json`
2. Auto-detect ESP32 on `/dev/ttyUSB0` or `/dev/ttyACM0` (no serial = "ESP32 not found", that's OK to test the UI)
3. Try the documented AIO GPS UART (`/dev/ttyS0` on CM4,
   `/dev/ttyAMA0` on CM5) or a safe external GPS — optional
4. Discover and load plugins from `plugins/` (currently: Wars Sync, JanOS Loot Import)
5. Show the cyberdeck UI

**You don't need any API keys to start.** Wars Sync (community server upload) is locked until level 6 (WARDRIVER, 6000 XP) — until then you can play offline.

If you want to use the community server (wdgwars.pl), edit `secrets.conf`:

```bash
cp secrets.conf.example secrets.conf
nano secrets.conf
```

### What gets installed

The installer pulls these system packages (Debian/Ubuntu):

- `python3-venv`, plus `libsdl2-dev` and `libsdl2-image-dev` on non-Parrot systems — for the pyxel game engine. The SDL2 development packages are intentionally skipped on Parrot OS because they can make dependency installation fail there.
- `tcpdump`, `aircrack-ng`, `iw` — for MITM and Dragon Drain attacks
- `rtl-433` — for RTL-SDR 433 MHz sensor decoding
- `dump1090` — ADS-B; built from [flightaware/dump1090](https://github.com/flightaware/dump1090)
  since the Debian `dump1090-mutability` package has been archived upstream
  since 2018 and no longer supports RTL-SDR v4 tuners
- `bluez`, `bluez-tools`, `pulseaudio-utils` — for BLE attacks
- `python3-rpi-lgpio` — GPIO library for Raspberry Pi 5 / CM5
- `python3-gi`, `gir1.2-glib-2.0` — BlueZ pairing agent (PipBoy watch MITM)

And these Python packages (in `.venv`):

- `pyxel` — retro game engine
- `pyserial` — ESP32 serial communication
- `Pillow` — sprite generation
- `scapy`, `netifaces` — packet manipulation (MITM, Dragon Drain)
- `bleak`, `dbus-python` — BLE attacks (RACE, BLE HID)
- `LoRaRF`, `cryptography`, `PyNaCl` — LoRa MeshCore radio

### Known platform notes

- **Linux only** — macOS and Windows are not supported.
- **Raspberry Pi OS Bookworm/Trixie**: requires `python3-rpi-lgpio` (auto-installed). Older `RPi.GPIO` from pip doesn't work on RPi5/CM5.
- **Generic Linux**: Pyxel requires SDL2 system libraries. The installer handles Debian/Ubuntu automatically; for Fedora/Arch/Alpine you'll need to install SDL2 manually.
- **dialout group**: Your user must be in `dialout` group for ESP32 serial access without sudo. If you see "permission denied" on serial:
  ```bash
  sudo usermod -a -G dialout $USER
  ```
  Then log out and back in.

## Hardware Requirements

### Required

| Component | Description |
|-----------|-------------|
| **ClockworkPi uConsole** | Primary platform (or any Linux with pyxel-compatible display) |
| **ESP32-C5** | Running [projectZero](https://github.com/LOCOSP/projectZero) firmware — WiFi/BLE scanning, deauth, handshake capture, Evil Twin, BLE HID |
| **GPS module** | AIO v2 GPS on `/dev/ttyS0` (CM4) or `/dev/ttyAMA0` (CM5), or an external GPS — real-time positioning, wardriving logs, map tracking |

### Optional (for full functionality)

| Component | Description |
|-----------|-------------|
| **External WiFi adapter** (monitor mode) | Required for **Dragon Drain** (WPA3 SAE flood). Recommended: **Alfa Network AWUS036ACH** or **AWUS036ACM** |
| **LoRa SX1262 module** | AIO v2 LoRa — packet sniffing, MeshCore mesh scanning, Meshtastic, APRS balloon tracking |
| **Flipper Zero** | USB-connected — SubGHz RX/TX, NFC read/emulate, signal replay from SD card |

> Attacks marked with a red **!** triangle in the menu require an external WiFi adapter with monitor mode support.

### AIO v2 module (uConsole only)

If you have a [HackerGadgets AIO v2](https://github.com/hackergadgets/aiov2_ctl)
module plugged into your uConsole, the game can toggle GPS / LoRa / SDR / USB
power rails on demand from the SYSTEM menu. This is what makes the "GPS [g]",
"LoRa [l]", "SDR [d]" and "USB [b]" entries actually do something.

To enable AIO v2 control and LoRa you need **three** things on the system:

1. **`pinctrl`** — already present on Raspberry Pi OS (provided by the `raspi-utils`
   package). Verify with `command -v pinctrl`. The game uses `pinctrl set/get`
   directly to flip the GPIO pins — this is faster and more reliable than going
   through the full `aiov2_ctl` subprocess chain at runtime.

2. **`aiov2_ctl`** — official AIO v2 control tool from HackerGadgets. Used by
   the game for hardware presence detection and first-time rail setup, and
   independently provides a CLI + system tray GUI. Install it with the
   official method:
   ```bash
   sudo apt install -y python3 python3-pyqt6 git
   git clone https://github.com/hackergadgets/aiov2_ctl.git
   cd aiov2_ctl
   sudo python3 ./aiov2_ctl.py --install
   ```
   This installs `aiov2_ctl` to `/usr/local/bin` and enables the
   `aiov2-rails-boot.service` systemd unit so the rails come up at boot.

   The game's `setup.sh` will run these steps automatically on uConsole-class
   hardware (when `pinctrl` is present), and the in-game SYSTEM menu also
   offers an "Install aiov2_ctl" action if it isn't found.

3. **SPI1 boot configuration** — the LoRa power switch does not create its SPI
   transport. The SX1262 requires `/dev/spidev1.0`. On a CM4, the boot config
   needs `dtparam=spi=on` and `dtoverlay=spi1-1cs`; CM5 needs the overlay. WDG
   keeps this boot-file change explicit because SPI1 may belong to another
   accessory on uConsoles without an AIO v2. Configure it with:
   ```bash
   cd /path/to/WatchDogsGo
   sudo WDG_ENABLE_AIO_LORA=1 bash setup.sh
   sudo reboot
   ```
   Setup makes a one-time `.wdg-before-aio-lora` backup beside the boot config
   before appending any missing lines. If `/dev/spidev1.0` remains absent after
   reboot, check whether `devterm-printer.service` is reserving SPI1.

Without these the game still runs fine, the AIO toggles just become no-ops
and their state always shows `OFF` in the SYSTEM tab.

## What It Is

A pyxel-based game where you walk around a real-world map while your ESP32 scans for WiFi networks and BLE devices. Captured handshakes, credentials, GPS coordinates, and network data are saved to disk. The game is a visual overlay on real security tooling.

- Per-tab character sprites (SCAN, SNIFF, ATTACK, ADDONS, SYSTEM)
- Cyberdeck menu system with 5 categories
- Real-time radar, map markers, particle effects
- Terminal with live ESP32 output and attack logs
- World map with coastlines, tile rendering, zoom levels
- Persistent XP system with 20 rank levels (NOOB → FINAL_BOSS)
- Smart XP — full points for new devices, 1 XP for duplicates
- Hacker hat classification (WHITE/BLUE/GREY/RED/BLACK) based on activity profile
- Achievement badges: FLIPPER, WARDRIVER, MESHCORE, HS HUNTER, WPA-SEC, EVIL TWIN
- Flipper Zero integration — SubGHz scanner/replay, NFC read/emulate
- MeshCore toast notifications — always-on-top across all screens
- Auto-reconnect ESP32 after USB replug
- Firmware version check + [Wi-Fi / USB OTA updater](docs/FIRMWARE_OTA.md) + ESP32 USB flasher

## Install

```bash
cd ~/python/esp32-watch-dogs
bash setup.sh
```

Requires Python 3.10+ and SDL2 libraries (auto-installed by setup.sh).

## Run

```bash
# Auto-detect ESP32 and GPS:
./run.sh

# Specify serial port:
./run.sh /dev/ttyUSB0

# Run as module (sudo needed for scapy/airmon/tcpdump):
sudo .venv/bin/python3 -m watchdogs
```

Or click the **Watch Dogs Go** desktop icon on the uConsole.

## Controls

### General

| Key | Action |
|-----|--------|
| `TAB` | Open / close cyberdeck menu |
| `SPACE` (hold) | Hack nearby device |
| `S` | Quick stop — all ESP32 + Python attacks |
| `ESC` | Quit game (sends stop to ESP32) |
| `` ` `` (backtick) | Toggle loot screen |

### Map Navigation

| Key | Action |
|-----|--------|
| `Arrow keys` | Pan map manually (GPS overrides when fix available) |
| `=` or `]` | Zoom in |
| `-` or `[` | Zoom out |
| `0` | Reset zoom to world view |

### Terminal

| Key | Action |
|-----|--------|
| `PgUp` / `PgDn` | Scroll terminal history |
| `Fn+U` / `Fn+K` | PgUp / PgDn on uConsole keyboard |

### Cyberdeck Menu (TAB)

| Key | Action |
|-----|--------|
| `LEFT` / `RIGHT` | Switch category tab |
| `UP` / `DOWN` | Navigate items |
| `ENTER` | Select / execute item |
| `ESC` | Close menu |

### Input Dialogs

| Key | Action |
|-----|--------|
| `A-Z`, `0-9` | Type characters |
| `BACKSPACE` | Delete last character |
| `ENTER` | Confirm input / next field |
| `ESC` | Cancel |

### MITM Sub-Screen

| Key | Action |
|-----|--------|
| `S` | Start attack (from idle) |
| `UP` / `DOWN` | Navigate lists |
| `ENTER` | Select |
| `Y` / `N` | Confirm / cancel |
| `X` | Stop running attack |
| `PgUp` / `PgDn` | Scroll live log |
| `ESC` | Back / exit |

## Cyberdeck Menu

### SCAN

| Item | Command | Description |
|------|---------|-------------|
| WiFi Scan | `scan_networks` | Scan nearby WiFi access points |
| BLE Scan | `scan_bt` | Scan Bluetooth Low Energy devices |
| BT Tracker | `bt_track` | Track specific BT device by MAC |
| AirTag Scan | `bt_airtag_scan` | Detect Apple AirTags nearby |

### SNIFF

| Item | Command | Description |
|------|---------|-------------|
| WiFi Wardrive | `scan_networks` | Continuous WiFi scan + GPS logging (WiGLE CSV) |
| BT Wardrive | `scan_bt` | Continuous BLE scan + GPS logging |
| Pkt Sniffer | `start_sniffer` | Raw 802.11 + BLE packet capture |
| HS Capture | `start_handshake` | Active capture to ESP32 SD; optional BSSID picker (firmware 1.7.9+) |
| HS Capture no SD | `start_handshake_serial` | Active capture streamed to uConsole; optional BSSID picker (firmware 1.7.9+) |
| HS Sniff | `start_hs_sniff_serial` | Passive EAPOL/PMKID capture to uConsole |
| All Wardrive | `start_wardrive_batch_serial` | Batched ESP32 WiFi+BLE with host GPS, WiGLE loot, optional serving-cell tracking, and the selected LoRa/ADS-B/433 collectors |
| All Wardrive (host BLE) | `start_wardrive_wifi_batch_serial` | Batched ESP32 WiFi plus uConsole BLE with the same host GPS and selected host collectors |
| ESP Dual Test | `start_wardrive_batch_serial` | Diagnostic WiFi+BLE transport without cellular collection |

### ATTACK

| Item | Command | Description |
|------|---------|-------------|
| Deauth | `start_deauth` | Targeted deauth on BSSID + channel |
| Blackout | `start_blackout` | All-channel deauth broadcast |
| HS Capture | `start_handshake` | Active capture to ESP32 SD; optional BSSID picker (firmware 1.7.9+) |
| HS Capture no SD | `start_handshake_serial` | Active capture streamed to uConsole; optional BSSID picker (firmware 1.7.9+) |
| Evil Twin | `start_portal` | Fake AP with captive portal (SSID input) |
| SAE Flood | `sae_overflow` | WPA3 SAE Commit overflow |
| Dragon Drain | Python-native | WPA3 SAE DoS via scapy **!** |
| MITM | Python-native | ARP spoofing + live traffic capture |
| BlueDucky | Python-native | BLE HID keystroke injection (CVE-2023-45866) |
| RACE Attack | Python-native | Airoha BT headphone exploit (CVE-2025-20700) |

> **!** = requires external WiFi adapter with monitor mode

Both active HS Capture screens keep the original all-nearby mode. Press `N` to
scan, filter and select up to 16 BSSIDs, or `A` to return to all nearby networks.
Capture uses deauthentication in either scope; see [HS Capture controls](docs/HS_CAPTURE.md).

### ADDONS

| Item | Command | Description |
|------|---------|-------------|
| BLE HID | `bt_hid` | Enable BLE HID keyboard mode on ESP32 |
| HID Type | `bt_hid_type` | Type text via BLE HID |
| Mesh Messenger | Python-native | MeshCore direct-SPI or Meshtastic daemon-backed chat and discovery |
| Flipper Zero | USB serial | SubGHz RX/TX, NFC read/emulate, signal replay |

### SYSTEM

| Item | Command | Description |
|------|---------|-------------|
| STOP ALL | `stop` | Emergency stop all operations |
| GPS | — | Toggle GPS module ON/OFF (AIO GPIO) |
| LoRa | — | Toggle LoRa module power; the selected mesh protocol follows Wardrive Settings |
| SDR | — | Toggle AIO SDR power; Wardrive Settings selects ADS-B, 433 MHz, or neither |
| Whitelist | — | Manage MAC whitelist — whitelisted devices are hidden from scans, attacks, and wardriving |
| Upload WPA-SEC | — | Incrementally upload every network's crackable handshake `.pcapng` to wpa-sec.stanev.org, regardless of the WDG whitelist; successful content hashes are remembered and invalid/empty captures are rejected locally (prompts for an API key if needed; old PCAP-only loot remains readable) |
| Download WPA-SEC | — | Download cracked passwords (potfile) from wpa-sec.stanev.org |
| Reboot ESP32 | `restart` | Restart ESP32 device |
| Download Map | — | Download OSM tiles (~10 km radius around current position) for offline street-level map. Press again to cancel. |
| Flash ESP32 | — | Download latest firmware from GitHub + flash via esptool. Board picker: WROOM / XIAO |

## Flipper Zero

Connect Flipper Zero via USB to access SubGHz and NFC features from within the game.

### SubGHz Toolkit
- **Signal Scanner (433/868 MHz)** — live monitoring on common frequencies
- **Signal Scanner (RAW)** — raw signal capture
- **Replay Signals** — browse folders on Flipper SD card, select and transmit `.sub` files
- **Flipper Chat** — SubGHz chat between Flippers (placeholder)

### NFC Toolkit
- **NFC Read Tag** — read full tag info (type, UID, ATQA, SAK, pages, NDEF, signature). Auto-saves to `loot/<session>/nfc/` with user-chosen name
- **NFC Scanner** — continuous NFC detection
- **NFC Emulate** — browse `.nfc` files on Flipper SD, select and emulate card

> Flipper auto-detects by USB VID:PID. If disconnected, the game shows a connection prompt.

### Flipper Controls

| Key | Action |
|-----|--------|
| `UP` / `DOWN` | Navigate menu / file list |
| `ENTER` | Select / transmit / emulate |
| `X` or `ESC` | Stop scanner / back |
| `PgUp` / `PgDn` | Scroll output log |

## XP & Progression

### Level System (20 ranks)

| Level | Title | XP Required |
|-------|-------|-------------|
| 1 | NOOB | 0 |
| 2 | SCRIPT_KIDDIE | 100 |
| 3 | SKIDDIE+ | 500 |
| 4 | WANNABE | 1,500 |
| 5 | PACKET_MONKEY | 3,000 |
| 6 | WARDRIVER | 6,000 |
| 7 | HACKER | 10,000 |
| 8 | NETRUNNER | 20,000 |
| 9 | PHREAKER | 35,000 |
| 10 | EXPLOIT_DEV | 50,000 |
| 11 | ELITE | 75,000 |
| 12 | SHADOW_OPS | 100,000 |
| 13 | GHOST | 150,000 |
| 14 | ZERO_DAY | 250,000 |
| 15 | APT_AGENT | 400,000 |
| 16 | CYBER_DEMON | 600,000 |
| 17 | CYBER_GOD | 1,000,000 |
| 18 | DIGITAL_DEITY | 2,500,000 |
| 19 | MATRIX_BREAKER | 5,000,000 |
| 20 | FINAL_BOSS | 10,000,000 |

### XP Rewards

| Action | New Device | Duplicate |
|--------|-----------|-----------|
| WiFi network scanned | 15 XP | 1 XP |
| BLE device detected | 10 XP | 1 XP |
| Handshake captured | 200 XP | — |
| Evil Twin credential | 150 XP | — |
| Evil Twin client | 25 XP | — |
| Device hacked (SPACE) | 50 XP | — |
| NFC tag read | 15 XP | — |
| NFC tag saved | 25 XP | — |
| Flipper TX signal | 25 XP | — |

XP persists across sessions via `loot_db.json`. Duplicate detection uses all-time loot history.

### Hacker Hat Profile

Dynamic classification based on ratio of attack vs recon activity:

| Hat | Color | Profile |
|-----|-------|---------|
| WHITE | White | Ethical recon — scanning & mapping only |
| BLUE | Blue | Blue Team — defensive security research |
| GREY | Grey | Grey Hat — mixed recon & offensive ops |
| RED | Red | Red Team — active penetration testing |
| BLACK | Black | Black Hat — aggressive attack operator |

### Achievement Badges

Persistent badges earned by milestones, saved to `loot_db.json`:

| Badge | Earned By |
|-------|-----------|
| FLIPPER | First Flipper Zero action |
| WARDRIVER | First WiFi/BLE wardriving session |
| MESHCORE | First MeshCore message received |
| HS HUNTER | First WPA handshake captured |
| WPA-SEC | First successful WPA-sec upload |
| EVIL TWIN | First credential captured via Evil Twin |

## Attack Details

### Dragon Drain (WPA3 SAE Flood)

Exploits CVE-2019-9494. Sends spoofed SAE Commit frames to overwhelm the target AP's elliptic curve computation, causing denial of service. Runs entirely on uConsole using scapy — does not use ESP32 serial. **Requires external WiFi adapter in monitor mode** (e.g., Alfa AWUS036ACH).

### MITM (ARP Spoofing)

Full man-in-the-middle attack with dedicated JanOS-style sub-screen:

1. **Idle** — attack description and info
2. **Interface selection** — auto-detect or pick from list
3. **Target mode** — single IP, scan subnet + select, or all devices
4. **Confirmation dialog** — shows victims, gateway, interface
5. **Running** — live scrolling log: DNS queries (cyan), HTTP requests (green), credentials (red)

Saves full pcap to `loot/<session>/mitm/`. Restores ARP tables on stop.

### BlueDucky (BLE HID Injection)

Exploits CVE-2023-45866 for unauthenticated Bluetooth HID pairing. Scans for BLE devices, pairs without user confirmation, and injects keystrokes. Includes Rick Roll payload.

### RACE Attack (Airoha BT Exploit)

Targets Airoha, Sony, and TRSPX Bluetooth SoCs (CVE-2025-20700/20701/20702). Extracts link keys and device info via GATT debug interface.

## Mesh Messenger

The messenger supports two mutually exclusive owners for the AIO v2 SX1262.
Choose **MeshCore** or **Meshtastic** under **SNIFF > Wardrive Settings > All
Wardrive collectors**. MeshCore uses WDG's direct LoRaRF/SPI implementation.
Meshtastic leaves the radio entirely under a daemon; WDG never opens SPI while
that protocol is selected. The preferred `meshtasticd-wdg` fork exposes a
restricted `/run/meshtasticd/wdg.sock` API, so WDG can receive nodes and
messages while the official phone app uses the standard Meshtastic GATT
service over BlueZ. An independently installed stock `meshtasticd` remains
available through the legacy local Client API on `127.0.0.1:4403`. Closing WDG
disconnects its client without stopping the selected daemon.

Opening **ADDONS > Mesh Messenger** uses the selected protocol. Both backends
provide channel messages, a heard-nodes panel, direct messages, background
reception, map markers, and session loot. Meshtastic node observations are
saved to `meshtastic_nodes.csv`, while received text is appended to
`meshtastic_messages.log`. Node names and channel configuration come from the
daemon; change them with a Meshtastic client.

When **Automatic LoRa collector** is enabled, both **All Wardrive** modes use
the selected protocol. MeshCore sends a direct zero-hop `DISCOVER_REQ` at most
every 30 seconds after moving 25 metres and listens seven seconds for tagged
repeater/room responses. Meshtastic sends a zero-hop NodeInfo request at most
every 60 seconds after moving 50 metres. The zero hop limit discovers nodes in
direct radio range without routing the request across the mesh.

**SNIFF > Wardrive Settings > All Wardrive collectors** controls whether WDG
may automatically use the powered LoRa and SDR devices. With automatic LoRa
off, switching the preferred protocol does not stop `meshtasticd` or claim SPI.
Opening Mesh Messenger remains an explicit request to start the selected
backend. Switching to MeshCore or powering LoRa off stops `meshtasticd` so the
direct driver or GPIO power control can safely own the hardware.
ADS-B and 433 MHz are mutually exclusive because they share the AIO RTL-SDR.
Enabling either one disables the other, and both may be left off.

Radio-owner handoffs preserve the exact active and enabled state of both the
fork and stock services. WDG refuses a handoff from an unknown, transitional,
unsupported, or double-active service state. If restoration is interrupted,
that original snapshot remains an ownership barrier until it is restored; a
new handoff or package update cannot replace it with the partly changed state.
On exit, WDG waits for every radio/service handoff and protected package
transaction, and lets Host BLE/watch workers release their daemon leases before
closing the restricted socket.

Run `sudo bash setup.sh` to install the root-owned, argument-allowlisted
Meshtastic service helper. Setup also grants the login account access to the
shared radio-lock group; log out and back in if setup reports that it added the
membership. Setup does not download or start a daemon package. After the first
fork package has been installed, hardware-checked, and explicitly adopted,
**Meshtastic Service > Update service** can install only validated tags from
[`Smethan/meshtastic-firmware`](https://github.com/Smethan/meshtastic-firmware)
and retains a transactional rollback. The helper deliberately refuses to use a
new package as its own first semantic baseline; the one-time adoption procedure
is documented below. No fork package release has been published as part of this
source change. See
[Meshtastic service integration](docs/MESHTASTIC_SERVICE.md) and the
[restricted local API contract](docs/MESHTASTIC_WDG_API.md).

- **Fullscreen chat** with scrollable message history
- **Multi-channel support** — public, hashtag (#name), and private channels
- **Heard Nodes panel** — [H] shows discovered nodes with type, RSSI, SNR, age
- **Channel picker** — [C] to select active TX channel, [/] quick-switch
- **Speech bubbles** on map with CB radio sprite when messages arrive
- **Toast notifications** — always-on-top across all screens with sound
- **Node discovery** with GPS coordinates saved to loot (dedup by node ID)
- **Persistent MeshCore config** — node name + channels saved to `~/.janos_meshcore.json`
- **Meshtastic config reuse** — identity, channels, and node database come from `meshtasticd`
- **LoRa HUD status** in bottom bar (in line with GPS info)

### Messenger Controls

| Key | Action |
|-----|--------|
| `A-Z`, `0-9` | Type message |
| `ENTER` | Send message on active channel |
| `Ctrl+A` | MeshCore advert or zero-hop Meshtastic NodeInfo discovery |
| `Ctrl+N` | Change MeshCore name; Meshtastic points to its own settings |
| `Ctrl+H` | Toggle Heard Nodes panel |
| `Ctrl+C` | Open channel picker |
| `[` / `]` | Quick-switch channels |
| `Ctrl+X` | Clear chat log |
| `PgUp` / `PgDn` | Scroll history |
| `ESC` | Back to map (selected mesh client stays active) |

## LoRa Features

Requires the SX1262 module on AIO v2 and `/dev/spidev1.0`. If LoRa reports that
the device is missing, run `sudo WDG_ENABLE_AIO_LORA=1 bash setup.sh` from the
WDG checkout and reboot once.

| Feature | Frequencies | Description |
|---------|-------------|-------------|
| MeshCore Messenger | Regional preset | Direct-SPI mesh chat with adverts and speech bubbles |
| Meshtastic Messenger | Daemon configuration | `meshtasticd` Client API chat, nodes, channels, and zero-hop discovery |

## Map

- **Coastlines**: Natural Earth 50m data (~2200 points)
- **Tile rendering**: downloadable OSM map tiles for detailed street-level view
- **Map Downloader**: SYSTEM > Download Map fetches OpenStreetMap tiles in a ~10 km radius around current GPS position (or map center without fix). Progress shown in terminal. Press again to cancel. Tiles saved to `maps/` for offline use.
- **14 zoom levels**: from WORLD (360deg) to CLOSE-UP (0.02deg)
- **Map dot layers**: SNIFF > Wardrive Settings > Map dot layers controls WiFi, BLE, cell, Flock, Axon, MeshCore (positioned LoRa), ADS-B, 433 MHz sensors, and handshake markers independently. Each layer can be `OFF`, `FADE`, or `KEEP`; hiding a layer never stops collection or loot storage.
- **Fade timing**: `FADE` uses a shared 15, 30, 60, or 120 second lifetime. Dots dim through the Pyxel palette, expire from the recent view, and invalidate the cached overlay so they are no longer drawn. WiFi and BLE share a 512-identity, newest-first display registry to prevent long drives from becoming an unbounded red/cyan blob; saved WiGLE data is unaffected. WiFi, BLE, cell, ADS-B, and 433 MHz start in `FADE 30s`; Flock, Axon, MeshCore, and handshake markers start in `KEEP`.
- **Wardrive trail**: choose `OFF`, a cached three-pixel cyan `SOLID` route, or a three-pixel `HEAT` route colored from cool to red by unique WiFi/BLE activity heard in the preceding ten seconds. GPS gaps, pauses, implausible jumps, and separate sessions remain disconnected. The main map keeps the full 4,096-point display window; the tiny radar uses the newest 160 points to avoid a GPS-update hitch.
- **Marker colors**: green = WiFi, cyan = BLE/MeshCore, purple = Flock, orange = Axon, red = handshake
- **Radar**: top-right corner, real-time device positions
- **GPS tracking**: auto-centers on live position when fix available

## GPS

For a uConsole SIM7600, WDG reads cached NMEA through ModemManager so the modem
has one control-plane owner. Explicit external GPS devices remain supported.
Automatic serial discovery excludes every ModemManager-owned port and known
ESP32/uConsole ACM control device.

Before removing the LTE board, open **SNIFF → Wardrive Settings** and turn
**LTE modem integration** OFF. The choice persists and is read before GPS
startup, so later launches do not start the ModemManager broker or enumerate
its ports. Cell mast tracking and experimental neighbors become inactive, but
Wi-Fi, ESP BLE, uConsole host BLE, and All Wardrive continue normally.

With LTE integration OFF, WDG discovers the AIOv2 GPS on the documented
`/dev/ttyS0` CM4 UART or `/dev/ttyAMA0` CM5 UART, plus safe external ACM
receivers, without probing generic `ttyUSB` modem ports or unrelated platform
UARTs. Turn the AIO GPS rail on from **SYSTEM → GPS**.
An explicitly configured USB GPS through `WDG_GPS_DEVICE` is still honored.
If LTE integration remains ON while the module is absent, WDG handles the
missing ModemManager location provider and falls back to external GPS.

On CM4, `/dev/ttyS0` must not also be the Linux serial console. The official
HackerGadgets AIO package removes the serial-console entry from
`/boot/firmware/cmdline.txt`, but that change takes effect only after a reboot.
WDG detects the running-kernel conflict, leaves the occupied UART alone, and
reports whether a reboot or a boot-command-line repair is required.

Turn LTE integration ON after reinstalling the SIM7600. A working external GPS
continues to be used; otherwise WDG reacquires ModemManager GNSS. The separate
**Cell mast tracking** setting controls WiGLE cell observations without
disabling SIM7600 GPS. Both All Wardrive radio modes use the same setting.

If an older `/etc/systemd/system/uconsole-sim.service` sends `AT+CGPS` directly,
WDG leaves internal GNSS and cellular tracking disabled until it is migrated:

```sh
./scripts/migrate_uconsole_sim_service.sh --status
sudo ./scripts/migrate_uconsole_sim_service.sh --apply
```

The apply operation saves a restorable copy under `/var/backups/watchdogs/`,
installs a power-only service, and requires one manual reboot. It never restarts
or powers down a live cellular connection itself.

Status in bottom HUD:
- **Left**: LoRa ON/OFF status
- **Right**: GPS status — `Waiting for GPS fix` / `Waiting fix Vis:N` / `51.1234N 17.9876E`

Without GPS: arrow keys for manual pan.

## Loot

Saved to `loot/<session>/`:

| File | Content |
|------|---------|
| `serial_full.log` | Complete ESP32 serial output |
| `wardriving.csv` | WiGLE 1.6 WiFi/BLE/cellular observations + GPS |
| `cell_diagnostics.jsonl` | Valid serving-cell measurements written with WiGLE rows |
| `cell_health.jsonl` | Cell broker lifecycle, recovery, and clean-stop evidence |
| `cell_neighbor_candidates.jsonl` | Opt-in provisional PCI/channel observations; never uploaded to WiGLE |
| `active_cell_session.json` | Present only while cellular collection has not cleanly stopped |
| `bt_devices.csv` | BLE devices with GPS coordinates |
| `handshakes/` | PCAPNG with radiotap metadata, HCCAPX, .22000 (hashcat-ready) |
| `mitm/` | MITM pcap captures |
| `attack_events.log` | Attack start/stop/credential log |
| `adsb_aircraft.csv` | ADS-B aircraft positions decoded during an enabled All Wardrive/add-on session |
| `meshcore_nodes.csv` | Discovered MeshCore nodes with GPS and, for new sessions, their public key and relay-hop count |
| `meshcore_messages.log` | MeshCore chat message history |
| `nfc/` | NFC tag dumps from Flipper Zero |
| `whitelist.json` | MAC address whitelist |

## Architecture

```
watchdogs/
  app.py              Main game loop, UI rendering, menu system
  serial_manager.py   ESP32 serial comm (115200 baud, USB auto-detect)
  gps_manager.py      NMEA parser (CM4/CM5 AIO UART or external GPS)
  loot_manager.py     Loot saving (CSV, PCAPNG, handshakes)
  network_manager.py  WiFi scan result parsing
  app_state.py        Shared state (networks, GPS, BLE devices)
  config.py           Constants, ESP32 commands, API endpoints
  coastline.py        World coastline data (Natural Earth 50m)
  tile_manager.py     Map tile rendering + download
  dragon_drain.py     WPA3 SAE flood (scapy, standalone)
  mitm.py             ARP spoofing + tcpdump (standalone)
  bt_ducky.py         BLE HID injection (D-Bus, standalone)
  race_attack.py      Airoha BT exploit (bleak GATT)
  lora_manager.py     LoRa SX1262 (sniffer, MeshCore multi-channel)
  flipper_manager.py  Flipper Zero serial CLI (SubGHz, NFC, storage)
  aio_manager.py      AIO v2 GPIO control
  upload_manager.py   WPA-sec upload (pcap) + download (potfile)
  portals.py          Evil Twin/Portal HTML templates + upload
  convert_sprite.py   Sprite asset conversion (pyxel 16-color palette)
```

## Configuration Files

| File | Location | Purpose |
|------|----------|---------|
| `secrets.conf` | Project root | WPA-sec + WiGLE + WDGoWars API keys |
| `.watchdogs_meshcore.json` | `~/` | MeshCore node name + channels |
| `.watchdogs_meshcore_key` | `~/` | Ed25519 keypair for MeshCore signing |
| `loot_db.json` | `loot/` | Aggregate stats, XP, badges |
| `.wpasec_uploads.json` | `loot/` | SHA-256 receipts for per-account WPA-sec acceptance and global permanent capture rejection |
| `last_run.log` | `~/.watchdogs/` | Game log (rotated to `previous_run.log`) |

## Troubleshooting & Bug Reports

The game writes a full log to `~/.watchdogs/last_run.log` on every launch
(rotated to `previous_run.log` on next start). It contains:

- A clearly-marked `=== SESSION START ===` block with diagnostic info
  (OS, hardware model, Python version, detected USB serial devices,
  game version, display environment)
- All log messages from game subsystems (serial, GPS, LoRa, plugins)
- Full Python tracebacks for any unhandled exception, captured before
  the process dies

### Reporting a bug

The fastest way — let the game format the report for you:

```bash
sudo -u $USER python3 -m watchdogs --bugreport > /tmp/wdg-bug.md
cat /tmp/wdg-bug.md   # review it (no API keys, no GPS, just diagnostics)
```

Then open [a new issue](https://github.com/LOCOSP/esp32-watch-dogs/issues/new),
paste the contents of `wdg-bug.md`, and add:

1. **What you tried to do** (one sentence)
2. **What happened instead** (one sentence)
3. **Was the game running before the bug?** Yes / no / hard to say

You can also do it manually if you prefer — just open
`~/.watchdogs/last_run.log`, scroll to the most recent
`=== SESSION START — copy from here for bug reports ===` marker, copy
everything from that line to the end, and paste it into your issue
inside a triple-backtick code block.

### Common issues

**Game won't start, "ImportError: pyxel"** — re-run setup:
```bash
cd ~/python/esp32-watch-dogs && bash setup.sh
```

**"Permission denied" on /dev/ttyUSB0** — your user is not in the
`dialout` group:
```bash
sudo usermod -a -G dialout $USER
# log out and back in (or reboot)
```

**ESP32 not detected** — check `lsusb` for one of: CP2102, CH340,
FTDI, or Espressif USB-JTAG. The game logs all USB serial devices it
sees in the diagnostic block.

**MeshCore radio stays "OFF"** — MeshCore and a Meshtastic daemon cannot own the
same SX1262 simultaneously. Select MeshCore in Wardrive Settings and open Mesh
Messenger; WDG will stop the selected daemon before opening SPI. Inspect both
possible owners before intervening:
```bash
systemctl status meshtasticd-wdg.service meshtasticd.service
```

**Meshtastic Messenger does not connect** — rerun setup if the protected helper
or policy is missing, then inspect the selected service and fork socket:
```bash
sudo bash setup.sh
systemctl status meshtasticd-wdg.service meshtasticd.service
sudo journalctl -u meshtasticd-wdg.service -n 100 --no-pager
ls -l /run/meshtasticd/wdg.sock
```
`AUTO` prefers the restricted fork socket. `LEGACY_TCP` is the explicit stock
daemon fallback. Setup installs the helper and policy but leaves package
installation to an explicit action after a compatible release exists. The
first stock-to-fork migration must be installed from the exact successful
tag-workflow Actions artifact and verified manually; do not install the
mutable draft-release attachment. After testing, publish the draft with that
artifact's workflow run ID and producer attempt, stop both services, and run:
```bash
sudo /usr/local/libexec/watchdogs-meshtastic adopt-installed vX.Y.Z-wdg.N
```
Use the exact tag whose validated package is installed. Later **Update
service** operations are transactional because this adoption seeds the private
rollback cache. If the current config has no stable `General.MACAddress`, the
adoption check pins the candidate's verified effective MAC before recording the
baseline; configurations it cannot edit conservatively require a manual pin.

**HTTPS errors when uploading to wdgwars.pl** — check `~/.watchdogs/last_run.log`
for SSL errors. Most often caused by an expired system CA bundle:
```bash
sudo apt-get install --reinstall ca-certificates
```

**"Invalid API key (401)"** when adding your wdgwars.pl key — copy
the key from your profile page on the portal exactly (64 hex chars,
no quotes, no spaces).

## Community contributions

The game is built around a real ESP32-C5 + AIO v2 setup, but the
community has worked out alternative paths for hardware that doesn't
match exactly. These live as opt-in tools maintained by their authors
— not bundled in this repo, not part of our `setup.sh`, no
maintenance commitment from us. Use at your own risk.

- **ESP32-less wardriving on uConsole AIO v1** —
  [`wdg_wifi_bridge.py`](https://github.com/LOCOSP/WatchDogsGo/issues/3)
  by [@FusedStamen](https://github.com/FusedStamen). Emulates the
  projectZero serial protocol over a PTY (`/tmp/esp32-pty`) using the
  host's own WiFi (via `iw`) and Bluetooth (via `bleak`) adapters; the
  game opens it like a normal ESP32 thanks to the char-device argv
  detection added in 0.9.7. Optional extras for handshake capture
  (`airodump-ng` + `hcxpcapngtool`) and packet sniffing (`tcpdump`)
  are documented in the issue. Full source in the
  [author's fork](https://github.com/FusedStamen/WatchDogsGo).

If you have a similar setup or contribution worth sharing, open an
issue with `[community]` in the title and we'll link it here.

## Contributing

Pull requests welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the
dev environment setup, coding style, and what currently needs help.
Bug reports go in [GitHub Issues](https://github.com/LOCOSP/esp32-watch-dogs/issues);
please use `python3 -m watchdogs --bugreport` to generate a paste-ready
diagnostic block (described above).

For the full list of what's working, what's WIP, and what's been fixed,
see [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE) — use it, fork it, ship it. The license file contains an
additional notice about the legal responsibility of using offensive
security tooling against systems you don't own.

## Repository

- **GitHub** (primary): [github.com/LOCOSP/esp32-watch-dogs](https://github.com/LOCOSP/esp32-watch-dogs)
- **Landing page**: [locosp.org](https://locosp.org)
- **Community portal**: [wdgwars.pl](https://wdgwars.pl)
