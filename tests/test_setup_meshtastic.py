"""Static/sourceable checks for the protected Meshtastic setup helper."""

import os
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


def run_mocked_radio_access(
        tmp_path: Path, *, group_exists: bool, memberships: str,
        ) -> tuple[list[str], str]:
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    log = tmp_path / "commands.log"

    def command(name: str, body: str = "exit 0\n") -> None:
        path = mock_bin / name
        path.write_text(
            "#!/bin/sh\n"
            f"printf '{name}:%s\\n' \"$*\" >>\"$MOCK_LOG\"\n"
            + body,
            encoding="utf-8",
        )
        path.chmod(0o755)

    command(
        "getent",
        '[ "${MOCK_GROUP_EXISTS:-0}" = 1 ]\n',
    )
    command("groupadd")
    command("usermod")
    command("install")
    command("systemd-tmpfiles")
    id_path = mock_bin / "id"
    id_path.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = -nG ]; then\n"
        "  printf '%s\\n' \"$MOCK_MEMBERSHIPS\"\n"
        "  exit 0\n"
        "fi\n"
        "exit 64\n",
        encoding="utf-8",
    )
    id_path.chmod(0o755)

    lock_dir = tmp_path / "run-lock-watchdogs"
    tmpfiles = tmp_path / "watchdogs-radio-lock.conf"
    env = os.environ.copy()
    env.update({
        "PATH": str(mock_bin) + ":/usr/bin:/bin",
        "MOCK_LOG": str(log),
        "MOCK_GROUP_EXISTS": "1" if group_exists else "0",
        "MOCK_MEMBERSHIPS": memberships,
    })
    result = subprocess.run(
        [
            "bash", "-c",
            ('source "$1"; WATCHDOGS_GROUP=watchdogs; '
             'RADIO_LOCK_DIR="$2"; '
             'RADIO_LOCK_PATH="$2/aio-sx1262.lock"; '
             'TRANSACTION_LOCK_PATH="$2/meshtastic-update.lock"; '
             'RADIO_TMPFILES_TARGET="$3"; '
             'wdg_meshtastic_prepare_radio_access sam'),
            "bash", str(SCRIPT), str(lock_dir), str(tmpfiles),
        ],
        check=True, capture_output=True, text=True, env=env,
    )
    return log.read_text(encoding="utf-8").splitlines(), result.stderr


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
    assert f"{helper} select-service wdg" in text
    assert f"{helper} select-service stock" in text
    assert f"{helper} install-tag v*-wdg.*" in text
    assert f"{helper} adopt-installed v*-wdg.*" in text
    assert f"{helper} rollback" in text
    assert "NOPASSWD: WDG_MESHTASTIC" in text
    assert "ALL=(ALL)" not in text
    assert "/bin/sh" not in text and "/bin/bash" not in text
    assert "systemctl *" not in text and "dpkg *" not in text


def test_radio_access_setup_creates_group_appends_user_and_repairs_lock_dir(
        tmp_path):
    tmpfiles = tmp_path / "watchdogs-radio-lock.conf"
    calls, stderr = run_mocked_radio_access(
        tmp_path, group_exists=False, memberships="sam adm dialout")

    assert "groupadd:--system watchdogs" in calls
    assert "usermod:--append --groups watchdogs sam" in calls
    assert any(
        call.startswith("install:-d -o root -g watchdogs -m 2750 ")
        for call in calls)
    assert any(
        call.startswith("install:-o root -g root -m 0644 ")
        and call.endswith(" " + str(tmpfiles))
        for call in calls)
    assert "systemd-tmpfiles:--create " + str(tmpfiles) in calls
    assert "log out and back in" in stderr
    assert not any("adm" in call or "dialout" in call for call in calls)


def test_radio_access_setup_is_idempotent_for_existing_membership(tmp_path):
    calls, stderr = run_mocked_radio_access(
        tmp_path, group_exists=True,
        memberships="sam adm watchdogs dialout")

    assert not any(call.startswith("groupadd:") for call in calls)
    assert not any(call.startswith("usermod:") for call in calls)
    assert any(
        call.startswith("install:-d -o root -g watchdogs -m 2750 ")
        for call in calls)
    assert "log out and back in" not in stderr


def test_portduino_policy_uses_resolved_login_uid_and_safe_defaults():
    text = bash_function("wdg_meshtastic_config_text", "1000")
    assert "enabled: true" in text
    assert "adapter_address: auto" in text
    assert "pairing_window_seconds: 120" in text
    assert "max_bonds: 1" in text
    assert "socket_path: /run/meshtasticd/wdg.sock" in text
    assert "allowed_uid: 1000" in text
    assert "ble_priority: true" in text


