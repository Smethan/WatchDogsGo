# Pig Detector

Open **SNIFF > Pig Detector** to start the opt-in, passive observation mode. Its settings are in **SNIFF > Wardrive Settings > Pig Detector**. It subscribes to an existing wardrive job when one is running; otherwise it requests the configured multi-C5 wardrive plan (including the split Wi-Fi bands plus BLE preset), or the legacy receiver. Stopping Pig Detector releases only a scan or SDR profile it started. The mode does not change SDR power. An occupied tuner is shown as busy.

For a dedicated P25 control-channel browser, site search, and recent decode log, use [P25 Monitor](P25_MONITOR.md) under **SNIFF**. The Pig Detector P25 profile remains available for combined Pig sessions.

## Evidence and location

The session's `pig_observations.jsonl` contains versioned observations with receiver/session/sequence, rule evidence, channel or frequency, observer fix, and any separately broadcast position. The file is capped at 4 MiB; the live map has bounded state. Flock and Axon keep their existing alerts and mute controls. New possible Raven service UUID and Utility `BodyWorn Remote` matches can alert when product alerts are enabled. WatchGuard Video OUI, i-PRO/Getac names, and other broad vendor clues are quiet leads. `Pigvision` is not a supported product rule. The checked source and evidence limitation for each rule are kept in `watchdogs/data/notable_signatures.json`.

The map uses a circle at the **receiver location** for a heard signal, a diamond at a **broadcast device position** for Remote ID or ADS-B, and a separate uncertainty circle for an **experimental RSSI area**. Heard-at coordinates never become device coordinates. A moving broadcast position gets a short trail while fresh. Drones and aircraft are neutral until their exact broadcast identifiers are entered in the user-maintained agency allowlist. Those mappings are user labels, not authentication of agency ownership.

An RSSI area requires a stable Wi-Fi or public BLE identity and at least six recent GPS observations on a spread, noncollinear route. A poor fit, GPS gap, rotating identity, single sighting, P25 activity, and anonymous RF energy produce no source area. Even a displayed area is a rough model with a radius, not a measured position. Passive Wi-Fi/BLE responses do not supply coordinates. BLE 5 extended advertisements are outside current capture coverage.

Remote ID uses bounded legacy BLE service data and a capability-gated Wi-Fi Beacon/NAN packet field from new C5 firmware. Old C5 firmware keeps its normal wardrive output. Truncated or malformed packs are ignored for positions. The decoder handles Basic ID and aircraft location; it does not interpret operator location or authentication messages.

## SDR profiles and range

Pig Detector offers **off**, **RF watchlist**, **P25 control**, **433**, and **ADS-B**, one at a time. With the `auto` setting, a nonempty watchlist selects RF watchlist; otherwise SDR stays off. The AIO SDR must already be powered on and free. WDG checks the rail again when an SDR action starts, so a change made outside WDG is reflected without restarting the app. It does not change the saved rail state at boot. Wi-Fi/BLE continue when SDR is unavailable.

Enter local RF channels as center frequency in MHz, bandwidth in kHz, and label. The watchlist visits each configured channel with `rtl_power` and shows last visit, coverage gap, measured power and noise baseline. Its threshold is relative to that channel's baseline. A rise above the threshold is **RF activity**, not proof of a police transmitter or a source position. Gain and PPM are configurable (`0` gain leaves automatic gain); watch baseline stability and decoder quality while tuning. A shorter watchlist reduces the gap between visits to each channel. Receiver antenna, terrain, local interference, duty cycle, and transmitter power still bound range. Software settings cannot guarantee a distance.

