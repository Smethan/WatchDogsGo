# All Wardrive, notable detections and route trail plan

Status: planned, not implemented. Repository setup and this document are complete.
Date: 2026-09-12. Expanded on 2026-09-12 with Flock/Axon detection, configurable notable-marker placement and optional route recording. Ordinary marker placement remains unchanged.

## Objective and ownership

Add combined 2.4/5 GHz Wi-Fi plus BLE discovery to LOCOSP-derived projectZero, stream observations over USB/serial without requiring GPS or an SD card on the ESP32, and add **All Wardrive** to Watch Dogs Go. The uConsole owns GPS, map placement, deduplication, XP and file storage. Also fix misleading scan status and competing scan timers. Add Flock and Axon signature detection with colored alerts and configurable observation-location markers, plus an optional host-GPS wardrive trail.

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

Use compact newline-delimited JSON records prefixed WDG:. Separate record kinds: capabilities, started, wifi, wifi_mgmt, ble, stats, stopped, error. The wifi_mgmt kind carries the bounded management-frame metadata needed for Flock matching; it is not an AP inventory record. Every session record carries protocol version, session token and a monotonic sequence number. Keep text logs outside this prefix. Place the exact schema, bounds and examples in docs/SERIAL_WARDRIVE_PROTOCOL.md in both repositories.

Observation fields:
- Wi-Fi: BSSID, SSID bytes, channel/band, RSSI, available authentication information, capture uptime and age at emission.
- BLE: address plus address type, full available name bytes (before UI truncation), RSSI, advertisement versus scan-response event type, bounded raw advertisement/service/manufacturer bytes, capture uptime and age at emission. Include payload truncation flags and collection capability details. Preserve all manufacturer-data sections and service UUID/service-data fields for host-side detection rather than only a preselected company ID. Current legacy advertisements fit in separate bounded records; this does not imply extended-advertising support.
- Wi-Fi management metadata: actual frame type/subtype, transmitter/receiver addresses and their roles, BSSID only where applicable, SSID bytes with absent versus empty distinction, channel and RSSI. Capture beacons, probe requests and probe responses needed by the documented Flock rules. No data-payload collection is needed for the initial scope. Advertise this capability separately; do not classify missing fields as negative evidence.
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
- Avoid changing attack features. The only expansion beyond AP/BLE inventory is bounded Wi-Fi management metadata and BLE advertisement content needed for Flock/Axon signatures. Classifying these observations does not connect to devices, inspect GATT, inject packets or trigger attacks. Existing BLE active scanning may request scan responses; do not describe it as zero-transmission passive reception. BLE means BLE discovery, not Classic Bluetooth scanning.

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

## 5. Flock and Axon classification on the uConsole

Primary additions: watchdogs/notable_detector.py, watchdogs/data/notable_signatures.json, docs/NOTABLE_DETECTIONS.md, synthetic test fixtures, and narrow persistence/protocol integration. Firmware transports evidence; WDG performs the matching so rules can be corrected without reflashing the ESP32. All Wardrive includes the classifier; existing Wi-Fi/BLE modes can use only the evidence they actually provide, with their limitations shown.

Scope is Flock and Axon only. Biscuit is a reference for behavior, not a claim of exact rule-table parity; its complete private rules are not available from its documentation. No follower detection, camera access, automatic reporting, Meta/drone detection or other Biscuit tools are included.

