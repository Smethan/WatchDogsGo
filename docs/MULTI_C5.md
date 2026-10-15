# Multi-device ESP32-C5 operation

WatchDogsGo 0.10.0 introduces a host runtime for multiple independently
identified projectZero devices. Firmware 1.8.0 advertises `c5_jobs_v1`; older
firmware remains on the existing single-device serial paths. The host discovers
supported serial endpoints without stopping devices. Use the old positional
serial-port argument to select the first device, or repeat `--esp-port PORT`
for explicit endpoints. Do not assume a disconnected job restarts: reconnecting
does not automatically resume or replay it.

## Devices and radio allocation

Each device has a stable USB identity, boot identity, board profile, supported
operations, channels, resource limits, and update methods. Settings under
**WARDRIVE → USB C5 devices and roles** can assign an alias and role. The
default XIAO profile provides 2.4 GHz Wi-Fi plus BLE; Waveshare Zero provides
5 GHz Wi-Fi. Other supported devices are assigned according to advertised
capabilities and the selected role.

The **P** preset toggles between split Wi-Fi/BLE work and a dedicated BLE scan
on a second ESP when two devices are available. ESP Wi-Fi and BLE
coexistence can be unstable. The separate host-BLE All Wardrive option keeps
ESP capture Wi-Fi-only and runs BlueZ on the uConsole; concurrent host BLE use
can also affect stability. For handshake helper captures, the helper follows
the target channel while receiving; Wi-Fi-only receive keeps BLE off.
Restoring the device's previous radio plan can leave an expected transition
gap. Device loss, lease expiry, or an uncertain command result is handled per
device and job. A healthy passive collector continues after its peer is lost.
A distributed foreground group stops its surviving members and requires a new
user start. Firmware reports external legacy ownership so that board is excluded
from helper allocation until fresh metadata confirms it is idle.

Per-device **Stop** stops that job, group **Stop** stops the selected job group,
and global `stop`/Stop All requests cleanup from every active device. A stop or
start is not considered complete from a UI timer alone; the host waits for the
job lifecycle result or its bounded timeout and compensates when command
delivery is uncertain. Terminal job results are cached by firmware so retries
can recover a response without starting the operation again.

## `c5_jobs_v1` transport

The host requests metadata with `get_device_info`. A capable board advertises
`c5_jobs_v1` and reports `device_id`, `boot_id`, board, flash/PSRAM capacity,
operations, channels, per-operation limits, and supported update methods.
Identity and capability queries use bounded serial writes. A write timeout or
partial write is reported as a USB transport error in the device health view;
the host retries missing metadata with a rate-limited backoff until a valid
reply arrives. Firmware reports `device_info_too_large` or
`device_info_serialization_failed` as a typed metadata error; WDG displays it in
health output and the terminal without treating it as a job error. During the
specific `boot_state=degraded`, `boot_reason=initializing`,
`c5_jobs_v1=false` startup state, WDG keeps identity and capability negotiation
on a rate-limited retry. It does not route that transient response through the
legacy scanner; permanent degraded recovery and firmware without this startup
marker keep their existing no-jobs/legacy handling. For a
standalone check, use a checkout containing this fix: the 0.10.4 diagnostic
still uses raw pySerial and `flush()`. Close WDG and other serial monitors,
then run `.venv/bin/python -m watchdogs.diagnose_firmware --port /dev/ttyACM0`
(replace the endpoint if it differs). The corrected diagnostic uses WDG's
native USB-JTAG line handling. WDG avoids pySerial `flush()`/POSIX `tcdrain()`
after command writes, which can wait indefinitely when native USB CDC stops
draining.

Commands remain newline-terminated console lines. A job starts with:

```text
job_start TOKEN BASE64URLJSON
```

`TOKEN` is 1–32 URL-safe alphanumeric/underscore/hyphen characters. The decoded
JSON request is at most 4096 bytes; the Base64 URL-safe representation omits
padding. WDG 0.10.3 serializes the boot nonce as lowercase hexadecimal so it
matches firmware through 1.8.1, while retaining uppercase identities internally.
Firmware 1.8.2 and newer also accept uppercase or mixed-case hexadecimal for the same
16-digit nonce; another boot's nonce remains invalid. Data and lifecycle records
use the existing payloads inside a `WDG:`
JSON envelope with protocol version 3, `device_id`, `boot_id`, job `session`
(the token), and a strictly increasing sequence number. Channel status may report `starting` while radio setup is in
progress; a `request_error` rejects a new start without confirming ownership.
Lines are limited to
1024 bytes; the console command buffer is 8192 bytes. A v3 wrapper does not
change the established Wi-Fi, BLE, batch, or handshake payload fields.

