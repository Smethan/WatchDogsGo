# All Wardrive implementation plan

Status: planned, not implemented. Repository setup and this document are complete.
Date: 2026-09-12.

## Objective and ownership

Add combined 2.4/5 GHz Wi-Fi plus BLE discovery to LOCOSP-derived projectZero, stream observations over USB/serial without requiring GPS or an SD card on the ESP32, and add **All Wardrive** to Watch Dogs Go. The uConsole owns GPS, map placement, deduplication, XP and file storage. Also fix misleading scan status and competing scan timers.

Repositories:
- Firmware: https://github.com/Smethan/projectZero (fork parent LOCOSP/projectZero).
- App: https://github.com/Smethan/WatchDogsGo (fork parent LOCOSP/WatchDogsGo).
- Work branch in both: feature/all-wardrive.
- Firmware baseline: ef35e9bbed28c441f53abdb6e634cf46428f912b.
- App baseline: 6412603d6fa7c8f2e6fe76873531d83c61adc63d.

Changes will be committed and pushed to these personal forks in working increments. No upstream PR, upstream merge, or upstream push is part of this task. Original clean clones are retained locally under ../.upstream-checkouts/. origin is the user's fork; upstream fetches from LOCOSP and has pushing disabled. Default push target is origin.

This plan does not authorize flashing or device deployment. Software builds and offline verification precede any separately arranged hardware validation.

## Confirmed starting points

projectZero already runs Wi-Fi promiscuous beacon capture and BLE together in start_wardrive_promisc. Its startup calls bt_nimble_init directly after initializing Wi-Fi. bt_start_scan_coex requests a 40 ms window per 160 ms interval. The XIAO build uses the same base configuration with a USB Serial/JTAG console overlay; software coexistence is enabled.

That existing mode is coupled to firmware GPS, SD logging, GPS-dependent re-log rules, and a persistent seen-device table. Its Wi-Fi and BLE serial formats differ from normal scan output and from one another. BLE serial output currently occurs inside the successful SD write path. Merely bypassing the GPS wait and SD initialization would therefore be incomplete.

Watch Dogs Go currently repeats scan_networks / scan_bt, both of which switch exclusive radio modes. Starting a new scan sends stop and delays roughly 500 ms. Old flags and rescan timers can survive this transition, and independently fire conflicting commands. UI titles and HUD labels use different flag-selection logic.

ESP-IDF 6.0.1 rates Wi-Fi STA scan + BLE scan as stable, but Wi-Fi sniffer RX + BLE scan as supported with unstable performance. Reuse the existing capture approach first and validate discovery performance; do not promise two independent RF receivers.
Reference: https://docs.espressif.com/projects/esp-idf/en/v6.0.1/esp32c5/api-guides/coexist.html

## 1. Define an additive serial protocol

Use a new start_wardrive_serial command, with an explicit host-supplied session token. It starts Wi-Fi 2.4 GHz, Wi-Fi 5 GHz and BLE together. Existing scan and standalone GPS/SD commands retain their behavior and formats.

Add a read-only get_capabilities command. Its versioned response advertises wardrive_serial_v1 and available scan bands. WDG probes on connection, with a timeout and graceful unsupported result for older firmware and wdg_wifi_bridge.py. Do not infer support from the version number alone.

Use compact newline-delimited JSON records prefixed WDG:. Separate record kinds: capabilities, started, wifi, ble, stats, stopped, error. Every session record carries protocol version, session token and a monotonic sequence number. Keep text logs outside this prefix. Place the exact schema, bounds and examples in docs/SERIAL_WARDRIVE_PROTOCOL.md in both repositories.

Observation fields:
- Wi-Fi: BSSID, SSID bytes, channel/band, RSSI, available authentication information, capture uptime and age at emission.
- BLE: address plus address type, name bytes, RSSI, available manufacturer/tag metadata, capture uptime and age at emission.
- Encode arbitrary SSID/name bytes as bounded hex fields; decode for display on the host. This avoids invalid UTF-8, commas, quotes and embedded line breaks corrupting framing.
- Stats: separate observation counters for Wi-Fi/BLE, queue drops and current activity/uptime. Heartbeat every 2 seconds even in a quiet environment.
- started is emitted only after both collectors are ready. If either fails, report an error and unwind partial startup; do not silently label Wi-Fi-only capture as All.
- stopped is a final event after producers are disabled and pending data is drained within a bounded timeout or discarded with an explicit count. No observations from that session may follow it.

Suggested initial bounds to be measured: 64 queued observations, maximum 1024-byte input line, per-device repeat emission at most once per second, maximum observation age of 2 seconds before dropping/reporting rather than mis-geotagging. These are tunable limits, not field-performance claims.

