# MeshCore Companion BLE for MeshMapper

WatchDogsGo can present the uConsole AIO v2 SX1262 as a standard MeshCore
companion radio to MeshMapper. The phone talks to BlueZ over the MeshCore
Nordic-UART GATT service; WDG continues to own the SX1262 directly over SPI.
Bluetooth is a control/data bridge, not another radio backend and not a second
radio owner.

This feature is opt-in and disabled on upgrades.

## Configure WDG

1. Start WDG and power on LoRa under **SYSTEM**.
2. Open **SNIFF > Wardrive Settings > LoRa settings**.
3. Select **MeshCore** as the LoRa protocol.
4. Open **MeshCore radio and companion BLE**.
5. Confirm the MeshCore regional preset used by the nodes you intend to map.
6. Select **Companion BLE adapter**. `auto` uses the first available BlueZ
   adapter; a stable controller MAC is safer on systems with USB Bluetooth.
   A dedicated adapter is strongly recommended because MeshMapper must be its
   only connected device when it subscribes to notifications.
7. Set **MeshMapper companion BLE** to `ON`.
8. Open **ADDONS > Mesh Messenger**, or enable **Automatic LoRa collector** and
   start All Wardrive. The settings page should progress from `STARTING` to
   `READY` and show its resolved `hci` controller.

The BLE peripheral exists only while WDG's MeshCore backend is running. It is
removed before the direct radio releases ownership, during protocol handoff,
when LoRa powers off, and at application shutdown.

BlueZ completes peripheral setup by calling back into WDG's exported D-Bus
objects. Current `main` prepares the GLib loop, registers the GATT application
asynchronously, and then runs the loop so it can service BlueZ's callbacks. It
registers the advertisement only after BlueZ accepts the application. Both
registrations use explicit `ObjectPath` values and share a bounded ten-second
startup deadline. Shutdown unregisters only the application and advertisement
objects that BlueZ confirmed it accepted.

## Connect MeshMapper

1. Grant MeshMapper the Bluetooth and location permissions required by the
   phone platform.
2. In WDG, choose **Open authenticated pairing**. This opens one bounded
   120-second window and temporarily makes the selected controller pairable.
3. In MeshMapper, choose its Bluetooth connection option, scan for radios, and
   select `MeshCore-<WDG node name>`. Do not pre-pair it in the phone's generic
   Bluetooth settings.
4. BlueZ generates a six-digit passkey and WDG shows it as **Pairing PIN**.
   Enter that passkey on the phone. The first device to claim the window is the
   only device WDG will accept; a successful exchange is retained as one
   paired, bonded, and trusted phone.
5. Let MeshMapper finish device query, self-identity, clock sync, API
   authentication, and channel setup. On first connection it can create the
   standard `#wardriving` channel in the first free slot.
6. Choose Active, Passive, or Hybrid mapping in MeshMapper. Active/Hybrid mode
   queues pings and discovery packets through WDG's single radio worker;
   received packets and discovery responses are notified back to the app.

Later connections reuse the retained BlueZ bond. To replace the phone,
disconnect it, choose **Forget paired phone** in WDG, and wait for the success
message. WDG asks BlueZ to remove only the exact retained device from the
selected adapter and clears the saved address/name only after that succeeds.
It never bulk-removes unrelated bonds. Open a new 120-second window after the
old bond is gone.

WDG supports the companion operations MeshMapper uses for normal mapping:

- device and self information;
- time get/set and advertised-name changes;
- fixed channel-slot enumeration, set/delete, and channel messages;
- raw receive (`0x88`) and control/discovery receive (`0x8E`);
- self advert and signed contact export;
- authenticated, bounded `CMD_SIGN` start/data/finish (`33`-`35`);
- radio/core/packet statistics and battery response;
- discovery/control transmit and path-hash-mode state.

The standard UUIDs are:

| Role | UUID |
|---|---|
| MeshCore service | `6E400001-B5A3-F393-E0A9-E50E24DCCA9E` |
| App to WDG (RX) | `6E400002-B5A3-F393-E0A9-E50E24DCCA9E` |
| WDG to app (TX/notify) | `6E400003-B5A3-F393-E0A9-E50E24DCCA9E` |

## Bluetooth controller coexistence

BlueZ adapters vary in how reliably they can run a connectable GATT peripheral
and an active BLE scan at the same time. WDG therefore uses a conservative
rule:

- If either companion or host-scan adapter is `auto`, host scanning pauses
  while the companion peripheral is running.
- If both settings contain the same controller MAC, host scanning pauses.
- If two different stable controller MACs are selected, WDG allows the
  companion peripheral and All Wardrive host scan to run concurrently.
- A paused host scan is automatically eligible for retry after the companion
  peripheral stops. Wi-Fi and the other All Wardrive collectors continue.

Meshtastic phone BLE does not run at the same time: selecting direct MeshCore
stops the Meshtastic daemon according to the existing exact service-state
handoff rules.

WDG also serializes temporary BlueZ pairing-agent ownership. A MeshMapper
window, PipBoy-watch pairing, and the Meshtastic daemon's pairing agent cannot
take the default-agent role at the same time. Starting a window obtains a
bounded shared lease; failure to restore the prior agent state becomes a
visible safety barrier instead of allowing another Bluetooth or LoRa handoff.

