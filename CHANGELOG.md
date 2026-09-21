# Changelog

All notable changes to **Watch Dogs Go** are documented in this file.
Format inspired by [Keep a Changelog](https://keepachangelog.com/).
This project follows [Semantic Versioning](https://semver.org/) — currently in
the `0.x` series, meaning the API and on-disk format may still change between
minor versions.

---

## [Unreleased]

### Added

- Extend both All Wardrive radio modes with enabled host-side ADS-B and
  MeshCore collection after the ESP32 acknowledges the scan.
- Add independent `OFF`, `FADE`, and `KEEP` display policies for WiFi, BLE,
  cell, Flock, Axon, MeshCore, ADS-B, 433 MHz sensor, and handshake map layers.
  The shared fade lifetime is selectable at 15, 30, 60, or 120 seconds.
- Add cached three-pixel wardrive trails with `OFF`, `SOLID`, and `HEAT`
  modes. Heat colors represent unique WiFi/BLE identities heard in the rolling
  ten-second window around each route sample.

### Changed

- Bound the combined recent WiFi/BLE display registry to 512 deduplicated
  identities with constant-time refresh and eviction. Map visibility remains
  independent of complete WiGLE, notable-detection, and route files.
- Render dense dots and trails through bounded cached overlays. Expired dots
  invalidate the affected cache instead of remaining visible until an
  unrelated map rebuild.
- Let historical, live, radar, and route caches finish while the GPS-following
  camera moves, cache the bounded historical candidate window across incoming
  scans, and limit the tiny radar trail to its newest 160 route points.

### Fixed

- Run AIO v2 SX1262 transmit/receive in LoRaRF's supported SPI-polling mode,
  eliminating the competing GPIO callback that caused MeshCore `TX error`
  failures on the uConsole. WPA-sec export now stores content-hash receipts for
  permanent unsupported-format and no-usable-handshake/password rejections, so
  unchanged captures are not submitted again while temporary failures retry.
- Detect an absent AIO v2 `/dev/spidev1.0` before LoRaRF initialization and
  show the exact opt-in setup/reboot command instead of raw `[Errno 2]` output.
  Setup can now add the documented CM4/CM5 SPI1 boot settings idempotently while
  preserving a one-time backup of the existing boot configuration.
- Refresh menu availability from the live LoRa, SDR, and watch state instead
  of leaving implemented Python-side actions permanently grey. MeshCore
  Messenger now retries a powered but stopped receiver and reports queued
  SX1262 initialization errors even after the receiver thread exits.
- Send ADS-B and MeshCore records with Wi-Fi/BLE through WDGWARS' signed JSON
  endpoint, including the current MeshCore network discriminator, normalized
  timestamps, validated MeshCore public keys, canonical node IDs, and relay
  hop counts.
- Allow an empty loot refresh to clear the historical map model, preventing
  removed or expired source points from looking permanently painted onto the
  map.

---

## [0.9.39] — 2026-09-19

### Added

- Add a persistent LTE modem integration toggle that is loaded before GPS
  startup and can be applied immediately from Wardrive Settings.

### Changed

- Select the documented AIOv2 GPS UART (`ttyS0` on CM4, `ttyAMA0` on CM5) and
  safe external GPS devices without requiring the SIM7600 or ModemManager. Both
  All Wardrive modes and host Bluetooth remain independent of the cellular
  setting.

### Fixed

- Skip all ModemManager startup work and automatic generic `ttyUSB` probing
  when LTE integration is disabled, while preserving the existing safe
  ModemManager GNSS and serving-cell path when enabled.
- Gracefully fall back to external GPS when the LTE module is absent.

## [0.9.31] — 2026-09-14

### Added

- Restore cellular mast tracking in both All Wardrive modes using
  ModemManager's cached 3GPP location and GPS-NMEA interfaces. Serving-cell
  rows are sampled on completed ten-second firmware batches and retain the
  uConsole GPS fix in WiGLE 1.6 output.
- Add opt-in experimental QMI neighbor measurements. Anonymous LTE neighbors
  are saved as provisional local diagnostics and yellow map dots; they are
  never exported as WiGLE cells.
- Add bounded cell health logs, an unclean-session sentinel, ModemManager
  reconnect backoff, QMI timeout cleanup, and a session circuit breaker.
- Add a reversible migration helper for replacing the uConsole's direct-AT
  GNSS boot service with a power-only service owned by ModemManager.

### Changed

- Use ModemManager as the internal SIM7600 GNSS owner. GPS discovery no longer
  opens ModemManager-owned tty ports or known ESP32/uConsole ACM devices.
- Start cellular work only after an All Wardrive firmware acknowledgement;
  cell failures do not affect the ESP32 scan lifecycle.

### Fixed

- Avoid the control-plane races caused by direct `AT+CGPS` boot writes and
  blind probing of SIM7600 AT ports.
- Ignore the SIM7600 qmicli PLMN when it disagrees with ModemManager's
  registered operator, preventing invalid WiGLE identities.

## [0.9.30] — 2026-09-14

### Fixed

- Remove live cellular mast detection from both All Wardrive modes after
  continued whole-uConsole crashes. WDG no longer creates a cellular worker,
  calls ModemManager, starts `qmicli`, discovers modem ports, or displays a
  live cellular status during a scan.
- Preserve parsing, upload, loot totals, and display of cellular rows written
  by earlier releases. Existing wardrive sessions are not modified.
- Remove cellular control utilities from WDG's setup dependency list. Existing
  system packages and the uConsole's cellular-data configuration are untouched.

## [0.9.29] — 2026-09-14

### Fixed

- Remove persistent raw-serial `AT+CPSI?` polling from the SIM7600 cellular
  fallback. Older QMI-backed ModemManager releases now use the synchronized
  qmi-proxy control path on `/dev/cdc-wdm*`, without opening any modem TTY.
- Slow cell sampling from 10 to 30 seconds. Permanently unsupported providers
  disable cellular collection for the current wardrive session; transient QMI
  errors retry after 60 seconds with exponential backoff capped at five minutes.
  Wi-Fi and BLE collection continue during either condition.
- Record only cellular observations with complete WiGLE identities. QMI LTE
  and UMTS neighbor measurements lacking global cell IDs are no longer
  representable as distinct masts.

### Documentation

- Add a source-cited SIM7600/ModemManager/libqmi stability analysis and clearly
  separate the established software defect from the unproven cause of the
  reported whole-console crash.

## [0.9.28] — 2026-09-14

### Fixed

- Recover SIM7600 serving-cell collection when ModemManager exposes
  `GetCellInfo` but returns `Core.Unsupported` and omits the modem's AT ports
  from `Modem.Ports`. WDG now discovers the secondary command interface from
  ModemManager's udev role tag and verifies that it belongs to the same physical
  modem before using the existing `AT+CPSI?` fallback.
- Avoid selecting the SIM7600 GPS, diagnostic, audio, primary AT, or another
  modem's serial interface during cellular fallback discovery.

## [0.9.27] — 2026-09-14

### Added

- Protocol-v2 parsing and state recovery for projectZero 1.7.10's ten-second
  All Wardrive batches.
- Background-scan phase, batch result counts, separate control/data ages and
  status-probe counts in the All Wardrive and diagnostic overlays.

### Changed

- Prefer batched Wi-Fi/BLE or batched Wi-Fi-only commands when firmware
  advertises them, with labeled v1 streaming fallback.
- Require 15 seconds of both control and ESP-data silence before stopping a v2
  scan; query firmware state first and accept its global cleanup as a stop
  fallback.
- Retain 30 seconds of host GPS history so delayed batch records use the nearest
  capture-time fix within three seconds.

## [0.9.26] — 2026-09-14

### Added

- All Wardrive now runs a host cellular collector in both ESP BLE and host
  BlueZ BLE modes. ModemManager supplies serving and neighboring cells when
  supported; SIM7600 `AT+CPSI?` remains a serving-cell fallback.
- Cellular observations use WiGLE cell identities, preserve repeated GPS
  samples, expose unique/observation counts, and appear as green map points.

### Changed

- New wardrive files use the WiGLE 1.6, 14-column layout. Readers and WDGoWars
  sync now parse columns by name and remain compatible with WiGLE 1.4 files.
- WDGoWars sync preserves cellular technology, channel, frequency, GPS
  accuracy, and signal fields instead of applying Wi-Fi authentication rules.

## [0.9.13] — 2026-06-04

Companion to 0.9.12 — handles the transient HTTP 413 the same user
(dtmcnamara) saw on the larger of two sessions during a sync. Now
documented and confirmed by the wdgwars admin as a WAF / LVE burst
event on the shared-host LiteSpeed / Imunify360 layer, **not** a real
payload size cap (admin tested clean uploads up to 32 MB).

### Background

The 413 response body started with `<!DOCTYPE HTML PUBLIC "-//W3C//DTD H…`
— HTML 4 doctype = LiteSpeed origin error page, not the Cloudflare
HTML 5 layout. So the rejection happened **before** the request hit the
portal app, on the shared-hosting WAF for that specific IP / cadence.
PHP `post_max_size` is 128 MB, `.htaccess` carries no `LimitRequestBody`,
and the user successfully posted 15 payloads in a row in the same
window with sizes 17–90 KB — confirming it's transient, not a fixed
limit.

### Added

- **`plugins/wardrive_upload.py`** — `HTTPError` handler now also
  catches `e.code == 413`. Behaviour: exponential backoff retry with
  delays `[1 s, 5 s, 30 s]` (max 3 attempts). Each retry re-signs a
  fresh nonce (since the old one would be rejected as stale anyway
  if the request did reach the app). On success the rest of the
  upload pipeline runs as usual — totals, badge sync, `_mark_uploaded`
  honouring the active-session rule from 0.9.6.
- If all three retries still come back 413, the session is **left in
  the pending queue** (no `_mark_uploaded`) so the next upload run
  picks it up. Log line:
  `HTTP 413 persisted through 3 retries — session left pending`.
- If a retry returns a *different* HTTP status (e.g. 401 because the
  user's key expired mid-batch), the loop bails out immediately
  rather than burning the remaining backoffs on what is clearly not
  a WAF issue.

### Unchanged

- The 429 (rate-limit) retry path is untouched — it still does a
  single 5 s wait + one retry. Behaviour confirmed correct since 0.9.5;
  no reason to bundle that change with this one.
- Server-side, the admin will replace the bare LiteSpeed HTML 413
  page with a JSON envelope via `ErrorDocument 413` in `.htaccess`,
  so future incidents log as `HTTP 413: {"error":"upstream-413",…}`
  instead of `HTTP 413: <!DOCTYPE HTML PUBLIC "-//W3C//DTD H…`. That
  change is independent of this client release and lands when the
  admin ships it.

---

## [0.9.12] — 2026-06-04

Bugfix for the **Captured Passwords** screen in `LOOT DATABASE`. A user
reported the count and the list were inflated with every Evil Twin /
Evil Portal log line — `AP: Client connected`, `Portal: Client count =`,
`Client connected to portal — switching to channel …` — instead of only
actual POST credentials.

In the screenshot the screen showed `Total: 14` for a session that had
only **2** real `Received POST data: email=…&password=…` captures.

### Root cause

The full Evil Twin / Portal event stream is written to
`<session>/evil_twin_capture.log` and `<session>/portal_passwords.log`
for forensics. Two sites then treated those files as "list of
passwords" without filtering:

- `LootManager._scan_session_dir` counted every line in the log file
  as one capture (`sum(1 for _ in open(...))`), so the all-time
  `passwords` / `et_captures` totals over-reported by the ratio of
  diagnostic events to credentials.
- `WatchDogsGame._load_all_captures` (the data source behind the
  Captured Passwords screen) iterated every line, ran
  `_parse_post_fields()` on it, and **on no match still appended the
  raw decoded line** as an entry — so connect/disconnect chatter
  showed up under "CREDENTIALS".

### Fixed

- `watchdogs/loot_manager.py` — both counters (`passwords`,
  `et_captures`) now only count lines containing `received post` or
  `form submission` (case-insensitive). The log files themselves are
  unchanged — they keep the full event timeline.
- `watchdogs/app.py` — `_load_all_captures` drops the `else` branch
  that was appending non-credential lines. Only lines that parse as
  POST form data make it onto the Captured Passwords screen now.

### Migration

None needed. Both code paths re-scan session dirs on launch, so once
the user `git pull`s and starts the game the inflated count corrects
itself — the screenshot's `Total: 14` will become the real `Total: 2`
without touching anything on disk.

---

## [0.9.11] — 2026-05-08

Quality-of-life follow-up to 0.9.10. The Stadia Maps tile endpoint
returns `HTTP 401 Unauthorized` for every request that doesn't carry
a key, so a user who runs `SYSTEM > Download Map` without first
adding `STADIA_API_KEY=…` to `secrets.conf` saw hundreds of
`[MAP] 0% — ERR tile … HTTP Error 401: Unauthorized` lines in the
terminal and no map — confusing first-run experience.

### Changed

- **`watchdogs/app.py`** — `_start_map_download()` now preflights
  `_load_stadia_key()` from `tile_manager`. If the key is empty the
  download bails early with two clear messages instead of starting
  the doomed worker thread:
    ```
    [MAP] No Stadia API key — add STADIA_API_KEY to secrets.conf
    [MAP] Free signup: https://stadiamaps.com
    ```
- **`watchdogs/tile_manager.py`** — docstring on `download_tiles()`
  and the throttle-sleep comment still referenced *"CartoDB Dark
  Matter"* / *"CartoDB usage policy"* (leftover from before
  FusedStamen's PR #5 swapped the source). Now reads "Stadia Maps
  Alidade Smooth Dark" / "polite to Stadia free tier" so a future
  reader doesn't think there's a second tile provider in the code.

No functional changes for users who already have a key configured —
their download path is unchanged.

---

## [0.9.10] — 2026-05-02

Re-enabled offline map tile downloads, this time backed by Stadia Maps
instead of the previously-disabled CartoDB Dark Matter source.
Cherry-picked from FusedStamen's PR #5 (`1719bbf`); each player decides
individually whether to opt in by adding their own free API key.

### Added

- **`watchdogs/tile_manager.py`** — `_load_stadia_key()` reads
  `STADIA_API_KEY` from `secrets.conf`, `_tile_url()` builds the
  Alidade Smooth Dark tile URL with the key appended. User-Agent
  string updated.
- **`watchdogs/app.py`** — removed the WIP/disabled gate from the
  `_dl_map` handler. `SYSTEM > Download Map` now actually downloads;
  pressing `[m]` again cancels in-flight. Menu label dropped its
  `(WIP)` suffix.
- **`secrets.conf.example`** — documents `STADIA_API_KEY=…`. Without
  it the Download Map entry tells the player to add one (free account
  at stadiamaps.com, no bulk-download restrictions on the personal
  tier).

### Verified

- 44 tiles downloaded over Watertown MA on the contributor's uConsole
  CM4, zero errors, tiles render under the existing radar/markers
  overlay (per PR #5 description).

---

## [0.9.9] — 2026-05-02

Two community contributions cherry-picked from FusedStamen's fork —
the rest of his branch (handshake architecture change, AWUS packet
sniffer, bridge scripts, whitelist.json) has been left out and will
need separate review.

### Added — Aircraft total on the loot screen
*(commit `d283e66` from FusedStamen, PR #4 target)*

- **`watchdogs/loot_manager.py`** — new
  `save_adsb_aircraft(icao, callsign, lat, lon, alt_ft, speed_kt, heading)`
  appends to `<session>/adsb_aircraft.csv` with header on first write,
  dedup-by-ICAO within a session, fsync'd. Same pattern as the
  existing `bt_devices` / `wardriving` writers.
- **`_scan_session_dir`** counts `adsb_aircraft.csv` rows and
  `_recalc_totals` / `_rebuild_db` carry the new `adsb` key so it
  rolls up into all-time totals.
- **`watchdogs/app.py`** — the `aircraft_new` event handler now also
  calls `loot.save_adsb_aircraft(...)` for each unique ICAO seen,
  capturing position/altitude/speed/heading.
- **Loot screen** gains an "Aircraft" row in both the ALL-TIME column
  (prefers server-side `_user_stats["aircraft"]` from the wardrive
  plugin so the number matches what wdgwars.pl reports, falls back to
  the local count) and the THIS SESSION column (live SDR aircraft
  count).

The CSV format `save_adsb_aircraft` writes is exactly what
`plugins/wardrive_upload.py:_parse_adsb` already expects, so existing
sessions get picked up automatically by the next upload.

### Fixed — Upload timeout false-failure
*(commit `4e5ec75` from FusedStamen, PR #6 target)*

- **`plugins/wardrive_upload.py`** — bumped per-request timeout from
  30 s to 90 s on both the main upload and the rate-limit retry. Large
  ADS-B sessions could exceed 30 s server-side processing, so the
  client got a `TimeoutError` on a request the server actually
  accepted, then retried and the dedup layer correctly rejected the
  duplicate — net effect was no progress.
- **New `(TimeoutError, OSError)` handler** marks the session as
  uploaded after a warning, instead of silently leaving it pending.
  Safe in combination with 0.9.6's chokepoint (active session is
  still skipped by `_mark_uploaded`) and 0.9.8's per-user
  `aircraft_already_seen` dedup (any future re-upload only credits
  genuinely new ICAOs).

### Not merged from the same fork

The submitted PRs (#4, #5, #6) all carried 8–10 commits from
FusedStamen's `main` branch beyond the target commit:

- `feat: switch HS capture to airodump-ng …` — would replace ESP32
  handshake capture with a host-side `airodump-ng` flow. Architecture
  change, separate decision.
- `feat: packet sniffer via AWUS036ACM wlan2, tcpdump monitor mode` —
  new feature, needs its own design + UI review.
- `Add XIAO auto-detect launcher, whitelist, and packet sniff bridge`
  — bundled `whitelist.json` (a private MAC ignore list, not
  appropriate for a shared repo) and an unsanitised bridge script.
- `feat: AIO v1 uConsole Config …` — modifies code paths shared with
  AIO v2 hardware; needs explicit AIO v1 vs v2 branching.
- `fix: skip active session in _pending_sessions …` — duplicates the
  fix already shipped as 0.9.6 (different code site, same intent).
PRs #4, #5 and #6 closed with cherry-pick reference.

---

## [0.9.8] — 2026-04-26

Surface the new `aircraft_already_seen` field that wdgwars.pl now
returns alongside `aircraft_imported`. Companion to 0.9.6 — the server
side of the same bug is fixed too: per-user `(user_id, icao)` dedup in
the `user_aircraft_seen` table with `INSERT IGNORE`, so a re-uploaded
session correctly credits aircraft that are new for that user.

### Server-side fix (wdgwars.pl, by the portal admin)

> Added `user_aircraft_seen` table (PRIMARY KEY user_id, icao) keyed
> per-user, INSERT IGNORE on each ICAO during upload — `aircraft_imported`
> now counts what's actually new for that user. Global
> `maps_trackedaircraft` stays as the latest-position directory.
> Plane Spotter / Plane Hunter / Sky Watcher badge counters all
> switched to the new table; existing badge progress preserved via
> best-effort backfill from prior `maps_trackedaircraft.uploaded_by_id`.

### Client change

- **`plugins/wardrive_upload.py`** reads the new `aircraft_already_seen`
  key from the upload response. Successful upload log now shows
  `+5 ac (3 seen)` instead of just `+5 ac`, so the user can see at a
  glance how the dedup decided. No effect when the field is missing
  (graceful default 0) — works against older server builds.

### Reproducer

Fresh user, blank slate:

| step                                  | request ICAOs    | expected response                          |
|---------------------------------------|------------------|--------------------------------------------|
| 1. first upload                       | {A,B,C}          | `aircraft_imported: 3, aircraft_already_seen: 0` |
| 2. same user, re-upload (active session)| {A,B,C,D,E,F,G,H}| `aircraft_imported: 5, aircraft_already_seen: 3` |

Combined with 0.9.6 (active session never marked uploaded), the
overnight ADS-B bug originally reported by the US user is fully
addressed end-to-end.

---

## [0.9.7] — 2026-04-26

Accept any character device as a "serial port" so community PTY bridges
work without patching the entry point.

### Background

GitHub issue [#1](https://github.com/LOCOSP/WatchDogsGo/issues/1)
(FusedStamen) — uConsole AIO v1 owners cannot get a real ESP32 + AIO v2
on the same hardware, so the community wrote `wdg_wifi_bridge.py` which
emulates the projectZero serial protocol on a PTY (`socat … PTY,link=/tmp/esp32-pty`)
backed by the uConsole's built-in MT7921 (Wi-Fi) and BCM4345 (BLE)
through `iw scan` and `bleak`. Game arguments only accepted `/dev/*`
and `COM*` though, so `/tmp/esp32-pty` was getting parsed as a
**loot path** instead and the game then started its normal ESP32
auto-detect (which finds nothing).

### Fixed

- **`watchdogs/__main__.py`** — argument detection moved into
  `_looks_like_serial(path)` which keeps the old `/dev/*` and `COM*`
  prefixes and additionally accepts any path that `os.stat()` reports
  as a character device. PTYs created by `socat link=…` follow that
  test, so `sudo ./run.sh /tmp/esp32-pty` now opens the bridge.

### Compatibility / safety review

- Argv is supplied by whoever launches the game (typically
  `sudo ./run.sh`) — no new attack surface; the same user already
  controlled `/dev/ttyUSB0` and any other path passed in.
- All previously-handled argv shapes keep their existing behaviour:
  - `/dev/ttyUSB0`, `COM3` → serial (unchanged)
  - regular files (`/etc/hosts`), directories (`/tmp`), non-existent
    paths, relative paths → loot path (unchanged)
  - `/dev/null` → still serial because it lives under `/dev/` (was
    already serial under the prefix rule)
- New behaviour:
  - existing character device outside `/dev/` (e.g. `/tmp/esp32-pty`
    symlink to `/dev/pts/N`) → serial (was loot path → broken).

---

## [0.9.6] — 2026-04-24

Hotfix for a data-loss bug in `plugins/wardrive_upload.py` reported by
a US user running ADS-B overnight:

> If you stop wardriving during a session with active ADS-B, it's
> marked as ready for upload. When a session is uploaded while still
> active, any data written to it after the upload is lost because the
> session gets marked as uploaded and never re-queued even though it
> continued accumulating data overnight. […] 578 unique new ICAOs
> seen after my upload — these are aircraft the server should have
> counted as new but apparently didn't.

The user's diagnosis was correct on the client side: as soon as
`_upload_worker` got an HTTP 200 it added the session directory name
to `_uploaded_sessions` and persisted that to
`plugins/.wardrive_state.json`. The currently-recording session — the
one ADS-B / GPS / MeshCore are still appending to — got the same
treatment, so any rows written after the upload (overnight aircraft,
late mesh adverts) silently fell out of the pending queue.

### Fixed

- **Active session is never marked as uploaded**. New helpers
  `_active_session_name()` and `_mark_uploaded(session_dir)` in
  `plugins/wardrive_upload.py` gate every place we used to call
  `_uploaded_sessions.add(name)`. The active session keeps appearing
  in `_pending_sessions()` until the game starts a fresh session
  directory — at which point the now-stale session can finally be
  marked done on the next upload.
- The upload log explicitly tells the user when this happened:
  `(active session — kept open for re-upload)` after a successful
  upload, or `active, will re-check next upload` for a session that
  was empty at upload time but might still receive data.

### Not fixed (server-side)

The user also flagged a second issue: after manually clearing the
session from `_uploaded_sessions` and re-uploading, the server only
credited the new MeshCore nodes and ignored 578 aircraft ICAOs that
were genuinely first-seen that day. That dedup decision happens
entirely on the wdgwars.pl side — there is nothing the game can do
about it. Forwarded to the wdgwars admin separately.

---

## [0.9.5] — 2026-04-24

Runtime-selectable regional presets for the MeshCore radio. MeshCore
upstream recently moved the default US/Canada preset from the legacy
wide mode (`910.525 / BW250 / SF11 / CR5`) to narrow mode
(`910.525 / BW62.5 / SF7 / CR5`), which is incompatible with the old
preset — nodes running different settings physically cannot hear each
other. Until now the game hard-coded EU/UK Narrow, so every US user
was invisible to their local mesh.

### Added

- **`watchdogs/lora_manager.py` — `MESHCORE_PRESETS`** — six canonical
  regional presets driving the SX1262:

  | Key              | Label                                 | Freq       | BW     | SF | CR |
  |------------------|---------------------------------------|------------|--------|----|----|
  | `eu_uk_narrow`   | EU/UK Narrow (default)                | 869.618 MHz| 62.5 kHz| 8  | 5  |
  | `eu_uk_default`  | EU/UK legacy wide                     | 869.525 MHz| 250 kHz | 11 | 5  |
  | `us_ca_narrow`   | US/Canada Narrow (new upstream default)| 910.525 MHz| 62.5 kHz| 7  | 5  |
  | `us_ca_default`  | US/Canada legacy wide                 | 910.525 MHz| 250 kHz | 11 | 5  |
  | `anz_narrow`     | AU/NZ Narrow                          | 915.525 MHz| 62.5 kHz| 7  | 5  |
  | `in_narrow`      | India Narrow                          | 865.525 MHz| 62.5 kHz| 7  | 5  |

- **`ADDONS > MeshCore Region`** menu entry — opens an overlay picker
  styled like the Evil Twin network picker (UP/DOWN + ENTER + ESC),
  listing every preset with freq / BW / SF. Confirming persists the
  choice to `~/.watchdogs_meshcore.json` and — if the radio is
  currently running — stops and restarts the MeshCore sniffer on the
  new settings immediately, without exiting to the menu.

### Changed

- **`LoRaManager.start_meshcore(region=None)`** now takes an optional
  region key instead of hardcoding the EU preset. Callers in `app.py`
  (auto-start on boot, `_toggle_lora`) pass `self._mc_region`.
- **`load_meshcore_config()` / `save_meshcore_config()`** round-trip a
  new `"region"` field. Unknown/missing keys fall back to
  `DEFAULT_MESHCORE_REGION = "eu_uk_narrow"` so existing installs keep
  their current behaviour on upgrade.

### Why this matters — reported by a US MeshCore user

> Old settings were: req=910.525, bw=250, cr=5, sf=11
> New settings are: req=910.525, bw=62.5, cr=5, sf=7
>
> Based on the official map, the settings seem to be all over the
> place. This is likely why I can't find anyone 😊

Narrow mode concentrates TX energy and cuts airtime per packet, which
lets more nodes share a channel with lower latency — but old-preset
nodes simply cannot demodulate the new waveform. This release lets
every user pick the preset that matches the mesh around them, right
from the in-game menu, without editing JSON by hand.

---

## [0.9.4] — 2026-04-24

Installer hardening after a user bug report: the Debian package
`dump1090-mutability` that the installer pointed at has been an
**archived upstream since 2018** — no RTL-SDR v4 support, no fixes.
The ADS-B Radar add-on would bail with an unhelpful
"Install: sudo apt install dump1090-mutability" message that then
failed on newer Debian images where the package no longer exists.

Full audit of every external binary the game shells out to
(`shutil.which` / `subprocess`) found one more gap: `iw`, used by
Dragon Drain and the serial bridge-interface check, was never in the
apt list — it is usually preinstalled on Raspbian, but a user on a
clean Debian image would silently lose those features.

### Fixed

- **`setup.sh`** now builds the actively-maintained
  [FlightAware `dump1090` fork](https://github.com/flightaware/dump1090)
  from source when `dump1090` is missing. Pulls `librtlsdr-dev`,
  `pkg-config`, `build-essential`, `git` first, clones shallow, makes
  with `-j$(nproc)`, installs to `/usr/local/bin/dump1090`. The binary
  name stays `dump1090` so the game's existing detection
  (`shutil.which("dump1090")`) and `dump1090 --net --quiet` subprocess
  call keep working unchanged.
- **`setup.sh`** adds `iw` to the apt dependency list so Dragon Drain
  and interface bridging work on clean Debian/Ubuntu installs.
- **`watchdogs/app.py`** no longer points the user at an archived apt
  package when `dump1090` is missing. The in-game message now tells
  them to rerun `./setup.sh` which builds the FA fork.
- **`README.md`** system-dependency list rewritten accordingly, plus
  adds the `python3-gi` / `gir1.2-glib-2.0` line that 0.9.2 needed
  but never made it into the docs.

### Audit summary

Cross-checked `requirements.txt`, `setup.sh` and runtime `shutil.which`
calls. All non-archived tools used by the game are covered:

| Binary | Package | Installed by setup.sh |
|--------|---------|----------------------|
| `tcpdump` | `tcpdump` | yes |
| `airmon-ng` / `aircrack-ng` | `aircrack-ng` | yes |
| `iw` | `iw` | **yes (new in 0.9.4)** |
| `rtl_433` | `rtl-433` | yes |
| `bluetoothctl`, `btmgmt` | `bluez` | yes |
| `hciconfig` | `bluez-tools` | yes |
| `pactl` | `pulseaudio-utils` | yes |
| `pinctrl` | `raspi-utils` | yes |
| `dump1090` | built from FA source | **yes (new in 0.9.4)** |
| `aiov2_ctl` | git clone + `./aiov2_ctl.py --install` | yes (RPi only) |

`bdaddr` (used by `race_attack.py` for BLE MAC spoofing) is not in any
standard Debian package and is left off the list — the module already
falls back to `btmgmt public-addr` when it is missing.

---

## [0.9.3] — 2026-04-21

Hotfix for `wdgwars.pl` upload flow. The server now sits behind Cloudflare
with Bot Fight Mode enabled, which drops HTTP requests carrying a generic
User-Agent (Python's urllib defaults to `Python-urllib/3.x`) with
**HTTP 403 error code 1010**. Symptom reported by a user:

```
/api/me: HTTP 403 — Invalid signature
Upload All: HTTP 403 error code: 1010    (×3 sessions)
Done: 0/4 sessions
```

### Fixed

- **`plugins/wardrive_upload.py`** now sets an explicit User-Agent on
  every request to the wardrive server. Format matches the spec the
  wdgwars admin provided: `WatchDogsGo/<version> (<platform>; Python/<ver>)`
  — e.g. `WatchDogsGo/0.9.3 (uConsole; Python/3.11)`. Platform is
  `uConsole` on Linux (our target), real `platform.system()` value
  elsewhere so dev boxes are distinguishable in server access logs.
- **Centralised HTTP call site** as `_open(req, timeout)` wrapper that
  injects `User-Agent` + `Accept: application/json` into every
  `urllib.request.Request` before it reaches `urlopen`. All 6 request
  sites in the plugin (POST `/api/upload/`, POST `/api/badges/`,
  GET `/api/me`, auth-push POST + GET polling) now route through it,
  so new endpoints can't accidentally ship without the header.

### Verified

- Live test against `https://wdgwars.pl/api/me` with the new UA and a
  garbage `X-API-Key` returns **HTTP 401 "Missing or invalid API key"**
  — the request reaches the application layer instead of being dropped
  by Cloudflare. Exactly the "good case" the admin described.

### What this does NOT fix

- If `/api/me` still returns **403 "Invalid signature"** after pulling
  this release, the user's API key is actually invalid on the server —
  regenerate it on the wdgwars.pl profile page and re-enter via
  `PLUGINS > Watch Dogs Go Wars Sync > Set API Key`.

---

## [0.9.2] — 2026-04-20

Integration pass for the **LilyGO T-Watch Ultra** (PipBoy-3000 firmware).
The watch firmware ≥ v0.4 hardened its BLE surface by requiring
MITM-authenticated encryption on the Nordic UART Service characteristics
— unauthenticated peers can no longer read notifications or issue
commands. This release brings the game in line with that change and
makes the first-pair flow work end-to-end on the uConsole.

### Added

- **Explicit `bluetoothctl pair` pre-step** in
  `watch_manager._async_connect`. Before opening the GATT session we
  now check `bluetoothctl info <addr>` for `Paired: yes` / `Trusted: yes`
  and, if either is missing, run `bluetoothctl pair <addr>` in a worker
  thread with a 90-second timeout. That drives BlueZ into the pair
  state cleanly and routes the passkey prompt through our existing
  D-Bus Agent1 (`KeyboardDisplay` IO capability, `RequestPasskey` →
  `provide_pin()` from the game UI). After success we mark the device
  trusted so every subsequent connect is silent. The previous flow
  relied on bleak's implicit Insufficient-Authentication retry, which
  is flaky on BlueZ.
- **`python3-gi` system dependency** installed + symlinked into the
  game's venv by `setup.sh`. `gi.repository.GLib` is what the BlueZ
  pairing agent's GLib main loop runs on; without it the agent
  silent-failed and the new firmware's pair request had nowhere to go.
  Same pattern we already use for `rpi-lgpio`: `apt install python3-gi
  gir1.2-glib-2.0`, then symlink `/usr/lib/python3/dist-packages/gi` +
  the `_gi*.so` / `_gi_cairo*.so` native extensions into
  `.venv/lib/pythonX.Y/site-packages/`. It can't go in
  `requirements.txt` because PyGObject via pip still needs
  `libgirepository1.0-dev` + `gobject-introspection` at apt level, and
  builds are fragile on the Raspberry Pi class targets.
- **First-run preflight check** for `python3-gi` added to
  `watchdogs/__main__.py` alongside `bleak` / `dbus-python`. If anyone
  pulls just the code without running `setup.sh`, the boot screen now
  shows `python3-gi — NOT INSTALLED (apt)` before the game launches.

### Changed

- **PIN entry overlay** in the Watch screen rewritten for the new flow:
  240×80 → 320×112 with explicit wording ("Look at the watch — it shows
  a 6-digit PIN. Type it here to complete pairing."), cyan PIN digits,
  and the full key hint `[0-9] Type   [BKSP] Erase   [ENTER] Confirm`.
  Centering redone for the Spleen 5x8 font (6 chars × 5 px).

### Fixed

- **Aircraft, ADS-B sensors and MeshCore nodes vanished at low zoom.**
  The map render order was calling `_draw_aircraft / _draw_sensors /
  _draw_markers` before `_draw_player`, and the player skull is a
  ~9×9 sprite with a 10–12-pixel scan ring drawn at the map centre.
  At low zoom every local source lands within that ring, so the plane
  plus-shapes and node diamonds were simply being overdrawn by the
  skull — visible only at CLOSE-UP zoom where quantization pushed
  them outside the ring. Render order is now:
  player → aircraft → sensors → markers → particles. Radar blips
  now sit cleanly on top of the player.

### Version

- `__version__` bumped `0.9.0` → `0.9.2`. The 0.9.1 CHANGELOG entry
  ships on top of the same `0.9.0` string because the version
  constant wasn't updated at the time of that release; the next pypi-
  style artefact is this one.

---

## [0.9.1] — 2026-04-19

Readability, performance and data-loss protection. No new gameplay.

### Added

#### Typography — Spleen 5x8 global font

- **`assets/spleen-5x8.bdf`** — [Spleen](https://github.com/fcambus/spleen) by
  Frederic Cambus, size 5x8, BSD-2-Clause, 76 KB BDF. Glyphs fit 95 ASCII
  codepoints; Polish letters are transliterated by the MeshCore chat path
  (`ą→a, ć→c, ę→e, ł→l, ń→n, ó→o, ś→s, ź→z, ż→z`, both cases) since BDF
  is ASCII-only and our Pyxel build doesn't support Unicode fonts.
- Loaded in `WatchDogsGame.__init__` as `self._font = pyxel.Font(path)`.
  The built-in pyxel 4x6 font is kept only as a fallback when the BDF
  fails to parse.
- **Applied globally** by monkey-patching `pyxel.text` right after the
  font loads:

  ```python
  _orig = pyxel.text
  _f = self._font
  def _patched_text(x, y, s, col, font=None, _o=_orig, _f=_f):
      _o(x, y, s, col, font if font is not None else _f)
  pyxel.text = _patched_text
  ```

  This covers ~370 call sites without touching them individually. Calls
  that pass a font explicitly (e.g. the messenger's legacy 8x8 `_big_font`
  for some labels) are untouched by the wrapper.
- Size trade: visible terminal lines drop from ~22 (4x6) to ~12 (5x8),
  but each line is actually legible on the uConsole panel.

#### Map markers

- **`_draw_markers` now branches on `MapMarker.type`**:
  - `"meshcore"` → 5x5 cyan diamond (`pyxel.rect` + 4 cardinal `pset`s)
    with 22/40 frames of an outer `circb` pulse ring at radius 6,
    label in cyan from zoom ≥ 4.
  - `"handshake"` → unchanged red skull shape (blinking ring at radius 4,
    body `rect(-2,-1,5,4)`, crown `rect(-1,-3,3,2)`, warning-colored
    centre pixel), label from zoom ≥ 5.
- **`_draw_aircraft`** replaced the old 3-pixel arrow with a 6x5 plus
  shape: `line(-3,0,+3,0)` wings, `pset(0,-2/-1/+1)` fuselage,
  `pset(±1,+2)` tail fins, plus a 14/40 frames `circb` pulse at radius 5.
  Altitude-colored (green < 5 kft, yellow < 20 kft, cyan above).
  Result: planes and nodes stay visible on globe/continent views where
  they previously collapsed to a single pixel under the coordinate
  quantization.

#### Power-cut resilience — event-driven fsync

- New module-level helpers in `watchdogs/loot_manager.py`:
  - `_fsync_file(fh)` — `fh.flush() + os.fsync(fh.fileno())`, wrapped
    in `try/except (OSError, ValueError)` so a concurrently-closed
    handle doesn't crash the call.
  - `_sync_append(path, text, encoding, newline)` — append wrapper.
  - `_sync_write(path, text, encoding, newline)` — truncating wrapper.
- Applied to every save path that produces **unrecoverable** user data:

  | File | Call site | Type |
  |---|---|---|
  | `portal_passwords.log` | `save_portal_event` | append |
  | `evil_twin_capture.log` | `save_evil_twin_event` | append |
  | `attacks.log` | `log_attack_event` | append |
  | `wardriving.csv` | `save_wardriving_network` / `save_wardriving_bt` | append + rewrite (on dedupe) |
  | `scan_results.csv` | `save_scan_results` | truncate |
  | `sniffer_aps.csv` | `save_sniffer_aps` | truncate |
  | `sniffer_probes.csv` | `save_sniffer_probes` | truncate |
  | `meshcore_nodes.csv` | `save_meshcore_node` | append |
  | `meshcore_messages.log` | `save_meshcore_message` | append |
  | `bt_devices.csv` | `save_bt_device` | append |
  | `bt_airtag.log` | `save_bt_airtag_event` | append |
  | `handshakes/*.txt` | `_save_handshake_txt` | truncate |
  | `handshakes/*.pcap` | `_save_pcap_stream` | binary truncate |
  | `handshakes/*.hccapx` | `_save_pcap_stream` | binary truncate |
  | `loot_db.json` | `_save_db` (tmp write before atomic rename) | truncate |

- Motivation: `with open(path, 'a') as fh: fh.write(...)` only pushes
  bytes into Python's FileIO buffer → `close()` flushes to the OS page
  cache → the kernel writes to the block device whenever it feels like
  it (on default ext4 / f2fs that's ~5–30 s via `dirty_writeback_centisecs`).
  A hard power cut anywhere in that window loses the data. `os.fsync`
  issues a FUA / cache-flush SCSI command and doesn't return until the
  block device confirms the write is on stable media.
- Cost on the uConsole's eMMC / Class 10 SD: ~5–20 ms per fsync call
  under typical load. Cheap per event (a captured password fires once),
  expensive per frame — so the serial log stream deliberately stays on
  `flush()` only (see next section).

#### Power-cut resilience — periodic background sync

- `LootManager.__init__` starts a `threading.Thread(name="loot-backup",
  daemon=True)` running `_periodic_backup_loop`.
- `BACKUP_INTERVAL = 30` seconds. The loop sleeps in 1-second
  increments so `close()` can flip `_backup_stop = True` and the thread
  exits within 1 s instead of potentially 30 s — important because
  `close()` then closes the serial log handle the thread was touching.
- Each pass calls `flush_all()`:
  1. `fsync` the open `serial_full.log` file handle (catches each ESP32
     line that was `flush()`'d but not yet `fsync`'d).
  2. `os.sync()` — a single syscall that schedules write-out of **all**
     dirty pages across every mounted filesystem. Handles stragglers
     from the tile cache, `loot_db.json` rewrites, plugin state files,
     log rotation, anything else.
- `os.sync()` latency on a Class 10 SD: 100 ms–2 s depending on
  backpressure. Running it off the main thread keeps the 30 FPS game
  loop untouched.
- `LootManager.close()` now stops the thread, runs a final
  `flush_all + os.sync`, writes the session summary (also fsync'd),
  updates `loot_db.json`, and only then closes `self._serial_fh`.

### Changed

#### UI relayout — from 4x6 to 5x8 metrics

- Most touches are mechanical: `len(text) * 4` → `len(text) * 5`,
  `y += 6` → `y += 8` (plus 1–2 px gap), row heights bumped 10 → 12
  or 13, vertical text centering offsets `(h - 6) // 2` → `(h - 8) // 2`.
- Selected concrete changes (file: `watchdogs/app.py`):

  | Component | Before | After |
  |---|---|---|
  | HUD top badge box | `rectb(x, 3, len(label)*4+3, 9)` | `rectb(x, 2, len(label)*5+4, HUD_TOP-4)` |
  | HUD bottom counter anchors | `4/52/110/146/190/240` | `4/60/125/170/220/270` |
  | Messages stack spacing | `y -= 8` | `y -= 10` |
  | Main menu tab height | 9 | 12 |
  | Main menu item height (`IH`) | 10 | 13 |
  | Input dialog field height | 16 | 20 |
  | Input dialog box width | 280 | 320 |
  | Confirm-quit dialog | 220x64 | 260x78 |
  | GPS-wait dialog | 260x58 | 280x70 |
  | Evil Twin picker | 480x220, rows 12 | 540x260, rows 14 |
  | Portal picker | 360x180 | 420x220 |
  | Cluster popup | 300 wide, rows 12 | 340 wide, rows 14 |
  | Captured-data overlay | 500x200, rows 10 | 560x240, rows 12 |
  | MeshCore toast | `len*6+60`, h=28 | `len*5+60`, h=32 |
  | MeshCore chat bubble | `len*4+8`, h=10 | `len*5+8`, h=12 |
  | Hacker-quip bubble | `len*4+16`, h=18 | `len*5+16`, h=20 |
  | Flipper Zero row height | 12 | 13, ASCII line 7→9 |
  | MITM row height | 10/12/14 mixed | 12 normalized |
  | MITM log line height | 7 | 9 |
  | Watch PIN overlay | 200x60 | 240x80 |
  | Loot screen column rows | `y += 9` | `y += 10` (`ROW_H` constant) |
  | Loot bottom list row height | 8 | 10 |
  | Whitelist row height | 10 | 12 |
  | Flash picker row height | 13 | 14 (`ROW_H` constant) |

#### MeshCore Messenger font

- `_draw_mc_screen` used to branch on `bf = self._big_font`, producing
  two parallel code paths with `line_h = 10 if bf else 6` and
  `char_w = 8 if bf else 4`. The branch is removed and the function
  now writes plain `pyxel.text(x, y, s, col)` calls that pick up the
  monkey-patched Spleen 5x8, with `line_h = 10`, `char_w = 5`.
- `self._big_font` itself is still loaded from `assets/font_8x8.bdf`
  for potential future use but is no longer passed to any `pyxel.text`
  call in the codebase.

### Performance

- **Cluster color / radius cache.** `_update_clusters` (throttled to
  ~1 Hz by `pyxel.frame_count - self._cluster_frame < 30` plus
  zoom/center change detection) now writes two extra fields on each
  cluster dict:

  ```python
  cl["color"] = C_HACK_CYAN if bt > wifi else C_SUCCESS
  cl["radius"] = min(5 + cl["count"] // 3, 12)
  ```

  `_draw_loot_points` reads `cl["color"]` / `cl["radius"]` instead of
  calling the former `_cluster_color(points)` per cluster per frame
  (which iterated `points` twice counting types).
- **Coastline bounding boxes.** `__init__` now builds
  `self._coast_bounds = [(min_lat, max_lat, min_lon, max_lon,
  antimerid), …]` one entry per segment in `COASTLINES`
  (~100 segments, ~2200 points). `_draw_coastlines` reads the precomputed
  bounds instead of rebuilding `lats = [p[0] for p in seg]` /
  `min()`/`max()` every frame, and the previous double loop that
  called `geo_to_screen` twice per point at zoom ≥ 5 (once for the
  line pass, once for the `pset` pass) was collapsed into a single
  pass.
- **Terminal color cache.** `_term_add` now computes the display color
  at append time via a new module-level `_color_for_terminal_line(line)`
  (which still does the 11 `startswith` / `in` checks), and stores it
  in a parallel `self._terminal_colors: list[int]` deque-trimmed to
  500 like `self.terminal_lines`. `_draw_terminal` indexes
  `colors_snap[i]` instead of re-evaluating all 11 checks per visible
  line per frame.

Combined, these three changes free roughly 10–20 ms of the 33 ms / 30 FPS
budget on the uConsole, measurably smoother when the map holds many loot
points and the terminal is actively streaming.

### Fixed

- **`loot_db.json` zero-byte window.** `_save_db` writes to
  `loot_db.tmp` then calls `Path.replace` which is atomic on the same
  filesystem, but the old code didn't fsync the tmp file before the
  rename — a crash between those two calls left a zero-byte
  `loot_db.json` next boot. The fix adds `_fsync_file(fh)` inside the
  tmp-write's `with` block so the on-disk bytes are durable before the
  rename flips the directory entry.
- **MeshCore nodes and ADS-B aircraft rendered identically** to
  handshake markers (a small red skull) because `_draw_markers` didn't
  branch on `MapMarker.type`. They now have the distinct shapes
  described in Added above.

---

## [0.9.0] — 2026-04-12 (early access release)

First public release. The core gameplay loop is complete and stable. Several
advanced attacks are present in the menu but marked **(WIP)** because they
need rework before they're safe for general users.

### Added

- **Watch Dogs Go Wars Sync plugin** with full ecosystem integration:
  - Upload wardriving sessions to community server (wdgwars.pl)
  - Pull identity, stats and badges from `GET /api/me` after entering API key
  - Auto-rename LoRa MeshCore node to `WDG_<username>` so other players see you
  - Auto-validate API key, show "Invalid key" inline
  - 8 game badges synchronized two-way with the server
  - Level gate: locked until **Lv.6 WARDRIVER** (6000 XP) to avoid spam
  - Obfuscated default endpoint (URL is built into the plugin, user only
    enters their API key)
- **JanOS Loot Import plugin** — migrate old JanOS session folders into the
  game with automatic XP recalculation
- **PipBoy Watch (T-Watch Ultra)** support over BLE (NUS) — read NFC,
  send/receive LoRa MeshCore, control ESP32 attacks from the watch
- **Bruce Firmware compatibility** — accepts wardriving CSVs uploaded from
  any of 50+ ESP32 boards running Bruce
- **First-run experience**:
  - One-line installer (`curl -sL https://locosp.github.io/WatchDogsGo/install | sudo bash`)
  - `setup.sh` auto-installs Python deps, SDL2, BlueZ, tcpdump, aircrack-ng,
    rtl_433, dump1090, RPi5/CM5 GPIO library
  - `secrets.conf.example` template with documented API keys
  - Desktop launcher `.desktop` file
  - First-launch warning if user is not in `dialout` group
- **File logging** to `~/.watchdogs/last_run.log` (rotated to `previous_run.log`)
  with full unhandled-exception capture for bug reports
- **HUD shortcuts hint** — opening MeshCore Messenger now shows your unique
  node name and the available `Ctrl+N`/`Ctrl+A`/`Ctrl+C`/`Ctrl+X` shortcuts
- **Plugin system** that supports multiple plugins with overlay UIs

### Changed

- **Renamed all `janos_*` files and `JANOS_*` env vars to `watchdogs_*` /
  `WDG_*`** with full backwards compatibility:
  - `~/.janos_meshcore_key` → `~/.watchdogs_meshcore_key` (auto-migrated)
  - `~/.janos_meshcore.json` → `~/.watchdogs_meshcore.json` (auto-migrated)
  - `JANOS_WPASEC_KEY` / `JANOS_WIGLE_*` / `JANOS_GPS_*` / `JANOS_SOUND` —
    both old and new names accepted on read; new name used when writing
- **WiGLE CSV header** identifies as `appRelease=WatchDogsGo` (was `JanOS`)
- **Auto-update URL** points to `esp32-watch-dogs` repository
- **XIAO ESP32-C5 flash** uses `--before usb-reset` so firmware updates work
  from the in-game menu without holding the BOOT button (the XIAO module
  inside the uConsole has no accessible buttons)
- **Plugin command dispatch** now encodes plugin index in the command key
  (`_p_<idx>_<action>`) so multiple plugins with the same action name can
  coexist
- **MeshCore node names** are now generated from the user's Ed25519 public
  key on first run (`WatchDogs_xxxxxxxx` — 8 hex chars), and replaced by
  `WDG_<username>` after the wdgwars.pl API key is set

### Fixed

- **Upload 403** caused by missing `/api/upload/` path on the configured
  server URL
- **HTTPS on `wdgwars.pl`**: switched Traefik from TLS-ALPN-01 to HTTP-01
  challenge, fixed `acme.json` permissions, removed stale failed cert
  entries, fixed read-only SQLite database in the wardrive container
- **CSV parser crash** on MeshCore node names containing commas
  (e.g. `h,1Prz3`) — switched from `line.split(",")` to `csv.reader` and
  `csv.writer` everywhere
- **Every fresh user broadcasting as literal `WatchDogs`** on the LoRa
  mesh — the buggy default never let the unique-name generator run
- **Plugin overlays not opening** because `janos_import` and
  `wardrive_upload` shared the action name `open_overlay` and dispatch
  picked the first plugin alphabetically (which lacked a draw hook)
- **`get_local_time(timeout=5000)` blocking the LVGL UI** for 5 s on the
  PipBoy Watch firmware (handled in the watch repo, listed here for
  visibility because it affected the in-game watch screen)

### Disabled (work in progress)

These features are present in the menu but show a "[FEATURE] disabled — work
in progress" message when activated. They will return in a future update.

- **Download Map** (SYSTEM tab) — needs new tile source and resume support
- **BLE HID** (ADDONS tab) — Bluez D-Bus stack needs rewrite
- **HID Type** (ADDONS tab) — depends on BLE HID
- **BlueDucky** (ATTACK tab) — Bluetooth pairing race conditions
- **RACE Attack** (ATTACK tab) — Airoha BT exploit needs hardening

### Known limitations

- **Linux only** (Debian / Raspberry Pi OS / Ubuntu). macOS, Windows,
  Fedora, Alpine are not supported by the installer.
- **Game requires sudo** for raw socket access, GPIO and serial ports.
- **uConsole-first design** — runs on other Linux systems but the UI
  is sized for the 640x360 uConsole display.
- **Single-instance only** — running two copies of the game on the same
  machine will fight over the ESP32 serial port.

---

## [0.x history] — pre-release

The project evolved from **JanOS**, a terminal-based wardriving app for the
ClockworkPi uConsole. The first commit of the Pyxel-based "Watch Dogs Go"
frontend lands in early 2026; everything before that was JanOS work and is
not tracked in this changelog.

The major pre-release milestones were:

- **JanOS → Watch Dogs Go rewrite** (Pyxel UI, RPG progression, badges)
- **projectZero firmware** for ESP32-C5 (replaces JanOS firmware)
- **PipBoy-3000 firmware** for T-Watch Ultra
- **wdgwars.pl portal** (Django → PHP rewrite, gang warfare, anti-cheat,
  WiGLE-compatible upload, badge system)
- **Bruce Firmware integration** — pull request to upstream
  `BruceDevices/firmware` adding native upload to wdgwars.pl

[0.9.13]: https://github.com/LOCOSP/WatchDogsGo/compare/v0.9.12...v0.9.13
[0.9.12]: https://github.com/LOCOSP/WatchDogsGo/compare/v0.9.11...v0.9.12
[0.9.11]: https://github.com/LOCOSP/WatchDogsGo/compare/v0.9.10...v0.9.11
[0.9.10]: https://github.com/LOCOSP/WatchDogsGo/compare/v0.9.9...v0.9.10
[0.9.9]: https://github.com/LOCOSP/WatchDogsGo/compare/v0.9.8...v0.9.9
[0.9.8]: https://github.com/LOCOSP/WatchDogsGo/compare/v0.9.7...v0.9.8
[0.9.7]: https://github.com/LOCOSP/WatchDogsGo/compare/v0.9.6...v0.9.7
[0.9.6]: https://github.com/LOCOSP/WatchDogsGo/compare/v0.9.5...v0.9.6
[0.9.5]: https://github.com/LOCOSP/WatchDogsGo/compare/v0.9.4...v0.9.5
[0.9.4]: https://github.com/LOCOSP/WatchDogsGo/compare/v0.9.3...v0.9.4
[0.9.3]: https://github.com/LOCOSP/WatchDogsGo/compare/v0.9.2...v0.9.3
[0.9.2]: https://github.com/LOCOSP/WatchDogsGo/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/LOCOSP/WatchDogsGo/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/LOCOSP/WatchDogsGo/releases/tag/v0.9.0
