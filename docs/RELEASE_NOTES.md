# Smethan WatchDogsGo

## Unreleased

- Detect when the CM4 AIOv2 GPS UART is still reserved as the Linux serial
  console. WDG no longer probes the occupied UART or misreports the powered GPS
  receiver as absent; it distinguishes a pending reboot from a persistent
  `/boot/firmware/cmdline.txt` conflict and displays the required repair.

## 0.9.39 — Optional LTE module and AIOv2-safe GPS fallback

- Add a persistent **LTE modem integration** switch to Wardrive Settings. WDG
  reads it before GPS initialization, so OFF skips the ModemManager broker and
  modem-port inventory on every launch instead of probing for removed hardware.
- When LTE integration is OFF, stop serving-cell and experimental-neighbor
  collection while leaving ESP Wi-Fi/BLE, host BLE, loot, and the All Wardrive
  scan lifecycle unchanged. Cell controls remain saved and are shown as
  inactive until LTE integration is enabled again.
- Keep AIOv2 and external GPS independent of the LTE module. Automatic
  LTE-disabled discovery selects the documented `/dev/ttyS0` CM4 or
  `/dev/ttyAMA0` CM5 GPS UART and safe ACM devices without broadly probing
  Bluetooth-capable platform UARTs or modem-style `ttyUSB` ports. An explicitly
  configured USB GPS remains supported.
- Apply the toggle immediately. Disabling it releases ModemManager-backed GNSS
  and searches for the AIO/external receiver; enabling it keeps a working
  external GPS or reacquires SIM7600 GNSS when no other provider is active.
- Preserve the existing SIM7600-safe path when LTE integration is ON:
  ModemManager remains the sole control-plane owner, GPS and serving-cell data
  share its cached Location interface, and no modem TTY is opened directly.
- Gracefully fall back to external GPS when LTE integration is ON but no modem
  exists or the ModemManager location interface is unavailable.

Focused modem ownership, settings migration, AIO UART fallback, absent-modem,
cell gating, and both All Wardrive-mode tests pass. No uConsole hardware was
accessed or modified for this release.

## 0.9.38 — Metadata-rich PCAPNG captures

- Save new passive HS Sniff and firmware-transferred active handshake captures
  as PCAPNG only. Each file uses radiotap IEEE 802.11 records with channel,
  RSSI, explicit microsecond timestamp resolution and consistent FCS stripping.
- Validate every firmware PCAPNG block, interface, radiotap header and declared
  transfer length before committing it. Rebase ESP boot-relative timestamps to
  host wall time while preserving packet intervals. A partial or malformed
  serial transaction creates no capture file.
- Accept projectZero 1.7.12's `CAPTURE_FORMAT: PCAPNG` transaction and require
  it to contain PCAPNG without a duplicate PCAP block. Existing older firmware
  PCAP transactions remain readable; new WDG capture paths do not create PCAP.
- Prefer a PCAPNG capture over a same-stem legacy PCAP during WPA-Sec upload,
  so only the richer artifact is transferred. Keep old PCAP-only loot
  uploadable and apply the Wi-Fi whitelist filter to both extensions.
- Save host MITM packet captures through `dumpcap` in native PCAPNG and count
  both PCAPNG and historical PCAP files in the loot database.
- Record the new `hs_capture_pcapng_v1` firmware capability. Complete active
  captures still retain HCCAPX for local `.22000` conversion; the PCAPNG is the
  artifact uploaded to WPA-Sec because it carries the surrounding packets.

The complete 299-test host suite passes. The generated files are accepted as
PCAPNG/radiotap by Wireshark tools and hcxtools. Real RF completeness still
depends on which beacon, authentication, association, probe and EAPOL packets
the radio observes; the container validation cannot create missing air traffic.

## 0.9.37 — Whitelist-safe active HS capture

- Apply the host Wi-Fi whitelist to **All nearby** in both HS Capture
  destinations. WDG sends up to 32 protected BSSIDs through firmware 1.7.11's
  `all-except` scope; BLE whitelist entries are ignored for this Wi-Fi policy.
- Fail closed when all-nearby capture has protected Wi-Fi BSSIDs but the
  firmware lacks `hs_capture_exclusions_v1`, the list is malformed, or more
  than 32 Wi-Fi entries would be required. WDG no longer silently falls back to
  the unprotected legacy command in those cases.
