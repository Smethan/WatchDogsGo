#!/bin/bash
# =============================================================================
# ESP32 Watch Dogs — Full Setup Script
# Creates venv, installs all dependencies, checks system requirements.
# Usage: ./setup.sh  (or called automatically by run.sh / install)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# shellcheck source=scripts/setup_packages.sh
source "$SCRIPT_DIR/scripts/setup_packages.sh"
# shellcheck source=scripts/setup_aio_spi.sh
source "$SCRIPT_DIR/scripts/setup_aio_spi.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}[OK]${NC} $1"; }
warn() { echo -e "  ${YELLOW}[!!]${NC} $1"; }
fail() { echo -e "  ${RED}[FAIL]${NC} $1"; }
info() { echo -e "  ${CYAN}[..]${NC} $1"; }

# All output of pip / apt is captured here so failures show real errors,
# not just "[error] setup.sh failed - see errors above" with nothing above.
PIP_LOG="/tmp/watchdogs-pip-$$.log"
APT_LOG="/tmp/watchdogs-apt-$$.log"
: > "$PIP_LOG"; : > "$APT_LOG"

dump_log_on_fail() {
    local what="$1" log="$2"
    fail "$what failed — last 25 lines of $log:"
    echo "----------------------------------------"
    tail -25 "$log"
    echo "----------------------------------------"
    echo "  Full log preserved at: $log"
}

echo ""
echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}  ESP32 Watch Dogs — Setup${NC}"
echo -e "${CYAN}========================================${NC}"
echo ""

ERRORS=0

# Resolve the account that owns this per-user checkout.  The setup script may
# run under sudo for apt/system installs, but Git, the venv and runtime data
# must remain owned by the login user.
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ] && id "$SUDO_USER" >/dev/null 2>&1 && [ "$(id -u "$SUDO_USER")" -ne 0 ]; then
    TARGET_USER="$SUDO_USER"
elif [ "$(id -u)" -ne 0 ]; then
    TARGET_USER="$(id -un)"
else
    fail "Cannot determine a non-root installation owner."
    fail "Log in as the intended user, then run: sudo bash setup.sh"
    exit 1
fi
TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_GROUP="$(id -gn "$TARGET_USER")"

run_as_target() {
    if [ "$(id -u)" -eq "$TARGET_UID" ]; then
        "$@"
    else
        sudo -n -H -u "$TARGET_USER" -- "$@"
    fi
}

ownership_mismatch_count() {
    find "$SCRIPT_DIR" -xdev \( ! -user "$TARGET_USER" -o ! -group "$TARGET_GROUP" \) -printf '.' 2>/dev/null | wc -c | tr -d '[:space:]'
}

repair_checkout_ownership() {
    local count
    count="$(ownership_mismatch_count)"
    if [ "${count:-0}" -eq 0 ]; then
        ok "Checkout ownership already correct ($TARGET_USER:$TARGET_GROUP)"
        return 0
    fi
    info "Repairing $count mixed-owner path(s) in the checkout..."
    if [ "$(id -u)" -eq 0 ]; then
        find "$SCRIPT_DIR" -xdev -exec chown -h "$TARGET_USER:$TARGET_GROUP" {} +
    else
        sudo find "$SCRIPT_DIR" -xdev -exec chown -h "$TARGET_USER:$TARGET_GROUP" {} +
    fi
    count="$(ownership_mismatch_count)"
    if [ "${count:-0}" -ne 0 ]; then
        fail "$count path(s) still have the wrong owner under $SCRIPT_DIR"
        return 1
    fi
    ok "Checkout ownership repaired ($TARGET_USER:$TARGET_GROUP)"
}

# Repair old sudo-created files before target-owned venv operations begin.
if ! repair_checkout_ownership; then
    exit 1
fi

# --- 0. Internet connectivity ---
echo "[0/8] Checking internet connectivity..."
if ping -c 1 -W 3 github.com &>/dev/null || ping -c 1 -W 3 1.1.1.1 &>/dev/null; then
    ok "Internet OK"
else
    fail "Cannot reach github.com or 1.1.1.1 — check network/DNS"
    exit 1
fi

# --- 1. Python 3 ---
echo "[1/8] Checking Python..."
if command -v python3 &>/dev/null; then
    PY_VER=$(python3 --version 2>&1)
    ok "Found $PY_VER"
