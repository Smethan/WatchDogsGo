"""Regression coverage for distro-specific setup package selection."""

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "setup_packages.sh"
AIO_HELPER = ROOT / "scripts" / "setup_aio_spi.sh"


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


def _missing_aio_spi_lines(config: Path, model: str) -> list[str]:
    result = subprocess.run(
        [
            "bash", "-c",
            'source "$1"; wdg_aio_spi_missing_lines "$2" "$3"',
            "bash", str(AIO_HELPER), str(config), model,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def test_cm4_aio_spi_setup_requires_spi_switch_and_spi1_overlay(tmp_path):
    config = tmp_path / "config.txt"
    config.write_text("# existing boot config\n")
    assert _missing_aio_spi_lines(
        config, "Raspberry Pi Compute Module 4 Rev 1.1") == [
            "dtparam=spi=on", "dtoverlay=spi1-1cs"]


def test_cm5_aio_spi_setup_requires_only_spi1_overlay(tmp_path):
    config = tmp_path / "config.txt"
    config.write_text("# existing boot config\n")
    assert _missing_aio_spi_lines(
        config, "Raspberry Pi Compute Module 5 Rev 1.0") == [
            "dtoverlay=spi1-1cs"]


def test_aio_spi_setup_is_idempotent_and_accepts_spacing(tmp_path):
    config = tmp_path / "config.txt"
    config.write_text(
        "dtparam = spi = on # enabled\n"
        "dtoverlay = spi1-1cs,cs0_pin=18\n")
    assert not _missing_aio_spi_lines(
        config, "Raspberry Pi Compute Module 4 Rev 1.1")


def test_setup_offers_explicit_aio_lora_configuration():
    setup = (ROOT / "setup.sh").read_text()
    assert 'source "$SCRIPT_DIR/scripts/setup_aio_spi.sh"' in setup
    assert 'WDG_ENABLE_AIO_LORA' in setup
    assert '/dev/spidev1.0' in setup
