"""Regression coverage for distro-specific setup package selection."""

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "setup_packages.sh"


def _selected_sdl_packages(os_id: str) -> list[str]:
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; packages=(base); '
            'wdg_append_sdl_packages packages "$2"; printf "%s\\n" "${packages[@]}"',
            "bash",
            str(HELPER),
            os_id,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


@pytest.mark.parametrize("os_id", ["parrot", "PARROT", "parrotsec"])
def test_parrot_skips_sdl_development_packages(os_id):
    assert _selected_sdl_packages(os_id) == ["base"]


@pytest.mark.parametrize("os_id", ["debian", "ubuntu", "raspbian", ""])
def test_other_distributions_keep_sdl_development_packages(os_id):
    assert _selected_sdl_packages(os_id) == [
        "base",
        "libsdl2-dev",
        "libsdl2-image-dev",
    ]


def test_os_id_reads_override_file(tmp_path):
    os_release = tmp_path / "os-release"
    os_release.write_text('PRETTY_NAME="Parrot Security"\nID="Parrot"\nID_LIKE=debian\n')
    env = os.environ.copy()
    env["WDG_OS_RELEASE_FILE"] = str(os_release)
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; wdg_os_id', "bash", str(HELPER)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.stdout.strip() == "parrot"


def test_setup_uses_package_selection_helper():
    setup = (ROOT / "setup.sh").read_text()
    assert 'source "$SCRIPT_DIR/scripts/setup_packages.sh"' in setup
    assert 'wdg_append_sdl_packages CORE_PKGS "$OS_ID"' in setup