## Security and current limitations

The peripheral is disabled by default. Its RX characteristic uses BlueZ's
`write-without-response` plus `encrypt-authenticated-write` flags; TX uses
`encrypt-authenticated-read` and `encrypt-authenticated-notify`. WDG admits
GATT operations only from the retained phone after BlueZ reports it as paired,
bonded, and trusted. Numeric-comparison and Just Works requests are rejected:
the supported first-pair path is the BlueZ-generated six-digit passkey shown
by WDG and entered on the phone.

BlueZ does not pass a device identity to `StartNotify`. WDG therefore permits
notification subscription only when the retained authenticated phone is the
sole connected `Device1` on the selected companion adapter. A second connected
device, even an unrelated headset or watch, blocks notifications. Prefer a
dedicated MeshMapper adapter; otherwise disconnect every other device from that
controller before connecting the app.

The Ed25519 private key is never returned. Authenticated `CMD_SIGN` transactions
are limited to 8 KiB, expire after 30 seconds, and are erased on completion,
overflow, expiry, disconnect, or shutdown. Command `33` starts the transaction,
`34` appends bounded data, and `35` returns the 64-byte Ed25519 signature. The
same commands are rejected when the caller has not passed the
authenticated-device checks.

Other limits:

- Repeater-admin commands, trace mode, firmware update, key import/export,
  contact-database administration, arbitrary raw transmit, radio retuning, and
  transmit-power changes are rejected.
- WDG currently supports the global/unscoped MeshCore flood domain. A non-empty
  MeshMapper flood-scope key is rejected instead of pretending it was applied.
- The BLE name is fixed for the lifetime of one peripheral session. A name
  changed through MeshMapper is persisted and used on the next MeshCore radio
  start; reconnect to see the new advertisement name.
- BLE frames and radio queues are bounded. Passive receive notifications may
  be dropped under pressure so command replies and discovery state are kept.
- Digital tests cover the protocol and lifecycle, but Android/iOS connection,
  BlueZ controller behavior, and a sustained mapping run still require target
  uConsole hardware validation.

MeshCore config and the Ed25519 key live at
`~/.watchdogs_meshcore.json` and `~/.watchdogs_meshcore_key`. WDG writes them as
mode `0600`; when launched through `sudo`, it assigns them to the invoking
desktop user (normally `pi`), not `root`.

Authenticated BLE protects the phone-to-WDG link; it does not encrypt local
files. MeshCore messages that WDG decodes are still written as plaintext to the
normal session `meshcore_messages.log`, and diagnostic output (including the
temporary passkey while its window is open) is written to
`~/.watchdogs/last_run.log`. Treat loot and diagnostic logs as sensitive at
rest. The six-digit passkey cannot reopen a closed window or replace an
existing retained bond.

## Troubleshooting

Check adapter inventory and capabilities:

```bash
bluetoothctl list
bluetoothctl show
```

If WDG reports **No BlueZ adapter found** but `bluetoothctl list` shows an
`hciX` controller, update to WDG 0.9.45 or newer. Some uConsole UART Bluetooth
controllers omit `/sys/class/bluetooth/hciX/address` even though BlueZ exposes
the controller normally. Current WDG releases supplement sysfs discovery with
BlueZ's `org.bluez.Adapter1.Address`; manually creating a sysfs file or changing
its ownership is not required.

WDG 0.9.45 could fail during peripheral startup while `bluetoothd` logged
`client_ready_cb() No object received`, followed by WDG reporting that
peripheral cleanup could not be verified. That release made a synchronous
`RegisterApplication` call before the GLib dispatcher could answer BlueZ's
object-manager callback. Current `main` uses the asynchronous GATT-then-
advertisement sequence described above, fails after a bounded timeout instead
of hanging, and does not attempt to unregister an object BlueZ never accepted.
The patched path has automated coverage but has not yet been validated with a
physical uConsole and MeshMapper session.

An earlier `main` build could finish bonding but then report `Pairing
verification failed: device is not the retained MeshMapper phone`. BlueZ
replaces a private LE connection address with the device's public identity
address after pairing; current `main` accepts that transition only for the
exact passkey-authenticated `Device1` object claimed in the open window. If the
failed build already created a phone bond without saving it in WDG, remove only
that phone once in the system Bluetooth settings (or with `bluetoothctl remove
<phone-address>`), then open a fresh WDG pairing window. Do not bulk-remove
other bonds.

If the WDG settings page reports that no GATT server or advertising manager is
available, confirm BlueZ is running and try the other controller. If MeshMapper
connects but cannot complete setup, disconnect any other device on the
companion controller, inspect `~/.watchdogs/last_run.log` for `[MC-BLE]`, and
verify the selected regional radio parameters match the local MeshCore network.
If WDG already shows a retained phone, reconnect that device or use **Forget
paired phone** before opening a window for a replacement.

Protocol references:

- [MeshCore companion protocol](https://github.com/meshcore-dev/MeshCore/blob/main/docs/companion_protocol.md)
- [MeshMapper connection guide](https://wiki.meshmapper.net/app_connection_guide/)
- [MeshMapper getting started](https://wiki.meshmapper.net/app_getting_started/)