The **P25 control** profile monitors one user-selected control channel with optional boatbod OP25. WDG creates an all-talkgroup blacklist, starts no vocoder, UDP player, recording, or voice output, and checks OP25's reported tuned frequency. It stops the profile if that frequency is unavailable or leaves the chosen control channel. When P25 is selected, the bottom HUD shows a compact state such as `P25:WAIT`, `P25:0`, or the decoded block count; Pig Detector settings show the full state, recent rate, or a startup error. A rising block count is a tuning clue, not a calibrated signal-quality score. While P25 holds the tuner, the RF watchlist is paused. A mapped system/talkgroup is shown as *configured network activity*; it never places a patrol unit on the map. `433` remains an `rtl_433` sensor decoder and produces neutral heard-at observations; `ADS-B` uses the existing aircraft collector.

The AIO RTL-SDR has one tuner. ADS-B uses the existing `readsb.service` SBS feed when that service is running; an empty feed waits for aircraft without taking the tuner away from `readsb`. If the feed is unavailable, ADS-B can start its own `dump1090` receiver when installed. P25, 433 MHz, RF watchlist, and a private `dump1090` receiver temporarily pause an active `readsb.service` to claim the tuner. WDG restores the service only when it paused it itself. If a handoff fails, the profile stays stopped and the error appears in Pig Detector settings and the terminal. `P25:0` means OP25 opened the tuner but has not decoded a control block; it does not confirm the selected frequency is active.

If `readsb` takes more than four seconds to stop, WDG reports a bounded handoff failure instead of claiming a tuner it cannot verify is free. Its recovery worker waits for the systemd job to settle and then restores the feed; retry the requested non-ADS-B profile after that recovery. Normal WDG shutdown may wait up to 95 seconds for a pending restore. A restore error after that limit needs the service and USB receiver checked separately.

For the optional OP25 dependency, WDG accepts a source checkout of [boatbod/op25 commit `71abcd0ead32f86f51615ea6cc8a6a4dba4c949a`](https://github.com/boatbod/op25/commit/71abcd0ead32f86f51615ea6cc8a6a4dba4c949a). Clone and pin the source:

```sh
git clone https://github.com/boatbod/op25.git "$HOME/op25-wdg"
git -C "$HOME/op25-wdg" checkout --detach 71abcd0ead32f86f51615ea6cc8a6a4dba4c949a
```

Build dependencies are described in [upstream's installation guide](https://github.com/boatbod/op25/blob/71abcd0ead32f86f51615ea6cc8a6a4dba4c949a/README-installation.md). Review the package transaction before using its `install.sh` on an existing uConsole. On the Parrot 7.4 image with `sdrpp-brown` and `tar1090`, its `gnuradio-dev`/`libvolk-dev` dependencies would remove those installed SDR packages. The uConsole setup instead installed GNU Radio runtime packages with apt's `--no-remove` guard and built OP25 against separately extracted development headers, leaving both packages installed.

The bundled `run.sh` sets `WDG_OP25_ROOT` to the launch user's `~/op25-wdg` when that directory exists. An explicit `WDG_OP25_ROOT` in the launch environment takes precedence. WDG checks the checkout's exact commit before offering P25; it neither installs OP25 nor downloads frequencies at runtime. This integration has software fixture coverage but still needs reception and no-audio validation on the actual uConsole after the separate hardware-test authorization.

## Practical limits

The AIO RTL-SDR and supplied antenna can receive local VHF, UHF, and 700/800 MHz signals when a suitable channel and signal are present. A distant P25 control site may be heard much farther away than a Wi-Fi/BLE device, but it identifies a network/site, not a nearby officer or patrol radio. Police speed radar uses frequencies beyond this tuner. `rtl_433` payloads are ordinary sensor readings unless another independently supported signature identifies something else. The user supplies local channels and identifier mappings; WDG does not fetch a public-safety database.

## Validation

The WDG gate is `./run-tests-local.sh wdg`, and C5 native/protocol tests are `./run-tests-local.sh projectzero` from the workspace root. The C5 firmware also needs both XIAO and 4 MiB Zero builds after this protocol change. Software tests do not establish RF range, signal classification accuracy in the field, antenna behavior, or actual OP25 operation on the uConsole.
