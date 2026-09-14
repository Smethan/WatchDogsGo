#!/bin/sh
# Reversibly give ModemManager sole ownership of SIM7600 GNSS/control ports.
set -eu

UNIT=/etc/systemd/system/uconsole-sim.service
GPS_SCRIPT=/usr/local/bin/setup-sim-gps
BACKUP_ROOT=/var/backups/watchdogs

usage() {
    echo "Usage: $0 --status | --apply | --restore BACKUP_DIRECTORY"
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "Run this operation with sudo." >&2
        exit 1
    fi
}

status() {
    echo "Unit: $UNIT"
    if [ ! -f "$UNIT" ]; then
        echo "Status: missing"
        exit 1
    fi
    if grep -Eq 'setup-sim-gps|AT\+CGPS|ttyUSB[0-9]+' "$UNIT"; then
        echo "Status: legacy direct-port service detected"
        exit 2
    fi
    if grep -q '^ExecStart=/usr/bin/uconsole-4g enable$' "$UNIT" \
            && grep -q '^RemainAfterExit=yes$' "$UNIT"; then
        echo "Status: ModemManager-safe power-only service installed"
        exit 0
    fi
    echo "Status: unknown service definition; inspect before applying"
    exit 3
}

apply_service() {
    require_root
    if [ ! -x /usr/bin/uconsole-4g ]; then
        echo "/usr/bin/uconsole-4g is missing or not executable." >&2
        exit 1
    fi
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    mkdir -p "$BACKUP_ROOT"
    backup=$(mktemp -d "$BACKUP_ROOT/uconsole-sim-$stamp.XXXXXX")
    if [ -f "$UNIT" ]; then
        cp -a "$UNIT" "$backup/uconsole-sim.service"
    fi
    if [ -f "$GPS_SCRIPT" ]; then
        cp -a "$GPS_SCRIPT" "$backup/setup-sim-gps"
    fi
    # systemd-analyze uses the candidate's basename as the unit name and older
    # releases reject files without a recognized unit suffix.
    temp=$(mktemp --suffix=.service)
    trap 'rm -f "$temp"' EXIT HUP INT TERM
    cat >"$temp" <<'EOF'
[Unit]
Description=Power on ClockworkPi uConsole 4G extension
After=local-fs.target
Before=ModemManager.service
Wants=ModemManager.service

[Service]
Type=oneshot
ExecStart=/usr/bin/uconsole-4g enable
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    # Validate the candidate before replacing the live unit. A validation
    # failure therefore leaves both the installed service and modem state alone.
    systemd-analyze verify "$temp"
    install -o root -g root -m 0644 "$temp" "$UNIT"
    rm -f "$temp"
    trap - EXIT HUP INT TERM
    systemctl daemon-reload
    systemctl enable uconsole-sim.service
    echo "Installed the power-only service."
    echo "Backup: $backup"
    echo "A reboot is required. Do not restart the service while cellular data is in use."
}

restore_service() {
    require_root
    backup=${1:-}
    if [ -z "$backup" ] || [ ! -f "$backup/uconsole-sim.service" ]; then
        echo "Backup must contain uconsole-sim.service." >&2
        exit 1
    fi
    # Check a requested backup before it can replace the live unit.
    systemd-analyze verify "$backup/uconsole-sim.service"
    install -o root -g root -m 0644 "$backup/uconsole-sim.service" "$UNIT"
    if [ -f "$backup/setup-sim-gps" ]; then
        cp -a "$backup/setup-sim-gps" "$GPS_SCRIPT"
    fi
    systemctl daemon-reload
    systemctl enable uconsole-sim.service
    echo "Restored service files from $backup"
    echo "A reboot is required. Do not restart the service while cellular data is in use."
}

case ${1:-} in
    --status)
        status
        ;;
    --apply)
        apply_service
        ;;
    --restore)
        shift
        restore_service "${1:-}"
        ;;
    *)
        usage >&2
        exit 1
        ;;
esac