else
    fail "python3 not found! Install Python 3.10+ (e.g. apt install python3)"
    exit 1
fi

# --- 2. venv module ---
echo "[2/8] Checking venv module..."
if python3 -c "import venv" 2>/dev/null; then
    ok "venv available"
else
    warn "venv not available — installing python3-venv..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y python3-venv >>"$APT_LOG" 2>&1 \
            && ok "Installed python3-venv" \
            || { dump_log_on_fail "python3-venv install" "$APT_LOG"; ERRORS=$((ERRORS+1)); }
    else
        fail "Install python3-venv manually"
        ERRORS=$((ERRORS + 1))
    fi
fi

# --- 3. System packages (apt) ---
# CRITICAL: must run BEFORE pip install so build deps are available for
# packages that compile from source (dbus-python, PyNaCl, sometimes pyxel
# on bleeding-edge Python on ARM64 where prebuilt wheels aren't available).
echo "[3/8] Installing system packages (build deps + libs + tools)..."
if command -v apt-get &>/dev/null; then
    # Core deps — required on every Debian/Ubuntu/RPi system
    CORE_PKGS=(
        # Build tooling and headers (for pip packages compiling from source)
        build-essential python3-dev python3-venv pkg-config curl ca-certificates
        # Dev headers needed by specific Python wheels:
        libdbus-1-dev libglib2.0-dev   # dbus-python
        libsodium-dev                  # PyNaCl
        libffi-dev libssl-dev          # cryptography, indirect deps
        # Native Python bindings (linked into venv in step 6)
        python3-gi gir1.2-glib-2.0     # BlueZ pairing agent
        # System tools shelled out to by the game
        tcpdump aircrack-ng iw rtl-433
        bluez bluez-tools pulseaudio-utils
        # Build deps for dump1090 (built from source in step 7)
        librtlsdr-dev libusb-1.0-0-dev libncurses-dev git
    )

    OS_ID="$(wdg_os_id)"
    wdg_append_sdl_packages CORE_PKGS "$OS_ID"
    if [[ "$OS_ID" == parrot* ]]; then
        info "Parrot OS detected — skipping libsdl2-dev and libsdl2-image-dev"
    fi

    # RPi-only packages — skipped on non-RPi systems (no fail)
    RPI_PKGS=(
        python3-rpi-lgpio python3-lgpio   # CM5/RPi5 GPIO for LoRa
        raspi-utils                       # provides pinctrl
    )

    SYS_PKGS=("${CORE_PKGS[@]}")
    if [ -f /sys/firmware/devicetree/base/model ] && \
       grep -qi 'raspberry\|clockwork' /sys/firmware/devicetree/base/model 2>/dev/null; then
        SYS_PKGS+=("${RPI_PKGS[@]}")
    fi

    MISSING=()
    for pkg in "${SYS_PKGS[@]}"; do
        dpkg -s "$pkg" &>/dev/null || MISSING+=("$pkg")
    done

    if [ ${#MISSING[@]} -gt 0 ]; then
        info "apt-get update..."
        sudo apt-get update >>"$APT_LOG" 2>&1 \
            || warn "apt-get update had errors (see $APT_LOG) — continuing"

        info "Installing ${#MISSING[@]} packages: ${MISSING[*]}"
        if sudo apt-get install -y "${MISSING[@]}" >>"$APT_LOG" 2>&1 ; then
            ok "System packages installed"
        else
            # Sometimes apt returns non-zero but most packages installed.
            # Re-check what's still missing for a clear error.
            STILL_MISSING=()
            for pkg in "${MISSING[@]}"; do
                dpkg -s "$pkg" &>/dev/null || STILL_MISSING+=("$pkg")
            done
            if [ ${#STILL_MISSING[@]} -gt 0 ]; then
                dump_log_on_fail "apt install" "$APT_LOG"
                fail "Still missing: ${STILL_MISSING[*]}"
                ERRORS=$((ERRORS + 1))
            else
                warn "apt reported errors but all packages present (see $APT_LOG)"
                ok "System packages installed"
            fi
        fi
    else
        ok "All system packages already present"
    fi
else
    warn "Not Debian/Ubuntu — install build deps + SDL2 manually"
fi

# --- 4. Virtual environment ---
echo "[4/8] Setting up virtual environment..."
if [ ! -d ".venv" ]; then
    info "Creating .venv..."
    run_as_target python3 -m venv .venv 2>/dev/null || run_as_target python3 -m venv .venv --without-pip
    ok "Created .venv"
else
    ok ".venv exists"
fi

if [ ! -f ".venv/bin/pip" ] && [ ! -f ".venv/bin/pip3" ]; then
    info "Bootstrapping pip..."
    curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
    run_as_target .venv/bin/python3 /tmp/get-pip.py --quiet
    rm -f /tmp/get-pip.py
    ok "pip installed"
fi

# --- 5. Python packages ---
echo "[5/8] Installing Python packages..."

# Run pip silently to keep output clean. On failure, dump the tail of the
# log — never silently swallow errors like the previous '--quiet 2>/dev/null'
# pattern did (that's why nobody could ever see why their install failed).
pip_run() {
    local desc="$1"; shift
    info "$desc"
    if run_as_target .venv/bin/pip "$@" >>"$PIP_LOG" 2>&1 ; then
        ok "$desc"
        return 0
    else
        dump_log_on_fail "$desc" "$PIP_LOG"
        return 1
    fi
}

if ! pip_run "Upgrading pip + wheel + setuptools" install --upgrade pip wheel setuptools ; then
    ERRORS=$((ERRORS + 1))
fi

if ! pip_run "Installing requirements.txt" install -r requirements.txt ; then
    fail "Common causes:"
    fail "  - missing apt build deps (re-check step [3] errors above)"
    fail "  - Python $PY_VER too new for some wheels"
    fail "  - intermittent network / PyPI timeout (re-run setup.sh)"
    ERRORS=$((ERRORS + 1))
fi

# --- 6. Link system Python modules into venv ---
# rpi-lgpio and python3-gi ship native .so files via apt that pip can't
# easily rebuild. Linking them in lets the venv use them directly.
# pip install LoRaRF pulls old RPi.GPIO 0.7.1 which doesn't know CM5 — we
# rm that copy and link the apt one in.
echo "[6/8] Linking system Python modules into venv..."
if [[ "$(uname)" == "Linux" ]]; then
    if [ -d "/usr/lib/python3/dist-packages/RPi" ] && \
       [ -f "/usr/lib/python3/dist-packages/RPi/GPIO/__init__.py" ]; then
        for sp in .venv/lib/python3.*/site-packages; do
            if [ -d "$sp" ]; then
                run_as_target rm -rf "$sp/RPi" "$sp/RPi.GPIO"* 2>/dev/null
                run_as_target ln -sf /usr/lib/python3/dist-packages/RPi "$sp/RPi"
                run_as_target ln -sf /usr/lib/python3/dist-packages/lgpio.py "$sp/lgpio.py" 2>/dev/null
                for so in /usr/lib/python3/dist-packages/_lgpio*.so; do
                    [ -f "$so" ] && run_as_target ln -sf "$so" "$sp/$(basename "$so")"
                done
                ok "rpi-lgpio linked into venv (LoRa GPIO)"
                break
            fi
        done
    else
        info "rpi-lgpio not present (non-RPi system) — skipping"
    fi

    if [ -d "/usr/lib/python3/dist-packages/gi" ]; then
        for sp in .venv/lib/python3.*/site-packages; do
            if [ -d "$sp" ] && [ ! -e "$sp/gi" ]; then
                run_as_target ln -sf /usr/lib/python3/dist-packages/gi "$sp/gi"
                for so in /usr/lib/python3/dist-packages/_gi*.so \
                          /usr/lib/python3/dist-packages/_gi_cairo*.so; do
                    [ -f "$so" ] && run_as_target ln -sf "$so" "$sp/$(basename "$so")"
                done
                for extra in pygobject_compat.py; do
                    f="/usr/lib/python3/dist-packages/$extra"
                    [ -f "$f" ] && run_as_target ln -sf "$f" "$sp/$extra"
                done
                ok "python3-gi linked into venv (BlueZ pairing)"
                break
            fi
        done
    fi
fi

# Verify required imports
MISSING_REQ=""
run_as_target .venv/bin/python3 -c "import pyxel" 2>/dev/null || MISSING_REQ="$MISSING_REQ pyxel"
run_as_target .venv/bin/python3 -c "import serial" 2>/dev/null || MISSING_REQ="$MISSING_REQ pyserial"
run_as_target .venv/bin/python3 -c "from PIL import Image" 2>/dev/null || MISSING_REQ="$MISSING_REQ Pillow"

if [ -z "$MISSING_REQ" ]; then
    ok "Required Python imports verified"
else
    fail "Missing required packages:$MISSING_REQ"
    fail "Check pip log: $PIP_LOG"
    ERRORS=$((ERRORS + 1))
fi

# Verify optional imports (advanced attacks + LoRa)
MISSING_OPT=""
run_as_target .venv/bin/python3 -c "import scapy" 2>/dev/null || MISSING_OPT="$MISSING_OPT scapy"
run_as_target .venv/bin/python3 -c "import netifaces" 2>/dev/null || MISSING_OPT="$MISSING_OPT netifaces"
run_as_target .venv/bin/python3 -c "import bleak" 2>/dev/null || MISSING_OPT="$MISSING_OPT bleak"
run_as_target .venv/bin/python3 -c "import dbus" 2>/dev/null || MISSING_OPT="$MISSING_OPT dbus-python"
run_as_target .venv/bin/python3 -c "from gi.repository import GLib" 2>/dev/null || MISSING_OPT="$MISSING_OPT python3-gi"
run_as_target .venv/bin/python3 -c "import LoRaRF" 2>/dev/null || MISSING_OPT="$MISSING_OPT LoRaRF"
run_as_target .venv/bin/python3 -c "import nacl" 2>/dev/null || MISSING_OPT="$MISSING_OPT PyNaCl"
run_as_target .venv/bin/python3 -c "import meshtastic" 2>/dev/null || MISSING_OPT="$MISSING_OPT meshtastic"

if [ -z "$MISSING_OPT" ]; then
    ok "Optional Python imports verified (all attacks available)"
else
    warn "Optional packages not available:$MISSING_OPT"
    warn "Some attacks or mesh clients may not work (MITM, Dragon Drain, BlueDucky, RACE, LoRa)"
fi

# --- 7. dump1090 from source + aiov2_ctl (uConsole only) ---
echo "[7/8] Checking dump1090 + uConsole tools..."

if DUMP1090_PATH=$(run_as_target python3 -m watchdogs.sdr_tools); then
    ok "dump1090 present ($DUMP1090_PATH) — reusing existing installation"
else
    info "Building FlightAware dump1090 from source..."
    TMP=$(mktemp -d)
    if git clone --depth=1 https://github.com/flightaware/dump1090.git "$TMP/dump1090" >>"$APT_LOG" 2>&1 \
       && (cd "$TMP/dump1090" && make -j"$(nproc)" >>"$APT_LOG" 2>&1) \
       && sudo install -m 755 "$TMP/dump1090/dump1090" /usr/local/bin/dump1090 >>"$APT_LOG" 2>&1 ; then
        ok "dump1090 installed (/usr/local/bin/dump1090)"
    else
        dump_log_on_fail "dump1090 build/install" "$APT_LOG"
        warn "dump1090 build failed — ADS-B Radar will not work (see $APT_LOG)"
    fi
    rm -rf "$TMP"
fi

# AIO v2 control (uConsole only)
if command -v pinctrl &>/dev/null && [ -f /sys/firmware/devicetree/base/model ]; then
    if ! command -v aiov2_ctl &>/dev/null; then
        info "Installing aiov2_ctl from GitHub (uConsole AIO v2 hardware)..."
        sudo apt-get install -y python3-pyqt6 git >>"$APT_LOG" 2>&1 || true
        TMP=$(mktemp -d)
        if git clone --depth=1 https://github.com/hackergadgets/aiov2_ctl.git "$TMP/aiov2_ctl" >>"$APT_LOG" 2>&1 \
           && (cd "$TMP/aiov2_ctl" && sudo python3 ./aiov2_ctl.py --install >>"$APT_LOG" 2>&1) ; then
            ok "aiov2_ctl installed"
        else
            warn "aiov2_ctl install failed — AIO toggles disabled (see $APT_LOG)"
        fi
        rm -rf "$TMP"
    else
        ok "aiov2_ctl present"
    fi

    # The LoRa power rail and its SPI transport are separate.  aiov2_ctl only
    # controls GPIO16; the kernel exposes the SX1262 after SPI1 is configured
    # in the boot file and the machine reboots.  Boot files are changed only
    # with an explicit opt-in because SPI1 pins may be used by another uConsole
    # accessory on systems without the AIO v2 board.
    if [ -e /dev/spidev1.0 ]; then
        ok "AIO LoRa SPI available (/dev/spidev1.0)"
    else
        AIO_SPI_CONFIG="$(wdg_aio_spi_config_path || true)"
        if [ "${WDG_ENABLE_AIO_LORA:-0}" = "1" ]; then
            if [ -z "$AIO_SPI_CONFIG" ] || [ ! -f "$AIO_SPI_CONFIG" ]; then
                warn "AIO LoRa requested, but no Raspberry Pi boot config was found"
                warn "Expected /boot/firmware/config.txt or /boot/config.txt"
            else
                AIO_MODEL="$(tr -d '\000' </sys/firmware/devicetree/base/model 2>/dev/null || true)"
                mapfile -t AIO_SPI_LINES < <(
                    wdg_aio_spi_missing_lines "$AIO_SPI_CONFIG" "$AIO_MODEL")
                if [ ${#AIO_SPI_LINES[@]} -gt 0 ]; then
                    AIO_SPI_BACKUP="${AIO_SPI_CONFIG}.wdg-before-aio-lora"
                    sudo test -e "$AIO_SPI_BACKUP" || \
                        sudo cp -a "$AIO_SPI_CONFIG" "$AIO_SPI_BACKUP"
                    {
                        printf '\n# WatchDogsGo: HackerGadgets AIO v2 LoRa (SPI1 CE0)\n'
                        printf '%s\n' "${AIO_SPI_LINES[@]}"
                    } | sudo tee -a "$AIO_SPI_CONFIG" >/dev/null
                    ok "Configured AIO LoRa SPI in $AIO_SPI_CONFIG"
                    warn "Reboot required before /dev/spidev1.0 and MeshCore are available"
                else
                    warn "AIO LoRa SPI boot settings are present, but /dev/spidev1.0 is missing"
                    warn "Reboot first; if it remains absent, verify the spi1-1cs overlay"
                fi
            fi
        else
            warn "AIO LoRa SPI device /dev/spidev1.0 is missing"
            warn "For an installed AIO v2, run: sudo WDG_ENABLE_AIO_LORA=1 bash setup.sh"
            warn "Then reboot the uConsole before enabling LoRa in WDG"
        fi
        if command -v systemctl >/dev/null 2>&1 && \
           systemctl is-enabled --quiet devterm-printer.service 2>/dev/null; then
            warn "devterm-printer.service may reserve SPI1; disable it if LoRa remains unavailable"
        fi
    fi
fi

# --- 8. Permissions and data directories ---
echo "[8/8] Permissions and data directories..."
[ -f "run.sh" ] && run_as_target chmod 755 run.sh
[ -f "setup.sh" ] && run_as_target chmod 755 setup.sh
[ -f "update.sh" ] && run_as_target chmod 755 update.sh
[ -f "watchdogs-launcher" ] && run_as_target chmod 755 watchdogs-launcher
ok "Scripts executable"

if [[ "$(uname)" == "Linux" ]]; then
    if id -nG "$TARGET_USER" 2>/dev/null | grep -qE '\b(dialout|tty)\b'; then
        ok "User '$TARGET_USER' in dialout/tty group (serial access)"
    else
        warn "User '$TARGET_USER' not in dialout group"
        warn "  Fix: sudo usermod -aG dialout $TARGET_USER  (then log out + log in)"
    fi
fi

run_as_target mkdir -p loot maps plugins firmware_cache
run_as_target chmod 755 loot maps plugins firmware_cache
ok "Data directories ready (loot, maps, plugins, firmware_cache)"

# Install only the root-owned, argument-allowlisted Meshtastic support helper.
# Firmware packages remain an explicit in-app install/update action and are
# revalidated by this helper before dpkg sees them.
if [[ "$(uname)" == "Linux" ]]; then
    info "Installing protected Meshtastic service/update helper..."
    if sudo bash "$SCRIPT_DIR/scripts/setup_meshtastic.sh" \
            --install-support "$TARGET_USER" "$TARGET_UID"; then
        ok "Meshtastic helper and private cache installed"
    else
        fail "Meshtastic helper setup failed"
        ERRORS=$((ERRORS + 1))
    fi
fi

if [ ! -f "secrets.conf" ] && [ -f "secrets.conf.example" ]; then
    run_as_target cp secrets.conf.example secrets.conf
    ok "secrets.conf created from template (edit to add API keys)"
fi
[ ! -f "secrets.conf" ] || run_as_target chmod 600 secrets.conf

# System-level steps above may have produced local files while elevated.
# Normalize once more, then verify using the target account rather than root.
if ! repair_checkout_ownership; then
    ERRORS=$((ERRORS + 1))
fi

permission_fail() {
    local path="$1" expectation="$2"
    fail "$path: expected $TARGET_USER:$TARGET_GROUP and $expectation"
    stat -c "  actual: %U:%G mode %a %n" "$path" 2>/dev/null || true
    fail "Repair: cd $SCRIPT_DIR && sudo bash setup.sh"
    ERRORS=$((ERRORS + 1))
}

for path in "$SCRIPT_DIR" "$SCRIPT_DIR/.git"; do
    if [ ! -e "$path" ] || ! run_as_target test -w "$path"; then
        permission_fail "$path" "writable by $TARGET_USER"
    fi
done
if [ -e "$SCRIPT_DIR/.git/index" ] && ! run_as_target test -w "$SCRIPT_DIR/.git/index"; then
    permission_fail "$SCRIPT_DIR/.git/index" "writable by $TARGET_USER"
fi
if [ ! -x "$SCRIPT_DIR/.venv/bin/python3" ] || ! run_as_target test -x "$SCRIPT_DIR/.venv/bin/python3"; then
    permission_fail "$SCRIPT_DIR/.venv/bin/python3" "an executable target-owned Python"
fi
for path in loot maps plugins firmware_cache; do
    if ! run_as_target test -w "$SCRIPT_DIR/$path"; then
        permission_fail "$SCRIPT_DIR/$path" "writable by $TARGET_USER"
    fi
done
if [ -f "$SCRIPT_DIR/secrets.conf" ]; then
    secret_owner="$(stat -c '%U:%G' "$SCRIPT_DIR/secrets.conf")"
    secret_mode="$(stat -c '%a' "$SCRIPT_DIR/secrets.conf")"
    if [ "$secret_owner" != "$TARGET_USER:$TARGET_GROUP" ] || [ "$secret_mode" != "600" ]; then
        permission_fail "$SCRIPT_DIR/secrets.conf" "$TARGET_USER:$TARGET_GROUP mode 600"
    fi
fi
remaining="$(ownership_mismatch_count)"
if [ "${remaining:-0}" -ne 0 ]; then
    fail "$remaining mixed-owner path(s) remain in $SCRIPT_DIR"
    find "$SCRIPT_DIR" -xdev \( ! -user "$TARGET_USER" -o ! -group "$TARGET_GROUP" \) -printf '  %u:%g %m %p\n' 2>/dev/null | head -20 || true
    fail "Repair: cd $SCRIPT_DIR && sudo bash setup.sh"
    ERRORS=$((ERRORS + 1))
else
    ok "Ownership verified for Git, venv, config and runtime data"
fi

# --- Summary ---
echo ""
if [ $ERRORS -eq 0 ]; then
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}  Setup complete! No errors.${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo ""
    echo "  Run the game:  sudo ./run.sh"
    echo ""
    rm -f "$PIP_LOG" "$APT_LOG" 2>/dev/null
else
    echo -e "${RED}========================================${NC}"
    echo -e "${RED}  Setup finished with $ERRORS error(s)${NC}"
    echo -e "${RED}========================================${NC}"
    echo ""
    echo "  Logs preserved for debugging:"
    [ -s "$PIP_LOG" ] && echo "    pip:  $PIP_LOG"
    [ -s "$APT_LOG" ] && echo "    apt:  $APT_LOG"
    echo ""
    echo "  Fix the errors above, then re-run: sudo bash setup.sh"
    echo ""
    exit 1
fi
