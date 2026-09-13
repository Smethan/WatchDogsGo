# ESP32 OTA update methods

**WDG 0.9.23+ → SYSTEM → OTA Update → Wi-Fi (enter network)** uses the OTA updater already in
**Smethan/projectZero 1.7.2+**. The ESP32 downloads the selected firmware from
GitHub through its own Wi-Fi connection. USB carries only small setup commands,
progress and version checks. It does not use esptool, erase the whole chip, or
transfer the firmware image through USB. No SD card or paid API is required.

1. Update WDG and restart it. Boot the ESP32 normally: **release BOOT and tap
   RESET** if it was left in download/bootloader mode. WDG needs a responsive
   connection to the running firmware, not the ROM downloader.
2. Open **SYSTEM → OTA Update** and select the Wi-Fi method. Enter the Wi-Fi network the ESP32 should join and
   its password. A phone hotspot with internet access also works. A blank password
   means an open network; blank SSID and password use an already-connected ESP32
   network. WDG does not copy the uConsole's saved network credentials.
3. Use **Tab / Up / Down** to select a field. On **VERSION**, use **Left / Right**
   to choose a release. The newest compatible stable release is selected initially;
   older compatible tags remain selectable. No fallback to another version occurs.
4. Select **START UPDATE** and press **Enter**. WDG stops scans, verifies the
   firmware and OTA layout, connects Wi-Fi, and requests the exact selected tag.
   An already-running selected version is reported without downloading or writing.
5. Leave power connected while progress and verification run. The screen remains
   open during the operation; Escape cannot interrupt it. WDG waits for the ESP32
   to reboot, then checks the version, running/boot slot and valid image state.
6. After the result, **Escape** resumes normal WDG serial use. Scans do not restart
   automatically. Passwords must be re-entered for a later attempt.

If the result is **unconfirmed**, the update might have completed, still be
running, or rolled back. It does not mean success or a proven failed write.
Keep power connected, allow the board to finish, and check its running version
before requesting another update. WDG does not retry an update automatically.
Wi-Fi errors before the update request are reported without requesting a write.

## Compatibility and limits

- Install the **full Smethan board bundle by USB once** if firmware is upstream,
  too old, or lacks both OTA slots. Version 1.7.1 has an OTA project-name validation
  mismatch; this UI requires 1.7.2+. Supported tagged app releases are filtered to
  that minimum, so an OTA downgrade retains a compatible updater.
- The existing firmware chooses XIAO versus WROOM from its compiled board profile.
  OTA updates the application in the inactive slot; it cannot change board type,
  bootloader or partition layout. Use the USB flasher for those changes.
- WDG validates the selected release's app asset URLs against its configured fork.
  The onboard updater uses its **compiled GitHub source**, HTTPS and ESP-IDF app
  validation. It does not expose its source via the serial preflight; use the
  actual Smethan firmware. WDG's ZIP/SHA256 manifest verification for USB flashing
  is not applied to the direct onboard OTA download.
- The current firmware accepts at most **31 UTF-8 bytes for an SSID** and **63
  bytes for a password** because its command handler reserves a trailing NUL.
  WDG rejects values that would be truncated. Spaces, quotes and backslashes are
  supported. Passwords use 8-63 bytes, or blank for open Wi-Fi. Captive portals and
  enterprise Wi-Fi authentication are not supported by this screen.
- WDG masks passwords, clears its input field on start/exit, and consumes command
  echoes privately without logging them to the terminal or loot. Its application
  holds credentials in memory while connecting. **The existing firmware can save
  a successful network password on its SD card.** This is firmware behavior and
  is disclosed on the screen.
- USB must remain connected and application-mode serial commands must work. This
  avoids the ROM/stub/bulk USB transfer path; it cannot repair a real power drop,
  a board stuck in BOOT mode, or a completely broken USB connection. Reconnects
  follow the original device's USB identity, never an arbitrary tty number.
- The firmware download can take up to five minutes before WDG moves to a final
  verification attempt. Reboot verification has a separate bounded wait. Losing
  USB before the last progress line does not itself prove failure.

## Validation

Tests cover credential quoting/nonlogging, capabilities and partition checks,
release validation, interruption recovery, resume offsets, checksum rejection,
wrong versions/slots, pending validation, serial ownership, and refusal to
reconnect to another device. Both updater menu screens were rendered.

On the user's uConsole/XIAO ESP32-C5, ordinary Wi-Fi OTA installed 1.7.6 and
confirmed its new valid boot slot. A 25-second passive transport test then
received **767 packet records longer than 256 bytes**, reconstructing **394
complete frames**, with **zero malformed records or incomplete frames** and
two reported drops. No EAPOL/PMKID exchange happened during that sample; it
verifies the repaired packet delivery path, not a live active-capture exchange.
Both active capture variants use the same repaired output helper.

The complete USB OTA hardware test then ran on firmware **1.7.7**. The host
closed its serial connection after the ESP32 wrote **65,536 bytes**, discarded
the acknowledgment, reconnected by the same USB identity, and resumed at the
board's saved offset. The **2,252,256-byte** image completed its whole-image
checksum and firmware validation, rebooted, and passed the expected-slot and
valid-state checks. Total elapsed time was **457 seconds** (7m37s), including
download, interruption recovery, hashing and reboot. This was a serial-close
interruption; reboot/power-loss checkpoint recovery is covered by simulated
host/firmware tests, not a physical power-cut test.

Automatic hotspot support was removed. The uConsole remains on its existing
network; neither updater method reconfigures its Wi-Fi or cellular connection.

## Resumable USB OTA

Select **METHOD → USB (resumable, no ESP32 Wi-Fi)**. Install firmware **1.7.7+**
once using Wi-Fi OTA to enable its USB receiver. WDG uses the uConsole's
internet connection (Wi-Fi, cellular, or another working host connection) to download the verified release bundle, asks the ESP32 for
its board profile, and sends only the matching application in small, acknowledged
blocks. Normal firmware runs the receiver; BOOT stays released. No SD is needed.

On a brief USB drop, WDG reconnects only to the same USB identity and resumes.
If it cannot reconnect after bounded attempts, close the result, reconnect the
board and select **the same firmware version** to resume. If the ESP32 lost
power, up to the last 4 KiB is resent from a durable checkpoint. An intentional
version change with another image pending requires **Ctrl+D** to enable discard
before START; this clears only the unfinished inactive-slot transfer.

Whole-image SHA256, project/image validation and post-reboot version/slot checks
must all pass before success is shown. A missing final acknowledgment is treated
as uncertain until reboot verification succeeds. USB OTA can also install an
older compatible app, but downgrading below 1.7.7 loses the supported USB OTA path;
use Wi-Fi OTA to upgrade again. Full USB flashing remains available for initial
installation, bootloader/partition changes, or recovery.