- Show the excluded whitelist count before startup and in the active capture
  scope. Manually selected capture retains its existing per-BSSID whitelist
  validation and firmware 1.7.9 compatibility.

Requires projectZero 1.7.11 when the Wi-Fi whitelist is nonempty. An empty
whitelist retains compatibility with older firmware. No device commands were
sent while implementing or testing this change.

## 0.9.36 — Smooth scan ingestion

- Replace per-observation WiGLE CSV reads, full rewrites, and `fsync` calls
  with session-resident identity indexes. Wi-Fi and BLE de-duplication and
  strongest-RSSI replacement are now constant-time operations on the display
  thread.
- Persist WiGLE rows, the BLE inventory, and cellular diagnostics through a
  dedicated background writer. It waits for 200 ms of quiet to combine a scan
  burst into one atomic snapshot and forces a checkpoint within one second
  when observations arrive continuously.
- Request a background checkpoint at the end of every batched All Wardrive
  scan, retain the existing 30-second filesystem sync, and synchronously drain
  all accepted observations when WDG exits. A sudden power loss can lose at
  most the newest uncheckpointed window instead of risking a partially
  rewritten CSV.
- Buffer the full serial log during a scan burst and flush it at most once per
  second, while preserving final and periodic durable flushes.
- Preserve the existing WiGLE 1.6 layout, first-seen timestamps, strongest
  observation selection, repeated cellular history, and atomic file replace
  behavior. No projectZero firmware change is required.

On the host benchmark, accepting 500 unique Wi-Fi results fell from about
0.78 seconds of display-thread work to 0.005 seconds, while the final durable
snapshot completed in about 0.008 seconds. Concurrent stress testing retained
all 2,000 unique rows with no temporary files left behind. The complete
287-test host suite and Python bytecode compilation pass. No patched code was
installed or run on the uConsole.

## 0.9.35 — Bounded and optional wardrive dots

- Add **Regular wardrive dots (2048 max)** to Wardrive Settings. It is enabled
  by default and persists in `wardrive_settings.json`. Turning it off hides
  ordinary historical clusters, live Wi-Fi/BLE nodes, discovery rings, and
  ordinary radar dots while collection and loot storage continue.
- Keep Flock/Axon map and radar markers, detection alerts, GPS trails, cellular
  neighbor indicators, aircraft, sensors, MeshCore nodes, handshake markers,
  and other special overlays visible when ordinary wardrive dots are disabled.
- Bound the combined ordinary map display to 2,048 nodes for the 4 GB CM4.
  New live Wi-Fi/BLE discoveries evict the globally oldest live visual node;
  live nodes reserve space from the historical display in 128-node blocks, so
  older historical points fall away as the current wardrive grows.
- Preserve complete WiGLE/loot files, identity history, Flock/Axon evidence,
  hacked totals, and session discovery counters when visual nodes are evicted.
  Historical data remains available to loot search and is restored as the
  newest bounded window after restart.
- Cap short discovery-ring queues at 256 per radio and coalesce live-map and
  radar cache publication to at most twice per second during dense result
  bursts. New rings still appear immediately while cached dots catch up.

The 2,048-point host benchmark measured about 2.8 ms for a complete historical
model build and 2.6 ms for a complete live model build before their frame
budgets are applied. Cached dispatch remained below 0.01 ms in the synthetic
backend. The complete 286-test host suite, Python bytecode compilation, shell
syntax checks, and source diff checks pass. No projectZero firmware update is
required, and no patched code was installed or run on the uConsole.

## 0.9.34 — Cached wardrive nodes and smoother moving maps

- Deduplicate historical map identities across sessions before rendering,
  retaining the observation with the strongest RSSI. Source loot files and
  their totals remain unchanged.
- Use adaptive geographic index levels for both wide and close views. Dense
  close-map queries no longer scan every point stored in a coarse degree cell.
- Build historical clusters incrementally under a two-millisecond frame budget,
  render them into an overscanned native image, and translate that image during
  ordinary GPS motion. Cluster selection and popups follow the translated
  positions.
- Render mature live Wi-Fi/BLE markers into two cached blink frames. The
  per-frame marker loops now contain only the short discovery-ring animations;
  Flock and Axon detections keep their existing precise colored overlays.
- Cache and deduplicate the small radar's dense node dots, with a one-millisecond
  build budget and translation across small GPS movements. Aircraft, handshake
  markers, and notable-device overlays remain live.
