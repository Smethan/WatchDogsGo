# Implementation and verification — 2026-09-12

## ModemManager cell and GNSS ownership — 2026-09-14

WDG 0.9.31 restores serving-cell tracking through ModemManager's cached 3GPP
location and GPS-NMEA sources. Both All Wardrive modes sample the serving cell
at completed firmware batches. ESP Dual Test and handshake modes remain
excluded. The default path performs no `GetCellInfo`, qmicli, direct AT, or
modem-device open. Experimental QMI neighbors are opt-in, rate-limited,
provisional, local-only, and circuit-broken after one session failure.

The host test suite covers the measured SIM7600 location, leading-zero MNCs,
source-mask restoration, managed-port exclusion, batch persistence, QMI PLMN
mismatch, neighbor identity boundaries, child timeout cleanup, crash sentinels,
and both ESP/host BLE modes. Hardware rollout additionally requires the
reversible power-service migration and one announced manual reboot. See
[CELLULAR_STABILITY.md](CELLULAR_STABILITY.md).

## Passive HS Sniff update — 2026-09-12

HS Sniff now has a dedicated status screen with per-AP/client PMKID and M1–M4
counts, channel, RSSI and age. Enter starts, S stops, and Escape/Tab returns to
the map while capture continues. Results remain available when reopening the
screen, and after stopping until the next capture. WDG must remain open.
The existing HS Capture item alone now warns that it requires an ESP32 SD card.

The new firmware capability `hs_sniff_serial_v1` streams passive raw frames
over serial without ESP32 SD/GPS or deauth. It runs separately from All Wardrive.
WDG saves raw PCAP plus JSONL classifications; message counts are observations,
not validation of a complete exchange. Firmware uses an eight-frame dynamic
queue (about 18.5 KB) and a 10 KB worker stack only while this mode is active.

Validation: **61 Python tests passed**, including M1–M4/PMKID parsing, malformed
and encrypted inputs, packet loss, per-client separation, stop drainage,
background screen navigation and mode changes. The firmware native harness
passed, and both final IDF 6.0.1 builds passed. Synthetic 640×360 previews
checked the status screen and SD warning placement. Hardware reception,
RF silence and uConsole storage/rendering performance still need field testing.

| Passive-capable build | IDF image size | Static HP SRAM |
|---|---:|---:|
| Standard ESP32-C5 | 2,246,311 bytes | 216,719 bytes |
| XIAO ESP32-C5 | 2,242,509 bytes | 216,907 bytes |

The new package is `artifacts/passive-hs-2026-09-12/` under the work folder,
with separate XIAO/standard images, logs, configurations, source commits and
checksums. See `build/passive-hs-manifest.json` for its provenance.
The earlier All Wardrive build documented below remains a historical checkpoint.

## Delivered (original All Wardrive checkpoint)

- projectZero: continuous Wi-Fi management reception plus BLE discovery over a versioned serial stream without ESP32 GPS/SD requirements. Bounded callback queue, evidence-aware throttle, separate counters, two-second heartbeat, host lease, stop cleanup and console ownership guard. Existing standalone mode is retained.
- WDG: SNIFF → **All Wardrive (6)**. GPS remains on the uConsole. Firmware capability checking, structured acknowledgments, stale-session rejection, heartbeat monitoring and explicit GPS-loss behavior. Legacy transitions clear old timers and wait for final stop completion, fixing conflicting scans/misleading labels.
- Flock and Axon: host-side public rules with source/evidence labels, purple/orange alerts, purple observation-location markers, independent detection switches and local suppression. **Precise Flock/Axon Markers defaults ON**; changing it never changes saved coordinates. Ordinary marker scatter stays intact.
- Optional cyan GPS trail, default OFF. Gaps and jumps start new segments; complete JSONL records survive interruption. Settings provide saved-route selection and matching notable history.
- Close-up maps: z15/z16 detail with small download areas and cached-parent fallback, using the existing source/account mechanism. No paid integration or account change. The 640×360 pixel-art display remains.
- CSV escaping/readback corrected for names containing commas/quotes/newlines. BLE inventory and wardrive CSV both accept explicit observation-time coordinates.

## Offline validation