def test_radio_lock_tmpfiles_policy_is_persistent_and_inode_safe():
    text = bash_function("wdg_meshtastic_radio_tmpfiles_text")
    assert text == (
        "d /run/lock/watchdogs 2750 root watchdogs -\n"
        "f /run/lock/watchdogs/aio-sx1262.lock 0660 root watchdogs -\n"
        "f /run/lock/watchdogs/meshtastic-update.lock 0600 root root -\n"
    )


def test_policy_change_warns_when_active_service_needs_restart(tmp_path):
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    systemctl = mock_bin / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        "[ \"$1 $2 $3\" = \"is-active --quiet meshtasticd-wdg.service\" ]\n",
        encoding="utf-8")
    systemctl.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(mock_bin) + ":/usr/bin:/bin"
    result = subprocess.run(
        [
            "bash", "-c",
            'source "$1"; wdg_meshtastic_warn_policy_restart "$2"',
            "bash", str(SCRIPT), "1",
        ],
        check=True, capture_output=True, text=True, env=env,
    )
    assert "policy changed" in result.stderr
    assert "sudo systemctl restart meshtasticd-wdg.service" in result.stderr


def test_unchanged_policy_never_prompts_for_service_restart(tmp_path):
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    systemctl = mock_bin / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    systemctl.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(mock_bin) + ":/usr/bin:/bin"
    result = subprocess.run(
        [
            "bash", "-c",
            'source "$1"; wdg_meshtastic_warn_policy_restart "$2"',
            "bash", str(SCRIPT), "0",
        ],
        check=True, capture_output=True, text=True, env=env,
    )
    assert result.stderr == ""


def test_existing_policy_updates_only_the_allowed_login_uid(tmp_path):
    source = tmp_path / "policy.yaml"
    output = tmp_path / "updated.yaml"
    source.write_text(
        "# retained comment\n"
        "phone_ble:\n"
        "  enabled: false\n"
        "  adapter_address: AA:BB:CC:DD:EE:FF\n"
        "wdg_api:\n"
        "  enabled: true\n"
        "  socket_path: /run/meshtasticd/wdg.sock\n"
        "  allowed_uid: 1000 # old account\n"
        "full_client_policy:\n"
        "  ble_priority: true\n",
        encoding="utf-8",
    )
    bash_function(
        "wdg_meshtastic_rewrite_allowed_uid",
        str(source), str(output), "1001")
    text = output.read_text(encoding="utf-8")
    assert "# retained comment" in text
    assert "enabled: false" in text
    assert "adapter_address: AA:BB:CC:DD:EE:FF" in text
    assert "allowed_uid: 1001" in text
    assert "1000" not in text


def test_existing_policy_rejects_missing_or_duplicate_allowed_uid(tmp_path):
    for index, text in enumerate((
        "wdg_api:\n  enabled: true\n",
        "wdg_api:\n  allowed_uid: 1000\n  allowed_uid: 1001\n",
        "wdg_api:\n  enabled: true\nwdg_api:\n  allowed_uid: 1000\n",
        "wdg_api: {allowed_uid: 1000}\n",
        "wdg_api:\n  nested:\n    allowed_uid: 1000\n",
        "wdg_api: &shared\n  allowed_uid: 1000\n",
        "wdg_api:\n  defaults: *shared\n  allowed_uid: 1000\n",
        "wdg_api:\n  <<: *shared\n  allowed_uid: 1000\n",
        "wdg_api:\n  allowed_uid: [1000]\n",
    )):
        source = tmp_path / f"policy-{index}.yaml"
        output = tmp_path / f"updated-{index}.yaml"
        source.write_text(text, encoding="utf-8")
        command = [
            "bash", "-c", 'source "$1"; shift; "$@"',
            "bash", str(SCRIPT), "wdg_meshtastic_rewrite_allowed_uid",
            str(source), str(output), "1002",
        ]
        result = subprocess.run(
            command, check=False, capture_output=True, text=True)
        assert result.returncode != 0


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
    assert 'wdg_meshtastic_prepare_radio_access "$user"' in script
    assert 'usermod --append --groups "$WATCHDOGS_GROUP" "$user"' in script
    assert 'install -d -o root -g "$WATCHDOGS_GROUP" -m 2750' in script
    assert 'systemd-tmpfiles --create "$RADIO_TMPFILES_TARGET"' in script
    assert 'policy_group="$MESHTASTIC_GROUP"' in script
    assert 'policy_mode="0640"' in script
    assert 'policy_group="root" policy_mode="0600"' in script
    assert "curl " not in script and "wget " not in script
    assert "systemctl start" not in script
    assert "dpkg --" not in script


def test_existing_parrot_package_selection_and_ownership_flow_remain():
    setup = SETUP.read_text(encoding="utf-8")
    assert 'wdg_append_sdl_packages CORE_PKGS "$OS_ID"' in setup
    assert 'if [[ "$OS_ID" == parrot* ]]' in setup
    assert "repair_checkout_ownership" in setup
    assert 'run_as_target mkdir -p loot maps plugins firmware_cache' in setup
