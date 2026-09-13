# Smethan WatchDogsGo

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
