# Updates from the Smethan forks

Both forks now use **main**, containing the complete feature/all-wardrive history.
The feature branch remains available as a historical checkpoint.

## One-time migration on an existing uConsole

Exit WDG. Inside the existing checkout, check `git remote -v` and make sure
origin is `https://github.com/Smethan/WatchDogsGo.git`. Then run:

```sh
git fetch origin
git merge --ff-only origin/main
bash update.sh
.venv/bin/python -m pip install 'esptool>=5.4,<6'
./run.sh
```

The first merge brings the updater into an older feature checkout; the updater
then switches that checkout to main. The pip command supplies the flasher
dependency missing from older installs; new installs get it from requirements.txt.
With WDG 0.9.20+, this pip step is optional: selecting Flash ESP32 automatically
prepares a private esptool environment when the running Python's tool is missing
or too old. That environment is reused; the app updater itself does not install it.
It refuses dirty tracked files, unexpected
repositories/branches, and commits that diverge from the published main branch.
It never resets, stashes, deletes or force-pushes user work. Untracked files are
left alone; Git still refuses a checkout if they would be overwritten.

For later updates choose **SYSTEM → Update WDG** or run `bash update.sh`.
Restart the program after updating. No device is flashed by the app updater.
When WDG runs as root, Git runs as the checkout directory's owner so new files
do not become root-owned. Dependency changes, if any, are documented in release
notes; the updater does not run arbitrary installers automatically.

### SIM7600 ownership migration for WDG 0.9.31+

Cell tracking and ModemManager-backed GNSS require ModemManager to be the only
process controlling the SIM7600. Older uConsole images install a boot service
that writes GPS commands directly to `/dev/ttyUSB3`; leaving that service in
place can race ModemManager and can make the modem or host unstable.

After updating WDG, exit the program and inspect the service without changing
it:

```sh
sudo scripts/migrate_uconsole_sim_service.sh --status
```

If it reports the legacy service, install the backed-up, power-only replacement:

```sh
sudo scripts/migrate_uconsole_sim_service.sh --apply
```

The helper prints the exact backup directory and does not restart the modem.
Reboot the uConsole once at a convenient time, then start WDG. To restore the
old definition, exit WDG and pass that directory to `--restore`; reboot again
after restoring.

Serving-cell recording is enabled by default during either All Wardrive mode.
The neighboring-cell probe is experimental and remains off until explicitly
enabled in **WARD SETTINGS**. It records candidates separately because the
SIM7600/QMI neighbor report does not include enough identity fields to create a
normal WiGLE cell row safely.

## Optional tools and dump1090

Startup reports missing optional dependencies without rerunning setup. To install
them, exit WDG and run `bash setup.sh` from the checkout once. Required startup
dependencies still trigger setup if missing.

Setup, startup checks and ADS-B radar share the same executable detection:
`dump1090`, `dump1090-fa`, or `dump1090-mutability`, on PATH or in standard system
binary directories (including `/usr/local/bin` when sudo omits it from PATH).
An existing executable is reused. If none is installed, setup builds FlightAware
dump1090 and installs it persistently as `/usr/local/bin/dump1090`. The build
dependencies include RTL-SDR, USB and ncurses development headers. A failed build
prints the end of its log and preserves the full log path for troubleshooting;
it is not retried automatically at the next WDG launch.

## Firmware

WDG 0.9.17 defaults All Wardrive to ESP32 Wi-Fi + BLE. The separate
All Wardrive (host BLE) option requires firmware 1.7.3+ for Wi-Fi-only serial
capture. ESP Dual Test can diagnose heartbeat behavior on
the previous serial-wardrive firmware before updating the ESP32. See
[All Wardrive and diagnostics](HOST_BLE_WARDRIVE.md).

**SYSTEM → Flash ESP32** shows the Smethan source and current/available version.
Choose XIAO for the XIAO ESP32-C5 USB board. **V/B** selects latest stable or an
explicit older published version. **Left/Right** selects Automatic or Manual
BOOT; the USB power cycle has been removed. See [flashing and rollback](FIRMWARE_FLASHING.md).
The downloader selects an exact ZIP from the selected stable Smethan/projectZero
release. It checks SHA256SUMS and the ZIP's board/version/file manifest before
invoking esptool. WDG's serial connection is paused throughout the wizard so
manual BOOT entry and retries are not disturbed by reconnects.
Each attempt uses a fresh cache directory. Missing assets or failed checks stop
the update; upstream binaries and stale cached files are never substituted.

The firmware ZIP includes the bootloader, partition table, initial OTA data and
application for the existing 8 MB layout. Flashing still requires working serial
access and may need the physical BOOT/RESET buttons depending on the hardware.
WDG 0.9.23 adds cellular-hotspot and resumable USB methods under [SYSTEM → OTA Update](FIRMWARE_OTA.md). Onboard firmware OTA is
fully configured after installing firmware 1.7.2 or later; 1.7.1 has an OTA
project-name validation mismatch, and older upstream firmware may still have
the LOCOSP OTA URL compiled into it. Stable/tagged
OTA uses the matching board's standalone application. Development-branch OTA is
disabled because this fork publishes versioned release assets.

## Publishing future updates

WDG code pushed to main is available to its Git updater immediately. For a named
source release, bump `watchdogs/__init__.py`, update `docs/RELEASE_NOTES.md`, commit
and push main, then push the matching `vX.Y.Z` tag. Tests must pass before Actions
publishes the release. Firmware has an independent version and workflow; see
Smethan/projectZero `docs/FORK_RELEASES.md`. Firmware is built on version tags,
not on every source push.

Both repositories are public. Workflows refuse private repositories and use
standard ubuntu-24.04 runners only, with timeouts and no caches or retained
Actions artifacts. Releases use GitHub hosting and the built-in GITHUB_TOKEN;
no personal access token, paid runner or external service is required.

Free-tier rules checked September 2026:
- https://docs.github.com/en/billing/concepts/product-billing/github-actions
- https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases
