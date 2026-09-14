# Smethan WatchDogsGo

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
