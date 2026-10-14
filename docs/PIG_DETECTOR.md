# Pig Detector

Open **SNIFF > Pig Detector** to start the opt-in, passive observation mode. Its settings are in **SNIFF > Wardrive Settings > Pig Detector**. It subscribes to an existing wardrive job when one is running; otherwise it requests the configured multi-C5 wardrive plan (including the split Wi-Fi bands plus BLE preset), or the legacy receiver. Stopping Pig Detector releases only a scan or SDR profile it started. The mode does not change SDR power. An occupied tuner is shown as busy.

## Evidence and location

The session's `pig_observations.jsonl` contains versioned observations with receiver/session/sequence, rule evidence, channel or frequency, observer fix, and any separately broadcast position. The file is capped at 4 MiB; the live map has bounded state. Flock and Axon keep their existing alerts and mute controls. New possible Raven service UUID and Utility `BodyWorn Remote` matches can alert when product alerts are enabled. WatchGuard Video OUI, i-PRO/Getac names, and other broad vendor clues are quiet leads. `Pigvision` is not a supported product rule. The checked source and evidence limitation for each rule are kept in `watchdogs/data/notable_signatures.json`.

The map uses a circle at the **receiver location** for a heard signal, a diamond at a **broadcast device position** for Remote ID or ADS-B, and a separate uncertainty circle for an **experimental RSSI area**. Heard-at coordinates never become device coordinates. A moving broadcast position gets a short trail while fresh. Drones and aircraft are neutral until their exact broadcast identifiers are entered in the user-maintained agency allowlist. Those mappings are user labels, not authentication of agency ownership.

An RSSI area requires a stable Wi-Fi or public BLE identity and at least six recent GPS observations on a spread, noncollinear route. A poor fit, GPS gap, rotating identity, single sighting, P25 activity, and anonymous RF energy produce no source area. Even a displayed area is a rough model with a radius, not a measured position. Passive Wi-Fi/BLE responses do not supply coordinates. BLE 5 extended advertisements are outside current capture coverage.

Remote ID uses bounded legacy BLE service data and a capability-gated Wi-Fi Beacon/NAN packet field from new C5 firmware. Old C5 firmware keeps its normal wardrive output. Truncated or malformed packs are ignored for positions. The decoder handles Basic ID and aircraft location; it does not interpret operator location or authentication messages.

## SDR profiles and range

Pig Detector offers **off**, **RF watchlist**, **P25 control**, **433**, and **ADS-B**, one at a time. With the `auto` setting, a nonempty watchlist selects RF watchlist; otherwise SDR stays off. The AIO SDR must already be powered on and free. Wi-Fi/BLE continue when SDR is unavailable.

Enter local RF channels as center frequency in MHz, bandwidth in kHz, and label. The watchlist visits each configured channel with `rtl_power` and shows last visit, coverage gap, measured power and noise baseline. Its threshold is relative to that channel's baseline. A rise above the threshold is **RF activity**, not proof of a police transmitter or a source position. Gain and PPM are configurable (`0` gain leaves automatic gain); watch baseline stability and decoder quality while tuning. A shorter watchlist reduces the gap between visits to each channel. Receiver antenna, terrain, local interference, duty cycle, and transmitter power still bound range. Software settings cannot guarantee a distance.

The **P25 control** profile monitors one user-selected control channel with optional boatbod OP25. WDG creates an all-talkgroup blacklist, starts no vocoder, UDP player, recording, or voice output, and checks OP25's reported tuned frequency. It stops the profile if that frequency is unavailable or leaves the chosen control channel. The settings view shows decoded control blocks and their recent rate as a tuning clue; this is not a calibrated signal-quality score. While P25 holds the tuner, the RF watchlist is paused. A mapped system/talkgroup is shown as *configured network activity*; it never places a patrol unit on the map. `433` remains an `rtl_433` sensor decoder and produces neutral heard-at observations; `ADS-B` uses the existing aircraft collector.

For the optional OP25 dependency, WDG accepts a source checkout of [boatbod/op25 commit `71abcd0ead32f86f51615ea6cc8a6a4dba4c949a`](https://github.com/boatbod/op25/commit/71abcd0ead32f86f51615ea6cc8a6a4dba4c949a). After installing its build dependencies per [upstream instructions](https://github.com/boatbod/op25/blob/71abcd0ead32f86f51615ea6cc8a6a4dba4c949a/README-installation.md), the source steps are:

```sh
git clone https://github.com/boatbod/op25.git "$HOME/op25-wdg"
git -C "$HOME/op25-wdg" checkout --detach 71abcd0ead32f86f51615ea6cc8a6a4dba4c949a
(cd "$HOME/op25-wdg" && ./install.sh)
```

Set `WDG_OP25_ROOT` to that source directory in WDG's launch environment. WDG checks the checkout's exact commit before offering P25; it neither installs OP25 nor downloads frequencies at runtime. This integration has software fixture coverage but still needs reception and no-audio validation on the actual uConsole after the separate hardware-test authorization.

## Practical limits

The AIO RTL-SDR and supplied antenna can receive local VHF, UHF, and 700/800 MHz signals when a suitable channel and signal are present. A distant P25 control site may be heard much farther away than a Wi-Fi/BLE device, but it identifies a network/site, not a nearby officer or patrol radio. Police speed radar uses frequencies beyond this tuner. `rtl_433` payloads are ordinary sensor readings unless another independently supported signature identifies something else. The user supplies local channels and identifier mappings; WDG does not fetch a public-safety database.

## Validation

The WDG gate is `./run-tests-local.sh wdg`, and C5 native/protocol tests are `./run-tests-local.sh projectzero` from the workspace root. The C5 firmware also needs both XIAO and 4 MiB Zero builds after this protocol change. Software tests do not establish RF range, signal classification accuracy in the field, antenna behavior, or actual OP25 operation on the uConsole.