## 2. Firmware: reuse capture, separate delivery from standalone logging

Primary files: ESP32C5/main/main.c; new ESP32C5/main/serial_wardrive.c and .h; ESP32C5/main/CMakeLists.txt; narrowly scoped supporting changes if required.

- Extract/reuse only the relevant beacon parsing, BLE advertisement parsing and Wi-Fi channel selection hooks. Share decoded observations through a sink interface so standalone SD mode and the new serial mode do not duplicate the radio implementation.
- New serial mode initializes both stacks through the existing coexistence path. It must not call ensure_ble_mode after Wi-Fi starts.
- It must not call init_sd_card, init_gps_uart, wait_for_gps_fix, or use current_gps validity to gate capture, re-observation or output. No GPS forwarding is required from WDG.
- Keep standalone GPS/SD wardriving, normal scan commands and their saved configuration intact. Serial mode explicitly selects all bands without rewriting saved standalone band/GPS settings or inheriting a hidden startup cooldown.
- In serial mode, deliver newly seen and repeated observations before any legacy 'already seen' return. Use a bounded, expiring rate-limit cache; a full historical dedup table must not stop discovery of new devices on a long drive. WDG remains the authority for persistent dedup/whitelisting.
- Radio callbacks copy fixed-size observations into a bounded queue and return promptly. Do not perform serial writes, SD access, blocking waits or per-event heap allocation in callbacks.
- One worker encodes and writes complete framed records. Serialize writes against normal console/log output sufficiently to prevent line interleaving. Apply bounded USB/serial write handling; backpressure drops data with visible counters instead of blocking radio tasks or growing memory indefinitely.
- Support global stop, repeated start, malformed arguments, initialization failure and serial client disappearance. A host keepalive every 5 seconds with a 15-second lease can stop an abandoned serial session when USB detach is not directly observable. Keepalive traffic contains no GPS.
- Starting a conflicting operation must not tear down a stack underneath active callbacks. Coordinate ownership, stop/join collectors, restore prior callbacks/band settings and release queues on every exit. Add new mode to existing busy checks and stop handling.
- Avoid changing attack features or introducing additional collection types. BLE means BLE discovery, not Classic Bluetooth scanning.

Initial firmware acceptance: with no ESP32 SD card and no GPS input, the new command starts both collectors, emits both types when signals are present, keeps emitting status when quiet, and cleanly returns to normal Wi-Fi/BLE scanning after stop.

## 3. WDG: parsing, host GPS and persistence

Primary files: new watchdogs/wardrive_protocol.py and watchdogs/scan_controller.py; watchdogs/serial_manager.py; focused edits to watchdogs/app.py, watchdogs/config.py and watchdogs/loot_manager.py.

- Route WDG: records to the new parser before the legacy text/CSV handlers. Keep legacy parsing intact. Validate schema/version, record kind, lengths, numeric bounds and session identity; reject malformed records without stopping the UI.
- Bound SerialLineBuffer growth and recover at a newline after oversized/truncated input. Clear parser/session state when the connection changes.
- Convert new records into the same normalized Wi-Fi/BLE events used by existing map, whitelist, XP and saving logic. Factor duplicated event handling out of the large app.py where practical rather than creating another independent loot path.
- GPS remains entirely on the uConsole. Keep a short history of actual GPS fixes with host monotonic timestamps. Estimate observation time using receipt time minus firmware-reported queue age; use an appropriately recent valid fix, never the manually positioned map or an old cached location.
- Explicitly treat serial transit time as residual uncertainty; do not claim exact clock synchronization. Bound buffering and expose late/dropped observations. Tests cover GPS-fix changes during delayed delivery.
- Retain WDG's current host-side GPS wait/cancel behavior when starting a wardrive. Once running, GPS loss does not stop the ESP32 collectors: show 'GPS unavailable', continue discovery/raw logging, and do not save falsely geotagged entries. Resume geotagging on fresh fixes. Do not retroactively assign an old observation a newly acquired location.
- Extend save functions narrowly to accept an explicit observation time/fix for the new mode, with existing callers retaining their current defaults. Use proper CSV escaping for new names/SSIDs. Dedup and repeat observations must not inflate map objects or award full new-device XP repeatedly.

## 4. WDG: All Wardrive and truthful status

