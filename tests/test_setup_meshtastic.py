"""Static/sourceable checks for the protected Meshtastic setup helper."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "setup_meshtastic.sh"
SETUP = ROOT / "setup.sh"


def bash_function(function: str, *args: str) -> str:
    command = [
        "bash", "-c", 'source "$1"; shift; "$@"',
        "bash", str(SCRIPT), function, *args,
    ]
    return subprocess.run(
        command, check=True, capture_output=True, text=True).stdout


def test_meshtastic_setup_scripts_have_valid_shell_syntax():
    subprocess.run(
        ["bash", "-n", str(SCRIPT), str(SETUP)],
        check=True, capture_output=True, text=True)


def test_sudoers_grants_only_the_closed_helper_interface():
    text = bash_function("wdg_meshtastic_sudoers_text", "sam")
    helper = "/usr/local/libexec/watchdogs-meshtastic"
    assert f"{helper} status wdg" in text
    assert f"{helper} status stock" in text
    for action in ("start", "stop", "enable", "disable"):
        assert f"{helper} {action} wdg" in text
        assert f"{helper} {action} stock" in text
    assert f"{helper} install-tag v*-wdg.*" in text
    assert f"{helper} rollback" in text
    assert "NOPASSWD: WDG_MESHTASTIC" in text
    assert "ALL=(ALL)" not in text
    assert "/bin/sh" not in text and "/bin/bash" not in text
    assert "systemctl *" not in text and "dpkg *" not in text


def test_portduino_policy_uses_resolved_login_uid_and_safe_defaults():
    text = bash_function("wdg_meshtastic_config_text", "1000")
    assert "enabled: true" in text
    assert "adapter_address: auto" in text
    assert "pairing_window_seconds: 120" in text
    assert "max_bonds: 1" in text
    assert "socket_path: /run/meshtasticd/wdg.sock" in text
    assert "allowed_uid: 1000" in text
    assert "ble_priority: true" in text


def test_setup_installs_support_without_downloading_or_starting_a_daemon():
    script = SCRIPT.read_text(encoding="utf-8")
    setup = SETUP.read_text(encoding="utf-8")
    assert 'sudo bash "$SCRIPT_DIR/scripts/setup_meshtastic.sh"' in setup
    assert '--install-support "$TARGET_USER" "$TARGET_UID"' in setup
    assert "/usr/local/libexec/watchdogs-meshtastic" in script
    assert "/var/cache/watchdogs/meshtasticd-wdg" in script
    assert "/var/backups/meshtasticd-wdg" in script
    assert "install -o root -g root -m 0755" in script
    assert "install -o root -g root -m 0644" in script
    assert "install -d -o root -g root -m 0700" in script
    assert "curl " not in script and "wget " not in script
    assert "systemctl start" not in script
    assert "dpkg --" not in script


def test_existing_parrot_package_selection_and_ownership_flow_remain():
    setup = SETUP.read_text(encoding="utf-8")
    assert 'wdg_append_sdl_packages CORE_PKGS "$OS_ID"' in setup
    assert 'if [[ "$OS_ID" == parrot* ]]' in setup
    assert "repair_checkout_ownership" in setup
    assert 'run_as_target mkdir -p loot maps plugins firmware_cache' in setup
