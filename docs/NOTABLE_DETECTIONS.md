# Flock and Axon detection coverage

These are radio signature matches and vendor candidates, not proof of a physical camera or its position. Every displayed geographic location means **the observer heard the signal here**. Names/payloads can be spoofed. There is no connection, GATT inspection, camera access, automatic reporting or upload in this feature.

## Controls

Open SNIFF → Wardrive Settings (O).

- Flock detection and Axon detection default ON.
- Precise Flock/Axon markers defaults ON. Turning it OFF changes only visual placement to the ordinary inventory's existing scatter; stored observation GPS never changes. Probe-only observations without an inventory scatter remain at their recorded fix.
- Wardrive trail defaults OFF. It records fresh host fixes during Wi-Fi, BLE or All wardriving and draws a cyan path. GPS gaps, pauses, disabled recording, jumps and sessions split the route. No line bridges an unknown section.
- D focuses the recent detection list; arrows select an entry. M mutes that observed identity locally; R resets device mutes. The list shows the supporting rule IDs and last observation time. Muting alerts does not discard ordinary inventory capture.
- H cycles through saved routes, then back to the current route. Saved routes load their matching notable history. Recording, if active, continues in the current session. The map camera keeps its normal GPS/pan behavior; route selection does not teleport the player.
- Settings live in `wardrive_settings.json` beside the existing app data. `suppressed_rules` can contain rule IDs; `suppressed_devices` contains `wifi:MAC` / `ble:MAC`. `realert_seconds` defaults to 60 (minimum 10).

Flock alerts use a purple header; Axon uses orange. Both categories use purple markers on the main map/minimap, with F/A labels on the main map. A ring keeps a point at the observer's center visible. Locations older than 60 seconds use an outline. Ordinary marker positioning and radar projection are unchanged.

## Rules

| Rule | All Wardrive | Legacy Wi-Fi/BLE wardrive | Meaning |
|---|---|---|---|
| flock-oui: B4:1E:52 | Wi-Fi transmitter/BSSID, public BLE | Wi-Fi only (legacy BLE omits address type) | Possible Flock |
| flock-name: Penguin-…, Flock-…, exact pigvision / FS Ext Battery | Full SSID/local name | Available SSID/name | Flock signature match |
| flock-wildcard-probe | Empty SSID probe request plus matching transmitter OUI | Unavailable | Flock signature match |
| flock-oem: company 0x09C8 | All AD manufacturer sections | Unavailable | XUNTONG OEM clue; **no popup by itself** |
| axon-oui: 00:25:DF | Wi-Fi/public BLE | Wi-Fi only | Possible Axon device |
| axon-company: 0x034D | Manufacturer AD, little endian | Unavailable | TASER vendor candidate |
| axon-service: 0xFC81 | 16-bit UUID AD/service data | Unavailable | Axon vendor candidate |
| axon-body-tag: BWCDEVICE | Inside actual service-data AD after the UUID | Unavailable | Axon body-camera signature |

Random/private BLE addresses and locally administered/multicast Wi-Fi addresses do not get OUI-based classification. Payload rules still apply to rotating BLE addresses. Advertisement and scan-response evidence share a bounded three-second cache by address/type; no attempt is made to identify a person or merge rotating addresses. Matches from both categories remain separate rather than arbitrarily choosing one.

A bare 10-digit name, broad OEM prefix lists, receiver-only address matches, unrelated Axon Networks prefixes (00:58:28 and 84:70:03), and Biscuit's unpublished SOUI database are not implemented. This is documented public-signature coverage, not exact parity with Biscuit's private firmware.

## Sources and provenance

The JSON ruleset contains source URLs and checked dates. Numeric identifiers and observed packet conventions are factual data; matcher code and synthetic fixtures were written independently. No third-party firmware implementation or signature database was copied.

- [Biscuit integration documentation](https://codehedge.github.io/Biscuit-Wiki/3rd-party-integration/wardrive.html): public Flock name/OUI/method examples. We use more conservative wording for its OEM clue.
- [Bluetooth SIG company identifiers](https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/company_identifiers/company_identifiers.yaml): 0x09C8 = XUNTONG; 0x034D = TASER International, Inc. Verified 2026-09-12.
- [Bluetooth SIG member UUIDs](https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/uuids/member_uuids.yaml): 0xFC81 = Axon Enterprise, Inc. Verified 2026-09-12.
- [Published Axon packet research](https://github.com/soyboi1312/all-cameras-are-beacons/blob/main/docs/axon.md): OUI and BWCDEVICE service-data convention; its field results are the source author's, not validation of this fork.

## Files and bounds

`notable_detections.jsonl` and `wardrive_trail.jsonl` are stored alongside the session's existing loot files. Detection rows keep first/last time, evidence, RSSI, the current observation fix (possibly null), and the last valid marker fix separately. Standard WiGLE rows remain WIFI/BLE. GPS-less observations remain in the raw serial log/BLE inventory without invented geotags.

The live list keeps the latest 1024 identities; the route display keeps up to 4096 points. Saved files retain all recorded rows. Older selected routes load as history, not live detections. Complete rows are recoverable after interruption; resuming an active route discards only an incomplete trailing JSON record. Existing background filesystem sync covers these local files; no external sync is added.