Rule design and provenance:
- Maintain a small, versioned, data-driven ruleset with stable rule IDs, category, product specificity, match method, evidence strength, source URL, checked date, and source/license provenance. Use public facts and independently written matchers; review licenses before copying any source or signature database. Do not import every broadly shared OEM prefix as a camera signature.
- Flock: supported Wi-Fi OUI/SSID patterns, distinctive BLE local names, manufacturer/service payload patterns, and documented Wi-Fi wildcard-probe combinations. Biscuit documents names such as Penguin, Flock, pigvision and FS Ext Battery and a manufacturer-ID clue 0x09C8. Treat these as candidates requiring precise field/pattern validation, not loose substring searches through arbitrary packet bytes.
- 0x09C8 identifies an OEM (XUNTONG) associated with some Flock hardware. Company-ID-only detection is not proof of a Flock camera. Names can be spoofed and shared OEM OUIs create false positives. Numeric-looking names alone are low-evidence candidates and should not spam alerts by default.
- Axon: evaluate the documented Axon/TASER OUI 00:25:DF, BLE company ID 0x034D, service UUID 0xFC81, and the BWCDEVICE service-data signature. Validate exact AD structure, byte order and signature context against primary registry entries and the published research during implementation. Match the full available payload before name truncation.
- A vendor-only match is 'Possible Axon device', not a confirmed body camera; Axon sells other equipment. Body-camera-specific wording requires an appropriate signature. Exclude unrelated Axon Networks company prefixes. Wi-Fi-only Axon matching, if retained, remains a vendor-level candidate; BLE provides the more specific camera evidence.
- Join advertisement and scan-response evidence only for the same address/type within a bounded time window. New name/payload evidence must bypass duplicate-only emission suppression so a later scan response can upgrade a match. Keep this cache bounded and discard stale evidence on session changes.
- Respect BLE address type and Wi-Fi multicast/locally administered bits when applying OUI rules. Payload matching still works with rotating addresses. Do not merge rotating addresses into a claimed persistent person/device identity without evidence.
- Match wifi_mgmt addresses by their real role. Receiver-address matches remain weak evidence of a referenced device, never proof that the transmitter is the camera. Do not fabricate a BSSID or WiGLE AP row for a probe-only observation. Matching addresses must also pass the user's whitelist checks.
- Return all supporting rule IDs/evidence and the strongest justified category, with labels such as Possible Flock, Flock signature match, Possible Axon device, and Axon body-camera signature. Evidence grades are descriptive, not probability percentages or physical confirmation. Record rule conflicts rather than arbitrarily choosing a brand.
- Preserve original Wi-Fi/BLE inventory records. Save classification details separately in the session, including first/last observation, method, evidence, RSSI and valid host GPS/accuracy if available. Keep standard WiGLE types WIFI/BLE; do not encode Flock/Axon as new network types or automatically upload camera locations.
- Add independent Flock and Axon detection toggles, enabled by default for new wardrive sessions. Reuse whitelisting and allow local suppression of a noisy signature/device without altering ordinary scan capture. Coalesce duplicate alerts; re-alert only after a configurable absence interval or a meaningful evidence upgrade.

References inspected during planning:
- Biscuit method families and Axon behavior: https://codehedge.github.io/Biscuit-Wiki/features/bluetooth-scanning.html
- Biscuit protocol/method examples: https://codehedge.github.io/Biscuit-Wiki/3rd-party-integration/wardrive.html
- Published Axon signature research and product-specificity caveats: https://github.com/soyboi1312/all-cameras-are-beacons/blob/main/docs/axon.md
- Published manufacturer/service and Wi-Fi matching research: https://github.com/colonelpanichacks/oui-spy-unified-blue/blob/master/README.md

Implementation deliverable: a documented coverage table listing which rule families work with All Wardrive versus the legacy scan formats, and which public signatures have been verified. Do not advertise unavailable Biscuit signatures as implemented.

## 6. Colored alerts and configurable Flock/Axon marker positions

Confirmed current behavior: app.py creates live Wi-Fi/BLE markers at player_lat/player_lon plus random offsets of up to 0.001 degrees per axis. _draw_radar projects those stored positions. Discovered devices are real, but the positions are decorative; they do not reveal the emitter's location or direction. Historical loot points use saved host GPS locations. The user explicitly wants ordinary Wi-Fi/BLE marker behavior left as-is; accurate placement applies only to Flock/Axon markers and can be toggled.

