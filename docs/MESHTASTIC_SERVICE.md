# Meshtastic and Unified SX1262 Service

WatchDogsGo 0.9.65 uses one persistent hardware manager for the AIO v2
SX1262. The manager, `watchdogs-sx1262d`, is the only process allowed to open
the SPI device, claim the radio GPIO lines, reset the chip, configure the RF
switch, or change the LoRa power rail.

```text
AIO SX1262
    |
    +-- watchdogs-sx1262d
          +-- meshtasticd-wdg BrokerRadioInterface
          +-- WDG MeshCore broker client
          +-- Reticulum sidecar broker client
```

Meshtastic routing, encryption, channels, NodeDB, identity, phone protocol, and
message state remain in `meshtasticd-wdg`. The manager understands only radio
power, bounded PHY settings, CAD, RX, TX, metrics, and exclusive leases.

## Release pairing

WDG 0.9.65 is pinned to `v2.8.0-wdg.14` from
[`Smethan/meshtastic-firmware`](https://github.com/Smethan/meshtastic-firmware).
The checked-in `meshtastic-stack.json` records the exact package filename,
SHA-256, source commit, ARM64 architecture, broker API 1.1, and WDG local API
1.1. Setup refuses a package whose release metadata, embedded manifest, source,
size, checksum, architecture, API, or package layout differs from that record.
The ARM64 Debian package is built locally and published as a release artifact;
setup does not compile it on the uConsole or invoke a GitHub firmware build.
The candidate is source commit `c6aa18a6a` with package SHA-256
`1c1789332da9b4835d593b2d4232ab8f8d310eb604bacf755e33b96fea91f7ee`.

This release is a physical uConsole acceptance candidate. Automated tests and
the fake-radio conformance suite pass, but real SX1262, Bluetooth, GPS, and
power-rail behavior must still be exercised on the target hardware.

## Install or upgrade

Close WDG and run the normal setup from the WDG checkout:

```bash
sudo bash setup.sh
```

That is the only supported installer and updater. There is no separate
`install`, `adopt`, or in-app Meshtastic package-update step.

Setup performs one root-owned transaction:

1. Installs the fixed support helper and takes the package transaction lock.
2. Detects stock/fork services, AIO hardware, SPI, GPIO mapping, BlueZ, and GPS
   configuration.
3. Backs up Meshtastic configuration/state, service enable/active/masked state,
   GPS configuration, and the non-secret WDG Bluetooth identity registry.
4. Downloads the exact public five-asset release from the Smethan fork.
5. Validates `SHA256SUMS`, `compatibility.json`, the Debian package, source
   metadata, runtime dependencies, maintainer scripts, and embedded policy.
6. Boots the installed direct-radio daemon only against a disposable state
   copy to establish the authoritative private identity/channel baseline. If
   an affected 0.9.55–0.9.57 run left the old daemon pointed at the broker,
   only the direct `Lora` block is recovered from the last validated backup.
7. Installs the package, writes the hardware-only manager policy, and validates
   the broker-backed daemon against another disposable copy before atomically
   changing the live Meshtastic configuration to broker radio. A node with no
   initialized state is created once through the broker and then validated.
8. Stops, disables, and masks stock `meshtasticd.service`, while retaining its
   package and state for rollback.
9. Starts `watchdogs-sx1262d.service` and then `meshtasticd-wdg.service`.
10. Commits only after the broker socket, Meshtastic socket, services, package,
    and preserved semantic identity all pass their checks.

On failure, setup restores the previous package, Meshtastic state, manager
policy, and exact enabled/disabled/masked service states. The timestamped
backup and transaction log remain for diagnosis. Repeating setup after a
successful migration validates the already-pinned stack and leaves its state
unchanged.

The installed layout is:

| Item | Path/name |
|---|---|
| Package | `meshtasticd-wdg` |
| Meshtastic binary | `/usr/lib/meshtasticd-wdg/meshtasticd` |
| Manager entry point | `/usr/lib/watchdogs-sx1262d/watchdogs-sx1262d` |
| Manager service | `watchdogs-sx1262d.service` |
| Fork service | `meshtasticd-wdg.service` |
| Stock service | `meshtasticd.service` (masked after successful migration) |
| Shared config | `/etc/meshtasticd/config.yaml` |
| Shared state | `/var/lib/meshtasticd` |
| Hardware policy | `/etc/watchdogs/sx1262.yaml` |
| Manager socket | `/run/watchdogs/sx1262d.sock` |
| WDG daemon socket | `/run/meshtasticd/wdg.sock` |
| Package cache | `/var/cache/watchdogs/meshtasticd-wdg` |
| Transaction backups | `/var/backups/meshtasticd-wdg` |

## Lease and power behavior

The manager exposes one active mode: `MESHTASTIC`, `MESHCORE`, `RETICULUM`, or
`OFF`, with `STARTING`, `TRANSITION`, and `FAULT` used during recovery.
Meshtastic is the powered boot/default/fallback mode. MeshCore and Reticulum
receive temporary leases while WDG is alive and renew them once per second.
Five seconds without a valid heartbeat revokes the lease, resets the chip, and
returns to Meshtastic.

Every request includes the current ownership generation. A delayed request
from a revoked mode is rejected before it can touch reconfigured hardware.
Mode changes quiesce the old protocol and its Bluetooth frontend, finish or
bound any in-flight TX, and reset the chip. Because a reset clears the SX1262
packet type, broker API 1.1 then performs complete LoRa, TCXO, regulator,
current-limit, and RF-switch initialization before granting the new generation.
The active client applies its bounded PHY configuration and starts RX before
WDG reports the protocol ready or exposes its Bluetooth frontend. An
initialization failure enters `FAULT` and grants no lease.

SYSTEM LoRa power controls are administrative manager commands:

- **Force OFF** rejects new TX immediately and gives the active protocol up to
  two seconds to quiesce its worker and Bluetooth frontend. It then
  resets/sleeps the chip, drives the rail low, persists OFF, and suppresses all
  fallback/GATT. WDG waits for confirmed `OFF` and rail-low read-back.
- **Force ON** raises the rail, waits for stabilization, probes the chip, and
  activates WDG's selected protocol when WDG has a live controller session;
  otherwise it activates Meshtastic.

WDG displays manager read-back state. It never falls back to `pinctrl`, direct
GPIO, or direct SPI when the manager is unavailable.

## Mode-exclusive Bluetooth

BlueZ owns all link keys and authentication. WDG stores only a bounded,
non-secret record tying one retained phone identity to one stable controller
address. The same authenticated bond is retained across protocol changes.

- **Meshtastic mode:** only Meshtastic GATT is registered and advertised. The
  official Meshtastic app may reconnect; MeshMapper finds no WDG service.
- **MeshCore mode:** Meshtastic GATT is unregistered and its phone is
  disconnected without deleting the bond. Only the MeshCore Nordic-UART
  service is advertised; the Meshtastic app finds no compatible service.
- **Reticulum or OFF:** neither phone-radio GATT application is registered.

Open the 120-second authenticated pairing window from WDG. BlueZ generates the
six-digit passkey WDG displays. Enter it in the phone app. Do not pre-pair from
the generic Bluetooth settings screen. `RANDOM_PIN` is the supported shared
bond mode; fixed-PIN DisplayOnly pairing remains unsupported by BlueZ and fails
closed.

GATT registration and advertising are asynchronous. A frontend advertises
only after its GATT application is registered, tracks only objects BlueZ
accepted, disconnects the retained device on deactivation, restores the prior
adapter pairable state, releases the serialized pairing-agent lease, and
re-registers the active frontend after `bluetoothd` restarts.

## Shared AIO GPS ownership

The AIO GPS UART has exactly one raw reader: managed `gpsd`. Linux does not
duplicate a TTY byte stream between consumers, so direct readers would split
NMEA data and recreate the reported fix flicker.

Setup migrates Meshtastic to:

```yaml
GPS:
  GpsdHost: 127.0.0.1
  GpsdPort: 2947
```

WDG consumes the same local gpsd JSON stream on a dedicated bounded reader
thread and publishes the latest TPV/SKY snapshot atomically. Provider order is:

1. Explicitly selected external GPS.
2. Managed AIO `gpsd`.
3. ModemManager only when AIO GPS is not configured or LTE GNSS is explicitly
   selected.
4. Other documented external detection.

While managed AIO gpsd is configured, a gpsd failure is reported and retried;
WDG does not silently reopen the UART or switch to ModemManager. Transport,
data-flow, valid-fix, stale-fix, explicit no-fix, and disconnected states are
tracked separately. A valid TPV is fresh for at most five seconds, but a newer
explicit `mode < 2` report invalidates it immediately. No stale fix survives a
transport reconnect.

Radio mode changes do not restart gpsd, change WDG's provider, reconnect
Meshtastic's gpsd client, or clear WDG's current fix.

WDG's diagnostic snapshot tracks independent report, TPV, and SKY ages;
visible and used satellite counts; first SKY/satellite/fix timing; reconnect
and provider-change counters; and the last observed GPS rail state. The LoRa
health screen shows the current report/TPV/SKY ages, satellite counts,
navigation state, and power-observation age. Missing reports display as `n/a`,
allowing a cold satellite acquisition to be distinguished from a disconnected
gpsd transport. These are telemetry changes only: WDG does not automatically
cycle GPS power or alter provider/fix-freshness behavior.

## Verification and troubleshooting

After setup, inspect the two active services and sockets:

```bash
systemctl status watchdogs-sx1262d.service meshtasticd-wdg.service
sudo journalctl -u watchdogs-sx1262d.service -n 100 --no-pager
sudo journalctl -u meshtasticd-wdg.service -n 100 --no-pager
sudo test -S /run/watchdogs/sx1262d.sock
sudo test -S /run/meshtasticd/wdg.sock
dpkg-query -W meshtasticd-wdg
```

Do not unmask/start stock `meshtasticd.service` beside this stack and do not
open `/dev/spidev1.0` or the LoRa GPIOs from another program. A broker `FAULT`
is intentionally visible; no protocol client silently reopens hardware.

For the hardware acceptance pass:

1. Confirm the old Meshtastic identity, channels, NodeDB, and phone bond remain.
2. Reconnect the official app in Meshtastic mode and confirm MeshMapper sees
   nothing.
3. Switch to MeshCore, confirm the Meshtastic app disconnects/sees nothing,
   and connect MeshMapper with the retained bond.
4. Switch to Reticulum and confirm both phone-radio services disappear.
5. Repeat mode changes, restart `bluetoothd`, and check only the active GATT
   frontend returns.
6. Wardrive with continuous gpsd TPV data while changing modes; verify no
   provider changes, UART disconnects, or false no-fix intervals.
7. Exercise real no-fix, WDG crash fallback, manager restart, Force OFF during
   RX/TX, and Force ON restoration.
8. Exchange over-air traffic with real peers in all three modes.

Keep `/var/cache/watchdogs/meshtasticd-wdg` and
`/var/backups/meshtasticd-wdg` until those checks pass. If setup itself fails,
use its reported transaction log and retained backup rather than manually
changing packages or service masks.

## Message and phone API behavior

The restricted WDG socket never consumes the firmware's global unrestricted
PhoneAPI queue. Meshtastic BLE therefore coexists with WDG, while a second
arbitrary TCP/serial PhoneAPI client remains unsupported.

Mesh Messenger gives each accepted local send a correlation ID and updates the
original row. Channel/broadcast messages use `want_ack=false` and become
`SENT` when accepted for transmission. Direct messages use `want_ack=true` and
remain non-terminal until API 1.1 reports `DELIVERED` or `FAILED`; tracking is
bounded and times out after 120 seconds.

With one Bluetooth controller and no connected phone, WDG may request a short
host-scan lease. A connected phone has priority. Use a second Bluetooth adapter
for uninterrupted phone GATT plus host BLE wardriving.
