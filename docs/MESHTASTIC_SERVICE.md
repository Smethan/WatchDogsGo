# Meshtastic Service Integration

WatchDogsGo supports two Meshtastic daemon configurations:

- **WDG fork:** the ARM64 `meshtasticd-wdg` package built from
  [`Smethan/meshtastic-firmware`](https://github.com/Smethan/meshtastic-firmware).
  WDG talks to its restricted local socket while the official phone app may use
  the standard Meshtastic GATT service over BlueZ.
- **Stock daemon:** an independently installed `meshtasticd`. WDG uses the
  legacy unrestricted PhoneAPI on `127.0.0.1:4403`. Phone BLE coexistence
  through the WDG fork is unavailable in this mode.

The settings are under **SNIFF > Wardrive Settings > Meshtastic Service**.
`AUTO` prefers the fork socket and also treats an installed fork service as
authoritative while it starts. `FORK_SOCKET` requires the fork. `LEGACY_TCP`
requires the stock service. WDG will not open the legacy TCP API against an
installed BLE-enabled fork because doing so could consume the same global
phone queues as the phone.

## Current release status

The source branches add the build, package, updater, and rollback paths. They do
not by themselves publish a Meshtastic release. After the one-time manual
migration and adoption described below, **Update service** can install only a
compatible, tagged ARM64 release from `Smethan/meshtastic-firmware`. It reports
that no compatible release exists until those assets are actually published.
Android phone interoperability and the uConsole radio/Bluetooth soak remain
hardware acceptance work. No physical-device acceptance is claimed by these
code changes.

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

## Install and update transaction

The first stock-to-fork migration requires a human hardware check because the
stock daemon does not expose the restricted, read-only semantic status needed
to establish an authoritative baseline. The protected updater refuses to
install the first fork package and will not validate a new package against
itself. For that one migration:

1. Download the immutable Actions artifact from the successful tag workflow,
   not the mutable draft-release attachment. Record the run ID and producer
   attempt printed in the draft release notes and verify that exact attempt.
   Then copy the five expected files into the helper's fixed root-only inbox.
   The helper accepts only the canonical tag, validates the protected copy, and
   seals it in the root-owned release cache before `apt` sees it:

   ```bash
   TAG=vX.Y.Z-wdg.N
   RUN_ID=123456789
   ATTEMPT=1
   ARTIFACT="meshtasticd-wdg-arm64-${RUN_ID}-${ATTEMPT}"
   CANDIDATE="$HOME/meshtastic-candidate-$TAG"
   WDG_CHECKOUT="$HOME/python/WatchDogsGo"
   INBOX="/var/cache/watchdogs/meshtasticd-wdg/first-install-inbox/$TAG"
   DEB_VERSION="${TAG#v}"
   DEB_VERSION="${DEB_VERSION/-wdg./+wdg}"
   DEB_NAME="meshtasticd-wdg_${DEB_VERSION}_arm64.deb"

   gh run view "$RUN_ID" --attempt "$ATTEMPT" \
     --repo Smethan/meshtastic-firmware --exit-status \
     --json attempt,conclusion,event,headBranch,headSha,status
   mkdir -m 0700 "$CANDIDATE"
   gh run download "$RUN_ID" --repo Smethan/meshtastic-firmware \
     --name "$ARTIFACT" --dir "$CANDIDATE"
   cd "$WDG_CHECKOUT"
   sudo bash scripts/setup_meshtastic.sh \
     --install-support "$USER" "$(id -u)"
   sudo install -d -o root -g root -m 0700 "$INBOX"
   for NAME in compatibility.json SHA256SUMS SOURCE.txt copyright "$DEB_NAME"; do
     sudo install -o root -g root -m 0600 "$CANDIDATE/$NAME" "$INBOX/$NAME"
   done
   PREPARED=$(sudo /usr/local/libexec/watchdogs-meshtastic \
     prepare-first-tag "$TAG")
   PACKAGE=$(printf '%s\n' "$PREPARED" | python3 -c \
     'import json,sys; print(json.load(sys.stdin)["package_path"])')
   sudo apt-get install "$PACKAGE"
   ```

   Confirm that `event` is `push`, `headBranch` is exactly `$TAG`, `status` is
   `completed`, `conclusion` is `success`, and `attempt` matches `$ATTEMPT`.
   `prepare-first-tag` never accepts a path. It reads only the fixed root-owned
   inbox for that tag, rejects extra/missing files and unsafe ownership or
   modes, repeats the release manifest, checksums, package, dependency,
   maintainer-script, privileged-policy, ARM64, and host checks, then copies the
   validated bytes into a private immutable-by-unprivileged-users cache. The
   package path returned to `apt` therefore cannot be replaced by the login user
   between validation and installation.
2. Verify the phone, radio, identity, and channels on the uConsole.
3. Publish that already-tested draft through the explicit `publish_draft`
   workflow operation, passing the same run ID and producer attempt:

   ```bash
   gh workflow run wdg-native.yml --repo Smethan/meshtastic-firmware \
     -f operation=publish_draft -f package_tag="$TAG" \
     -f tested_run_id="$RUN_ID" -f tested_artifact_attempt="$ATTEMPT"
   ```

   Publication independently requires a successful tag-push workflow, fetches
   the exact non-expired artifact named by those values, rejects any extra or
   non-regular entries, and byte-compares all five draft assets before reading
   their checksums. If the Actions artifact has expired, create and test a new
   candidate tag; the draft alone is not sufficient provenance.
4. Stop both `meshtasticd.service` and `meshtasticd-wdg.service`.
5. Adopt the exact installed tag:

   ```bash
   sudo /usr/local/libexec/watchdogs-meshtastic adopt-installed vX.Y.Z-wdg.N
   ```

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
phone protocol. Open an explicit 120-second window with **Open pairing**. The
six-digit BlueZ passkey appears in WDG status and the daemon journal. One phone
bond is retained. Disconnect and choose **Forget paired phone** before pairing
a replacement.

`NO_PIN` follows the upstream Just Works mode. `RANDOM_PIN` uses BlueZ's
generated six-digit passkey. BlueZ Agent1 does not provide a supported way for
a DisplayOnly Linux LE peripheral to force the upstream fixed PIN, so a
configured `FIXED_PIN` fails closed with `fixed_pin_unsupported`; the daemon
does not advertise or silently substitute another pairing mode. Select
`RANDOM_PIN` or explicitly opt into `NO_PIN` before enabling phone BLE.

Store adapter choices by controller MAC, not by `hci0`/`hci1`; Linux controller
numbers can change after reboot or USB changes. **Phone BLE adapter** selects
the fork's controller. **Host scan adapter** selects Bleak's Host BLE scanner.

Only one unrestricted PhoneAPI client can own the firmware's global phone
queues. The owner is shown as `none`, `bluetooth`, `bluetooth_pending`, or
`tcp`. The BLE and TCP transports coordinate that lease; the restricted WDG
socket never consumes it. A phone may therefore coexist with WDG, but an
arbitrary simultaneous TCP/serial PhoneAPI client is unsupported.

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

See [Meshtastic WDG local API](MESHTASTIC_WDG_API.md) for the JSON protocol and
its security boundary.