- Use **purple Flock pop-ups** and **orange Axon pop-ups**, with named palette constants and readable text at 640x360. Show category, evidence/method, a short identity/name, RSSI and GPS availability; put longer evidence in a details/list view. Never display a vendor-only candidate simply as a confirmed camera.
- Show a queued, deduplicated notification overlay above both map and menus. Share screen-space arbitration with existing MeshCore toasts so simultaneous messages remain accessible and do not overlap or disappear behind a submenu. Detection notifications are independent of scan status messages.
- Per the user's request, both categories use **purple dots on the minimap**. Use F/A labels where space permits and category/evidence in the details view; retain the distinct purple/orange pop-up colors. Use matching purple notable markers on the main map for consistency. Normal inventory colors remain available for other devices.
- Leave ordinary Wi-Fi/BLE markers, their random offsets, and the existing radar's general game-style behavior unchanged. Add **Precise Flock/Axon Markers ON/OFF**, default ON. When ON, Flock/Axon dots use the valid uConsole GPS fix at observation time, on both the main map and minimap. When OFF, only their visual placement follows the existing game-style scatter. The toggle must never change stored observation coordinates. Describe precise positions as 'heard here', not measured camera positions, bearings or RSSI-derived distances; no localization or triangulation is claimed.
- Promote the matched inventory marker to the notable style rather than adding a misleading duplicate for the same observed identity. Keep actual observation coordinates separate from display coordinates; do not merge Wi-Fi and BLE identities solely because their vendor or name matches.
- Separate first/best/last observation metadata instead of mutating a supposed fixed camera position. Live markers use last valid observation location and age; history retains recorded observations. Previously heard mobile Axon devices are not implied to remain at an old point.
- GPS-invalid detections still alert and appear in a list with location unavailable, but get no geographic dot until a later valid observation. Never use manually panned coordinates or (0,0) as a fake location. Valid locations on the equator/prime meridian must not be rejected just for a zero component.
- Render notable dots through the existing main-map/minimap projection. Do not overhaul the general radar scale, orientation or ordinary marker rendering. In precise mode, a dot marks the observer's recorded location when the signal was heard and cannot point toward the emitting camera. Ensure center/player rendering does not completely hide a notable dot at the observer's current location; an outline/count/cluster treatment can preserve visibility without changing its geographic position.
- For Flock/Axon dots only, maintain a recent-observation window (initial default 60 seconds) for bright live detections. Fade/mark older notable observations as history and retain saved detection details. Leave ordinary marker age/rendering behavior unchanged. Prioritize notable dots in overlaps without geographic displacement when precise placement is ON.
- Reuse the existing cluster/details UI where practical and extend it with Flock/Axon evidence and last-seen information. Do not redesign unrelated game screens.

## 7. Optional GPS wardrive trail

No GPS breadcrumb/route-trail recorder or renderer was found in WDG's current watchdogs/ or plugins/ sources. projectZero's standalone KML mode is unrelated to the host-only design and will not be used.

Primary addition: watchdogs/wardrive_trail.py plus focused app, settings and session-storage integration.

- Add a persistent **Wardrive Trail ON/OFF** setting under SNIFF wardrive settings, default OFF. When enabled, record and display the uConsole's traveled path during Wi-Fi, BLE or All wardriving. Turning it off stops adding points and hides the overlay without deleting the saved trail; turning it back on begins a new segment.
- Sample fresh host GPS fixes, independent of whether new devices were discovered. Do not sample the player/camera position: its existing roughly 30 m movement filter and manual panning are unsuitable for a route.
- Store timestamped latitude/longitude, available altitude/accuracy and segment boundaries. Initial sampling target: up to 1 point per second, filtering stationary jitter using accuracy-aware distance and a time threshold; retain meaningful turns. Validate fresh-fix timestamps so a frozen receiver does not add false movement or retain false location validity.
- Start new segments on GPS loss, stale fixes, implausible jumps, user pause/stop, disconnect gaps and session changes. Do not draw a straight line across unknown travel, bridge separate sessions, or record menu navigation as movement. Re-entering a scan mode may continue the same saved session only with an explicit segment boundary.
- Draw a thin cyan route underneath discoveries/player markers on the main map, and its clipped visible portion on the minimap. Detection purple/orange remains visually distinct. Save the current session route alongside loot with periodic flushes and recovery of complete records after interruption.
- Keep rendering cost bounded through viewport clipping and display-only simplification; retain the saved observations rather than silently truncating long routes. Reopen the matching saved route when its session is loaded. No new server sync is required.
- Record only while the setting is enabled and wardriving is active. Route storage, display and any future export remain on the uConsole; the firmware receives no GPS. Automatic uploading and a full standalone navigation system are outside this change.

## 8. Verification and build artifacts

