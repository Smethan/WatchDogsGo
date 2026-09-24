#!/bin/bash
# Install the fixed, root-owned WatchDogsGo Meshtastic privilege boundary.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER_SOURCE="$SCRIPT_DIR/scripts/watchdogs_meshtastic_helper.py"
VALIDATOR_SOURCE="$SCRIPT_DIR/watchdogs/meshtastic_updates.py"
HELPER_TARGET="/usr/local/libexec/watchdogs-meshtastic"
LIB_TARGET="/usr/local/libexec/watchdogs-meshtastic-lib"
SUDOERS_TARGET="/etc/sudoers.d/watchdogs-meshtastic"
CONFIG_TARGET="/etc/meshtasticd/wdg-portduino.yaml"
CACHE_ROOT="/var/cache/watchdogs/meshtasticd-wdg"
BACKUP_ROOT="/var/backups/meshtasticd-wdg"

wdg_meshtastic_valid_user() {
    local user="$1" uid="$2"
    [[ "$user" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || return 1
    [[ "$uid" =~ ^[0-9]+$ ]] || return 1
    [ "$uid" -ne 0 ] || return 1
    id "$user" >/dev/null 2>&1 || return 1
    [ "$(id -u "$user")" = "$uid" ]
}

wdg_meshtastic_sudoers_text() {
    local user="$1"
    cat <<EOF
# WatchDogsGo: closed Meshtastic service/package helper.
Cmnd_Alias WDG_MESHTASTIC = \\
    $HELPER_TARGET status wdg, \\
    $HELPER_TARGET status stock, \\
    $HELPER_TARGET start wdg, \\
    $HELPER_TARGET start stock, \\
    $HELPER_TARGET stop wdg, \\
    $HELPER_TARGET stop stock, \\
    $HELPER_TARGET enable wdg, \\
    $HELPER_TARGET enable stock, \\
    $HELPER_TARGET disable wdg, \\
    $HELPER_TARGET disable stock, \\
    $HELPER_TARGET install-tag v*-wdg.*, \\
    $HELPER_TARGET rollback
$user ALL=(root) NOPASSWD: WDG_MESHTASTIC
EOF
}

wdg_meshtastic_config_text() {
    local uid="$1"
    cat <<EOF
# Linux-only policy for Smethan/meshtastic-firmware.
phone_ble:
  enabled: true
  adapter_address: auto
  pairing_window_seconds: 120
  max_bonds: 1
wdg_api:
  enabled: true
  socket_path: /run/meshtasticd/wdg.sock
  allowed_uid: $uid
full_client_policy:
  ble_priority: true
EOF
}

wdg_meshtastic_refuse_symlink() {
    local path="$1"
    if [ -L "$path" ]; then
        echo "Refusing symlink at protected path: $path" >&2
        return 1
    fi
}

wdg_install_meshtastic_support() {
    local user="$1" uid="$2"
    if [ "$(id -u)" -ne 0 ]; then
        echo "Run Meshtastic support setup with sudo." >&2
        return 1
    fi
    if ! wdg_meshtastic_valid_user "$user" "$uid"; then
        echo "Invalid non-root WatchDogsGo account: $user / $uid" >&2
        return 1
    fi
    [ -f "$HELPER_SOURCE" ] && [ ! -L "$HELPER_SOURCE" ] || {
        echo "Missing helper source: $HELPER_SOURCE" >&2; return 1; }
    [ -f "$VALIDATOR_SOURCE" ] && [ ! -L "$VALIDATOR_SOURCE" ] || {
        echo "Missing validator source: $VALIDATOR_SOURCE" >&2; return 1; }

    for path in "$HELPER_TARGET" "$LIB_TARGET" "$SUDOERS_TARGET" \
                "$CACHE_ROOT" "$BACKUP_ROOT" "$CONFIG_TARGET"; do
        wdg_meshtastic_refuse_symlink "$path"
    done

    install -d -o root -g root -m 0755 /usr/local/libexec
    install -d -o root -g root -m 0755 "$LIB_TARGET"
    install -o root -g root -m 0755 "$HELPER_SOURCE" "$HELPER_TARGET"
    install -o root -g root -m 0644 \
        "$VALIDATOR_SOURCE" "$LIB_TARGET/meshtastic_updates.py"
    install -d -o root -g root -m 0700 "$CACHE_ROOT" "$BACKUP_ROOT"

    install -d -o root -g root -m 0755 /etc/meshtasticd
    if [ ! -e "$CONFIG_TARGET" ]; then
        local config_temp
        config_temp="$(mktemp)"
        wdg_meshtastic_config_text "$uid" >"$config_temp"
        install -o root -g root -m 0600 "$config_temp" "$CONFIG_TARGET"
        rm -f "$config_temp"
    else
        [ -f "$CONFIG_TARGET" ] || {
            echo "Protected Meshtastic policy is not a regular file." >&2; return 1; }
        local config_owner config_mode
        config_owner="$(stat -c '%u:%g' "$CONFIG_TARGET")"
        config_mode="$(stat -c '%a' "$CONFIG_TARGET")"
        [ "${config_owner%%:*}" = "0" ] || {
            echo "Protected Meshtastic policy must be owned by root." >&2; return 1; }
        if (( 8#$config_mode & 8#022 )); then
            echo "Protected Meshtastic policy must not be group/other writable." >&2
            return 1
        fi
    fi

    local sudoers_temp
    sudoers_temp="$(mktemp)"
    wdg_meshtastic_sudoers_text "$user" >"$sudoers_temp"
    chmod 0440 "$sudoers_temp"
    visudo -cf "$sudoers_temp" >/dev/null
    install -o root -g root -m 0440 "$sudoers_temp" "$SUDOERS_TARGET"
    rm -f "$sudoers_temp"

    "$HELPER_TARGET" version >/dev/null
    echo "Meshtastic helper installed for $user (UID $uid)."
}

wdg_meshtastic_setup_main() {
    if [ "$#" -ne 3 ] || [ "$1" != "--install-support" ]; then
        echo "Usage: setup_meshtastic.sh --install-support LOGIN_USER LOGIN_UID" >&2
        return 2
    fi
    wdg_install_meshtastic_support "$2" "$3"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    wdg_meshtastic_setup_main "$@"
fi
