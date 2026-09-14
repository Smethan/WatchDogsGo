# Stable SIM7600 cell collection

This note records the source-level diagnosis and the design used by WDG 0.9.29.
It separates what is established from what still needs a hardware soak.

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

## Implemented access path

WDG now follows the QMI control plane already in use:

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

The Debian package containing `qmicli` is `libqmi-utils`; WDG's setup script
already installs it on Raspberry Pi/Clockwork systems. The host validation used
qmicli 1.38.0 and confirmed the NAS option is present. WDG uses only an argument
array, never a shell command.

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

The host test suite covers the official qmicli LTE text format, serving-cell
selection among multiple physical cells, SIM7600 QMI-port selection, exact
proxy command arguments, permanent and transient error policy, one-time
ModemManager fallback, UI retry suppression, WiGLE storage, and the rest of
WDG. It also scans the shipped code to ensure no cellular provider references
`ttyUSB`, `AT+CPSI`, or pySerial.

No patched WDG code or AT command was run on the uConsole during this repair.
A post-release hardware check should first confirm that cellular data remains
connected and the console remains stable during a long All Wardrive session.
If a whole-console crash remains, collect `journalctl -b -1 -k`,
`journalctl -b -1 -u ModemManager`, and power/undervoltage evidence from the
previous boot before changing the modem path again.

## Sources

[^1]: ModemManager source comparison: [1.20.4 QMI backend](https://gitlab.freedesktop.org/mobile-broadband/ModemManager/-/blob/1.20.4/src/mm-broadband-modem-qmi.c), [1.20.4 MBIM backend](https://gitlab.freedesktop.org/mobile-broadband/ModemManager/-/blob/1.20.4/src/mm-broadband-modem-mbim.c), and [1.22.0 QMI backend](https://gitlab.freedesktop.org/mobile-broadband/ModemManager/-/blob/1.22.0/src/mm-broadband-modem-qmi.c).
[^2]: ModemManager, [Port and device detection](https://modemmanager.org/docs/modemmanager/port-and-device-detection/).
[^3]: pySerial, [API documentation](https://pyserial.readthedocs.io/en/stable/pyserial_api.html).
[^4]: ClockworkPi, [uConsole 4G extension firmware and AT examples](https://github.com/clockworkpi/uConsole/wiki/How-to-upgrade-4G-extension-firmware), and the [SIM7500/SIM7600 AT manual in the uConsole repository](https://github.com/clockworkpi/uConsole/blob/master/SIM7500_SIM7600%20Series_AT%20Command%20Manual_V3.00.pdf).
[^5]: ModemManager, [qmi-proxy debugging notes](https://modemmanager.org/docs/modemmanager/debugging/), and libqmi [qmicli NAS implementation](https://gitlab.freedesktop.org/mobile-broadband/libqmi/-/blob/1.32.2/src/qmicli/qmicli-nas.c).
