#!/usr/bin/env bash
set -euo pipefail

# Run this inside a clean Debian 13 (trixie) ARM64 environment.  The default
# output is deliberately outside the WatchDogsGo repository.

readonly SOURCE_VERSION='3.25-5+deb13u2'
readonly BINARY_VERSION='3.25-5+deb13u2+wdg1'
readonly REQUIRED_ARCH='arm64'
readonly PATCH_NAME='0001-nmea-preserve-zero-azimuth-satellites-with-signal.patch'

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_dir="$(cd -- "${script_dir}/../.." && pwd -P)"
workspace_dir="$(cd -- "${repo_dir}/.." && pwd -P)"
build_root="${1:-${workspace_dir}/gpsd-build/${BINARY_VERSION}}"

if [[ "$(dpkg --print-architecture)" != "${REQUIRED_ARCH}" ]]; then
    printf 'error: this recipe must run on Debian arm64 (found %s)\n' \
        "$(dpkg --print-architecture)" >&2
    exit 1
fi

if [[ "${build_root}" != /* ]]; then
    printf 'error: build directory must be an absolute path: %s\n' \
        "${build_root}" >&2
    exit 1
fi

case "${build_root}" in
    "${repo_dir}"|"${repo_dir}"/*)
        printf 'error: build output must stay outside the WatchDogsGo repo: %s\n' \
            "${build_root}" >&2
        exit 1
        ;;
esac

if [[ -e "${build_root}" ]]; then
    printf 'error: build directory already exists; choose a fresh path: %s\n' \
        "${build_root}" >&2
    exit 1
fi

mkdir -p -- "${build_root}/source" "${build_root}/out"

sudo apt-get update
sudo apt-get install --yes --no-install-recommends \
    build-essential \
    ca-certificates \
    devscripts \
    dpkg-dev \
    equivs \
    quilt

cd -- "${build_root}/source"
apt-get source "gpsd=${SOURCE_VERSION}"

mapfile -t source_dirs < <(find . -mindepth 1 -maxdepth 1 -type d -name 'gpsd-*' -print)
if [[ "${#source_dirs[@]}" -ne 1 ]]; then
    printf 'error: expected one unpacked gpsd source directory, found %d\n' \
        "${#source_dirs[@]}" >&2
    exit 1
fi
source_dir="$(cd -- "${source_dirs[0]}" && pwd -P)"
cd -- "${source_dir}"

actual_source_version="$(dpkg-parsechangelog -S Version)"
if [[ "${actual_source_version}" != "${SOURCE_VERSION}" ]]; then
    printf 'error: source version mismatch: expected %s, found %s\n' \
        "${SOURCE_VERSION}" "${actual_source_version}" >&2
    exit 1
fi

if grep -Fxq -- "${PATCH_NAME}" debian/patches/series; then
    printf 'error: Debian source already contains patch %s\n' "${PATCH_NAME}" >&2
    exit 1
fi
install -m 0644 -- "${script_dir}/patches/${PATCH_NAME}" \
    "debian/patches/${PATCH_NAME}"
printf '%s\n' "${PATCH_NAME}" >>debian/patches/series

# `apt-get source` leaves Debian's existing quilt series applied.  Adding a
# filename to `series` does not apply the new patch by itself, and the binary
# build does not guarantee that it will push a newly appended patch.  Apply it
# explicitly and fail before compilation if the pinned source no longer
# matches.
QUILT_PATCHES=debian/patches quilt push -a

# Keep the focused regression cases beside the packaging recipe, then inject
# them into gpsd's normal daemon-regression glob for this binary-only build.
for fixture in "${script_dir}"/tests/*.log "${script_dir}"/tests/*.log.chk; do
    fixture_target="test/daemon/$(basename -- "${fixture}")"
    install -m 0644 -- "${fixture}" "${fixture_target}"
    if [[ "${fixture}" == *.log.chk ]]; then
        # regress-driver records gpsd's CRLF wire output verbatim.  Keep the
        # repository fixtures reviewable as LF text and normalize the injected
        # golden files to the format used by upstream's daemon tests.
        sed -i 's/\r$//' "${fixture_target}"
        sed -i 's/$/\r/' "${fixture_target}"
    fi
done

changelog_tmp="$(mktemp --tmpdir="${source_dir}/debian" changelog.XXXXXX)"
{
    cat -- "${script_dir}/changelog.wdg"
    cat -- debian/changelog
} >"${changelog_tmp}"
chmod 0644 "${changelog_tmp}"
mv -- "${changelog_tmp}" debian/changelog

actual_binary_version="$(dpkg-parsechangelog -S Version)"
if [[ "${actual_binary_version}" != "${BINARY_VERSION}" ]]; then
    printf 'error: binary version mismatch: expected %s, found %s\n' \
        "${BINARY_VERSION}" "${actual_binary_version}" >&2
    exit 1
fi

sudo mk-build-deps --install --remove \
    --tool 'apt-get --yes --no-install-recommends' \
    debian/control

export DEB_BUILD_OPTIONS="parallel=$(nproc)"
dpkg-buildpackage --build=binary --unsigned-changes --unsigned-source

# Debian's gpsd packaging deliberately ignores a failing `scons check` in
# debian/rules so that architecture-specific test flakes do not abort official
# builds.  This patched build must be stricter: repeat the fully configured
# upstream gate and allow any regression failure to stop artifact publication.
(
    cd -- "${source_dir}"
    python3 /usr/bin/scons check "-j$(nproc)"
)

mapfile -t built_debs < <(find "${build_root}/source" -maxdepth 1 -type f \
    -name "*_${BINARY_VERSION}_${REQUIRED_ARCH}.deb" -print | sort)
if [[ "${#built_debs[@]}" -eq 0 ]]; then
    printf 'error: build produced no %s binary packages for version %s\n' \
        "${REQUIRED_ARCH}" "${BINARY_VERSION}" >&2
    exit 1
fi

for package in "${built_debs[@]}"; do
    package_version="$(dpkg-deb -f "${package}" Version)"
    package_arch="$(dpkg-deb -f "${package}" Architecture)"
    if [[ "${package_version}" != "${BINARY_VERSION}" || \
          "${package_arch}" != "${REQUIRED_ARCH}" ]]; then
        printf 'error: unexpected package metadata in %s: version=%s arch=%s\n' \
            "${package}" "${package_version}" "${package_arch}" >&2
        exit 1
    fi
    install -m 0644 -- "${package}" "${build_root}/out/"
done

for required_package in gpsd libgps30t64; do
    required_path="${build_root}/out/${required_package}_${BINARY_VERSION}_${REQUIRED_ARCH}.deb"
    if [[ ! -f "${required_path}" ]]; then
        printf 'error: required package was not built: %s\n' "${required_path}" >&2
        exit 1
    fi
done

changes_path="$(find "${build_root}/source" -maxdepth 1 -type f \
    -name "gpsd_${BINARY_VERSION}_${REQUIRED_ARCH}.changes" -print -quit)"
buildinfo_path="$(find "${build_root}/source" -maxdepth 1 -type f \
    -name "gpsd_${BINARY_VERSION}_${REQUIRED_ARCH}.buildinfo" -print -quit)"
for metadata_path in "${changes_path}" "${buildinfo_path}"; do
    if [[ -n "${metadata_path}" ]]; then
        install -m 0644 -- "${metadata_path}" "${build_root}/out/"
    fi
done

(
    cd -- "${build_root}/out"
    sha256sum -- * >SHA256SUMS
)

printf 'Built gpsd %s for %s\nArtifacts: %s\n' \
    "${BINARY_VERSION}" "${REQUIRED_ARCH}" "${build_root}/out"
