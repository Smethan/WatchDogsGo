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
WATCHDOGS_GROUP="watchdogs"
MESHTASTIC_GROUP="meshtasticd"
RADIO_LOCK_DIR="/run/lock/watchdogs"
RADIO_LOCK_PATH="$RADIO_LOCK_DIR/aio-sx1262.lock"
TRANSACTION_LOCK_PATH="$RADIO_LOCK_DIR/meshtastic-update.lock"
RADIO_TMPFILES_DIR="/etc/tmpfiles.d"
RADIO_TMPFILES_TARGET="$RADIO_TMPFILES_DIR/watchdogs-radio-lock.conf"

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
    $HELPER_TARGET select-service wdg, \\
    $HELPER_TARGET select-service stock, \\
    $HELPER_TARGET install-tag v*-wdg.*, \\
    $HELPER_TARGET prepare-first-tag v*-wdg.*, \\
    $HELPER_TARGET adopt-installed v*-wdg.*, \\
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

wdg_meshtastic_radio_tmpfiles_text() {
    cat <<EOF
d $RADIO_LOCK_DIR 2750 root $WATCHDOGS_GROUP -
f $RADIO_LOCK_PATH 0660 root $WATCHDOGS_GROUP -
f $TRANSACTION_LOCK_PATH 0600 root root -
EOF
}

