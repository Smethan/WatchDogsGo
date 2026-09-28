# Meshtastic Service Integration

WatchDogsGo supports two Meshtastic daemon configurations:

- **WDG fork:** the ARM64 `meshtasticd-wdg` package built from
  [`Smethan/meshtastic-firmware`](https://github.com/Smethan/meshtastic-firmware).
  WDG talks to its restricted local socket while the official phone app may use
  the standard Meshtastic GATT service over BlueZ.
- **Stock daemon:** an independently installed `meshtasticd`. WDG uses the
  legacy unrestricted PhoneAPI on `127.0.0.1:4403`. Phone BLE coexistence
  through the WDG fork is unavailable in this mode.

The settings are under **SNIFF > Wardrive Settings > LoRa settings >
Meshtastic service and phone BLE**.
`AUTO` prefers the fork socket and also treats an installed fork service as
authoritative while it starts. `FORK_SOCKET` requires the fork. `LEGACY_TCP`
requires the stock service. WDG will not open the legacy TCP API against an
installed BLE-enabled fork because doing so could consume the same global
phone queues as the phone.

## Current release status

WDG 0.9.46 is paired with firmware `v2.8.0-wdg.7`, which adds WDG local API 1.1
delivery correlation and authenticated phone-bond controls. Upgrade and verify
the firmware package first, then update WDG. That order gives WDG the required
API on its first post-upgrade connection; API 1.0 lacks shared-bond adoption and
cleanup even though its earlier node/message controls remain usable.

The source branches and tags drive the build, package, updater, and rollback
paths. After the one-time manual migration and adoption described below,
**Update service** can install only a compatible, tagged ARM64 release from
`Smethan/meshtastic-firmware`. It reports that no compatible release exists
until those assets are actually published.
Android phone interoperability and the uConsole radio/Bluetooth soak remain
hardware acceptance work. No physical-device acceptance is claimed by these
code changes.

The daemon stores the retained phone address, exact controller address, and
random-PIN authentication provenance as one bounded versioned identity record.
An older address-only record is loaded as `unknown`, not inferred to be secure
from the current Bluetooth setting. It may be inspected and explicitly adopted
through WDG after exact BlueZ verification, but it cannot authorize GATT access
until that upgrade succeeds. A record written for another controller is handled
the same way.

The firmware workflow keeps hardware acceptance separate from publication. A
matching tag builds once and stages the verified files in a draft GitHub
release. The authenticated repository owner can download that draft with `gh`
and test its exact package on the uConsole. After acceptance, an explicit
`publish_draft` workflow run revalidates and publishes those same bytes without
rebuilding them. Draft releases are invisible to the in-game updater.

## Setup and package layout

Run the normal WDG setup from the checkout as the login user:

```bash
sudo bash setup.sh
```

Setup installs the root-owned, argument-allowlisted helper at
`/usr/local/libexec/watchdogs-meshtastic`, its protected release validator, a
narrow sudoers rule for the login account, private cache/backup directories,
and `/etc/meshtasticd/wdg-portduino.yaml`. It records the real login UID in the
policy. Rerunning setup updates that UID without replacing the selected phone
adapter or other valid policy values. Once the `meshtasticd` account exists,
the policy is `root:meshtasticd` mode `0640`, so the daemon can read it without
allowing group writes. Setup creates the `watchdogs` system
group, adds the login account when needed, and repairs `/run/lock/watchdogs` to
`root:watchdogs` mode `2750`. It precreates
`/run/lock/watchdogs/aio-sx1262.lock` as `root:watchdogs` mode `0660`; group
members can lock that inode but cannot replace it. Log out and back in if setup
reports a new group membership; the current login session will not acquire it
retroactively.

If setup changes the policy while `meshtasticd-wdg.service` is active, it
prints the exact restart command. The daemon reads the policy at startup, so
run that command after setup; setup does not silently interrupt an active mesh
session.

Setup does **not** silently install or start a firmware package. The helper
accepts only release tags shaped like `vX.Y.Z-wdg.N`; the unprivileged app
cannot pass it a URL, package path, service name, or arbitrary command.

The fork package installs side by side with stock Meshtastic:

| Item | Path/name |
|---|---|
| Package | `meshtasticd-wdg` |
| Binary | `/usr/lib/meshtasticd-wdg/meshtasticd` |
| Fork service | `meshtasticd-wdg.service` |
| Stock service | `meshtasticd.service` |
| Shared config | `/etc/meshtasticd/config.yaml` |
| Shared state | `/var/lib/meshtasticd` |
| WDG host policy | `/etc/meshtasticd/wdg-portduino.yaml` |
| WDG socket | `/run/meshtasticd/wdg.sock` |
| Package cache | `/var/cache/watchdogs/meshtasticd-wdg` |
| Transaction backups | `/var/backups/meshtasticd-wdg` |

The two systemd units conflict, so only one daemon can be active. The fork runs
as the `meshtasticd` system account, starts after BlueZ when it is available,
and continues radio/local-API operation if phone Bluetooth is unavailable.
Closing WDG disconnects only the restricted client; it leaves the selected
daemon running for mesh reception and phone reconnects.

## Shared AIO GPS ownership

The AIO GPS UART must have exactly one raw reader. Linux does not duplicate a
TTY byte stream between consumers: two direct readers divide NMEA bytes and can
both report an open device while neither receives complete position sentences.

When `/etc/meshtasticd/config.yaml` claims the CM4/CM5 platform GPS with
`GPS.SerialPath`, rerunning `sudo bash setup.sh` installs and configures gpsd as
the sole `/dev/serial0` owner. Setup preserves one-time backups at:

```text
/etc/default/gpsd.wdg-before-shared-gps
/etc/meshtasticd/config.yaml.wdg-before-shared-gps
```

Meshtastic is migrated to its native gpsd input:

```yaml
GPS:
  GpsdHost: 127.0.0.1
  GpsdPort: 2947
```

WDG records the authoritative ownership policy in
`/etc/watchdogs/gpsd.conf` and consumes the same local gpsd JSON stream. If
gpsd is temporarily unavailable, WDG fails closed and retries the broker; it
does not open the raw UART and recreate the competing-reader failure. Explicit
external GPS paths and ModemManager GNSS remain separate provider choices.

The boot screen's **GPS transport** line confirms only that a provider is
available. The map/HUD reports the independent satellite-fix state. A powered
receiver may legitimately need time and a clear view of the sky before the
first fix.

## Install and update transaction

The first stock-to-fork migration requires a human hardware check because the
stock daemon does not expose the restricted, read-only semantic status needed
to establish an authoritative baseline. The protected updater refuses to
install the first fork package and will not validate a new package against
itself. For that one migration:

1. Install WDG normally so `setup.sh` places the root-owned operator command at
   `/usr/local/bin/watchdogs-meshtastic-release`. Run it as the desktop user,
   not through `sudo`, because draft commands use that user's authenticated
   GitHub CLI session. For an ordinary public first release:

   ```bash
   watchdogs-meshtastic-release install-public vX.Y.Z-wdg.N
   ```

   For a draft that still needs the first physical hardware test, record the
   successful tag-push workflow run ID and producer attempt from the draft
   notes, authenticate `gh`, and run:

   ```bash
   watchdogs-meshtastic-release install-draft vX.Y.Z-wdg.N \
     --run-id 123456789 --attempt 1
   ```

   Both first-install commands start the new fork for the mandatory local
   hardware check but leave it unadopted. The draft tool additionally requires
   the exact repository, workflow path, tag, run ID, attempt,
   successful push conclusion, source commit, unexpired canonical artifact
   name, and five-file set. It downloads as the unprivileged user, copies those
   files into the fixed root-only inbox, then the protected helper independently
   repeats the release manifest, checksums, package, dependency,
   maintainer-script, privileged-policy, ARM64, and host checks before `apt`
   sees the sealed package. It never downloads the mutable draft attachment.
2. Verify the phone, radio, identity, and channels on the uConsole.
3. Publish and adopt that already-tested draft using the same run ID and
   producer attempt:

   ```bash
   watchdogs-meshtastic-release publish-adopt-draft vX.Y.Z-wdg.N \
     --run-id 123456789 --attempt 1 --yes
   ```

   `--yes` makes the permanent publication action explicit. The publishing
   workflow independently fetches the same non-expired artifact, rejects extra
   or non-regular entries, and byte-compares all five draft assets. The command
   waits for a public, non-prerelease release before adoption. If the artifact
   expired, create and test a new candidate tag; the draft alone is not
   sufficient provenance.
4. To adopt a public package that is already installed, or perform a later
   protected update, use:

   ```bash
   watchdogs-meshtastic-release adopt vX.Y.Z-wdg.N
   watchdogs-meshtastic-release update vX.Y.Z-wdg.N
   ```

   `adopt` snapshots both daemon states, rejects overlapping or transitioning
   ownership, stops only the active daemon, runs the protected adoption, and
   restores exactly the daemon that had been active. It does not alter the
   enabled/disabled selection. `status` shows both service states without
   mutation.

Adoption resolves and validates that exact Smethan release, requires its
package version and every installed package payload to match the verified
release, starts the candidate only against a disposable copy of the stopped
live state, verifies identity, channels, and effective MAC, confirms live state
did not change, and seeds the private rollback cache. It does not start either
service. If `General.MACAddress` is absent, adoption first proves a conservative
pin against the disposable copy and then atomically writes only that setting to
the live primary config. YAML it cannot edit without changing meaning fails
closed with instructions to set the MAC manually. A tag mismatch or any
validation failure leaves nothing adopted.

After adoption, **Update service** resolves a published release from
`Smethan/meshtastic-firmware`. Before replacing the installed package, the
protected helper:

1. Validates the release tag, compatibility manifest, SHA-256, package size,
   ARM64 ELF, fixed package contents, systemd unit, and narrow BlueZ D-Bus
   policy.
2. Stops both possible radio-owning services and creates a private timestamped
   backup of Meshtastic configuration, state, and the exact service states.
3. Requires the adopted, verified cached copy of the currently installed fork
   package before replacing it, so a package rollback is possible.
4. Installs the candidate without maintainer scripts starting a daemon.
5. Starts the candidate against a copied state/config directory and a private
   socket, with Bluetooth disabled, then checks its local API identity and
   channel summary.
6. Starts the live fork and confirms the same node ID, long/short names,
   public/private-key presence, ordered channel indexes/names/roles, and PSK
   presence. Secret bytes are never printed or copied into status JSON.
7. Enables the fork and records the rollback transaction only after the health
   checks pass.

Volatile node history may change while the daemon runs. Identity, channel, and
critical configuration files may not. A failed install restores the package,
state, and the exact active/enabled service selection captured before the
transaction.

The in-game updater also treats a retained direct-radio handoff snapshot as an
ownership barrier. It restores that older snapshot before release lookup or
any privileged package/service mutation, and aborts the update if exact
restoration cannot be confirmed. The update worker is non-daemonized, and WDG
shutdown waits for it to finish package validation and any automatic rollback
before closing the control socket or exiting.

The most recent completed transaction can be restored with:

```bash
sudo /usr/local/libexec/watchdogs-meshtastic rollback
```

Do not delete `/var/cache/watchdogs/meshtasticd-wdg` or
`/var/backups/meshtasticd-wdg` until phone, WDG, and radio checks pass; those
directories contain the verified rollback package and state backup.

## SX1262 ownership

The fork service holds an exclusive `flock` for its process lifetime at:

```text
/run/lock/watchdogs/aio-sx1262.lock
```

WDG's direct MeshCore driver acquires the same lock before opening SPI or
claiming GPIO. Switching to MeshCore stops the exact active Meshtastic service,
waits for the lock, and then starts direct radio access. Switching back closes
the direct worker and SPI/GPIO first, releases the lock, starts the previously
selected service, and waits for its socket or TCP endpoint. A failed handoff
restores the prior service when possible. Turning LoRa off stops the current
owner before WDG changes the AIO power rail.

Before a handoff, WDG captures both services as one logical snapshot of
`LoadState`, `ActiveState`, and `UnitFileState`. It proceeds only from stable
states it can reproduce exactly: a missing unit, or a loaded unit that is
active/inactive and enabled/disabled. Transitional states receive a bounded
settling wait; unknown, failed, linked, masked, `enabled-runtime`, inconsistent,
or double-active states fail closed before mutation. Once mutation starts, the
snapshot is retained until both units again match it. A failed restore blocks
new daemon selection, client startup, direct-radio suspension, and package
updates rather than replacing the rollback token with a snapshot of partial
state.

All ownership-changing workers share one application barrier. Power-off first
invalidates pending starts, then waits for every displaced handoff worker to
exit before stopping the owner and cutting GPIO power. WDG shutdown likewise
joins every registered handoff worker. This prevents a late completion from
restarting a daemon or claiming SPI after another path has taken ownership.

If lock acquisition reports another owner, do not force-remove the lock file.
Inspect the services instead:

```bash
systemctl status meshtasticd-wdg.service meshtasticd.service
```

## Phone Bluetooth and pairing

The fork exports the standard Meshtastic GATT service through BlueZ. The
official phone app remains a normal Meshtastic client; WDG does not proxy the
phone protocol. WDG 0.9.46 treats the authenticated phone as one bond belonging
to a stable controller MAC, not as separate MeshCore and Meshtastic bonds. If
MeshCore already retained a phone on the controller Meshtastic is configured
to use, WDG asks API 1.1 to adopt that exact BlueZ device. Adoption succeeds
only when the controller and phone addresses match and BlueZ reports the device
as paired, bonded, and trusted. It never creates a new bond or stores BlueZ key
material.

With no retained phone, open an explicit 120-second window with **Open
pairing**. WDG first requires the daemon's authenticated `RANDOM_PIN` policy.
The six-digit BlueZ passkey appears in WDG status and the daemon journal. One
phone bond is retained and shared with MeshCore on that controller. Disconnect
and choose **Forget paired phone** before pairing a replacement. The
Meshtastic forget flow removes the daemon identity and exact BlueZ device, then
removes WDG's metadata; it never bulk-removes unrelated devices.

`NO_PIN` remains an upstream Just Works mode, but it is not accepted by WDG's
shared-bond workflow. `RANDOM_PIN` uses BlueZ's generated six-digit passkey.
BlueZ Agent1 does not provide a supported way for a DisplayOnly Linux LE
peripheral to force the upstream fixed PIN, so a configured `FIXED_PIN` fails
closed with `fixed_pin_unsupported`; the daemon does not advertise or silently
substitute another pairing mode. WDG explicitly selects `RANDOM_PIN` before
opening or reconciling its pairing flow.

The controller-keyed non-secret registry is the invoking user's
`~/.watchdogs/bluetooth_bonds.json`. WDG securely opens the directory with
`O_DIRECTORY|O_NOFOLLOW` and performs file operations relative to that verified
descriptor. An owner-correct legacy `~/.watchdogs` directory created as `0755`
is hardened in place to `0700` with `fchmod`; a symlinked or wrong-owner path
still fails closed. The existing registry must be a real owner-correct file
with exact mode `0600`. Writes are atomic. It stores only controller/phone
addresses, display name, random-PIN authentication label, source backend,
active/cleanup state, and timestamp. Passkeys and Bluetooth link keys remain
exclusively under BlueZ.

The prior MeshCore address/name settings are a one-release compatibility
mirror. WDG migrates them only if a read-only BlueZ `Device1` lookup finds the
exact address under the exact selected `Adapter1` and all of `Paired`,
`Bonded`, and `Trusted` are true. Missing or ambiguous state is not guessed.

If **Forget paired phone** is run while MeshCore owns the controller, BlueZ is
cleaned immediately but the stopped daemon may still retain the old address.
WDG records `cleanup_pending`; on the next Meshtastic API 1.1 connection it
clears only a matching daemon identity, then removes the tombstone. A mismatch
fails closed, and another phone cannot pair until the cleanup is resolved.

Store adapter choices by controller MAC, not by `hci0`/`hci1`; Linux controller
numbers can change after reboot or USB changes. **Phone BLE adapter** selects
the fork's controller. **Host scan adapter** selects Bleak's Host BLE scanner.

Only one unrestricted PhoneAPI client can own the firmware's global phone
queues. The owner is shown as `none`, `bluetooth`, `bluetooth_pending`, or
`tcp`. The BLE and TCP transports coordinate that lease; the restricted WDG
socket never consumes it. A phone may therefore coexist with WDG, but an
arbitrary simultaneous TCP/serial PhoneAPI client is unsupported.

## Message delivery state

Mesh Messenger gives every accepted local send a correlation ID and updates
that original row rather than appending a second result. Channel/broadcast
messages use `want_ack=false`: once the daemon accepts one for transmission,
its terminal display state is `SENT`. Direct messages use `want_ack=true` and
remain non-terminal at `SENT` until API 1.1 reports `DELIVERED` or `FAILED`.
The daemon bounds direct delivery tracking and reports failure if Meshtastic
returns a routing error or no terminal result arrives within 120 seconds.

This distinction matters on sparse meshes. Broadcast packets do not have one
specific peer that can acknowledge them, so requesting an acknowledgement
caused unnecessary retries and eventual failure messages; that behavior could
look like a service crash even though `meshtasticd-wdg` remained running.

With two powered Bluetooth controllers, assign distinct stable MACs to phone
BLE and Host BLE scanning. With one controller and no connected phone, WDG asks
the daemon for a scan lease of at most 20 seconds; advertising resumes on
release, expiry, or WDG disconnect. A connected phone has priority. If the
controller cannot scan and maintain the link, WDG pauses Host BLE and leaves
the phone connected. **Retry shared adapter** clears that session-only pause
after the scan stops. A second USB Bluetooth adapter is the reliable choice for
uninterrupted phone BLE plus Host BLE wardriving.

WDG also uses a separate pairing-agent lease, capped at 120 seconds, while its
PipBoy Watch flow owns BlueZ. The lease is released on completion, error,
timeout, or WDG disconnect. WDG shutdown stops Host BLE, watch readiness, and
pairing work while the restricted socket is still available for lease release.
If cleanup cannot be confirmed, it does not deliberately close that control
socket during the remaining application cleanup.

## Diagnostics

For the fork service:

```bash
sudo systemctl status meshtasticd-wdg.service
sudo journalctl -u meshtasticd-wdg.service -n 100 --no-pager
ls -l /run/meshtasticd/wdg.sock
```

For stock Meshtastic:

```bash
sudo systemctl status meshtasticd.service
sudo journalctl -u meshtasticd.service -n 100 --no-pager
```

The WDG Meshtastic Service screen distinguishes daemon connectivity, selected
backend, radio state, phone BLE state, pairing PIN, and full-client owner. If a
policy error disables only the socket/BLE surfaces, inspect
`/etc/meshtasticd/wdg-portduino.yaml`, rerun `sudo bash setup.sh`, and restart
the fork service. Radio operation is intentionally kept separate from those
restricted interfaces.

For an apparent app or worker crash, retain both
`~/.watchdogs/last_run.log` and `~/.watchdogs/previous_run.log`. WDG 0.9.46
adds fatal-signal dumps for Python, full unhandled-thread tracebacks, and an
explicit clean/unclean game-exit line; the next launch rotates the failed run
into `previous_run.log`.

The API, storage, and UI paths have automated coverage. WDG 0.9.46 and firmware
`v2.8.0-wdg.7` were not validated on a physical uConsole or phone/controller
combination as part of this implementation.

See [Meshtastic WDG local API](MESHTASTIC_WDG_API.md) for the JSON protocol and
its security boundary.
