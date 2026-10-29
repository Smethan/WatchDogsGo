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
- **Left / Right** switches between the combined view and individual ESP32
  views when multiple managed ESP32s are capturing. With one capture ESP32,
  its identified view is shown directly.

Results remain visible after stopping. SD and no-SD results are separate; starting
a new run of a variant clears that variant's previous display.

On managed ESP32-C5 jobs, the capture screen keeps the selected target scope
and the observed AP/client, EAPOL and PMKID table visible. The combined view
groups rows by AP and client and adds packet sightings from the participating
capture ESP32s; the individual views show each device's own observations and
status. A passive helper is identified separately and does not contribute to
handshake tabs or totals. Repeated sightings on different ESP32s can appear in
the combined packet counts. These counts remain observations, not proof of a
validated exchange; use the saved capture artifacts for that distinction.

These are the existing **active** capture commands, including their existing
deauthentication behavior. [HS Sniff](PASSIVE_HS_SNIFF.md) is the passive option.
The original SD capture retains its warning triangle and SD requirement. Neither
active capture mode needs GPS.

## Confirmed channel 144 failure on firmware 1.8.5

The installed XIAO running projectZero 1.8.5 advertises channel 144, but a
managed handshake job on channel 144 alone fails with `channel_apply_failed`
and ESP-IDF error 258 (`ESP_ERR_INVALID_ARG`). The same device successfully
applies channels 140, 149 and 165. This matches the reported no-SD failure;
the driver rejects channel 144 even though firmware advertises it.

WatchDogsGo 0.10.6 omits channel 144 from targetless handshake plans when
firmware version is missing or older than 1.8.6. A targetless explicit channel
list containing 144 is rejected if any included device has missing or older
firmware, because that list would be sent to that device. A selected network on
channel 144 can instead be assigned to an eligible firmware 1.8.6-or-newer C5
that advertises the channel; planning fails before dispatch if no such device
can cover it. The projectZero 1.8.6 candidate in this release omits channel 144
from its metadata, so it cannot use channel 144. A future firmware build can
use it only when its metadata advertises the channel. Other supported channels
remain available.

The firmware 1.8.6 secondary-channel readback change is separate: it accepts an
SDK-selected 5 GHz secondary channel when the requested primary channel matches.
That check does not explain or fix the channel 144 driver invalid-argument error.

## Channel 144 evidence with firmware 1.8.7

projectZero 1.8.7 sets the US country at Wi-Fi initialization
and enables channel 144 only on the XIAO profile. WROOM and Zero remain
conservative, and US 2.4 GHz channels 12–14 remain excluded. A live direct
`packet_monitor` tune/readback on XIAO succeeded for US primary 144 with
`ESP_OK`. The installed XIAO candidate also completed a managed no-SD job on
144 alone and another across 6/140/144/149 with no errors, clean stop, and
ready recovery. Those jobs used synthetic selected BSSIDs with WatchDogsGo
0.10.6, so they do not establish a controlled AP/client handshake or PCAPNG
save. WatchDogsGo 0.10.7 uses capability-aware mixed-board scheduling so all
capable boards are retained; a physical two-board run remains unverified.

The 1.8.5 failure and 1.8.6 workaround above are historical.

## Reading channel numbers during managed capture

The progress screen labels the ESP32's **Radio hop** separately from the
network table. The hop is the channel currently applied by that ESP32; it can
reach 144 during an all-nearby sweep without assigning channel 144 to every
network. A network row's **CH** is the receive channel of its latest captured
EAPOL or PMKID frame. With multiple foreground ESP32s, the ALL tab merges
matching AP/client rows and uses the channel of the latest such frame; a
per-device tab shows that ESP32's own row. The optional **N** network picker
shows the channel reported by the completed Wi-Fi scan for each BSSID.

If a picker or evidence row reports 144 for an AP known to use another channel,
record that AP's BSSID and compare it with the saved capture or scan record
from the same run. A hop display alone cannot establish the AP's channel.

Firmware 1.8.8 also closes the managed BLE scan and its owned NimBLE host when
BLE-enabled Wardrive stops. A live XIAO diagnostic run of the preceding source
retained about 29 KiB of internal RAM after this stop; the next handshake task
then failed allocation. Wi-Fi-only Wardrive did not reproduce that failure.

On 2026-10-29, the XIAO 1.8.8 application-USB OTA candidate (build ID
`4bf86c248c4b409d`) passed a same-boot sequence of two handshake captures,
Wi-Fi-only Wardrive, another handshake, BLE-enabled Wardrive, and a final
handshake. Every job started, stopped, released its token, and returned ready
without errors. After the BLE Wardrive stop, internal free memory was about
46 KiB, and the following handshake worker started successfully. The active
handshake status crossed channel 144 and continued to 149 without a power
cycle.

