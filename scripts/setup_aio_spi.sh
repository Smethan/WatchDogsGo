#!/bin/bash
# AIO v2 LoRa boot-configuration helpers for setup.sh.

# This file is sourced by setup.sh and by shell-level regression tests.  It
# only reports the required lines; the elevated setup script owns writes to
# the boot partition.

wdg_aio_spi_config_path() {
    if [ -n "${WDG_BOOT_CONFIG:-}" ]; then
        printf '%s\n' "$WDG_BOOT_CONFIG"
        return
    fi
    local candidate
    for candidate in /boot/firmware/config.txt /boot/config.txt; do
        if [ -f "$candidate" ]; then
            printf '%s\n' "$candidate"
            return
        fi
    done
    return 1
}

wdg_aio_spi_missing_lines() {
    local config="$1"
    local model="${2:-}"

    # CM4 needs the primary SPI switch enabled in addition to the SPI1
    # overlay.  Unknown Raspberry Pi models receive the conservative CM4
    # configuration; CM5 only needs the documented SPI1 overlay.
    if [[ ! "$model" =~ [Cc]ompute[[:space:]]+[Mm]odule[[:space:]]+5 ]] && \
       ! grep -Eq '^[[:space:]]*dtparam[[:space:]]*=[[:space:]]*spi[[:space:]]*=[[:space:]]*on([[:space:]]*(#.*)?)?$' "$config" 2>/dev/null; then
        printf '%s\n' 'dtparam=spi=on'
    fi
    if ! grep -Eq '^[[:space:]]*dtoverlay[[:space:]]*=[[:space:]]*spi1-1cs([,[:space:]]|$)' "$config" 2>/dev/null; then
        printf '%s\n' 'dtoverlay=spi1-1cs'
    fi
}