- Settle camera easing exactly when less than half a display pixel remains,
  avoiding endless subpixel cache and tile movement.
- Add a repeatable synthetic benchmark for geographic indexing, background
  model construction, raster dispatch, and cached drawing.

The complete 281-test host suite, Python bytecode compilation, shell syntax
checks, and source diff checks pass. No projectZero firmware update is required,
and no patched code was installed or run on the uConsole during this work.

## 0.9.33 — Safe map refresh and clear bottom HUD

- Keep native `pyxel.Image` tile resources owned by the Pyxel draw thread.
  Background map-download completion can invalidate decoded Python tile data,
  but native display images are released by the next draw callback. This fixes
  the repeated `Image is unsendable, but is being dropped on another thread`
  traceback seen after a map manifest refresh.
- Place the bottom shortcut and active-tool text after the rendered `CELL`
  counter and before the reserved GPS region. The shortcut is right-aligned and
  switches to a complete compact form when visible LoRa status reduces space.

The complete 268-test host suite passes. No projectZero firmware update is
required, and no patched code was run on the uConsole during this repair.

## 0.9.32 — Faster detailed maps and large wardrive sessions

- Replace per-frame Python tile pixel loops with prepared `pyxel.Image` tiles
  and native blits. Prepared tiles retain the existing projection dimensions,
  parent fallback crops and z15/z16 close-up detail.
- Raise the decoded source cache to 64 tiles, add bounded prepared-image,
  missing-tile and resolved-parent caches, and synchronize cache invalidation
  with the background map downloader. A faster nibble lookup also reduces the
  one-time cost when entering an unseen area or zoom.
- Index historical loot and live Wi-Fi/BLE objects geographically. Clustering,
  map markers and radar views query the visible area instead of projecting the
  complete archive every frame. Appended observations update these indexes
  incrementally.
- Cache projected map and radar trails while leaving the complete JSONL route
  unchanged. Fully offscreen and zero-length display segments are skipped, and
  close-map labels are capped while every point remains visible.
- Move the recurring historical CSV, password and loot-total refresh off the
  Pyxel update thread. Refresh workers are single-flight and publish completed
  snapshots back to the game thread.
- Replace the growing linear BSSID search used for target selection and repeated
  full-list hacked-device HUD counts with indexed or event-maintained state.

Representative host probes reduced prepared detailed-tile frames from roughly
20–116 ms to 0.1–0.6 ms; a one-pixel pan across a 50,000-point synthetic archive
fell from a full scan to about 0.3 ms. Absolute uConsole timings will differ.
The full 264-test host suite passes. No projectZero firmware update is required.

## 0.9.31 — ModemManager-owned cell mast tracking

- Restore serving-cell tracking in **All Wardrive** and **All Wardrive (host
  BLE)** through ModemManager's cached `Modem.Location` interface. WDG records
  one valid serving-cell observation at each completed firmware batch without
  opening an AT, GPS, or QMI device node.
- Move the internal SIM7600 GNSS feed behind the same ModemManager broker.
  Serial GPS discovery excludes all ModemManager-owned ports and known ESP32
  and uConsole ACM control devices.
- Add **Cell mast tracking** (on by default after its ownership preflight) and
  **Experimental QMI neighbors** (off by default) to Wardrive Settings.
  Experimental neighbors are rate-limited, circuit-broken, local-only yellow
  dots and never become WiGLE cells without a global identity.
- Add `cell_health.jsonl`, `active_cell_session.json`, and provisional neighbor
  diagnostics so an unclean stop can be distinguished from a normal session.
- Add `scripts/migrate_uconsole_sim_service.sh` with status, apply, backup, and
  restore operations. WDG refuses internal modem GPS/cell access while the old
  direct-AT GNSS service remains installed.

No projectZero firmware change is required. The service migration is explicit
and requires one announced manual reboot before hardware validation.

## 0.9.30 — Disable cellular mast collection

- Remove the cellular background collector from **All Wardrive** and **All
  Wardrive (host BLE)** after crashes continued with the QMI proxy
  implementation. These modes no longer call ModemManager, launch `qmicli`,
  inspect cellular devices, open modem ports, or run a cellular retry timer.
- Remove the live `CELL:...` wardrive overlay. Wi-Fi, ESP BLE, host BlueZ BLE,
  GPS trails, Flock/Axon detection, and map behavior are unchanged.
