# Stable SIM7600 location ownership

WDG 0.9.31 restores serving-cell tracking with a different ownership model.
ModemManager is the only process that controls the internal SIM7600. WDG reads
its cached `Modem.Location` state over D-Bus for both NMEA and registered-cell
identity; the safe default never opens a modem device node or launches qmicli.

## Optional hardware and AIOv2 swaps

WDG 0.9.39 adds **LTE modem integration** under **SNIFF → Wardrive Settings**.
Turn it OFF before removing the SIM7600. The value is persisted and loaded
before `GpsManager` starts, so an LTE-disabled launch does not acquire the
ModemManager broker or query its managed-port inventory.

LTE OFF also gates serving-cell and experimental-neighbor collection. It does
not stop or alter ESP Wi-Fi/BLE, host BLE, All Wardrive, or ordinary loot. The
separate cell settings are retained and resume when LTE integration is turned
back on.

GPS remains independent. With LTE OFF, WDG checks the documented AIOv2 UART
(`/dev/ttyS0` on CM4 or `/dev/ttyAMA0` on CM5), then safe ACM devices. It does
not broadly probe other platform UARTs, which may carry Bluetooth, and it skips
automatic `ttyUSB` probing because a still-installed modem may expose AT ports
there even when integration is disabled. Operators using an external USB GPS
can select it explicitly with `WDG_GPS_DEVICE`. The AIOv2 GPS power rail must
also be enabled through **SYSTEM → GPS** or `aiov2_ctl GPS on`.

Changing the option at runtime first stops the cell owner, then releases
ModemManager GNSS and searches for an external receiver. Wi-Fi and Bluetooth
workers are not stopped. Enabling the option leaves a working external GPS in
place; if no GPS provider is active, it tries ModemManager GNSS. If the modem
is absent while integration is ON, broker failure is contained and serial GPS
fallback still runs.

## Required one-time migration

Older uConsole setups may run `/usr/local/bin/setup-sim-gps` at boot. That
script writes `AT+CGPS` directly to `/dev/ttyUSB3` as ModemManager is claiming
the same composite USB modem. WDG detects this service and leaves its internal
GNSS and cell features disabled until ownership is migrated.

Check and apply the migration from the repository root:

```sh
./scripts/migrate_uconsole_sim_service.sh --status
sudo ./scripts/migrate_uconsole_sim_service.sh --apply
```

The helper backs up the existing unit and GNSS script under
`/var/backups/watchdogs/uconsole-sim-<UTC timestamp>.<suffix>/`, validates and
installs a power-only oneshot service, and leaves the running service/modem untouched. A
manual reboot is required afterward. To restore a saved definition:

```sh
sudo ./scripts/migrate_uconsole_sim_service.sh --restore \
  /var/backups/watchdogs/uconsole-sim-<UTC timestamp>.<suffix>
```

## Safe serving-cell path

The shared broker enables only ModemManager location sources that WDG needs and
restores the bits it added at shutdown. One worker owns the SystemBus
connection, polls cached location once per second with bounded timeouts, and
publishes immutable snapshots to the UI. D-Bus failures use exponential
backoff and cannot stop the ESP32 wardrive session.

Cell persistence begins only after the ESP32 acknowledges All Wardrive. A
registered cell is written with the GPS fix at each completed ten-second
firmware batch. MCC/MNC stays a string, LAC/TAC/cell ID is converted from the
ModemManager hexadecimal representation, and missing dBm uses WiGLE's `-113`
unknown-strength value. Both ESP BLE and host BLE modes use this same path.

On the tested SIM7600G-H, ModemManager returned
`311,480,0,2038216,8308`, which becomes the valid LTE identity
`311480_33544_33784342`. The raw qmicli output reported a conflicting PLMN, so
WDG always treats ModemManager's registered operator and global identity as
canonical.

## Experimental neighbor candidates

Wardrive Settings includes **Experimental QMI neighbors**, disabled by default.
When enabled, WDG performs at most one qmi-proxy NAS location request every 60
seconds after a completed ESP batch. The request has an eight-second deadline,
cannot overlap, and is disabled for the rest of the session after its first
failure. Serving-cell collection continues.

SIM7600 LTE neighbor entries provide PCI, channel, and radio measurements but
no globally unique cell ID. WDG records them only in
`cell_neighbor_candidates.jsonl` and shows yellow dots where they were heard.
They are marked provisional and excluded from `wardriving.csv`, WiGLE uploads,
valid-cell totals, and history. A matching QMI TAC/cell ID may enrich the
current serving cell's signal, but QMI cannot replace its identity.

## Crash evidence

`cell_health.jsonl` stores state changes, modem generation, query duration,
circuit-breaker reason, and clean shutdown without storing subscriber or IP
identifiers. `active_cell_session.json` exists until all cell workers stop; a
remaining file on next launch identifies an unclean session. If a full console
power loss occurs, collect that file, `journalctl -b -1`, pstore, and the health
log before repeating a test.

## Historical 0.9.29 design

The remainder records the source-level diagnosis and QMI-first design briefly
used by WDG 0.9.29. It is retained to explain why 0.9.31 does not return to
persistent AT access or automatic qmicli fallback.

## Finding

The affected uConsole exposes its SIM7600 as a QMI modem. ModemManager 1.20.4
publishes the generic D-Bus `GetCellInfo` method, but its QMI backend does not
implement that operation. In the upstream source, 1.20.4 wires cell information
only in the MBIM backend; QMI `GetCellInfo` was added in ModemManager
1.22.0.[^1] The observed `Core.Unsupported` response is therefore expected for
that software/backend combination.

