# Patched gpsd for partial NMEA GSV visibility

WatchDogsGo uses this one-time Debian package rebuild for receivers that report
satellite signal strength before they know satellite azimuth.  Stock gpsd 3.25
parses an empty azimuth field as numeric zero and then clears a completed GSV
sky view when every azimuth is zero.  The local patch privately records whether
each zero came from an empty field and keeps that partial entry when the same
satellite has positive signal strength.  Literal `000` azimuth fields still
follow the original SiRFstarII garbage-rejection path, while gpsd's established
numeric sky-view and DOP output remain unchanged.

The recipe is intentionally pinned:

- Debian source: `gpsd 3.25-5+deb13u2`
- WDG binary version: `3.25-5+deb13u2+wdg1`
- Build architecture: `arm64`
- Required install pair: `gpsd` and `libgps30t64`

The `+wdg1` package sorts after Debian's `+deb13u2`, but a future Debian
`+deb13u3` sorts after it.  Do not place an apt hold on gpsd: a future official
security update should replace this package, at which point the patch must be
re-evaluated against that source.

## Build environment

Run `build-arm64.sh` inside a clean Debian 13 (trixie) ARM64 environment.  The
intended local builder is an ARM64 QEMU VM using Debian's official genericcloud
image.  Verify the downloaded image against Debian's published checksum before
booting it, copy or mount the WatchDogsGo checkout into the guest, and run the
script as a normal sudo-capable user.  A native ARM64 Debian 13 machine is also
valid.  Do not run the recipe on the uConsole: package compilation belongs on
the local builder.

The guest must have trixie binary and source repositories enabled so this exact
command resolves:

```sh
apt-get source gpsd=3.25-5+deb13u2
```

From the WatchDogsGo checkout in the ARM64 guest:

```sh
./packaging/gpsd/build-arm64.sh
```

By default, artifacts go to the checkout's sibling directory:

```text
../gpsd-build/3.25-5+deb13u2+wdg1/out/
```

The script refuses to reuse an existing build directory or place build output
inside the WatchDogsGo repository.  Pass a fresh absolute directory as its only
argument to override the default.  It verifies the source version, applies the
DEP-3 patch through Debian's quilt series, installs declared build dependencies,
injects the three focused fixtures into gpsd's normal regression glob, runs the
package's normal binary build, and then repeats the complete upstream test gate
as a hard failure.  That explicit repeat matters because Debian's gpsd rules
allow their first test invocation to fail.  The recipe then checks every ARM64
package's version and architecture, requires both install packages, and writes
`SHA256SUMS` beside the resulting packages.

## Focused upstream regression

The source-level change was tested with three synthetic NMEA fixtures using
gpsd's own `gpsfake`/`regress-driver` harness:

1. A completed three-satellite GSV set with empty azimuth fields and positive
   signal on two satellites emits `SKY` with `nSat:3`.
2. The same empty-azimuth form with every signal value zero remains rejected
   and emits no satellite `SKY` report.
3. A SiRF-style set with literal `000` azimuth fields and positive signal also
   remains rejected, preserving the original false-positive safeguard.

The fixtures and checked output live in `tests/` beside this recipe.  The build
script copies them into gpsd's `test/daemon/` directory so its normal regression
glob exercises all three cases.  Checked-output files are stored as ordinary LF
text in this repository and normalized to gpsd's CRLF wire-record format when
injected.  The patch also updates the checked output for the existing
`mtk-3301`, `tr737A+`, and `nl551e` no-fix traces: all three contain genuine
empty-angle, positive-signal GSV reports and now emit the partial `SKY` data.
The existing `tn200-all` SiRF trace uses explicit zero angles, remains unchanged,
and is still rejected.  The binary packages carry only the production driver
change; the checked-output hunks merely describe its intended effect.  Before
changing the guard or rebasing it to a newer gpsd release, rerun all fixtures
and the Debian package's complete test phase.

## Installation boundary

Install `gpsd` and `libgps30t64` from the same output directory in one apt
transaction; gpsd has an exact-version dependency on the library.  Preserve the
uConsole's `/etc/default/gpsd`, managed marker, and stock cached packages for
rollback.  Installation and device validation are deliberately separate from
this build recipe.