- Keep existing cellular WiGLE rows readable, uploadable, and included in
  historical loot totals. This update does not delete or rewrite prior loot.
- Stop installing ModemManager/libqmi/libmbim tools as WDG dependencies. The
  update does not uninstall existing packages or alter the uConsole's cellular
  connection.

The complete 228-test suite passed on the host, including a regression check
that the All Wardrive UI has no cellular collector. No code was run or installed
on the uConsole during this rollback.

## 0.9.29 — Safe QMI cellular collection

- Remove the persistent SIM7600 AT-port fallback introduced in 0.9.28. WDG no
  longer opens `ttyUSB2`, `ttyUSB3`, or any other modem serial interface for
  cell collection.
- On the affected ModemManager 1.20/QMI combination, use the same read-only NAS
  cell-location request that newer ModemManager releases use, submitted through
  qmi-proxy on `/dev/cdc-wdm0` so access is synchronized with the existing
  cellular data connection.
- Sample every 30 seconds, enforce a 12-second query deadline, stop retrying
  permanently unsupported configurations for the current session, and back off
  transient retries from 60 seconds to a five-minute cap. All Wardrive Wi-Fi
  and BLE continue if cellular collection is unavailable.
- Save LTE/UMTS neighbors only when the backend supplies a complete global
  identity. QMI physical-cell measurements without a global cell ID are not
  mislabeled as distinct WiGLE masts.

The source-level diagnosis and remaining hardware test are documented in
`docs/CELLULAR_STABILITY.md`. The complete 241-test suite passed on the host;
no patched code or AT command was run on the uConsole during this repair.

## 0.9.28 — SIM7600 cellular fallback discovery

- Fix **All Wardrive** cellular detection on QMI-controlled SIM7600 modems where
  ModemManager 1.20 exposes `GetCellInfo` but answers `Core.Unsupported` and
  omits both AT command ports from its D-Bus `Modem.Ports` list.
- When that happens, WDG now reads the existing ModemManager udev role tags,
  accepts only `AT_SECONDARY`, and verifies the candidate belongs to the same
  physical modem. GPS, QCDM diagnostic, audio, primary AT, and other modems are
  excluded.
- The fallback remains serving-cell-only through `AT+CPSI?`; neighboring cells
  still require modem/ModemManager `GetCellInfo` support.

Diagnosis on the affected uConsole confirmed that `/dev/ttyUSB3` is tagged as
the SIM7600's secondary AT interface and answers `AT+CPSI?` while WDG and the
QMI data bearer remain active. No application changes were installed on the
uConsole during validation. The complete 233-test suite passed on the host.

## 0.9.27 — Batched All Wardrive and recoverable liveness

- Prefer projectZero firmware 1.7.10's ten-second batch transport for **All
  Wardrive**, **All Wardrive (host BLE)** and **ESP Dual Test**. The overlay now
  shows `Background scan #N x/10s`, result counts and the transition to the next
  scan. The terminal prints one scan banner, grouped result rows and a completion
  banner instead of receiving an unbounded live firmware stream. Firmware with
  only v1 remains available and is labeled `LEGACY STREAM`.
- Track firmware control and ESP observation time separately. A late heartbeat
  first triggers `wardrive_status`; WDG retries the state query and only stops
  after 15 seconds when both control and ESP data are absent. Host BLE and cell
  observations never renew the ESP32 clocks. Missed start/stop records can be
  recovered from a status response, and the firmware's final global cleanup
  line is accepted as a v2 stop fallback.
- Keep BlueZ BLE collection, saving, map updates and Flock/Axon matching live in
  host-BLE mode while buffering only its terminal rows until the ESP batch
  result boundary. Cellular serving/neighbor collection continues throughout
  both All Wardrive modes.
- Extend GPS history to a bounded 30-second/600-fix window and select the nearest
  fix within three seconds of each delayed observation. This preserves the
  capture-time position of records delivered after a ten-second batch.
- Remove the artificial 100 ms delay from keepalive/status commands. Other
  console commands retain their existing pacing.

The full 231-test host suite, bytecode compilation and shell syntax checks pass.
Both companion firmware variants compile with ESP-IDF 6.0.1 and pass native
transport and stack tests. A prolonged XIAO/uConsole field test is still needed
to establish whether the original timeout is eliminated under dense RF load.

## 0.9.25 — Optional targets for both active HS Capture modes

