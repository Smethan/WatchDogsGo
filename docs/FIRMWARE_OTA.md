# Updating ESP32 firmware over Wi-Fi

**WDG 0.9.22+ → SYSTEM → Wi-Fi OTA** uses the OTA updater already in
**Smethan/projectZero 1.7.2+**. The ESP32 downloads the selected firmware from
GitHub through its own Wi-Fi connection. USB carries only small setup commands,
progress and version checks. It does not use esptool, erase the whole chip, or
transfer the firmware image through USB. No SD card or paid API is required.

1. Update WDG and restart it. Boot the ESP32 normally: **release BOOT and tap
   RESET** if it was left in download/bootloader mode. WDG needs a responsive
   connection to the running firmware, not the ROM downloader.
2. Open **SYSTEM → Wi-Fi OTA**. Enter the Wi-Fi network the ESP32 should join and
   its password. A phone hotspot with internet access also works. A blank password
   means an open network; blank SSID and password use an already-connected ESP32
   network. WDG does not copy the uConsole's saved network credentials.
3. Use **Tab / Up / Down** to select a field. On **VERSION**, use **Left / Right**
   to choose a release. The newest compatible stable release is selected initially;
   older compatible tags remain selectable. No fallback to another version occurs.
4. Select **START WI-FI UPDATE** and press **Enter**. WDG stops scans, verifies the
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

Offline tests cover credential quoting/nonlogging, rejected credentials, missing
capabilities/partitions, old firmware, successful updates, explicit OTA failures,
missing final messages, wrong versions/slots, pending validation, USB errors,
serial ownership, and refusal to reconnect to another device. The version list
was checked against published 1.7.2-1.7.5 assets; setup/progress/result screens were
rendered. The affected uConsole still needs an end-to-end hardware OTA test.
