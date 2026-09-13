# Flashing or rolling back ESP32-C5 firmware

Update to **WDG 0.9.20+**, restart, and open **SYSTEM → Flash ESP32**.

1. Use **Up/Down** to choose the correct board. Use **XIAO ESP32-C5** for the Seeed
   XIAO; the WROOM image is a different board build.
2. Use **V/B** to move forward/backward through firmware versions. **Latest
   stable** remains available, or select an explicit tag such as **v1.7.3**.
   A short background request loads the published versions. If it fails, V/B
   retries the list request; an explicit unavailable version fails rather than
   switching to latest.
3. Use **Left/Right** to choose **Automatic** or **Manual BOOT**.
4. Press **Enter** to download, verify and flash the selected release. Keep USB
   connected while flashing. The first attempt may also install flashing tools.
5. After success, release BOOT and press **Escape** to resume WDG's connection.

For **Manual BOOT**, enter download mode *while this window is open*: hold BOOT,
tap RESET (or unplug/reconnect USB while holding BOOT), then release BOOT before
pressing Enter. WDG has already closed its serial connection and leaves USB
power on. It does not poll, send keepalives, or reconnect during this preparation.

Escape closes an idle/failed wizard and resumes normal serial use. Escape during
an active flash only hides the window; flashing continues and retains exclusive
use of WDG's serial connection. Opening the wizard pauses the previous ESP32
operation; it does not automatically resume that scan afterward.

## Why this differs from the old built-in flasher

The old uConsole/XIAO path switched the USB rail off and on before calling
esptool. That could discard manual bootloader entry. The standalone
`projectZero/ESP32C5/binaries-esp32c5/flash_board.py` does not power-cycle USB.
The old built-in flasher also used a forced USB reset, a hard reset afterward,
and allowed esptool versions as old as 5.0. The working desktop record used 5.4.

| Setting | Automatic | Manual BOOT |
| --- | --- | --- |
| USB power cycle | None | None |
| Before flashing | `default-reset` (esptool detects native USB-JTAG) | `no-reset` |
| Baud argument | 460800, like the script | 115200, without a high baud change |
| After flashing | `watchdog-reset` | `watchdog-reset` |
| Flash mode/frequency | DIO / 80 MHz | DIO / 80 MHz |
| Flash size | Detected by esptool | Detected by esptool |

Espressif documents that a watchdog reset can leave native USB-JTAG download
mode by re-sampling the boot straps. ESP32-C5 implements this reset; do not
generalize it to every ESP32 chip. See [esptool reset options](https://docs.espressif.com/projects/esptool/en/latest/esp32c5/esptool/advanced-options.html)
and [troubleshooting](https://docs.espressif.com/projects/esptool/en/latest/esp32c5/troubleshooting.html).
USB-JTAG does not use a physical UART bit rate; the lower baud argument alone
is not a guarantee of slower USB transfers or a fix for a bad cable.

The verified bundle's bootloader, partition table, initial OTA data and app
offsets remain authoritative. This update does not add a full-chip erase or
copy the app into an additional OTA slot merely to imitate the standalone
script's optional behavior. Selecting a different version deliberately replaces
those firmware regions with the selected release, including its initial OTA data.

## Tools, logs and download checks

The flasher uses esptool **5.4 or newer, below 6**. It first checks WDG's current
Python. If necessary, it creates `firmware_cache/esptool-venv`, installs the tool
there, and reuses that environment on later attempts. This needs internet and
Python venv/pip support on first use; failures are reported before invoking the
device flasher. It does not rerun setup, replace GUI dependencies, or install
system-wide packages.

Every attempt saves `firmware_cache/flash-<timestamp>.log` beneath the WDG
checkout. It includes the requested/actual release, board, esptool version and
Python path, selected USB device, command and complete esptool output. The
console prints the exact log path. If a transfer still fails, use that log to
distinguish connection, stub upload, writing, verification and final reset errors.
Failed writes are not retried automatically with other firmware or ports.

The version selector lists up to 100 published release records from
**Smethan/projectZero**, excluding drafts, prereleases, non-version tags and
releases without the expected checksummed board bundles. Before writing, every
selected version still passes the existing archive hash, manifest, board,
version, filename, offset and individual file hash checks. No upstream or stale
cache fallback is used. A serial number identifies the chosen USB device across
tty renumbering; physical USB location is used when no serial number is exposed.
Missing or ambiguous devices stop the attempt.

## Capture regression and rollback

The user reported HS Sniff/HS Capture problems with the current firmware. The
flasher update does not establish the cause of that regression. **v1.7.3** is the
previous published release and can now be chosen directly. It supports passive
HS Sniff but predates the 1.7.4 active-capture progress stream, so active capture
shows only available legacy M-number sightings and **PMKID: N/A**.

Validation is offline: simulated port renumbering, exclusive wizard ownership,
failure cleanup, exact release selection, output logging, command construction,
and dependency reuse. Both 1.7.3 board bundles were downloaded and verified
through WDG, and private esptool installation/reuse and screen rendering were
checked. No connected hardware was flashed during this change.
