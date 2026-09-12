# All Wardrive and ESP Dual Test

WDG 0.9.16 has two distinct modes under SNIFF:

| Mode | Wi-Fi radio | BLE radio | Firmware required |
| --- | --- | --- | --- |
| All Wardrive (6) | ESP32, continuous management capture | uConsole/BlueZ | 1.7.3 or newer |
| ESP Dual Test (8) | ESP32 | ESP32 | Existing `wardrive_serial_v1` firmware |

## Why change it?

Espressif marks C5 Wi-Fi sniffer plus BLE coexistence as supported with unstable
performance. That does not prove a given timeout was a firmware crash. WDG also
had a separate false-timeout path: only started/stats messages renewed its
seven-second watchdog, even while valid discovery records were arriving.
Now every validated record with the current session and a new sequence number
renews it. Stale sessions, duplicate sequences, malformed records and host BLE
observations cannot keep an unresponsive ESP32 session alive. The firmware's
15-second host lease and five-second WDG keepalives remain in effect.

Reference: https://docs.espressif.com/projects/esp-idf/en/v6.0.1/esp32c5/api-guides/coexist.html

## Test before reflashing

1. Update WDG with SYSTEM → Update WDG or `bash update.sh`, then restart.
2. Select SNIFF → ESP Dual Test (8). Both scans stay on the ESP32; no host
   Bluetooth is started. Use the same area and conditions that caused timeouts.
3. Watch the overlay: `stats` is the age of the last firmware status message;
   `data` is the age of the last accepted firmware record (including status).
   `false stops` counts episodes where the old stats-only watchdog would have
   fired while the new watchdog still had recent valid records. `gaps` counts
   missing record sequence numbers, not all over-the-air packet loss.
4. A false-stop warning with continuing discoveries demonstrates the old WDG
   watchdog could be mistaken. If all records stop for seven seconds, the scan
   stops normally; that indicates a stream interruption, which may be firmware,
   USB, host processing, or power. It does not alone prove a coexistence crash.
5. Stop with the normal STOP action. Timing snapshots are appended once per
   second and on state changes to `wardrive_diagnostics.jsonl` in the current
   loot session. The console prints its location. It includes counts, firmware
   stats, sequence gaps, invalid records, errors and false-timeout episodes.
   Serial disconnects are recorded before resetting the controller.

The test leaves the normal keepalive active and retains the real-silence
watchdog. It does not deliberately run an uncontrolled or disconnected scan.

## Switch to host Bluetooth

Flash firmware 1.7.3+ for the correct board, enable Bluetooth in the uConsole's
OS, and choose All Wardrive (6). The capability check prevents old firmware from
accidentally running ESP32 BLE as well. Firmware receives
`start_wardrive_wifi_serial <session>`; Wi-Fi keeps the 2.4/5 GHz hopping and raw
management observations used for probe/OUI/SSID matches, without ESP32 BLE scanning.

Bleak (already in requirements.txt) runs BlueZ discovery in a background thread
using the system-selected adapter. No adapter-wide reset, unpairing or power
change is performed. Missing/off adapters and startup permission errors are
reported and stop the Wi-Fi session too. A sparse BLE environment alone is not
treated as a failed scanner. The UI shows BLE startup/running state and separate
firmware/host queue drops. Changing mode, STOP, serial loss or quitting closes
the host scanner. A new scan refuses to start until the previous scanner closes.

BlueZ supplies parsed advertising fields, which can combine advertisements and
scan responses. WDG reconstructs AD fields for its existing name, manufacturer,
service UUID and service-data matching. These are normalized observations, not
raw packet captures; fields longer than a single AD element are omitted. Public
vs random address type is retained when provided, and unknown types do not
generate manufacturer OUI matches. Ordinary markers, precise Flock/Axon markers,
purple/orange alerts, WiGLE saves and GPS trails continue through the same code.

Firmware builds and mocked scanner/transport tests have passed. Actual uConsole
Bluetooth discovery, simultaneous radio reception and a prolonged field soak
still need hardware validation. Neither mode guarantees complete reception.
