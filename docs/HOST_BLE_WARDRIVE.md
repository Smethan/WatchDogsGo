# All Wardrive and ESP Dual Test

WDG 0.9.27 has three distinct modes under SNIFF. All Wardrive defaults to the
ESP32 for both radios; host Bluetooth is an explicit separate option. Firmware
1.7.10 adds the preferred ten-second batch transport; older firmware keeps the
streaming fallback.

WDG 0.9.30 disables cellular mast collection in both All Wardrive modes after
continued whole-uConsole crashes. Neither mode calls ModemManager, launches
qmicli, inspects cellular devices, or opens a modem port. Historical cellular
rows remain readable. See [the suspended cellular implementation
notes](CELLULAR_STABILITY.md).

| Mode | Wi-Fi radio | BLE radio | Firmware required |
| --- | --- | --- | --- |
| All Wardrive (6) | ESP32, ten-second batch | ESP32, same batch | 1.7.10 preferred; v1 fallback |
| All Wardrive (host BLE) (9) | ESP32, ten-second batch | uConsole/BlueZ, continuous | 1.7.10 preferred; 1.7.3 fallback |
| ESP Dual Test (8) | ESP32, ten-second batch | ESP32, same batch | 1.7.10 preferred; v1 fallback |

## Why change it?

Espressif marks C5 Wi-Fi sniffer plus BLE coexistence as supported with unstable
performance. That does not prove a given timeout was a firmware crash. WDG also
had a separate false-timeout path: only started/stats messages renewed its
seven-second watchdog, even while valid discovery records were arriving.
Version 0.9.27 tracks control heartbeats and ESP observations independently.
After six seconds without control it asks `wardrive_status` for the current
phase. It stops after 15 seconds only if both control and ESP records are absent.
Stale sessions, duplicate sequences, malformed records, host BLE observations
and cell measurements cannot keep an unresponsive ESP32 session alive. The
firmware's 15-second host lease and five-second WDG keepalives remain in effect.

Reference: https://docs.espressif.com/projects/esp-idf/en/v6.0.1/esp32c5/api-guides/coexist.html

## Test before reflashing

1. Update WDG with SYSTEM → Update WDG or `bash update.sh`, then restart.
2. Select SNIFF → ESP Dual Test (8). Both scans stay on the ESP32; no host
   Bluetooth is started. Use the same area and conditions that caused timeouts.
3. Watch the overlay: `ctl` is the age of the last firmware control frame;
   `data` is the age of the last ESP32 observation, and `probes` counts explicit
   state queries. `gaps` counts
   missing record sequence numbers, not all over-the-air packet loss. Its
   percentage is missing sequence positions divided by the latest accepted
   sequence number, over the whole session so far.
4. A late heartbeat produces a status query rather than an immediate stop. If
   both control and ESP observations stop for 15 seconds, WDG stops the scan.
   That indicates a stream interruption, which may be firmware, USB, host
   processing, or power. It does not alone prove a coexistence crash.
5. Stop with the normal STOP action. Timing snapshots are appended once per
   second and on state changes to `wardrive_diagnostics.jsonl` in the current
   loot session. The console prints its location. It includes counts, firmware
   stats, sequence gaps, invalid records, errors and false-timeout episodes.
   Serial disconnects are recorded before resetting the controller.

The test leaves the normal keepalive active and retains the real-silence
watchdog. It does not deliberately run an uncontrolled or disconnected scan.

## What gaps mean

A jump from sequence 100 to 104 adds three gaps: records 101–103 were not
accepted. This can include serial output loss/partial writes, framing problems,
or records rejected by WDG's validator. A gap does not identify a unique missed
network or device; repeated advertisements and Wi-Fi management/summary records
also have sequence numbers. Firmware queue overflow or stale observations
discarded before numbering contribute to firmware `drops` instead and may not
create sequence gaps. Intentional repeat suppression is not counted as a gap.

Gaps are a capture-completeness signal, not a crash indicator. A cumulative
count needs a denominator: 1,000 gaps in 100,000 positions is 1%, whereas 1,000
in 2,000 is 50%. These are examples, not safe/unsafe thresholds. Repeated
sightings can still discover devices despite some loss, but brief sightings or
signature evidence can be missed. Neither gaps nor drops measure all RF loss.
Compare timing logs, gap percentage, `invalid_records`, firmware drops and
discovery continuity when diagnosing sustained high loss.

## Switch to host Bluetooth

Flash firmware 1.7.3+ for the correct board, enable Bluetooth in the uConsole's
OS, and choose All Wardrive (host BLE) (9). The capability check prevents old firmware from
accidentally running ESP32 BLE as well. Firmware receives
`start_wardrive_wifi_batch_serial <session>` on 1.7.10, or the v1
`start_wardrive_wifi_serial <session>` fallback; Wi-Fi keeps the 2.4/5 GHz hopping and raw
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