- Add the same network picker to **HS Capture** (ESP32 SD) and **HS Capture no
  SD** (serial/uConsole). Press **N**, **R** to scan, **F** or **/** to filter by
  SSID, **G** to set a minimum RSSI, **O** to sort, and **Space** to select up to
  16 BSSIDs. **Enter** applies the selection; **A** keeps the original
  all-nearby behavior.
- Keep both captures active and background-friendly, including their existing
  deauthentication behavior and PMKID/M1/M2/M3/M4 progress screens. The SD
  variant still requires an ESP32 SD card. The no-SD variant still transfers
  its results to the uConsole after stop. Neither mode requires GPS.
- Bind selected starts to one complete five-minute firmware scan snapshot.
  Disconnects, incomplete scans, expired or changed tokens, missing BSSIDs,
  open/WEP networks, unsupported channels and current WDG whitelist entries are
  rejected instead of falling back to all-nearby capture.
- Require Smethan projectZero **1.7.9+** only for network selection. All-nearby
  capture continues to use the compatible legacy command on older firmware.
  PMKID and M1-M4 values remain packet sightings; they do not claim a matched
  exchange even when every column is populated.
- Treat the final canonical `SSID: ... AP: ...` line as the commit for each
  no-SD artifact. `CAPTURE_KIND` precedes its blocks without triggering the
  legacy fallback; sequential valid/partial artifacts cannot share buffered
  data. Bound and sanitize SSID/BSSID filename components from older or
  malformed firmware output.

The full 220-test host suite, bytecode compilation, source diff checks,
sequential valid/partial serial-artifact coverage and a 640x360 picker render passed.
Firmware protocol/build tests cover the companion changes. Targeted capture and
RF completeness were not physically validated for this release.

## 0.9.24 — Faster USB OTA

Measured on the uConsole: **53.33 seconds** for a complete published-release
USB update, versus **445.65 seconds** through the old receiver (about **8.4x
faster**). Deliberate serial close/lost-ACK recovery finished in **60.23 seconds**.
Both booted a verified valid slot with no pending transfer. See
[hardware validation](https://github.com/Smethan/WatchDogsGo/blob/main/docs/USB_OTA_VALIDATION_2026-09-13.md) for method and limits.

- Automatically negotiate 4 KiB base64 blocks with XIAO firmware 1.7.8+.
  Supported receivers get a whole block without the old 64-byte write pauses;
  progress updates once per percentage point instead of once per 256 bytes.
- Retain the existing 1.7.7 receiver path and WROOM compatibility. Reconnects
  remain bound to the original USB identity, verify the receiver offset and
  renegotiate its transfer capabilities. Two consecutive large-block failures
  fall back to the smaller format without discarding completed progress.
- Keep release verification, CRCs, durable resume, full-image verification and
  post-reboot slot/version checks. No network configuration changes.
- Update WDG, then install firmware 1.7.8 using USB OTA. That first transfer
  uses the old receiver speed; later transfers use fast mode automatically.
- Targeted HS Capture was deferred; existing capture modes are unchanged.

## 0.9.23 — Resumable USB OTA and handshake display guidance

- **SYSTEM → OTA Update** now offers ordinary Wi-Fi OTA and resumable
  application-mode USB transfer. Select METHOD with Left/Right, then START.
  USB uses the uConsole's available internet connection, including cellular;
  the ESP32 needs no Wi-Fi connection and WDG does not change host networks.

- USB mode downloads/verifies the board-specific fork bundle on the uConsole,
  sends CRC-checked blocks, and resumes at the ESP32's acknowledged/checkpointed
  offset after disconnects or reboot. Full-image SHA256 and firmware validation
  precede boot selection. Install firmware **1.7.7+ once via Wi-Fi OTA** first.
  Ctrl+D explicitly toggles discard of an unfinished different image; normal
  START resumes the same image. Bootloader and partition table are not rewritten.
- Explain empty HS Capture counters caused by dropped progress copies. Firmware
  **1.7.6** fixes the root cause: packet lines were larger than the console's
  256-byte USB TX ring. The fix also covers HS Sniff and serial wardrive output.

Hardware validation on the uConsole/XIAO C5 completed a 2.25 MB USB update,
resumed after a deliberate disconnect at 64 KiB, and verified the new valid boot
slot (7m37s total). The passive packet-delivery check received 394 complete
frames with no malformed or incomplete records. WDG's 181 tests and firmware
build/transport checks pass.

The capture packet-copy fix requires the firmware update, not just WDG.
See [OTA methods](FIRMWARE_OTA.md) for requirements and recovery behavior.

## 0.9.22 — Update ESP32 firmware over Wi-Fi

- Add **SYSTEM → Wi-Fi OTA** with masked network details, published version
  selection, download progress, and post-reboot verification. The ESP32 downloads
  its board-specific app directly from **Smethan/projectZero** using existing
  firmware commands; the firmware image does not pass through USB/esptool.
- Require running fork firmware **1.7.2+**, its serial capabilities, and a valid
  two-slot OTA layout before requesting the update. Select explicit stable tags,
  including older compatible releases. No extra firmware build or SD card needed.
- Stop scans, reserve serial ownership, and block USB power toggling during the
  update. Reconnect only to the original USB identity. Report success only after
  confirming the requested version in the expected, valid OTA slot; uncertain
  outcomes remain unconfirmed and are never automatically retried.
- Keep passwords and firmware command echoes out of WDG logs; clear the UI's
  password on start/exit. Existing firmware may save successful Wi-Fi credentials
  on its SD card. Keep ESP32 power connected throughout the operation.

See [Wi-Fi OTA instructions and limitations](FIRMWARE_OTA.md). This bypasses bulk
USB transfers, but still needs working application-mode USB commands and stable
power. Offline protocol/failure tests, live release-list validation and rendered
screens were checked. An end-to-end OTA on the affected uConsole is still pending.

## 0.9.21 — Identify the USB device before flashing

- Remove arbitrary ttyUSB/ttyACM fallback detection. Auto-detection prefers a
  single native Espressif device over generic USB-UART adapters and refuses
  ambiguous candidates instead of choosing the first port.
- Preserve USB metadata when WDG opens its serial connection, so BOOT/reset
  renumbering cannot make the flasher trust an unrelated device at the old path.
  Preferred paths must have recognized USB IDs; a missing preferred path never
  silently selects another board. Serial/location identity is required.
- Show the target port and USB ID/serial/location in the flash wizard. **R**
  refreshes metadata without opening/resetting ports. Enter is blocked without
  a selected target; a board appearing later is not silently selected by the
  flashing worker. Bound targets remain bound across failed refreshes.
- Log both the USB identity and an explicit old-port -> new-port message when
  the same board renumbers. A shared USB-UART bridge ID identifies an adapter,
  not the chip behind it; esptool still checks the ESP32-C5 chip before writing.

Update WDG and restart. No firmware update is needed for this selection repair.
The reported uConsole freeze has not been reproduced or attributed to a
particular USB device; this release fixes confirmed selection weaknesses.

## 0.9.20 — uConsole flasher repair and firmware rollback selection

- Remove the uConsole-only USB power cycle before flashing. Automatic mode uses
  the standalone script's `default-reset` / `watchdog-reset` sequence; Manual
  BOOT mode preserves an already-entered bootloader with `no-reset` at 115200.
- Pause WDG serial polling and reconnects for the whole flash wizard, including
  manual BOOT entry and failed attempts. ESC resumes normal use when idle;
  hiding a running flash keeps its serial reservation until the worker finishes.
- Follow the selected USB device across port renumbering instead of flashing or
  reconnecting to the first available tty. Save full output and tool/version/port
  details in `firmware_cache/flash-<timestamp>.log`.
- Require esptool 5.4+ (below 6). If the running Python has an older/missing tool,
  the flasher prepares a private environment on first use and reuses it later.
  Updating WDG does not rerun the full setup or install system-wide packages.
- **V/B** selects a published firmware version, including older releases;
  **Left/Right** selects Automatic or Manual BOOT. Explicit versions never fall
  back to latest, and board/manifest/SHA256 verification still applies.

Update WDG and restart; no new ESP firmware is required for this flasher update.
To roll back one release, select **XIAO ESP32-C5**, **v1.7.3**, and the desired
connection mode. See [flashing and rollback](FIRMWARE_FLASHING.md).

Offline tests, a real private-tool installation, both 1.7.3 bundle downloads and
UI renders passed. The uConsole transfer and reported HS regression still need
hardware verification; this release adds rollback but does not claim to repair
the firmware capture regression.

## 0.9.19 — Wait for HS Capture file transfer on stop

- Keep the capture finishing indicator and defer pending mode switches when
  firmware reports "all operations stopped" before its no-SD file dump ends.
  The final capture cleanup line releases the transition. Forced task stops
  show an error instead of claiming the capture completed normally.
- Includes the 0.9.18 progress submenus below; firmware 1.7.4 remains current.

## 0.9.18 — HS Capture progress screens

- **HS Capture** and **HS Capture no SD** now open background-friendly submenus
  in both SNIFF and ATTACK, with per-AP/client PMKID and M1/M2/M3/M4 columns.
  Enter starts, S stops, Escape/Tab returns to the map without stopping.
- Live packet progress requires **Smethan projectZero 1.7.4+**. Older firmware
  retains its capture behavior with coarse M-number logs where available;
  live PMKIDs are marked unavailable. Channel/RSSI are not guessed.
- Existing active behavior and SD/no-SD file destinations are unchanged. The
  SD warning remains visible, and serial capture waits for its file dump on stop.
- Clarified the note in both capture screens: packet counts do not establish
  matching handshake pairs. Fixed completion logs re-enabling the HS indicator.

Update WDG and restart, then use SYSTEM → Flash ESP32 with the correct board
image for full progress support. See [HS Capture](HS_CAPTURE.md) for controls,
storage timing, counter interpretation and firmware details.

## 0.9.17 — ESP32 default and explicit host BLE option

- **All Wardrive (6)** uses the ESP32 for both Wi-Fi and BLE again, with the
  corrected heartbeat handling from 0.9.16.
- **All Wardrive (host BLE) (9)** is a separate option using ESP32 Wi-Fi and
  uConsole BLE. Only this option requires the firmware 1.7.3 Wi-Fi-only capability.
- **ESP Dual Test (8)** remains available. Its gap counter now includes a
  cumulative percentage of serial sequence positions missing; timing logs also
  include the percentage. Gaps measure unaccepted serial records, not missing
  unique devices or all RF packet loss.

Update WDG and restart; this release does not require a new firmware flash.
Firmware 1.7.3 is still required if selecting the host BLE option.

## 0.9.16 — uConsole BLE and heartbeat diagnostics

- **All Wardrive (6)** now captures Wi-Fi on the ESP32 and BLE on the uConsole.
  Requires firmware **1.7.3+** and an enabled host Bluetooth adapter. Bluetooth
  startup errors stop both scans with a visible explanation; there is no silent
  fallback to ESP32 BLE. GPS, WiGLE output, markers, trails and Flock/Axon matching
  use the existing host pipeline.
- **ESP Dual Test (8)** retains ESP32 Wi-Fi+BLE scanning and works with previous
  serial-wardrive firmware. It compares stats-message age with valid-record age,
  reports false timeouts the old rule would cause and saves a timing log. Both
  modes still stop after seven seconds with no valid firmware records.
- Startup no longer runs setup for missing optional dependencies. dump1090
  detection recognizes existing source/package installs; explicit setup includes
  its ncurses/USB build dependencies and reports build errors.

Update WDG and restart. To test the old heartbeat issue first, select ESP Dual
Test before flashing. For split All Wardrive, flash the 1.7.3 XIAO release using
SYSTEM → Flash ESP32. See `docs/HOST_BLE_WARDRIVE.md` for diagnostic interpretation.

Automated tests and both firmware builds pass; these checks do not establish
hardware stability or reception completeness on the user's uConsole.

This fork includes All Wardrive, background passive HS Sniff, Flock/Axon detection,
optional GPS trails and detailed close-up maps. Routine session keepalives no
longer flood the console.

**SYSTEM → Update WDG** (or `bash update.sh`) follows Smethan/WatchDogsGo main.
It only fast-forwards clean checkouts and preserves locally diverged work by
stopping with an error. The former feature/all-wardrive branch can migrate to
main automatically. Restart WDG after an update to load the new code.

**SYSTEM → Flash ESP32** downloads published Smethan/projectZero releases,
verifies archive/file checksums, board identity, version and offsets, and then
flashes the selected board. The XIAO and standard images stay separate. There
is no fallback to LOCOSP downloads and no reuse of stale cached firmware files.
WDG reserves the serial port during flashing so its reconnect loop and capture
keepalives cannot compete with esptool. Normal polling reconnects afterward.

Source releases and tests use standard public GitHub Actions runners. See
`docs/FORK_UPDATES.md` for migration and release instructions.