A separate managed Wi-Fi scan on that build found 76 APs on channels 1, 6,
11, 40, 44, and 157, with no AP reported on 144. A bounded no-SD capture
received 81 frames on those same varied channels and stopped cleanly, but
contained no EAPOL or PMKID evidence row. Therefore the specific report that
all progress-screen network rows show 144 was not reproduced or disproved by
that run. A controlled AP/client exchange or a saved same-run row with its
packet evidence is still needed for that display claim.

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

With WDG 0.9.42 and projectZero 1.7.13, the picker adds three columns from the
latest completed **Pkt Sniffer** session: `CLI` is the number of unique client
links attributed to that BSSID, `PKTS` is the number of packets attributed to
that BSSID, and `PRB` is the number of unique station/SSID probe-request pairs
for the row's exact SSID. Probe requests do not carry a destination BSSID, so
`PRB` is deliberately SSID-associated rather than presented as BSSID evidence.
The header shows the sniffer's total received packets, total unique probe pairs
and sample age. A dash means no completed compatible sample is available; it
does not mean a measured zero. Both active capture variants and Evil Twin use
the same token-bound scan snapshot, while their capture/deauthentication
behavior remains otherwise unchanged.

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

Successful uploads and permanent capture rejections are recorded by SHA-256 in
`loot/.wpasec_uploads.json`. Successful receipts remain per-account. Permanent
capture rejections apply across accounts because identical unsupported or empty
capture bytes cannot become useful with a different key. Unsupported formats
and captures with no usable handshake, PMKID, or password data are permanent;
network errors, rate limits,
authentication failures, server errors, and interrupted transfers remain
retryable. A renamed capture remains skipped; changed capture bytes and a
different WPA-sec API key cause accepted captures to be considered for the new
account. The receipt file is replaced atomically after each decision so a crash
cannot leave a partially written ledger. Existing version-1 success receipts
remain valid.

WPA-sec upload considers every captured network; the WDG whitelist does not
filter this explicit export action. `.22000` files are local hashcat artifacts
and are never uploaded. Before sending a pending PCAP/PCAPNG, WDG accepts a
current nonempty companion `.22000` as prior hcxtools validation or runs
`hcxpcapngtool` with WPA-sec-compatible options. Empty containers and captures
that yield no crackable handshake or PMKID receive permanent local rejection
receipts rather than being revalidated or retried on every upload.

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

## When a managed capture stops with an error

`operation_failed` means the firmware stopped the job after a specific failure;
it is a cleanup result, not the underlying cause. For channel failures, WDG
keeps the first job error on the progress screen and records each error in
`~/.watchdogs/last_run.log` with the device, boot ID, job token, endpoint,
failure type, requested and actual channel, Wi-Fi mode, band, and SDK error.
If the generic `operation_failed (channel_apply_failed)` arrives first, a later
detailed channel error replaces it only when it belongs to the same device,
boot, and job. Storage errors and another device's errors are never replaced.
A later cleanup or `boot changed` message does not replace a specific reason.
The log is rotated to
`previous_run.log` on the next WDG launch, so save the relevant session before
restarting WDG if the failure needs investigation.

`boot changed` means WDG received a different firmware boot ID during an active
job. This is evidence of a changed firmware session, but the message alone does
not distinguish a device reset from stale metadata after a reconnect. Note the
time and any USB disconnect or reset, then inspect the same WDG log session.
If a channel error preceded it, diagnose that first. When reporting a failure,
include the detailed channel error and any later cleanup or boot message.

The firmware 1.8.6 readback fix accepts the ESP32-C5 SDK's automatically
selected 5 GHz secondary channel when the requested primary channel matches;
2.4 GHz still requires no secondary channel. It handles readback validation and
is separate from the confirmed 1.8.5 channel 144 SDK error described above.

Native firmware tests cover framing, bounded queues, storage identity, cleanup,
allocation failure, target parsing/filtering, scan lifecycle and exchange
isolation. Synthetic WDG tests cover malformed messages, chunk loss,
M-number/PMKID parsing, client separation, target filtering/selection, older
firmware, menu navigation, mode switches, disconnects and preservation of the
existing serial file dump. Both ESP32-C5 release variants build. These checks do
not establish RF capture completeness or hardware runtime stability; targeted
capture has not been physically RF-validated for this release.
