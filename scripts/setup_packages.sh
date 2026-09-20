#!/bin/bash

# Package-selection helpers shared by setup.sh and its regression tests.

wdg_os_id() {
    local os_release_file="${WDG_OS_RELEASE_FILE:-/etc/os-release}"
    local key value

    [ -r "$os_release_file" ] || return 0

    while IFS='=' read -r key value; do
        if [ "$key" = "ID" ]; then
            value="${value#\"}"
            value="${value%\"}"
            value="${value#\'}"
            value="${value%\'}"
            printf '%s\n' "${value,,}"
            return 0
        fi
    done < "$os_release_file"
}

wdg_append_sdl_packages() {
    local -n packages_ref="$1"
    local os_id="${2,,}"

    # These development packages make dependency installation fail on Parrot.
    # Keep them for every other supported Debian-family distribution.
    case "$os_id" in
        parrot*) return 0 ;;
    esac

    packages_ref+=(libsdl2-dev libsdl2-image-dev)
}