Offline automated checks (synthetic observations only):
- Firmware framing and limits: escaped/binary labels, both record kinds, timestamps, overflow/drop counts and orderly stop output.
- WDG parser: split/combined reads, CRLF, malformed/oversized lines, unknown versions, unrelated logs, stale sessions and capability timeouts.
- Scan state transitions: Wi-Fi -> BLE -> All -> stop; All -> legacy; stop during start; delayed legacy stop replies; failure of one collector; disconnect/reconnect; S and STOP ALL. Assert no legacy rescan while All is active.
- Host GPS/loot: fix loss/recovery, delayed records crossing a location change, dedup, whitelist behavior, correct WIFI/BLE types, RSSI updates and CSV escaping. No imaginary coordinates.
- Detector fixtures: positives for each supported rule family and negative lookalikes/shared OEM IDs; randomized MACs; wrong address roles; malformed/truncated AD structures; multiple manufacturer sections; repeated/scan-response updates; conflict handling; whitelisting; vendor-versus-camera wording; and bounded alert rates. Use synthetic or publishable fixtures with provenance, not private scan captures.
- Map/notification checks: ordinary Wi-Fi/BLE marker placement remains unchanged; precise Flock/Axon markers default ON, toggle switches only their display placement, saved coordinates remain accurate in both settings; purple dots for both categories, purple/orange pop-ups above menus, MeshCore notification coexistence, overlapping center dots, stale/history fading, no-GPS observations, saved-history classification, and active-versus-total counts.
- Trail checks: route follows fresh GPS with zero discoveries, toggling, loss/recovery/stale fix gaps, stationary jitter, manual pan immunity, turn retention, long-session memory/render bounds, crash recovery, reload and no crossing of unrelated segments. Include an entirely simulated moving route for 640x360 visual validation.
- Simulated serial/PTY integration: one stream supplies both protocols continuously; UI state and resulting saved observations agree. Mock unrelated hardware/attack modules; no actual scanning is needed for these tests.

Build both XIAO and standard ESP32-C5 variants using the existing ESP-IDF 6.0.1 configuration, with clean separate build directories to avoid overlay leakage. Confirm image/partition fit and record baseline/changed memory use. Run Python compilation and focused automated tests. Check menu/HUD layout at 640x360 with simulated data, including all transitions and long counters.

Prefer local builds. If a fork CI workflow is needed, add a build-only feature-branch workflow that uploads artifacts, not releases, notifications, device actions or upstream changes. Existing upstream workflows have release and notification behavior and do not automatically cover this branch; do not run them indiscriminately.

Deliver firmware artifacts separately from source history, with checksums, commit IDs, board identity, build configuration and logs. No firmware flashing in this phase. Document that WDG's existing firmware flasher currently points to LOCOSP release assets; those assets will not contain the new feature until published there. Do not silently use them as if they were fork builds.

Later hardware validation, clearly separate from offline completion: XIAO with no SD/GPS, repeated start/stop and cable replug, sparse/dense environments, 2.4/5 GHz coverage, and at least a 30-minute combined soak. Compare Wi-Fi-only, BLE-only and combined discovery/counter behavior under comparable conditions; collect memory/drop/backpressure evidence. If sniffer coexistence is inadequate, evaluate STA-scan coexistence or host BLE rather than misrepresenting alternating scans as combined capture.

## 9. Commit and documentation sequence

1. docs: record the coordinated All Wardrive plan (this setup phase; both forks).
2. Firmware: protocol contract + serial coexistence capture + lifecycle tests.
3. WDG: protocol handling + host geotagging + parser/persistence tests.
4. WDG: All Wardrive menu + scan-state/display fix + transition tests.
5. Both: extended observation metadata; WDG signature rules, detection persistence and colored alerts.
6. WDG: configurable precise Flock/Axon placement and notable history, plus optional GPS trail; retain ordinary marker behavior.
7. Both: integration adjustments, build/test documentation, coverage/provenance and firmware artifacts.

Commit coherent, reviewable changes and push each validated checkpoint to origin/feature/all-wardrive. Verify the remote SHA after pushes. Do not rewrite source history, force-push shared branches, commit real scan logs/secrets, create upstream PRs or merge into upstream. Keep this document updated if implementation changes a design decision.

## Completion criteria

The forks contain committed/pushed source and documentation, supported firmware can stream Wi-Fi and BLE without ESP32 GPS/SD dependencies, WDG provides All Wardrive with host GPS/storage and accurate state, Flock/Axon matching with evidence-aware colored alerts and purple dots with configurable precise placement works while ordinary marker placement stays unchanged, the optional segmented GPS trail records/displays/reloads correctly, focused tests pass, both firmware variants build, and all unperformed hardware checks are explicitly reported. Field reliability is not claimed until the separate device tests pass.
