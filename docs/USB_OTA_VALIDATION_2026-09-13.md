# USB OTA hardware validation — 2026-09-13

Tested XIAO ESP32-C5 native USB on the same uConsole and cable used for the
previous slow application-mode updater. WDG 0.9.24, firmware 1.7.8.

| Run | Total time | Image transfer | Result |
| --- | ---: | ---: | --- |
| Install new receiver through existing 1.7.7 path | 445.65 s | Original 256-byte format | Valid 1.7.8 boot |
| Normal published-release fast update | 53.33 s | 37.56 s | Valid 1.7.8 boot, no pending update |
| Published-release fast update with serial close / lost ACK | 60.23 s | 39.42 s | Resumed, valid 1.7.8 boot, no pending update |

Each application image was 2,253,296 bytes. The bootstrap used a locally built,
checksummed bundle from firmware commit `dcbef0a`, validated by the existing
bundle validator, through the production USB OTA runner with an offline bundle
fixture. Both fast runs used the **unmodified release downloader** and the
published GitHub release, including manifest/archive/image checks. Thus the
53.33-second total includes downloading and verifying the release, slot
preparation, image transfer, final validation, reboot and boot confirmation;
the bootstrap timing uses an already staged local bundle.

Normal fast transfer sent 551 blocks, zero legacy chunks and needed one begin.
The resume run deliberately closed serial at offset 131072 and discarded the
ACK. It needed two begins but still only 551 blocks: progress was queried and
continued without resending the completed prefix. Neither run needed compatibility
fallback. Tests also simulate repeated large-block failures and verify fallback
to smaller chunks without losing progress.

Measured overall time improved about **8.4x** versus the old receiver install.
The fast data phase transferred about 60 kB/s. This is a measurement of app-mode
USB OTA; no new ROM/esptool timing or physical power-cut test was performed.
Both versions keep CRC checks, durable sector checkpoints, whole-image SHA256,
ESP image validation and valid-slot/version confirmation after reboot.

Final device state: firmware 1.7.8, boot=running `ota_1`, valid state 2,
USB OTA inactive with no pending hash/bytes. WDG 0.9.24 was installed on the
uConsole, its settings hash was unchanged, and WDG was left closed.

Validation: 192 WDG tests; firmware native tests including all 4096 final-sector
lengths; actual pinned SDK quiet-input test at full block-command size; guarded
RX patch tests; both board builds and local stack checks. Public standard GitHub
runners published firmware v1.7.8 and WDG v0.9.24. Capture features and automatic
hotspot configuration were not changed by these releases.