Jobs renew a 15-second firmware lease with `job_keepalive TOKEN` every five
seconds. The firmware emits a heartbeat every two seconds. If no valid job
record arrives for seven seconds, the host queries status and requests stop;
the firmware's 15-second lease is the final disconnect cleanup bound.
Use firmware 1.8.3 for repaired first combined Wi-Fi/BLE startup and immediate
external-job handoff after a stop. Replacement admission waits for the old
supervisor's last shared-state access; cleanup timeout retains ownership and
eventual terminal completion. Terminal
results are cached for the most recent four job tokens. A repeated identical
start is idempotent; a conflicting token/request or another active job returns
busy. If delivery is uncertain, the host checks status and attempts bounded
compensation rather than blindly starting another job. The firmware never
automatically resumes an active operation after USB disconnect or reboot.

## Bounds, captures, and files

Selected targets carry an explicit BSSID, channel, numeric authentication value
(0–255), and hex-encoded SSID. The host validates the entire immutable target
set against the assigned channel plan before dispatch; it does not truncate or
silently replace targets. Limits are 16 targets per WROOM/XIAO device, 8 per
Waveshare Zero, 32 exclusions per operation, and one SAE target per operation.
Operation-specific AP/client/probe limits come from device metadata.

The device panel reports aggregate drops and separate queue, age, output, and
saturation losses. Zero metadata also exposes its 64-entry observation queue,
eight 2304-byte passive frame slots, 48 KiB job allocation budget, and 32 KiB
minimum remaining internal heap. Allocation and radio setup must finish before
the board confirms that a job started; a memory rejection leaves it idle.

Each device, boot, and job owns its capture parser and artifact directory.
Handshake packets are streamed in chunks of at most 240 bytes and assembled
only within the matching device/boot/job. Host handshake validation follows the
firmware exchange validator's AP, station, and replay boundaries. A packet
classification or partial exchange is evidence of observation only; it does
not claim that a usable handshake was recovered. Existing WiGLE CSV output is
preserved, with source/provenance metadata written alongside each device's
capture artifacts.

## Board update profiles

| Profile | Flash / PSRAM | App slots | Full-install NVS | Release app limit | Update methods |
| --- | --- | --- | --- | --- | --- |
| XIAO ESP32-C5 | 8 MiB / reported by firmware | `0x20000` and `0x410000`, each `0x3F0000` | `0x9000` / `0x6000` | `0x3E0000` | USB OTA, ROM |
| ESP32-C5-WROOM-1 | 8 MiB / reported by firmware | `0x20000` and `0x410000`, each `0x3F0000` | `0x9000` / `0x6000` | `0x3E0000` | USB OTA, ROM |
| Waveshare ESP32-C5-Zero | 4 MiB / no PSRAM | `0x20000`, size `0x3E0000` | `0x9000` / `0x6000` | `0x3D0000` | ROM only |

Zero's generated layout also reserves partition metadata at `0xF000` (size
`0x2000`) and PHY data at `0x11000`; it is a single-app image without the
dual-slot USB-OTA layout. The Zero profile is unsupported by USB OTA. Package
format 2 manifests include generated board geometry, file offsets, sizes and
hashes, source dirty state, and a hash of the disposable build source. Existing
WROOM and XIAO artifact names remain; Zero adds a board-specific asset name.

Use WDG 0.10.1 or newer for firmware updates. The update reservation belongs to
the selected serial endpoint and its saved physical USB identity, including
legacy firmware and boards that have not answered capability queries. Normal
polling, discovery and global Stop leave that endpoint to the updater while
healthy peers continue. A hidden flash window retains its target and reservation
until it is reopened and closed after completion. Failed or stale claims stop
before handing off the connection; repeated release cannot close a replacement
session. Serial reads and close/reopen are synchronized so a disconnected
descriptor becomes a device disconnect instead of terminating WDG.

The complete WDG suite passed 1,232 tests, the firmware native harness passed
25 groups, and all three pinned ESP-IDF 6.0.1 builds and package checks passed.
Actual application sizes are 2,297,952 bytes for XIAO, 1,910,624 bytes for Zero,
and 2,301,520 bytes for WROOM. See [the measured software report](https://github.com/Smethan/projectZero/blob/v1.8.0/docs/MULTI_C5_VALIDATION.md).
Physical acceptance remains open; see [the multi-device hardware record](https://github.com/Smethan/projectZero/blob/v1.8.0/docs/HARDWARE_ACCEPTANCE_MULTI_C5.md).