wdg_meshtastic_rewrite_allowed_uid() {
    local source="$1" destination="$2" uid="$3"
    awk -v uid="$uid" '
        BEGIN {
            in_wdg_api = 0
            section_count = 0
            allowed_count = 0
            child_indent = 0
            allowed_indent = 0
            failed = 0
        }
        {
            lines[NR] = $0
            syntax = $0
            sub(/[[:space:]]*#.*/, "", syntax)
            if (syntax ~ /(^|[[:space:]])<<[[:space:]]*:/ ||
                    syntax ~ /(^|[[:space:]])[&*][A-Za-z0-9_-]+/ ||
                    syntax ~ /^[[:space:]]*-/ || syntax ~ /^[[:space:]]*\t/) {
                failed = 1
            }
        }
        /^wdg_api[[:space:]]*:/ {
            if ($0 !~ /^wdg_api:[[:space:]]*($|#)/)
                failed = 1
            section_count++
            in_wdg_api = 1
            next
        }
        in_wdg_api && /^[^[:space:]#]/ {
            in_wdg_api = 0
        }
        in_wdg_api && /^[[:space:]]*($|#)/ { next }
        in_wdg_api {
            match($0, /^[[:space:]]+/)
            indent = RLENGTH
            if (indent == 0) {
                failed = 1
                next
            }
            content = substr($0, indent + 1)
            if (content ~ /^[A-Za-z_][A-Za-z0-9_-]*[[:space:]]*:/ &&
                    (child_indent == 0 || indent < child_indent))
                child_indent = indent
            if (content ~ /^allowed_uid[[:space:]]*:/) {
                allowed_count++
                allowed_indent = indent
                allowed_line = NR
                if (content !~ /^allowed_uid:[[:space:]]*[0-9]+[[:space:]]*($|#)/)
                    failed = 1
            }
        }
        END {
            if (failed || section_count != 1 || allowed_count != 1 ||
                    child_indent == 0 || allowed_indent != child_indent) {
                exit 42
            }
            comment = lines[allowed_line]
            sub(/^[^#]*/, "", comment)
            prefix = substr(lines[allowed_line], 1, allowed_indent)
            lines[allowed_line] = prefix "allowed_uid: " uid
            if (comment ~ /^#/)
                lines[allowed_line] = lines[allowed_line] " " comment
            for (line = 1; line <= NR; line++)
                print lines[line]
        }
    ' "$source" >"$destination"
}

wdg_meshtastic_refuse_symlink() {
    local path="$1"
    if [ -L "$path" ]; then
        echo "Refusing symlink at protected path: $path" >&2
        return 1
    fi
}

wdg_meshtastic_warn_policy_restart() {
    local changed="$1"
    [ "$changed" -eq 1 ] || return 0
    if command -v systemctl >/dev/null 2>&1 && \
            systemctl is-active --quiet meshtasticd-wdg.service; then
        echo "Meshtastic WDG policy changed while the service is active." >&2
        echo "Apply it with: sudo systemctl restart meshtasticd-wdg.service" >&2
    fi
}

wdg_meshtastic_prepare_radio_access() {
    local user="$1" memberships membership_changed=0 tmpfiles_temp
    if ! getent group "$WATCHDOGS_GROUP" >/dev/null; then
        groupadd --system "$WATCHDOGS_GROUP"
    fi
    memberships=" $(id -nG "$user") "
    if [[ "$memberships" != *" $WATCHDOGS_GROUP "* ]]; then
        usermod --append --groups "$WATCHDOGS_GROUP" "$user"
        membership_changed=1
    fi
    install -d -o root -g "$WATCHDOGS_GROUP" -m 2750 "$RADIO_LOCK_DIR"
    install -d -o root -g root -m 0755 "$RADIO_TMPFILES_DIR"
    tmpfiles_temp="$(mktemp)"
    wdg_meshtastic_radio_tmpfiles_text >"$tmpfiles_temp"
    install -o root -g root -m 0644 \
        "$tmpfiles_temp" "$RADIO_TMPFILES_TARGET"
    rm -f "$tmpfiles_temp"
    systemd-tmpfiles --create "$RADIO_TMPFILES_TARGET"
    if [ "$membership_changed" -eq 1 ]; then
        echo "Added $user to $WATCHDOGS_GROUP; log out and back in before using the Meshtastic radio." >&2
    fi
}

wdg_install_meshtastic_support() {
    local user="$1" uid="$2" policy_changed=0
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
                "$CACHE_ROOT" "$BACKUP_ROOT" "$CONFIG_TARGET" \
                "$RADIO_LOCK_DIR" "$RADIO_LOCK_PATH" \
                "$TRANSACTION_LOCK_PATH" \
                "$RADIO_TMPFILES_DIR" "$RADIO_TMPFILES_TARGET"; do
        wdg_meshtastic_refuse_symlink "$path"
    done

    wdg_meshtastic_prepare_radio_access "$user"

    install -d -o root -g root -m 0755 /usr/local/libexec
    install -d -o root -g root -m 0755 "$LIB_TARGET"
    install -o root -g root -m 0755 "$HELPER_SOURCE" "$HELPER_TARGET"
    install -o root -g root -m 0644 \
        "$VALIDATOR_SOURCE" "$LIB_TARGET/meshtastic_updates.py"
    install -d -o root -g root -m 0700 "$CACHE_ROOT" "$BACKUP_ROOT"

    install -d -o root -g root -m 0755 /etc/meshtasticd
    local policy_group="root" policy_mode="0600"
    if getent group "$MESHTASTIC_GROUP" >/dev/null; then
        policy_group="$MESHTASTIC_GROUP"
        policy_mode="0640"
    fi
    if [ ! -e "$CONFIG_TARGET" ]; then
        local config_temp
        config_temp="$(mktemp)"
        wdg_meshtastic_config_text "$uid" >"$config_temp"
        install -o root -g "$policy_group" -m "$policy_mode" \
            "$config_temp" "$CONFIG_TARGET"
        policy_changed=1
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
        local config_temp
        config_temp="$(mktemp)"
        if ! wdg_meshtastic_rewrite_allowed_uid \
                "$CONFIG_TARGET" "$config_temp" "$uid"; then
            rm -f "$config_temp"
            echo "Protected Meshtastic policy must contain exactly one wdg_api.allowed_uid entry." >&2
            return 1
        fi
        if ! cmp -s "$config_temp" "$CONFIG_TARGET"; then
            policy_changed=1
        fi
        install -o root -g "$policy_group" -m "$policy_mode" \
            "$config_temp" "$CONFIG_TARGET"
        rm -f "$config_temp"
    fi
    wdg_meshtastic_warn_policy_restart "$policy_changed"

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