- Add **All Wardrive** under SNIFF, shortcut **3** (currently unused there). Existing WiFi Wardrive and BT Wardrive stay available.
- All Wardrive starts one continuous firmware session; it must not enable either legacy auto-repeat loop.
- Model scan mode (none, Wi-Fi, BLE, all) separately from lifecycle (idle, starting, running, stopping, disconnected/error). Track requested state separately from confirmed activity. This is a focused scan controller, not a rewrite of every tool state.
- On any scan transition, cancel pending commands and clear the previous scan's completion/start timers before issuing the next request.
- For new mode, wait for started/stopped acknowledgments with timeout handling. For legacy transitions, wait for the strongest existing completion/stop signal; an unresolved timeout leaves a visible uncertain/error state instead of dispatching overlapping operations.
- Late stop text, stale observations, disconnects and reconnects must not re-enable old flags or cancel a new confirmed session. Bind new records to their session token; clear capabilities and active state on reconnect.
- Make S, STOP ALL, toggling All Wardrive off, and app exit share the same cleanup path. Reconnection must not silently resume an old session.
- Terminal title: ALL WARDRIVE. HUD/menu state: WiFi + BLE plus Starting/Running/Stopping as appropriate. Drive these from the same controller so no display silently prioritizes Wi-Fi over BLE.
- Distinguish cumulative unique discoveries from fresh activity. Show per-protocol last-seen age and a visible warning if heartbeats stop or queue drops occur. A lack of nearby BLE advertisements alone is not a scanner-failure signal.
- Older/unsupported firmware: show the option with a clear explanation that updated firmware is required; do not send a speculative start command or show a false running state. Other modes remain usable.

## 5. Verification and build artifacts

Offline automated checks (synthetic observations only):
- Firmware framing and limits: escaped/binary labels, both record kinds, timestamps, overflow/drop counts and orderly stop output.
- WDG parser: split/combined reads, CRLF, malformed/oversized lines, unknown versions, unrelated logs, stale sessions and capability timeouts.
- Scan state transitions: Wi-Fi -> BLE -> All -> stop; All -> legacy; stop during start; delayed legacy stop replies; failure of one collector; disconnect/reconnect; S and STOP ALL. Assert no legacy rescan while All is active.
- Host GPS/loot: fix loss/recovery, delayed records crossing a location change, dedup, whitelist behavior, correct WIFI/BLE types, RSSI updates and CSV escaping. No imaginary coordinates.
- Simulated serial/PTY integration: one stream supplies both protocols continuously; UI state and resulting saved observations agree. Mock unrelated hardware/attack modules; no actual scanning is needed for these tests.

Build both XIAO and standard ESP32-C5 variants using the existing ESP-IDF 6.0.1 configuration, with clean separate build directories to avoid overlay leakage. Confirm image/partition fit and record baseline/changed memory use. Run Python compilation and focused automated tests. Check menu/HUD layout at 640x360 with simulated data, including all transitions and long counters.

Prefer local builds. If a fork CI workflow is needed, add a build-only feature-branch workflow that uploads artifacts, not releases, notifications, device actions or upstream changes. Existing upstream workflows have release and notification behavior and do not automatically cover this branch; do not run them indiscriminately.

Deliver firmware artifacts separately from source history, with checksums, commit IDs, board identity, build configuration and logs. No firmware flashing in this phase. Document that WDG's existing firmware flasher currently points to LOCOSP release assets; those assets will not contain the new feature until published there. Do not silently use them as if they were fork builds.

Later hardware validation, clearly separate from offline completion: XIAO with no SD/GPS, repeated start/stop and cable replug, sparse/dense environments, 2.4/5 GHz coverage, and at least a 30-minute combined soak. Compare Wi-Fi-only, BLE-only and combined discovery/counter behavior under comparable conditions; collect memory/drop/backpressure evidence. If sniffer coexistence is inadequate, evaluate STA-scan coexistence or host BLE rather than misrepresenting alternating scans as combined capture.

## 6. Commit and documentation sequence

1. docs: record the coordinated All Wardrive plan (this setup phase; both forks).
2. Firmware: protocol contract + serial coexistence capture + lifecycle tests.
3. WDG: protocol handling + host geotagging + parser/persistence tests.
4. WDG: All Wardrive menu + scan-state/display fix + transition tests.
5. Both: integration adjustments, build/test documentation and artifact provenance.

Commit coherent, reviewable changes and push each validated checkpoint to origin/feature/all-wardrive. Verify the remote SHA after pushes. Do not rewrite source history, force-push shared branches, commit real scan logs/secrets, create upstream PRs or merge into upstream. Keep this document updated if implementation changes a design decision.

## Completion criteria

The forks contain committed/pushed source and documentation, supported firmware can stream Wi-Fi and BLE without ESP32 GPS/SD dependencies, WDG provides All Wardrive with host GPS/storage and accurate state, focused tests pass, both firmware variants build, and all unperformed hardware checks are explicitly reported. Field reliability is not claimed until the separate device tests pass.
