## Passive handshake/PMKID serial extension

`hs_sniff_serial_v1: true` advertises `start_hs_sniff_serial TOKEN`. This selects
a separate passive Wi-Fi capture mode using the same owner, session, keepalive,
status and stop protocol. It uses WIFI_MODE_NULL, no BLE scanning or Wi-Fi
transmission commands, and requires neither GPS nor SD. It captures unencrypted
EAPOL plus association/reassociation requests/responses, beacons and probe
responses for RSN PMKID and SSID context.

`hs_packet` records carry `packet` (monotonic per-session frame ID), `offset`,
`total`, `capture_ms`, `age_ms`, `channel`, `rssi`, and `data_hex`. Raw 802.11
packets have FCS removed. Maximum total is 2304 bytes. Chunks are 240 bytes except
the final chunk; offsets start at zero and increase by 240. Each chunk gets a
new session sequence number. Hosts discard incomplete or nonconsecutive frames.
No HS records are sent by All Wardrive, and no BLE/inventory records are sent
by HS Sniff. `wifi_count` counts queued raw frames in this mode; `ble_count` is 0.

The eight-frame queue is allocated for the session and released on cleanup.
The worker uses a 10 KB stack. Repeat beacons/probe responses with identical
BSSID/tagged fields are limited to one per ten seconds. The ordinary two-second
age limit applies. Stop disables reception, drains for up to 1.5 seconds plus
one in-progress frame, counts remaining drops, frees the queue and emits stopped.
WDG writes radiotap PCAPNG and identifies visible PMKIDs on the uConsole; receiving EAPOL
does not by itself establish handshake completeness.