WDG 0.9.28 worked around the missing backend by opening the modem's secondary
AT TTY and keeping it open while issuing `AT+CPSI?`. That path is correlated
with the reported full-console crashes, but there is no recovered kernel log
proving one exact electrical or kernel cause. There are two concrete reasons
not to keep it:

1. ModemManager treats all USB interfaces as ports of one managed device and
   uses port-role rules to control access. Its documentation warns that unsafe
   modem features can cause irrecoverable modem firmware crashes.[^2]
2. pySerial opens a supplied port immediately and documents that opening can
   activate or glitch RTS/DTR.[^3] The SIM7600 AT manual describes DTR behavior
   that can change a data call or command mode. A persistent second controller
   is therefore the wrong ownership model even though a one-shot command may
   appear to work.

ClockworkPi's own SIM7600 instructions use short-lived `socat` commands on an
AT port for maintenance and diagnostics.[^4] They do not establish persistent
AT polling alongside a ModemManager-managed QMI bearer as a supported
application interface.

## 0.9.29 access path (removed)

WDG 0.9.29 followed the QMI control plane already in use:

1. Ask ModemManager for `GetCellInfo`. This preserves native serving and
   neighbor support on backends that implement it.
2. If that operation is unavailable, find the SIMCom QMI port from
   ModemManager's own `Modem.Ports` metadata. No TTY probing or udev serial
   fallback is performed.
3. Run the read-only NAS `Get Cell Location Info` request with
   `qmicli --device-open-proxy`. ModemManager documents qmi-proxy as the
   process that synchronizes access to QMI control ports.[^5] ModemManager's
   QMI backend uses the same NAS request added in 1.22.0.[^1]
4. Apply a 12-second process deadline, keep only one request in flight, and
   sample every 30 seconds.
5. Disable cellular collection for the current wardrive session when the
   required QMI command or capability is permanently unavailable. A transient
   failure retries after 60 seconds, then doubles to a five-minute cap. Wi-Fi
   and BLE wardriving continue either way.

The Debian package containing `qmicli` is `libqmi-utils`; WDG 0.9.29 briefly
added it to the Raspberry Pi/Clockwork setup package list. WDG 0.9.30 removed
that dependency. The 0.9.29 host validation used qmicli 1.38.0 and confirmed
the NAS option was present.

## What can be recorded

The QMI response includes the LTE serving cell's PLMN, TAC, global cell ID,
EARFCN, physical cell ID and signal measurements. Those fields form a valid
WiGLE cell identity and are saved with the uConsole's GPS fix.

QMI also reports LTE and UMTS neighbor radio measurements, but those entries do
not include a globally unique cell ID. ModemManager 1.22 likewise marks the
matching LTE physical cell as serving and attaches the global cell ID only to
that entry.[^1] WDG does not invent IDs or write ambiguous neighbors into a
WiGLE file. GERAN neighbor records are retained when QMI supplies their own
PLMN, LAC and cell ID. A newer ModemManager/backend may return more fully
identified neighbors through D-Bus.

This differs from Android's WiGLe path: Android's telephony API may expose
fully identified neighboring `CellInfo` objects that the modem's Linux QMI
response does not provide. The uConsole can match WiGLe's file shape and
serving-cell behavior, but cannot reconstruct identity fields absent from the
modem response.

## Validation and remaining evidence

The 0.9.29 host test suite covered the official qmicli LTE text format,
serving-cell selection among multiple physical cells, SIM7600 QMI-port
selection, exact proxy command arguments, error policy, ModemManager fallback,
UI retry suppression, WiGLE storage, and the rest of WDG. Hardware use then
showed that the whole-uConsole crash remained.

WDG 0.9.30 removed the provider module and every All Wardrive lifecycle hook.
Before the 0.9.31 redesign, staged read-only testing confirmed that cached
ModemManager locations, standalone qmi-proxy requests, GNSS, and the ESP32
batch stream could coexist without a reproduced crash. That result does not
prove the old path safe; it supports eliminating the independently discovered
serial ownership races and adding durable breadcrumbs before field rollout.

## Sources

[^1]: ModemManager source comparison: [1.20.4 QMI backend](https://gitlab.freedesktop.org/mobile-broadband/ModemManager/-/blob/1.20.4/src/mm-broadband-modem-qmi.c), [1.20.4 MBIM backend](https://gitlab.freedesktop.org/mobile-broadband/ModemManager/-/blob/1.20.4/src/mm-broadband-modem-mbim.c), and [1.22.0 QMI backend](https://gitlab.freedesktop.org/mobile-broadband/ModemManager/-/blob/1.22.0/src/mm-broadband-modem-qmi.c).
[^2]: ModemManager, [Port and device detection](https://modemmanager.org/docs/modemmanager/port-and-device-detection/).
[^3]: pySerial, [API documentation](https://pyserial.readthedocs.io/en/stable/pyserial_api.html).
[^4]: ClockworkPi, [uConsole 4G extension firmware and AT examples](https://github.com/clockworkpi/uConsole/wiki/How-to-upgrade-4G-extension-firmware), and the [SIM7500/SIM7600 AT manual in the uConsole repository](https://github.com/clockworkpi/uConsole/blob/master/SIM7500_SIM7600%20Series_AT%20Command%20Manual_V3.00.pdf).
[^5]: ModemManager, [qmi-proxy debugging notes](https://modemmanager.org/docs/modemmanager/debugging/), and libqmi [qmicli NAS implementation](https://gitlab.freedesktop.org/mobile-broadband/libqmi/-/blob/1.32.2/src/qmicli/qmicli-nas.c).
