# Passive HS Sniff (no ESP32 SD card)

SNIFF → **HS Sniff** opens a dedicated status screen. **Enter** starts capture,
**S** stops it, and **Escape/Tab** returns to the map without stopping. Reopening
the screen retains the live results. The capture runs independently of screen
visibility while WDG remains open. Stopped results remain visible until a new
capture starts. Use Up/Down to inspect rows and scroll.

The mode listens for EAPOL and management frames, including visible
PMKIDs, and streams the packets to WDG over serial. It never invokes either
existing active handshake command. The original **HS Capture** now has a warning
triangle and the note **Requires SD card on ESP32**, in both menu locations.
Neither **HS Capture no SD** nor **HS Sniff** gets that SD warning.

The active modes also have [HS Capture progress screens](HS_CAPTURE.md) as of
WDG 0.9.18, with live PMKID/M-number progress supplied by firmware 1.7.4+.

## Firmware requirement

This needs the fork firmware advertising `hs_sniff_serial_v1: true`.
See [serial packet extension](PASSIVE_HS_PROTOCOL.md) for the wire format.
The command is `start_hs_sniff_serial TOKEN`. The old `start_sniffer` only reports
inventory; `start_pcap radio` writes to the ESP32's SD; and
`start_handshake_serial` performs deauths. The internal passive handshake enum
alone does not expose a passive no-SD console operation. A WDG-only alias to
these commands would not provide this feature.

The new serial mode uses WIFI_MODE_NULL with management/data reception and
channel hopping. It does not start BLE discovery, a Wi-Fi scan, associations,
probe requests, or deauthentication. It shares the serial session owner with
All Wardrive, so they run separately. Selecting another mode first stops the
current session and waits for its final acknowledgement.

## Capture files and counters

WDG saves `handshakes/passive_<timestamp>.pcapng` under the current loot session
on the uConsole. The capture uses IEEE 802.11 radiotap (link type 127), without
the FCS, and records the channel and RSSI reported by the ESP32. Packets are
timestamped using host receipt time minus firmware-reported age; this includes
residual serial delay. No GPS fix is required. New captures do not also create
a classic PCAP copy.

A same-named `.jsonl` records EAPOL message classifications and observed,
unencrypted PMKIDs with AP/client MACs, channel, RSSI and the SSID when known.
PMKIDs are recognized in RSN information
elements and RSN EAPOL-Key PMKID KDEs. Encrypted key data is not decoded. An
absent SSID remains unknown. The PCAPNG retains the original packets for later
analysis; this mode does not generate HCCAPX or claim a complete handshake.

The display counts **EAPOL frames** and distinct observed **PMKIDs**. Each AP/client
row shows PMKID and M1/M2/M3/M4 counts, channel, RSSI and last-seen age. Rows without
a client address indicate that the address was not available in the observed
frame. Group rekeys/error/request key frames do not fill M1-M4 columns. Counts
include retransmissions and messages seen across this session; they are not
validation that all four messages belong to one exchange. Rows are bounded to
512 and address-to-SSID context to 1024 entries.

Receiving
one EAPOL frame does not establish a complete or usable four-way handshake and
does not award the completed-handshake game event. PMKIDs only appear if a nearby
exchange actually includes one while the radio is listening on that channel.

## Reliability and limits

Frames are bounded to 2304 bytes and sent in 240-byte hex chunks inside bounded
`WDG:` records. Firmware allocates an eight-frame queue only during HS Sniff;
callbacks do no serial I/O or allocation. Repeated beacons/probe responses are
throttled to preserve serial capacity. EAPOL and association frames are not
deduplicated by the firmware. Unsupported/oversized or aged queued frames can be
dropped and are counted. Channel hopping can miss part or all of an exchange.

WDG accepts only complete, correctly sequenced packets from the current session;
partial/corrupt packets never become PCAPNG records. Complete writes are flushed,
synced periodically and synced on close. A storage error stops capture.
Use Stop and wait for completion before unplugging to allow the bounded queue
drain. Forced exit/cable removal can lose packets still in transit.

Validation is offline: synthetic EAPOL/PMKID fixtures, malformed/encrypted inputs,
chunk loss, M1-M4/group-key classification, client separation, background screen
navigation, mode transitions and the firmware callback/queue/lease harness.
Compiled firmware is not a substitute for a hardware RF silence and capture test.