- **47 Python tests passed**: bounded/split framing, malformed records, capabilities/timeouts, session lifecycle, stale stop acknowledgments, no legacy rescans during All, simulated mixed Wi-Fi/BLE ingestion, real local PTY framing, GPS delay/loss/frozen fixes, CSV/dedup, whitelist/evidence negatives, BLE caching, route gaps/recovery, and tile parent fallback.
- Python compilation passed for the watchdogs package.
- Firmware native harness compiled the real serial module against RTOS/radio/transport shims and passed synthetic management/BLE framing, role separation, malformed frame rejection, RSSI-only throttling, scan-response updates, queue overflow, age drops, partial writes, lease expiry, startup failure, and stop-during-start checks.
- Simulated Pyxel rendering at 640×360 verified purple Flock/orange Axon headers, purple map/radar rings, cyan trails, settings/details and MeshCore toast separation. Previews use synthetic streets and coordinates; they are not field captures or map-quality comparisons.
- Final **standard and XIAO ESP32-C5 builds passed** with `espressif/idf:v6.0.1`, in separate build directories. XIAO uses the USB Serial/JTAG overlay. Both fit their application partition with about 46% free.

| Build | Image size reported by IDF | Static HP SRAM |
|---|---:|---:|
| Baseline standard | 2,238,713 bytes | 204,161 bytes |
| Changed standard | 2,244,395 bytes | 216,703 bytes |
| Changed XIAO | 2,240,593 bytes | 216,891 bytes |

Changed standard minus baseline: +5,682 image bytes, +12,542 static SRAM bytes. The session worker also allocates a 6 KB task stack while active; this table is link-time static use, not measured runtime free heap.

Firmware build source commit: `69550cfd150da2e54772cdf1932620c6bdec7f7a`.
WDG source checkpoint: `3f296fd7833ec6c6dff02959d484ce65c5830393`.
Later commits record documentation/formatting. See `build/manifest.json` for exact board hashes and container digest.

## Reproduce offline checks

From WatchDogsGo, install its ordinary dependencies plus pytest, then run:

```sh
python -m pytest -q tests
python -m compileall -q watchdogs
PYTHONPATH=. xvfb-run -a python tests/render_wardrive_preview.py
```

From projectZero:

```sh
bash tests/serial_wardrive/run.sh
```

For firmware, copy ESP32C5 into a separate build directory for each variant. Append sdkconfig.xiao only to the XIAO copy's sdkconfig, as upstream CI does. Mount each copy at `/project` in the pinned IDF container, use `/project` as the working directory, and run `idf.py --preview -B build-standard build size` or `idf.py --preview -B build-xiao build size`. Keep the same container path across incremental builds. Do not invoke the upstream release/notification workflow merely to build.

## Artifacts and remaining checks

Local artifact directory: `/home/sam/Projects/uConsole-work/artifacts/all-wardrive-2026-09-12/`. It contains separate board images, bootloader/partition/OTA files, build configs, full build logs, manifest and SHA256SUMS. Binaries are kept outside the source repositories; source and compact build provenance are committed to both forks.

WDG's existing firmware release picker still fetches LOCOSP assets. Those upstream assets do not include this feature; any later installation must use the matching fork build. No firmware installation was performed or inferred from this implementation request.

Still unperformed: real XIAO start/stop and cable replug, no-SD/no-ESP-GPS operation, reception on both Wi-Fi bands, BLE discovery rates, sparse/dense environments and a 30-minute coexistence soak. C5 Wi-Fi sniffer/BLE reception shares airtime and is documented as supported with unstable performance. The software/build tests do not establish field completeness or exact camera identity/location. Host SD write latency and renderer performance on the uConsole also need field measurement.

## Capability handshake follow-up

A user reported that ordinary Wi-Fi works after installing the fork, but All
Wardrive shows the old-firmware warning. That warning previously conflated a
missing four-second startup reply with an explicit negative capability reply.
The host now makes three bounded attempts across eight seconds, keeps a timeout
as unknown support, and allows another probe when All Wardrive is selected.
Explicit positive/negative replies are shown in the terminal. Scan commands still
require positive capability confirmation. No firmware rebuild is needed for this
host fix; the actual cause on the user's board still requires serial evidence.

With WDG closed, query the ESP32 directly (substitute its actual port):

```sh
sudo .venv/bin/python -m watchdogs.diagnose_firmware --port /dev/ttyACM0
```

This prints the version response, raw capability responses, and a result. It sends
only version/capability queries and neither starts scanning nor flashes firmware.
