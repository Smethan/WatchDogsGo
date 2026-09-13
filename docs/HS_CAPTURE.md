# HS Capture progress screens

In either **SNIFF** or **ATTACK**, selecting **HS Capture** or **HS Capture no SD**
opens a progress screen. It does not immediately start capture.

- **Enter** starts the displayed variant, stopping any previous ESP32 operation
  and waiting for its final stop acknowledgement first.
- **S** stops this capture, or cancels its pending start. In the no-SD variant,
  wait for the serial file transfer and cleanup to finish before unplugging.
- **Escape / Tab** returns to the map. Capture keeps running while WDG remains
  open; reopen the same menu item to check its progress.
- **Up / Down** scrolls AP/client rows and shows full addresses below the table.

Results remain visible after stopping. SD and no-SD results are separate; starting
a new run of a variant clears that variant's previous display.

These are the existing **active** capture commands, including their existing
deauthentication behavior. [HS Sniff](PASSIVE_HS_SNIFF.md) is the passive option.
The original SD capture retains its warning triangle and SD requirement.

## Updates and storage

Use **WDG 0.9.18+** and **Smethan projectZero firmware 1.7.4+** for the live
PMKID/M1/M2/M3/M4 table. Update WDG through SYSTEM → Update WDG and restart;
flash the correct board through SYSTEM → Flash ESP32. The XIAO ESP32-C5 requires
the XIAO image.

| Mode | Capture storage | Live display |
| --- | --- | --- |
| HS Capture | ESP32 SD card, `/lab/handshakes/` | Serial progress copies |
| HS Capture no SD | Existing serial PCAP/HCCAPX dump to the uConsole loot session on stop | Serial progress copies |

Progress copies do not create a second PCAP or change when the existing capture
files are saved. No-SD capture still keeps its capture data on the ESP32 until
the existing file dump runs; the table does not make it crash-persistent.

With older firmware, capture still works. WDG displays any existing M1–M4
log sightings under their AP, with unknown client/channel/RSSI. **PMKID: N/A**
means live PMKID reporting is unavailable, not that none were captured. Selected
network mode on older firmware may not provide these coarse M-number logs.

## What the counts mean

The table counts EAPOL packets and distinct observed, unencrypted PMKIDs per
AP/client. PMKIDs are parsed from RSN elements and EAPOL-Key PMKID KDEs, using
the same parser as HS Sniff. Existing firmware capture filters and deduplication
still determine which frames reach the PCAP and progress stream.

**Packet counts only; matching handshake pairs are not checked.** A row with
all four M columns populated can include separate connection attempts or
retransmissions. The screen does not correlate replay counters and nonces to
certify a matching message pair, check a password, or verify a MIC. Some usable
EAPOL captures need only a matching pair; a PMKID is a separate capture type.
See the [Hashcat message-pair format](https://hashcat.net/wiki/doku.php?id=hccapx)
and [WPA-PBKDF2-PMKID+EAPOL documentation](https://hashcat.net/wiki/doku.php?id=cracking_wpawpa2).

The active capture PCAP observer does not receive channel/RSSI metadata. These
columns show **--** instead of guessed values. SSIDs remain unknown unless
SSID-bearing context was forwarded.

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
the original frame was saved in the ESP32's PCAP.

**Drops** reports firmware progress queue/output/age losses; **Gaps** counts
missing progress sequence positions, and **Incomplete** counts abandoned
partial frames. They overlap and should not be added as unique RF packet loss.
More than seven seconds without progress produces a note, not an automatic
capture stop. Disconnects and final cleanup stop the local running indicator.

Native firmware tests cover framing, bounded queues, storage identity, cleanup
and allocation failure. Synthetic WDG tests cover malformed messages, chunk
loss, M-number/PMKID parsing, client separation, older firmware, menu navigation,
mode switches, disconnects and preservation of the existing serial file dump.
Both ESP32-C5 release variants build. These checks do not establish RF capture
completeness or hardware runtime stability.
