# HS Capture progress screens

In either **SNIFF** or **ATTACK**, selecting **HS Capture** or **HS Capture no SD**
opens a progress screen. It does not immediately start capture.

- **Enter** starts the displayed variant, stopping any previous ESP32 operation
  and waiting for its final stop acknowledgement first.
- **N** opens the optional network picker. **A** restores the original
  all-nearby scope without requiring a scan.
- **S** stops this capture, or cancels its pending start. In the no-SD variant,
  wait for the serial file transfer and cleanup to finish before unplugging.
- **Escape / Tab** returns to the map. Capture keeps running while WDG remains
  open; reopen the same menu item to check its progress.
- **Up / Down** scrolls AP/client rows and shows full addresses below the table.

Results remain visible after stopping. SD and no-SD results are separate; starting
a new run of a variant clears that variant's previous display.

These are the existing **active** capture commands, including their existing
deauthentication behavior. [HS Sniff](PASSIVE_HS_SNIFF.md) is the passive option.
The original SD capture retains its warning triangle and SD requirement. Neither
active capture mode needs GPS.

## Optional network selection

Press **N** from either capture screen, then use:

| Key | Action |
| --- | --- |
| **R** | Run a fresh nearby-network scan. This stops the current ESP32 operation and clears previous checks. |
| **F** or **/** | Edit the case-insensitive SSID/name filter; Enter or Escape finishes editing. |
| **G** | Cycle minimum RSSI through any, -100, -90, -80, -70, -60, -50 and -40 dBm. |
| **O** | Sort by strongest signal or by name. |
| **Space** | Check or uncheck the highlighted BSSID, up to 16 networks. |
| **C** | Clear the name/RSSI filters and all checks. |
| **Enter** | Apply the checked BSSIDs and return to the capture screen. Press Enter there to start. |
| **A** | Use the original all-nearby behavior and return to the capture screen. |
| **Escape / Tab** | Return to the capture screen without applying draft changes. |

Selections use BSSIDs, so duplicate or hidden SSIDs remain distinct. Checked
rows stay checked when a filter hides them. Open and WEP networks cannot be
selected because they have no WPA handshake to capture. WDG also refuses a
currently whitelisted BSSID; the firmware rechecks each selected BSSID against
the completed scan before it starts. SD and no-SD capture remember their applied
choices separately.

The scan is an immutable snapshot identified by a host token and expires after
five minutes. Starting another scan, losing the ESP32 connection, an incomplete
serial result, a missing/changed BSSID, an unsupported channel or an expired
token makes selected startup fail closed. WDG never converts a stale selection
to all-nearby capture. Once capture starts, its BSSID/channel set does not change.
The capture remains active if you leave either the picker or progress screen.

## Updates and storage

Use **WDG 0.9.25+** and **Smethan projectZero firmware 1.7.9+** for optional
network selection. The original all-nearby capture remains compatible with
older firmware; the live PMKID/M1/M2/M3/M4 table requires WDG 0.9.19+ and
firmware 1.7.4+. Update WDG through SYSTEM → Update WDG and restart; flash the
correct board through SYSTEM → Flash ESP32. The XIAO ESP32-C5 requires the XIAO
image.

Use **WDG 0.9.38+** with **firmware 1.7.12+** for PCAPNG-only capture storage.
The new format keeps each observed M1-M4 packet plus beacon, authentication and
association context when those frames were seen. It uses radiotap link type 127
to preserve ESP-reported channel/RSSI and strips the FCS consistently. WDG
validates every block and rebases the ESP boot-relative timestamps to wall time
before committing the host file. It does not create a duplicate classic PCAP.

| Mode | Capture storage | Live display |
| --- | --- | --- |
| HS Capture | ESP32 SD card, `/lab/handshakes/*.pcapng` | Serial progress copies |
| HS Capture no SD | Serial PCAPNG/HCCAPX dump to the uConsole loot session on stop | Serial progress copies |

Progress copies do not create a second capture file or change when the capture
files are saved. No-SD capture still keeps its capture data on the ESP32 until
the existing file dump runs; the table does not make it crash-persistent.
WDG 0.9.19 waits for final capture cleanup even if the firmware's general stop
acknowledgement arrives first, so a pending mode switch cannot start mid-dump.

Firmware 1.7.12 builds each stored artifact from one AP/station/replay exchange.
A matching M1+M2 or M2+M3 pair produces a `valid` PCAPNG and HCCAPX. A captured
association request can still produce a `pmkid` or `partial` PCAPNG at stop without
being labeled a valid EAPOL exchange. The SD mode waits up to two seconds after
the first validated pair, or saves immediately when all M1-M4 have arrived, so
later exchange messages can join the same artifact. It flushes remaining
association evidence at stop. The no-SD mode sends
its bounded per-network artifacts only after capture has stopped and the radio
callback has drained. Each serial artifact declares `VALID`, `PMKID` or
`PARTIAL` plus `CAPTURE_FORMAT: PCAPNG` before its blocks; WDG saves it only
when the final SSID/AP metadata
line commits the complete sequence. It requires a canonical final BSSID and
bounds/sanitizes both filename components before writing.

SYSTEM → Upload WPA-SEC prefers PCAPNG when a same-stem legacy PCAP is also
present and uploads only one copy. Old PCAP-only loot remains uploadable, but
firmware 1.7.12 and WDG 0.9.38 create only PCAPNG for new handshake and packet
captures. PCAPNG supplies useful capture context to WPA-Sec/hcxtools; it cannot
reconstruct authentication, association, probe or EAPOL packets that were not
heard over the air.

Successful uploads are recorded by SHA-256 in `loot/.wpasec_uploads.json`.
Later uploads skip matching content for the same WPA-sec account while retrying
failed or interrupted transfers. A renamed capture remains skipped; changed
capture bytes and a different WPA-sec API key are treated as new work. The
receipt file is replaced atomically after each confirmed HTTP success so a
crash cannot leave a partially written ledger.

With older firmware, all-nearby capture still works when the Wi-Fi whitelist is
empty. Firmware 1.7.11 advertises `hs_capture_exclusions_v1`; when one or more
Wi-Fi BSSIDs are whitelisted, WDG sends them as an immutable all-nearby
exclusion set for both capture destinations. Firmware rejects their frames at
the receive callback and checks the exclusion again before deauthentication.
WDG refuses to start all-nearby capture with a nonempty Wi-Fi whitelist when
that capability is unavailable, so protected networks are never silently sent
to older firmware. BLE whitelist entries do not affect Wi-Fi capture. Up to 32
Wi-Fi BSSIDs can be excluded; a larger or malformed list fails closed.

The picker requires the `hs_capture_targets_v1` capability and refuses selected
startup when it is not available. WDG displays any existing M1–M4
log sightings under their AP, with unknown client/channel/RSSI. **PMKID: N/A**
means live PMKID reporting is unavailable, not that none were captured. Selected
network mode on older firmware may not provide these coarse M-number logs.

## What the counts mean

The table counts EAPOL packets and distinct observed, unencrypted PMKIDs per
AP/client. PMKIDs are parsed from RSN elements and EAPOL-Key PMKID KDEs, using
the same parser as HS Sniff. Existing firmware capture filters and deduplication
still determine which frames reach the PCAPNG and progress stream.

**The UI shows packet sightings; it does not validate an exchange.** A row with
all four M columns populated can include separate connection attempts or
retransmissions. The screen does not correlate replay counters and nonces to
certify a matching message pair, check a password, or verify a MIC. Firmware
1.7.9 only labels a saved exchange complete/valid when the stored frames belong
to the same AP, station and normalized replay exchange. That artifact decision
does not promote the UI counters into validation. Some usable EAPOL captures
need only a matching pair; a PMKID is a separate capture type.
See the [Hashcat message-pair format](https://hashcat.net/wiki/doku.php?id=hccapx)
and [WPA-PBKDF2-PMKID+EAPOL documentation](https://hashcat.net/wiki/doku.php?id=cracking_wpawpa2).

The saved PCAPNG receives channel/RSSI metadata directly from the firmware
capture queue. The lower-bandwidth live progress stream still forwards only raw
802.11 bytes, so its table columns show **--** instead of guessed values. SSIDs
remain unknown unless SSID-bearing context was forwarded.

## Transport and limits

Firmware 1.7.4 reports `HSC:` JSON records independently of the wardrive/passive
`WDG:` transport. Version 1 status records contain `started`, `stats`, or `stopped`,
an uptime-derived session ID, increasing sequence number, `storage` (`sd` or
`serial`), and counters. `hs_packet` records carry at most 240 bytes as hex plus
packet ID, total, offset, capture uptime and age. Radio metadata is absent.

WDG binds progress to an expected capture/storage, rejects retired sessions and
non-increasing sequence numbers, and only parses complete, contiguous frames.
Frames are bounded to 2304 bytes, rows to 512, SSID context to 1024, and the PMKID
deduplication cache to 4096. Telemetry loss can undercount the display even when
the original frame was saved in the ESP32's PCAPNG.

**Drops** reports firmware progress queue/output/age losses; **Gaps** counts
missing progress sequence positions, and **Incomplete** counts abandoned
partial frames. They overlap and should not be added as unique RF packet loss.
More than seven seconds without progress produces a note, not an automatic
capture stop. Disconnects and final cleanup stop the local running indicator.

Native firmware tests cover framing, bounded queues, storage identity, cleanup,
allocation failure, target parsing/filtering, scan lifecycle and exchange
isolation. Synthetic WDG tests cover malformed messages, chunk loss,
M-number/PMKID parsing, client separation, target filtering/selection, older
firmware, menu navigation, mode switches, disconnects and preservation of the
existing serial file dump. Both ESP32-C5 release variants build. These checks do
not establish RF capture completeness or hardware runtime stability; targeted
capture has not been physically RF-validated for this release.
